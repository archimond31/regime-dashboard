#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_vt_why.py —— VT60周 在 2023 之后为什么跑赢？拆到「是谁给的、什么机制给的」

四个候选解释，必须用数据分出来：
  H1 再分配：VT 把仓位从高波动币挪向低波动币，而 2023 之后高波动币恰好是输家
  H2 水平：VT 只是整体仓位更小（少下注），牛市里少赚、熊市里少亏
  H3 择时：系数变化与未来收益负相关（高波动时缩仓，恰好在跌之前）
  H4 平滑度：VT60周 的 σ 估计比 VT20周 稳、比 VT60月 灵敏 —— 纯参数效应

分解口径：sleeve 收益 ≈ Σ_t pos_t · r_t（4H 复利可近似为加总，误差 <0.1%）
某币对组合的贡献 = W_c × sleeve 年收益
仓位差 diff_t = pos_t·(s_t·κ − 1)·r_t，再拆成
    水平 ā·Σ pos_t r_t      （ā = 平均缩放偏离）
    择时 Σ pos_t r_t (a_t−ā)  （缩放偏离与收益的协方差）
"""
import contextlib
import io
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, RAW, POS = E.COINS, E.W, E.RAW, E.POS
YEARS = [2023, 2024, 2025, 2026]
ARMS = ["等资金", "VT20周", "VT60周", "VT60月"]


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def nav_full(arm, c):
    b4, base, _p = RAW[c]
    return E.nav_daily(b4, base, POS[arm][c], E.idx_of(b4, "2020-09-01"), c)


NAV = {arm: {c: nav_full(arm, c) for c in COINS} for arm in ARMS}


def nav_at(c, arm, d, side="before"):
    nv = NAV[arm][c]
    ks = sorted(nv)
    if side == "before":
        prev = [x for x in ks if x <= d]
        return nv[prev[-1]] if prev else None
    nxt = [x for x in ks if x >= d]
    return nv[nxt[0]] if nxt else None


def year_ret(arm, c, y):
    a = nav_at(c, arm, f"{y}-01-01")
    b = nav_at(c, arm, f"{y + 1}-01-01")
    if a is None or b is None:
        return None
    return b / a - 1


# ---------- 1. 逐年分币贡献（加总近似） ----------
print("【1】2023-2026 累计：各币对组合的贡献（W_c × sleeve 累计收益，pp）")
print(pad("币", 6) + pad("等资金", 10) + pad("VT60周", 10) + pad("差", 9) +
      pad("VT20周", 10) + pad("VT60月", 10) + pad("σ中位", 9))
TOT = {a: 0.0 for a in ARMS}
ROWS = []
for c in COINS:
    vals = {}
    for arm in ARMS:
        prod = 1.0
        ok = True
        for y in YEARS:
            r = year_ret(arm, c, y)
            if r is None:
                ok = False
                break
            prod *= 1 + r
        vals[arm] = (prod - 1) * W[c] * 100 if ok else None
    if vals["等资金"] is None:
        continue
    for arm in ARMS:
        TOT[arm] += vals[arm]
    vm = sorted(E.daily_vol(RAW[c][0], 60).values())
    sig = vm[len(vm) // 2] if vm else float("nan")
    ROWS.append((vals["VT60周"] - vals["等资金"], c, vals, sig))
for d, c, vals, sig in sorted(ROWS, reverse=True):
    print(pad(c, 6) + pad(f"{vals['等资金']:+.1f}", 10) + pad(f"{vals['VT60周']:+.1f}", 10) +
          pad(f"{d:+.1f}", 9) + pad(f"{vals['VT20周']:+.1f}", 10) +
          pad(f"{vals['VT60月']:+.1f}", 10) + pad(f"{sig:.0%}", 9))
print(pad("合计", 6) + pad(f"{TOT['等资金']:+.1f}", 10) + pad(f"{TOT['VT60周']:+.1f}", 10) +
      pad("", 9) + pad(f"{TOT['VT20周']:+.1f}", 10) + pad(f"{TOT['VT60月']:+.1f}", 10))
print("  注意：逐币贡献是加总近似，不含复利交叉项，所以合计 ≠ 实际组合收益；")
print("        用途只是排序（谁给的差额多），不要当收益率读。")

# ---------- 2. 水平 vs 择时 ----------
print("\n【2】VT60周 相对等资金：水平效应（整体仓位）vs 择时效应（缩放与收益的协方差）")


def decomp(c, arm, y):
    b4 = RAW[c][0]
    k0 = E.idx_of(b4, f"{y}-01-01")
    k1 = E.idx_of(b4, f"{y + 1}-01-01")
    if k1 - k0 < 2:            # HYPE 2024-12 才上线，2023 没有 bar
        return None
    base_pos = POS["等资金"][c]
    vt_pos = POS[arm][c]
    a = []          # 缩放偏离 a_t = s_t·κ − 1 = vt/eq − 1
    pr = []         # pos·r
    for k in range(k0 + 1, k1):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        pr.append(base_pos[k] * r)
        a.append((vt_pos[k] / base_pos[k] - 1) if base_pos[k] else 0.0)
    abar = sum(a) / len(a)
    lvl = sum(abar * x for x in pr)
    tim = sum((a[i] - abar) * pr[i] for i in range(len(pr)))
    return lvl * W[c] * 100, tim * W[c] * 100, abar


print(pad("年", 7) + pad("水平效应", 12) + pad("择时效应", 12) + pad("合计", 10) +
      pad("平均缩放ā", 11))
for y in YEARS:
    L = T = 0.0
    ab = []
    for c in COINS:
        d = decomp(c, "VT60周", y)
        if d is None:
            continue
        l, t, a = d
        L += l
        T += t
        ab.append(a)
    print(pad(y, 7) + pad(f"{L:+.1f}pp", 12) + pad(f"{T:+.1f}pp", 12) +
          pad(f"{L + T:+.1f}pp", 10) + pad(f"{sum(ab) / len(ab):+.2f}", 11))
print("  水平效应 = 平均缩放偏离 × 等资金 sleeve 收益（纯粹是「仓位大小」）")
print("  择时效应 = 缩放偏离与当期收益的协方差（「高波动时缩仓有没有躲过下跌」）")

# ---------- 3. 波动排名 vs 收益排名 ----------
print("\n【3】高波动币是不是恰好是输家（2023 起，等资金 sleeve 累计）")
print(pad("币", 6) + pad("σ(60日)", 10) + pad("σ排名", 8) + pad("累计收益", 12) +
      pad("收益排名", 10))
rows = []
for c in COINS:
    prod = 1.0
    ok = True
    for y in YEARS:
        r = year_ret("等资金", c, y)
        if r is None:
            ok = False
            break
        prod *= 1 + r
    if not ok:
        continue
    vm = sorted(E.daily_vol(RAW[c][0], 60).values())
    rows.append((vm[len(vm) // 2] if vm else 0, prod - 1, c))
by_sig = sorted(rows)
sr = {c: i + 1 for i, (_s, _r, c) in enumerate(by_sig)}
by_ret = sorted(rows, key=lambda x: x[1])
rr = {c: i + 1 for i, (_s, _r, c) in enumerate(by_ret)}
for sig, ret, c in by_sig:
    print(pad(c, 6) + pad(f"{sig:.0%}", 10) + pad(sr[c], 8) + pad(f"{ret:+.1%}", 12) +
          pad(rr[c], 10))
n = len(rows)
d2 = sum((sr[c] - rr[c]) ** 2 for _s, _r, c in rows)
rho = 1 - 6 * d2 / (n * (n * n - 1))
print(f"  斯皮尔曼秩相关 ρ(σ排名, 收益排名) = {rho:+.2f}"
      f"   （正=高波动确实高收益，则缩高波动会亏；负=高波动是输家）")

# ---------- 4. 系数路径：20 周 vs 60 周 vs 60 月 ----------
print("\n【4】各币平均缩放系数（已含 κ，逐年）—— 谁比谁更灵敏")
print(pad("币", 6) + "".join(pad(f"{a}·{y}", 14) for a in ("VT20", "VT60", "VT60M")
                            for y in (2023, 2026)))
for c in COINS:
    line = pad(c, 6)
    for arm in ("VT20周", "VT60周", "VT60月"):
        for y in (2023, 2026):
            k0 = E.idx_of(RAW[c][0], f"{y}-01-01")
            k1 = E.idx_of(RAW[c][0], f"{y + 1}-01-01")
            eqp, vtp = POS["等资金"][c], POS[arm][c]
            seg = [(vtp[k] / eqp[k]) for k in range(k0 + 1, k1) if eqp[k]]
            line += pad(f"{sum(seg) / len(seg):.2f}" if seg else "—", 14)
    print(line)

# ---------- 5. 反事实：BTC 不参与缩放 ----------
print("\n【5】反事实——把 BTC 从缩放里摘出来（其余照旧，κ 重算保总量）")


def build_no_btc():
    E.LO, E.HI, E.TARGET = 0.30, 2.00, 0.60
    pos = {}
    for c in COINS:
        if c == "BTC":
            pos[c] = list(RAW[c][2])                    # 不动
        else:
            pos[c] = E.apply_vt(RAW[c][0], RAW[c][2], 60, 7)
    g = sum(W[c] * sum(abs(p) for p in pos[c]) / len(pos[c]) for c in COINS)
    k = 0.686 / g
    return {c: [p * k for p in pos[c]] for c in COINS}, g, k


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


PNB, g, k = build_no_btc()
print(pad("臂", 26) + pad("2023", 10) + pad("2024", 10) + pad("2025", 10) +
      pad("2026YTD", 10) + pad("2023起累计", 12) + pad("回撤", 10))
BASE = {c: POS["等资金"][c] for c in COINS}
VT = {c: POS["VT60周"][c] for c in COINS}
for name, P in (("等资金", BASE), ("VT60周（全缩）", VT),
                (f"VT60周（BTC 不缩 κ={k:.2f}）", PNB)):
    line = pad(name, 26)
    prod = 1.0
    for y in (2023, 2024, 2025, 2026):
        rr = run(P, f"{y}-01-01", f"{y + 1}-01-01")
        line += pad(f"{rr[0]:+.1%}" if rr else "—", 10)
        if rr and y < 2026:
            prod *= 1 + rr[0]
    r26 = run(P, "2026-01-01", None)
    prod *= 1 + r26[0]
    full = run(P)
    line += pad(f"{prod:.2f}×", 12) + pad(f"{full[1]:.1%}", 10)
    print(line)
