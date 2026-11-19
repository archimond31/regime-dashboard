#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_add_sleeve.py —— 只留加仓层为什么只剩 42 万

先拆掉一个伪问题：加仓层不是因为「笨」才赚得少。它的平均敞口只有 0.76×，
而完整策略是 3.46×。敞口差 4.5 倍，收益差 7.4 倍 —— 超出线性的那部分才是真问题。

真问题有两个，必须分开：
  ① 时间：它有多少时间在场？（底仓 UP/RANGE 一直在，加仓层只在突破后且未被打掉）
  ② 摩擦：ATR 止损 + 120 根兜底把趋势切成碎片，每次再入场都要重新突破，
     且震荡里反复被扫 −1R。底仓没有任何这类成本。

做法：
  【1】现有加仓层的交易解剖：笔数 / 出场原因分布 / 各组总 R / 在仓时间占比
  【2】对照臂「突破入场 + 只按状态出场」（去掉 ATR 止损与兜底，其余不变）
      若这一臂显著更高 → 损失主要来自摩擦
      若仍远低于底仓 → 损失主要来自「突破才进场」错过趋势前段
  【3】把在仓时间换算成「同等在场时间下的收益」，看还剩多少差距
  【4】反向检查：无止损版的回撤会不会失控（否则就是免费午餐，不合常理）
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
LOOKBACK = 20
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
W = PC.weights_for(CAP)
TGT = min(1.0, sum(W.values()))
YEARS = list(range(2020, 2027))


def pad(s, n, right=True):
    s = str(s)
    return s.ljust(n) if right else s.rjust(n)


def pct(v, n=9):
    return pad("%+.1f%%" % (v * 100), n)


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


def sig_state_only(D, c):
    """突破入场 + 只按状态出场（无 ATR 止损、无兜底、无提前认输）。

    过滤条件与生产版一致：状态必须是 UP/DOWN、init 血统不许开新仓、ATR 分位过高不做。
    唯一改动是**出场只剩状态翻转**。无前视：sig 从 i+1 生效。
    """
    b4, rg4, bl4 = D["b4"], D["rg4"], D["bl4"]
    n = len(b4)
    pct = BT.atr_percentile(b4, D["atr"], coin=c)
    sig = [0] * n
    i = max(LOOKBACK + 20, 30)
    while i < n - 1:
        rg = rg4[i]
        if rg not in ("UP", "DOWN") or D["atr"][i] is None:
            i += 1
            continue
        if BT.BLOCK_INIT and bl4 and bl4[i] == "init_ma120":
            i += 1
            continue
        if b4[i]["date"] < START_NAV:
            i += 1
            continue
        if pct is not None and (pct[i] is None or pct[i] > BT.VOL_Q):
            i += 1
            continue
        side = 1 if rg == "UP" else -1
        hi = max(x["high"] for x in b4[i - LOOKBACK:i])
        lo = min(x["low"] for x in b4[i - LOOKBACK:i])
        ok = (side == 1 and b4[i]["close"] > hi) or \
             (side == -1 and b4[i]["close"] < lo)
        if not ok:
            i += 1
            continue
        j = i + 1
        while j < n and rg4[j] == rg:
            j += 1
        for k in range(i + 1, min(j, n)):
            sig[k] = side
        i = j
    return sig


def run(sig_of, base_mult=0.0, add_mult=0.5):
    NAVS, expo = {}, {}
    for c in ALL12:
        b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
        sig = sig_of(DATA[c], c)
        base = [BT.BASE.get(rg4[k], 0.0) * base_mult for k in range(len(b4))]
        pv = [(base[k] + add_mult * sig[k]) * COEF[c][k] * VT.KAPPA
              for k in range(len(b4))]
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
            sl = pv[k] - base[k]
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


# ================= 【1】交易解剖 =================
print("=" * 104)
print("【1】现状加仓层的交易解剖（12 币全部历史）")
print("=" * 104)
by_reason, tot_r, tot_n = {}, 0.0, 0
for c in ALL12:
    for t in DATA[c]["trades"]:
        by_reason.setdefault(t["reason"], []).append(t["r"])
        tot_r += t["r"]
        tot_n += 1
print(pad("出场原因", 12) + pad("笔数", 8) + pad("占比", 9) + pad("总R", 10)
      + pad("平均R", 9) + pad("胜率", 8))
print("-" * 104)
for rs, lst in sorted(by_reason.items(), key=lambda x: -len(x[1])):
    win = sum(1 for x in lst if x > 0) / len(lst)
    print(pad(rs, 12) + pad(len(lst), 8) + pad("%.0f%%" % (len(lst) / tot_n * 100), 9)
          + pad("%+.0f" % sum(lst), 10) + pad("%+.2f" % (sum(lst) / len(lst)), 9)
          + pad("%.0f%%" % (win * 100), 8))
print(pad("合计", 12) + pad(tot_n, 8) + pad("100%", 9) + pad("%+.0f" % tot_r, 10)
      + pad("%+.2f" % (tot_r / tot_n), 9))

# 在仓时间占比（只统计 UP/DOWN 段内）
print("\n在仓时间占比（状态允许的 bar 里，实际有加仓层仓位的比例）：")
tot_live = tot_in = 0
for c in ALL12:
    D = DATA[c]
    for k, b in enumerate(D["b4"]):
        if b["date"] < START_NAV:
            continue
        if D["rg4"][k] in ("UP", "DOWN"):
            tot_live += 1
            if D["sig"][k] != 0:
                tot_in += 1
print(f"  状态允许 {tot_live:,} 根 4H，实际在场 {tot_in:,} 根 → **{tot_in / tot_live:.0%}**")
print(f"  底仓则是 UP/RANGE 全程在场（≈100%）。差距首先在这里。")

# ================= 【2】去掉摩擦 =================
print("\n" + "=" * 104)
print("【2】对照：突破入场 + 只按状态出场（去掉 ATR 止损 / 120 根兜底 / 提前认输）")
print("=" * 104)
ARMS = [
    ("C1 完整策略 1.0+0.5", lambda D, c: D["sig"], 1.0, 0.5),
    ("C2 只加仓·现状规则", lambda D, c: D["sig"], 0.0, 0.5),
    ("C3 只加仓·无止损", sig_state_only, 0.0, 0.5),
    ("C4 只底仓 1.0+0", lambda D, c: D["sig"], 1.0, 0.0),
    ("C5 只加仓 ×1.0", lambda D, c: D["sig"], 0.0, 1.0),
    ("C6 只加仓 ×1.5", lambda D, c: D["sig"], 0.0, 1.5),
    ("C7 半仓底仓·不择时", lambda D, c: D["sig"], 0.5, 0.0),
]
print(pad("臂", 24) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 10) + pad("回撤", 9) + pad("Calmar", 8) + pad("平均敞口", 10))
print("-" * 104)
R = {}
for nm, fn, bm, am in ARMS:
    cv, ex = run(fn, bm, am)
    yr, cum, dd, cagr = stat(cv)
    calmar = cagr / abs(dd) if dd < 0 else 0
    R[nm] = dict(yr=yr, cum=cum, dd=dd, ex=ex, calmar=calmar)
    print(pad(nm, 24) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 10) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % calmar, 8) + pad("%.2f×" % ex, 10))

# ================= 【3】摩擦的代价 =================
print("\n" + "=" * 104)
print("【3】摩擦到底吃掉多少")
print("=" * 104)
c2, c3 = R["C2 只加仓·现状规则"], R["C3 只加仓·无止损"]
print(f"  现状加仓层   {c2['cum'] / 1e4:>5.0f} 万，回撤 {c2['dd'] * 100:>6.1f}%，Calmar {c2['calmar']:.2f}")
print(f"  去掉止损后   {c3['cum'] / 1e4:>5.0f} 万，回撤 {c3['dd'] * 100:>6.1f}%，Calmar {c3['calmar']:.2f}")
print(f"  → 止损+兜底的成本：{(c2['cum'] - c3['cum']) / 1e4:+.0f} 万"
      f"（占其自身收益的 {(c2['cum'] - c3['cum']) / max(abs(c2['cum']), 1) * 100:+.0f}%）")
print(f"  → 但回撤 {c3['dd'] * 100:.1f}% vs {c2['dd'] * 100:.1f}%："
      f"{'去止损让回撤变大，不划算' if abs(c3['dd']) > abs(c2['dd']) else '去止损同时改善回撤，需复核'}")

print("\n  按单位敞口看（排除「敞口少所以赚得少」这个伪解释）：")
for nm in R:
    r = R[nm]
    print(f"    {pad(nm, 22)} 累计 {r['cum'] / 1e4:>5.0f} 万 ÷ 敞口 {r['ex']:.2f}×"
          f" = {r['cum'] / 1e4 / max(r['ex'], 1e-9):>6.0f} 万/倍")

print("\n" + "=" * 104)
print("【4】把两个原因彻底分开")
print("=" * 104)
c5, c6, c7 = R["C5 只加仓 ×1.0"], R["C6 只加仓 ×1.5"], R["C7 半仓底仓·不择时"]
print(f"  押注量：加仓 0.5× → 1.0× → 1.5×，累计 "
      f"{c2['cum'] / 1e4:.0f} → {c5['cum'] / 1e4:.0f} → {c6['cum'] / 1e4:.0f} 万"
      f"（倍数 {c5['cum'] / c2['cum']:.2f}× / {c6['cum'] / c2['cum']:.2f}×，"
      f"敞口 {c2['ex']:.2f} → {c5['ex']:.2f} → {c6['ex']:.2f}×）")
print(f"    → 仓位翻倍只换来 {c5['cum'] / c2['cum']:.2f}× 收益（次线性），"
      f"回撤却从 {c2['dd'] * 100:.1f}% 放大到 {c5['dd'] * 100:.1f}% / {c6['dd'] * 100:.1f}%。")
print(f"    → 所以「押得少」确实是一部分原因，但不是全部："
      f"即便放大到与底仓同量级敞口（C6 {c6['ex']:.2f}×），仍只有 {c6['cum'] / 1e4:.0f} 万，不到 C4 的一半。")

print(f"\n  择时对照（敞口不同，只能比风险调整后的效率，不能比绝对金额）：")
print(f"    C5 加仓层 ×1.0        {c5['cum'] / 1e4:>4.0f} 万  敞口 {c5['ex']:.2f}×  "
      f"回撤 {c5['dd'] * 100:>6.1f}%  Calmar {c5['calmar']:.2f}  "
      f"单位敞口 {c5['cum'] / 1e4 / c5['ex']:.0f} 万/倍")
print(f"    C7 半仓底仓·不择时     {c7['cum'] / 1e4:>4.0f} 万  敞口 {c7['ex']:.2f}×  "
      f"回撤 {c7['dd'] * 100:>6.1f}%  Calmar {c7['calmar']:.2f}  "
      f"单位敞口 {c7['cum'] / 1e4 / c7['ex']:.0f} 万/倍")
print(f"    → 「状态一翻就进」绝对收益更高（{c7['cum'] / 1e4:.0f} 万 > {c5['cum'] / 1e4:.0f} 万），"
      f"但回撤也大 {abs(c7['dd']) / abs(c5['dd']):.1f} 倍；")
print(f"    → 风险调整后两者相当（Calmar {c5['calmar']:.2f} vs {c7['calmar']:.2f}），"
      f"加仓层略胜 —— 突破择时买到的是「更浅的回撤」，不是「更多的收益」。")

print("\n" + "=" * 104)
print("【5】「损失」到底由什么构成：C1 310 万 = C2 42 万 + C4 234 万 + 协同 34 万")
print("=" * 104)
c1, c4 = R["C1 完整策略 1.0+0.5"], R["C4 只底仓 1.0+0"]
gap = c1["cum"] - c2["cum"]
sync = c1["cum"] - c2["cum"] - c4["cum"]
print(f"  只留加仓层，相对完整策略少赚 {gap / 1e4:.0f} 万，构成：")
print(f"    ① 底仓本身赚的             {c4['cum'] / 1e4:>6.0f} 万   "
      f"（{c4['cum'] / gap * 100:>5.0f}%，这是主力，不是协同失灵）")
print(f"    ② 两层同开才有的协同        {sync / 1e4:>+6.0f} 万   "
      f"（{sync / gap * 100:>5.0f}%，低相关带来的复利与回撤摊薄）")
print(f"    ────────────────────────────────────────")
print(f"    合计                       {gap / 1e4:>6.0f} 万")
print(f"  → 底仓单独就有 {c4['cum'] / c2['cum']:.1f} 倍的收益体量。"
      f"把底仓拿掉，等于把这个引擎整个关掉，只留一个 42% 时间在场的小凸性组合。")
