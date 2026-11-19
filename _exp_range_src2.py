#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_range_src2.py —— 方向反转后的折扣菜单

_exp_range_src.py 的发现推翻了原假设：
  UP→RANGE   203 段，段内平均 +8.78%，下跌占比 35%  ← 仍在涨，该保持满仓
  DOWN→RANGE 200 段，段内平均 +0.50%，下跌占比 46%  ← 零期望，该减配

所以正确形式是「UP→RANGE 保持 1.0，DOWN→RANGE 打折」。
本脚本把 DOWN 这一侧的折扣扫一遍，找出 Calmar 与收益的交点。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
STATES = ("UP", "RANGE", "DOWN")


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
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig, trades=trades, atr=atr, bl4=bl4)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
print("加载完成\n", flush=True)


def bseq(c, cut_up=1.0, cut_dn=1.0):
    rg4 = DATA[c]["rg4"]
    out = [1.0] * len(rg4)
    src = None
    for k in range(len(rg4)):
        s = rg4[k]
        if s == "RANGE":
            if src is None:
                prev = rg4[k - 1] if k > 0 else None
                src = prev if prev in ("UP", "DOWN") else "?"
            out[k] = cut_up if src == "UP" else (cut_dn if src == "DOWN" else 1.0)
        else:
            src = None
    return out


def run(fn):
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = fn(c)
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = b[k] + BT.ADD * sig[k]
            elif rg == "RANGE":
                p = b[k]
            elif rg == "DOWN":
                p = BT.ADD * sig[k]
            else:
                p = 0.0
            pv.append(p * coef)
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        w = W.get(c, 0.09)
        nav, res = 1.0, {}
        k0 = next((k for k, x in enumerate(b4) if x["date"] >= START_NAV), len(b4))
        for k in range(k0 + 1, len(b4)):
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pv[k] * r
            nav *= x if x > 0 else 0.0
            base_k = b[k] * COEF[c][k] * VT.KAPPA
            sl = pv[k] - base_k if rg4[k] != "DOWN" else pv[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pv[k] != pv[k - 1]:
                d = abs(pv[k] - pv[k - 1])
                nav *= (1 - BT.COST * d)
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * d / qv) * d)
            res[b4[k]["date"]] = nav
        NAVS[c] = res
    FIRST = {c: min(NAVS[c]) for c in ALL12}
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
    return out


def stat(cv):
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    yr = {y: lastv[y] / first[y] - 1 for y in first}
    cum = sum(CAP * yr.get(y, 0.0) for y in YEARS)
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    yrs = len(cv) / 252.0
    cagr = (cv[-1][1] / cv[0][1]) ** (1 / max(yrs, 1e-9)) - 1
    return yr, cum, dd, cagr


def roll(cv, win=250):
    return [cv[i + win][1] / cv[i][1] - 1
            for i in range(0, len(cv) - win, 21)]


print("=" * 110)
print("DOWN→RANGE 折扣菜单（UP→RANGE 固定 1.0）")
print("=" * 110)
print(pad("DN折扣", 10) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8) + pad("滚动胜基线", 12))
print("-" * 110)
base_cv = None
base_roll = None
rows = []
for cut in (1.00, 0.75, 0.50, 0.25, 0.00):
    cv = run(lambda c, v=cut: bseq(c, 1.0, v))
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    if base_cv is None:
        base_cv, base_roll = cv, roll(cv)
        wtxt = "—"
    else:
        rs = roll(cv)
        m = min(len(rs), len(base_roll))
        w = sum(1 for i in range(m) if rs[i] > base_roll[i])
        wtxt = f"{w}/{m}"
    rows.append((cut, cum, dd, cal, wtxt))
    print(pad("%.2f" % cut, 10) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8)
                                          for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8) + pad(wtxt, 12))

b = rows[0]
print("\n  相对基线（1.00）：")
for cut, cum, dd, cal, wtxt in rows[1:]:
    print(f"    DN={cut:.2f}  累计 {(cum - b[1]) / 1e4:+5.0f} 万   "
          f"回撤 {(dd - b[2]) * 100:+5.1f}pp   Calmar {cal - b[3]:+.2f}")

print("\n  读法：")
print("  · Calmar 峰值出现在哪一档，就是「风险效率最优」")
print("  · 若峰值出现在 0.00，说明 DOWN→RANGE 段是纯负担")
print("  · 滚动胜列永远小于 50% 是正常的 —— 这是用收益换风险，不是免费午餐")
