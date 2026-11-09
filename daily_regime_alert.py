#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_regime_alert.py —— 日线市场状态（趋势）判断 + 每日邮件提醒

功能：
  1) 取 21 个品种日线（加密 Binance spot / 贵金属、商品能源、宏观 Yahoo ETF，经代理 127.0.0.1:10809）
  2) 用 G 方案最终状态机计算当下趋势：UP（上升）/ RANGE（震荡）/ DOWN（下降）
  3) 生成 HTML 邮件正文（分四组表格 + 简洁说明），经 SMTP（默认 QQ 邮箱）发送

用法：
  python daily_regime_alert.py              # 取数 + 判断 + 发邮件（需 mail_config.json）
  python daily_regime_alert.py --dry-run    # 只判断，打印到控制台，不发邮件

配置：mail_config.json（与脚本同目录），模板见 mail_config.json.example。
  若 mail_config.json 不存在或 auth_code 为空，自动退化为 dry-run（打印 + 存 regime_report.html）。

状态机规则（G 方案最终版）：
  S0  MA120 初始化：策略起点 close>=MA120 → UP，否则 DOWN（只一次）
  S1  趋势→震荡：RSI14 连续 2 日站上 70/30 后「回落离开」极值 → RANGE（强趋势 RSI 持续
      超买/超卖不离场则不切，避免单边暴涨暴跌误切）
  S2  震荡→趋势：收盘上穿 MA60 → UP、下穿 → DOWN；或 RSI 连续 2 日重新站上 70/跌破 30
      （range 再触发，解决 MA60 单侧暴涨暴跌永不穿越的锁死）
  S3  日线 RSI 背离（最高优先级）：顶背离→DOWN、底背离→UP，无论当前状态
  S4  反向穿越纠错（仅对 RANGE 穿越进入的趋势态生效）：连续 2 日反向站稳 MA60 → 纠错
  S5  背离失败检测：背离后价格突破背离摆动极值价连续 2 日 → 背离失败，回 RANGE
仅用 Python 标准库，无第三方依赖。
"""
import bisect
import json
import os
import sys
import time
import smtplib
import urllib.request
import urllib.error
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta

from visualize_btc_regime import daily_states, PROXY

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
MAIL_CFG = os.path.join(HERE, "mail_config.json")
REPORT_HTML = os.path.join(HERE, "regime_report.html")
START = "2024-01-01"      # 策略判定起点（与可视化一致）
WARM_START = "2021-01-01"  # 数据起点：日线 MA120 热身 + 周线 MA24 热身（2021 起周线约 300 周）
# 周线参数 = 日线参数的 ÷5 机械映射，自由度为 0：120 交易日≈24 周、60 交易日≈12 周。
# 不允许为周线单独调参（否则等于在 18 个品种上开了两个自由度，极易过拟合）。
WEEKLY_MA_SLOW, WEEKLY_MA_FAST = 120 // 5, 60 // 5

ASSETS = [
    # 加密（Hyperliquid 永续合约日线；统一源，无地域封锁、无 key、无限流；USD/枚）
    {"key": "BTC",  "label": "BTC 比特币",    "group": "加密", "source": "hyperliquid", "symbol": "BTC",  "unit": "USD/枚"},
    {"key": "ETH",  "label": "ETH 以太坊",    "group": "加密", "source": "hyperliquid", "symbol": "ETH",  "unit": "USD/枚"},
    {"key": "SOL",  "label": "SOL Solana",    "group": "加密", "source": "hyperliquid", "symbol": "SOL",  "unit": "USD/枚"},
    {"key": "BNB",  "label": "BNB",           "group": "加密", "source": "hyperliquid", "symbol": "BNB",  "unit": "USD/枚"},
    {"key": "XRP",  "label": "XRP 瑞波",      "group": "加密", "source": "hyperliquid", "symbol": "XRP",  "unit": "USD/枚"},
    {"key": "AAVE", "label": "AAVE",          "group": "加密", "source": "hyperliquid", "symbol": "AAVE", "unit": "USD/枚"},
    {"key": "LINK", "label": "LINK Chainlink","group": "加密", "source": "hyperliquid", "symbol": "LINK", "unit": "USD/枚"},
    {"key": "HYPE", "label": "HYPE Hyperliquid","group": "加密", "source": "hyperliquid", "symbol": "HYPE", "unit": "USD/枚"},
    {"key": "CRV",  "label": "CRV Curve",     "group": "加密", "source": "hyperliquid", "symbol": "CRV",  "unit": "USD/枚"},
    {"key": "UNI",  "label": "UNI Uniswap",   "group": "加密", "source": "hyperliquid", "symbol": "UNI",  "unit": "USD/枚"},
    # 商品（Yahoo 期货连续）
    {"key": "GOLD", "label": "黄金 GC 期货",  "group": "商品", "source": "yahoo", "symbol": "GC=F", "unit": "美元/盎司"},
    {"key": "SI",   "label": "白银 SI 期货",  "group": "商品", "source": "yahoo", "symbol": "SI=F", "unit": "美元/盎司"},
    {"key": "HG",   "label": "铜 HG 期货",    "group": "商品", "source": "yahoo", "symbol": "HG=F", "unit": "美元/磅"},
    {"key": "CL",   "label": "WTI 原油 CL",   "group": "商品", "source": "yahoo", "symbol": "CL=F", "unit": "美元/桶"},
    {"key": "BZ",   "label": "布伦特原油 BZ", "group": "商品", "source": "yahoo", "symbol": "BZ=F", "unit": "美元/桶"},
    # 宏观（Yahoo ETF）
    {"key": "QQQ",  "label": "纳指 QQQ",      "group": "宏观", "source": "yahoo", "symbol": "QQQ", "unit": "美元"},
    {"key": "SPY",  "label": "标普 SPY",      "group": "宏观", "source": "yahoo", "symbol": "SPY", "unit": "美元"},
    {"key": "TLT",  "label": "美债 TLT",      "group": "宏观", "source": "yahoo", "symbol": "TLT", "unit": "美元"},
    # A 股指数（腾讯行情为主源、东方财富为备源；指数无需复权；单位：点）
    {"key": "SH",     "label": "上证综指",  "group": "A股", "source": "ashare",
     "symbol": "sh000001", "em_secid": "1.000001", "unit": "点"},
    {"key": "HS300",  "label": "沪深300",   "group": "A股", "source": "ashare",
     "symbol": "sh000300", "em_secid": "1.000300", "unit": "点"},
    {"key": "CYB",    "label": "创业板指",  "group": "A股", "source": "ashare",
     "symbol": "sz399006", "em_secid": "0.399006", "unit": "点"},
    {"key": "ZZ1000", "label": "中证1000",  "group": "A股", "source": "ashare",
     "symbol": "sh000852", "em_secid": "1.000852", "unit": "点"},
]

GROUP_ORDER = ["加密", "商品", "宏观", "A股"]

# A 股时区与收盘时刻：15:00（北京时间）之后当日 bar 才算完整
CN_TZ = timezone(timedelta(hours=8))
CN_CLOSE_HOUR = 15


# ---------------- 取数 ----------------
def http_get(url, referer=None, timeout=30, retries=3, direct=False):
    """direct=True 时强制直连——国内行情源（腾讯/东财）不应绕道本地海外代理。"""
    proxy = None if direct else PROXY
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if referer:
        headers["Referer"] = referer
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} -> {last}")


def fetch_binance_daily(symbol, start_dt):
    """从 start_dt 起抓 Binance 日线（startTime 分页，绕开单次 limit=1000 上限）。"""
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows, cursor = [], start_ms
    while cursor < end_ms:
        # data-api.binance.vision = 币安官方公开数据镜像，免地域封锁（api.binance.com 对美国 IP 返回 451）
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1d"
               f"&startTime={cursor}&limit=1000")
        data = json.loads(http_get(url))
        if not data:
            break
        for k in data:
            if k[0] >= end_ms:
                break
            rows.append({
                "date": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                "close": float(k[4]),
            })
        if len(data) < 1000:
            break
        cursor = data[-1][0] + 1
        time.sleep(0.3)
    seen = {}
    for r in rows:
        seen.setdefault(r["date"], r)
    return [seen[d] for d in sorted(seen)]


def fetch_yahoo_daily(symbol, start_dt):
    p1 = int(start_dt.timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}"
           f"?period1={p1}&period2={p2}&interval=1d&includePrePost=false&events=div%2Csplit")
    raw = http_get(url, referer="https://finance.yahoo.com/")
    result = json.loads(raw).get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"Yahoo empty result for {symbol}")
    r = result[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators", {}).get("quote") or [{}])[0]
    c = q.get("close") or []
    rows = []
    for i, t in enumerate(ts):
        close = c[i] if i < len(c) and c[i] is not None else None
        if close is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
            "close": close,
        })
    return rows


def http_post(url, payload, timeout=30, retries=3):
    if PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    else:
        opener = urllib.request.build_opener()
    headers = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8")
    last = None
    for i in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"POST failed after {retries} tries: {url} -> {last}")


def fetch_hyperliquid_daily(symbol):
    """Hyperliquid 原生 API 日线（短历史币种 HYPE 用，覆盖 2024-11 TGE 起）。"""
    url = "https://api.hyperliquid.xyz/info"
    payload = {"type": "candleSnapshot",
               "req": {"coin": symbol, "interval": "1d",
                       "startTime": 0, "endTime": int(time.time() * 1000)}}
    data = json.loads(http_post(url, payload))
    rows = []
    for c in data:
        rows.append({
            "date": datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "close": float(c["c"]),
        })
    return rows


def drop_unclosed(rows):
    """剔除未收盘日线（date >= 今日 UTC）。适用于加密 / 美股 / 商品（按 UTC 日线）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [r for r in rows if r["date"] < today]
    return out


def drop_unclosed_cn(rows):
    """A 股：北京时间 15:00 收盘，15:00 之后当日 bar 已完整。

    必须与 drop_unclosed 分开——定时任务在 21:30（北京）运行，此刻 UTC 仍是同一天，
    用 UTC 判断会把当天已收盘的 A 股 K 线删掉，导致 MA/RSI 静默偏移一天。"""
    now = datetime.now(CN_TZ)
    today = now.strftime("%Y-%m-%d")
    if now.hour >= CN_CLOSE_HOUR:
        return [r for r in rows if r["date"] <= today]
    return [r for r in rows if r["date"] < today]


def fetch_tencent_daily(code, count=2000):
    """腾讯行情日线（A 股指数，主源）。count 上限 2000 根（约 8 年），超出会返回空结构。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{count},qfq")
    data = json.loads(http_get(url, direct=True))
    node = (data.get("data") or {}).get(code) or {}
    # 指数无复权，返回键为 day（个股前复权时为 qfqday）
    bars = node.get("day") or node.get("qfqday") or []
    out = []
    for b in bars:
        if len(b) >= 3:
            out.append({"date": b[0], "close": float(b[2])})
    return out


def fetch_eastmoney_daily(secid, lmt=2000):
    """东方财富日线（A 股指数，备源）。secid：1.xxxxxx 沪市 / 0.xxxxxx 深市。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55"
           f"&klt=101&fqt=1&lmt={lmt}&end=20500101")
    data = json.loads(http_get(url, direct=True))
    lines = ((data.get("data") or {}).get("klines")) or []
    out = []
    for line in lines:
        p = line.split(",")
        if len(p) >= 3:
            out.append({"date": p[0], "close": float(p[2])})
    return out


def fetch_ashare_daily(a):
    """A 股指数：腾讯主源 → 东财备源，任一拿到足量数据即返回。

    两源同一交易日收盘价实测逐位一致，可互为校验。"""
    errs = []
    try:
        rows = fetch_tencent_daily(a["symbol"])
        if len(rows) >= 300:
            return rows
        errs.append(f"tencent 仅 {len(rows)} 根")
    except Exception as e:  # noqa: BLE001
        errs.append(f"tencent: {e}")
    try:
        rows = fetch_eastmoney_daily(a["em_secid"])
        if len(rows) >= 300:
            return rows
        errs.append(f"eastmoney 仅 {len(rows)} 根")
    except Exception as e:  # noqa: BLE001
        errs.append(f"eastmoney: {e}")
    raise RuntimeError("A股双源均失败 —— " + " | ".join(errs))


# ---------------- 指标与状态机 ----------------
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    g = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    if len(g) < n:
        return out
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def compute_regime(closes, dates, start=None, ma_slow=120, ma_fast=60):
    """G 方案最终状态机回放，返回最后状态的关键字段。

    与可视化脚本同源：调用 daily_states（ob=70 / os=30 / 背离 / 两日确认 /
    反向穿越纠错 / 背离失败检测），提取最后状态、最近一次触发原因与持续天数。
    ma_slow/ma_fast：日线 120/60；周线传 WEEKLY_MA_SLOW/FAST（24/12，÷5 机械映射）。"""
    start = start or START
    s0 = bisect.bisect_left(dates, start)
    last = len(closes) - 1
    if s0 < ma_slow:
        # 短历史品种（如 HYPE，2024-12 才 TGE）：不足以在 start 前热身满慢线，
        # 退化为「数据起点 + 慢线长度」作为动态判定起点
        s0 = ma_slow
    if s0 >= len(closes):
        return {"state": "N/A", "reason": "insufficient_data",
                "as_of": dates[last] if dates else ""}
    st, _div, triggers = daily_states(closes, s0, 70, 30, True, 2, True,
                                      ma_slow=ma_slow, ma_fast=ma_fast)
    rsi = rsi_wilder(closes)
    ma60, ma120 = sma(closes, ma_fast), sma(closes, ma_slow)
    state = st[last]
    trigger, since_idx = "init_ma120", s0
    for i in range(last, s0 - 1, -1):
        if triggers[i]:
            trigger, since_idx = triggers[i], i
            break
    return {
        "state": state,
        "trigger": trigger,
        "trigger_date": dates[since_idx],
        "close": closes[last],
        "rsi": rsi[last],
        "ma60": ma60[last],
        "ma120": ma120[last],
        "since_days": last - since_idx,
        "as_of": dates[last],
    }


def compute_regime_weekly(rows, start=None):
    """周线状态：同一 G 方案状态机，MA24/MA12（= MA120/MA60 的 ÷5 机械映射）。

    rows 为日线 [{date, close}]；先聚合成周线再回放状态机。
    周线不足 WEEKLY_MA_SLOW+5 根时返回 N/A（不硬凑结论）。"""
    from visualize_btc_regime import to_weekly
    start = start or START
    wrows = to_weekly(rows)
    if len(wrows) < WEEKLY_MA_SLOW + 5:
        return {"state": "N/A", "reason": "weekly_insufficient",
                "as_of": wrows[-1]["date"] if wrows else "", "n_weeks": len(wrows)}
    wc = [r["close"] for r in wrows]
    wd = [r["date"] for r in wrows]
    reg = compute_regime(wc, wd, start=start, ma_slow=WEEKLY_MA_SLOW, ma_fast=WEEKLY_MA_FAST)
    reg["n_weeks"] = len(wrows)
    return reg


# ---------------- 状态展示 ----------------
STATE_CN = {"UP": "上升", "RANGE": "震荡", "DOWN": "下降", "N/A": "数据不足"}
STATE_COLOR = {"UP": "#d23c3c", "RANGE": "#b8860b", "DOWN": "#1a7f37", "N/A": "#888888"}
TRIGGER_CN = {
    "init_ma120": "MA120 初始化",
    "divergence_top": "顶背离（切下降）",
    "divergence_bottom": "底背离（切上升）",
    "div_fail_top": "顶背离失败（回震荡）",
    "div_fail_bottom": "底背离失败（回震荡）",
    "div_expiry_top": "顶背离过期（回震荡）",
    "div_expiry_bottom": "底背离过期（回震荡）",
    "rsi_overbought_exit": "RSI 离开超买 70（转震荡）",
    "rsi_oversold_exit": "RSI 离开超卖 30（转震荡）",
    "cross_fix_up": "反向穿越纠错（站上 MA60→上升）",
    "cross_fix_down": "反向穿越纠错（跌破 MA60→下降）",
    "ma60_cross_up": "MA60 上穿（转上升）",
    "ma60_cross_down": "MA60 下穿（转下降）",
    "rsi_retrigger_up": "RSI 再触发（震荡转上升）",
    "rsi_retrigger_down": "RSI 再触发（震荡转下降）",
}


def trig_cn(key, ma_slow=120, ma_fast=60):
    """触发原因中文：MA120 / MA60 字样按实际周期替换（周线为 MA24 / MA12）。"""
    txt = TRIGGER_CN.get(key, key)
    return txt.replace("MA120", f"MA{ma_slow}").replace("MA60", f"MA{ma_fast}")


def resonance_gate(daily_state, weekly_state):
    """日线与周线的方向关系。返回 (gate, 说明)。

    gate:
      pass  —— 方向一致（或周线震荡，不构成反向）
      block —— 日线与周线方向相反（状态描述，不是禁止交易的指令）
      na    —— 日线无方向或周线数据不足，不适用

    重要：此函数最初按「风险闸门」设计（周线反向就不做逆势单）。该假设已用 2021-2026
    全 22 品种样本验证并证伪——冲突格的前瞻收益没有显著劣势（20 日 p=0.59/0.75），
    闸门版策略仅跑赢 9/22 品种（等于掷硬币）。因此返回值现在只用于**状态描述**，
    不接入任何信号过滤。详见 backtest_multi_tf.py / backtest_multi_tf.html。
    """
    if daily_state in (None, "", "N/A") or weekly_state in (None, "", "N/A"):
        return "na", "周线数据不足"
    if daily_state == "RANGE":
        return "na", "日线震荡，无方向"
    if daily_state == "UP" and weekly_state == "DOWN":
        return "block", "日线上升、周线下降"
    if daily_state == "DOWN" and weekly_state == "UP":
        return "block", "日线下降、周线上升"
    if daily_state == weekly_state:
        return "pass", "日周同向"
    return "pass", "周线震荡，不构成反向"


GATE_CN = {"pass": "一致", "block": "反向", "na": "—"}
GATE_COLOR = {"pass": "#1a7f37", "block": "#b8860b", "na": "#999"}


def fmt_price(p):
    """按价格量级自适应小数位，避免 DOGE/SUI 等低价币显示成 0。"""
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    return f"{p:,.4f}"


def _render_table(rows):
    trs = ""
    for r in rows:
        state = r["regime"]["state"]
        color = STATE_COLOR.get(state, "#888")
        wreg = r.get("weekly") or {}
        wstate = wreg.get("state")
        gate = r.get("gate", "na")
        if wstate:
            wcell = f"<span style='color:{STATE_COLOR.get(wstate, '#888')};'>{STATE_CN.get(wstate, wstate)}</span>"
        else:
            wcell = "—"
        if gate == "block":
            wcell += " <span style='color:#c00;font-weight:bold;'>⚠拦截</span>"
        trs += (
            f"<tr>"
            f"<td>{r['label']} <span style='color:#999;font-size:11px;'>{r.get('unit','')}</span></td>"
            f"<td style='background:{color};color:#fff;font-weight:bold;text-align:center;'>"
            f"{STATE_CN.get(state, state)}</td>"
            f"<td style='text-align:center;'>{wcell}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('close'))}</td>"
            f"<td style='text-align:right;'>{r['regime'].get('rsi') or 0:.1f}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('ma60'))}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('ma120'))}</td>"
            f"<td style='white-space:nowrap;color:#666;'>{r['regime'].get('trigger_date', '—')}</td>"
            f"<td>{trig_cn(r['regime'].get('trigger', ''))}</td>"
            f"<td style='text-align:center;'>{r['regime'].get('since_days', '-')}</td>"
            f"</tr>"
        )
    return (
        f"<table border='0' cellpadding='6' cellspacing='0' "
        f"style='border-collapse:collapse;font-size:13px;'>"
        f"<tr style='background:#f0f0f0;text-align:left;'>"
        f"<th>品种</th><th>日线</th><th>周线</th><th>收盘</th><th>RSI14</th><th>MA60</th><th>MA120</th>"
        f"<th>触发日期</th><th>最近触发</th><th>持续(日)</th></tr>{trs}</table>"
    )


def render_html(reports, run_at):
    groups = {}
    for r in reports:
        groups.setdefault(r.get("group", "其他"), []).append(r)
    blocks = ""
    for g in GROUP_ORDER:
        if g not in groups:
            continue
        blocks += (f"<h3 style='margin:16px 0 4px;color:#444;border-left:4px solid #888;"
                   f"padding-left:8px;'>{g}（{len(groups[g])}）</h3>{_render_table(groups[g])}")
    n_total = len(reports)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:'Microsoft YaHei',Arial,sans-serif;color:#222;">
  <h2 style="margin-bottom:4px;">市场状态（趋势）提醒</h2>
  <p style="color:#888;margin-top:0;">生成时间：{run_at}（北京时间）｜监控 {n_total} 个品种</p>
  {blocks}
  <p style="font-size:13px;color:#555;margin-top:12px;">
    判定规则（日线状态机 G 方案）：MA120 初始化定方向（收盘≥MA120→上升，否则下降）→
    趋势中 RSI 连续 2 日站上 70/30 后回落离开极值转震荡（强趋势 RSI 持续超买/超卖不离场则不切）→
    震荡中收盘上穿 MA60 转上升、下穿转下降，或 RSI 连续 2 日重新站上 70/跌破 30（再触发）。
    叠加日线 RSI 背离（顶背离切下降、底背离切上升，最高优先级）、反向穿越纠错、背离失败检测。
  </p>
  <p style="font-size:12px;color:#999;">本提醒为规则化市场状态分类，非投资建议。数据源：Hyperliquid（加密永续）/ Yahoo Finance（商品期货·ETF）/ 腾讯行情（A 股指数，东财备源）。</p>
</body></html>"""
    return html


def send_mail(cfg, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["receivers"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    server = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
    try:
        server.login(cfg["sender"], cfg["auth_code"])
        server.sendmail(cfg["sender"], cfg["receivers"], msg.as_string())
    finally:
        server.quit()


def load_mail_config():
    if not os.path.exists(MAIL_CFG):
        return None
    with open(MAIL_CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("auth_code") or not cfg.get("sender") or not cfg.get("receivers"):
        return None
    cfg.setdefault("smtp_host", "smtp.qq.com")
    cfg.setdefault("smtp_port", 465)
    return cfg


def main():
    dry_run = "--dry-run" in sys.argv
    cfg = load_mail_config()
    if cfg is None and not dry_run:
        print("[提示] 未检测到 mail_config.json 或授权码为空，退化为 dry-run（只判断、不发邮件）。")

    warm_dt = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    reports = []
    for a in ASSETS:
        try:
            if a["source"] == "binance":
                rows = fetch_binance_daily(a["symbol"], warm_dt)
            elif a["source"] == "hyperliquid":
                rows = fetch_hyperliquid_daily(a["symbol"])
            elif a["source"] == "ashare":
                rows = fetch_ashare_daily(a)
            else:
                rows = fetch_yahoo_daily(a["symbol"], warm_dt)
            # A 股按北京时间 15:00 判断当日是否已收盘，其余按 UTC 日线
            rows = drop_unclosed_cn(rows) if a["source"] == "ashare" else drop_unclosed(rows)
            closes = [r["close"] for r in rows]
            dates = [r["date"] for r in rows]
            reg = compute_regime(closes, dates)
            wreg = compute_regime_weekly(rows)
            gate, gate_note = resonance_gate(reg["state"], wreg.get("state"))
            reports.append({"key": a["key"], "label": a["label"],
                            "group": a.get("group", "其他"), "unit": a.get("unit", ""),
                            "regime": reg, "weekly": wreg, "gate": gate, "gate_note": gate_note})
            print(f"[{a['key']:5}] 日{STATE_CN.get(reg['state'], reg['state']):3} "
                  f"周{STATE_CN.get(wreg.get('state'), wreg.get('state') or '—'):3} "
                  f"{GATE_CN.get(gate, gate):2} "
                  f"close={reg.get('close', 0):,.4f}  RSI={reg.get('rsi') or 0:.1f}  "
                  f"as_of={reg.get('as_of')}  触发={reg.get('trigger')}  持续{reg.get('since_days', '-')}日")
        except Exception as e:  # noqa: BLE001
            print(f"[{a['key']:5}] 取数/计算失败: {e}")
            reports.append({"key": a["key"], "label": a["label"],
                            "group": a.get("group", "其他"), "unit": a.get("unit", ""),
                            "regime": {"state": "N/A", "reason": str(e)},
                            "weekly": {"state": "N/A"}, "gate": "na", "gate_note": ""})

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    subject = f"市场状态提醒 {run_at[:10]}"
    html = render_html(reports, run_at)
    with open(REPORT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    if dry_run or cfg is None:
        print(f"\n[dry-run] HTML 已写入 {REPORT_HTML}")
        return

    send_mail(cfg, subject, html)
    print(f"\n[OK] 邮件已发送至 {', '.join(cfg['receivers'])}，主题：{subject}")


if __name__ == "__main__":
    sys.exit(main())
