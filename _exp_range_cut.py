#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_range_cut.py —— 如果只是想降低 RANGE 段风险，代价是多少

_exp_range_ch.py 已证明「RANGE 段用价格位置择时减仓」是负价值：
触发仅 5.1%、且打不过同等平均水平的恒定折扣。

那剩下的唯一选择是「恒定折扣」—— 无择时，纯风险偏好。
本脚本给出折扣率 → 收益/回撤 的完整菜单，让决策者自己选点，
而不是假装存在一个又能降风险又能增收益的免费午餐。
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


def run(rng_cut=1.0, up_cut=1.0):
    """RANGE 段底仓 × rng_cut，UP 段底仓 × up_cut（一般不动，留作敏感性）。"""
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = up_cut + BT.ADD * sig[k]
            elif rg == "RANGE":
                p = rng_cut
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
            base_k = (up_cut if rg4[k] == "UP" else
                      (rng_cut if rg4[k] == "RANGE" else 0.0)) * COEF[c][k] * VT.KAPPA
            sl = pv[k] - base_k
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


print("=" * 104)
print("RANGE 段底仓折扣菜单（无择时，纯风险偏好；其余规则完全不变）")
print("=" * 104)
print(pad("RANGE底仓", 12) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8))
print("-" * 104)
base = None
for cut in (1.00, 0.95, 0.90, 0.80, 0.70, 0.50, 0.00):
    cv = run(rng_cut=cut)
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    if base is None:
        base = cum
    print(pad("%.2f" % cut, 12) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8)
                                          for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8))

print("\n  读法：折扣越低，收益与回撤同向下降。")
print("  · Calmar 若基本不变 → 纯粹是「缩放」，没有风险效率改善")
print("  · Calmar 若随折扣下降 → 连缩放都不划算，RANGE 段是正贡献资产")
