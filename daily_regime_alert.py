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
from datetime import datetime, timezone

from visualize_btc_regime import daily_states, PROXY

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
MAIL_CFG = os.path.join(HERE, "mail_config.json")
REPORT_HTML = os.path.join(HERE, "regime_report.html")
START = "2024-01-01"      # 策略判定起点（与可视化一致）
WARM_START = "2023-06-01"  # 数据起点（供 MA120 热身 120+ 根）

ASSETS = [
    # 加密（Binance 现货日线）
    {"key": "BTC",  "label": "BTC 比特币",    "group": "加密", "source": "binance", "symbol": "BTCUSDT"},
    {"key": "ETH",  "label": "ETH 以太坊",    "group": "加密", "source": "binance", "symbol": "ETHUSDT"},
    {"key": "SOL",  "label": "SOL Solana",    "group": "加密", "source": "binance", "symbol": "SOLUSDT"},
    {"key": "BNB",  "label": "BNB",           "group": "加密", "source": "binance", "symbol": "BNBUSDT"},
    {"key": "XRP",  "label": "XRP 瑞波",      "group": "加密", "source": "binance", "symbol": "XRPUSDT"},
    {"key": "AAVE", "label": "AAVE",          "group": "加密", "source": "binance", "symbol": "AAVEUSDT"},
    {"key": "LINK", "label": "LINK Chainlink","group": "加密", "source": "binance", "symbol": "LINKUSDT"},
    {"key": "HYPE", "label": "HYPE Hyperliquid","group": "加密", "source": "hyperliquid", "symbol": "HYPE"},
    {"key": "CRV",  "label": "CRV Curve",     "group": "加密", "source": "binance", "symbol": "CRVUSDT"},
    {"key": "UNI",  "label": "UNI Uniswap",   "group": "加密", "source": "binance", "symbol": "UNIUSDT"},
    # 贵金属（Yahoo COMEX 期货连续，美元/盎司）
    {"key": "GOLD", "label": "黄金 GC 期货",  "group": "贵金属", "source": "yahoo", "symbol": "GC=F"},
    {"key": "SLV",  "label": "白银 SI 期货",  "group": "贵金属", "source": "yahoo", "symbol": "SI=F"},
    # 商品能源（Yahoo ETF）
    {"key": "UNG",  "label": "天然气 UNG",    "group": "商品能源", "source": "yahoo", "symbol": "UNG"},
    {"key": "USO",  "label": "原油 WTI USO",  "group": "商品能源", "source": "yahoo", "symbol": "USO"},
    {"key": "CPER", "label": "铜 CPER",       "group": "商品能源", "source": "yahoo", "symbol": "CPER"},
    # 宏观（Yahoo ETF）
    {"key": "QQQ",  "label": "纳指 QQQ",      "group": "宏观", "source": "yahoo", "symbol": "QQQ"},
    {"key": "SPY",  "label": "标普 SPY",      "group": "宏观", "source": "yahoo", "symbol": "SPY"},
    {"key": "TLT",  "label": "美债 TLT",      "group": "宏观", "source": "yahoo", "symbol": "TLT"},
]

GROUP_ORDER = ["加密", "贵金属", "商品能源", "宏观"]


# ---------------- 取数 ----------------
def http_get(url, referer=None, timeout=30, retries=3):
    if PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
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
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d"
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
    """剔除未收盘日线（date >= 今日 UTC）。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = [r for r in rows if r["date"] < today]
    return out


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


def compute_regime(closes, dates):
    """G 方案最终状态机回放，返回最后状态的关键字段。

    与可视化脚本同源：调用 daily_states（ob=70 / os=30 / 背离 / 两日确认 /
    反向穿越纠错 / 背离失败检测），提取最后状态、最近一次触发原因与持续天数。"""
    s0 = bisect.bisect_left(dates, START)
    last = len(closes) - 1
    if s0 < 120:
        # 短历史品种（如 HYPE，2024-11 才 TGE）：不足以在 2024-01-01 前热身 120 根，
        # 退化为「数据起点 + 120 根热身」作为动态判定起点
        s0 = 120
    if s0 >= len(closes):
        return {"state": "N/A", "reason": "insufficient_data",
                "as_of": dates[last] if dates else ""}
    st, _div, triggers = daily_states(closes, s0, 70, 30, True, 2, True)
    rsi = rsi_wilder(closes)
    ma60, ma120 = sma(closes, 60), sma(closes, 120)
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
        trs += (
            f"<tr>"
            f"<td>{r['label']}</td>"
            f"<td style='background:{color};color:#fff;font-weight:bold;text-align:center;'>"
            f"{STATE_CN.get(state, state)}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('close'))}</td>"
            f"<td style='text-align:right;'>{r['regime'].get('rsi') or 0:.1f}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('ma60'))}</td>"
            f"<td style='text-align:right;'>{fmt_price(r['regime'].get('ma120'))}</td>"
            f"<td style='white-space:nowrap;color:#666;'>{r['regime'].get('trigger_date', '—')}</td>"
            f"<td>{TRIGGER_CN.get(r['regime'].get('trigger', ''), r['regime'].get('trigger', ''))}</td>"
            f"<td style='text-align:center;'>{r['regime'].get('since_days', '-')}</td>"
            f"</tr>"
        )
    return (
        f"<table border='0' cellpadding='6' cellspacing='0' "
        f"style='border-collapse:collapse;font-size:13px;'>"
        f"<tr style='background:#f0f0f0;text-align:left;'>"
        f"<th>品种</th><th>状态</th><th>收盘</th><th>RSI14</th><th>MA60</th><th>MA120</th>"
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
  <p style="font-size:12px;color:#999;">本提醒为规则化市场状态分类，非投资建议。数据源：Binance / Yahoo Finance。</p>
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
            else:
                rows = fetch_yahoo_daily(a["symbol"], warm_dt)
            rows = drop_unclosed(rows)
            closes = [r["close"] for r in rows]
            dates = [r["date"] for r in rows]
            reg = compute_regime(closes, dates)
            reports.append({"key": a["key"], "label": a["label"],
                            "group": a.get("group", "其他"), "regime": reg})
            print(f"[{a['key']:5}] {STATE_CN.get(reg['state'], reg['state']):3}  "
                  f"close={reg.get('close', 0):,.4f}  RSI={reg.get('rsi', 0):.1f}  "
                  f"as_of={reg.get('as_of')}  触发={reg.get('trigger')}  持续{reg.get('since_days', '-')}日")
        except Exception as e:  # noqa: BLE001
            print(f"[{a['key']:5}] 取数/计算失败: {e}")
            reports.append({"key": a["key"], "label": a["label"],
                            "group": a.get("group", "其他"),
                            "regime": {"state": "N/A", "reason": str(e)}})

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
