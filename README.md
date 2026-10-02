# dsh-store-prune

[![check](https://github.com/astraytraveller/dsh-store-prune/actions/workflows/check.yml/badge.svg)](https://github.com/astraytraveller/dsh-store-prune/actions/workflows/check.yml)

A **DSH** profile plugin that reclaims disk space by sweeping **orphaned** packages out of the pnpm content-addressed store — it starts with your profile, watches for plugin installs, and exits with it. No Windows Startup entry, no scheduled task.

[中文说明](README.zh-CN.md)

## Why this exists

On the store format used by recent pnpm (tested on pnpm 12.4.2, Windows):

- **`pnpm store prune` is a no-op.** It prints nothing and frees 0 bytes, even when the only project that used a package has been deleted. Verified twice with controlled experiments.
- **`pnpm remove` does not clean the store.** It drops the hardlinks in `node_modules`, the lockfile entry and the manifest claim; the store copy stays forever. The pnpm docs say it outright: *"it does not automatically remove packages"*.
- **There is no hook to ride on.** pnpm does not run the root project's `postinstall` for `pnpm add` (verified), so the trigger has to come from outside.

Measured on the author's machine: one sweep reclaimed **2.83 GB** — 9,074 content-addressed files plus 51 `-exec` build caches.

## How it decides what is orphaned

Every in-use project holds a **hardlink** to each store file it uses, so the link count is the ground truth:

| store file state | meaning | action |
| --- | --- | --- |
| `st_nlink >= 2` | at least one in-use project links it | never touched |
| `st_nlink == 1` | nothing links it | orphan → deleted |
| `*-exec` file | postinstall build cache, **never hardlinked** | decided by the ownership rows in `index.db` instead |

On top of that, every run is guarded by:

1. the store must exist and look like a pnpm store (`files/` + `index.db`);
2. fewer protected packages than `--min-protected` (default 50) → refuse (catches "`node_modules` is gone or unreadable");
3. `BEGIN IMMEDIATE` on `index.db` **before deleting anything** — if pnpm holds the lock, refuse and delete nothing;
4. `index.db` is backed up to `index.db.bak-<timestamp>` first (newest 3 kept);
5. files belonging to any protected project are never deleted (counted as `spared`);
6. if any file of an index row is deleted, the **whole row** is dropped — no rows pointing at missing files;
7. dry-run by default: only `--execute` deletes.

## Install

### As a DSH profile plugin (recommended)

```powershell
dsh plugin --profile web add github:astraytraveller/dsh-store-prune

# pin a tag or commit if you prefer
dsh plugin --profile web add github:astraytraveller/dsh-store-prune#v1.0.0

# uninstall
dsh plugin --profile web remove dsh-store-prune
```

The plugin takes effect on the **next profile start** (restart DSH). It registers itself in the profile's `dsh.profile.bundles` via `cordis.patch.yml`.

Requirements: DSH with profile-bundle support, `git`, and Python 3.11+ (standard library only — the engine uses `sqlite3`). Built and tested on Windows; the engine itself is mostly portable, the install watcher is Windows-specific (`Get-CimInstance`).

### Standalone (without DSH)

The Python engine does not depend on DSH at all:

```powershell
cd scripts

python .\prune-orphans.py                 # dry run: report only (this is the default)
python .\prune-orphans.py --execute       # actually delete
.\prune-orphans.ps1 -Execute              # same, with per-day logs
python .\watch-installs.py --execute      # keep watching install activity until Ctrl+C
```

## Configuration

| environment variable | default | meaning |
| --- | --- | --- |
| `DSH_STORE_PRUNE_DIR` | `<package>/scripts` | directory holding `watch-installs.py` and `prune-orphans.py` |
| `DSH_STORE_PRUNE_LOGS` | `<DSH_STORE_PRUNE_DIR>/logs` | where `plugin.log` / `watcher.log` are written |
| `DSH_STORE_PRUNE_PYTHON` | `C:\Python314\pythonw.exe` → `python.exe` → first `python`/`python3` on `PATH` | interpreter used for the watcher |

If the scripts or an interpreter cannot be found, the plugin writes a single `skipped: ...` line and starts nothing — it never breaks profile startup. Any error inside `apply()` is logged, never thrown.

## What the plugin does at runtime

- starts `pythonw.exe watch-installs.py --execute --log <logs>/watcher.log` with `cwd` = scripts dir;
- restarts it after 30 s if it dies unexpectedly, giving up after 5 rapid (<10 s) restarts;
- kills it when the profile unloads (`ctx.effect` disposer).

The watcher itself:

- **trigger** — a change to `$DSH_HOME\profiles\*\package.json`, `pnpm-lock.yaml` or `node_modules\.modules.yaml`, which covers both `dsh plugin install` and the dshmarket GUI;
- **quiescence** — a run waits for `--quiet-seconds` (90 s) of no change, so a slow install (lockfile first, linking still in progress) is never cut in half;
- **busy check** — skips while a pnpm process is alive;
- **store paths participate in quiescence only, never as a trigger**, otherwise a cleanup would re-trigger itself forever;
- `--cooldown` (600 s) floors the run rate, `--interval` (20 s) is the polling period;
- if the engine refuses (exit 3), the trigger is re-armed for the next round.

## Logs

- `logs\plugin.log` — plugin start/stop/restart/skip lines.
- `logs\watcher.log` — `install activity: <path>`, `running: ...`, `engine exit=0 {...}`, `REFUSED: ...` (rotated at 1 MB).

A healthy steady state looks like:

```
store: 16390 files / 721.6 MB; unlinked CAS files: 0; -exec caches: 41
planned: 0 CAS files / 0.0 MB, 0 -exec caches / 0.0 MB, 0 index rows -> total 0.0 MB
```

## Engine CLI

```
prune-orphans.py [--execute] [--store PATH] [--project DIR]... [--min-protected N]
                 [--quiet] [--log FILE] [--plan FILE] [--json]
```

- `--project DIR` (repeatable) — projects whose packages are protected. Default: every `$DSH_HOME\profiles\*\` that has a `node_modules`, plus the current directory.
- `--store PATH` — override store detection. Default order: the first protected project's `node_modules\.modules.yaml` → its `storeDir`; otherwise a drive default (`C:` → `%LOCALAPPDATA%\pnpm\store\v11`, other drives → `<drive>:\.pnpm-store\v11`).
- `--plan FILE` — dump the full plan (files / index rows / exec caches) as JSON.
- Exit codes: **0** ok, **3** refused by a safety rail, **1** error.

## Rollback

1. Stop the watcher: `dsh plugin --profile web remove dsh-store-prune` (or kill the process).
2. Restore the index: copy `store\v11\index.db.bak-<timestamp>` back over `index.db`.
3. If a profile's links were somehow damaged, reinstall it: `dsh plugin --profile web install` — anything missing is re-downloaded from the registry.

Deleting store files can never break a profile by itself: a profile's `node_modules` entries **are** hardlinks, and hardlinked files are never deleted.

## Limitations

1. Only the store actually used by a protected project is cleaned. If you keep profiles on another drive, pass `--project` for them (profiles under `$DSH_HOME\profiles` are picked up automatically).
2. If DSH is **force-killed**, the watcher may survive as an orphan process. It keeps doing the same harmless work; on the next start a second watcher appears and the engine's `BEGIN IMMEDIATE` lock makes the later one refuse (exit 3). No corruption.
3. Nothing is cleaned while the profile is not running — by design, since plugin installs only happen while DSH runs.
4. pnpm's `-exec` build caches are only deleted once **all** index rows referencing them are dropped.

## Tests

```powershell
npm test          # test/smoke.mjs: stub tool dir, real spawn, disposer check
```

The smoke test builds a temporary `scripts/` directory with stub Python files, loads the plugin with a fake `ctx`, and asserts that the child process is spawned with the expected arguments, that `plugin.log` records it, and that the disposer kills it. It skips (exit 0) when no Python interpreter is available.

Honest status: the engine is verified by dry-runs and the smoke test above, and the watcher's start-up/parameters were verified on the author's machine — but the full **install → 90 s → sweep** chain has not yet been observed in production there, because every install so far finished within seconds of a profile restart.

## License

MIT
