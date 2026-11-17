#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_vt_softcap.py —— 软约束变体：「只减不加」

完整波动目标化（s ⊂ [0.30, 2.00]）会**放大**低波动币（BTC 系数中位 1.04，
偶尔到 2.0），这需要额外杠杆，也会把资金推向 BTC——用户不一定接受。

软约束（soft cap）：
    s_c = clip(TARGET / σ_c, LO, 1.0)      ← 上限硬性锁在 1.0
只在高波动时缩仓，永不放大。平均敞口必然低于等资金（少下注），
所以做两种口径：
  (a) κ = 1 —— 真·不加杠杆，看「少下注」本身换来什么
  (b) κ 对齐 —— 与等资金同平均敞口，剔除「仓位更小」这个混杂因素

口径：季度再平衡，50 万含成本。
"""
import contextlib
import io
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, RAW = E.COINS, E.W, E.RAW
G0 = 0.686          # 等资金平均总敞口（已核实）


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def build(lb, hi=2.00, lo=0.30, target=0.60, kappa=None):
    E.LO, E.HI, E.TARGET = lo, hi, target
    pos = {c: E.apply_vt(RAW[c][0], RAW[c][2], lb, 7) for c in COINS}
    g = sum(W[c] * sum(abs(p) for p in pos[c]) / len(pos[c]) for c in COINS)
    k = 1.0 if kappa is None else kappa
    return {c: [p * k for p in pos[c]] for c in COINS}, g


def gross(P):
    return sum(W[c] * sum(abs(p) for p in P[c]) / len(P[c]) for c in COINS)


def run(P, d0=None, d1=None):
    nv = {}
    for c in COINS:
        b4, base, _p = RAW[c]
        nv[c] = E.nav_daily(b4, base, P[c], E.idx_of(b4, d0 or "2020-09-01"), c)
    dates = sorted(set().union(*[set(n) for n in nv.values()]))
    if d0:
        dates = [d for d in dates if d >= d0]
    if d1:
        dates = [d for d in dates if d < d1]
    if not dates:
        return None
    start = dates[0]
    last, units = {}, {}
    for c in COINS:
        ks = sorted(nv[c])
        prev = [d for d in ks if d <= start]
        last[c] = nv[c][prev[-1]] if prev else 1.0
        units[c] = W[c] / last[c]
    cur_q, out = None, []
    for d in dates:
        for c in COINS:
            if d in nv[c]:
                last[c] = nv[c][d]
        v = sum(units[c] * last[c] for c in COINS)
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if cur_q is not None and q != cur_q:
            gr = sum(abs(v * W[c] - units[c] * last[c]) for c in COINS) / v / 2
            v *= (1 - gr * 7e-4)
            for c in COINS:
                units[c] = v * W[c] / last[c]
        cur_q = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd


BASE = {c: E.POS["等资金"][c] for c in COINS}
ARMS = [("等资金", BASE)]
ARMS.append(("VT60 全幅 κ1", build(60, 2.00)[0]))
ARMS.append(("VT120 全幅 κ1", build(120, 2.00)[0]))
ARMS.append(("SOFT60 只减不加 κ1", build(60, 1.00)[0]))
ARMS.append(("SOFT120 只减不加 κ1", build(120, 1.00)[0]))
ARMS.append(("SOFT60 κ对齐", build(60, 1.00, kappa=G0 / gross(build(60, 1.00)[0]))[0]))
ARMS.append(("SOFT120 κ对齐", build(120, 1.00, kappa=G0 / gross(build(120, 1.00)[0]))[0]))

print("【1】主表（季度再平衡，50 万，含成本）")
print(pad("臂", 24) + pad("平均敞口", 10) + pad("全样本", 11) + pad("最大回撤", 11) +
      pad("Calmar", 9) + pad("2024", 10) + pad("2025", 10) + pad("2026YTD", 10))
FULL = {}
for name, P in ARMS:
    r, dd = run(P)
    FULL[name] = P
    line = pad(name, 24) + pad(f"{gross(P):.3f}×", 10) + pad(f"{1 + r:.1f}×", 11) + \
        pad(f"{dd:.1%}", 11) + pad(f"{r / abs(dd):.1f}", 9)
    for y in (2024, 2025, 2026):
        rr = run(P, f"{y}-01-01", f"{y + 1}-01-01")
        line += pad(f"{rr[0]:+.1%}" if rr else "—", 10)
    print(line)

print("\n【2】滚动 365 天窗口（对比等资金，每 90 天起一窗）")
print(pad("臂", 24) + pad("胜", 6) + pad("负", 6) + pad("中位差", 10) + pad("均值差", 10))
for name, P in ARMS[1:]:
    diffs, t = [], date(2021, 1, 1)
    while True:
        t1 = t.replace(year=t.year + 1)
        if t1 > date(2026, 6, 1):
            break
        a = run(BASE, t.isoformat(), t1.isoformat())[0]
        b = run(P, t.isoformat(), t1.isoformat())[0]
        diffs.append((b - a) * 100)
        t = t.replace(year=t.year + 1)
    ds = sorted(diffs)
    print(pad(name, 24) + pad(sum(1 for x in diffs if x > 0.2), 6) +
          pad(sum(1 for x in diffs if x < -0.2), 6) +
          pad(f"{ds[len(ds) // 2]:+.1f}pp", 10) +
          pad(f"{sum(diffs) / len(diffs):+.1f}pp", 10))

print("\n【3】SOFT120 κ对齐：分币平均仓位变化")
P = FULL["SOFT120 κ对齐"]
print(pad("币", 6) + pad("等资金", 10) + pad("SOFT120", 10) + pad("变化", 9) +
      pad("σ中位", 8) + pad("平均系数", 10))
for c in COINS:
    b4 = RAW[c][0]
    vm = sorted(E.daily_vol(b4, 120).values())
    sig = vm[len(vm) // 2] if vm else float("nan")
    a = sum(abs(x) for x in BASE[c]) / len(BASE[c])
    b = sum(abs(x) for x in P[c]) / len(P[c])
    print(pad(c, 6) + pad(f"{a:.2f}", 10) + pad(f"{b:.2f}", 10) +
          pad(f"{(b / a - 1) * 100:+.0f}%", 9) + pad(f"{sig:.0%}", 8) +
          pad(f"{b / a:.2f}", 10))
