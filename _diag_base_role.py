#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_base_role.py —— 底仓到底在贡献什么？把它和加仓层拆开看

用户质疑：现货只是开多的一个工具/市场，凭什么给它一个独立的结构性地位（1.0×）？
这个质疑要分成两件事回答：
  ① 工具层面：现货 vs 永续的真实差异只有三条 —— 资金费、强平、可否做空。
     这三条都不构成「1.0× vs 0.5×」的理由。仓位比例是风险分配，不是通道选择。
  ② 结构层面：底仓真正的身份不是「现货」，而是**一套慢速退出通道**
     （日线状态翻转才走）。加仓层是快速通道（4H 突破 + 1.5ATR 止损 + 120 根兜底）。
     两条通道速度不同，才需要分层。

所以真正该问的是：那条慢通道值不值 67% 的敞口？本脚本用六臂分解回答：
  A1 完整（底仓 1.0 + 加仓 0.5）           ← 现状
  A2 只有底仓（1.0 + 0）                   ← 慢通道独挑
  A3 只有加仓层（0 + 0.5）                 ← 快通道独挑
  A4 合并 1.5×，全用 4H 规则（0 + 1.5）     ← 取消分层、敞口不变
  A5 合并 1.0×，全用 4H 规则（0 + 1.0）     ← 取消分层、敞口降到 1.0
  A6 挪一半（0.5 + 1.0）                   ← 峰值仍 1.5，但把敞口从慢通道移到快通道

注意：不同臂的平均敞口不同，直接比累计盈亏不公平。所以同时给
「每单位平均敞口的年化」和 Calmar（收益/最大回撤）——后者才是决策依据。
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


def pct(v, n=9, sign=True):
    return pad(("%+.1f%%" % (v * 100)) if sign else ("%.1f%%" % (v * 100)), n)


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
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig, trades=trades)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
print("加载完成\n", flush=True)


def run(base_mult, add_mult):
    """base_mult: 底仓系数乘数（1.0=现状）；add_mult: 加仓层乘数（0.5=现状）。"""
    NAVS, expo = {}, {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        base = [BT.BASE.get(rg4[k], 0.0) * base_mult for k in range(len(b4))]
        pv = [(base[k] + add_mult * sig[k]) * COEF[c][k] * VT.KAPPA
              for k in range(len(b4))]
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        w = W.get(c, 0.09)
        nav, res = 1.0, {}
        k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
        for k in range(k0 + 1, len(b4)):
            d10 = b4[k]["date"][:10]
            expo[d10] = expo.get(d10, 0.0) + abs(pv[k]) * w   # 组合口径：跨币求和
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
    avg_ex = sum(expo.values()) / max(1, len(expo))
    return out, avg_ex


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
    yrs = (len(cv) / 252.0)
    cagr = (cv[-1][1] / cv[0][1]) ** (1 / max(yrs, 1e-9)) - 1
    return yr, cum, dd, cagr


ARMS = [
    ("A1 完整 1.0+0.5（现状）", 1.0, 0.5),
    ("A2 只有底仓 1.0+0", 1.0, 0.0),
    ("A3 只有加仓 0+0.5", 0.0, 0.5),
    ("A4 合并 1.5× 全 4H 规则", 0.0, 1.5),
    ("A5 合并 1.0× 全 4H 规则", 0.0, 1.0),
    ("A6 挪一半 0.5+1.0", 0.5, 1.0),
]

print("=" * 118)
print("六臂分解（口径 A，本金恒 50 万，2020-09 起）")
print("=" * 118)
print(pad("臂", 26) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 10) + pad("回撤", 9) + pad("Calmar", 8)
      + pad("平均敞口", 10) + pad("年化/敞口", 12))
print("-" * 118)
R = {}
for nm, bm, am in ARMS:
    cv, avg_ex = run(bm, am)
    yr, cum, dd, cagr = stat(cv)
    calmar = cagr / abs(dd) if dd < 0 else float("nan")
    R[nm] = dict(yr=yr, cum=cum, dd=dd, cagr=cagr, ex=avg_ex, calmar=calmar, cv=cv)
    print(pad(nm, 26) + "".join(pad("%+.1f" % (yr.get(y, 0.0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 10) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % calmar, 8) + pad("%.2f×" % avg_ex, 10)
          + pad("%+.0f%%" % (cagr / avg_ex * 100 if avg_ex else 0), 12))
print("  （平均敞口 = 组合口径 Σ|仓位|×权重 的日均值；年化/敞口 = 复利年化 ÷ 平均敞口，"
      "衡量「每 1 倍敞口一年换多少」）")

print("\n" + "=" * 118)
print("加仓层自己赚不赚钱：A3 的逐年（敞口只有 0.5×，别跟 A1 直接比金额）")
print("=" * 118)
a2, a3 = R["A2 只有底仓 1.0+0"], R["A3 只有加仓 0+0.5"]
print("  " + pad("年", 8) + pad("只有底仓", 11) + pad("只有加仓", 11) + pad("两者差", 10))
for y in YEARS:
    print("  " + pad(y, 8) + pct(a2["yr"].get(y, 0), 11) + pct(a3["yr"].get(y, 0), 11)
          + pad("%+.1f" % ((a3["yr"].get(y, 0) - a2["yr"].get(y, 0)) * 100), 10))
print(f"\n  底仓贡献 {a2['cum'] / 1e4:.0f} 万，加仓层贡献 {a3['cum'] / 1e4:.0f} 万"
      f"（口径 A 不能相加，因为两者共享本金基准，只做定性比较）")

print("\n" + "=" * 118)
print("关键对比：把敞口从慢通道挪到快通道（峰值都保持 1.5×）")
print("=" * 118)
a1, a4, a6 = R["A1 完整 1.0+0.5（现状）"], R["A4 合并 1.5× 全 4H 规则"], R["A6 挪一半 0.5+1.0"]
for nm, r in (("现状 1.0+0.5", a1), ("全快通道 0+1.5", a4), ("挪一半 0.5+1.0", a6)):
    print(f"  {pad(nm, 20)} 累计 {r['cum'] / 1e4:>4.0f} 万   回撤 {r['dd'] * 100:>6.1f}%"
          f"   Calmar {r['calmar']:.2f}   年化 {r['cagr'] * 100:>5.1f}%")
print("\n  → Calmar 更高 = 同样一分风险换到的收益更多。若「全快通道」Calmar 更高，")
print("    说明那条慢通道不但没用，还在拖后腿 —— 底仓就该合并进加仓层。")
