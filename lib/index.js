/**
 * dsh-store-prune — 在 profile 运行期间守护 pnpm store 的孤儿清理。
 *
 * 设计取舍：
 * - SQLite（store 的 index.db）读写留在已验证的 Python 引擎 `prune-orphans.py` 里，
 *   不在 JS 里重写 —— profile 解析不到宿主嵌套的 @libsql/client，而 Python 3.14 自带 sqlite3。
 * - 本插件只做「随 profile 启停 + 守护进程 + 记日志」，触发/静默/pnpm 忙判/加锁全部由
 *   `watch-installs.py` 负责，保证只有一份会改写 store 的逻辑。
 * - apply() 内任何异常只记日志、绝不向外抛：插件坏掉不允许拖垮 profile 启动。
 */
import { spawn } from 'node:child_process'
import { appendFileSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'store-prune'

/** 守护进程退出后的重启延迟。 */
const RESTART_DELAY_MS = 30_000
/** 连续快速失败时最多重试到这个次数，然后放弃（等下次 profile 启动）。 */
const MAX_RAPID_RESTARTS = 5
/** 插件包根目录（lib/index.js 的上一级）。 */
const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
/** 工具目录默认位置：包内 scripts/（可用 DSH_STORE_PRUNE_DIR 覆盖）。 */
const DEFAULT_TOOL_DIR = join(PACKAGE_ROOT, 'scripts')

/** Windows 的商店别名（0 字节 reparse point）不是可用的解释器，必须跳过。 */
const PYTHON_NAMES = ['pythonw.exe', 'python.exe', 'python3', 'python']

/** 从 PATH 里找解释器；跳过 WindowsApps 下的商店别名存根。 */
function pathCandidates() {
  const dirs = (process.env.PATH ?? '').split(process.platform === 'win32' ? ';' : ':')
  const found = []
  for (const dir of dirs) {
    if (dir === '' || /[\\/]WindowsApps[\\/]?$/i.test(dir)) continue
    for (const name of PYTHON_NAMES) found.push(join(dir, name))
  }
  return found
}

/** pythonw.exe 优先：无控制台窗口。找不到时返回 undefined，由调用方放弃启动。 */
function resolvePython() {
  const candidates = [
    process.env.DSH_STORE_PRUNE_PYTHON?.trim(),
    'C:\\Python314\\pythonw.exe',
    'C:\\Python314\\python.exe',
    ...pathCandidates(),
  ].filter((item) => item !== undefined && item !== '')
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  return undefined
}

export function apply(ctx) {
  let child = null
  let disposed = false
  let restartTimer = null
  let rapidRestarts = 0
  let logFile = null

  const log = (line) => {
    const text = `${new Date().toISOString()} ${line}\n`
    try {
      if (logFile !== null) appendFileSync(logFile, text)
    } catch {
      /* 日志写不进去也不能影响宿主 */
    }
    console.log(`[store-prune] ${line}`)
  }

  const cleanupChild = () => {
    if (child === null) return
    const target = child
    child = null
    try {
      target.removeAllListeners('exit')
      target.kill()
    } catch (error) {
      log(`failed to stop watcher pid=${String(target.pid)}: ${String(error?.message ?? error)}`)
    }
  }

  const dispose = () => {
    disposed = true
    if (restartTimer !== null) {
      clearTimeout(restartTimer)
      restartTimer = null
    }
    cleanupChild()
    log('disposed with profile')
  }

  try {
    const toolDir = process.env.DSH_STORE_PRUNE_DIR?.trim() || DEFAULT_TOOL_DIR
    const watcher = join(toolDir, 'watch-installs.py')
    const engine = join(toolDir, 'prune-orphans.py')
    const logsDir = process.env.DSH_STORE_PRUNE_LOGS?.trim() || join(toolDir, 'logs')

    ctx.effect(() => () => dispose(), 'store-prune: watcher process')

    if (!existsSync(watcher) || !existsSync(engine)) {
      log(`skipped: ${existsSync(watcher) ? '' : `${watcher} missing; `}${existsSync(engine) ? '' : `${engine} missing`}`.trim())
      return
    }
    const python = resolvePython()
    if (python === undefined) {
      log('skipped: no python interpreter found (set DSH_STORE_PRUNE_PYTHON)')
      return
    }
    try {
      mkdirSync(logsDir, { recursive: true })
    } catch {
      /* 目录已存在或不可写，交给下面的 append 兜底 */
    }
    logFile = join(logsDir, 'plugin.log')

    const start = () => {
      if (disposed) return
      const startedAt = Date.now()
      let proc = null
      try {
        const args = [watcher, '--execute', '--log', join(logsDir, 'watcher.log')]
        proc = spawn(python, args, {
          cwd: toolDir,
          detached: false,
          stdio: 'ignore',
          windowsHide: true,
        })
        child = proc
        log(`watcher started pid=${String(proc.pid)} (${python} ${args.join(' ')})`)
      } catch (error) {
        log(`spawn failed: ${String(error?.message ?? error)}`)
        return
      }
      proc.on('error', (error) => {
        log(`watcher error: ${String(error?.message ?? error)}`)
      })
      proc.on('exit', (code, signal) => {
        if (child === proc) child = null
        if (disposed) return
        rapidRestarts = Date.now() - startedAt < 10_000 ? rapidRestarts + 1 : 0
        if (rapidRestarts > MAX_RAPID_RESTARTS) {
          log(`watcher exited (code=${String(code)}, signal=${String(signal)}) too often; not restarting`)
          return
        }
        log(`watcher exited (code=${String(code)}, signal=${String(signal)}); restarting in ${RESTART_DELAY_MS / 1000}s`)
        restartTimer = setTimeout(() => {
          restartTimer = null
          start()
        }, RESTART_DELAY_MS)
        restartTimer.unref?.()
      })
    }

    start()
  } catch (error) {
    log(`apply failed (ignored): ${String(error?.message ?? error)}`)
  }
}
