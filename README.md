# regime-dashboard — 市场趋势状态机监控面板

基于 **G 方案**（MA120 初始化 + RSI 阈值 30/70 + 两日确认 + 背离信号 + 背离失败检测）的日线趋势状态机，覆盖 22 个品种，四类分组：

| 分组 | 品种 |
| --- | --- |
| 加密 | BTC、ETH、SOL、BNB、XRP、AAVE、LINK、HYPE、CRV、UNI（Hyperliquid 永续） |
| 商品 | 黄金 GC=F、白银 SI=F、铜 HG=F、WTI 原油 CL=F、布伦特 BZ=F（Yahoo 期货） |
| 宏观 | 纳指 QQQ、标普 SPY、长债 TLT（Yahoo ETF） |
| A股 | 上证综指 sh000001、沪深300 sh000300、创业板指 sz399006、中证1000 sh000852（腾讯行情，东财备源） |

## 状态定义

- **▲ 上升（UP）** / **▼ 下降（DOWN）** / **— 震荡（RANGE）**
- 策略起点以 close 与 MA120 的关系定初始趋势；其后由 RSI(14) 进出 30/70 边界 + 两日确认驱动状态切换；RSI 背离为最高优先级信号，背离失败自动回震荡。

## 多周期（周线）状态

面板除了日线状态，还并列显示**周线状态**，用的是**同一套** `daily_states()` 状态机：

| 日线参数 | 周线参数 | 换算依据 |
| --- | --- | --- |
| MA120（初始化定方向） | MA24 | 120 交易日 ≈ 24 周 |
| MA60（震荡中穿越） | MA12 | 60 交易日 ≈ 12 周 |
| RSI14 / 70 / 30 / 两日确认 | 完全相同 | 不调 |

**参数是 ÷5 的机械映射，自由度为 0**——不为周线单独调参，避免在全样本上多开两个自由度。

实现要点：`to_weekly()` 按 ISO 周聚合收盘价（G 方案只依赖收盘序列，不需要 OHLC），
**只取已走完的周**（当前周未收盘不参与判定，否则周线状态会周内逐日抖动），
交易日数 < 3 的残缺周丢弃（ETF 长假周）。

### 「日周反向」不是交易信号

面板会对日线与周线方向相反的品种标注「日周反向」。这只是**状态描述**，不构成过滤规则。

原本的设计是把它当「风险闸门」（周线反向就不做逆势单）。该假设已用 2021-07 ~ 2026-11
全 22 品种样本（含 A 股）验证并**证伪**：

- 冲突格（日↑周↓ / 日↓周↑）的 20 日前瞻收益相对同向格没有显著劣势（p = 0.592 / 0.745）
- 分段一致性正好等于掷硬币（多头侧 6/11、空头侧 5/11，合计 50%）
- 闸门版策略仅跑赢 **9/22** 品种（掷硬币期望 11），等权平均 −1.6% vs 纯日线 +42.8%
- 空头侧的中位差甚至是 **+0.28%**：日线转下而周线仍上升时，做空反而更赚——与闸门假设完全相反

完整方法与数据见 `backtest_multi_tf.py`，报告见 `backtest_multi_tf.html`。
可能的原因：日线状态机本身已有反向穿越纠错 + 背离失败检测，把「逆势」提前过滤掉了，
再叠一层周线闸门属于冗余，且周线 MA24 太慢、与日线信号的时间尺度不匹配。

## 数据源

- 加密：Hyperliquid 永续合约（candleSnapshot 原生 API，免地域封锁/免限流）
- 商品 / 宏观：Yahoo Finance（期货连续 / ETF）
- A 股指数：**腾讯行情为主源**（`web.ifzq.gtimg.cn`，2000 根 ≈ 8 年）、**东方财富为备源**
  （`push2his.eastmoney.com`）。两源同一交易日收盘价逐位一致，可互为校验；任一失败自动切换。
- 代理开关：本地默认 `127.0.0.1:10809`，云端（GitHub Actions）设 `REGIME_PROXY=none` 直连。
  国内行情源（腾讯/东财）强制直连，不走本地海外代理。

### A 股收盘判断为什么单独一套

`drop_unclosed()` 用 **UTC 日期**剔除未收盘 bar，适用于加密 / 美股 / 商品。但 A 股在北京时间
**15:00** 收盘，而定时任务在 21:30（北京）运行——那一刻 UTC 还是同一天。若沿用 UTC 判断，
会把**当天已收盘的 A 股 K 线删掉**，导致 MA/RSI 静默偏移一天且不报错。

因此 `drop_unclosed_cn()` 单独按北京时间判断：15:00 之后保留当日 bar，之前剔除。
两种规则按 `source == "ashare"` 分流，不能混用。

## 文件结构

- `visualize_btc_regime.py` — G 方案状态机核心（`daily_states`，支持 `ma_slow`/`ma_fast`）+ 取数 + 绘图 + `to_weekly()`
- `visualize_eth_gold_regime.py` — Yahoo 取数（`fetch_yahoo_daily`）
- `daily_regime_alert.py` — 22 品种清单 + 每日邮件监控（本地 SMTP）+ A 股双源取数
- `build_dashboard.py` — 生成自包含 HTML 面板（状态统计 + 周线列 + 筛选 tab + 趋势区间图）
- `backtest_multi_tf.py` — 多周期共振验证（事件研究 + 显著性 + 分段一致性 + 策略口径）
- `.github/workflows/dashboard.yml` — GitHub Actions 定时构建 + 部署到 Pages

## 云端部署（电脑关机也能运行）

GitHub Actions 每天 **北京时间 21:30**（UTC 13:30）自动：

1. 拉取 22 品种最新日线
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
