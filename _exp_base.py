#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_base.py —— 底仓退出机制对照（P0-1）

问题：底仓 base=1.0 在 UP/RANGE 恒定，唯一退出通道是「状态翻 DOWN」。
     2022 年信号层 +23R 却亏 51.9%，2025 年 +122R 却亏 9.7% —— 两次都是底仓。
     状态机判错时，底仓没有任何独立的风控通道，只能等它自己认错。

五臂（信号层完全不动，只改底仓系数）：
  A 基线      base = 1.0（UP/RANGE），维持现状
  B MA120    日线收盘跌破 MA120 → 底仓减半（仍持有，等状态机认错）
  C TRAIL    底仓独立跟踪止损：从区间高点回撤 > 3×ATR(4H) → 底仓清零到状态翻转为 DOWN
  D RANGE    区间（RANGE）底仓打折 0.5，UP 时仍 1.0
  E MA60     日线收盘跌破 MA60 → 底仓减半（比 MA120 敏感）

口径：
  · **口径 A**（本金恒 50 万、每年提走、收益简单相加）为主，这是唯一自洽的口径
  · 权重固定 weights_for(500000)，滑点按 50 万算，各臂同口径
  · 均线用**前一日**收盘计算，不用当日（避免未来函数）
  · 交易层（加仓/止损/状态机）一律不动 —— 只隔离底仓这一个变量

判定标准（跑之前写死）：
  ① 口径 A 累计盈亏不能低于基线（否则就是拿收益换安慰）
  ② 2022 与 2025 两个亏损年必须改善，且不能靠牺牲其他年
  ③ 滚动 15 窗（每窗 250 交易日）里改善窗口 ≥ 9 个
  三条全过才算候选，缺一不落地。
"""
import math
import os
import sys
from datetime import date

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


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def pct(v, n=9, sign=True):
    if v is None:
        return pad("—", n)
    return pad(("%+.1f%%" % (v * 100)) if sign else ("%.1f%%" % (v * 100)), n)


# ---------------- 数据 ----------------
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
    return dict(b4=b4, rg4=rg4, atr=atr, sig=sig, trades=trades, d1=d1)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
print("加载完成", flush=True)


# ---------------- 底仓规则 ----------------
def ma_map(d1, n):
    """日线 MA 的对齐表：{4H 日期: 该日**之前**已收盘的 MA_n}。

    用前一日 MA —— 当日日线在 4H 日内尚未收盘，用当日 MA 是未来函数。
    """
    dc = [(x["date"][:10], x["close"]) for x in d1]
    out = {}
    prev = None
    for i in range(len(dc)):
        d, _ = dc[i]
        if i >= n - 1:
            cur = sum(x[1] for x in dc[i - n + 1:i + 1]) / n
            out[d] = prev if prev is not None else cur
            prev = cur
        else:
            prev = None
    return out


def make_base(kind, D):
    """返回该臂的底仓系数序列（与 b4 等长），取值 0/0.5/1.0。"""
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    n = len(b4)
    if kind == "A":
        return [BT.BASE.get(rg4[k], 0.0) for k in range(n)]

    if kind == "D":                     # RANGE 打折
        return [0.5 if rg4[k] == "RANGE" else BT.BASE.get(rg4[k], 0.0)
                for k in range(n)]

    if kind in ("B", "E"):              # 跌破 MA 减半
        lb = 120 if kind == "B" else 60
        mm = ma_map(D["d1"], lb)
        out = []
        for k in range(n):
            b = BT.BASE.get(rg4[k], 0.0)
            if b > 0:
                m = mm.get(b4[k]["date"][:10])
                if m and b4[k]["close"] < m:
                    b *= 0.5
            out.append(b)
        return out

    if kind == "C":                     # 底仓独立跟踪止损
        out, peak, dead = [], None, False
        K = 3.0
        for k in range(n):
            rg = rg4[k]
            if rg == "DOWN":
                peak, dead = None, False
                out.append(0.0)
                continue
            if peak is None:
                peak = b4[k]["close"]
            if not dead:
                peak = max(peak, b4[k]["close"])
                a = atr[k]
                if a and b4[k]["close"] < peak - K * a:
                    dead = True
            out.append(0.0 if dead else 1.0)
        return out

    raise ValueError(kind)


# ---------------- 净值 ----------------
def navs_of(base_list):
    out = {}
    for c in ALL12:
        D = DATA[c]
        b4, sig = D["b4"], D["sig"]
        pos = [base_list[c][k] + BT.ADD * sig[k] for k in range(len(b4))]
        pv = [p * k * VT.KAPPA for p, k in zip(pos, COEF[c])]
        out[c] = (b4, base_list[c], pv)
    return out


def get_nav(c, w, pack):
    b4, base, pos = pack
    qv = PC.CAP_4H_WORST.get(c) or 1e12
    res, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = pos[k] - base[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)
            nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * d / qv) * d)
        res[b4[k]["date"]] = nav
    return res


def curve(NAVS, FIRST, ALLD, start, end=None):
    last, units, cq, cash = {}, {}, None, 0.0
    out = []
    for d in [x for x in ALLD if x >= start and (end is None or x < end)]:
        live = [c for c in ALL12 if FIRST[c] <= d]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
        if not units:
            v = 1.0
        else:
            v = cash + sum(units.get(c, 0.0) * last.get(c, 1.0) for c in live)
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


def metrics(cv, years):
    """口径 A：逐年收益 + 累计盈亏；另给全段复利倍数与回撤。"""
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    yr = {y: lastv[y] / first[y] - 1 for y in first}
    cum = sum(CAP * yr.get(y, 0.0) for y in years)
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return yr, cum, dd, cv[-1][1] / cv[0][1] - 1


ARMS = [("A 基线", "A"), ("B MA120减半", "B"), ("C 3ATR止损", "C"),
        ("D RANGE打5折", "D"), ("E MA60减半", "E")]

print("\n" + "=" * 100)
print("【1】各臂底仓系数分布（全样本占比）")
print("=" * 100)
print(pad("臂", 16) + pad("底仓=1.0", 11) + pad("底仓=0.5", 11)
      + pad("底仓=0", 10) + pad("平均底仓", 11))
print("-" * 100)
BASES = {}
for name, kind in ARMS:
    bl = {c: make_base(kind, DATA[c]) for c in ALL12}
    BASES[kind] = bl
    tot = [x for c in ALL12 for x in bl[c]]
    n = len(tot)
    print(pad(name, 16) + pad("%.0f%%" % (sum(1 for x in tot if x == 1.0) / n * 100), 11)
          + pad("%.0f%%" % (sum(1 for x in tot if x == 0.5) / n * 100), 11)
          + pad("%.0f%%" % (sum(1 for x in tot if x == 0.0) / n * 100), 10)
          + pad("%.3f" % (sum(tot) / n), 11))

# ---------------- 跑各臂 ----------------
RES = {}
for name, kind in ARMS:
    packs = navs_of(BASES[kind])
    NAVS = {c: get_nav(c, W.get(c, 0.09), packs[c]) for c in ALL12}
    FIRST = {c: min(NAVS[c]) for c in ALL12}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    # 口径 A：每年独立（本金恒 50 万）
    cvs = {}
    for y in range(2020, 2027):
        s = START_NAV if y == 2020 else f"{y}-01-01"
        cvs[y] = curve(NAVS, FIRST, ALLD, s, f"{y + 1}-01-01")
    yr = {}
    for y, cv in cvs.items():
        if len(cv) < 20:
            continue
        yr[y] = cv[-1][1] / cv[0][1] - 1
    cum = sum(CAP * v for v in yr.values())

    # 全段（复利，仅作参考）
    cvall = curve(NAVS, FIRST, ALLD, START_NAV)
    pk, dd = cvall[0][1], 0.0
    for _, v in cvall:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)

    # 滚动 15 窗（每窗 250 交易日 ≈ 1 年，步长半年）
    days = [d for d, _ in cvall]
    rolls = []
    step = 125
    for i in range(0, len(days) - 250, step):
        s, e = days[i], days[i + 250]
        c2 = curve(NAVS, FIRST, ALLD, s, e)
        if len(c2) > 200:
            rolls.append(c2[-1][1] / c2[0][1] - 1)
    RES[kind] = dict(yr=yr, cum=cum, dd=dd, mult=cvall[-1][1] / cvall[0][1],
                     rolls=rolls, name=name)

YEARS = sorted(RES["A"]["yr"])
print("\n" + "=" * 100)
print("【2】口径 A 逐年收益（本金恒 50 万、每年提走）")
print("=" * 100)
print(pad("臂", 16) + "".join(pad(y, 9) for y in YEARS) + pad("累计盈亏U", 13)
      + pad("vs基线", 11))
print("-" * 100)
for kind, _ in [(k, n) for n, k in ARMS]:
    r = RES[kind]
    print(pad(r["name"], 16) + "".join(pct(r["yr"].get(y), 9) for y in YEARS)
          + pad("%+.0f" % r["cum"], 13)
          + pad("%+.0f" % (r["cum"] - RES["A"]["cum"]), 11))

print("\n" + "=" * 100
      )
print("【3】滚动 250 交易日窗口（15 窗，步长 125 日）—— 稳定性检验")
print("=" * 100)
print(pad("臂", 16) + pad("窗口数", 8) + pad("改善", 7) + pad("变差", 7)
      + pad("中位差pp", 11) + pad("均值差pp", 11))
print("-" * 100)
base_rolls = RES["A"]["rolls"]
for kind, _ in [(k, n) for n, k in ARMS]:
    if kind == "A":
        continue
    rr = RES[kind]["rolls"]
    m = min(len(rr), len(base_rolls))
    diffs = sorted((rr[i] - base_rolls[i]) * 100 for i in range(m))
    up = sum(1 for x in diffs if x > 0)
    dn = sum(1 for x in diffs if x < 0)
    med = diffs[len(diffs) // 2] if diffs else 0
    print(pad(RES[kind]["name"], 16) + pad(m, 8) + pad(up, 7) + pad(dn, 7)
          + pad("%+.1f" % med, 11) + pad("%+.1f" % (sum(diffs) / len(diffs) if diffs else 0), 11))

print("\n" + "=" * 100)
print("【4】全段复利（参考，非决策口径）与最大回撤")
print("=" * 100)
print(pad("臂", 16) + pad("倍数", 10) + pad("最大回撤", 11) + pad("vs基线回撤", 13))
print("-" * 100)
for kind, _ in [(k, n) for n, k in ARMS]:
    r = RES[kind]
    print(pad(r["name"], 16) + pad("%.2f×" % r["mult"], 10) + pct(r["dd"], 11)
          + pad("%+.1fpp" % ((r["dd"] - RES["A"]["dd"]) * 100), 13))

print("\n" + "=" * 100)
print("【5】判定（标准在文件头写死）")
print("=" * 100)
print("① 累计盈亏不低于基线：", end="")
print(" / ".join(f"{RES[k]['name']}{'✓' if RES[k]['cum'] >= RES['A']['cum'] else '✗'}"
                for k, _ in [(k, n) for n, k in ARMS] if k != "A"))
print("② 2022 与 2025 同时改善：", end="")
print(" / ".join(
    f"{RES[k]['name']}{'✓' if RES[k]['yr'].get(2022, 0) > RES['A']['yr'].get(2022, 0) and RES[k]['yr'].get(2025, 0) > RES['A']['yr'].get(2025, 0) else '✗'}"
    for k, _ in [(k, n) for n, k in ARMS] if k != "A"))
print("③ 滚动改善窗口 ≥9：", end="")
_ok = []
for kind, _ in [(k, n) for n, k in ARMS]:
    if kind == "A":
        continue
    m = min(len(RES[kind]["rolls"]), len(base_rolls))
    up = sum(1 for i in range(m) if RES[kind]["rolls"][i] > base_rolls[i])
    _ok.append(f"{RES[kind]['name']}{up}{'✓' if up >= 9 else '✗'}")
print(" / ".join(_ok))

# ================= 参数网格 =================
print("\n" + "=" * 100)
print("【6】参数网格：MA 长度 × 跌破后的底仓系数（值 = 口径 A 累计盈亏，万元）")
print("=" * 100)
print("1.00 = 不减仓（等于基线）。只跑「跌破 MA」这一族，因为 B/E 已经证明它优于 C/D。")

MAS = [40, 50, 60, 80, 100, 120]
CVS = [0.0, 0.25, 0.50, 0.75, 1.00]
MM = {lb: {c: ma_map(DATA[c]["d1"], lb) for c in ALL12} for lb in MAS}


def base_ma(D, mm, cv):
    b4, rg4 = D["b4"], D["rg4"]
    out = []
    for k in range(len(b4)):
        b = BT.BASE.get(rg4[k], 0.0)
        if b > 0 and cv < 1.0:
            m = mm.get(b4[k]["date"][:10])
            if m and b4[k]["close"] < m:
                b *= cv
        out.append(b)
    return out


def evaluate(bl):
    packs = navs_of(bl)
    NAVS = {c: get_nav(c, W.get(c, 0.09), packs[c]) for c in ALL12}
    FIRST = {c: min(NAVS[c]) for c in ALL12}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    yr = {}
    for y in range(2020, 2027):
        s = START_NAV if y == 2020 else f"{y}-01-01"
        cv_ = curve(NAVS, FIRST, ALLD, s, f"{y + 1}-01-01")
        if len(cv_) > 20:
            yr[y] = cv_[-1][1] / cv_[0][1] - 1
    return yr, sum(CAP * v for v in yr.values())


GRID = {}
print(pad("MA", 7) + "".join(pad("cv=%.2f" % c, 12) for c in CVS))
print("-" * 100)
for lb in MAS:
    line = pad(lb, 7)
    for cvc in CVS:
        if cvc == 1.0:
            GRID[(lb, cvc)] = (RES["A"]["yr"], RES["A"]["cum"])
        else:
            bl = {c: base_ma(DATA[c], MM[lb][c], cvc) for c in ALL12}
            GRID[(lb, cvc)] = evaluate(bl)
        line += pad("%.0f" % (GRID[(lb, cvc)][1] / 1e4), 12)
    print(line)
print("-" * 100)
print("基线（不减仓）= %.0f 万" % (RES["A"]["cum"] / 1e4))

# ================= 时间切分 =================
print("\n" + "=" * 100)
print("【7】样本内/样本外切分：2020-2023 选参数，2024-2026 验证")
print("=" * 100)
print("防止「MA60+0.5 只是 2021 一次事件的运气」。")


def seg_sum(yr, ys):
    return sum(CAP * yr.get(y, 0.0) for y in ys)


UP_Y = [2020, 2021, 2022, 2023]
DN_Y = [2024, 2025, 2026]
rank = []
for (lb, cvc), (yr, _c) in GRID.items():
    rank.append((seg_sum(yr, UP_Y), seg_sum(yr, DN_Y), lb, cvc))
rank.sort(reverse=True)
print(pad("排名", 6) + pad("MA", 6) + pad("cv", 7) + pad("2020-23万", 13)
      + pad("2024-26万", 13) + pad("合计万", 11) + "  备注")
print("-" * 100)
top_in = rank[0]
for i, (a, b, lb, cvc) in enumerate(rank[:8], 1):
    print(pad(i, 6) + pad(lb, 6) + pad("%.2f" % cvc, 7) + pad("%.0f" % (a / 1e4), 13)
          + pad("%.0f" % (b / 1e4), 13) + pad("%.0f" % ((a + b) / 1e4), 11)
          + ("  ← 样本内最优" if (lb, cvc) == (top_in[2], top_in[3]) else ""))
print("-" * 100)
# 样本内最优在样本外的排名
dn_sorted = sorted(rank, key=lambda x: -x[1])
pos_dn = next(i for i, x in enumerate(dn_sorted, 1)
              if (x[2], x[3]) == (top_in[2], top_in[3]))
print(f"样本内最优 = MA{top_in[2]} cv={top_in[3]:.2f}"
      f"（前段 {top_in[0] / 1e4:.0f} 万）；它在 2024-26 段排第 {pos_dn}/{len(rank)} 位"
      f"（{top_in[1] / 1e4:.0f} 万）。")
base_up = seg_sum(RES["A"]["yr"], UP_Y)
base_dn = seg_sum(RES["A"]["yr"], DN_Y)
print(f"基线：前段 {base_up / 1e4:.0f} 万，后段 {base_dn / 1e4:.0f} 万。")
print("若样本内最优在后段仍排前列，说明这不是拟合出来的；若掉到中下游，就别采用。")

# ================= 候选滚动检验 =================
print("\n" + "=" * 100)
print("【8】候选参数的滚动检验（5 个，含最优点与稳健点）")
print("=" * 100)
print("网格单调说明「没有最优点，只有方向」。所以不能按网格最大值选，")
print("必须挑「前后两段都排名靠前 + 滚动窗口多数胜出」的那个。")
CANDS = [(40, 0.25), (50, 0.25), (60, 0.50), (60, 0.25), (80, 0.50)]
print(pad("参数", 16) + pad("前段排名", 10) + pad("后段排名", 10)
      + pad("合计万", 10) + pad("滚动胜/负", 12) + pad("中位差pp", 11))
print("-" * 100)
# 前/后段排名的分母
r_up = sorted((seg_sum(v[0], UP_Y), k) for k, v in GRID.items())
r_dn = sorted((seg_sum(v[0], DN_Y), k) for k, v in GRID.items())
UP_RANK = {k: i for i, (_, k) in enumerate(reversed(r_up), 1)}
DN_RANK = {k: i for i, (_, k) in enumerate(reversed(r_dn), 1)}

for lb, cvc in CANDS:
    yr, cum = GRID[(lb, cvc)]
    bl = {c: base_ma(DATA[c], MM[lb][c], cvc) for c in ALL12}
    packs = navs_of(bl)
    NAVS = {c: get_nav(c, W.get(c, 0.09), packs[c]) for c in ALL12}
    FIRST = {c: min(NAVS[c]) for c in ALL12}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    cvall = curve(NAVS, FIRST, ALLD, START_NAV)
    days = [d for d, _ in cvall]
    rolls = []
    for i in range(0, len(days) - 250, 125):
        c2 = curve(NAVS, FIRST, ALLD, days[i], days[i + 250])
        if len(c2) > 200:
            rolls.append(c2[-1][1] / c2[0][1] - 1)
    m = min(len(rolls), len(base_rolls))
    diffs = sorted((rolls[i] - base_rolls[i]) * 100 for i in range(m))
    up = sum(1 for x in diffs if x > 0)
    dn = len(diffs) - up
    med = diffs[len(diffs) // 2] if diffs else 0
    print(pad("MA%d cv=%.2f" % (lb, cvc), 16)
          + pad("%d/%d" % (UP_RANK[(lb, cvc)], len(GRID)), 10)
          + pad("%d/%d" % (DN_RANK[(lb, cvc)], len(GRID)), 10)
          + pad("%.0f" % (cum / 1e4), 10)
          + pad("%d/%d" % (up, dn), 12) + pad("%+.1f" % med, 11))
print("-" * 100)
print("选择逻辑（不是取最大值）：前后两段排名都要进前 8，且滚动胜场 ≥12/17。")
print("若最优点（MA40/50 cv=0）在后段掉出前 8，说明它是靠 2022 一年撑起来的。")

# ================= 可执行性：日频 + 滞回 =================
print("\n" + "=" * 100)
print("【9】可执行性检验：4H 频 vs 日频 vs 日频+滞回")
print("=" * 100)
print("约束：实盘是「邮件提醒 + 手动下单」。4H 频的底仓跳跃一天最多 6 次，不可执行。")
print("日频版 = 每天第一根 4H 用**前一交易日收盘 vs 前一交易日 MA** 判定，全天不变。")
print("滞回带 h：跌破 MA×(1-h) 才减仓，涨回 MA×(1+h) 才恢复，防止在均线附近抖动。")


def daily_ctx(d1, lb):
    """{日期: (前一交易日收盘, 前一交易日 MA_lb)} —— 严格无未来函数。"""
    dc = [(x["date"][:10], x["close"]) for x in d1]
    ma = {}
    for i in range(lb - 1, len(dc)):
        ma[dc[i][0]] = sum(x[1] for x in dc[i - lb + 1:i + 1]) / lb
    out = {}
    for i in range(1, len(dc)):
        out[dc[i][0]] = (dc[i - 1][1], ma.get(dc[i - 1][0]))
    return out


def base_ma_v2(D, ctx, cv, daily=True, h=0.0):
    """daily=False → 每根 4H 用当前 close 判；daily=True → 每天首根用前一日收盘判。"""
    b4, rg4 = D["b4"], D["rg4"]
    out, on, last_d = [], None, None
    for k in range(len(b4)):
        d = b4[k]["date"][:10]
        b0 = BT.BASE.get(rg4[k], 0.0)
        if b0 <= 0:
            out.append(0.0)
            on, last_d = None, d
            continue
        if (on is None) or (not daily) or (d != last_d):
            pclose, pma = ctx.get(d, (None, None))
            px = pclose if daily else b4[k]["close"]
            if pma and px:
                if on is None:
                    on = cv if px < pma * (1 + h) else 1.0
                elif on >= 1.0 and px < pma * (1 - h):
                    on = cv
                elif on < 1.0 and px > pma * (1 + h):
                    on = 1.0
            elif on is None:
                on = 1.0
        out.append(on)
        last_d = d
    return out


def turnover(bl):
    """底仓系数的年化切换次数（12 币加总 / 6.2 年）。"""
    n = 0
    for c in ALL12:
        s = bl[c]
        n += sum(1 for k in range(1, len(s)) if abs(s[k] - s[k - 1]) > 1e-9)
    return n / 6.2


CTX = {lb: {c: daily_ctx(DATA[c]["d1"], lb) for c in ALL12} for lb in (50, 60)}
VARIANTS = [
    ("MA50 cv.25  4H频", 50, 0.25, False, 0.0),
    ("MA50 cv.25  4H+1%", 50, 0.25, False, 0.010),
    ("MA50 cv.25  4H+2%", 50, 0.25, False, 0.020),
    ("MA50 cv.25  4H+3%", 50, 0.25, False, 0.030),
    ("MA60 cv.50  4H频", 60, 0.50, False, 0.0),
    ("MA60 cv.50  4H+2%", 60, 0.50, False, 0.020),
    ("MA60 cv.50  4H+4%", 60, 0.50, False, 0.040),
    ("MA50 cv.25  日频+1%", 50, 0.25, True, 0.010),
]
print(pad("变体", 22) + pad("合计万", 10) + pad("vs基线", 11) + pad("2022", 9)
      + pad("2025", 9) + pad("年换手次", 11) + pad("日均", 8) + pad("最大单日", 10))
print("-" * 100)
for label, lb, cvc, daily, h in VARIANTS:
    bl = {c: base_ma_v2(DATA[c], CTX[lb][c], cvc, daily, h) for c in ALL12}
    yr, cum = evaluate(bl)
    to = turnover(bl)
    # 最大单日切换次数（12 币合计）
    per_day = {}
    for c in ALL12:
        s, b4 = bl[c], DATA[c]["b4"]
        for k in range(1, len(s)):
            if abs(s[k] - s[k - 1]) > 1e-9:
                d = b4[k]["date"][:10]
                per_day[d] = per_day.get(d, 0) + 1
    mx = max(per_day.values()) if per_day else 0
    print(pad(label, 22) + pad("%.0f" % (cum / 1e4), 10)
          + pad("%+.0f" % ((cum - RES["A"]["cum"]) / 1e4), 11)
          + pct(yr.get(2022), 9) + pct(yr.get(2025), 9)
          + pad("%.0f" % to, 11) + pad("%.1f" % (to / 250), 8) + pad(mx, 10))
print("-" * 100)
print(f"基线：{RES['A']['cum'] / 1e4:.0f} 万，2022 {RES['A']['yr'].get(2022) * 100:+.1f}%，"
      f"2025 {RES['A']['yr'].get(2025) * 100:+.1f}%，底仓切换 {turnover(BASES['A']):.0f} 次/年")
print("「年换手次」= 12 币底仓系数变化次数总和 / 6.2 年。实盘每天超过 2~3 次就难手动跟。")

# ================= 最终候选完整验证 =================
print("\n" + "=" * 100)
print("【10】最终候选 MA50 / cv=0.25 / 4H频+1%滞回 —— 完整逐年 + 滚动")
print("=" * 100)
FIN_LB, FIN_CV, FIN_H = 50, 0.25, 0.010
fin_bl = {c: base_ma_v2(DATA[c], CTX[FIN_LB][c], FIN_CV, False, FIN_H) for c in ALL12}
packs = navs_of(fin_bl)
NAVS = {c: get_nav(c, W.get(c, 0.09), packs[c]) for c in ALL12}
FIRST = {c: min(NAVS[c]) for c in ALL12}
ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
fin_yr = {}
for y in range(2020, 2027):
    s = START_NAV if y == 2020 else f"{y}-01-01"
    c2 = curve(NAVS, FIRST, ALLD, s, f"{y + 1}-01-01")
    if len(c2) > 20:
        fin_yr[y] = c2[-1][1] / c2[0][1] - 1
cvall = curve(NAVS, FIRST, ALLD, START_NAV)
pk, fin_dd = cvall[0][1], 0.0
for _, v in cvall:
    pk = max(pk, v)
    fin_dd = min(fin_dd, v / pk - 1)
print(pad("年份", 8) + pad("基线", 10) + pad("候选", 10) + pad("差额pp", 10)
      + pad("权益(基线)", 14) + pad("权益(候选)", 14))
print("-" * 100)
eqb = eqf = 1.0
for y in sorted(RES["A"]["yr"]):
    a, b = RES["A"]["yr"].get(y), fin_yr.get(y)
    if a is None or b is None:
        continue
    eqb *= 1 + a
    eqf *= 1 + b
    print(pad(y, 8) + pct(a, 10) + pct(b, 10) + pad("%+.1f" % ((b - a) * 100), 10)
          + pad("%.0f万" % (CAP * eqb / 1e4), 14) + pad("%.0f万" % (CAP * eqf / 1e4), 14))
print("-" * 100)
cum_b = RES["A"]["cum"]
cum_f = sum(CAP * v for v in fin_yr.values())
print(f"口径 A 累计：基线 {cum_b / 1e4:.0f} 万 → 候选 {cum_f / 1e4:.0f} 万"
      f"（{cum_f - cum_b:+,.0f} U，{(cum_f / cum_b - 1) * 100:+.0f}%）")
print(f"最大回撤：基线 {RES['A']['dd'] * 100:.1f}% → 候选 {fin_dd * 100:.1f}%")
print(f"年换手：{turnover(fin_bl):.0f} 次/年（日均 {turnover(fin_bl) / 250:.1f} 次）")

days = [d for d, _ in cvall]
rolls = []
for i in range(0, len(days) - 250, 125):
    c2 = curve(NAVS, FIRST, ALLD, days[i], days[i + 250])
    if len(c2) > 200:
        rolls.append(c2[-1][1] / c2[0][1] - 1)
m = min(len(rolls), len(base_rolls))
diffs = sorted((rolls[i] - base_rolls[i]) * 100 for i in range(m))
up = sum(1 for x in diffs if x > 0)
print(f"\n滚动 250 日窗口（{m} 个）：改善 {up} / 变差 {m - up}，"
      f"中位差 {diffs[len(diffs) // 2]:+.1f}pp，均值差 {sum(diffs) / len(diffs):+.1f}pp")
print(f"最差一个窗口：{diffs[0]:+.1f}pp；最好一个：{diffs[-1]:+.1f}pp")
print("\n三条硬标准复核：")
print(f"  ① 累计不低于基线：{cum_f / 1e4:.0f} 万 vs {cum_b / 1e4:.0f} 万 → "
      f"{'✓' if cum_f >= cum_b else '✗'}")
print(f"  ② 2022 与 2025 同时改善：2022 {fin_yr.get(2022, 0) * 100:+.1f}% vs "
      f"{RES['A']['yr'].get(2022, 0) * 100:+.1f}%；2025 {fin_yr.get(2025, 0) * 100:+.1f}% vs "
      f"{RES['A']['yr'].get(2025, 0) * 100:+.1f}% → "
      f"{'✓' if fin_yr.get(2022, 0) > RES['A']['yr'].get(2022, 0) and fin_yr.get(2025, 0) > RES['A']['yr'].get(2025, 0) else '✗'}")
print(f"  ③ 滚动改善 ≥9/{m}：{up} → {'✓' if up >= 9 else '✗'}")
