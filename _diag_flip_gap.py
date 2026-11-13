#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：日线翻 DOWN（卖底仓）到 4H 建空头之间，隔多久。

策略里这两件事不是同时发生的：
  ① 日线状态翻转 → 底仓(base)从 1.0 变 0 → **卖出现货多头**
  ② 之后某根 4H 收盘跌破前 20 根低点 → **建立空头**

中间是空仓期。这里量一下它有多长。
"""
import statistics
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()

allg, percoin = [], {}

for c in COINS:
    if c == "HYPE":
        b4 = load_hype("4h")
        b4 = [dict(x, date=x["date"][:10]) for x in b4]
        rg4 = align_regime_safe(b4, regime_series(to_daily(b4)))
        coin_tag = "HYPE"
    else:
        b, rg, _a, _b = V2.load_all()[c]
        b4 = agg4h(b)
        rg4 = [rg[x["src"]] for x in b4]
    atr = atr_wilder(b4, 14)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c)

    # 状态切换点：前一根不是 DOWN、这一根是 DOWN
    gaps = []
    for k in range(1, len(rg4)):
        if rg4[k] == "DOWN" and rg4[k - 1] != "DOWN":
            # 从此处往后找第一笔空头入场
            nxt = [t for t in trades if t["side"] < 0 and t["i"] >= k]
            if not nxt:
                continue
            gap = nxt[0]["i"] - k
            gaps.append((b4[k]["date"], gap, round(gap * 4 / 24, 1)))
    percoin[c] = gaps
    allg += [g[1] for g in gaps]

print(f"{'币':<6}{'翻空次数':>9}{'建空次数':>9}{'中位间隔':>10}{'均值':>9}{'最长':>9}")
print("-" * 60)
for c in COINS:
    g = [x[1] for x in percoin[c]]
    if not g:
        print(f"{c:<6}{0:>9}{0:>9}{'—':>10}{'—':>9}{'—':>9}")
        continue
    print(f"{c:<6}{len(g):>9}{len(g):>9}{statistics.median(g):>8.0f}根"
          f"{statistics.mean(g):>8.1f}{max(g):>8}根")

print("\n" + "=" * 60)
if allg:
    allg_sorted = sorted(allg)
    n = len(allg_sorted)
    print(f"全部样本 {n} 次「翻 DOWN → 建空」")
    print(f"  中位   {statistics.median(allg):.0f} 根 4H  = {statistics.median(allg) * 4 / 24:.1f} 天")
    print(f"  均值   {statistics.mean(allg):.1f} 根    = {statistics.mean(allg) * 4 / 24:.1f} 天")
    print(f"  最短   {min(allg)} 根 = {min(allg) * 4 / 24:.1f} 天")
    print(f"  最长   {max(allg)} 根 = {max(allg) * 4 / 24:.1f} 天")
    print(f"  隔 1 根以内（当天就建空）  {sum(1 for x in allg if x <= 1) / n * 100:.0f}%")
    print(f"  隔 6 根以内（1 天内）      {sum(1 for x in allg if x <= 6) / n * 100:.0f}%")
    print(f"  隔 42 根以上（7 天以上）   {sum(1 for x in allg if x > 42) / n * 100:.0f}%")

    print("\n最长的 8 次：")
    flat = [(c, d, g) for c in COINS for d, g in [(x[0], x[1]) for x in percoin[c]]]
    flat.sort(key=lambda t: -t[2])
    for c, d, g in flat[:8]:
        print(f"  {c:<6} 翻空于 {d}   隔 {g:>3} 根 4H（{g * 4 / 24:.1f} 天）才建空")

    print("\n最短的 5 次：")
    for c, d, g in flat[-5:]:
        print(f"  {c:<6} 翻空于 {d}   隔 {g:>3} 根 4H（{g * 4 / 24:.1f} 天）才建空")
