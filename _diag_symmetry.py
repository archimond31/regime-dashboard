#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_symmetry.py —— 底仓 vs 加仓层、多头 vs 空头，规则到底哪几处不对称

用户问的三件事：
  ① 底仓入场规则和加仓入场规则的差异
  ② 空头开仓规则与多头加仓是否对称
  ③ 空头开仓与「现货底仓加仓」是否对称

规则对照可以靠读代码，但「不对称到什么程度、代价多少」只能靠数据。
本脚本分五段：

  【1】多空加仓层的交易解剖对照（笔数/胜率/总R/持仓/出场原因/flip 去向）
  【2】敞口不对称：时间加权的多头敞口 vs 空头敞口，各状态占比与状态内收益
  【3】RANGE 段的裸奔：底仓在 RANGE 里没有加仓层、没有止损，代价多少
  【4】UP→RANGE 降级：加仓层被 flip 打掉、底仓原地不动，发生多少次
  【5】资金费方向：多头付、空头收，换算成对各自收益的贡献

全部口径与生产一致：4H 收盘、无前视、12 币、2020-09 起。
"""
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

START = "2020-09-01"
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
W = PC.weights_for(500_000)


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

# 收集成 (coin, trade)，后面每一段都要按币回查 rg4
BUCK = {"long": [], "short": []}
for c in ALL12:
    for t in DATA[c]["trades"]:
        BUCK["long" if t["side"] > 0 else "short"].append((c, t))
NL, NS = len(BUCK["long"]), len(BUCK["short"])

# ============================================================
# 【1】多空加仓层对照
# ============================================================
print("=" * 100)
print("【1】加仓层的多空对照（同一套规则，只是方向相反）")
print("=" * 100)
print(pad("方向", 8) + pad("笔数", 8) + pad("胜率", 8) + pad("总R", 10) + pad("平均R", 9)
      + pad("PF", 7) + pad("持仓根", 9) + pad("R/根", 9) + pad("笔数占比", 10))
print("-" * 100)
for k, lab in (("long", "多头"), ("short", "空头")):
    xs = [t for _c, t in BUCK[k]]
    rs = [t["r"] for t in xs]
    w = [x for x in rs if x > 0]
    l = [x for x in rs if x <= 0]
    pf = (sum(w) / abs(sum(l))) if l and sum(l) else float("inf")
    bars = sum(t["bars_held"] for t in xs) / len(xs)
    print(pad(lab, 8) + pad(len(xs), 8) + pad("%.1f%%" % (len(w) / len(xs) * 100), 8)
          + pad("%+.0f" % sum(rs), 10) + pad("%+.2f" % (sum(rs) / len(rs)), 9)
          + pad("%.2f" % pf, 7) + pad("%.0f" % bars, 9)
          + pad("%+.3f" % (sum(rs) / len(rs) / bars), 9)
          + pad("%.0f%%" % (len(xs) / (NL + NS) * 100), 10))

print("\n出场原因分布（笔数 + 该组总 R）：")
print(pad("方向", 8) + pad("stop", 18) + pad("flip", 18) + pad("timeout", 18)
      + pad("early", 12))
print("-" * 100)
for k, lab in (("long", "多头"), ("short", "空头")):
    xs = [t for _c, t in BUCK[k]]
    row = pad(lab, 8)
    for rsn in ("stop", "flip", "timeout", "early"):
        g = [t for t in xs if t["reason"] == rsn]
        row += pad(f"{len(g)} ({sum(t['r'] for t in g):+.0f}R)" if g else "0", 18)
    print(row)

print("\nflip 出场时，日线翻到了哪个状态（同一根 4H 的状态）：")
print(pad("方向", 8) + pad("→ RANGE", 18) + pad("→ 反向", 18) + pad("→ 同向", 14)
      + pad("合计", 10))
print("-" * 100)
for k, lab in (("long", "多头"), ("short", "空头")):
    to_rg = to_opp = to_same = 0
    for c, t in BUCK[k]:
        if t["reason"] != "flip":
            continue
        rg4 = DATA[c]["rg4"]
        st = rg4[t["j"]] if t["j"] < len(rg4) else None
        if st == "RANGE":
            to_rg += 1
        elif (st == "DOWN") if k == "long" else (st == "UP"):
            to_opp += 1
        else:
            to_same += 1
    print(pad(lab, 8) + pad(to_rg, 18) + pad(to_opp, 18) + pad(to_same, 14)
          + pad(to_rg + to_opp + to_same, 10))
print("  → 「→ RANGE」这一类，加仓层必须离场，但底仓 1.0 原封不动 —— 这是最容易被忽略的不对称")

# ============================================================
# 【2】敞口与在场时间
# ============================================================
print("\n" + "=" * 100)
print("【2】敞口与在场时间的不对称")
print("=" * 100)
days = {}
bars_by_state = {"UP": 0, "RANGE": 0, "DOWN": 0}
for c in ALL12:
    D = DATA[c]
    b4, rg4, sig = D["b4"], D["rg4"], D["sig"]
    for k in range(len(b4)):
        d = b4[k]["date"][:10]
        if d < START:
            continue
        s = rg4[k]
        # 预热期 rg4 可能是 None（数据不足以判定），不算进任何状态
        if s in bars_by_state:
            bars_by_state[s] += 1
        base = BT.BASE.get(s, 0.0)
        pv = (base + BT.ADD * sig[k]) * COEF[c][k] * VT.KAPPA * W.get(c, 0.08)
        e = days.setdefault(d, [0.0, 0.0])
        if pv > 0:
            e[0] += pv
        else:
            e[1] += -pv

tot_days = len(days)
long_on = sum(1 for v in days.values() if v[0] > 1e-9)
short_on = sum(1 for v in days.values() if v[1] > 1e-9)
avg_l = sum(v[0] for v in days.values()) / tot_days
avg_s = sum(v[1] for v in days.values()) / tot_days
print(f"  多头敞口：平均 {avg_l:.2f}×，{long_on / tot_days:.0%} 的日期在场")
print(f"  空头敞口：平均 {avg_s:.2f}×，{short_on / tot_days:.0%} 的日期在场")
print(f"  → 多头 / 空头 敞口比 = {avg_l / max(avg_s, 1e-9):.2f} 倍")

tb = sum(bars_by_state.values())
print("\n  状态占比（按 4H 根数，12 币合计）：")
for s in ("UP", "RANGE", "DOWN"):
    print(f"    {s:<6}{bars_by_state[s] / tb * 100:>6.1f}%")

st_ret = {"UP": [], "RANGE": [], "DOWN": []}
for c in ALL12:
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    for k in range(1, len(b4)):
        if b4[k]["date"][:10] < START:
            continue
        s = rg4[k]
        if s in st_ret:
            st_ret[s].append(b4[k]["close"] / b4[k - 1]["close"] - 1)
print("\n  各状态下单根 4H 的平均价格收益（等权、无杠杆，即裸吃 beta 的效率）：")
for s in ("UP", "RANGE", "DOWN"):
    xs = st_ret[s]
    mu = sum(xs) / len(xs)
    win = sum(1 for x in xs if x > 0) / len(xs)
    print(f"    {s:<6} 均值 {mu * 100:+.4f}%   胜率 {win:.1%}   样本 {len(xs):,}")

# ============================================================
# 【3】RANGE 段：多头有底仓、加仓层不许开
# ============================================================
print("\n" + "=" * 100)
print("【3】RANGE 段的不对称：多头底仓 1.0 在场、加仓层恒为 0、空头全空")
print("=" * 100)
segs = []
for c in ALL12:
    b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
    n = len(b4)
    k = 0
    while k < n:
        s = rg4[k]
        j = k
        while j < n and rg4[j] == s:
            j += 1
        if s == "RANGE" and b4[k]["date"][:10] >= START and j - k >= 6:
            segs.append({"c": c, "bars": j - k,
                         "ret": b4[j - 1]["close"] / b4[k]["close"] - 1,
                         "nxt": rg4[j] if j < n else None})
        k = j
print(f"  RANGE 段共 {len(segs)} 个（≥6 根 4H 才计），平均 "
      f"{sum(s['bars'] for s in segs) / len(segs):.0f} 根")
by_nxt = {}
for s in segs:
    by_nxt.setdefault(s["nxt"], []).append(s["ret"])
print("\n  按「接着翻到哪个状态」分组，看该 RANGE 段本身的涨跌：")
print(pad("接着翻到", 12) + pad("段数", 8) + pad("平均涨跌", 12) + pad("中位", 12)
      + pad("下跌占比", 10))
print("-" * 100)
for k2 in ("UP", "DOWN", "RANGE", None):
    xs = by_nxt.get(k2)
    if not xs:
        continue
    xs2 = sorted(xs)
    med = xs2[len(xs2) // 2]
    print(pad(str(k2) if k2 else "数据末尾", 12) + pad(len(xs), 8)
          + pad("%+.2f%%" % (sum(xs) / len(xs) * 100), 12)
          + pad("%+.2f%%" % (med * 100), 12)
          + pad("%.0f%%" % (sum(1 for x in xs if x < 0) / len(xs) * 100), 10))
dn = by_nxt.get("DOWN") or []
if dn:
    print(f"\n  → 紧接着翻 DOWN 的 RANGE 段 {len(dn)} 个，底仓在这段里平均 "
          f"{sum(dn) / len(dn) * 100:+.2f}%（无止损、无加仓、无对冲）")

# ============================================================
# 【4】UP→RANGE 降级：加仓层出、底仓留
# ============================================================
print("\n" + "=" * 100)
print("【4】UP → RANGE 降级：加仓层被 flip 打掉，底仓原地不动")
print("=" * 100)
cnt_drop = 0
after = []
for c in ALL12:
    D = DATA[c]
    b4, rg4 = D["b4"], D["rg4"]
    n = len(b4)
    for t in D["trades"]:
        j = t["j"]
        if t["reason"] != "flip" or t["side"] != 1 or j >= n:
            continue
        if rg4[j] != "RANGE":
            continue
        cnt_drop += 1
        if b4[j]["date"][:10] < START:
            continue
        k = j
        while k < n and rg4[k] == "RANGE":
            k += 1
        after.append(b4[min(k, n - 1)]["close"] / b4[j]["close"] - 1)
print(f"  加仓层因「UP→RANGE」被 flip 平掉的次数：{cnt_drop}")
if after:
    neg = [x for x in after if x < 0]
    print(f"  其中 {len(after)} 笔发生在统计期内，之后 RANGE 继续走到终点时，")
    print(f"  底仓在剩余 RANGE 里平均 {sum(after) / len(after) * 100:+.2f}%"
          f"（中位 {sorted(after)[len(after) // 2] * 100:+.2f}%），"
          f"下跌的占 {len(neg) / len(after):.0%}")
print("  → 规则事实：flip 对加仓层是「立刻出场」，对底仓是「无事发生」（UP/RANGE 同为 1.0）")

# ============================================================
# 【5】资金费方向
# ============================================================
print("\n" + "=" * 100)
print("【5】资金费的多空不对称（多头付、空头收；假设 funding 恒为 +3bp/日）")
print("=" * 100)
lb = sum(t["bars_held"] for _c, t in BUCK["long"])
sb = sum(t["bars_held"] for _c, t in BUCK["short"])
# 累计根数是「12 币 × 6.2 年」的合计，直接看会严重高估。
# 换算成「单币年化」才有意义：该币平均每年有百分之多少的时间在场 × 3bp/日 × 365。
YRS = 6.22                      # 2020-09-01 → 2026-11-18
occ_l = lb / 6 / len(ALL12) / (365 * YRS)
occ_s = sb / 6 / len(ALL12) / (365 * YRS)
print(f"  多头加仓层：累计 {lb:,} 根 4H → 单币平均在场 {occ_l:.1%} 的时间")
print(f"  空头加仓层：累计 {sb:,} 根 4H → 单币平均在场 {occ_s:.1%} 的时间")
print(f"  换算成年化（相对该层的名义敞口，3bp/日 × 365 = {BT.FUND_DAILY * 365 * 100:.1f}%/年满仓）：")
print(f"    多头付 {occ_l * BT.FUND_DAILY * 365 * 100:>5.2f}%/年")
print(f"    空头收 {occ_s * BT.FUND_DAILY * 365 * 100:>5.2f}%/年")
print(f"  → 方向不对称是事实，但量级只有 1~2%/年，"
      f"远小于 1R（=1.5ATR，平均 4~6% 名义）的量级")
print("  → 底仓 1.0× 走现货，不吃资金费，所以多头的 funding 负担只在加仓层")
print("\n  注意：funding 并非恒正。熊市里 BTC 永续 funding 常转负，那时空头要付钱。")
print("  本段按恒正估算，是空头的最有利假设 —— 真实结果只会比这差。")
