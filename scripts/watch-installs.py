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

State survives restarts: the last manifest snapshot, the "a cleanup is still pending"
flag and the last run time are kept in logs/watch-state.json. `dsh plugin add` restarts
the profile, which replaces this process; a fresh process that simply re-baselined the
manifests would score the install that caused the restart as "always been like that"
and never clean up after it.

Safety net: a run is skipped while a pnpm process is alive (a half-finished install can
leave store files that are momentarily unreferenced), and if the store index is locked by
a concurrent pnpm process the engine itself refuses (exit 3); the trigger is re-armed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
STATE_VERSION = 1


def load_engine(path: str = ENGINE):
    spec = importlib.util.spec_from_file_location("prune_orphans_engine", path)
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


def state_path_for(args) -> str:
    """Explicit --state wins; otherwise live next to the log this instance writes."""
    if args.state:
        return args.state
    base = os.path.dirname(args.log) if args.log else os.path.join(HERE, "logs")
    return os.path.join(base, "watch-state.json")


def fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


def load_state(path: str) -> dict | None:
    """Trigger state left by an earlier watcher instance, or None when unusable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return None
    raw = data.get("manifests")
    if not isinstance(raw, dict):
        return None
    manifests = {}
    for key, value in raw.items():
        # json has no tuples: a stamp comes back as [mtime_ns, size], None stays None.
        manifests[str(key)] = (value[0], value[1]) if isinstance(value, list) and len(value) == 2 else None
    try:
        last_run = float(data.get("last_run") or 0.0)
    except (TypeError, ValueError):
        last_run = 0.0
    return {"manifests": manifests, "pending": bool(data.get("pending")), "last_run": last_run}


def save_state(path: str, manifests: dict, pending: bool, last_run: float) -> None:
    """Record what has already been seen -- atomically, so a reader never sees half a file."""
    payload = {
        "version": STATE_VERSION,
        "pending": bool(pending),
        "last_run": float(last_run),
        "saved": time.time(),
        "manifests": {k: (list(v) if v else None) for k, v in manifests.items()},
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


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


def run_engine(engine_path: str, execute: bool, log: Logger, logfile: str | None) -> int:
    cmd = [sys.executable, engine_path, "--quiet", "--json"]
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
    engine_path = args.engine or ENGINE
    engine = load_engine(engine_path)
    statefile = state_path_for(args)

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

    prior = load_state(statefile)
    if prior is None:
        # Nothing to inherit: this process defines the baseline. Persist it anyway, so
        # the *next* restart has something to compare against.
        log(f"watch state: none usable at {statefile}; taking a fresh baseline")
        save_state(statefile, man_state, False, last_run)
    else:
        # Compare against what the previous instance had already seen instead of the
        # current manifests: that is what keeps an install that landed across a restart
        # (every `dsh plugin add` restarts the profile) visible to this instance.
        man_state = prior["manifests"]
        armed = prior["pending"]
        last_run = prior["last_run"]
        log(f"watch state: restored from {statefile} ({len(man_state)} paths, "
            f"pending={armed}, last run {fmt_time(last_run)})")
        if armed:
            # A restart is not evidence that the install finished; re-apply a full quiet
            # window before touching the store again.
            last_change = time.time()
            log(f"watch state: a cleanup is still pending from an earlier run; "
                f"waiting quiet={args.quiet_seconds}s first")

    while True:
        time.sleep(args.interval)
        now = time.time()

        new_man = snapshot(manifests)
        changed = [p for p, v in new_man.items() if man_state.get(p) != v]
        if changed:
            man_state = new_man
            for p in changed:
                log(f"install activity: {p}")
            armed = True
            last_change = now
            save_state(statefile, man_state, True, last_run)
            log("watch state: cleanup pending once the store is quiet")

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
        save_state(statefile, man_state, False, last_run)
        code = run_engine(engine_path, args.execute, log, logfile)
        if code == 3:
            log("store was busy or refused a safety rail; re-arming")
            armed = True
            last_change = now
            save_state(statefile, man_state, True, last_run)
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
    ap.add_argument("--state", help="trigger state file (default: watch-state.json next to --log)")
    ap.add_argument("--engine", help="engine script (default: prune-orphans.py next to this file)")
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
