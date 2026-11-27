#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_rebal.py —— P1-4「调仓频率 A/B」：底仓到底该多久对一次账

## 为什么有这个脚本

回测里底仓是**逐根 4H 重算**的：`pv[k] = base_series(...)[k] * coef[k]`，
所以目标一变，仓位当场跟着变，一分钱摩擦都不多付（只有换手费）。
实盘不是这样：底仓「一段一次」——只在该段第一根建/减/清，之后不动。

两条路径的差在 2026-11-27 已经算出来了：
    · 逐 bar 重算 → 目标 1,098 U（UP/RANGE 那 7 个币的现实上限）
    · 一段一次   → 实持 1,004 U
    · **欠配 94 U ≈ 本金的 4.7%**，按年化 45.5% 粗算约少赚 2.1%/年
（这条是 STRATEGY.md 挂着没做的 P1-4。）

但「补仓」不是免费的：VT 波动目标化系数每 7 天重算一次（`VT.REBAL_DAYS`），
每次重算目标就漂一点；如果每漂一点就补一笔，摩擦会吃掉一部分收益。
**所以真正要回答的不是「补不补」，而是「漂多少才值得补」**。

## 五臂

    bar      逐 bar 重算       ← 现有回测口径，收益上界（不可实现）
    seg      一段一次           ← 实盘现状，THR = ∞
    thr30    相对偏差 > 30% 才补
    thr20    相对偏差 > 20% 才补
    thr10    相对偏差 > 10% 才补
    thr05    相对偏差 > 5%  才补

「相对偏差」= |理论 − 实际| / max(|理论|, |实际|)。这样它同时处理两边：
    · 目标涨到 0  → 相对偏差 = 1，立刻清（状态转 DOWN 必须出）
    · 目标从 0 起 → 相对偏差 = 1，立刻建（建底仓那一笔）
    · 段内系数漂移 → 偏差通常 5~30%，取决于漂了多少
段首一律建到位（那本来就是「建/减/清底仓」这笔单），所以五臂的分歧**只在段内**。

## 口径

三个都报，别混：
    · 口径 A：本金恒 50 万、每年提走、收益率简单相加 → 累计盈亏（万）
    · 口径 C：固定 50 万权重跑全复利 → 只借它比 Calmar，绝对值是幻觉
    · 口径 B：全复利 + 每年按当年本金重算权重 + 容量约束 → **对外报收益只能用这个**
自检：`bar` 臂必须复现 口径 A ≈ +278 万、口径 B ≈ 10.34×，否则先别信别的臂。

## 摩擦的算法

底仓换手和加仓层换手走同一段代码（`if pv[k] != pv[k-1]`），所以改 `pv` 的稀疏程度
就自动改变了摩擦——不需要另外估。这里只是把每一步的拖累累加出来单独报告
（`drag`），方便看「这段收益是用多少摩擦换的」。

用法：python _exp_rebal.py
"""
import math
import os
import sys
import datetime as _dt

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
ALL11 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "NEAR"]
SWAP_COST = 7e-4
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


print(f"加载 {len(ALL11)} 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL11}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL11}
print("加载完成\n", flush=True)


# ============================================================
# 1) 把理论仓位拆成「底仓」和「加仓层」两条腿
# ============================================================
# 这一步必须拆开。原来 pv 是一坨：
#     UP    → (b + ADD·sig)·coef
#     RANGE → b·coef
#     DOWN  → ADD·sig·coef
# 拆的规则严格照上式反推：
#     base = b·coef        （UP/RANGE 有，DOWN 为 0）
#     add  = ADD·sig·coef  （UP/DOWN 有，RANGE 为 0）
# 加仓层是 4H 突破触发、本来逐根就会变，不属于「一段一次」的问题；
# 要压稀疏的只有底仓。所以阈值只作用在 base 上，add 保持理论值。
TH = {}
for c in ALL11:
    b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
    b = BT.base_series(b4, DATA[c]["d1"], rg4)
    bt_, at_ = [], []
    for k in range(len(b4)):
        coef = COEF[c][k] * VT.KAPPA
        rg = rg4[k]
        if rg == "UP":
            bt_.append(b[k] * coef)
            at_.append(BT.ADD * sig[k] * coef)
        elif rg == "RANGE":
            bt_.append(b[k] * coef)
            at_.append(0.0)
        elif rg == "DOWN":
            bt_.append(0.0)
            at_.append(BT.ADD * sig[k] * coef)
        else:
            bt_.append(0.0)
            at_.append(0.0)
    TH[c] = (bt_, at_)


def base_actual(base_t, rg4, thr):
    """把「理论底仓」压成「实际底仓」。

    thr = None  → 一段一次（只在段首建到位）
    thr = 0.0   → 逐 bar 重算（等价于回测口径）
    其余         → 段首建到位，段内相对偏差 > thr 才补
    """
    if thr == 0.0:
        return list(base_t)
    out, cur, prev = [], None, None
    for k in range(len(base_t)):
        want = base_t[k]
        if prev is None or rg4[k] != prev:
            cur = want                       # 段首：建/减/清底仓那一笔
        elif thr is not None:
            sc = max(abs(want), abs(cur))
            if sc > 1e-12 and abs(want - cur) / sc > thr:
                cur = want
        out.append(cur)
        prev = rg4[k]
    return out


# ============================================================
# 2) 单币净值（含资金费、成本、冲击）
# ============================================================
def coin_navs(thr):
    NAVS, meta = {}, {}
    for c in ALL11:
        b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
        bt_, at_ = TH[c]
        ba = base_actual(bt_, rg4, thr)
        pv = [ba[k] + at_[k] for k in range(len(b4))]
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        w50 = PC.weights_for(CAP).get(c, 0.09)
        nav, res = 1.0, {}
        k0 = next((k for k, x in enumerate(b4) if x["date"] >= START_NAV), len(b4))
        drag, n_reb, under = 0.0, 0, []
        for k in range(k0 + 1, len(b4)):
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pv[k] * r
            nav *= x if x > 0 else 0.0
            # 资金费只对加仓层（永续）收：底仓走现货
            sl = pv[k] - ba[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pv[k] != pv[k - 1]:
                d = abs(pv[k] - pv[k - 1])
                drag += BT.COST * d
                nav *= (1 - BT.COST * d)
                imp = 100 / 1e4 * math.sqrt(nav * CAP * max(w50, 1e-6) * d / qv) * d
                drag += imp
                nav *= (1 - imp)
            if ba[k] != ba[k - 1]:
                n_reb += 1
            if bt_[k] > 1e-9:
                under.append(ba[k] / bt_[k] - 1)
            res[b4[k]["date"]] = nav
        NAVS[c] = res
        meta[c] = dict(drag=drag, n_reb=n_reb,
                       under=(sum(under) / len(under) if under else 0.0))
    return NAVS, meta


# ============================================================
# 3) 组合层：口径 C（固定权重季度再平衡）与口径 B（动态权重年度）
# ============================================================
def port_fix(NAVS):
    """口径 C：权重恒按 50 万算、季度再平衡。返回日净值曲线。"""
    W = PC.weights_for(CAP)
    TGT = min(1.0, sum(W.values()))
    FIRST = {c: min(NAVS[c]) for c in ALL11}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL11]))
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    for d in ALLD:
        live = [c for c in ALL11 if FIRST[c] <= d]
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
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c])
                        for c in live) / v / 2
                v *= (1 - min(g, 1.0) * SWAP_COST)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((d, v))
    return out


def port_dyn(NAVS):
    """口径 B：年度再平衡 + 权重随净值重算 + 容量约束（Σw 变小 → 差额变现金）。"""
    FIRST = {c: min(NAVS[c]) for c in ALL11}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL11]))
    last, units, cash, out = {}, {}, 0.0, []
    cur_bucket = None
    for d in ALLD:
        live = [c for c in ALL11 if FIRST[c] <= d]
        if not live:
            continue
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)
        bucket = int(d[:4])
        need = (cur_bucket is None) or (bucket != cur_bucket) or (set(units) != set(live))
        if need:
            W_now = PC.weights_for(CAP * v)
            tgt = min(1.0, sum(W_now.values()))     # 必须取全量币的 Σw
            sw = sum(W_now.get(c, 0.0) for c in live)
            uw = {c: W_now.get(c, 0.0) / sw * tgt for c in live} if sw > 0 else {}
            if units:
                g = sum(abs(v * uw.get(c, 0.0) - units.get(c, 0.0) * last[c])
                        for c in live) / v / 2
                v *= (1 - min(g, 1.0) * SWAP_COST)
            cash = v * max(0.0, 1.0 - tgt)
            units = {c: v * uw.get(c, 0.0) / last[c] for c in live}
            cur_bucket = bucket
        out.append((d, v))
    return out


def stat(cv):
    d0 = _dt.date.fromisoformat(cv[0][0])
    d1 = _dt.date.fromisoformat(cv[-1][0])
    yrs = (d1 - d0).days / 365.25
    mult = cv[-1][1] / cv[0][1]
    cagr = mult ** (1 / max(yrs, 1e-9)) - 1
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return dict(mult=mult, cagr=cagr, dd=dd,
                calmar=cagr / abs(dd) if dd else 0.0, yrs=yrs)


def cal_a(cv):
    """口径 A：逐年收益 × 50 万，简单相加（本金每年提走，不复利）。"""
    tot, by_y = 0.0, {}
    for y in YEARS:
        v = [p for p in cv if p[0][:4] == str(y)]
        if len(v) < 5:
            continue
        r = v[-1][1] / v[0][1] - 1
        by_y[y] = r
        tot += CAP * r
    return tot, by_y


# ============================================================
ARMS = [
    ("bar   逐bar重算（上界）", 0.0),
    ("seg   一段一次（现状）", None),
    ("thr30 偏差>30% 才补", 0.30),
    ("thr20 偏差>20% 才补", 0.20),
    ("thr10 偏差>10% 才补", 0.10),
    ("thr05 偏差>5%  才补", 0.05),
]

print("=" * 118)
print("【1】五臂对照（口径 A 主看收益，口径 B 是对外口径，口径 C 只借 Calmar）")
print("=" * 118)
print(pad("臂", 24) + pad("口径A累计", 12) + pad("B倍数", 9) + pad("B年化", 9)
      + pad("B回撤", 9) + pad("B_Calmar", 10) + pad("C回撤", 9) + pad("C_Calmar", 10)
      + pad("摩擦/年", 9) + pad("欠配", 8) + pad("调仓次数", 9))
print("-" * 118)
RES = {}
for nm, thr in ARMS:
    NAVS, meta = coin_navs(thr)
    cvC = port_fix(NAVS)
    cvB = port_dyn(NAVS)
    a_tot, _by = cal_a(cvC)
    sB, sC = stat(cvB), stat(cvC)
    drag = sum(m["drag"] for m in meta.values()) / len(ALL11)
    drag_yr = drag / sC["yrs"]
    under = sum(m["under"] for m in meta.values()) / len(ALL11)
    nreb = sum(m["n_reb"] for m in meta.values()) / len(ALL11)
    RES[nm] = dict(a=a_tot, b=sB, c=sC, drag=drag_yr, under=under,
                   nreb=nreb, navs=NAVS, by=_by, cvC=cvC)
    print(pad(nm, 24) + pad("%+.0f 万" % (a_tot / 1e4), 12)
          + pad("%.2f×" % sB["mult"], 9)
          + pad("%+.1f%%" % (sB["cagr"] * 100), 9)
          + pad("%.1f%%" % (sB["dd"] * 100), 9)
          + pad("%.2f" % sB["calmar"], 10)
          + pad("%.1f%%" % (sC["dd"] * 100), 9)
          + pad("%.2f" % sC["calmar"], 10)
          + pad("%.2f%%" % (drag_yr * 100), 9)
          + pad("%+.1f%%" % (under * 100), 8)
          + pad("%.0f" % nreb, 9))

print("\n  注：「摩擦/年」是单币平均年化拖累（换手费 + 冲击，底仓与加仓层合并计）。")
print("      「欠配」是段内实际底仓相对理论底仓的平均偏离（负=常年少持）。")
print("      「调仓次数」是单币全期底仓变动的根数（逐 bar 重算时几乎每根都变）。")

# ---- 自检 ----
print("\n  自检（bar 臂必须复现现有口径，否则下面的臂不能信）：")
rb = RES["bar   逐bar重算（上界）"]
print(f"    bar 臂 口径 A = {rb['a'] / 1e4:+.0f} 万（STRATEGY.md 记 +278 万）")
print(f"    bar 臂 口径 B = {rb['b']['mult']:.2f}×（记 10.34×）")
_okA = abs(rb["a"] / 1e4 - 278) < 40
_okB = abs(rb["b"]["mult"] - 10.34) / 10.34 < 0.15
print(f"    → {'✓ 复现，可以往下看' if (_okA and _okB) else '✗ 偏离，先查拆分是否等价'}")

# ============================================================
print("\n" + "=" * 118)
print("【2】相对 seg（实盘现状）的边际：补仓到底换来了什么")
print("=" * 118)
base_seg = RES["seg   一段一次（现状）"]
print(pad("臂", 24) + pad("Δ口径A", 11) + pad("ΔB倍数", 11) + pad("ΔB年化", 10)
      + pad("ΔB回撤", 10) + pad("ΔCalmar", 10) + pad("Δ摩擦/年", 11) + "结论")
print("-" * 118)
for nm, thr in ARMS:
    if nm.startswith("seg"):
        continue
    r = RES[nm]
    da = r["a"] - base_seg["a"]
    dm = r["b"]["mult"] - base_seg["b"]["mult"]
    dc = r["b"]["cagr"] - base_seg["b"]["cagr"]
    dd = r["b"]["dd"] - base_seg["b"]["dd"]
    dk = r["b"]["calmar"] - base_seg["b"]["calmar"]
    df = r["drag"] - base_seg["drag"]
    note = ""
    if nm.startswith("bar"):
        note = "不可实现（每根都要下单）"
    elif dk > 0.02 and da > 0:
        note = "更优"
    elif dk < -0.02:
        note = "更差"
    else:
        note = "打平"
    print(pad(nm, 24) + pad("%+.0f 万" % (da / 1e4), 11)
          + pad("%+.2f×" % dm, 11) + pad("%+.1fpp" % (dc * 100), 10)
          + pad("%+.1fpp" % (dd * 100), 10) + pad("%+.2f" % dk, 10)
          + pad("%+.2fpp/年" % (df * 100), 11) + note)

# ============================================================
print("\n" + "=" * 118)
print("【3】逐年（口径 A）—— 看是不是单年事件驱动")
print("=" * 118)
hdr = pad("年", 8)
for nm, _ in ARMS:
    hdr += pad(nm.split()[0], 10)
print(hdr)
print("-" * 118)
for y in YEARS:
    line = pad(y, 8)
    for nm, _ in ARMS:
        r = RES[nm]["by"].get(y)
        line += pad("—" if r is None else "%+.1f%%" % (r * 100), 10)
    print(line)
line = pad("合计", 8)
for nm, _ in ARMS:
    line += pad("%+.0f万" % (RES[nm]["a"] / 1e4), 10)
print("-" * 118)
print(line)

# ============================================================
print("\n" + "=" * 118)
print("【4】稳健性：2022 起独立窗口（全期指标被 2022 决定过一次，别再犯）")
print("=" * 118)
print(pad("臂", 24) + pad("2022起_C倍数", 14) + pad("C年化", 10) + pad("C回撤", 10)
      + pad("C_Calmar", 11))
print("-" * 118)
for nm, _ in ARMS:
    cv = [p for p in RES[nm]["cvC"] if p[0] >= "2022-01-01"]
    s = stat(cv)
    print(pad(nm, 24) + pad("%.2f×" % s["mult"], 14)
          + pad("%+.1f%%" % (s["cagr"] * 100), 10)
          + pad("%.1f%%" % (s["dd"] * 100), 10) + pad("%.2f" % s["calmar"], 11))
print("\n  注：这一节用口径 C 曲线切窗口（只比风险效率，绝对值不可实现）。")
