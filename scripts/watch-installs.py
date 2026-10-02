#!/usr/bin/env python3
"""Watch DSH profiles for plugin installs and prune the pnpm store afterwards.

pnpm has no usable hook for this (it does not run the root project's postinstall on
`add` -- verified -- and its own `store prune` is a no-op on this store format), so the
trigger has to come from outside:

  * a *trigger* is a change to a profile's package.json / pnpm-lock.yaml /
    node_modules/.modules.yaml -- that is DSH's plugin manager (or dshmarket) finishing
    or starting an install;
  * the store itself (index.db + files/) is watched for *quiescence only*, never as a
    trigger, so this process cannot re-trigger itself after its own cleanup run;
  * nothing runs until every watched path has been untouched for --quiet-seconds, which
    keeps a slow install (lockfile written first, linking still in progress) safe;
  * --cooldown puts a floor between two cleanup runs.

Safety net: a run is skipped while a pnpm process is alive (a half-finished install can
leave store files that are momentarily unreferenced), and if the store index is locked by
a concurrent pnpm process the engine itself refuses (exit 3); the trigger is re-armed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "prune-orphans.py")
MANIFEST_NAMES = ("package.json", "pnpm-lock.yaml")
STORE_FILES = "files"


def load_engine():
    spec = importlib.util.spec_from_file_location("prune_orphans_engine", ENGINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profiles() -> list[str]:
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    root = os.path.join(home, "profiles")
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, n) for n in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, n))]


def manifest_paths(extra: list[str]) -> list[str]:
    out = list(extra)
    for profile in profiles():
        for name in MANIFEST_NAMES:
            out.append(os.path.join(profile, name))
        out.append(os.path.join(profile, "node_modules", ".modules.yaml"))
    return out


def store_paths(engine) -> list[str]:
    out = []
    for profile in profiles():
        store = engine.store_from_modules_yaml(profile)
        if not store:
            store = engine.default_store_for(profile)
        for path in (os.path.join(store, "index.db"), os.path.join(store, STORE_FILES)):
            if path not in out:
                out.append(path)
    return out


def stamp(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def snapshot(paths: list[str]) -> dict:
    return {p: stamp(p) for p in paths}


class Logger:
    def __init__(self, path: str | None):
        self.path = path
        if path:
            os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        if not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 1_000_000:
                os.replace(self.path, self.path + ".old")
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


PNPM_RE = re.compile(r'(^|[\\/"\s])pnpm(\.cmd|\.bat|\.exe|\.cjs)?("|\s|$)')


def pnpm_busy() -> list[str]:
    """Command lines of running pnpm processes; empty when Windows cannot tell us."""
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    return [line for line in (proc.stdout or "").splitlines()
            if line and PNPM_RE.search(line)]


def run_engine(execute: bool, log: Logger, logfile: str | None) -> int:
    cmd = [sys.executable, ENGINE, "--quiet", "--json"]
    if execute:
        cmd.append("--execute")
    if logfile:
        cmd += ["--log", logfile]
    log(f"running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        log("engine timed out after 3600s")
        return 1
    tail = (proc.stdout or "").strip().splitlines()
    log(f"engine exit={proc.returncode} {tail[-1] if tail else ''}")
    if proc.stderr and proc.stderr.strip():
        log("engine stderr: " + proc.stderr.strip()[-500:])
    return proc.returncode


def watch(args) -> None:
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    logfile = args.log or os.path.join(HERE, "logs", "watcher.log")
    log = Logger(logfile)
    engine = load_engine()

    manifests = manifest_paths(args.watch)
    stores = store_paths(engine)
    log(f"watching {len(manifests)} manifest paths and {len(stores)} store paths")
    log(f"mode={'EXECUTE' if args.execute else 'dry-run'} quiet={args.quiet_seconds}s "
        f"interval={args.interval}s cooldown={args.cooldown}s profiles={home}\\profiles")

    man_state = snapshot(manifests)
    store_state = snapshot(stores)
    armed = False
    last_change = 0.0
    last_run = 0.0

    while True:
        time.sleep(args.interval)
        now = time.time()

        new_man = snapshot(manifests)
        if new_man != man_state:
            changed = [p for p, v in new_man.items() if man_state.get(p) != v]
            man_state = new_man
            for p in changed:
                log(f"install activity: {p}")
            armed = True
            last_change = now

        new_store = snapshot(stores)
        if new_store != store_state:
            store_state = new_store
            last_change = now

        if not armed:
            continue
        if now - last_change < args.quiet_seconds:
            continue
        if now - last_run < args.cooldown:
            continue

        busy = pnpm_busy()
        if busy:
            log(f"pnpm is still running ({len(busy)} process(es)); re-arming instead of pruning")
            armed = True
            last_change = now
            continue

        armed = False
        last_run = now
        code = run_engine(args.execute, log, logfile)
        if code == 3:
            log("store was busy or refused a safety rail; re-arming")
            armed = True
            last_change = now
        if args.once:
            log("--once: exiting after the first cleanup")
            return


def main() -> int:
    ap = argparse.ArgumentParser(description="watch DSH plugin installs and prune orphans afterwards")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--dry-run", action="store_true", help="report only; this is already the default")
    ap.add_argument("--quiet-seconds", type=int, default=90)
    ap.add_argument("--cooldown", type=int, default=600)
    ap.add_argument("--interval", type=int, default=20)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="append", default=[], help="extra manifests to watch (testing)")
    ap.add_argument("--log", help="watcher log path")
    args = ap.parse_args()

    while True:
        try:
            watch(args)
            return 0
        except KeyboardInterrupt:
            return 0
        except Exception:
            Logger(args.log or os.path.join(HERE, "logs", "watcher.log"))(
                "watcher crashed, restarting in 60s:\n" + traceback.format_exc())
            if args.once:
                return 1
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
