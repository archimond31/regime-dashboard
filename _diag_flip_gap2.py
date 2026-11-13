#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补两项：① 翻 DOWN 但最终没建空的比例 ② 卖掉底仓之后价格往哪走。"""
import statistics, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_skeleton as BT
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC","ETH","SOL","BNB","XRP","LINK","CRV","UNI","AAVE"]
COINS = BIN9 + ["HYPE"]
for _k in list(V2.SYMBOLS):
    if _k not in BIN9: del V2.SYMBOLS[_k]
V2.load_all()

tot_flip = tot_short = 0
fwd = {20: [], 42: [], 120: []}
cur = {}

for c in COINS:
    if c == "HYPE":
        b4 = load_hype("4h"); b4 = [dict(x, date=x["date"][:10]) for x in b4]
        rg4 = align_regime_safe(b4, regime_series(to_daily(b4)))
    else:
        b, rg, _a, _b = V2.load_all()[c]
        b4 = agg4h(b); rg4 = [rg[x["src"]] for x in b4]
    sig, trades = BT.signals(b4, rg4, atr_wilder(b4,14), vol_q=BT.VOL_Q, coin=c)

    nflip = 0
    last_flip = None
    for k in range(1, len(rg4)):
        if rg4[k] == "DOWN" and rg4[k-1] != "DOWN":
            nflip += 1
            last_flip = k
            nxt = [t for t in trades if t["side"] < 0 and t["i"] >= k]
            if nxt: tot_short += 1
            # 卖出底仓后价格怎么走
            for h in fwd:
                j = k + h
                if j < len(b4):
                    fwd[h].append(b4[j]["close"] / b4[k]["close"] - 1)
    tot_flip += nflip
    # 当前这轮
    if rg4[-1] == "DOWN" and last_flip is not None:
        nxt = [t for t in trades if t["side"] < 0 and t["i"] >= last_flip]
        cur[c] = (b4[last_flip]["date"], (nxt[0]["i"] - last_flip) if nxt else None,
                  b4[-1]["date"])

print("=" * 62)
print(f"翻 DOWN 共 {tot_flip} 次，其中最终建了空的 {tot_short} 次 "
      f"（{tot_short/tot_flip*100:.0f}%）")
print(f"→ 另外 {tot_flip-tot_short} 次（{(1-tot_short/tot_flip)*100:.0f}%）"
      f"卖掉底仓后直到状态翻回去，都没等到做空信号")
print()
print("卖掉底仓之后，价格往哪走（负=继续跌，说明卖对了；正=卖早了）")
print("-" * 62)
print(f"{'持有期':<12}{'中位':>10}{'均值':>10}{'下跌占比':>10}{'样本':>8}")
for h, lab in ((20,"20根≈3.3天"),(42,"42根≈7天"),(120,"120根≈20天")):
    v = fwd[h]
    if v:
        print(f"{lab:<12}{statistics.median(v)*100:>+9.1f}%{statistics.mean(v)*100:>+9.1f}%"
              f"{sum(1 for x in v if x<0)/len(v)*100:>9.0f}%{len(v):>8}")
print()
print("当前这轮（最新状态仍为 DOWN 的币）")
print("-" * 62)
print(f"{'币':<6}{'翻空日':>12}{'建空日隔':>12}")
for c,(fd,gap,last) in sorted(cur.items()):
    gs = f"{gap} 根 ≈{gap*4/24:.0f} 天" if gap is not None else "未建空"
    print(f"{c:<6}{fd:>12}{gs:>16}   数据截至 {last}")
