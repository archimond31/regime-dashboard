#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_vt_grid.py —— 波动目标化参数网格（防止「一档参数撞出来的好结果」）

主实验（_exp_vt_rebal.py）里 VT60周 表现最好。必须回答两个问题：
  1) lb（波动回看窗口）换掉 60 还成立吗？还是只有 60 好？
  2) TARGET / LO / HI 到底影响什么？

**先想清楚一件事**：s_c = clip(TARGET / σ_c, LO, HI)，随后整臂用 κ 归一化到
同一平均敞口。κ 是全局乘数 → **TARGET 在 clip 不起作用时是纯哑变量**，
真正有影响的是 LO/HI（决定哪些币被卡住）和 lb（决定 σ 的估计）。
所以这一节的重点是 lb 的稳健性，TARGET 只需要验证「不是敏感参数」。

口径：季度再平衡（主口径），50 万含成本。
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, RAW = E.COINS, E.W, E.RAW
CAP = E.CAP


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def build(lb, rebal=7, lo=0.30, hi=2.00, target=0.60):
    """返回 κ 对齐后的 POS[c] 与 κ。"""
    E.LO, E.HI, E.TARGET = lo, hi, target
    pos = {c: (list(RAW[c][2]) if lb is None
               else E.apply_vt(RAW[c][0], RAW[c][2], lb, rebal)) for c in COINS}
    g = E.avg_gross([(c, pos[c]) for c in COINS])
    kappa = E.POS["等资金"]["__gross__"] / g
    return {c: [p * kappa for p in pos[c]] for c in COINS}, kappa, g


def run(POSC, d0=None, d1=None):
    """季度再平衡组合指标。"""
    nv = {}
    for c in COINS:
        b4, base, _p = RAW[c]
        nv[c] = E.nav_daily(b4, base, POSC[c], E.idx_of(b4, d0 or "2020-09-01"), c)
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
            gross = sum(abs(v * W[c] - units[c] * last[c]) for c in COINS) / v / 2
            v *= (1 - gross * 7e-4)
            for c in COINS:
                units[c] = v * W[c] / last[c]
        cur_q = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    fin = vals[-1] / vals[0] - 1
    return fin, dd


def yr(POSC, y0):
    r = run(POSC, f"{y0}-01-01", f"{y0 + 1}-01-01")
    return r[0] if r else None


BASE = {c: E.POS["等资金"][c] for c in COINS}
rb, ddb = run(BASE)
print(pad("基准（等资金）", 24) + pad(f"{1 + rb:.1f}×", 12) + pad(f"{ddb:.1%}", 11) +
      pad(f"{rb / abs(ddb):.1f}", 9))

print("\n【1】lb 网格（rebal=7, TARGET=0.60, LO/HI=0.30/2.00）")
print(pad("lb", 6) + pad("κ", 8) + pad("全样本", 11) + pad("最大回撤", 11) +
      pad("Calmar", 9) + pad("2021", 10) + pad("2023", 10) + pad("2024", 10) +
      pad("2025", 10) + pad("2026YTD", 10))
for lb in (10, 20, 30, 45, 60, 90, 120, 180):
    P, kappa, _g = build(lb)
    r, dd = run(P)
    line = pad(lb, 6) + pad(f"{kappa:.2f}", 8) + pad(f"{1 + r:.1f}×", 11) + \
        pad(f"{dd:.1%}", 11) + pad(f"{r / abs(dd):.1f}", 9)
    for y in (2021, 2023, 2024, 2025):
        v = yr(P, y)
        line += pad(f"{v:+.1%}", 10)
    v = yr(P, 2026)
    line += pad(f"{v:+.1%}", 10)
    print(line)

print("\n【2】LO/HI 网格（lb=60, rebal=7）")
print(pad("LO / HI", 14) + pad("κ", 8) + pad("全样本", 11) + pad("最大回撤", 11) +
      pad("Calmar", 9) + pad("触上限", 10) + pad("触下限", 10))
for lo, hi in ((0.30, 2.00), (0.50, 2.00), (0.30, 1.50), (0.50, 1.50), (0.20, 2.50)):
    P, kappa, _g = build(60, lo=lo, hi=hi)
    r, dd = run(P)
    # 触限比例
    E.LO, E.HI, E.TARGET = lo, hi, 0.60
    nh = nl = tot = 0
    for c in COINS:
        b4 = RAW[c][0]
        for v in E.daily_vol(b4, 60).values():
            s = 0.60 / v
            tot += 1
            nh += s >= hi - 1e-9
            nl += s <= lo + 1e-9
    print(pad(f"{lo:.2f} / {hi:.2f}", 14) + pad(f"{kappa:.2f}", 8) +
          pad(f"{1 + r:.1f}×", 11) + pad(f"{dd:.1%}", 11) +
          pad(f"{r / abs(dd):.1f}", 9) + pad(f"{nh / tot:.0%}", 10) +
          pad(f"{nl / tot:.0%}", 10))

print("\n【3】TARGET 哑变量验证（lb=60，看是否只有 clip 在起作用）")
print(pad("TARGET", 9) + pad("κ", 8) + pad("全样本", 11) + pad("最大回撤", 11) +
      pad("Calmar", 9) + pad("换手代理", 10))
for tg in (0.35, 0.45, 0.60, 0.80, 1.00):
    P, kappa, _g = build(60, target=tg)
    r, dd = run(P)
    # 换手代理：Σ|Δpos| / 2 / 年
    turn = sum(sum(abs(P[c][k] - P[c][k - 1]) for k in range(1, len(P[c])))
               for c in COINS) / 2 / 6.2
    print(pad(f"{tg:.2f}", 9) + pad(f"{kappa:.2f}", 8) + pad(f"{1 + r:.1f}×", 11) +
          pad(f"{dd:.1%}", 11) + pad(f"{r / abs(dd):.1f}", 9) +
          pad(f"{turn:.0f}", 10))
