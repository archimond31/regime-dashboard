#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_range_all.py —— 「RANGE 一律允许底仓」vs「按来路打折」对照

用户方案（2026-11-20）：
    震荡一律允许开底仓（取消来路折扣），
    震荡转上升底仓留着，震荡转下降底仓清掉。

后两条是**状态机翻转的自动结果**，不需要额外规则：
    RANGE → UP   底仓系数 1.0 → 1.0，留着
    RANGE → DOWN 底仓系数 1.0 → 0.0，清掉（这是 BASE 表 DOWN=0 决定的）
真正要改的只有一条：RANGE_RATIO_BY_SRC["DOWN"] 从 0.0 改成 1.0。

七段输出：
  【0】前提：RANGE 段的来路分布（确认改动影响面）
  【1】主对照：两臂逐年 + 全期
  【2】前视检查（原样 vs 左移一格）
  【3】时间切分 + 滚动 250 日
  【4】分来路贡献拆解（新方案多出来的底仓，落在哪类段上）
  【5】RANGE 段内的实际收益归因
口径 A（本金恒 50 万、每年提走、收益率简单相加）= 唯一自洽口径。
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


def seg_list(c):
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    n, out, k = len(b4), [], 0
    while k < n:
        s = rg4[k]
        j = k
        while j < n and rg4[j] == s:
            j += 1
        if s in STATES and b4[k]["date"][:10] >= START_NAV:
            src = rg4[k - 1] if k > 0 and rg4[k - 1] in STATES else "?"
            dst = rg4[j] if j < n and rg4[j] in STATES else "?"
            out.append(dict(st=s, src=src, dst=dst, i=k, j=j, bars=j - k,
                            ret=b4[j - 1]["close"] / b4[k]["close"] - 1))
        k = j
    return out


def range_by_src(c, cut_dn, shift=0):
    """逐 bar 的 RANGE 底仓系数。UP→RANGE 恒 1.0（不动），只调 DOWN→RANGE。"""
    rg4 = DATA[c]["rg4"]
    out = [1.0] * len(rg4)
    src = None
    for k in range(len(rg4)):
        s = rg4[k]
        if s == "RANGE":
            if src is None:
                prev = rg4[k - 1] if k > 0 else None
                src = prev if prev in ("UP", "DOWN") else "?"
            out[k] = cut_dn if src == "DOWN" else 1.0
        else:
            src = None
    if shift:
        return [1.0] + out[:-1]
    return out


def run(cut_dn, shift=0):
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = range_by_src(c, cut_dn, shift)
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = b[k] + 0.5 * sig[k]
            elif rg == "RANGE":
                p = b[k]
            elif rg == "DOWN":
                p = 0.5 * sig[k]
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
    return [cv[i + win][1] / cv[i][1] - 1 for i in range(0, len(cv) - win, 21)]


# ============================================================
# 【0】来路分布
# ============================================================
print("=" * 104)
print("【0】RANGE 段来路分布 —— 本次改动只影响「DOWN→RANGE」这一类")
print("=" * 104)
segs = []
for c in ALL12:
    segs += [dict(s, c=c) for s in seg_list(c)]
rng = [s for s in segs if s["st"] == "RANGE"]
print(f"  12 币 RANGE 段合计 {len(rng)} 个\n")
print(pad("来路", 8) + pad("段数", 8) + pad("平均根数", 10) + pad("段内平均", 14)
      + pad("下跌占比", 10) + pad("累计贡献", 12))
print("-" * 104)
for src in ("UP", "DOWN", "?"):
    xs = [s for s in rng if s["src"] == src]
    if not xs:
        continue
    tot = sum(x["ret"] for x in xs)
    print(pad(src, 8) + pad(len(xs), 8)
          + pad("%.0f" % (sum(x["bars"] for x in xs) / len(xs)), 10)
          + pad("%+.2f%%" % (tot / len(xs) * 100), 14)
          + pad("%.0f%%" % (sum(1 for x in xs if x["ret"] < 0) / len(xs) * 100), 10)
          + pad("%+.0f%%" % (tot * 100), 12))

# ============================================================
# 【1】主对照
# ============================================================
ARMS = [("A0 现行 UP1.0/DN0.0", 0.0), ("A1 中间 UP1.0/DN0.5", 0.5),
        ("A2 新案 UP1.0/DN1.0", 1.0)]
print("\n" + "=" * 112)
print("【1】主对照（口径 A，本金恒 50 万）")
print("=" * 112)
print(pad("臂", 24) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8))
print("-" * 112)
R = {}
for nm, dn in ARMS:
    cv = run(dn)
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    R[nm] = dict(yr=yr, cum=cum, dd=dd, calmar=cal, cv=cv, dn=dn, cagr=cagr)
    print(pad(nm, 24) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8))

a0 = R["A0 现行 UP1.0/DN0.0"]
print("\n  相对现行（A0）的差额：")
print(pad("臂", 24) + pad("累计差额", 13) + pad("回撤变化", 12) + pad("Calmar变化", 13)
      + pad("年化变化", 12))
print("-" * 112)
for nm, _dn in ARMS[1:]:
    r = R[nm]
    print(pad(nm, 24) + pad("%+.0f 万" % ((r["cum"] - a0["cum"]) / 1e4), 13)
          + pad("%+.1fpp" % ((r["dd"] - a0["dd"]) * 100), 12)
          + pad("%+.2f" % (r["calmar"] - a0["calmar"]), 13)
          + pad("%+.2fpp" % ((r["cagr"] - a0["cagr"]) * 100), 12))

print("\n  逐年（%）：" + pad("", 8) + "".join(pad(y, 9) for y in YEARS))
print("-" * 112)
for nm, _dn in ARMS:
    print(pad(nm, 24) + "".join(pad("%+.0f" % (R[nm]["yr"].get(y, 0) * 100), 9)
                                for y in YEARS))

# ============================================================
# 【2】前视检查
# ============================================================
print("\n" + "=" * 112)
print("【2】前视检查：原样 vs 左移一格（来路只用 rg4[k-1]，理论上应当无差）")
print("=" * 112)
for nm, dn in ARMS:
    cvs = run(dn, shift=1)
    _yr, cum_s, _dd, _cg = stat(cvs)
    dif = (R[nm]["cum"] - cum_s) / 1e4
    verdict = "前视嫌疑" if dif > 30 else ("安全" if abs(dif) < 15 else "轻微")
    print(pad(nm, 24) + pad("%.0f 万" % (R[nm]["cum"] / 1e4), 11)
          + pad("%.0f 万" % (cum_s / 1e4), 11) + pad("%+.0f 万" % dif, 11)
          + pad(verdict, 12))

# ============================================================
# 【3】时间切分 + 滚动
# ============================================================
print("\n" + "=" * 112)
print("【3】时间切分 + 滚动 250 日")
print("=" * 112)


def sub(cv, a, b):
    v = [p for p in cv if a <= p[0] < b]
    if len(v) < 30:
        return None
    pk, dd = v[0][1], 0.0
    for _, x in v:
        pk = max(pk, x)
        dd = min(dd, x / pk - 1)
    return v[-1][1] / v[0][1] - 1, dd


print(pad("臂", 24) + pad("2020-2023", 14) + pad("2024-2026", 14)
      + pad("滚动胜现行", 13) + pad("中位差", 11) + pad("最差", 10))
print("-" * 112)
base_roll = roll(a0["cv"])
for nm, dn in ARMS:
    a = sub(R[nm]["cv"], "2020-01-01", "2024-01-01")
    b = sub(R[nm]["cv"], "2024-01-01", "2027-01-01")
    rs = roll(R[nm]["cv"])
    m = min(len(rs), len(base_roll))
    dif = sorted(rs[i] - base_roll[i] for i in range(m))
    win = sum(1 for x in dif if x > 0)
    print(pad(nm, 24)
          + pad("%+.0f%% （回撤 %.0f%%）" % (a[0] * 100, a[1] * 100) if a else "—", 14)
          + pad("%+.0f%% （回撤 %.0f%%）" % (b[0] * 100, b[1] * 100) if b else "—", 14)
          + pad(f"{win}/{m}", 13)
          + pad("%+.1fpp" % (dif[m // 2] * 100), 11)
          + pad("%+.1fpp" % (dif[0] * 100), 10))

# ============================================================
# 【4】增量底仓落在哪
# ============================================================
print("\n" + "=" * 112)
print("【4】新方案多出来的底仓，全部落在 DOWN→RANGE 段上 —— 那批段赚了还是亏了")
print("=" * 112)
dn_segs = [s for s in rng if s["src"] == "DOWN"]
tot = sum(s["ret"] for s in dn_segs)
print(f"  DOWN→RANGE：{len(dn_segs)} 段，平均 {tot / len(dn_segs) * 100:+.2f}%，"
      f"累计 {tot * 100:+.0f}%（每 1× 底仓名义）")
neg = [s for s in dn_segs if s["ret"] < 0]
print(f"    其中下跌 {len(neg)} 段（{len(neg) / len(dn_segs):.0%}），"
      f"平均 {sum(s['ret'] for s in neg) / len(neg) * 100:+.2f}%")
pos = [s for s in dn_segs if s["ret"] >= 0]
if pos:
    print(f"    上涨 {len(pos)} 段（{len(pos) / len(dn_segs):.0%}），"
          f"平均 {sum(s['ret'] for s in pos) / len(pos) * 100:+.2f}%")
print()
print("  最差 5 段：")
for s in sorted(dn_segs, key=lambda x: x["ret"])[:5]:
    print(f"    {s['c']:<6}{s['i'] and DATA[s['c']]['b4'][s['i']]['date'][:10]}  "
          f"{s['bars']:>4} 根   {s['ret'] * 100:+7.1f}%")
print("  最好 5 段：")
for s in sorted(dn_segs, key=lambda x: -x["ret"])[:5]:
    print(f"    {s['c']:<6}{s['i'] and DATA[s['c']]['b4'][s['i']]['date'][:10]}  "
          f"{s['bars']:>4} 根   {s['ret'] * 100:+7.1f}%")

# ============================================================
# 【5】逐币影响
# ============================================================
print("\n" + "=" * 112)
print("【5】逐币：新方案新增的底仓暴露落在哪些币上")
print("=" * 112)
print(pad("币", 8) + pad("DN→RANGE 段数", 15) + pad("新增底仓根数", 14)
      + pad("这批段累计收益", 16) + pad("平均每段", 11))
print("-" * 112)
for c in ALL12:
    xs = [s for s in dn_segs if s["c"] == c]
    if not xs:
        print(pad(c, 8) + pad(0, 15) + pad(0, 14) + pad("—", 16) + pad("—", 11))
        continue
    tot_c = sum(s["ret"] for s in xs)
    print(pad(c, 8) + pad(len(xs), 15)
          + pad(sum(s["bars"] for s in xs), 14)
          + pad("%+.0f%%" % (tot_c * 100), 16)
          + pad("%+.2f%%" % (tot_c / len(xs) * 100), 11))
print("\n  注：这是「每 1× 名义的裸价格收益」，不含 vol_target 缩放与容量权重。")
print("      实际组合影响见【1】，因为每个币的权重和再平衡路径不同。")
