#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
visualize_eth_gold_regime.py —— ETH / 黄金(GLD) 2024 年以来行情区间可视化（G 方案最终版）

复用 visualize_btc_regime 的 G 方案状态机（方案 a 30/70 + 背离 + 两日确认 + 反向穿越纠错
+ RANGE 再触发 + <3天碎片合并），对 ETH（Binance ETHUSDT 日线）与黄金（Yahoo GLD 日线）
各画一张区间图。规则与 BTC 完全一致，仅品种/数据源不同。

数据源（均经本地代理 127.0.0.1:10809）：
  ETH   Binance spot klines ETHUSDT 1d（24x7 UTC 日界）
  GOLD  Yahoo Finance GLD（SPDR Gold Shares ETF，连续、无换月跳空）
"""
import bisect
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from visualize_btc_regime import (
    daily_states, merge_short_segments, build, gen_svg, seg_table,
    STATE_CN, fetch_binance_daily, PROXY,
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
WARM_START = "2023-06-01"   # 数据起点（供 MA120 热身 120+ 根）
START = "2024-01-01"        # 策略判定起点
MIN_DAYS = 3                # 合并持续天数 < 3 的过渡碎片


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
    raise RuntimeError(f"GET failed: {url} -> {last}")


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
        rows.append({"date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "close": close})
    return rows


def drop_unclosed(rows):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [r for r in rows if r["date"] < today]


def run_asset(label, rows):
    rows = drop_unclosed(rows)
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    s0 = bisect.bisect_left(dates, START)
    # G 方案：70/30 + 背离 + 两日确认 + 反向穿越纠错 + RANGE 再触发
    st, div, _trig = daily_states(closes, s0, 70, 30, True, 2, True)
    pts, segs = build(dates, closes, st)
    raw_n = len(segs)
    segs = merge_short_segments(segs, MIN_DAYS)
    merged = raw_n - len(segs)
    marks = []
    if div:
        bull, bear = div
        for i in range(s0, len(dates)):
            if bull[i]:
                marks.append({"date": dates[i], "price": closes[i], "kind": "bull"})
            if bear[i]:
                marks.append({"date": dates[i], "price": closes[i], "kind": "bear"})
    print(f"\n===== {label}：{raw_n} 个区间 → 过滤 <{MIN_DAYS}天 后 {len(segs)} 个 =====")
    if div:
        bs = [dates[i] for i in range(s0, len(dates)) if div[0][i]]
        br = [dates[i] for i in range(s0, len(dates)) if div[1][i]]
        print(f"  底背离(切上升) {len(bs)} 次: {bs}")
        print(f"  顶背离(切下降) {len(br)} 次: {br}")
    for s in segs:
        chg = (s["end_close"] / s["start_close"] - 1) * 100
        print(f"  [{STATE_CN[s['state']]:3}] {s['start']} ~ {s['end']}  {s['days']:>3}天  "
              f"{s['start_close']:>9,.2f} -> {s['end_close']:>9,.2f}  ({chg:+.1f}%)")
    return {"label": label, "pts": pts, "segs": segs, "marks": marks,
            "merged": merged, "as_of": dates[-1]}


def main():
    warm_dt = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    eth_rows = fetch_binance_daily("ETHUSDT", warm_dt)
    gld_rows = fetch_yahoo_daily("GLD", warm_dt)

    results = [
        run_asset("ETH 以太坊 (Binance ETHUSDT)", eth_rows),
        run_asset("黄金 GLD ETF (Yahoo GLD)", gld_rows),
    ]

    blocks = []
    for r in results:
        merged_note = f"｜合并 <{MIN_DAYS}天碎片 {r['merged']} 个" if r["merged"] else ""
        blocks.append(
            f"<h3 style='margin:14px 0 2px;'>{r['label']}{merged_note}</h3>"
            f"{gen_svg(r['pts'], r['segs'], r['marks'])}"
            f"{seg_table(r['segs'])}")

    as_of = max(r["as_of"] for r in results)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>ETH 与黄金 2024以来行情区间（G方案）</title></head>
<body style="font-family:'Microsoft YaHei',Arial,sans-serif;color:#222;margin:16px;">
<h2 style="margin:4px 0;">ETH 与黄金 2024 年以来行情区间 · G 方案最终版</h2>
<p style="color:#888;margin-top:0;">数据源：Binance ETHUSDT 日线 / Yahoo GLD 日线（截至 {as_of}）。红=上升 / 黄=震荡 / 绿=下降。MA120 均在策略起点 {START} 初始化。<br>规则与 BTC 完全一致（G 方案）：趋势→震荡 = RSI 连续 2 日站上极值(70/30)后回落离开极值；震荡→趋势 = MA60 穿越或 RSI 再触发；反向穿越纠错 + 背离失败检测（价格突破背离摆动极值→纠错回震荡）；持续 &lt;{MIN_DAYS} 天碎片已合并。</p>
{''.join(blocks)}
<p style="font-size:12px;color:#999;">▲=底背离(切上升) ▼=顶背离(切下降)。状态为规则化分类，非投资建议。</p>
</body></html>"""

    out = "eth_gold_regime_2024.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
