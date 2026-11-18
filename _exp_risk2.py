#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_risk2.py —— P0-3：风险预算换指标

== 为什么换 ==
`_diag_risk.py` 已证：波动率尺子量不到真实风险。
  2022 年组合波动 43% → 最大回撤 -55.7%
  2023 年组合波动 50% → 最大回撤 -18.7%
波动更高的一年，回撤只有三分之一。所以「按 σ 定仓位」（VT60）只对了一半：
它管住了平均波动，没管住「一路阴跌」。真正该当尺子的是**已发生的损失**。

== 两条候选尺子（用户提法）==
  R1 底仓相对**入场价**回撤超 k×ATR → 减半（固定止损，可恢复）
  R2 底仓相对**峰值**回撤超 k×ATR → 减半（吊灯止损，可恢复）
  R3 **状态挂满 N 天** → 底仓减半（老状态折价，纯机械无未来函数）
  R4/R5 = R1/R3、R2/R3 叠加

R1 与 R2 的区别不是学术细节：R1 的止损位在建仓那一刻就固定了，顺着趋势走高也不跟着抬
（盈利单可能被一根回抽打掉）；R2 抬止损，但均线附近更容易被扫。两个都要测。

== 与已上线的底仓闸门的关系 ==
闸门（MA50）和本实验的 risk_scale 是**两个独立来源**，都要求减仓时取乘积：
  base = BASE[state] × gate(MA50) × risk_scale(ATR/年龄)
所以基线就是当前生产配置（378 万），所有臂都在它之上比较。

== 判定标准（先写死，跑完不许改）==
① 累计盈亏 ≥ 基线（口径 A，7 年）
② 最大回撤不劣于基线
③ 滚动 17 窗 ≥ 12 胜
④ 剔除最佳一年后仍 ≥ 基线（防单年撑起）
四条全过才落地；否则维持现状，把结论写进注释而不是硬上线。
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
BARS_PER_DAY = 6


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
    sig, _ = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig, atr=atr)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
print("加载完成，开始算各臂 …", flush=True)

COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
BASE_G = {c: BT.base_series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}

# 状态段年龄（同状态连续 4H 根数）
AGE = {}
for c in ALL12:
    rg4 = DATA[c]["rg4"]
    a = [0] * len(rg4)
    for i in range(1, len(rg4)):
        a[i] = a[i - 1] + 1 if rg4[i] == rg4[i - 1] else 0
    AGE[c] = a


def risk_one(D, age, rule):
    """单条规则的 risk_scale 序列（1.0 / 0.5）。"""
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    n = len(b4)
    kind, par = rule
    if kind == "age":
        lim = int(par * BARS_PER_DAY)
        return [0.5 if (BT.BASE.get(rg4[i], 0.0) > 0 and age[i] >= lim) else 1.0
                for i in range(n)]
    out = [1.0] * n
    entry = peak = atr_e = None
    armed = False
    for i in range(n):
        px = b4[i]["close"]
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            entry = peak = atr_e = None
            armed = False
            continue
        if entry is None:                       # 底仓从 0 建起来的这一根 = 入场
            entry, peak = px, px
            atr_e = atr[i] or px * 0.02
            armed = False
        peak = max(peak, px)
        a_now = atr[i] or atr_e
        if kind == "fix":
            if px < entry - par * atr_e:
                armed = True
            elif armed and px > entry:
                armed = False
        elif kind == "trail":
            if px < peak - par * a_now:
                armed = True
            elif armed and px > peak - 0.5 * par * a_now:
                armed = False
        out[i] = 0.5 if armed else 1.0
    return out


def risk_series(c, rules):
    if not rules:
        return [1.0] * len(DATA[c]["b4"])
    tot = [1.0] * len(DATA[c]["b4"])
    for r in rules:
        s = risk_one(DATA[c], AGE[c], r)
        tot = [a * b for a, b in zip(tot, s)]
    return tot


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


def build(rules):
    NAVS = {}
    for c in ALL12:
        rs = risk_series(c, rules)
        base = [BASE_G[c][k] * rs[k] for k in range(len(rs))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
    return NAVS


FIRST_CACHE = {}


def curve(NAVS, start=START_NAV, end=None):
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    if not FIRST_CACHE:
        for c in ALL12:
            FIRST_CACHE[c] = min(NAVS[c])
    FIRST = FIRST_CACHE
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
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


def metrics(cv):
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
    return yr, cum, dd, cv[-1][1] / cv[0][1] - 1


ARMS = [
    ("R0 基线（只 MA50 闸门）", []),
    ("R1 入场价 -1.5ATR 减半", [("fix", 1.5)]),
    ("R2 峰值 -1.5ATR 减半", [("trail", 1.5)]),
    ("R3 状态挂满 90 天减半", [("age", 90)]),
    ("R4 R1+R3", [("fix", 1.5), ("age", 90)]),
    ("R5 R2+R3", [("trail", 1.5), ("age", 90)]),
]

print("\n" + "=" * 104)
print("【1】六臂主对照（口径 A：本金恒 50 万、每年提走）")
print("=" * 104)
print(pad("臂", 26) + "".join(pad(y, 9) for y in YEARS)
      + pad("累计盈亏", 12) + pad("倍数", 9) + pad("回撤", 9))
print("-" * 104)
RES = {}
for name, rules in ARMS:
    cv = curve(build(rules))
    yr, cum, dd, mult = metrics(cv)
    RES[name] = dict(yr=yr, cum=cum, dd=dd, mult=mult, cv=cv, rules=rules)
    print(pad(name, 26) + "".join(pct(yr.get(y, 0.0), 9) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 12) + pad("%.2f×" % mult, 9)
          + pad("%.1f%%" % (dd * 100), 9))

BASE = RES["R0 基线（只 MA50 闸门）"]

print("\n" + "=" * 104)
print("【2】相对基线：逐年差（pp）")
print("=" * 104)
print(pad("臂", 26) + "".join(pad(y, 9) for y in YEARS) + pad("总差", 10))
print("-" * 104)
for name, r in RES.items():
    if name == "R0 基线（只 MA50 闸门）":
        continue
    d = [ (r["yr"].get(y, 0) - BASE["yr"].get(y, 0)) * 100 for y in YEARS]
    print(pad(name, 26) + "".join(pad("%+.1f" % x, 9) for x in d)
          + pad("%+.1f 万" % ((r["cum"] - BASE["cum"]) / 1e4), 10))

print("\n" + "=" * 104)
print("【3】滚动 250 交易日窗（步长 125）—— 稳定性")
print("=" * 104)
DATES = [d for d, _ in BASE["cv"]]
print(pad("臂", 26) + pad("窗口", 8) + pad("改善", 8) + pad("变差", 8)
      + pad("持平", 8) + pad("中位差(pp)", 12))
print("-" * 104)


def rolling(cv, base_cv):
    """250 交易日窗（步长 125）逐窗比较。"""
    wins = loss = tie = 0
    diffs = []
    for i in range(0, len(DATES) - 250 + 1, 125):
        s, e = DATES[i], DATES[i + 250]
        cb = [x for x in base_cv if s <= x[0] < e]
        cr = [x for x in cv if s <= x[0] < e]
        if len(cb) < 200 or len(cr) < 200:
            continue
        d = ((cr[-1][1] / cr[0][1]) - (cb[-1][1] / cb[0][1])) * 100
        diffs.append(d)
        if d > 0.5:
            wins += 1
        elif d < -0.5:
            loss += 1
        else:
            tie += 1
    return wins, loss, tie, (sorted(diffs)[len(diffs) // 2] if diffs else 0.0), len(diffs)


for name, r in RES.items():
    if name == "R0 基线（只 MA50 闸门）":
        continue
    wins, loss, tie, med, nw = rolling(r["cv"], BASE["cv"])
    print(pad(name, 26) + pad(nw, 8) + pad(wins, 8) + pad(loss, 8)
          + pad(tie, 8) + pad("%+.1f" % med, 12))

print("\n" + "=" * 104)
print("【4】参数网格：ATR 倍数 × 规则类型（口径 A 累计盈亏，万）")
print("=" * 104)
KEEP = {}
for kind in ("fix", "trail"):
    print(f"\n  {kind}：" + pad("k", 8) + pad("累计", 11) + pad("vs基线", 11)
          + pad("回撤", 9) + pad("滚动胜/负", 12))
    for k in (1.0, 1.5, 2.0, 3.0):
        cv = curve(build([(kind, k)]))
        yr, cum, dd, mult = metrics(cv)
        wins, loss, _, _, _ = rolling(cv, BASE["cv"])
        KEEP[(kind, k)] = (cum, dd, wins, loss, yr, cv)
        print(f"  {'':5}" + pad(k, 8) + pad("%.0f 万" % (cum / 1e4), 11)
              + pad("%+.0f 万" % ((cum - BASE["cum"]) / 1e4), 11)
              + pad("%.1f%%" % (dd * 100), 9) + pad(f"{wins}/{loss}", 12))

print("\n  age：" + pad("天", 8) + pad("累计", 11) + pad("vs基线", 11)
      + pad("回撤", 9) + pad("滚动胜/负", 12))
for d_ in (60, 90, 120, 180):
    cv = curve(build([("age", d_)]))
    yr, cum, dd, mult = metrics(cv)
    wins, loss, _, _, _ = rolling(cv, BASE["cv"])
    KEEP[("age", d_)] = (cum, dd, wins, loss, yr, cv)
    print(f"  {'':5}" + pad(d_, 8) + pad("%.0f 万" % (cum / 1e4), 11)
          + pad("%+.0f 万" % ((cum - BASE["cum"]) / 1e4), 11)
          + pad("%.1f%%" % (dd * 100), 9) + pad(f"{wins}/{loss}", 12))

print("\n" + "=" * 104)
print("【5】判定（标准在文件头写死）")
print("=" * 104)
print("  ① 累计 ≥ 基线 %.0f 万" % (BASE["cum"] / 1e4))
print("  ② 回撤不劣于基线 %.1f%%" % (BASE["dd"] * 100))
print("  ③ 滚动 ≥ 12 胜")
print("  ④ 剔除最佳一年后仍 ≥ 基线\n")
print(pad("候选", 26) + pad("①", 5) + pad("②", 5) + pad("③", 7) + pad("④", 5)
      + pad("结论", 10))
print("-" * 104)
for (kind, k), (cum, dd, wins, loss, yr, _cv) in KEEP.items():
    ok1 = cum >= BASE["cum"]
    ok2 = dd >= BASE["dd"] - 1e-9
    ok3 = wins >= 12
    best = max(YEARS, key=lambda y: yr.get(y, -9))
    cum_wo = sum(CAP * yr.get(y, 0.0) for y in YEARS if y != best)
    base_wo = sum(CAP * BASE["yr"].get(y, 0.0) for y in YEARS if y != best)
    ok4 = cum_wo >= base_wo
    nm = f"{kind} k={k}" if kind != "age" else f"age {k} 天"
    verdict = "✓ 可落地" if (ok1 and ok2 and ok3 and ok4) else "✗ 不达标"
    print(pad(nm, 26) + pad("✓" if ok1 else "✗", 5) + pad("✓" if ok2 else "✗", 5)
          + pad("✓" if ok3 else "✗", 7) + pad("✓" if ok4 else "✗", 5)
          + pad(verdict, 10))

print("\n" + "=" * 104)
print("【6】换手：新规则会不会把低频策略变成高频")
print("=" * 104)
for name, rules in ARMS:
    n_flip = 0
    for c in ALL12:
        s = risk_series(c, rules)
        n_flip += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    print(f"  {pad(name, 26)} 规则翻转 {n_flip:>5} 次 / 6.2 年"
          f"（≈{n_flip / 6.2:.0f} 次/年）")
