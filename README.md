# regime-dashboard — 市场趋势状态机监控面板

基于 **G 方案**（MA120 初始化 + RSI 阈值 30/70 + 两日确认 + 背离信号 + 背离失败检测）的日线趋势状态机，覆盖 18 个品种，三类分组：

| 分组 | 品种 |
| --- | --- |
| 加密 | BTC、ETH、SOL、BNB、XRP、AAVE、LINK、HYPE、CRV、UNI（Hyperliquid 永续） |
| 商品 | 黄金 GC=F、白银 SI=F、铜 HG=F、WTI 原油 CL=F、布伦特 BZ=F（Yahoo 期货） |
| 宏观 | 纳指 QQQ、标普 SPY、长债 TLT |

## 状态定义

- **▲ 上升（UP）** / **▼ 下降（DOWN）** / **— 震荡（RANGE）**
- 策略起点以 close 与 MA120 的关系定初始趋势；其后由 RSI(14) 进出 30/70 边界 + 两日确认驱动状态切换；RSI 背离为最高优先级信号，背离失败自动回震荡。

## 数据源

- 加密：Hyperliquid 永续合约（candleSnapshot 原生 API，免地域封锁/免限流）
- 商品 / 宏观：Yahoo Finance（期货连续 / ETF）
- 代理开关：本地默认 `127.0.0.1:10809`，云端（GitHub Actions）设 `REGIME_PROXY=none` 直连

## 文件结构

- `visualize_btc_regime.py` — G 方案状态机核心（`daily_states`）+ 取数 + 绘图
- `visualize_eth_gold_regime.py` — Yahoo 取数（`fetch_yahoo_daily`）
- `daily_regime_alert.py` — 18 品种清单 + 每日邮件监控（本地 SMTP）
- `build_dashboard.py` — 生成自包含 HTML 面板（状态统计 + 筛选 tab + 趋势区间图）
- `.github/workflows/dashboard.yml` — GitHub Actions 定时构建 + 部署到 Pages

## 云端部署（电脑关机也能运行）

GitHub Actions 每天 **北京时间 21:30**（UTC 13:30）自动：

1. 拉取 18 品种最新日线
2. 跑 G 方案状态机
3. 生成 `regime_dashboard.html`
4. 部署到 GitHub Pages 的 `gh-pages` 分支

访问地址：`https://<你的用户名>.github.io/regime-dashboard/`

## 本地运行

```bash
# 生成面板
python build_dashboard.py

# 每日邮件监控（需 mail_config.json）
python daily_regime_alert.py
```
