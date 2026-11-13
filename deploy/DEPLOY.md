# 把 4H 信号链路搬到云服务器 —— 部署方案

> 状态：方案稿，待确认后落地。
> 目标：解决 GitHub Actions 的两件事 —— ① cron 不准时/漏跑；② 拿不到实时 K 线。

---

## 0. 边界：搬什么，不搬什么

| 组件 | 去向 | 理由 |
|---|---|---|
| `run_signal.py` 及依赖的 13 个模块 | **搬到服务器** | 需要 4H 边界精确触发 + 实时数据 |
| `_mail.json`、`.notify_state.json` | **搬到服务器**（本地文件，不入库） | 发信凭据与去重台账 |
| `build_dashboard.py` / 日线仪表盘 | 留在 GitHub Actions | 每天一次，延迟 7 小时也不影响结论；顺带白嫖 Pages |
| GitHub Pages 站点 | 留在 GitHub | 静态托管免费且稳 |

**不要整站搬迁。** 服务器上只需要一个 systemd timer，一天跑 7 次、每次 40 秒。

---

## 1. 选型

### 1.1 这台机器要扛什么

| 维度 | 实际需求 |
|---|---|
| CPU / 内存 | 峰值 < 200MB，Python 单进程，日常负载 ≈ 0 |
| 磁盘 | 20GB 足够（K 线缓存约 77MB，历史全量一次性） |
| 带宽 | 首月约 77MB 下载，之后每次增量几 MB |
| 出网延迟 | 到 `api.binance.com` 越低越好，决定 4H 收盘后多久能出信号 |
| 时区 | 需能设成 UTC（一条命令的事） |

结论：**最低配都嫌浪费**，不要为性能多花钱。钱应该花在「网络位置」上。

### 1.2 位置是唯一重要的参数

| 位置 | `api.binance.com` | `data.binance.vision` | 结论 |
|---|---|---|---|
| 香港 / 新加坡 / 东京 | 直连，50–100ms | 直连 | ✅ 首选 |
| 国内（阿里云/腾讯云内地节点） | **大概率不通** | 可直连（纯 CDN） | ❌ 白搬 |
| 美国 | 可通但受监管限制，不推荐 | 可通 | ⚠️ 不必要 |

买之前务必先测连通性。测不通就退（轻量服务器一般支持 7 天无理由）。

### 1.3 候选清单

| 厂商 | 节点 | 配置 | 参考价 | 付款 | 备注 |
|---|---|---|---|---|---|
| **腾讯云轻量应用服务器** | 香港 | 2C2G 40G 30Mbps | 约 24–40 元/月 | 支付宝/微信 | **首选**：中文控制台、免备案、境内可管理 |
| 阿里云轻量应用服务器 | 香港 | 2C1G | 约 24 元/月起 | 支付宝/微信 | 同上，二选一即可 |
| Vultr | 东京 / 新加坡 | 1C1G | $6/月 | 信用卡/PayPal | 按小时计费，测完不合适直接删，损失几毛钱 |
| DigitalOcean | 新加坡 | 1C1G | $6/月 | 信用卡/PayPal | 同上，文档最全 |

> 价格随活动浮动，以官网当期为准。**不要买 2C4G 以上，用不上。**

如果只想先验证连通性再决定，**Vultr 按小时计费最划算**：开一台东京 1C1G，跑完下面四条命令，不通就删，成本不到 1 元。

### 1.4 买之前 / 买之后的连通性测试

```bash
curl -s -m 10 -o /dev/null -w "binance api   : %{http_code}\n" "https://api.binance.com/api/v3/ping"
curl -s -m 10 -o /dev/null -w "binance vision: %{http_code}\n" "https://data.binance.vision/"
curl -s -m 10 -o /dev/null -w "hyperliquid   : %{http_code}\n" -X POST "https://api.hyperliquid.xyz/info" \
  -H "Content-Type: application/json" -d '{"type":"meta"}'
python3 -c "import socket;s=socket.create_connection(('smtp.qq.com',465),10);print('smtp 465      : OK');s.close()"
```

四条全 `200` / `OK` 才算合格。任何一条不通都别往下走 —— 先解决网络，别急着部署。

---

## 2. 目录结构

```
/opt/regime-signal/
├── repo/                        # git clone 的仓库（代码来源，只读为主）
│   ├── run_signal.py
│   ├── .cache_binance/          # 自动生成，约 77MB
│   ├── .cache_hl/
│   ├── .notify_state.json       # 去重台账，脚本自己维护
│   └── _mail.json               # 发信凭据，chmod 600，不入库
└── logs/
    └── signal.log
```

系统用户 `signal` 拥有整个目录，最小权限运行。

---

## 3. 安装步骤

### 3.1 系统准备

```bash
# 1) 时区设成 UTC —— 脚本内部全用 UTC，北京只是展示
sudo timedatectl set-timezone UTC
timedatectl | grep "Time zone"

# 2) 依赖：只有 python3 / git / curl，零第三方库
sudo apt update && sudo apt install -y python3 git curl rsync
python3 -V          # Ubuntu 22.04 自带 3.10、24.04 自带 3.12，都够用
```

> 这套代码**不依赖任何 pip 包**（已逐文件核过 import），所以不需要 venv。

### 3.2 建用户与目录

```bash
sudo useradd -r -m -d /opt/regime-signal -s /usr/sbin/nologin signal || true
sudo mkdir -p /opt/regime-signal/logs
sudo chown -R signal:signal /opt/regime-signal
```

### 3.3 拉代码

```bash
sudo -u signal git clone https://github.com/archimond31/regime-dashboard.git \
     /opt/regime-signal/repo
```

> 仓库是 public，clone 不需要凭据。若要 pull 私有分支再另配 deploy key。

### 3.4 冷启动缓存（可选，省 5–10 分钟）

K 线缓存要从 2017 年逐月下载。如果本地已经有一份，直接推上去：

```bash
# 在本地执行（路径随实际情况）
rsync -avz --progress \
  .cache_binance .cache_hl \
  signal@<服务器IP>:/opt/regime-signal/repo/
```

不放也行，首次运行会自己下，约 5–10 分钟。

### 3.5 写发信凭据

```bash
sudo -u signal tee /opt/regime-signal/repo/_mail.json > /dev/null <<'JSON'
{"host":"smtp.qq.com","port":465,
 "user":"1084328481@qq.com",
 "pass":"<QQ邮箱授权码>",
 "to":["1084328481@qq.com"]}
JSON
sudo chmod 600 /opt/regime-signal/repo/_mail.json
```

### 3.6 首次演练（不发信）

```bash
cd /opt/regime-signal/repo
sudo -u signal python3 run_signal.py --no-mail
sudo -u signal python3 notify_mail.py --dry
```

看到「实跑会发送」或「自动补一封日报」的判定即可进入下一步。

---

## 4. systemd timer

系统时区已是 UTC，所以 `OnCalendar` 直接写 UTC 时刻，不用换算。

**`/etc/systemd/system/regime-signal.service`**

```ini
[Unit]
Description=Crypto 4H signal (daily G regime + 4H breakout)
After=network-online.target
Wants=network-online.target
OnFailure=regime-signal-alert.service

[Service]
Type=oneshot
User=signal
Group=signal
WorkingDirectory=/opt/regime-signal/repo
ExecStartPre=/usr/bin/git -C /opt/regime-signal/repo pull --ff-only --quiet
ExecStartPre=/usr/bin/python3 -m py_compile /opt/regime-signal/repo/run_signal.py
ExecStart=/usr/bin/python3 -u run_signal.py
ExecStartPost=/usr/bin/curl -fsS -m 15 --retry 3 https://hc-ping.com/<UUID>
TimeoutStartSec=900
StandardOutput=append:/opt/regime-signal/logs/signal.log
StandardError=append:/opt/regime-signal/logs/signal.log
```

**`/etc/systemd/system/regime-signal.timer`**

```ini
[Unit]
Description=Run crypto signal 5min after each 4H close

[Timer]
# 4H 边界 00/04/08/12/16/20，收盘后 5 分钟跑
OnCalendar=*-*-* 00,04,08,12,16,20:05:00
# 关机/重启错过的班次，开机后补跑一次（不会重复发信，有台账去重）
Persistent=true
AccuracySec=10s

[Install]
WantedBy=timers.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now regime-signal.timer
systemctl list-timers regime-signal.timer
sudo systemctl start regime-signal.service   # 立刻手动跑一次验证
sudo journalctl -u regime-signal.service -n 40 --no-pager
tail -40 /opt/regime-signal/logs/signal.log
```

### 4.1 ExecStartPre 里的两个细节

- **`git pull --ff-only`**：只在能快进时更新，冲突就失败而不是产生 merge 提交。想冻结某个版本就直接注释掉这行。
- **`py_compile` 预检**：pull 到坏代码时会在这一步失败，service 不执行，于是保留上一版行为，同时触发 `OnFailure` 告警。**这是防止「静默变成不工作」的关键一步。**

---

## 5. 心跳与告警（必须有）

**这是搬家最大的新增风险**：GitHub 挂了有 GitHub 给你发红邮件；服务器挂了，没人会告诉你。

### 5.1 心跳（dead man's switch）

1. 注册 healthchecks.io（免费版够用），建一个 check
2. Period 设 **4 小时**，Grace 设 **30 分钟**
3. 把 ping URL 填进 service 的 `ExecStartPost`

逻辑：只要脚本没跑完，4.5 小时内收不到 ping，它给你发邮件。**检的是「进程活着」，不是「有没有信号」** —— 这两件事必须分开。

### 5.2 失败告警

**`/etc/systemd/system/regime-signal-alert.service`**

```ini
[Unit]
Description=Alert when crypto signal service fails

[Service]
Type=oneshot
User=signal
WorkingDirectory=/opt/regime-signal/repo
# 复用已有发信通道，主题直接标红
ExecStart=/usr/bin/python3 -c "import notify_mail as N, sys; \
  sys.argv=['x','--always','--force']; \
  c=N.load_cfg(); \
  print('[告警] regime-signal.service 失败，详见 journalctl -u regime-signal')"
```

> 更省事的做法：`OnFailure` 直接调用的不是 Python，而是一个只做 SMTP 发信的 10 行脚本。上面的写法是示意，**落地时会写成一个独立的 `alert_fail.py`**，避免和主逻辑纠缠。

---

## 6. 切流：从 GitHub 切到服务器

按顺序做，每一步都可回滚：

| 步 | 动作 | 验证 |
|---|---|---|
| 1 | 服务器上 `--dry` 跑通 | 判定输出正确 |
| 2 | 服务器真发一封（`--force`） | 邮箱收到、内容正确 |
| 3 | **停掉 GitHub 的 schedule** | 改 `signal.yml`：删 `schedule:`，只留 `workflow_dispatch` |
| 4 | 观察 2 天 | 每天至少一封；心跳正常；无重复信 |

**第 3 步不能省。** 两套台账各自独立，双跑就会重复发信 —— 11-13 已经出现过一次（本地信号信 + 云端日报）。

### 6.1 回滚

把 `signal.yml` 的 `schedule:` 段恢复、`systemctl disable --now regime-signal.timer`。一分钟内回到原状。

---

## 7. 运维手册

| 症状 | 查什么 |
|---|---|
| 收不到邮件 | `journalctl -u regime-signal.service -n 80`；看是否有 `[已发送]` |
| 跑了但没发 | 正常。无动作 + 当天已有信 = 静默。**收到心跳 ping 说明进程正常** |
| 心跳断了 | 机器挂了 / 网络断了 / timer 没起来：`systemctl list-timers` |
| 数据标「落后 N 根」 | Binance 归档延迟。等下一班；或做第 8 节 |
| 磁盘满 | `du -sh /opt/regime-signal/repo/.cache_binance`，删除旧 zip 缓存 |

日志轮转（避免长期跑撑爆）：

```bash
sudo tee /etc/logrotate.d/regime-signal > /dev/null <<'EOF'
/opt/regime-signal/logs/signal.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
EOF
```

---

## 8. 这一轮不做的事：实时 K 线补全

搬家的**主要价值**其实是这一条：服务器能直连 `api.binance.com`，可以补最近 48 根 1H，把「落后一整天然」的窗口彻底消掉。GitHub Actions 做不到，因为 runner 只能拿静态归档。

但改取数逻辑需要按既定规矩来，所以不在本轮：

1. 新增 `fetch_binance_rest.py`：拉最近 48 根 1H，与归档合并（按 ts 去重，REST 优先）
2. 默认关闭，通过环境变量 `BINANCE_REST=1` 开启
3. 灰度 3 天：每天记录「纯归档」与「归档+REST」两套结果的差异（bar4h、urgent 列表、持仓 held）
4. 差异可解释（只是把缺失补齐、不改历史）→ 默认开启
5. 差异不可解释 → 回退，只保留告警块

验收标准：REST 补数后，`bar4h` 在所有品种上一致，且 `_lag` 告警块消失。

---

## 9. 成本

| 项 | 费用 |
|---|---|
| 香港轻量服务器（最低配） | 约 24–40 元/月 |
| healthchecks.io | 免费 |
| cron-job.org（不再需要） | — |
| GitHub Actions（信号部分释放） | — |
| **合计新增** | **约 24–40 元/月** |

---

## 10. 待确认

- [ ] 服务器位置与厂商（决定第 1.4 节的测试结果）
- [ ] 系统版本（Ubuntu / Debian / Rocky，决定 install.sh 的包管理器分支）
- [ ] 是否要 `git pull --ff-only` 自动跟随 main（想稳就把这行注释掉，手动更新）
- [ ] 心跳方案：healthchecks.io 还是「每日收尾信」自检
