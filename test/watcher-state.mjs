/**
 * Trigger-state test for scripts/watch-installs.py.
 *
 * The bug this pins down: the watcher used to keep its trigger state (manifest
 * snapshot, "armed", last run) in memory only. `dsh plugin add` restarts the profile,
 * which replaces the watcher, and the new process re-baselined the manifests -- so the
 * install that had just landed across that restart looked like it had always been there
 * and was never cleaned up. State now lives in logs/watch-state.json.
 *
 * Everything here is hermetic: DSH_HOME points at a temporary fake home, and --engine
 * points at a stub engine that only appends its argv to runs.txt, so no real pnpm store
 * is ever read or written. It exits 0 with a skip notice when no Python is available.
 *
 * Run it when no pnpm process is busy: the watcher deliberately re-arms instead of
 * pruning while pnpm is alive.
 *
 *   node test/watcher-state.mjs
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const WATCHER = join(PACKAGE_ROOT, 'scripts', 'watch-installs.py')

const ENGINE_STUB = `"""Stub engine: never touches a real store, just records that it was run."""
import json, os, sys


def store_from_modules_yaml(profile):
    return None


def default_store_for(profile):
    return os.path.join(profile, "store")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "runs.txt"), "a", encoding="utf-8") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
    print(json.dumps({"ok": True, "deleted": 0}))
    raise SystemExit(0)
`

function findPython() {
  const override = process.env.DSH_STORE_PRUNE_PYTHON?.trim()
  if (override && existsSync(override)) return override
  const names = process.platform === 'win32'
    ? ['pythonw.exe', 'python.exe', 'python3.exe']
    : ['python3', 'python']
  const dirs = (process.env.PATH ?? '').split(process.platform === 'win32' ? ';' : ':')
  for (const dir of dirs) {
    if (dir === '' || /[\\/]WindowsApps[\\/]?$/i.test(dir)) continue
    for (const name of names) {
      const candidate = join(dir, name)
      if (existsSync(candidate)) return candidate
    }
  }
  return undefined
}

const failures = []
const check = (condition, message) => {
  if (condition) {
    console.log(`  ok   ${message}`)
  } else {
    console.log(`  FAIL ${message}`)
    failures.push(message)
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function waitFor(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    if (predicate()) return true
    if (Date.now() > deadline) {
      console.log(`  FAIL timed out after ${timeoutMs}ms waiting for ${label}`)
      failures.push(label)
      return false
    }
    await sleep(100)
  }
}

function processAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

const python = findPython()
if (python === undefined) {
  console.log('watcher-state: no python interpreter found — skipping (set DSH_STORE_PRUNE_PYTHON to run it)')
  process.exit(0)
}

const root = mkdtempSync(join(tmpdir(), 'dsh-store-prune-state-'))
const home = join(root, 'home')
const profile = join(home, 'profiles', 'demo')
const logs = join(root, 'logs')
const manifest = join(profile, 'package.json')
const engineStub = join(root, 'stub-engine.py')
const runsFile = join(root, 'runs.txt')
const stateFile = join(logs, 'watch-state.json')
const watcherLog = join(logs, 'watcher.log')
const children = []

const readLog = () => (existsSync(watcherLog) ? readFileSync(watcherLog, 'utf8') : '')
const readRuns = () => (existsSync(runsFile) ? readFileSync(runsFile, 'utf8').trim().split('\n').filter(Boolean) : [])
const readState = () => {
  try {
    return JSON.parse(readFileSync(stateFile, 'utf8'))
  } catch {
    return null
  }
}

function startWatcher(extra) {
  // stdio:'ignore' on purpose: the watcher reports through --log, and no pipe is opened.
  const child = spawn(python, [
    WATCHER,
    '--engine', engineStub,
    '--log', watcherLog,
    '--interval', '1',
    ...extra,
  ], { env: { ...process.env, DSH_HOME: home }, stdio: 'ignore', windowsHide: true })
  children.push(child)
  return child
}

function stop(child) {
  if (child.exitCode === null && processAlive(child.pid)) {
    try {
      child.kill()
    } catch {
      /* already gone */
    }
  }
}

try {
  mkdirSync(profile, { recursive: true })
  mkdirSync(logs, { recursive: true })
  writeFileSync(engineStub, ENGINE_STUB)
  writeFileSync(join(profile, 'pnpm-lock.yaml'), "lockfileVersion: '9.0'\n")
  writeFileSync(manifest, JSON.stringify({ name: 'demo', version: '1.0.0' }))

  // 1. A fresh instance baselines the manifests, then catches a change and sweeps.
  const first = startWatcher(['--quiet-seconds', '1', '--cooldown', '0', '--once', '--execute'])
  await sleep(2500)
  check(readState() !== null, 'a fresh watcher persists its baseline snapshot')
  check(readState()?.pending === false, 'a fresh baseline is not armed')

  writeFileSync(manifest, JSON.stringify({ name: 'demo', version: '1.0.1' }))
  await waitFor(() => readRuns().length === 1, 25_000, 'the cleanup run triggered by the first change')
  await waitFor(() => !processAlive(first.pid), 15_000, 'the --once watcher to exit')

  const log1 = readLog()
  check(/install activity: .*package\.json/.test(log1), 'a manifest change is logged as install activity')
  check(/engine exit=0/.test(log1), 'the engine ran and exited 0')
  check(readRuns()[0]?.includes('--execute') === true, '--execute is passed through to the engine')
  check(readState()?.pending === false, 'state is no longer pending after a successful run')
  check((readState()?.last_run ?? 0) > 0, 'state records the last run time')

  // 2. The regression: the change lands while no watcher is alive (profile restart).
  writeFileSync(manifest, JSON.stringify({ name: 'demo', version: '1.0.2' }))
  const log2Mark = readLog().length
  const second = startWatcher(['--quiet-seconds', '30', '--cooldown', '0'])
  await sleep(4000)
  const log2 = readLog().slice(log2Mark)
  check(/install activity: .*package\.json/.test(log2),
    'REGRESSION: a change made while no watcher was alive is still seen after a restart')
  check(readState()?.pending === true, 'that change is persisted as pending')
  check(readRuns().length === 1, 'nothing ran while quiet-seconds was unsatisfied')
  stop(second)

  // 3. The inherited pending cleanup runs after yet another restart, with no new change.
  const log3Mark = readLog().length
  const third = startWatcher(['--quiet-seconds', '1', '--cooldown', '0', '--once', '--execute'])
  await waitFor(() => readRuns().length === 2, 25_000, 'the inherited pending cleanup to run')
  await waitFor(() => !processAlive(third.pid), 15_000, 'the second --once watcher to exit')
  const log3 = readLog().slice(log3Mark)
  check(/still pending from an earlier run/.test(log3), 'a restart reports the inherited pending cleanup')
  check(!/install activity: .*package\.json/.test(log3), 'it ran without needing a new file change')
  check(/engine exit=0/.test(log3), 'the inherited run also exits 0')
  check(readState()?.pending === false, 'the inherited pending flag is cleared afterwards')

  // 4. The cooldown floor also survives a restart.
  writeFileSync(manifest, JSON.stringify({ name: 'demo', version: '1.0.3' }))
  const log4Mark = readLog().length
  const fourth = startWatcher(['--quiet-seconds', '1', '--cooldown', '3600'])
  await sleep(4000)
  const log4 = readLog().slice(log4Mark)
  check(/install activity: .*package\.json/.test(log4), 'the next change is seen')
  check(readRuns().length === 2, 'the persisted last run still blocks a second prune inside the cooldown')
  check(readState()?.pending === true, 'the blocked cleanup stays pending for later')
  stop(fourth)

  // 5. A corrupt state file degrades to a fresh baseline instead of breaking the watcher.
  writeFileSync(stateFile, '{ this is not json')
  const log5Mark = readLog().length
  const fifth = startWatcher(['--quiet-seconds', '30', '--cooldown', '0'])
  await waitFor(() => /none usable at/.test(readLog().slice(log5Mark)), 10_000, 'the fallback log line')
  writeFileSync(manifest, JSON.stringify({ name: 'demo', version: '1.0.4' }))
  await waitFor(() => /install activity: .*package\.json/.test(readLog().slice(log5Mark)), 10_000,
    'the watcher to keep working after the fallback')
  check(readState()?.version === 1, 'the corrupt state file is replaced by a valid one')
  stop(fifth)
} catch (error) {
  console.log(`  FAIL unexpected error: ${String(error?.stack ?? error)}`)
  failures.push('unexpected error')
} finally {
  for (const child of children) stop(child)
  await sleep(200)
  rmSync(root, { recursive: true, force: true })
}

if (failures.length > 0) {
  console.log(`watcher-state: ${failures.length} check(s) failed`)
  process.exit(1)
}
console.log('watcher-state: all checks passed')
