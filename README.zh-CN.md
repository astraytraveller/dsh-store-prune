# dsh-store-prune

[English](README.md)

一个 **DSH profile 插件**：在 profile 运行期间监视插件安装活动，把 pnpm 内容寻址 store 里**再没有任何在用项目硬链接的孤儿包**清掉，回收磁盘空间。随 profile 启动而启动、随 profile 退出而退出 —— 不需要 Windows 启动项（Startup 的 `.vbs`）或计划任务，这是它存在的全部理由。

## 为什么需要它

在本机验证过的 pnpm 行为（Windows、pnpm 12.4.2）：

- **`pnpm store prune` 是空操作**：不打印任何东西、释放 0 字节 —— 即使某个包唯一的使用项目已经被删掉。用对照实验验证过两次。
- **`pnpm remove` 不碰 store**：它只删 `node_modules` 里的硬链接、lockfile 条目和 `package.json` 里的声明；store 里的副本会永久留下。pnpm 文档原话：*"it does not automatically remove packages"*。
- **pnpm 没有现成的钩子**：`pnpm add` 不会执行根项目的 `postinstall`（已验证），所以触发只能来自外部。

本机实测：一次清理回收 **2.83 GB** —— 9,074 个内容寻址文件加 51 个 `-exec` 构建缓存。

## 判定依据：硬链接数

在用项目对它用到的每个 store 文件都持有**硬链接**，所以链接数就是事实来源：

| store 文件状态 | 含义 | 动作 |
| --- | --- | --- |
| `st_nlink >= 2` | 至少有一个在用项目链接它 | 绝不动 |
| `st_nlink == 1` | 没人链接它 | 孤儿 → 删除 |
| `*-exec` 文件 | postinstall 构建缓存，**从不被硬链接** | 改看它在 `index.db` 索引里的归属行 |

每次运行还有这些护栏：

1. store 必须存在且看起来像 pnpm store（有 `files/` 和 `index.db`）；
2. 保护包数量少于 `--min-protected`（默认 50）→ 拒绝（能抓住「`node_modules` 丢了或读不到」）；
3. **删任何东西之前**先对 `index.db` 执行 `BEGIN IMMEDIATE`；拿不到锁（有 pnpm 在跑）→ 拒绝，什么都不删；
4. 先备份 `index.db` 为 `index.db.bak-<时间戳>`（只保留最新 3 个）；
5. 属于任何受保护项目的文件永不删除（计入 `spared`）；
6. 只要某个索引行有**任意**文件被删，整行就丢弃 —— 不留指向缺失文件的索引行；
7. 默认 dry-run；只有 `--execute` 才真删。

## 安装

### 作为 DSH profile 插件（推荐）

```powershell
dsh plugin --profile web add github:astraytraveller/dsh-store-prune

# 想锁定版本就带上 tag 或 commit
dsh plugin --profile web add github:astraytraveller/dsh-store-prune#v1.0.0

# 卸载
dsh plugin --profile web remove dsh-store-prune
```

plugin 在**下次 profile 启动**时生效（重启 DSH）。它通过 `cordis.patch.yml` 把自己登记进 profile 的 `dsh.profile.bundles`。

要求：支持 profile bundle 的 DSH、`git`、Python 3.11+（只用标准库，引擎用的是 `sqlite3`）。开发和测试都在 Windows 上；引擎本身基本可移植，安装监视器是 Windows 专属（用 `Get-CimInstance` 查 pnpm 进程）。

### 独立使用（不装 DSH）

Python 引擎完全不依赖 DSH：

```powershell
cd scripts

python .\prune-orphans.py                 # dry run：只报告（这是默认行为）
python .\prune-orphans.py --execute       # 真正删除
.\prune-orphans.ps1 -Execute              # 同上，带按日期分文件的日志
python .\watch-installs.py --execute      # 持续监视安装活动，Ctrl+C 退出
```

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DSH_STORE_PRUNE_DIR` | `<包目录>/scripts` | 工具目录（须同时含 `watch-installs.py` 与 `prune-orphans.py`） |
| `DSH_STORE_PRUNE_LOGS` | `<DSH_STORE_PRUNE_DIR>/logs` | `plugin.log` / `watcher.log` 写在哪里 |
| `DSH_STORE_PRUNE_PYTHON` | `C:\Python314\pythonw.exe` → `python.exe` → PATH 上第一个 `python`/`python3` | 启动监视器用的解释器 |

找不到脚本或解释器时，插件只写一行 `skipped: ...` 日志、不启动任何进程 —— 不会影响 DSH 启动。`apply()` 内部任何异常只记日志、绝不向外抛。

## 插件运行时做什么

- 用 `pythonw.exe watch-installs.py --execute --log <logs>/watcher.log` 启动监视器，`cwd` = scripts 目录；
- 子进程意外退出：30 s 后重启；若 10 s 内连续退出超过 5 次则放弃（等下次 profile 启动）；
- profile 卸载/退出（`ctx.effect` 的 disposer）时杀掉子进程。

监视器自身：

- **触发**：`$DSH_HOME\profiles\*\` 下 `package.json`、`pnpm-lock.yaml` 或 `node_modules\.modules.yaml` 发生变化 —— 覆盖 `dsh plugin install` **和** dshmarket GUI 两条路径；
- **静默判定**：最后一次变动之后还要等 `--quiet-seconds`（默认 90 秒）静默才跑引擎 —— 这样「早早写完 lockfile、但还在慢慢链接」的安装不会被中途剪掉；
- **忙判**：有 pnpm 进程活着就跳过；
- store 自身（`index.db`、`files\`）只参与静默判定、**绝不作为触发条件**，否则清理会触发自己陷入死循环；
- `--cooldown`（默认 600 秒）限制运行频率，`--interval`（默认 20 秒）是轮询周期；
- 引擎被拒绝（exit 3）时只是重新武装、等下一次。

## 看日志

- `logs\plugin.log` —— 插件的启停 / 重启 / skip。
- `logs\watcher.log` —— `install activity: <path>`、`running: ...`、`engine exit=0 {...}`、`REFUSED: ...`（超过 1 MB 轮转）。

健康的稳态运行长这样：

```
store: 16390 files / 721.6 MB; unlinked CAS files: 0; -exec caches: 41
planned: 0 CAS files / 0.0 MB, 0 -exec caches / 0.0 MB, 0 index rows -> total 0.0 MB
```

## 引擎 CLI

```
prune-orphans.py [--execute] [--store PATH] [--project DIR]... [--min-protected N]
                 [--quiet] [--log FILE] [--plan FILE] [--json]
```

- `--project DIR`（可重复）—— 其包受保护的项目。默认：所有带 `node_modules` 的 `$DSH_HOME\profiles\*\`，加上当前目录。
- `--store PATH` —— 覆盖 store 检测。默认顺序：第一个受保护项目的 `node_modules\.modules.yaml` → 其中的 `storeDir`；否则用盘符默认值（`C:` → `%LOCALAPPDATA%\pnpm\store\v11`，其他盘 → `<盘符>:\.pnpm-store\v11`）。
- `--plan FILE` —— 把完整计划（文件 / 索引行 / exec 缓存）导出为 JSON。
- 退出码：**0** 正常，**3** 被安全轨拒绝，**1** 出错。

## 回滚

1. 停掉监视器：`dsh plugin --profile web remove dsh-store-prune`（或直接结束进程）。
2. 恢复索引：把 `store\v11\index.db.bak-<时间戳>` 复制回 `index.db`。
3. 万一 profile 的链接出了问题，重装它：`dsh plugin --profile web install` —— 缺什么就从 registry 重新下载。

删 store 文件本身永远不会弄坏 profile：profile 的 `node_modules` 条目**就是**硬链接，而硬链接文件永远不会被删。

## 限制

1. 只清理受保护项目实际在用的那个 store。如果你在别的盘维护 profile，要为它传 `--project`（位于 `$DSH_HOME\profiles` 下的会被自动纳入）。
2. DSH 被**强杀**（不是正常退出）时监视器可能成为孤儿进程 —— 它继续做同样的、无害的工作；下一次启动会再起一个，两者并发时引擎的 `BEGIN IMMEDIATE` 锁会让后到者拒绝执行（exit 3），不会损坏 store。
3. profile 不在运行时没有任何清理 —— 这是设计使然：插件安装本来就只在 DSH 运行时发生。
4. pnpm 的 `-exec`（构建缓存）文件只有在引用它的**所有**索引行都被丢弃时才删。

## 测试

```powershell
npm test          # test/smoke.mjs：桩工具目录 + 真实 spawn + disposer 检查
```

冒烟测试会建一个临时 `scripts/` 目录（里面是桩 Python 文件），用一个假的 `ctx` 加载插件，断言子进程带着预期参数被启动、`plugin.log` 有记录、disposer 能杀掉它；没有可用 Python 解释器时跳过（exit 0）。

诚实的进度说明：引擎经过 dry-run 与上述冒烟测试验证，监视器的启动与参数也在开发机上验证过；但**「装插件 → 90 s → 清理」这条端到端链路尚未在生产中观察到**，因为至今每次安装都在 profile 重启前几秒就结束了。

## 许可

MIT
