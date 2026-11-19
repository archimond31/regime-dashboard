#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_range_src.py —— 按 RANGE 段的「来路」区分底仓

用户方案：上升转 RANGE 打 0.5，下降转 RANGE 打 1.0。

这是一个**不预测方向**的方案 —— 只用「进入 RANGE 之前那段是什么状态」做区分。
比上一轮的「RANGE 内择时」（用价格位置预测破位方向）合理得多，因为：
  · 来路是已发生的事实，不需要预测
  · 它表达的是一个结构判断：趋势刚熄火的盘整 vs 暴跌后筑底的盘整

但前提必须先验证：**UP→RANGE 和 DOWN→RANGE 的表现真的不同吗？**

六段输出：
  【0】来路 × 去向 交叉表 + 各组段内涨跌（验证前提）
  【1】主对照：六臂
  【2】前视检查（两种口径：原样 / 左移一格）
  【3】滚动 250 日 + 时间切分
  【4】分段贡献：UP→RANGE 段与 DOWN→RANGE 段的底仓各自赚亏多少

口径 A（本金恒 50 万、每年提走、收益率简单相加）。
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


def seg_list(c, min_bars=1):
    """枚举每个状态段：(状态, 来路, 去向, 起始k, 结束k, 段内涨跌, 根数)。"""
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    n = len(b4)
    out = []
    k = 0
    while k < n:
        s = rg4[k]
        j = k
        while j < n and rg4[j] == s:
            j += 1
        if s in STATES and b4[k]["date"][:10] >= START_NAV and j - k >= min_bars:
            src = rg4[k - 1] if k > 0 and rg4[k - 1] in STATES else "?"
            dst = rg4[j] if j < n and rg4[j] in STATES else "?"
            out.append(dict(st=s, src=src, dst=dst, i=k, j=j, bars=j - k,
                            ret=b4[j - 1]["close"] / b4[k]["close"] - 1))
        k = j
    return out


def range_by_src(c, cut_up=1.0, cut_dn=1.0, shift=0):
    """逐 bar 底仓系数：RANGE 段按来路打折，其余保持 1.0。

    shift=0 → 直接用（来路在第 k 根开始时就已知，理论上无前视）
    shift=1 → 再左移一格（保守口径，用于前视检查）
    """
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    out = [1.0] * len(b4)
    src = None
    for k in range(len(rg4)):
        s = rg4[k]
        if s == "RANGE":
            if src is None:
                prev = rg4[k - 1] if k > 0 else None
                src = prev if prev in ("UP", "DOWN") else "?"
            out[k] = cut_up if src == "UP" else (cut_dn if src == "DOWN" else 1.0)
        else:
            src = None
    if shift:
        return [1.0] + out[:-1]
    return out


def run(bseq_of, al=0.5, as_=0.5):
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = bseq_of(c)
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = b[k] + al * sig[k]
            elif rg == "RANGE":
                p = b[k]
            elif rg == "DOWN":
                p = as_ * sig[k]
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
    return [cv[i + win][1] / cv[i][1] - 1
            for i in range(0, len(cv) - win, 21)]


# ============================================================
# 【0】来路 × 去向 交叉表
# ============================================================
print("=" * 104)
print("【0】前提验证：RANGE 段按「来路」分组，表现真的不同吗？")
print("=" * 104)
segs = []
for c in ALL12:
    segs += [dict(s, c=c) for s in seg_list(c)]
rng = [s for s in segs if s["st"] == "RANGE"]
print(f"  RANGE 段共 {len(rng)} 个（无长度过滤）\n")
print(pad("来路", 8) + pad("段数", 8) + pad("平均根数", 10) + pad("段内平均涨跌", 14)
      + pad("中位", 12) + pad("下跌占比", 10))
print("-" * 104)
for src in ("UP", "DOWN", "?"):
    xs = [s for s in rng if s["src"] == src]
    if not xs:
        continue
    rs = sorted(x["ret"] for x in xs)
    print(pad(src, 8) + pad(len(xs), 8)
          + pad("%.0f" % (sum(x["bars"] for x in xs) / len(xs)), 10)
          + pad("%+.2f%%" % (sum(x["ret"] for x in xs) / len(xs) * 100), 14)
          + pad("%+.2f%%" % (rs[len(rs) // 2] * 100), 12)
          + pad("%.0f%%" % (sum(1 for x in xs if x["ret"] < 0) / len(xs) * 100), 10))

print("\n  来路 × 去向 交叉（每格：段数 / 段内平均涨跌）：")
print(pad("来路＼去向", 12) + "".join(pad(d, 18) for d in ("UP", "DOWN", "?")))
print("-" * 104)
for src in ("UP", "DOWN", "?"):
    row = pad(src, 12)
    for dst in ("UP", "DOWN", "?"):
        xs = [s for s in rng if s["src"] == src and s["dst"] == dst]
        row += pad(f"{len(xs)} / {sum(x['ret'] for x in xs) / len(xs) * 100:+.1f}%"
                   if xs else "—", 18)
    print(row)

print("\n  只用「来路」能不能区分？看两组去向分布：")
for src in ("UP", "DOWN"):
    xs = [s for s in rng if s["src"] == src]
    if not xs:
        continue
    up_n = sum(1 for x in xs if x["dst"] == "UP")
    dn_n = sum(1 for x in xs if x["dst"] == "DOWN")
    print(f"    {src}→RANGE ({len(xs)} 段)：后续翻 UP {up_n} 段（{up_n / len(xs):.0%}）"
          f"、翻 DOWN {dn_n} 段（{dn_n / len(xs):.0%}）")

# ============================================================
# 【1】主对照
# ============================================================
ARMS = [
    ("R0 基线（不区分）", lambda c: range_by_src(c, 1.0, 1.0)),
    ("R1 用户方案 UP0.5/DN1.0", lambda c: range_by_src(c, 0.5, 1.0)),
    ("R2 更激进 UP0.0/DN1.0", lambda c: range_by_src(c, 0.0, 1.0)),
    ("R3 不分来路一律0.5", lambda c: range_by_src(c, 0.5, 0.5)),
    ("R4 反向 UP1.0/DN0.5", lambda c: range_by_src(c, 1.0, 0.5)),
    ("R5 UP0.5/DN0.75", lambda c: range_by_src(c, 0.5, 0.75)),
]
print("\n" + "=" * 112)
print("【1】主对照：按 RANGE 来路打折（口径 A）")
print("=" * 112)
print(pad("臂", 26) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8))
print("-" * 112)
R = {}
for nm, fn in ARMS:
    cv = run(fn)
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    R[nm] = dict(yr=yr, cum=cum, dd=dd, calmar=cal, cv=cv, fn=fn)
    print(pad(nm, 26) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8))

r0 = R["R0 基线（不区分）"]
print("\n  相对基线：")
print(pad("臂", 26) + pad("累计差额", 12) + pad("回撤变化", 12) + pad("Calmar变化", 13))
print("-" * 112)
for nm in R:
    if nm.startswith("R0"):
        continue
    r = R[nm]
    print(pad(nm, 26) + pad("%+.0f 万" % ((r["cum"] - r0["cum"]) / 1e4), 12)
          + pad("%+.1fpp" % ((r["dd"] - r0["dd"]) * 100), 12)
          + pad("%+.2f" % (r["calmar"] - r0["calmar"]), 13))

# ============================================================
# 【2】前视检查
# ============================================================
print("\n" + "=" * 112)
print("【2】前视检查：原样 vs 左移一格")
print("=" * 112)
print(pad("臂", 26) + pad("原样", 11) + pad("左移1格", 11) + pad("差额", 11)
      + pad("判定", 12))
print("-" * 112)
for nm, _fn in ARMS[1:]:
    a = R[nm]["cum"]
    cut_up, cut_dn = {"R1": (0.5, 1.0), "R2": (0.0, 1.0), "R3": (0.5, 0.5),
                      "R4": (1.0, 0.5), "R5": (0.5, 0.75)}[nm.split()[0]]
    cv_b = run(lambda c, u=cut_up, d=cut_dn: range_by_src(c, u, d, shift=1))
    _yr, b, _dd, _cg = stat(cv_b)
    dif = (a - b) / 1e4
    verdict = "前视嫌疑" if dif > 30 else ("安全" if abs(dif) < 15 else "轻微")
    print(pad(nm, 26) + pad("%.0f 万" % (a / 1e4), 11) + pad("%.0f 万" % (b / 1e4), 11)
          + pad("%+.0f 万" % dif, 11) + pad(verdict, 12))
print("  说明：来路只依赖 rg4[k-1]（历史），理论上原样即无前视；左移一格应几乎不变。")

# ============================================================
# 【3】滚动 + 切分
# ============================================================
print("\n" + "=" * 112)
print("【3】滚动 250 日 + 时间切分")
print("=" * 112)
SPLIT = "2024-01-01"
base_roll = roll(r0["cv"])


def sub(cv, a, b):
    v = [p for p in cv if a <= p[0] < b]
    if len(v) < 30:
        return None
    pk, dd = v[0][1], 0.0
    for _, x in v:
        pk = max(pk, x)
        dd = min(dd, x / pk - 1)
    return v[-1][1] / v[0][1] - 1, dd


print(pad("臂", 26) + pad("2020-2023", 13) + pad("2024-2026", 13)
      + pad("滚动胜基线", 12) + pad("中位差", 11) + pad("最差", 10))
print("-" * 112)
for nm in R:
    if nm.startswith("R0"):
        continue
    a = sub(R[nm]["cv"], "2020-01-01", SPLIT)
    b = sub(R[nm]["cv"], SPLIT, "2027-01-01")
    rs = roll(R[nm]["cv"])
    m = min(len(rs), len(base_roll))
    dif = sorted(rs[i] - base_roll[i] for i in range(m))
    win = sum(1 for x in dif if x > 0)
    print(pad(nm, 26)
          + pad("%+.0f%%" % (a[0] * 100) if a else "—", 13)
          + pad("%+.0f%%" % (b[0] * 100) if b else "—", 13)
          + pad(f"{win}/{m}", 12)
          + pad("%+.1fpp" % (dif[m // 2] * 100), 11)
          + pad("%+.1fpp" % (dif[0] * 100), 10))

# ============================================================
# 【4】分段贡献
# ============================================================
print("\n" + "=" * 112)
print("【4】底仓在两类 RANGE 段上各自赚亏多少（按 1× 名义的价格收益率）")
print("=" * 112)
for src in ("UP", "DOWN"):
    xs = [s for s in rng if s["src"] == src]
    tot = sum(s["ret"] for s in xs)
    print(f"  {src}→RANGE：{len(xs)} 段，累计 {tot * 100:+.0f}%（每 1× 底仓名义）")
    print(f"    打 0.5 后这批段少赚/少亏 {tot * 50:+.0f}%")
print("\n  若 UP→RANGE 的累计是正的，那打 0.5 就是主动放弃正收益；")
print("  若是负的，打 0.5 才是真正的避险。这一行决定方案成不成立。")
