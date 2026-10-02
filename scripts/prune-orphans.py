#!/usr/bin/env python3
"""Remove orphaned packages from a pnpm 12+ content-addressable store.

Why this exists: pnpm never deletes store content on uninstall/upgrade, and on this
machine `pnpm store prune` is a no-op (measured: 0 lines of output, 0.00 MB freed even
after deleting a whole project). This script does what prune is supposed to do.

How orphans are identified -- two independent gates, both must agree:

  * a CAS file (`files/<d[:2]>/<d[2:]>`) whose `st_nlink == 1` is not hard-linked into
    any project on this volume, so its bytes back no installed package;
  * a package (<name>@<version>) read from every protected project's node_modules is
    never touched, whatever its link count.

Files owned by a protected package are also excluded explicitly (`spared` counter), so
an install done with `packageImportMethod=copy` is still safe.

An index row is dropped if ANY of its files is being deleted, so pnpm re-fetches a
package instead of trying to link a half-missing one. `-exec` side-effect caches are
never hard-linked (that is normal, not a sign of being unused), so they are deleted only
when every index row that references them is itself being dropped.

The store's write lock is taken before anything is deleted and released after the rows
are gone, so a concurrent pnpm process cannot observe a half-mutated store.

Exit codes: 0 = ok (including "nothing to do"), 3 = refused by a safety rail, 1 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

HEX128 = re.compile(rb"[0-9a-f]{128}")
DEFAULT_MIN_PROTECTED = 50
KEEP_BACKUPS = 3


def log(msg: str, handle=None) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    if handle is not None:
        handle.write(line + "\n")
        handle.flush()


def default_store_for(anchor: str) -> str:
    """pnpm picks the store per drive: %LOCALAPPDATA% on C:, <drive>:\\.pnpm-store elsewhere."""
    drive = os.path.splitdrive(os.path.abspath(anchor))[0].upper()
    if drive.startswith("C"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "pnpm", "store", "v11")
    return f"{drive}\\.pnpm-store\\v11"


# pnpm writes node_modules/.modules.yaml as JSON-flavoured YAML: the key is quoted and
# indented, and the line ends with a comma because more keys follow --
#     "storeDir": "C:\\Users\\you\\AppData\\Local\\pnpm\\store\\v11",
# 1.1.0 only matched a bare top-level `storeDir:`, so detection never fired and every
# run silently fell through to the drive default. Both spellings are accepted here.
STORE_DIR_LINE = re.compile(
    r"""^[ \t]*["']?storeDir["']?[ \t]*:[ \t]*(?P<value>"(?:[^"\\]|\\.)*"|'[^']*'|[^,\r\n]+)""",
    re.M,
)


def yaml_scalar(raw: str) -> str:
    """Undo YAML/JSON scalar quoting so an escaped Windows path comes back with single separators."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # a double-quoted scalar escapes backslashes and quotes
        return value[1:-1].replace("\\\\", "\\").replace('\\"', '"')
    if len(value) >= 2 and value[0] == value[-1] == "'":
        # a single-quoted scalar escapes only the quote itself
        return value[1:-1].replace("''", "'")
    return value


def store_from_modules_yaml(project: str) -> str | None:
    """`node_modules/.modules.yaml` records the storeDir a project was installed from."""
    path = os.path.join(project, "node_modules", ".modules.yaml")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = STORE_DIR_LINE.search(text)
    if not m:
        return None
    return yaml_scalar(m.group("value")) or None


def resolve_store(projects: list[str], explicit: str | None) -> tuple[str, str]:
    if explicit:
        return os.path.abspath(explicit), "explicit --store"
    for project in projects:
        found = store_from_modules_yaml(project)
        if found:
            return os.path.abspath(found), f"node_modules/.modules.yaml of {project}"
    anchor = projects[0] if projects else os.getcwd()
    return default_store_for(anchor), f"drive default for {anchor}"


def default_projects() -> list[str]:
    """Every DSH profile plus the current directory -- the projects this store serves."""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    out = []
    profiles = os.path.join(home, "profiles")
    if os.path.isdir(profiles):
        for name in sorted(os.listdir(profiles)):
            nm = os.path.join(profiles, name, "node_modules")
            if os.path.isdir(nm):
                out.append(os.path.join(profiles, name))
    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, "node_modules")) and cwd not in out:
        out.append(cwd)
    return out


def protected_labels(projects: list[str]) -> set[str]:
    labels: set[str] = set()
    for project in projects:
        root = os.path.join(project, "node_modules")
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            if "package.json" not in files or os.path.basename(base) == "node_modules":
                continue
            try:
                with open(os.path.join(base, "package.json"), encoding="utf-8") as fh:
                    m = json.load(fh)
            except Exception:
                continue
            if m.get("name") and m.get("version"):
                labels.add(f"{m['name']}@{m['version']}")
    return labels


def cas_digest(path: str, files_root: str) -> str | None:
    rel = os.path.relpath(path, files_root).split(os.sep)
    if len(rel) == 2 and len(rel[0]) == 2 and len(rel[1]) == 126:
        d = (rel[0] + rel[1]).lower()
        if HEX128.fullmatch(d.encode()):
            return d
    return None


def path_of(files_root: str, digest: str, is_exec: bool = False) -> str:
    return os.path.join(files_root, digest[:2], digest[2:] + ("-exec" if is_exec else ""))


def human(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser(description="prune orphaned packages from a pnpm store")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--store", help="store directory (default: auto-detect)")
    ap.add_argument("--project", action="append", default=[], help="project whose installed packages are protected (repeatable)")
    ap.add_argument("--min-protected", type=int, default=DEFAULT_MIN_PROTECTED)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--log", help="append a log file")
    ap.add_argument("--plan", help="write the deletion plan as JSON to this path")
    ap.add_argument("--json", action="store_true", help="print a JSON summary to stdout")
    args = ap.parse_args()

    handle = open(args.log, "a", encoding="utf-8") if args.log else None
    started = time.time()
    projects = args.project or default_projects()
    store, how = resolve_store(projects, args.store)
    files_root = os.path.join(store, "files")
    db = os.path.join(store, "index.db")
    mode = "EXECUTE" if args.execute else "dry-run"
    log(f"=== prune-orphans [{mode}] store={store} ({how})", handle)
    log(f"protected projects: {', '.join(projects) if projects else '(none)'}", handle)

    if not os.path.isdir(files_root) or not os.path.isfile(db):
        log(f"REFUSED: {store} does not look like a pnpm store (files/ + index.db)", handle)
        return 3

    labels = protected_labels(projects)
    log(f"protected packages: {len(labels)}", handle)
    if len(labels) < args.min_protected:
        log(f"REFUSED: only {len(labels)} protected packages (< --min-protected {args.min_protected}); "
            f"project node_modules looks missing or unreadable", handle)
        return 3

    # ---- scan the store ----
    orphan: set[str] = set()
    execs: dict[str, tuple[str, int]] = {}
    store_files = store_bytes = 0
    for base, _dirs, files in os.walk(files_root):
        for name in files:
            path = os.path.join(base, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            store_files += 1
            store_bytes += st.st_size
            digest = cas_digest(path, files_root)
            if digest is not None:
                if st.st_nlink == 1:
                    orphan.add(digest)
            elif name.endswith("-exec") and len(name) == 131:
                execs[(os.path.basename(base) + name[:-5]).lower()] = (path, st.st_size)
    log(f"store: {store_files} files / {human(store_bytes)}; "
        f"unlinked CAS files: {len(orphan)}; -exec caches: {len(execs)}", handle)

    # ---- read the index ----
    try:
        con = sqlite3.connect(db, timeout=10, isolation_level=None)
        con.execute("PRAGMA busy_timeout = 10000")
        rows = con.execute("SELECT key, data FROM package_index").fetchall()
    except sqlite3.Error as exc:
        log(f"REFUSED: cannot read {db}: {exc}", handle)
        return 3
    log(f"index rows: {len(rows)}", handle)

    exec_owners: dict[str, set[str]] = {}
    protected_files: set[str] = set()
    drop_rows: list[str] = []
    drop_labels: set[str] = set()
    kept_rows = mixed_rows = 0
    for raw_key, blob in rows:
        key = raw_key.decode("utf-8", "replace") if isinstance(raw_key, bytes) else str(raw_key)
        if isinstance(blob, str):
            blob = blob.encode("utf-8", "surrogateescape")
        label = key.split("\t")[-1]
        hashes = {h.decode() for h in HEX128.findall(blob)}
        for h in hashes & execs.keys():
            exec_owners.setdefault(h, set()).add(label)
        if label in labels:
            protected_files |= hashes
            continue
        if hashes & orphan:
            drop_rows.append(key)
            drop_labels.add(label)
            if hashes - orphan:
                mixed_rows += 1
        else:
            kept_rows += 1

    delete = sorted(orphan - protected_files)
    freed = sum(os.path.getsize(path_of(files_root, d)) for d in delete
                if os.path.exists(path_of(files_root, d)))
    exec_delete = sorted(h for h, owners in exec_owners.items() if owners <= drop_labels)
    exec_freed = sum(execs[h][1] for h in exec_delete)
    log(f"rows: kept={kept_rows} (in use elsewhere) dropped={len(drop_rows)} (partly shared={mixed_rows})", handle)
    log(f"planned: {len(delete)} CAS files / {human(freed)}, {len(exec_delete)} -exec caches / {human(exec_freed)}, "
        f"{len(drop_rows)} index rows -> total {human(freed + exec_freed)}", handle)

    plan = {
        "store": store, "mode": mode, "protectedPackages": len(labels),
        "deleteFiles": len(delete), "deleteBytes": freed,
        "deleteExecs": len(exec_delete), "deleteExecBytes": exec_freed,
        "dropRows": len(drop_rows), "keepRows": kept_rows, "mixedRows": mixed_rows,
        "rows": drop_rows, "files": delete, "execs": exec_delete,
    }
    if args.plan:
        try:
            with open(args.plan, "w", encoding="utf-8") as fh:
                json.dump(plan, fh, indent=1)
        except OSError as exc:
            log(f"warning: could not write plan file: {exc}", handle)

    if not args.execute:
        log("dry run: nothing was deleted (pass --execute)", handle)
    else:
        # Take the store's write lock BEFORE deleting anything, so no pnpm process can
        # observe rows that point at files we already removed.
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            log(f"REFUSED: store index.db is locked by another process ({exc}); nothing deleted", handle)
            return 3

        backup = f"{db}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.copy2(db, backup)
            log(f"backed up index.db -> {backup}", handle)
        except OSError as exc:
            log(f"REFUSED: cannot back up index.db ({exc}); nothing deleted", handle)
            con.execute("ROLLBACK")
            return 3

        gone = failed = 0
        for d in delete:
            try:
                os.remove(path_of(files_root, d))
                gone += 1
            except OSError:
                failed += 1
        egone = efailed = 0
        for h in exec_delete:
            try:
                os.remove(execs[h][0])
                egone += 1
            except OSError:
                efailed += 1
        log(f"deleted: CAS {gone} (failed {failed}), -exec {egone} (failed {efailed})", handle)

        try:
            con.executemany("DELETE FROM package_index WHERE key = ?", [(k,) for k in drop_rows])
            con.execute("COMMIT")
        except sqlite3.Error as exc:
            log(f"ERROR: could not update the index ({exc}); rolling back", handle)
            con.execute("ROLLBACK")
            return 1
        left = con.execute("SELECT count(*) FROM package_index").fetchone()[0]
        try:
            con.execute("VACUUM")
        except sqlite3.Error as exc:
            log(f"warning: VACUUM failed: {exc}", handle)
        log(f"index rows now: {left}", handle)

        # keep only the most recent backups
        backups = sorted(
            (os.path.join(os.path.dirname(db), f) for f in os.listdir(os.path.dirname(db))
             if f.startswith("index.db.bak-")),
            key=os.path.getmtime, reverse=True)
        for old in backups[KEEP_BACKUPS:]:
            try:
                os.remove(old)
            except OSError:
                pass
        log(f"index backups kept: {min(len(backups), KEEP_BACKUPS)}", handle)

    con.close()
    os.environ["PRUNE_ORPHANS_SUMMARY"] = "1"
    summary = {"ok": True, **{k: v for k, v in plan.items() if k not in ("rows", "files", "execs")},
               "seconds": round(time.time() - started, 1)}
    if args.json or not args.quiet:
        print(json.dumps(summary, ensure_ascii=True), flush=True)
    if handle:
        handle.write(json.dumps(summary, ensure_ascii=True) + "\n")
        handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
