#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_vt_long.py —— lb=120 / 180 的完整对照，并追问「是不是越长越好」

上一轮 lb 网格给出单调改善（60→20.1× / 120→19.9× / 180→21.6×），但 180 只多
0.7×，完全可能是 2021 一个点撑的。这一版专门验证三件事：

  A. 剔除 2021 后还单调吗？（2022 起 / 2023 起分别算）
  B. 继续往长推（240 / 365）会怎样？如果一直更好，说明价值不在「时变」，
     而在「静态的 1/σ 倾斜」——那是另一种东西，得单独测。
  C. 反事实：每个 lb 都把 BTC 摘出缩放，看单调性是否还在。

新增一臂 WALK-FWD 静态倾斜（最严格的样本外）：
  每年初用「截至上年末」的历史中位波动定系数，**全年不动**，
  再按当年平均敞口做 κ 归一。完全没有未来函数，也没有年内调仓。

口径：季度再平衡、50 万、含手续费/资金费/滑点。
"""
import contextlib
import io
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, RAW = E.COINS, E.W, E.RAW
TARGET, LO, HI = 0.60, 0.30, 2.00
YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


# ---------- 仓位构造 ----------
def vt_pos(c, lb):
    E.LO, E.HI, E.TARGET = LO, HI, TARGET
    return E.apply_vt(RAW[c][0], RAW[c][2], lb, 7)


T_BARS = max(len(RAW[c][0]) for c in COINS)


def gross_of(P):
    """时间加权平均总敞口：Σ_c W_c · Σ_t|pos| / T，T = 最长序列的 bar 数。

    不能按日期并集取平均——HYPE 的 date 只有「YYYY-MM-DD」（日粒度），
    Binance 是「YYYY-MM-DD HH:MM」（4H 粒度），并集会错位。
    也不能各币分别求均值再相加——HYPE 只有 2024-12 之后的数据，
    那样等于让它占满仓权重 6 年。
    """
    return sum(W[c] * sum(abs(p) for p in P[c]) / T_BARS for c in COINS)


def align(P):
    k = G0 / gross_of(P)
    return {c: [p * k for p in P[c]] for c in COINS}


ARMS = {"等资金": {c: list(RAW[c][2]) for c in COINS}}
G0 = gross_of(ARMS["等资金"])
for lb in (60, 120, 180, 240, 365):
    ARMS[f"VT{lb}"] = align({c: vt_pos(c, lb) for c in COINS})

# 静态（后视）：全样本中位波动定系数，仅作极限参照
STATIC = {}
for c in COINS:
    vs = sorted(E.daily_vol(RAW[c][0], 365).values())
    sig = vs[len(vs) // 2] if vs else 1.0
    STATIC[c] = min(HI, max(LO, TARGET / sig))
ARMS["STATIC后视"] = align({c: [p * STATIC[c] for p in RAW[c][2]] for c in COINS})

# walk-forward 静态倾斜：每年初用历史定系数，全年不动
WFCOEF = {}
for c in COINS:
    b4 = RAW[c][0]
    vm = E.daily_vol(b4, 180)
    for y in YEARS:
        hist = sorted(v for d, v in vm.items() if d < f"{y}-01-01")
        WFCOEF[(c, y)] = (min(HI, max(LO, TARGET / hist[len(hist) // 2]))
                          if hist else 1.0)
WFPOS = {}
for c in COINS:
    b4 = RAW[c][0]
    pos = []
    for k, b in enumerate(b4):
        y = int(b["date"][:4])
        coef = WFCOEF.get((c, y), 1.0)
        pos.append(RAW[c][2][k] * coef)
    WFPOS[c] = pos
ARMS["WF静态"] = align(WFPOS)

# ---------- 净值缓存 + 组合 ----------
NAV = {}
for name, P in ARMS.items():
    NAV[name] = {}
    for c in COINS:
        b4, base, _p = RAW[c]
        NAV[name][c] = E.nav_daily(b4, base, P[c], E.idx_of(b4, "2020-09-01"), c)


def portfolio(arm, d0=None, d1=None):
    nv = NAV[arm]
    dates = sorted(set().union(*[set(n) for n in nv.values()]))
    if d0:
        dates = [d for d in dates if d >= d0]
    if d1:
        dates = [d for d in dates if d < d1]
    if not dates:
        return []
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
            g = sum(abs(v * W[c] - units[c] * last[c]) for c in COINS) / v / 2
            v *= (1 - g * 7e-4)
            for c in COINS:
                units[c] = v * W[c] / last[c]
        cur_q = q
        out.append((d, v))
    return out


def stat(cur):
    if not cur:
        return None, None
    vals = [x[1] for x in cur]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd


def dd_win(cur):
    pk, pkd, best = -1e18, cur[0][0], (0.0, None, None)
    for d, v in cur:
        if v > pk:
            pk, pkd = v, d
        if v / pk - 1 < best[0]:
            best = (v / pk - 1, pkd, d)
    pv = next(v for d, v in cur if d == best[1])
    rec = next((d for d, v in cur if d > best[2] and v >= pv), None)
    return best[0], best[1], best[2], rec


NAMES = ["等资金", "VT60", "VT120", "VT180", "VT240", "VT365", "STATIC后视", "WF静态"]

# ---------- 1 ----------
print("【1】主表（季度再平衡，50 万，含成本）")
print(pad("臂", 11) + pad("全样本", 11) + "".join(pad(y, 9) for y in YEARS) +
      pad("回撤", 10) + pad("Calmar", 9) + pad("换手/年", 9))
FULL, TURN = {}, {}
for name in NAMES:
    cur = portfolio(name)
    r, dd = stat(cur)
    FULL[name] = (r, dd, cur)
    line = pad(name, 11) + pad(f"{1 + r:.1f}×", 11)
    for y in YEARS:
        rr, _ = stat(portfolio(name, f"{y}-01-01",
                              f"{y + 1}-01-01" if y < 2026 else None))
        line += pad(f"{rr:+.0%}", 9) if rr is not None else pad("—", 9)
    t = 0.0
    for c in COINS:
        P = ARMS[name][c]
        t += sum(abs(P[k] - P[k - 1]) for k in range(1, len(P)))
    TURN[name] = t / 2 / 6.2
    line += pad(f"{dd:.1%}", 10) + pad(f"{r / abs(dd):.1f}", 9) + pad(f"{TURN[name]:.0f}", 9)
    print(line)

# ---------- 2 ----------
print("\n【2】剔掉 2021 之后还单调吗")
print(pad("臂", 11) + pad("2022起累计", 12) + pad("年化", 9) + pad("回撤", 10) +
      pad("Calmar", 9) + pad("2023起累计", 12) + pad("年化", 9) + pad("回撤", 10))
for name in NAMES:
    line = pad(name, 11)
    for d0, yrs in (("2022-01-01", 4.9), ("2023-01-01", 3.9)):
        r, dd = stat(portfolio(name, d0))
        line += pad(f"{1 + r:.2f}×", 12) + pad(f"{(1 + r) ** (1 / yrs) - 1:+.1%}", 9) + \
            pad(f"{dd:.1%}", 10)
        if d0 == "2022-01-01":
            line += pad(f"{r / abs(dd):.1f}", 9)
    print(line)

# ---------- 3 ----------
print("\n【3】滚动 365 天窗口（每 90 天起一窗，2021 起）· 对等资金")
print(pad("臂", 11) + pad("胜", 5) + pad("负", 5) + pad("中位差", 10) + pad("均值差", 10) +
      pad("最差窗", 10) + pad("最好窗", 10))
WINS = {}
for name in NAMES[1:]:
    diffs, t = [], date(2021, 1, 1)
    while True:
        t1 = t.replace(year=t.year + 1)
        if t1 > date(2026, 6, 1):
            break
        a = stat(portfolio("等资金", t.isoformat(), t1.isoformat()))[0]
        b = stat(portfolio(name, t.isoformat(), t1.isoformat()))[0]
        diffs.append((b - a) * 100)
        t = t.replace(year=t.year + 1)
    WINS[name] = diffs
    ds = sorted(diffs)
    print(pad(name, 11) + pad(sum(1 for x in diffs if x > 0.2), 5) +
          pad(sum(1 for x in diffs if x < -0.2), 5) +
          pad(f"{ds[len(ds) // 2]:+.1f}pp", 10) +
          pad(f"{sum(diffs) / len(diffs):+.1f}pp", 10) +
          pad(f"{ds[0]:+.1f}pp", 10) + pad(f"{ds[-1]:+.1f}pp", 10))

# ---------- 4 ----------
print("\n【4】最长回撤区间（看长窗是不是真改善了风险）")
print(pad("臂", 11) + pad("最大回撤", 11) + pad("峰值日", 13) + pad("谷底日", 13) +
      pad("修复日", 13))
for name in NAMES:
    dd, p, t, rec = dd_win(FULL[name][2])
    print(pad(name, 11) + pad(f"{dd:.1%}", 11) + pad(p, 13) + pad(t, 13) +
          pad(rec or "未修复", 13))

# ---------- 5 ----------
print("\n【5】反事实：BTC 不参与缩放（各 lb 重置 κ）")
print(pad("臂", 22) + pad("全样本", 11) + pad("2023起累计", 12) + pad("回撤", 10) +
      pad("2021", 10))
for lb in (60, 120, 180):
    P = {}
    for c in COINS:
        P[c] = list(RAW[c][2]) if c == "BTC" else vt_pos(c, lb)
    P = align(P)
    nm = f"VT{lb}·BTC不缩"
    NAV[nm] = {c: E.nav_daily(RAW[c][0], RAW[c][1], P[c],
                              E.idx_of(RAW[c][0], "2020-09-01"), c) for c in COINS}
    r, dd = stat(portfolio(nm))
    r23, _ = stat(portfolio(nm, "2023-01-01"))
    r21, _ = stat(portfolio(nm, "2021-01-01", "2022-01-01"))
    print(pad(nm, 22) + pad(f"{1 + r:.1f}×", 11) + pad(f"{1 + r23:.2f}×", 12) +
          pad(f"{dd:.1%}", 10) + pad(f"{r21:+.0%}", 10))

# ---------- 6 ----------
print("\n【6】长窗有没有退化成「几乎不动」：系数离散度与触限")
print(pad("lb", 6) + pad("系数范围内", 14) + pad("截面标准差", 12) + pad("触上限", 9) +
      pad("触下限", 9) + pad("年内变动", 11))
for lb in (60, 120, 180, 365):
    allsc, within = [], []
    for c in COINS:
        vm = sorted(E.daily_vol(RAW[c][0], lb).values())
        if not vm:
            continue
        sc = [min(HI, max(LO, TARGET / v)) for v in vm]
        allsc += sc
        within.append(max(sc) - min(sc))
    lo_, hi_ = min(allsc), max(allsc)
    mean = sum(allsc) / len(allsc)
    sd = math.sqrt(sum((x - mean) ** 2 for x in allsc) / len(allsc))
    hh = sum(1 for x in allsc if x >= HI - 1e-9) / len(allsc)
    hl = sum(1 for x in allsc if x <= LO + 1e-9) / len(allsc)
    print(pad(lb, 6) + pad(f"{lo_:.2f} ~ {hi_:.2f}", 14) + pad(f"{sd:.3f}", 12) +
          pad(f"{hh:.1%}", 9) + pad(f"{hl:.1%}", 9) +
          pad(f"{sum(within) / len(within):.2f}", 11))
