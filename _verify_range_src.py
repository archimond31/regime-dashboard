#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_range_src.py —— RANGE 来路折扣上线前的四项核验

2026-11-19 起 portfolio_config.RANGE_RATIO_BY_SRC = {"UP": 1.0, "DOWN": 0.0}。

【1】range_ratio_series() 的语义：RANGE 段内部沿用「进入时的来路」，不逐根重看
【2】base_series() 与 build() 两条入口逐 bar 一致（这是最容易分叉的地方）
【3】生产代码算出的组合净值 == 实验脚本的 291 万（口径必须完全对上）
【4】实盘链路（analyze 里 k_seg 回找来路）与回测逐 bar 一致
【5】无前视：把 rg4 整体右移一格（模拟「晚一根才知道」），差异应在噪声级

任何一项不过，就不该上线。
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

FAIL = []

# ============================================================
# 【1】range_ratio_series 语义
# ============================================================
print("=" * 96)
print("【1】range_ratio_series() 的段内一致性")
print("=" * 96)
bad = 0
for c in ALL12:
    rg4 = DATA[c]["rg4"]
    rr = BT.range_ratio_series(rg4)
    # 逐段检查：同一 RANGE 段内，rr 应当只有一个值（沿用进入时的来路）
    i = 0
    while i < len(rg4):
        if rg4[i] != "RANGE":
            i += 1
            continue
        j = i
        while j < len(rg4) and rg4[j] == "RANGE":
            j += 1
        seg = set(rr[i:j])
        if len(seg) > 1:
            bad += 1
            print(f"  ✗ {c} RANGE 段 [{i},{j}) 内出现多个系数 {seg}")
        i = j
print(f"  段内系数唯一性：{'✓ 全部通过' if bad == 0 else f'✗ {bad} 段不一致'}")
if bad:
    FAIL.append("[1] 段内系数不唯一")

# 抽查：UP→RANGE 应为 1.0，DOWN→RANGE 应为 0.0
n_up = n_dn = 0
up_ok = dn_ok = 0
for c in ALL12:
    rg4 = DATA[c]["rg4"]
    rr = BT.range_ratio_series(rg4)
    i = 1
    while i < len(rg4):
        if rg4[i] != "RANGE":
            i += 1
            continue
        j = i
        while j < len(rg4) and rg4[j] == "RANGE":
            j += 1
        prev = rg4[i - 1]
        if prev == "UP":
            n_up += 1
            if rr[i] == PC.RANGE_RATIO_BY_SRC["UP"]:
                up_ok += 1
        elif prev == "DOWN":
            n_dn += 1
            if rr[i] == PC.RANGE_RATIO_BY_SRC["DOWN"]:
                dn_ok += 1
        i = j
print(f"  UP→RANGE   {up_ok}/{n_up} 段系数 = {PC.RANGE_RATIO_BY_SRC['UP']}")
print(f"  DOWN→RANGE {dn_ok}/{n_dn} 段系数 = {PC.RANGE_RATIO_BY_SRC['DOWN']}")
if up_ok != n_up or dn_ok != n_dn:
    FAIL.append("[1] 来路映射不符")

# 【加固】当前配置 UP/DOWN 都是 1.0，上面两项检查会退化成恒真，测不出
# 「RANGE 段第二根起前一根已是 RANGE，来路丢失」这类 bug。这里临时把两个系数
# 设成不同的值再跑一遍，验证「段内沿用进入时来路」的逻辑本身仍然正确。
# 一旦将来把 DOWN 调回 0.0，这段就是真正的保护网。
print("\n  【加固】临时配置 UP=1.0 / DOWN=0.25 重跑，验证段内延续逻辑")
_save = dict(PC.RANGE_RATIO_BY_SRC)
PC.RANGE_RATIO_BY_SRC.clear()
PC.RANGE_RATIO_BY_SRC.update({"UP": 1.0, "DOWN": 0.25})
hard_bad = 0
try:
    for c in ALL12:
        rg4 = DATA[c]["rg4"]
        rr = BT.range_ratio_series(rg4)
        i = 1
        while i < len(rg4):
            if rg4[i] != "RANGE":
                i += 1
                continue
            j = i
            while j < len(rg4) and rg4[j] == "RANGE":
                j += 1
            want = {"UP": 1.0, "DOWN": 0.25}.get(rg4[i - 1])
            if want is not None and set(rr[i:j]) != {want}:
                hard_bad += 1
                print(f"    ✗ {c} 来路{rg4[i - 1]} 段 [{i},{j}) 得到 {set(rr[i:j])}，"
                      f"应为 {{{want}}}")
            i = j
finally:
    PC.RANGE_RATIO_BY_SRC.clear()
    PC.RANGE_RATIO_BY_SRC.update(_save)
print(f"  加固检查：{'✓ 通过（段内沿用进入时来路，不漂移）' if hard_bad == 0 else f'✗ {hard_bad} 段失败'}")
if hard_bad:
    FAIL.append("[1] 段内延续逻辑失效（临时配置下暴露）")

# ============================================================
# 【2】base_series 与 build 一致
# ============================================================
print("\n" + "=" * 96)
print("【2】base_series() 与 build() 内部 base 序列逐 bar 对比")
print("=" * 96)
diff_total = 0
for c in ALL12:
    D = DATA[c]
    bs = BT.base_series(D["b4"], D["d1"], D["rg4"])
    rr = BT.range_ratio_series(D["rg4"])
    ref = [BT.BASE.get(D["rg4"][k], 0.0) * rr[k] for k in range(len(D["rg4"]))]
    d = sum(1 for k in range(len(bs)) if abs(bs[k] - ref[k]) > 1e-12)
    diff_total += d
    if d:
        print(f"  ✗ {c}: {d} 根不一致")
print(f"  合计分歧：{diff_total} 根 {'✓' if diff_total == 0 else '✗'}")
if diff_total:
    FAIL.append("[2] base_series 与 build 分叉")

# ============================================================
# 【3】组合净值复现 291 万
# ============================================================


def run(bseq_of):
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = bseq_of(c)
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
                dd = abs(pv[k] - pv[k - 1])
                nav *= (1 - BT.COST * dd)
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * dd / qv) * dd)
            res[b4[k]["date"]] = nav
        NAVS[c] = res
    FIRST = {c: min(NAVS[c]) for c in ALL12}
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    for dt_ in ALLD:
        live = [c for c in ALL12 if FIRST[c] <= dt_]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if dt_ in NAVS[c]:
                last[c] = NAVS[c][dt_]
        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)
        q = (int(dt_[:4]), (int(dt_[5:7]) - 1) // 3)
        if (cq is None) or (q != cq) or (set(units) != set(live)):
            wsum = sum(uw.values())
            if units:
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c])
                        for c in live) / v / 2
                v *= (1 - min(g, 1.0) * 7e-4)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((dt_, v))
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
    return yr, cum, dd


print("\n" + "=" * 96)
print("【3】用生产代码的 base_series() 重算组合净值")
print("=" * 96)
cv_new = run(lambda c: BT.base_series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]))
yr, cum, dd = stat(cv_new)
print("  年份  " + "".join(f"{y:>9}" for y in YEARS))
print("  " + "-" * 78)
print("  收益% " + "".join(f"{yr.get(y, 0) * 100:>+9.0f}" for y in YEARS))
# 两种口径分开报，别混：cum 是口径 A（每年提走、收益率简单相加），
# cagr 是复利年化（净值不提走）—— Calmar 必须用后者，用 cum/CAP/年数 会被严重高估。
yrs = len(cv_new) / 252.0
cagr = (cv_new[-1][1] / cv_new[0][1]) ** (1 / yrs) - 1
print(f"\n  累计 {cum / 1e4:.0f} 万（口径 A）   回撤 {dd * 100:.1f}%   "
      f"复利年化 {cagr * 100:+.1f}%   Calmar {cagr / abs(dd):.2f}")
EXPECT_CUM = 291e4       # 2026-11-20 折中档 DOWN=0.5 → 291 万（DN1.0=318 万 / DN0.0=261 万）
ok3 = abs(cum - EXPECT_CUM) / EXPECT_CUM < 0.02
print(f"  期望 291 万 ±2% → {'✓ 复现' if ok3 else '✗ 不一致'}")
if not ok3:
    FAIL.append("[3] 净值与实验不一致")

# ============================================================
# 【4】实盘链路逐 bar 一致
# ============================================================
print("\n" + "=" * 96)
print("【4】实盘 analyze() 的来路推导 vs 回测 range_ratio_series()")
print("=" * 96)


def live_src_at(rg4, k):
    """复刻 live_signals.analyze() 里的 k_seg 回找逻辑，在第 k 根上求来路。"""
    state = rg4[k]
    if state != "RANGE":
        return None
    ks = k
    while ks > 0 and rg4[ks - 1] == state:
        ks -= 1
    prev = rg4[ks - 1] if ks > 0 else None
    return prev if prev in ("UP", "DOWN") else None


diff4 = 0
for c in ALL12:
    rg4 = DATA[c]["rg4"]
    rr = BT.range_ratio_series(rg4)
    for k in range(len(rg4)):
        if rg4[k] != "RANGE":
            continue
        want = PC.RANGE_RATIO_BY_SRC.get(live_src_at(rg4, k), PC.RANGE_RATIO_DEFAULT)
        if abs(rr[k] - want) > 1e-12:
            diff4 += 1
print(f"  逐 bar 分歧：{diff4} 根 {'✓' if diff4 == 0 else '✗'}")
if diff4:
    FAIL.append("[4] 实盘链路与回测不一致")

# ============================================================
# 【5】前视检查
# ============================================================
print("\n" + "=" * 96)
print("【5】前视检查：rg4 整体右移一格（晚一根才知道状态）")
print("=" * 96)
sh = {}
for c in ALL12:
    D = DATA[c]
    rg4 = D["rg4"]
    rs = [rg4[0]] + rg4[:-1]
    rr = BT.range_ratio_series(rs)
    sh[c] = [BT.BASE.get(rg4[k], 0.0) * rr[k] for k in range(len(rg4))]
cv_sh = run(lambda c: sh[c])
yr2, cum2, dd2 = stat(cv_sh)
print(f"  原样 {cum / 1e4:.0f} 万 / 回撤 {dd * 100:.1f}%"
      f"　右移1格 {cum2 / 1e4:.0f} 万 / 回撤 {dd2 * 100:.1f}%"
      f"　差 {(cum - cum2) / 1e4:+.0f} 万")
if (cum - cum2) / 1e4 > 30:
    FAIL.append("[5] 疑似前视")
    print("  ✗ 差异超过 30 万，疑似前视")
else:
    print("  ✓ 差异在噪声级（来路只用 rg4[k-1]，理论上本就无前视）")

# ============================================================
print("\n" + "=" * 96)
if FAIL:
    print("核验未通过：")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1)
print("全部通过。RANGE 底仓系数（UP=%.2f / DOWN=%.2f）与回测、实盘三方口径一致。"
      % (PC.RANGE_RATIO_BY_SRC["UP"], PC.RANGE_RATIO_BY_SRC["DOWN"]))
print("=" * 96)
