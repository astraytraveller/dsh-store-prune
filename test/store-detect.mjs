/**
 * Store-detection test for scripts/prune-orphans.py.
 *
 * The bug this pins down: pnpm writes `node_modules/.modules.yaml` as JSON-flavoured
 * YAML — the key is quoted and indented, the value is a double-quoted Windows path with
 * escaped separators, and the line ends in a comma because more keys follow:
 *
 *     virtualStoreDir: "C:\\proj\\node_modules\\.pnpm",
 *     storeDir: "C:\\Users\\you\\AppData\\Local\\pnpm\\store\\v11",
 *
 * 1.1.0 only matched a bare top-level `storeDir:`, so the detector never fired and every
 * run fell through to the drive default. That is not just noise: a profile installed from
 * a store on another drive (or from `--store`) was pointed at the wrong store entirely.
 *
 * Everything here is hermetic: the fixture projects live under a temporary directory and
 * the engine is imported by path (its `main()` is behind a `__main__` guard), so no real
 * pnpm store is read or written, and no pnpm process is needed. It exits 0 with a skip
 * notice when no Python is available.
 *
 *   node test/store-detect.mjs
 *   DSH_STORE_PRUNE_ENGINE=<other prune-orphans.py> node test/store-detect.mjs
 *     (the second form runs the same checks against another engine copy; the checks are
 *      expected to FAIL against the 1.1.0 detector and pass against the current one)
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const ENGINE = process.env.DSH_STORE_PRUNE_ENGINE?.trim()
  || join(PACKAGE_ROOT, 'scripts', 'prune-orphans.py')

const DRIVER = `"""Import the engine by path, ask it where the store is, write the answers as JSON.

Nothing goes to stdout: the caller spawns this with stdio ignored, so a failure has to
land in the output file.
"""
import importlib.util, json, sys, traceback


def run(engine_path, cases_path, out_path):
    spec = importlib.util.spec_from_file_location("prune_orphans_under_test", engine_path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)

    with open(cases_path, encoding="utf-8") as fh:
        cases = json.load(fh)

    results = []
    for case in cases:
        projects = case["projects"]
        resolved = engine.resolve_store(projects, None)
        results.append({
            "name": case["name"],
            "perProject": [engine.store_from_modules_yaml(p) for p in projects],
            "resolved": resolved[0],
            "source": resolved[1],
        })
    return results


def main():
    engine_path, cases_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        payload = {"results": run(engine_path, cases_path, out_path)}
    except BaseException:
        payload = {"error": traceback.format_exc()}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return 1 if "error" in payload else 0


if __name__ == "__main__":
    raise SystemExit(main())
`

// --- fixtures ---------------------------------------------------------------
// String.raw keeps the backslash escapes exactly as pnpm writes them on disk.
const PNPM11 = String.raw`lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

virtualStoreDir: "C:\\proj\\node_modules\\.pnpm",
storeDir: "C:\\Users\\demo\\AppData\\Local\\pnpm\\store\\v11",
packageManager: pnpm@10.4.1
`
const EXPECT_PNPM11 = String.raw`C:\Users\demo\AppData\Local\pnpm\store\v11`

// Only `virtualStoreDir` — it must not be mistaken for `storeDir`.
const VIRTUAL_ONLY = String.raw`lockfileVersion: '9.0'

virtualStoreDir: "C:\\proj\\node_modules\\.pnpm",
packageManager: pnpm@10.4.1
`

// The spelling 1.1.0 did support, kept so the fix cannot regress it.
const POSIX_BARE = "lockfileVersion: '9.0'\n\nstoreDir: /srv/pnpm-store/v11\n"

// YAML single quotes escape only the quote itself, so the backslashes are literal.
const SINGLE_QUOTED = String.raw`storeDir: 'D:\pnpm-store\v11'` + '\n'

const CASES = [
  // the two spellings 1.1.0 got wrong
  { name: 'pnpm-10-quoted-escaped', yaml: PNPM11, expect: EXPECT_PNPM11, regression: true },
  { name: 'pnpm-10-crlf', yaml: PNPM11.replace(/\n/g, '\r\n'), expect: EXPECT_PNPM11, regression: true },
  // and the spellings it must keep supporting
  { name: 'virtual-store-dir-only', yaml: VIRTUAL_ONLY, expect: null },
  { name: 'bare-top-level', yaml: POSIX_BARE, expect: '/srv/pnpm-store/v11' },
  { name: 'single-quoted', yaml: SINGLE_QUOTED, expect: String.raw`D:\pnpm-store\v11` },
  { name: 'no-modules-yaml', yaml: null, expect: null },
]

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

// Windows and POSIX spell the same location differently; compare canonical paths.
const norm = (value) => (value === null || value === undefined
  ? null
  : resolve(value).replace(/\\/g, '/').toLowerCase())

const python = findPython()
if (python === undefined) {
  console.log('store-detect: no python interpreter found — skipping (set DSH_STORE_PRUNE_PYTHON to run it)')
  process.exit(0)
}
if (!existsSync(ENGINE)) {
  console.log(`store-detect: engine not found at ${ENGINE} — skipping (set DSH_STORE_PRUNE_ENGINE)`)
  process.exit(0)
}

const root = mkdtempSync(join(tmpdir(), 'dsh-store-prune-detect-'))
const projectsDir = join(root, 'projects')
const casesFile = join(root, 'cases.json')
const outFile = join(root, 'out.json')
const driverFile = join(root, 'detect.py')

const projectPath = (name) => join(projectsDir, name)

try {
  mkdirSync(projectsDir, { recursive: true })
  writeFileSync(driverFile, DRIVER)

  for (const item of CASES) {
    const project = projectPath(item.name)
    if (item.yaml === null) {
      mkdirSync(project, { recursive: true })
      continue
    }
    mkdirSync(join(project, 'node_modules'), { recursive: true })
    writeFileSync(join(project, 'node_modules', '.modules.yaml'), item.yaml)
  }

  const withYaml = projectPath('pnpm-10-quoted-escaped')
  const cases = CASES.map((item) => ({ name: item.name, projects: [projectPath(item.name)] }))
  cases.push({ name: 'first-usable-project-wins', projects: [projectPath('no-modules-yaml'), withYaml] })
  writeFileSync(casesFile, JSON.stringify(cases))

  const child = spawn(python, [driverFile, ENGINE, casesFile, outFile], {
    stdio: 'ignore',
    windowsHide: true,
  })
  const exitCode = await new Promise((settle) => child.on('exit', (code) => settle(code)))

  if (exitCode !== 0 || !existsSync(outFile)) {
    const payload = existsSync(outFile) ? readFileSync(outFile, 'utf8') : '(no output file)'
    console.log(`  FAIL the engine driver exited ${exitCode}\n${payload}`)
    failures.push('driver failed')
    throw new Error('driver failed')
  }

  const { results } = JSON.parse(readFileSync(outFile, 'utf8'))
  const byName = new Map(results.map((entry) => [entry.name, entry]))

  for (const item of CASES) {
    const entry = byName.get(item.name)
    if (entry === undefined) {
      check(false, `${item.name}: the driver reported no result`)
      continue
    }
    const got = entry.perProject[0] ?? null
    if (item.expect === null) {
      check(got === null, `${item.name}: no storeDir is detected (got ${JSON.stringify(got)})`)
    } else {
      const what = item.regression === true
        ? `REGRESSION ${item.name}: a quoted, indented storeDir is read`
        : `${item.name}: storeDir is still read`
      check(got === item.expect, `${what} as ${JSON.stringify(item.expect)} (got ${JSON.stringify(got)})`)
    }
  }

  // The fallback still has to work when there is genuinely no storeDir to read.
  const fallback = byName.get('no-modules-yaml')
  check(typeof fallback?.resolved === 'string' && fallback.resolved.length > 0,
    'a project with no .modules.yaml still resolves to the drive default')
  check(/^drive default for /.test(fallback?.source ?? ''),
    `that resolution is labelled as the drive default (got ${JSON.stringify(fallback?.source)})`)

  // ...and a usable storeDir must be preferred over it, wherever it appears in the list.
  const pair = byName.get('first-usable-project-wins')
  check(norm(pair?.resolved) === norm(EXPECT_PNPM11),
    `resolve_store() prefers the first project that records a storeDir (got ${JSON.stringify(pair?.resolved)})`)
  check((pair?.source ?? '').includes('.modules.yaml') && (pair?.source ?? '').includes(withYaml),
    `that resolution names the .modules.yaml it came from (got ${JSON.stringify(pair?.source)})`)
} catch (error) {
  if (!failures.includes('driver failed')) {
    console.log(`  FAIL unexpected error: ${String(error?.stack ?? error)}`)
    failures.push('unexpected error')
  }
} finally {
  rmSync(root, { recursive: true, force: true })
}

if (failures.length > 0) {
  console.log(`store-detect: ${failures.length} check(s) failed`)
  process.exit(1)
}
console.log('store-detect: all checks passed')
