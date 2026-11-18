#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_lookahead.py —— 底仓闸门在回测里有没有一格前视？

起因：_exp_delay_1h.py 的 D=0 结果与「完全不开闸门」几乎相同，而 D>=1 全部落到
同一个数。这不是延迟的锅，是**索引错位**的征兆。

回测的收益约定：nav *= 1 + pos[k] × r[k]，其中 r[k] = close[k]/close[k-1]。
也就是说 pos[k] 是「在 bar k 期间持有的仓位」，必须在 close[k-1] 之前就定下来。

逐项核对：
  · 加仓层 sig[k]：signals() 里是 `for k in range(i+1, exit_j+1): sig[k] = side`，
    入场在 close[i]，仓位从 i+1 生效 → **无前视** ✓
  · 日线状态 BASE[rg4[k]]：align_regime_safe 取「严格早于该 4H 日期的最后一根日线」
    → **无前视** ✓
  · 底仓闸门 gate[k]：用 b4[k]["close"] 与「前一交易日 MA」比较 —— 价格用的是**当根**收盘，
    却乘在「到该收盘为止」的收益上 → **有一格前视** ✗

本脚本把三种写法并排跑，确认差异量级：
  A 现状（gate[k]）
  B 平移一格（gate[k-1]，= 无前视的正确写法）
  C 不开闸门
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import base_gate as BG
import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import load_bars, to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h

CAP = 500_000
F4 = BT.FUND_DAILY / 6.0
START_NAV = "2020-09-01"
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
W = PC.weights_for(CAP)
TGT = min(1.0, sum(W.values()))
YEARS = list(range(2020, 2027))


def pct(v, n=9):
    return ("%+.1f%%" % (v * 100)).rjust(n)


def pad(s, n, right=True):
    s = str(s)
    return s.ljust(n) if right else s.rjust(n)


def load_coin(c):
    if c == "HYPE":
        b4 = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
        d1 = [dict(x, date=x["date"][:10]) for x in load_hype("1d")]
    else:
        b = load_bars(c, "1h", tail=False)
        b4 = agg4h(b)
        d1 = to_daily(b)
    reg, blood = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg)
    bl4 = align_regime_safe(b4, blood)
    atr = atr_wilder(b4, 14)
    sig, _ = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
GATE = {c: BG.series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}
print("加载完成\n", flush=True)


def get_nav(c, w, base):
    b4, sig = DATA[c]["b4"], DATA[c]["sig"]
    pv = [(base[k] + BT.ADD * sig[k]) * COEF[c][k] * VT.KAPPA for k in range(len(b4))]
    qv = PC.CAP_4H_WORST.get(c) or 1e12
    res, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pv[k] * r
        nav *= x if x > 0 else 0.0
        sl = pv[k] - base[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pv[k] != pv[k - 1]:
            d = abs(pv[k] - pv[k - 1])
            nav *= (1 - BT.COST * d)
            nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * d / qv) * d)
        res[b4[k]["date"]] = nav
    return res


FIRST = {}


def run(gates):
    NAVS = {}
    for c in ALL12:
        base = [BT.BASE.get(DATA[c]["rg4"][k], 0.0) * gates[c][k]
                for k in range(len(gates[c]))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
        if c not in FIRST:
            FIRST[c] = min(NAVS[c])
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    for d in ALLD:
        live = [c for c in ALL12 if FIRST[c] <= d]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if (cq is None) or (q != cq) or (set(units) != set(live)):
            wsum = sum(uw.values())
            if units:
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c]) for c in live) / v / 2
                v *= (1 - min(g, 1.0) * 7e-4)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((d, v))
    first, lastv = {}, {}
    for d, v in out:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    yr = {y: lastv[y] / first[y] - 1 for y in first}
    cum = sum(CAP * yr.get(y, 0.0) for y in YEARS)
    pk, dd = out[0][1], 0.0
    for _, v in out:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return yr, cum, dd


def shift(s):
    return [s[0]] + s[:-1]


ONE = {c: [1.0] * len(DATA[c]["b4"]) for c in ALL12}
ARMS = [
    ("A 现状 gate[k]（含前视）", GATE),
    ("B 平移 gate[k-1]（正确）", {c: shift(GATE[c]) for c in ALL12}),
    ("C 不开闸门", ONE),
]

print("=" * 104)
print("三种写法的口径 A 逐年（本金恒 50 万）")
print("=" * 104)
print(pad("写法", 28) + "".join(pad(y, 9) for y in YEARS) + pad("累计", 11) + pad("回撤", 9))
print("-" * 104)
R = {}
for nm, g in ARMS:
    yr, cum, dd = run(g)
    R[nm] = (yr, cum, dd)
    print(pad(nm, 28) + "".join(pct(yr.get(y, 0.0)) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9))

print("\n" + "=" * 104)
print("结论")
print("=" * 104)
ca = R["A 现状 gate[k]（含前视）"][1]
cb = R["B 平移 gate[k-1]（正确）"][1]
cc = R["C 不开闸门"][1]
print(f"  前视贡献：{ca / 1e4:.0f} 万 - {cb / 1e4:.0f} 万 = {(ca - cb) / 1e4:.0f} 万")
print(f"  正确写法相对不开闸门：{(cb - cc) / 1e4:+.0f} 万"
      f"（回撤 {R['B 平移 gate[k-1]（正确）'][2] * 100:.1f}% vs {R['C 不开闸门'][2] * 100:.1f}%）")
print(f"  → 若为负：闸门在无前视口径下不值得开，应当撤回。")
