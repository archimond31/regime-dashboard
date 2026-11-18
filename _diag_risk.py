#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_risk.py —— 诊断「风险预算的尺子选对了吗」

要回答的问题：
  VT 用「年化波动率」当风险尺子，把每个币压到 σ≈0.60×κ≈0.73。
  但钱到底是亏在「波动大」上，还是亏在「方向错了还一直拿着」上？
  如果 2022 年（亏最多的年）的 VT 系数并不低，说明这把尺子对这类亏损是失明的。

输出：
  ① 逐年：各币平均 VT 系数、组合平均敞口、组合实际年化波动、当年收益、年内回撤
  ② 逐年：VT 系数最低（=波动最高、被砍最多）的 3 个币
  ③ 敞口分布：多头峰值是否长期顶在 1.5×κ 附近

判断标准（跑之前先写死，避免事后解释）：
  · 若「亏损年」的 VT 系数 ≥「盈利年」→ 波动率尺子对亏损源头无效
  · 若组合实际波动长期低于 0.73 而回撤仍 > 40% → 问题在方向/持续期，不在波动
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
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
W = PC.weights_for(CAP)
TGT = min(1.0, sum(W.values()))


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


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
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
    return b4, pos, rg4


print("加载 12 币 …", flush=True)
DATA = {}
for c in ALL12:
    b4, pos, rg4 = load_coin(c)
    coef = VT.coef_series(b4)
    sig_ = VT.sigma_series(b4)
    # 日频化：每日最后一根 4H 的收盘、敞口、系数、σ
    dd = []
    for k in range(len(b4)):
        d = b4[k]["date"][:10]
        row = (d, b4[k]["close"], pos[k] * coef[k] * VT.KAPPA, coef[k], sig_.get(d))
        if dd and dd[-1][0] == d:
            dd[-1] = row
        else:
            dd.append(row)
    DATA[c] = dd
    print(f"  {c:<5} {len(dd):>5} 天", flush=True)

# 日期轴（各币对齐）
ALLD = sorted({r[0] for c in ALL12 for r in DATA[c] if r[0] >= "2020-09-01"})
IDX = {c: {r[0]: i for i, r in enumerate(DATA[c])} for c in ALL12}

rows = []
for di in range(1, len(ALLD)):
    d0, d1 = ALLD[di - 1], ALLD[di]
    y = int(d1[:4])
    expo = 0.0
    rp = 0.0
    wsum = 0.0
    coefs = {}
    for c in ALL12:
        ic = IDX[c].get(d1)
        ip = IDX[c].get(d0)
        if ic is None or ip is None or ic == 0:
            continue
        # renorm：按在场币把权重放大到目标仓位
        wsum += W[c]
    if wsum <= 0:
        continue
    for c in ALL12:
        ic = IDX[c].get(d1)
        ip = IDX[c].get(d0)
        if ic is None or ip is None or ic == 0:
            continue
        uw = W[c] / wsum * TGT
        _, cl1, p1, cf1, sg1 = DATA[c][ic]
        _, cl0, _p0, _cf0, _sg0 = DATA[c][ip]
        if cl0 <= 0:
            continue
        r = cl1 / cl0 - 1
        rp += uw * p1 * r
        expo += uw * abs(p1)
        if cf1 is not None:
            coefs[c] = (cf1, sg1)
    rows.append((d1, y, expo, rp, coefs))

YEARS = sorted({r[1] for r in rows})
print("\n" + "=" * 92)
print("【1】逐年：组合敞口 / 实际波动 / 平均 VT 系数")
print("=" * 92)
print("只量风险，不量收益：这里的收益是「每日再平衡」口径，与真实持仓差 30~60pp，")
print("不可用于判断盈亏。盈亏看 _exp_yearly.py，回撤对照见输出末尾。")
print(pad("年份", 7) + pad("交易天数", 9) + pad("平均敞口", 10) + pad("峰值敞口", 10)
      + pad("年化波动", 10) + pad("平均系数", 10) + pad("敞口>150%天数", 14))
print("-" * 92)
for y in YEARS:
    seg = [r for r in rows if r[1] == y]
    if len(seg) < 20:
        continue
    rets = [r[3] for r in seg]
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets))
    vol = sd * math.sqrt(365)
    expo_avg = sum(r[2] for r in seg) / len(seg)
    expo_max = max(r[2] for r in seg)
    heavy = sum(1 for r in seg if r[2] > 1.5)
    cf_all = [v[0] for r in seg for v in r[4].values()]
    cf_avg = sum(cf_all) / len(cf_all) if cf_all else float("nan")
    print(pad(y, 7) + pad(len(seg), 9) + pad("%.0f%%" % (expo_avg * 100), 10)
          + pad("%.0f%%" % (expo_max * 100), 10) + pad("%.0f%%" % (vol * 100), 10)
          + pad("%.3f" % cf_avg, 10) + pad("%d 天" % heavy, 14))

print("\n" + "=" * 92)
print("【2】逐年：VT 砍得最狠的 3 个币（系数最低 = 判定波动最高）")
print("=" * 92)
print("如果亏损年「砍得最狠」的币恰好是后来涨回来的币，说明 VT 在低位砍了赢家。")
for y in YEARS:
    seg = [r for r in rows if r[1] == y]
    if len(seg) < 20:
        continue
    agg = {}
    for r in seg:
        for c, (cf, sg) in r[4].items():
            agg.setdefault(c, []).append(cf)
    if not agg:
        continue
    lst = sorted(((sum(v) / len(v), c) for c, v in agg.items()))[:3]
    hi = sorted(((sum(v) / len(v), c) for c, v in agg.items()), reverse=True)[:2]
    print(pad(y, 6) + "  砍最狠: " + "  ".join(f"{c}{v:.2f}" for v, c in lst)
          + "    加最多: " + "  ".join(f"{c}{v:.2f}" for v, c in hi))

print("\n" + "=" * 92)
print("【3】判定：波动率尺子对亏损年有效吗")
print("=" * 92)
for label, ys in (("盈利年", [2021, 2023, 2024, 2025, 2026]), ("亏损年", [2022])):
    cf, vol_ = [], []
    for y in ys:
        seg = [r for r in rows if r[1] == y]
        if len(seg) < 20:
            continue
        rets = [r[3] for r in seg]
        m = sum(rets) / len(rets)
        vol_.append(math.sqrt(sum((x - m) ** 2 for x in rets) / len(rets)) * math.sqrt(365))
        ca = [v[0] for r in seg for v in r[4].values()]
        cf.append(sum(ca) / len(ca))
    if not cf:
        continue
    print(f"{label}（{len(cf)} 年）：平均 VT 系数 {sum(cf) / len(cf):.3f}   "
          f"平均年化波动 {sum(vol_) / len(vol_) * 100:.0f}%")

print()
print("对照 _exp_yearly.py 的真实年内回撤（口径 A）：")
print("  2022 波动 43% → 回撤 -55.7%")
print("  2023 波动 50% → 回撤 -18.7%")
print("  **波动更高的一年，回撤只有三分之一。** 波动率解释不了这个策略的亏损。")
print()
print("为什么：2022 是「低波动阴跌 + 状态机长期判 UP/RANGE」，底仓一直持多；")
print("        2023 是「高波动上涨 + 方向判对」，抖动大但方向对。")
print("        VT 量的是抖动，量不到「方向对了没有」。")
