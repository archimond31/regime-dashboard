#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_parity_vt.py —— 生产模块 vol_target.py 与实验口径的一致性校验

实验（_exp_vt_long.py）用的是「距上次刷新满 7 天就更新」的有状态规则；
生产不能用有状态规则（进程重启会漂），改成「距锚点第几个 7 天」的日历桶。
两者节奏相同但相位不同，必须确认结果没跑偏。

同时校验 KAPPA=1.22 这个硬编码常数是不是对的：
κ 应当让 VT60 的平均总敞口等于 0.686×（等资金口径）。
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

import vol_target as VT

COINS, W, RAW = E.COINS, E.W, E.RAW
T_BARS = max(len(RAW[c][0]) for c in COINS)


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def gross(P):
    return sum(W[c] * sum(abs(p) for p in P[c]) / T_BARS for c in COINS)


EQ = {c: list(RAW[c][2]) for c in COINS}
RAWVT = {}
for c in COINS:
    cs = VT.coef_series(RAW[c][0])
    RAWVT[c] = [p * k for p, k in zip(RAW[c][2], cs)]

g_eq, g_vt = gross(EQ), gross(RAWVT)
kappa_emp = g_eq / g_vt
print(f"等资金平均总敞口 {g_eq:.3f}×    VT60 原始 {g_vt:.3f}×")
print(f"经验 κ = {kappa_emp:.4f}    代码里硬编码 KAPPA = {VT.KAPPA:.4f}"
      f"    偏差 {(VT.KAPPA / kappa_emp - 1) * 100:+.1f}%")

ALIGN = {c: [p * kappa_emp for p in RAWVT[c]] for c in COINS}
PROD = {c: [p * VT.KAPPA for p in RAWVT[c]] for c in COINS}
print(f"对齐后敞口：经验κ {gross(ALIGN):.3f}×   硬编码κ {gross(PROD):.3f}×")


def portfolio(P, d0=None, d1=None):
    nv = {}
    for c in COINS:
        b4, base, _p = RAW[c]
        nv[c] = E.nav_daily(b4, base, P[c], E.idx_of(b4, "2020-09-01"), c)
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


EXP = {  # _exp_vt_long.py 的实验值（有状态刷新规则 + 经验 κ）
    "全样本": "20.1×", "2021": "+151%", "2023": "+190%", "2024": "+110%",
    "2025": "-7%", "2026": "+101%", "回撤": "-54.4%",
}

print("\n【1】生产规则 vs 实验规则")
print(pad("口径", 16) + pad("全样本", 11) + "".join(pad(y, 10) for y in
      ("2021", "2023", "2024", "2025", "2026")) + pad("回撤", 10))
r, dd = portfolio(PROD)
line = pad("生产（κ=%.2f）" % VT.KAPPA, 16) + pad(f"{1 + r:.1f}×", 11)
for y in (2021, 2023, 2024, 2025, 2026):
    rr = portfolio(PROD, f"{y}-01-01", f"{y + 1}-01-01" if y < 2026 else None)
    line += pad(f"{rr[0]:+.0%}", 10)
line += pad(f"{dd:.1%}", 10)
print(line)
print(pad("实验（对照）", 16) + pad(EXP["全样本"], 11) +
      "".join(pad(EXP[y], 10) for y in ("2021", "2023", "2024", "2025", "2026")) +
      pad(EXP["回撤"], 10))

print("\n【2】逐币系数（最新一根）与缩放后金额（5000 U 本金）")
print(pad("币", 6) + pad("σ年化", 9) + pad("系数", 8) + pad("底仓原", 10) +
      pad("底仓缩放", 11) + pad("峰值原", 10) + pad("峰值缩放", 11))
from portfolio_config import WEIGHTS, BASE_RATIO, ADD_RATIO
CAP = 5000.0
for c in COINS:
    cs = VT.coef_series(RAW[c][0])
    sm = VT.sigma_series(RAW[c][0])
    sig = sm.get(RAW[c][0][-1]["date"][:10])
    k = cs[-1]
    w = WEIGHTS[c]
    print(pad(c, 6) + pad(f"{sig:.0%}" if sig else "—", 9) + pad(f"{k:.2f}", 8) +
          pad(f"{CAP * w * BASE_RATIO:.0f}", 10) +
          pad(f"{CAP * w * BASE_RATIO * k * VT.KAPPA:.0f}", 11) +
          pad(f"{CAP * w * (BASE_RATIO + ADD_RATIO):.0f}", 10) +
          pad(f"{CAP * w * (BASE_RATIO + ADD_RATIO) * k * VT.KAPPA:.0f}", 11))

# ---------- 3. 刷新相位敏感性 ----------
print("\n【3】刷新相位敏感性（把周锚点平移 0~6 天，看结果抖多大）")
print(pad("相位偏移", 11) + pad("κ", 8) + pad("全样本", 11) + pad("2023", 9) +
      pad("2026", 9) + pad("回撤", 10))
import datetime as _dt
res = []
for off in range(7):
    VT.ANCHOR = _dt.date(2020, 1, 6) + _dt.timedelta(days=off)
    RV = {}
    for c in COINS:
        cs = VT.coef_series(RAW[c][0])
        RV[c] = [p * k for p, k in zip(RAW[c][2], cs)]
    kk = gross(EQ) / gross(RV)
    P2 = {c: [p * kk for p in RV[c]] for c in COINS}
    r2, dd2 = portfolio(P2)
    r23 = portfolio(P2, "2023-01-01", "2024-01-01")[0]
    r26 = portfolio(P2, "2026-01-01", None)[0]
    res.append((1 + r2, dd2))
    print(pad(f"+{off} 天", 11) + pad(f"{kk:.3f}", 8) + pad(f"{1 + r2:.1f}×", 11) +
          pad(f"{r23:+.0%}", 9) + pad(f"{r26:+.0%}", 9) + pad(f"{dd2:.1%}", 10))
xs = [x for x, _ in res]
print(f"  中位 {sorted(xs)[3]:.1f}×   最差 {min(xs):.1f}×   最好 {max(xs):.1f}×"
      f"   极差 {(max(xs) / min(xs) - 1) * 100:.0f}%")
print(f"  作为对照：等资金 17.6×，实验口径 VT60 20.1×")
