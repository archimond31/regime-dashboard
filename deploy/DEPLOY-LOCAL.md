# 本地运行方案（Windows 任务计划）

> **只是想让它跑起来？不用看这份文档。**
> 在 PyCharm 里打开 `monitor.py` 按运行，一个窗口一直开着就行，Ctrl+C 停。
> 本文档里的计划任务方案（`install_task.ps1`）已于 2026-11-13 停用 ——
> 两条路会在同一分钟抢跑，让同一根 4H 发出两封邮件（台账只按 bar 去重，
> 同一秒双启动两边都还没写台账，挡不住）。要切回计划任务用 `deploy\task_ctl.cmd on`。

> 决策：2026-11-13，信号链路从 GitHub Actions 改为本机全时段跑。
> 云端 `signal.yml` 的 `schedule` 已停用，只保留 `workflow_dispatch` 手动兜底。

---

## 1. 为什么改

GitHub Actions 的两件事治不了：

| 问题 | 表现 |
|---|---|
| cron 不准时 | 实测延迟 0.5–9 小时、整班漏跑；7 次/天常只落到 1–2 次 |
| 拿不到实时 K 线 | runner 只能读 `data.binance.vision` 静态归档，而当日归档要到**次日**才发布 |

第二条最要命：**北京时间 00:05 / 04:05 / 08:05 这三班，实际在用前一天的数据判今天的势。**
11-13 早上就是实例 —— 08:56 报「需动作 0」，09:45 数据补齐后算出「LINK 止损临近」。

> **改到本机只解决第一条（准时），不解决第二条。**
> 滞后的根在数据源 —— `data.binance.vision` 的当日归档要次日才发布，本地读的是同一个源。
> **第二条已于 2026-11-13 修掉**（`data-api.binance.vision` 补尾，境内可直连），
> 详见第 8 节。下面这几段是修复前的状态，保留作为「为什么要改」的记录。

本机跑的实际收益：

- **不再漏班**。这是最要紧的：11-11 那次静默停摆，错过一个真实开仓。
- 北京 **12:05 / 16:05 / 20:05** 三班，前一天归档已发布，数据是齐的，结论准确。
- 北京 **00:05 / 04:05 / 08:05** 三班当时仍会滞后 4~6 根，邮件正文会带红色告警块 ——
  **那是正常现象，不是故障**。告警块的作用就是让你别把这几班当成最新状态。
  （补尾上线后这三班也不滞后了；告警块保留作为兜底，真滞后时会亮。）

代价是**关机就停摆**（见第 6 节）。

---

## 2. 已配置的内容

| 项 | 值 |
|---|---|
| 任务名 | `RegimeSignal4H` |
| 触发时刻（北京） | `08:05` `12:05` `16:05` `20:05` `00:05` `04:05` —— 即每根 4H 收盘后 5 分钟 |
| 运行身份 | 当前用户 `archimond`，不需要密码、不需要管理员 |
| Python | `C:\Users\archimond\AppData\Local\Programs\Python\Python313\python.exe`（3.13.6） |
| 日志 | `regime_rsi_ema_strategy/logs/signal.log`（UTF-8） |
| 错过补跑 | `StartWhenAvailable`，开机后补跑一次 |
| 睡眠唤醒 | `WakeToRun` |
| 电池 | 允许在电池上运行、不用电池也不中止 |
| 并发 | 同一任务不并行（`IgnoreNew`） |

### 文件

```
deploy/
├── install_task.ps1    # 注册/卸载计划任务
├── run_signal.cmd      # 执行入口（任务调它）
├── DEPLOY-LOCAL.md     # 本文件
└── DEPLOY.md           # 云服务器方案（未采纳，留作备选）
logs/
└── signal.log          # 运行日志
```

---

## 3. 常用命令

```powershell
# 看状态与下次运行
Get-ScheduledTaskInfo -TaskName RegimeSignal4H | Format-List

# 立刻手动跑一次
Start-ScheduledTask -TaskName RegimeSignal4H

# 跑一次演练（不发信），直接调 cmd 更直观
& '...\deploy\run_signal.cmd' --force --dry

# 暂停（比如出门几天）
Disable-ScheduledTask -TaskName RegimeSignal4H

# 恢复
Enable-ScheduledTask -TaskName RegimeSignal4H

# 彻底卸载
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1 -Remove

# 重新注册（改了触发时刻或 Python 路径后）
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1
```

**不想记命令的话，双击 `deploy\task_ctl.cmd` 就行**：

| 操作 | 效果 |
|---|---|
| 双击（无参数） | 显示 State / 上次运行 / 上次结果 / 下次运行 |
| `task_ctl.cmd off` | 暂停自动跑 |
| `task_ctl.cmd on` | 恢复自动跑 |
| `task_ctl.cmd run` | 立刻跑一次 |

带参数调用时窗口不会停留，可以直接在资源管理器的地址栏里敲：
`cmd /c task_ctl.cmd off`

---

## 3.1 在 PyCharm 里手动跑

> 2026-11-13 项目已迁到 `PycharmProjects\pythonProject2\boros_funding_arbitrage\regime_rsi_ema_strategy`
> （为了和 A 股项目放在一起管理）。迁移后计划任务已重新注册指向新路径。

### 打开方式有两条，差别只在 Working directory

**方式 A（推荐）：直接打开子目录**

```
C:\Users\archimond\PycharmProjects\pythonProject2\boros_funding_arbitrage\regime_rsi_ema_strategy
```

`File → Open` 选它。此时 Working directory 默认就是对的，不用动。

**方式 B：打开 boros 根**，让侧边栏同时看到 A 股项目和它

```
C:\Users\archimond\PycharmProjects\pythonProject2\boros_funding_arbitrage
```

**这种方式建议改一处，但严格说不改也能跑** —— 得分清你 Run 的是哪个脚本：

```
Run → Edit Configurations → Working directory
建议改为： C:\Users\archimond\PycharmProjects\pythonProject2\boros_funding_arbitrage\regime_rsi_ema_strategy
```

- **Run `run_signal.py`：不受影响。** 它内部用 `subprocess.run(..., cwd=HERE)` 把三个子脚本的
  工作目录切到项目根，自己读台账也用绝对路径（`os.path.join(HERE, ...)`）。
  实测在 boros 根下启动同样正确。
- **直接 Run 子脚本（`live_signals.py` / `gen_signal_html.py` / `notify_mail.py`）：必须改。**
  这三个用的是相对路径（`_signal.json`、`signal_now.html`、`.cache_binance`、`.notify_state.json`）。
  不改的症状：文件写到 boros 根、K 线缓存找不到要重下 77MB、去重台账丢失可能重复发信。

所以：只调主入口就不用管这一项；要在 PyCharm 里单独跑某一步（比如只想重发一封邮件），
务必先把 Working directory 指到项目根。

方式 B 的另一个代价：boros 里有 `data/ahist`（6000+ 只股票的 parquet），PyCharm 会连它一起索引，
首次打开可能要等几分钟。

### 找到并运行

1. 左侧 Project 面板点一下，直接键盘敲 `run_signal` → 自动定位到 `run_signal.py`
2. 右键 → **Run 'run_signal'**（或点行号区的绿色三角）
3. 停止：红色方块，`Ctrl+F2`

Interpreter 随便选 3.8 以上的都行，这套代码**零第三方库**，不需要 venv。

### 解释器报 `CreateProcess error=2` 怎么办

症状：

```
Cannot run program "...\pythonProject1\venv\Scripts\python.exe"
(in directory "...\regime_rsi_ema_strategy"): CreateProcess error=2, 系统找不到指定的文件
```

这是 PyCharm 的项目解释器指向了一个**已被删除的 venv**，与代码无关。
`boros_funding_arbitrage` 有自己的 `.venv`（3.13.9、471 个包），改指它即可：

`Ctrl+Alt+S` → `Project: boros_funding_arbitrage` → `Python Interpreter`
→ `Add Local Interpreter` → `Virtualenv` → `Existing`
→ 填 `...\boros_funding_arbitrage\.venv\Scripts\python.exe`

两个要点：

- **不影响计划任务**。`deploy/run_signal.cmd` 里写死的是独立安装的
  `Python313\python.exe`，不经过 PyCharm、不依赖项目 venv。
- 同一个配置也决定 A 股脚本能不能跑。解释器坏掉时 `a_share_stock_picker.py` 之类
  同样启动不了，改回 `.venv` 是一举两得。

### 嵌套 git 仓库的注意点

`regime_rsi_ema_strategy` 自带 `.git`（指向 `archimond31/regime-dashboard`），放进 `boros_funding_arbitrage`
（另一个 git 仓库）内部后，两者是**独立的嵌套仓库**：

- 在 regime 目录里 `git` 命令操作的是 regime-dashboard
- 在 boros 根里 `git status` 会把它显示成一个未跟踪目录
- **不要在 boros 根执行 `git add -A`** —— git 会把它记成 gitlink（submodule 指针），
  以后 clone 会指向一个不存在的 commit

建议在 boros 根的 `.gitignore` 里加一行：

```
regime_rsi_ema_strategy/
```


### 常用参数（Script parameters 里填）

| 参数 | 效果 |
|---|---|
| 留空 | 正常跑：有动作发信号信，无动作则当天第一封补日报 |
| `--dry` | 演练：完整跑，但**绝不发信**，只打印判定 |
| `--no-mail` | 只重新生成 `signal_now.html`，完全不碰邮件 |
| `--force --dry` | 绕过「同 bar 跳过」，强制完整跑一遍，仍然不发信 |
| `--always` | 无动作也发（**会真发**，别拿它试验） |

### 一个必须注意的冲突

**PyCharm 里手动跑、和计划任务自动跑，是两个独立进程，共用同一份台账文件。**

同时跑会出问题：两边都读 `.notify_state.json` 判断「这根 4H 发过没有」，可能都判为没发过 →
各发一封。窗口只有 40 秒，概率不高，但不是零。

所以手动跑之前，先暂停任务；或者干脆只用 `--dry`（演练不发信，不会重复）：

```
双击 deploy\task_ctl.cmd off      → 改 PyCharm 里的代码 / 手动跑
双击 deploy\task_ctl.cmd on       → 跑完恢复
```

要打断点调试的话，右键 → **Debug 'run_signal'**，加 `--dry` 参数，不会误发信。


查看日志最后 40 行：

```powershell
Get-Content '...\logs\signal.log' -Tail 40 -Encoding utf8
```

---

## 4. 两个实现细节（踩过的坑）

### 4.1 时间戳必须由 Python 打，不能让 cmd 打

Windows 的 `%DATE%` 带本地化星期（`2026/11/13 周五`），这是 **GBK 字节**。写进 UTF-8 日志就变成 `2026/11/13 ����`。

所以 `run_signal.cmd` 里**故意不写任何时间戳**，由 `run_signal.py` 的 `main()` 开头打印：

```
===== 2026-11-13 10:08:17 北京 开始（参数：--force --dry）=====
```

整份日志因此是单一编码，`Get-Content -Encoding utf8` 直接可读。

### 4.2 Python 用独立安装，不用 WorkBuddy 自带的

WorkBuddy 的 python 在 `binaries/python/versions/3.13.12/` 下，**版本号写在路径里**，WorkBuddy 一升级路径就变，任务会静默失败。

`run_signal.cmd` 里优先用独立安装，找不到才退回 PATH 里的 `python`。改路径后重跑一次 `install_task.ps1` 即可（cmd 不用重装，它是读文件执行的）。

---

## 5. 与云端的关系

**`schedule` 已停用。** 原因不是嫌它慢，是**两套台账互相看不见**：

- 云端的 `.notify_state.json` 存在 `actions/cache`
- 本地那份在仓库目录
- 两边各自判断「这根 4H 发过没有」，同时跑 = 同一根发两封信

11-13 已经踩过一次（本地信号信 + 云端日报）。

恢复云端兜底（比如笔记本要长期送修）：

```yaml
# .github/workflows/signal.yml
on:
  schedule:
    - cron: '5 0,1,4,8,12,16,20 * * *'
  workflow_dispatch:
```

**恢复前先把本地任务 `Disable` 掉**，否则就是双跑。

另：`dashboard.yml`（日线仪表盘 + Pages 部署）继续留在云端，不受影响 —— 它一天跑一次，延迟几小时也无所谓。

---

## 6. 可靠性：让「没收到邮件」不再是模糊信号

### 6.1 核心问题

补这套东西之前，收件箱只有两种状态：**有邮件** 和 **没邮件**。
而「没邮件」有两种完全不同的含义 —— 真的没动作（正常），或者任务跑挂了（故障）。

**静默故障是最贵的。** 11-11 那次停摆 24 小时、错过一次真实开仓，就是因为两者分不清。

### 6.2 现在有三层，一一对应

| 状态 | 谁告诉你 | 邮件长什么样 |
|---|---|---|
| 有动作 / 当天第一封 | `notify_mail.py` | 【加密信号】或【加密日报】 |
| 跑了，但没事可做 | 无邮件 **+ 心跳正常** | — |
| **跑挂了** | `alert_fail.py` | 【加密信号·故障】带退出码与日志尾部 |
| **根本没跑**（关机/任务被禁/python 没了） | **心跳超时**（外部服务） | 由 healthchecks.io 发出 |

前两行是「内容层」，第三行是「进程层」，第四行是「机器层」。三层都沉默，才需要你担心。

### 6.3 失败告警（已内置，无需配置）

`run_signal.cmd` 拿到非 0 退出码时会自动调 `alert_fail.py`，发一封带**退出码、最后 60 行日志、可能原因**的邮件。

已验证的行为（用临时目录造了一个必然失败的脚本）：

```
cmd exit = 1          <- 真实退出码被保留，没有被告警逻辑污染
[告警] 加载邮件配置失败：No module named 'notify_mail'   <- 告警脚本确实被调用了
```

`alert_fail.py` 自己的设计原则：**报不出去也不改退出码**。它再失败，顶多是这封邮件丢了，不会掩盖真正的问题。

调试时用 `python alert_fail.py 1 --dry`，只打印不真发。

### 6.4 心跳（需要你花 3 分钟配一次）

这是唯一能发现「机器关了 / 任务被禁 / python 路径失效」的手段 —— 因为那些情况下脚本压根没机会发信。

1. 打开 https://healthchecks.io，注册（免费版够用）
2. 新建一个 Check：
   - **Period: 4 hours**
   - **Grace: 1 hour**
3. 复制它的 ping URL（形如 `https://hc-ping.com/xxxxxxxx-xxxx-...`）
4. 存成本地文件，**一行，只有 URL 本身**：

```
deploy\heartbeat.url
```

`run_signal.cmd` 每次**成功**跑完会 curl 一下这个地址（失败路径故意不 ping，这样挂掉也会触发超时告警）。文件不存在时行为不变，不影响运行。

这个文件已在 `.gitignore` 里 —— UUID 等同口令，泄露了别人就能伪造心跳。

> 不想用第三方服务的话，替代方案是「每天固定时间自查今天有没有发信」，但那依赖同一台机器，
> 机器关了就一起失效 —— 等于没检。心跳必须是外部的。

### 6.5 降低「跑不成」的概率

| 措施 | 做法 | 状态 |
|---|---|---|
| 睡眠时唤醒 | `WakeToRun` | ✅ 已开 |
| 开机补跑 | `StartWhenAvailable` | ✅ 已开 |
| 失败重试 | 失败后 10 分钟重试，最多 2 次 | ✅ 已开（`RestartCount=2`） |
| 电池上不中断 | `AllowStartIfOnBatteries` + `DontStopIfGoingOnBatteries` | ✅ 已开 |
| 不并行 | `MultipleInstances = IgnoreNew` | ✅ 已开 |
| **合盖不睡眠** | 电源选项 → 选择关闭盖子的功能 → **不采取任何操作** | ⬜ **需要你手动设** |

最后一项最重要 —— 笔记本合盖进入现代待机（S0）时，`WakeToRun` 经常唤不醒。这一步没做，前面的设置有一半是摆设。

### 6.6 仍然存在的限制

| 限制 | 影响 | 缓解 |
|---|---|---|
| **关机期间不跑** | 期间那几根 4H 的信号会错过 | 心跳会告警；长期外出时切回云端 schedule |
| 补跑只补一次 | 开机后不会逐根补齐，补跑用的是**当时最新**数据 | 接受。补跑的意义是「恢复通知」，不是「重放历史」 |
| 任务被系统禁用 | Windows 更新、组策略偶尔会禁 | 心跳能发现 |

第二条值得展开：假设机器关了 10 小时，期间有一根 4H 触发开仓。开机后补跑，脚本看到的是最新 bar，那笔仓会以「已持有 N 根」出现，而不是「刚开仓」。**通知会迟到，但状态是对的** —— 不会让你误以为空仓。

### 6.7 三个自查命令

```powershell
# 任务还活着吗（LastTaskResult=0 正常）
Get-ScheduledTaskInfo -TaskName RegimeSignal4H | Format-List LastRunTime, LastTaskResult, NextRunTime

# 今天发过信吗
Get-Content '...\logs\signal.log' -Tail 30 -Encoding utf8 | Select-String "已发送|跳过|告警"

# 心跳最近一次是什么时候（healthchecks.io 后台也会显示）
Get-Content '...\deploy\heartbeat.url'
```


---

## 7. 故障排查

| 症状 | 查什么 |
|---|---|
| 某天完全没邮件 | `Get-ScheduledTaskInfo` 看 `LastRunTime` / `LastTaskResult`；再 `Get-Content logs\signal.log -Tail 60` |
| 有日志但没发信 | 正常。看日志里是 `[跳过] 今天已有信、本轮无动作` 还是 `[已发送]` |
| `LastTaskResult = 0x1` | 脚本报错，日志里有 traceback |
| `LastTaskResult = 0x2` | 路径问题，检查任务动作里的 cmd 路径 |
| 数据标「落后 N 根」 | 归档延迟，等下一班 |
| 邮件里 `[未配置]` | `_mail.json` 丢了或字段不全 |

`LastTaskResult` 对照：`0` 成功、`267011`（0x41303）还没跑过、`267009`（0x41301）正在运行。

---

## 8. REST 补数（已做，2026-11-13）

原始问题：本地跑治不了「拿到的是昨天数据」，瓶颈在 `data.binance.vision`，不在调度。
当日归档要到**次日约 09:44（北京）**才发布，所以一天里归档末端几乎不动 ——
一个 4H 策略每 4 小时醒一次，却每次都拿到同一根 K 线，实际退化成「每天看一次」。

### 最终做法（与原先设想不同，更省事）

没有走 `api.binance.com`（境内 502，要代理）。实测发现 Binance 还有一个
**`data-api.binance.vision`**：与静态归档同源、同一交易所、同一口径，且**国内可直连**。

`fetch_binance_vision.py` 里加了 `_fetch_rest()`，在 `load_bars()` 末尾只补
「归档末端 → 现在」这一小段，历史归档一个字节都不动。三个关键约束：

1. **只收已收盘的 bar**（`open_time + 周期 <= now`）。未收盘那根的 close 还在跳，
   拿去判信号等于未来函数 —— 这是这条腿最容易埋进去的错。
2. **异常全吞**：REST 挂了就退回纯归档，宁可数据旧，不能让整轮信号跑不出来。
3. **`end` 有值时不补**（调用方在切历史窗口，回测/复现不该掺实时数据）；
   `tail=False` 可显式关掉。

### 验收（实测）

| 项 | 结果 |
|---|---|
| 9 个 Binance 品种 REST 可达 | 全部 < 1s 到手 |
| 归档 vs REST 重叠区数值 | 27 根 × 4 字段全不一致 **0** 处（无口径冲突） |
| BTC 1H 序列 | 纯归档末根 `11-12 23:00` → 补尾后 `11-13 02:00`，且已收盘 |
| 4H 聚合 | 末个完整桶仍是 `11-13 00:00`，下一桶 3/4 根，等 03:00 收盘成 4/4 |
| HTML 滞后告警块 | 已消失 |

顺带修掉的一个 bug：`auto_end = end is None` 必须在给 `end` 填默认值**之前**记下来，
否则 `end is None` 永远为假，补尾分支一次都不会进（第一次跑就是 0 根新增，才发现）。

### 效果

04:05 UTC（12:05 北京）那一班就能看到 `11-13 04:00` 那根收盘，之后真正每 4 小时一次。
不再需要「等次日归档」。

