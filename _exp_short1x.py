#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_short1x.py —— 空头从 0.5× 提到 1.0×，行不行

背景：当前 DOWN 时 base=0、add=0.5，空头峰值 0.5×；多头峰值 1.5×。
用户问能不能把空头也提到 1×。

"提到 1×" 有三种完全不同的实现，必须分开：
  A 类「只放大突破层」：base 仍 0，add 0.5→1.0。
     → 峰值 1.0×，但仍要 4H 跌破才进，在场时间不增加。
  B 类「给空头加底仓」：DOWN 状态就持有 0.5×，突破再加 0.5×。
     → 峰值 1.0×，且**不等突破就在场**，在场时间大幅提高。
  C 类「给空头加底仓，但受守卫」：同上，但 base 只在「非 init 血统 +
     ATR 分位 ≤85%」时才持有 —— 与多头加仓层的开仓门槛对齐。

关键怀疑（来自 _diag_symmetry.py 的实测）：
  DOWN 占 43.6% 的时间，但该状态内单根 4H 平均收益 +0.0094%（几乎零漂移）。
  若"判 DOWN ≠ 在下跌"，放大空头就是把零漂移的波动同步放大 ——
  收益不线性增长，回撤线性增长，Calmar 反而变差。

做五段：
  【1】八臂主对照：逐年 + 累计 + 回撤 + Calmar + 平均敞口
  【2】假 DOWN 伤害：DOWN 段内价格大涨的段，各臂损失多少（最硬的风险检验）
  【3】滚动窗口 250 日
  【4】单位敞口效率（排除"押得多所以赚得多"的伪解释）
  【5】保证金与资金费代价

口径：A（本金恒 50 万、每年提走、收益率简单相加），与全部历史结论一致。
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


def _allow(c):
    """逐 bar：是否「允许开新仓」—— 只看门槛，不看突破。

    与 signals() 里的三个过滤逐条对齐：状态是 UP/DOWN、不是 init 血统、
    ATR 分位 ≤ vol_q。空头底仓若不受这三条约束，就会在 HYPE 2025-04
    那种「假 DOWN 48 天」里全程满仓做空。
    """
    D = DATA[c]
    b4, rg4, bl4 = D["b4"], D["rg4"], D["bl4"]
    pct = BT.atr_percentile(b4, D["atr"], coin=c)
    out = []
    for k in range(len(b4)):
        ok = rg4[k] in ("UP", "DOWN") and D["atr"][k] is not None
        if ok and BT.BLOCK_INIT and bl4 and bl4[k] == "init_ma120":
            ok = False
        if ok and pct is not None and (pct[k] is None or pct[k] > BT.VOL_Q):
            ok = False
        out.append(ok)
    return out


ALLOW = {c: _allow(c) for c in ALL12}
print("加载完成\n", flush=True)


def run(bl=1.0, al=0.5, bs=0.0, as_=0.5, bs_guard=False):
    """bl/al = 多头 base/add 倍数；bs/as_ = 空头 base/加仓 倍数。

    多头 base 走现货、不付资金费；多头 add 与**全部空头仓位**走永续、付资金费。
    空头 base 的存在意义是「不等 4H 跌破就在场」，因此它同样是永续。
    bs_guard=True 时，空头 base 只在 allow 为真的 bar 持有。
    """
    NAVS, expo = {}, {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        al_ok = ALLOW[c]
        pv, lb_pv = [], []
        for k in range(len(b4)):
            rg = rg4[k]
            s = sig[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = bl + al * s
                lb = bl
            elif rg == "RANGE":
                p = bl
                lb = bl
            elif rg == "DOWN":
                b_eff = bs if (not bs_guard or al_ok[k]) else 0.0
                p = -b_eff + as_ * s          # s = -1 → -b_eff - as_
                lb = 0.0
            else:
                p, lb = 0.0, 0.0
            pv.append(p * coef)
            lb_pv.append(lb * coef)
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        w = W.get(c, 0.09)
        nav, res = 1.0, {}
        k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
        for k in range(k0 + 1, len(b4)):
            d10 = b4[k]["date"][:10]
            expo[d10] = expo.get(d10, 0.0) + abs(pv[k]) * w
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pv[k] * r
            nav *= x if x > 0 else 0.0
            # 只有多头现货底仓那一层不吃资金费
            sl = pv[k] - lb_pv[k]
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
    avg_ex = sum(expo.values()) / max(1, len(expo))
    return out, avg_ex


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
    out = []
    for i in range(0, len(cv) - win, 21):
        out.append(cv[i + win][1] / cv[i][1] - 1)
    return out


# ============================================================
# 【1】八臂主对照
# ============================================================
ARMS = [
    ("S0 现状 空0+0.5", 1.0, 0.5, 0.0, 0.5, False),
    ("S1 空0+1.0 只放大突破", 1.0, 0.5, 0.0, 1.0, False),
    ("S2 空0.5+0.5 加空头底仓", 1.0, 0.5, 0.5, 0.5, False),
    ("S3 空1.0+0 全程无守卫", 1.0, 0.5, 1.0, 0.0, False),
    ("S4 空0+1.5 对称多头峰值", 1.0, 0.5, 0.0, 1.5, False),
    ("S5 空0.5+1.0 峰值1.5", 1.0, 0.5, 0.5, 1.0, False),
    ("S6 空1.0+0 带守卫", 1.0, 0.5, 1.0, 0.0, True),
    ("S7 空0.5+0.5 带守卫", 1.0, 0.5, 0.5, 0.5, True),
]
print("=" * 116)
print("【1】空头加码八臂对照（口径 A：本金恒 50 万、每年提走、收益率简单相加）")
print("=" * 116)
print(pad("臂", 30) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8) + pad("敞口", 8))
print("-" * 116)
R = {}
for nm, bl, al, bs, as_, g in ARMS:
    cv, ex = run(bl, al, bs, as_, g)
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    R[nm] = dict(yr=yr, cum=cum, dd=dd, ex=ex, calmar=cal, cv=cv)
    print(pad(nm, 30) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8) + pad("%.2f×" % ex, 8))

s0 = R["S0 现状 空0+0.5"]
print("\n  相对 S0 的增量：")
print(pad("臂", 30) + pad("累计差额", 12) + pad("回撤变化", 12) + pad("Calmar变化", 13)
      + pad("敞口变化", 12))
print("-" * 116)
for nm in R:
    if nm == "S0 现状 空0+0.5":
        continue
    r = R[nm]
    print(pad(nm, 30) + pad("%+.0f 万" % ((r["cum"] - s0["cum"]) / 1e4), 12)
          + pad("%+.1fpp" % ((r["dd"] - s0["dd"]) * 100), 12)
          + pad("%+.2f" % (r["calmar"] - s0["calmar"]), 13)
          + pad("%+.2f×" % (r["ex"] - s0["ex"]), 12))

# ============================================================
# 【2】假 DOWN 伤害
# ============================================================
print("\n" + "=" * 116)
print("【2】假 DOWN 伤害：DOWN 段内价格大涨的段，放大空头会亏多少")
print("=" * 116)
down_segs = []
for c in ALL12:
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    n = len(b4)
    k = 0
    while k < n:
        if rg4[k] != "DOWN":
            k += 1
            continue
        j = k
        while j < n and rg4[j] == "DOWN":
            j += 1
        if b4[k]["date"][:10] >= START_NAV and j - k >= 6:
            down_segs.append({"c": c, "i": k, "j": j, "bars": j - k,
                              "ret": b4[j - 1]["close"] / b4[k]["close"] - 1})
        k = j
bad = [s for s in down_segs if s["ret"] > 0.20]
print(f"  DOWN 段共 {len(down_segs)} 个（≥6 根 4H），其中段内涨幅 >20% 的 "
      f"{len(bad)} 个 = {len(bad) / len(down_segs):.1%}")
print("\n  最差的 10 段（判 DOWN 却大涨）—— 放大空头最危险的地方：")
print("  " + pad("币", 7) + pad("起始", 13) + pad("根数", 7) + pad("段内涨跌", 12))
print("  " + "-" * 60)
for s in sorted(bad, key=lambda x: -x["ret"])[:10]:
    print("  " + pad(s["c"], 7) + pad(DATA[s["c"]]["b4"][s["i"]]["date"][:10], 13)
          + pad(s["bars"], 7) + pad("%+.1f%%" % (s["ret"] * 100), 12))


def base_pnl(c, i, j, guard):
    """空头 base 在 [i, j) 段上的价格收益贡献（每 1× 名义，负数=亏）。"""
    b4 = DATA[c]["b4"]
    ok = ALLOW[c] if guard else None
    tot = 0.0
    for k in range(i + 1, j):
        if ok is not None and not ok[k]:
            continue
        tot += b4[k]["close"] / b4[k - 1]["close"] - 1
    return -tot          # 空头：跌了才赚


print("\n  各臂在这些假 DOWN 段上的合计损益（每 1× 名义的价格收益率）：")
print(pad("臂", 30) + pad("突破层", 12) + pad("空头base层", 13) + pad("合计", 12)
      + pad("占其总收益", 13))
print("-" * 116)
for nm, bl, al, bs, as_, g in ARMS:
    brk = 0.0
    for s in bad:
        for t in DATA[s["c"]]["trades"]:
            if t["side"] == -1 and s["i"] <= t["i"] < s["j"]:
                brk += t["r"] * t["stop_pct"]
    bp = sum(base_pnl(s["c"], s["i"], s["j"], g) for s in bad) if bs else 0.0
    tot = as_ * brk + bs * bp
    share = tot / (R[nm]["cum"] / CAP) * 100 if R[nm]["cum"] else 0
    print(pad(nm, 30) + pad("%+.1f%%" % (as_ * brk * 100), 12)
          + pad("%+.1f%%" % (bs * bp * 100), 13)
          + pad("%+.1f%%" % (tot * 100), 12)
          + pad("%+.1f%%" % share, 13))
print("  → 「空头 base 层」那一列随 bs 线性放大，且没有任何止损保护；带守卫的臂会小很多")

# ============================================================
# 【3】滚动窗口
# ============================================================
print("\n" + "=" * 116)
print("【3】滚动 250 日（每 21 日一个起点，与 S0 逐窗口比较）")
print("=" * 116)
base_roll = roll(s0["cv"])
print(pad("臂", 30) + pad("窗口数", 9) + pad("胜 S0", 10) + pad("中位差", 11)
      + pad("最好", 10) + pad("最差", 10))
print("-" * 116)
for nm in R:
    if nm == "S0 现状 空0+0.5":
        continue
    rs = roll(R[nm]["cv"])
    m = min(len(rs), len(base_roll))
    dif = sorted(rs[i] - base_roll[i] for i in range(m))
    win = sum(1 for x in dif if x > 0)
    print(pad(nm, 30) + pad(m, 9) + pad(f"{win}/{m}", 10)
          + pad("%+.1fpp" % (dif[m // 2] * 100), 11)
          + pad("%+.1fpp" % (dif[-1] * 100), 10)
          + pad("%+.1fpp" % (dif[0] * 100), 10))

# ============================================================
# 【4】单位敞口
# ============================================================
print("\n" + "=" * 116)
print("【4】单位敞口效率（排除「押得多所以赚得多」这个伪解释）")
print("=" * 116)
print(pad("臂", 30) + pad("累计", 11) + pad("敞口", 9) + pad("万/倍", 10)
      + pad("Calmar", 9))
print("-" * 116)
for nm in R:
    r = R[nm]
    print(pad(nm, 30) + pad("%.0f 万" % (r["cum"] / 1e4), 11)
          + pad("%.2f×" % r["ex"], 9)
          + pad("%.0f" % (r["cum"] / 1e4 / max(r["ex"], 1e-9)), 10)
          + pad("%.2f" % r["calmar"], 9))
print("  → 若「万/倍」随空头放大而下降，说明放大的是波动不是收益")

# ============================================================
# 【5】保证金
# ============================================================
print("\n" + "=" * 116)
print("【5】保证金占用（空头只能走永续，按 10% 维持保证金）")
print("=" * 116)
print(pad("臂", 30) + pad("空头峰值", 11) + pad("保证金占用", 13)
      + pad("占本金", 10))
print("-" * 116)
for nm, bl, al, bs, as_, g in ARMS:
    peak = bs + as_
    marg = CAP * peak * 0.10
    print(pad(nm, 30) + pad("%.1f×" % peak, 11)
          + pad("%.0f 万" % (marg / 1e4), 13)
          + pad("%.1f%%" % (peak * 10), 10))
print("\n  注：空头没有现货可借，每一层都在吃资金费与强平风险；")
print("  而多头底仓 1.0× 走现货，零资金费、零强平。这是两者最根本的执行层不对称。")
print("  放到 1.0× 意味着 1 倍杠杆做空 —— 有 1.5ATR 止损兜着，但插针下强平风险随杠杆上升。")
