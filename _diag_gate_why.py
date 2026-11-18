#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_gate_why.py —— 底仓闸门为什么改善这么多？把 +68 万拆开看

【1】逐年拆：差额到底是哪几年给的？有没有一年撑起全部
【2】敞口法拆：ΔPnL = Σ w × (底仓系数差) × VT × 4H 收益，按年 / 按币
【3】事件法：每次「1.0→0.25」后面价格是继续跌还是涨回去 —— 砍对了多少次
【4】机制量化：底仓占敞口的比重、闸门生效的时间占比、生效期 vs 非生效期的收益
【5】2022 / 2025 解剖：那两年具体躲掉了哪几段
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


def pad(s, n, right=True):
    s = str(s)
    return s.ljust(n) if right else s.rjust(n)


def pct(v, n=8, sign=True):
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
    sig, _ = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
print("加载完成", flush=True)

COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
BASE_A = {c: [BT.BASE.get(DATA[c]["rg4"][k], 0.0) for k in range(len(DATA[c]["b4"]))]
          for c in ALL12}
BASE_G = {c: BT.base_series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}
GATE = {c: BG.series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}


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


NAVS_A = {c: get_nav(c, W.get(c, 0.09), BASE_A[c]) for c in ALL12}
NAVS_G = {c: get_nav(c, W.get(c, 0.09), BASE_G[c]) for c in ALL12}
FIRST = {c: min(NAVS_A[c]) for c in ALL12}
ALLD = sorted(set().union(*[set(NAVS_A[c]) for c in ALL12]))


def curve(NAVS, start=START_NAV, end=None):
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    for d in [x for x in ALLD if x >= start and (end is None or x < end)]:
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


def yearly(cv):
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    yr = {y: lastv[y] / first[y] - 1 for y in first if len(first) >= 0}
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return yr, dd


CVA, CVG = curve(NAVS_A), curve(NAVS_G)
YA, DDA = yearly(CVA)
YG, DDG = yearly(CVG)

# ================= 【1】逐年 =================
print("\n" + "=" * 96)
print("【1】逐年收益：差额是哪几年给的")
print("=" * 96)
print(pad("年", 8) + pad("基线", 10) + pad("闸门", 10) + pad("差(pp)", 11)
      + pad("折算 U", 13) + pad("闸门打折天数占比", 16))
print("-" * 96)
tot_diff = 0.0
for y in YEARS:
    if y not in YA:
        continue
    # 该年闸门打折的 bar 占「底仓本应在场」的 bar 的比例
    nq = nt = 0
    for c in ALL12:
        b4, rg4, g = DATA[c]["b4"], DATA[c]["rg4"], GATE[c]
        for k in range(len(b4)):
            d = b4[k]["date"]
            if not (d >= (START_NAV if y == 2020 else f"{y}-01-01") and d < f"{y + 1}-01-01"):
                continue
            if BT.BASE.get(rg4[k], 0.0) > 0:
                nt += 1
                if g[k] < 1.0:
                    nq += 1
    dp = (YG[y] - YA[y]) * 100
    du = CAP * (YG[y] - YA[y])
    tot_diff += du
    print(pad(y, 8) + pct(YA[y], 10) + pct(YG[y], 10) + pad("%+.1f" % dp, 11)
          + pad("%+.1f 万" % (du / 1e4), 13) + pad("%.0f%%" % (nq / nt * 100 if nt else 0), 16))
print("-" * 96)
print(pad("合计", 8) + pad("", 10) + pad("", 10) + pad("", 11)
      + pad("%+.1f 万" % (tot_diff / 1e4), 13))
print(f"最大回撤：基线 {DDA * 100:.1f}%  →  闸门 {DDG * 100:.1f}%")

# ================= 【2】敞口法 =================
print("\n" + "=" * 96)
print("【2】敞口法分解：ΔPnL = Σ 权重 × (底仓系数差) × VT系数 × κ × 4H收益")
print("    正数 = 闸门帮你省下的亏损；负数 = 闸门让你错过的上涨")
print("=" * 96)
by_year, by_coin = {}, {}
for c in ALL12:
    b4 = DATA[c]["b4"]
    for k in range(1, len(b4)):
        d = b4[k]["date"]
        if d < START_NAV:
            continue
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        db = (GATE[c][k] - 1.0) * BT.BASE.get(DATA[c]["rg4"][k], 0.0) \
            * COEF[c][k] * VT.KAPPA
        v = W.get(c, 0.09) * db * r
        by_year[int(d[:4])] = by_year.get(int(d[:4]), 0.0) + v
        by_coin[c] = by_coin.get(c, 0.0) + v
print(pad("年", 8) + pad("闸门贡献", 14))
print("-" * 96)
for y in sorted(by_year):
    print(pad(y, 8) + pad("%+.1f 万" % (by_year[y] * CAP / 1e4), 14))
print(pad("合计", 8) + pad("%+.1f 万" % (sum(by_year.values()) * CAP / 1e4), 14))
print("\n按币（同一口径，正=省下的亏损）：")
print("  " + "  ".join(f"{c}:{by_coin[c] * CAP / 1e4:+.0f}万"
                       for c in sorted(by_coin, key=lambda x: -by_coin[x])))

# ================= 【3】事件法 =================
print("\n" + "=" * 96)
print("【3】事件法：每次「全额 → 打折」之后，价格继续跌还是涨回去")
print("=" * 96)
ev = []
for c in ALL12:
    b4, g, rg4 = DATA[c]["b4"], GATE[c], DATA[c]["rg4"]
    k = 1
    while k < len(b4):
        if BT.BASE.get(rg4[k], 0) > 0 and g[k] < 1.0 and g[k - 1] >= 1.0:
            # 找回到全额（或状态转 DOWN）的那根
            e = k + 1
            while e < len(b4) and BT.BASE.get(rg4[e], 0) > 0 and g[e] < 1.0:
                e += 1
            if e < len(b4):
                chg = b4[e]["close"] / b4[k]["close"] - 1
                ev.append((c, b4[k]["date"], e - k, chg))
                k = e
        k += 1
print(f"  共 {len(ev)} 次打折事件（12 币合计）")
good = [x for x in ev if x[3] < 0]
bad = [x for x in ev if x[3] >= 0]
print(f"  砍对了（期间下跌）：{len(good)} 次 / {len(good) / len(ev):.0%}   "
      f"中位 {sorted(x[3] for x in good)[len(good) // 2] * 100:+.1f}%")
print(f"  砍错了（期间上涨）：{len(bad)} 次 / {len(bad) / len(ev):.0%}   "
      f"中位 {sorted(x[3] for x in bad)[len(bad) // 2] * 100:+.1f}%")
print(f"  每次事件平均持续 {sum(x[2] for x in ev) / len(ev):.1f} 根 4H"
      f"（≈{sum(x[2] for x in ev) / len(ev) * 4:.0f} 小时）")
print("\n  最赚的 5 次（躲掉跌幅最大）：")
for c, d, n, ch in sorted(ev, key=lambda x: x[3])[:5]:
    print(f"    {pad(c, 6)} {d}  {n:>3} 根   {ch * 100:+7.1f}%")
print("  最亏的 5 次（错过上涨最多）：")
for c, d, n, ch in sorted(ev, key=lambda x: -x[3])[:5]:
    print(f"    {pad(c, 6)} {d}  {n:>3} 根   {ch * 100:+7.1f}%")

# ================= 【4】机制量化 =================
print("\n" + "=" * 96)
print("【4】机制：底仓在组合里到底占多大、闸门有多少时间在生效")
print("=" * 96)
add_share = BT.ADD / (BT.BASE.get("UP", 1.0) + BT.ADD)
print(f"  峰值敞口结构：底仓 1.0 + 加仓 0.5 = 1.5 → 底仓占 {1 / 1.5:.0%}，"
      f"加仓层只占 {add_share:.0%}")
print(f"  也就是说 12 币组合里约 {1 / 1.5:.0%} 的风险敞口挂在一个「只有状态翻转才退出」的仓上。")
er_hit, er_all, r_hit, r_all = 0, 0, 0.0, 0.0
for c in ALL12:
    b4 = DATA[c]["b4"]
    for k in range(1, len(b4)):
        if b4[k]["date"] < START_NAV:
            continue
        if BT.BASE.get(DATA[c]["rg4"][k], 0) <= 0:
            continue
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        er_all += 1
        r_all += r
        if GATE[c][k] < 1.0:
            er_hit += 1
            r_hit += r
print(f"  底仓在场（UP/RANGE）的 4H bar 共 {er_all:,} 根，其中 {er_hit / er_all:.0%} 被闸门打折")
print(f"  打折期间平均 4H 收益 {r_hit / er_hit * 100:+.3f}%  "
      f"vs 未打折期间 {(r_all - r_hit) / (er_all - er_hit) * 100:+.3f}%")
print("  → 闸门挑出来的那段，本身就是负漂移的；这才是它赚钱的全部理由。")

# ================= 【5】2022 / 2025 解剖 =================
print("\n" + "=" * 96)
print("【5】两个关键年：闸门在那一年具体做了什么")
print("=" * 96)
for y in (2022, 2025):
    print(f"\n  ── {y} 年（基线 {YA[y] * 100:+.1f}% → 闸门 {YG[y] * 100:+.1f}%，"
          f"差 {(YG[y] - YA[y]) * 100:+.1f}pp）")
    rows = []
    for c in ALL12:
        b4 = DATA[c]["b4"]
        n_hit = n_all = 0
        for k in range(len(b4)):
            d = b4[k]["date"]
            if not (d >= f"{y}-01-01" and d < f"{y + 1}-01-01"):
                continue
            if BT.BASE.get(DATA[c]["rg4"][k], 0) <= 0:
                continue
            n_all += 1
            if GATE[c][k] < 1.0:
                n_hit += 1
        if n_all:
            rows.append((c, n_all, n_hit, n_hit / n_all))
    for c, n_all, n_hit, sh in sorted(rows, key=lambda x: -x[3])[:5]:
        print(f"    {pad(c, 6)} 底仓在场 {n_all:>4} 根，打折 {n_hit:>4} 根（{sh:>4.0%}）")
