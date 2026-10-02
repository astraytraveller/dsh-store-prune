/**
 * Smoke test for dsh-store-prune's plugin layer.
 *
 * It does not touch a real pnpm store: a temporary tool directory is created with
 * stub Python files, the plugin is loaded with a fake cordis context, and the test
 * asserts that
 *
 *   1. the watcher is spawned with `--execute --log <logs>/watcher.log`,
 *   2. logs/plugin.log records the child pid,
 *   3. running the ctx.effect disposer (profile unload) kills that child.
 *
 * It exits 0 with a skip notice when no Python interpreter is available, so the
 * test stays honest on machines that cannot run the plugin at all.
 *
 *   node test/smoke.mjs
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

const WATCHER_STUB = `import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "started.json"), "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
time.sleep(300)
`
const ENGINE_STUB = `raise SystemExit(0)
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
  console.log('smoke: no python interpreter found — skipping (set DSH_STORE_PRUNE_PYTHON to run it)')
  process.exit(0)
}

const toolDir = mkdtempSync(join(tmpdir(), 'dsh-store-prune-smoke-'))
const logsDir = join(toolDir, 'logs')
let childPid = null

try {
  writeFileSync(join(toolDir, 'watch-installs.py'), WATCHER_STUB)
  writeFileSync(join(toolDir, 'prune-orphans.py'), ENGINE_STUB)
  mkdirSync(logsDir, { recursive: true })

  process.env.DSH_STORE_PRUNE_DIR = toolDir
  process.env.DSH_STORE_PRUNE_LOGS = logsDir
  process.env.DSH_STORE_PRUNE_PYTHON = python

  const { apply, name } = await import(pathToFileURL(join(PACKAGE_ROOT, 'lib/index.js')).href)
  check(name === 'store-prune', `plugin exports name=store-prune`)
  check(typeof apply === 'function', 'plugin exports apply()')

  const disposers = []
  const ctx = {
    effect(factory, label) {
      check(typeof label === 'string' && label.includes('watcher'), `effect registered with label "${label}"`)
      disposers.push(factory())
    },
  }

  apply(ctx)
  check(disposers.length === 1, 'apply() registered exactly one effect')

  const startedJson = join(toolDir, 'started.json')
  const pluginLog = join(logsDir, 'plugin.log')
  if (!(await waitFor(() => existsSync(startedJson), 20_000, 'the stub watcher to start'))) throw new Error('watcher never started')

  const args = JSON.parse(readFileSync(startedJson, 'utf8'))
  check(args[0] === '--execute', `watcher is started in execute mode (got ${JSON.stringify(args[0])})`)
  check(args[1] === '--log', `watcher gets an explicit --log flag (got ${JSON.stringify(args[1])})`)
  check(args[2] === join(logsDir, 'watcher.log'), `watcher log path is ${join(logsDir, 'watcher.log')}`)

  const log = existsSync(pluginLog) ? readFileSync(pluginLog, 'utf8') : ''
  const match = /watcher started pid=(\d+)/.exec(log)
  check(match !== null, 'plugin.log records "watcher started pid=..."')
  if (match !== null) {
    childPid = Number(match[1])
    check(processAlive(childPid), `child pid ${childPid} is alive`)
  }

  for (const dispose of disposers) dispose()
  const afterDispose = existsSync(pluginLog) ? readFileSync(pluginLog, 'utf8') : ''
  check(afterDispose.includes('disposed with profile'), 'plugin.log records the profile dispose')
  if (childPid !== null) {
    await waitFor(() => !processAlive(childPid), 10_000, `child pid ${childPid} to exit`)
    check(!processAlive(childPid), `disposer killed child pid ${childPid}`)
  }
} catch (error) {
  console.log(`  FAIL unexpected error: ${String(error?.stack ?? error)}`)
  failures.push('unexpected error')
} finally {
  if (childPid !== null && processAlive(childPid)) {
    try {
      process.kill(childPid)
    } catch {
      /* already gone */
    }
  }
  rmSync(toolDir, { recursive: true, force: true })
}

if (failures.length > 0) {
  console.log(`smoke: ${failures.length} check(s) failed`)
  process.exit(1)
}
console.log('smoke: all checks passed')
