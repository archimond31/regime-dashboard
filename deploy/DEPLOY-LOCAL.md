# 本地运行方案（Windows 任务计划）

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
> 要彻底解决必须做 REST 补数，见第 8 节。

本机跑的实际收益：

- **不再漏班**。这是最要紧的：11-11 那次静默停摆，错过一个真实开仓。
- 北京 **12:05 / 16:05 / 20:05** 三班，前一天归档已发布，数据是齐的，结论准确。
- 北京 **00:05 / 04:05 / 08:05** 三班仍会滞后 4~6 根，邮件正文会带红色告警块 ——
  **那是正常现象，不是故障**。告警块的作用就是让你别把这几班当成最新状态。

代价是**关机就停摆**（见第 6 节）。

---

## 2. 已配置的内容

| 项 | 值 |
|---|---|
| 任务名 | `RegimeSignal4H` |
| 触发时刻（北京） | `08:05` `12:05` `16:05` `20:05` `00:05` `04:05` —— 即每根 4H 收盘后 5 分钟 |
| 运行身份 | 当前用户 `archimond`，不需要密码、不需要管理员 |
| Python | `C:\Users\archimond\AppData\Local\Programs\Python\Python313\python.exe`（3.13.6） |
| 日志 | `research/regime_rsi_ema_strategy/logs/signal.log`（UTF-8） |
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

## 6. 已知限制

| 限制 | 影响 | 缓解 |
|---|---|---|
| **关机期间不跑** | 最长可漏一整段，期间的入场信号会错过 | 开盘前开机；长期外出时切回云端 schedule |
| 休眠 | `WakeToRun` 通常能唤醒，但笔记本合盖 + 现代待机（S0）可能失效 | 电源选项把「合盖」设为「不采取任何操作」 |
| 单点故障 | 机器挂了没人告诉你 | 靠「每天至少一封」反查：某天收不到任何邮件 = 有问题 |
| 补跑只补一次 | 开机后 `StartWhenAvailable` 只跑一次，不会逐根补齐；且补跑时用的是**当时最新**的数据，历史那根 4H 的信号会被跳过 | 接受。补跑的意义是「恢复通知」，不是「重放历史」 |

最后一条值得展开：假设机器关了 10 小时，期间有一根 4H 触发开仓。开机后补跑，脚本看到的是最新的 bar，那笔仓会以「已持有 N 根」的形式出现，而不是「刚开仓」。**通知会迟到，但状态是对的** —— 不会让你误以为空仓。

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

## 8. 未做的事：REST 补数（数据滞后的唯一解）

本地跑治不了「拿到的是昨天数据」，因为瓶颈在 `data.binance.vision`，不在调度。

真正的解法是取数时用 Binance REST 补最近 48 根 1H，与归档按 ts 合并（REST 优先）。
`api.binance.com` 在境内基本不通，所以这条路要先解决网络（走代理，或者前面评估过的境外服务器）。

按既定规矩，这项改动需要先灰度：

1. 新增 `fetch_binance_rest.py`，默认关闭，`BINANCE_REST=1` 才启用
2. 每天记录「纯归档」与「归档 + REST」两套结果的差异（`bar4h`、`urgent` 列表、`held`）
3. 差异可解释（只是把缺失补齐、不改历史）→ 默认开启
4. 差异不可解释 → 回退，只保留告警块

验收标准：所有品种的 `bar4h` 一致，且邮件里的红色告警块消失。

在此之前，接受「早班数据旧一班」这个已知缺陷。
