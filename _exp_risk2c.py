#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_risk2c.py —— ATR 吊灯的三道过关检验

_exp_risk2b.py 的结论：它不是「变相降仓」—— 同样平均底仓 0.55，恒定折价只有 204 万，
ATR 择时有 548 万（超额 +344 万）。但有两个红字：
  · k 单调到 0.25 都不回头（0.25 与 0.5 并列 548 万）→ 边界解，没有最优点
  · 减半后强制保持 6 根，512 万 → 377 万；保持 18 根 → 292 万
    收益几乎全部来自「立刻恢复」，这是高频剥头皮的特征，不是风险预算

所以必须回答：OOS 还在不在？换币还在不在？晚一根执行还剩多少？

  【1】时间外样本：2018-2021 定方向，2022-2026 验证（不重新选参，只看排序是否稳定）
  【2】币种外样本：只对 BTC+ETH 开规则 vs 只对其余 10 币开规则，谁的贡献大
  【3】延迟一根 4H 执行：信号与成交错开一根，还剩多少（这是实盘唯一躲不掉的摩擦）
  【4】尺子本身重要吗：ATR 吊灯 vs「固定百分比从峰值回撤」，若等价则 ATR 无特殊价值
  【5】翻转的分布：多少币、多频繁、平均持有多久 —— 判断能不能手动执行
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
    sig, _ = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig, atr=atr)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
BASE_G = {c: BT.base_series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}
print("加载完成", flush=True)


def trail_atr(D, k):
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    n = len(b4)
    out = [1.0] * n
    peak = None
    st = 1.0
    for i in range(n):
        px = b4[i]["close"]
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            peak, st = None, 1.0
            continue
        if peak is None:
            peak, st = px, 1.0
        peak = max(peak, px)
        a = atr[i] or px * 0.02
        if px < peak - k * a:
            st = 0.5
        elif st < 1.0 and px > peak - 0.5 * k * a:
            st = 1.0
        out[i] = st
    return out


def trail_pct(D, p):
    b4, rg4 = D["b4"], D["rg4"]
    n = len(b4)
    out = [1.0] * n
    peak = None
    st = 1.0
    for i in range(n):
        px = b4[i]["close"]
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            peak, st = None, 1.0
            continue
        if peak is None:
            peak, st = px, 1.0
        peak = max(peak, px)
        if px < peak * (1 - p):
            st = 0.5
        elif st < 1.0 and px > peak * (1 - 0.5 * p):
            st = 1.0
        out[i] = st
    return out


def delay(s, n=1):
    return [s[0]] * n + s[:-n]


def get_nav(c, w, base):
    b4, sig = DATA[c]["b4"], DATA[c]["sig"]
    pv = [(base[k] + BT.ADD * sig[k]) * COEF[c][k] * VT.KAPPA for k in range(len(b4))]
    qv = PC.CAP_4H_WORST.get(c) or 1e12
    res, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
    for k in range(k0 + 1, len(b4)):
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
    return res


FIRST = {}


def build(scaler=None, only=None):
    NAVS = {}
    for c in ALL12:
        s = [1.0] * len(DATA[c]["b4"]) if (scaler is None or (only and c not in only)) \
            else scaler(c)
        base = [BASE_G[c][k] * s[k] for k in range(len(s))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
        if c not in FIRST:
            FIRST[c] = min(NAVS[c])
    return NAVS


def curve(NAVS, start=None, end=None):
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    for d in [x for x in ALLD if (start is None or x >= start) and (end is None or x < end)]:
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


def cum_of(cv):
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    return sum(CAP * (lastv[y] / first[y] - 1) for y in YEARS if y in first)


def ret_of(cv):
    return cv[-1][1] / cv[0][1] - 1 if len(cv) > 1 else 0.0


def dd_of(cv):
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return dd


NAV0 = build()
CV0 = curve(NAV0)
CUM0 = cum_of(CV0)
print(f"基线：{CUM0 / 1e4:.0f} 万，回撤 {dd_of(CV0) * 100:.1f}%\n")

# ============ 【1】时间外样本 ============
print("=" * 96)
print("【1】时间外样本：前半段（2018~2021）与后半段（2022~2026）分开算")
print("     若「k 越小越好」在两段都成立，方向才是稳的；只有一段成立就是拟合")
print("=" * 96)
print(pad("k", 8) + pad("前半段收益", 13) + pad("vs基线", 11)
      + pad("后半段收益", 13) + pad("vs基线", 11) + pad("全段累计", 11))
print("-" * 96)
CVA_H1 = curve(NAV0, None, "2022-01-01")
CVA_H2 = curve(NAV0, "2022-01-01", None)
print(pad("基线", 8) + pad("%+.1f%%" % (ret_of(CVA_H1) * 100), 13) + pad("—", 11)
      + pad("%+.1f%%" % (ret_of(CVA_H2) * 100), 13) + pad("—", 11)
      + pad("%.0f 万" % (CUM0 / 1e4), 11))
for k in (0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
    NAV = build(lambda c, kk=k: trail_atr(DATA[c], kk))
    cv1 = curve(NAV, None, "2022-01-01")
    cv2 = curve(NAV, "2022-01-01", None)
    print(pad(k, 8) + pad("%+.1f%%" % (ret_of(cv1) * 100), 13)
          + pad("%+.1fpp" % ((ret_of(cv1) - ret_of(CVA_H1)) * 100), 11)
          + pad("%+.1f%%" % (ret_of(cv2) * 100), 13)
          + pad("%+.1fpp" % ((ret_of(cv2) - ret_of(CVA_H2)) * 100), 11)
          + pad("%.0f 万" % (cum_of(curve(NAV)) / 1e4), 11))

# ============ 【2】币种外样本 ============
print("\n" + "=" * 96)
print("【2】币种外样本：规则只作用在 BTC+ETH vs 只作用在其余 10 币")
print("=" * 96)
print(pad("作用范围", 26) + pad("累计", 11) + pad("vs基线", 11) + pad("回撤", 9))
print("-" * 96)
for nm, only in (("只 BTC+ETH", ("BTC", "ETH")),
                 ("只其余 10 币", tuple(c for c in ALL12 if c not in ("BTC", "ETH")))):
    NAV = build(lambda c: trail_atr(DATA[c], 1.5), only=only)
    cv = curve(NAV)
    print(pad(nm, 26) + pad("%.0f 万" % (cum_of(cv) / 1e4), 11)
          + pad("%+.0f 万" % ((cum_of(cv) - CUM0) / 1e4), 11)
          + pad("%.1f%%" % (dd_of(cv) * 100), 9))
print("  （BTC+ETH 合计权重 %.0f%%，其余 10 币 %.0f%%）"
      % (sum(W[c] for c in ("BTC", "ETH")) * 100,
         sum(W[c] for c in ALL12 if c not in ("BTC", "ETH")) * 100))

# 逐币贡献
print("\n  逐币：只在这一个币上开规则（k=1.5）")
print("  " + pad("币", 7) + pad("权重", 8) + pad("累计差", 11) + pad("回撤", 9)
      + pad("币自身收益差", 13))
for c in ALL12:
    NAV = build(lambda cc, c0=c: trail_atr(DATA[cc], 1.5) if cc == c0
                else [1.0] * len(DATA[cc]["b4"]))
    cv = curve(NAV)
    s = trail_atr(DATA[c], 1.5)
    nf = sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    print("  " + pad(c, 7) + pad("%.1f%%" % (W[c] * 100), 8)
          + pad("%+.0f 万" % ((cum_of(cv) - CUM0) / 1e4), 11)
          + pad("%.1f%%" % (dd_of(cv) * 100), 9) + pad(f"{nf} 次翻转", 13))

# ============ 【3】延迟一根 ============
print("\n" + "=" * 96)
print("【3】延迟一根 4H 执行（信号出来后下一根才成交）—— 实盘躲不掉的摩擦")
print("=" * 96)
print(pad("k", 8) + pad("即时累计", 12) + pad("晚1根", 12) + pad("损失", 11)
      + pad("晚2根", 12) + pad("损失", 11))
print("-" * 96)
for k in (0.5, 1.0, 1.5, 2.0):
    NAV_a = build(lambda c, kk=k: trail_atr(DATA[c], kk))
    NAV_1 = build(lambda c, kk=k: delay(trail_atr(DATA[c], kk), 1))
    NAV_2 = build(lambda c, kk=k: delay(trail_atr(DATA[c], kk), 2))
    ca, c1, c2 = cum_of(curve(NAV_a)), cum_of(curve(NAV_1)), cum_of(curve(NAV_2))
    print(pad(k, 8) + pad("%.0f 万" % (ca / 1e4), 12) + pad("%.0f 万" % (c1 / 1e4), 12)
          + pad("%+.0f 万" % ((c1 - ca) / 1e4), 11)
          + pad("%.0f 万" % (c2 / 1e4), 12) + pad("%+.0f 万" % ((c2 - ca) / 1e4), 11))

# ============ 【4】尺子本身重要吗 ============
print("\n" + "=" * 96)
print("【4】ATR 自适应尺子 vs 固定百分比尺子（同样从峰值回撤就减半）")
print("     ATR(14) 在 4H 上中位约为价格的 1.x%，所以 p≈1.5~2% 是可比的量级")
print("=" * 96)
print(pad("尺子", 22) + pad("平均底仓", 11) + pad("累计", 11) + pad("vs基线", 11)
      + pad("回撤", 9) + pad("翻转/年", 10))
print("-" * 96)
for p in (0.01, 0.015, 0.02, 0.03, 0.05):
    NAV = build(lambda c, pp=p: trail_pct(DATA[c], pp))
    cv = curve(NAV)
    nf = 0
    for c in ALL12:
        s = trail_pct(DATA[c], p)
        nf += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    print(pad("固定 %.1f%%" % (p * 100), 22) + pad("—", 11)
          + pad("%.0f 万" % (cum_of(cv) / 1e4), 11)
          + pad("%+.0f 万" % ((cum_of(cv) - CUM0) / 1e4), 11)
          + pad("%.1f%%" % (dd_of(cv) * 100), 9) + pad("%.0f" % (nf / 6.2), 10))
for k in (1.0, 1.5, 2.0):
    NAV = build(lambda c, kk=k: trail_atr(DATA[c], kk))
    cv = curve(NAV)
    nf = 0
    for c in ALL12:
        s = trail_atr(DATA[c], k)
        nf += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    print(pad("ATR×%.1f" % k, 22) + pad("—", 11)
          + pad("%.0f 万" % (cum_of(cv) / 1e4), 11)
          + pad("%+.0f 万" % ((cum_of(cv) - CUM0) / 1e4), 11)
          + pad("%.1f%%" % (dd_of(cv) * 100), 9) + pad("%.0f" % (nf / 6.2), 10))

# ============ 【5】翻转分布 ============
print("\n" + "=" * 96)
print("【5】能不能手动执行：每个币一年翻几次、平均持有多久")
print("=" * 96)
print("  " + pad("币", 7) + pad("翻转次数", 10) + pad("次/年", 9)
      + pad("打折 bar 占比", 14) + pad("平均 1 段持续(根)", 18))
tot = 0
for c in ALL12:
    s = trail_atr(DATA[c], 1.5)
    rg4, b4 = DATA[c]["rg4"], DATA[c]["b4"]
    nf = sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    live = [i for i in range(len(s)) if BT.BASE.get(rg4[i], 0) > 0
            and b4[i]["date"] >= START_NAV]
    half = sum(1 for i in live if s[i] < 1.0)
    yrs = max(1e-9, (len(live) / 6.0) / 365.25)
    tot += nf
    print("  " + pad(c, 7) + pad(nf, 10) + pad("%.0f" % (nf / yrs), 9)
          + pad("%.0f%%" % (half / len(live) * 100 if live else 0), 14)
          + pad("%.1f" % (len(live) / max(1, nf) * 2), 18))
print(f"\n  12 币合计 {tot} 次翻转 / 6.2 年 = {tot / 6.2:.0f} 次/年 "
      f"= 每天 {tot / 6.2 / 365:.1f} 次")
