#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_risk2b.py —— P0-3 的决定性检验：ATR 止损到底是「择时」还是「变相降仓」

_exp_risk2.py 的结果有两处可疑：
  ① trail k 单调：k=1.0 → 512 万、k=1.5 → 471 万、k=2.0 → 444 万、k=3.0 → 396 万。
     「越小越好」意味着没有最优点，只有方向 —— 和底仓闸门那次一样的味道。
  ② 换手 660 次/年（12 币合计，≈1.8 次/天）。对一套手动执行的低频策略，这不是小数。
这两点合起来指向一个更朴素的解释：**它只是把底仓平均权重降低了**。
底仓前面已经被证明接近净负 alpha（参数网格单调 + 闸门有效），那么「少持有」自然就变好，
根本不需要 ATR 这个尺子。

本脚本做正面对决：
  【A】恒定折价：base = BASE×闸门×c（c 固定，无任何信号）—— 0 次换手
  【B】ATR 吊灯减半：base = BASE×闸门×risk(k)
  然后按**平均底仓系数**对齐比较：同样的平均敞口，谁的收益/回撤更好？
  只有 B 在同等平均敞口下明显占优，才说明 ATR 带来了真信息。

  【C】k 细化网格，确认单调性是否延续到 k<1（若延续 → 过拟合信号，不是规律）
  【D】换手抑制：减半后强制保持 N 根，能否在不损失收益的前提下把 660 次/年压下来
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import base_gate as BG
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


def pct(v, n=9, sign=True):
    return pad(("%+.1f%%" % (v * 100)) if sign else ("%.1f%%" % (v * 100)), n)


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


def trail(D, k, hold=0):
    """峰值 - k×ATR 减半；减半后至少保持 hold 根才允许恢复。"""
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    n = len(b4)
    out = [1.0] * n
    peak = None
    armed = 0                    # >0 = 已减半，剩余强制保持根数
    for i in range(n):
        px = b4[i]["close"]
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            peak = None
            armed = 0
            continue
        if peak is None:
            peak = px
        peak = max(peak, px)
        a = atr[i] or px * 0.02
        if armed > 0:
            armed -= 1
            out[i] = 0.5
            continue
        if px < peak - k * a:
            armed = max(hold, 0)
            out[i] = 0.5
            if hold:
                armed = hold - 1
        elif out[i - 1] < 1.0 and px > peak - 0.5 * k * a:
            out[i] = 1.0
        else:
            out[i] = out[i - 1]
    return out


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
AVG = {}


def build(scaler):
    """scaler(c) -> 与 b4 等长的系数序列（乘在 BASE_G 上）。"""
    NAVS = {}
    acc = [0.0, 0]
    for c in ALL12:
        s = scaler(c)
        base = [BASE_G[c][k] * s[k] for k in range(len(s))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
        if c not in FIRST:
            FIRST[c] = min(NAVS[c])
        # 平均底仓系数：只在「底仓本应在场」的 bar 上平均
        b4, rg4 = DATA[c]["b4"], DATA[c]["rg4"]
        for k in range(len(b4)):
            if b4[k]["date"] >= START_NAV and BT.BASE.get(rg4[k], 0.0) > 0:
                acc[0] += s[k]
                acc[1] += 1
    return NAVS, (acc[0] / acc[1] if acc[1] else 1.0)


def curve(NAVS):
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


def metrics(cv):
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
    return yr, cum, dd, cv[-1][1] / cv[0][1] - 1


def rolling(cv, base_cv):
    DATES = [d for d, _ in base_cv]
    wins = loss = 0
    for i in range(0, len(DATES) - 250 + 1, 125):
        s, e = DATES[i], DATES[i + 250]
        cb = [x for x in base_cv if s <= x[0] < e]
        cr = [x for x in cv if s <= x[0] < e]
        if len(cb) < 200:
            continue
        d = ((cr[-1][1] / cr[0][1]) - (cb[-1][1] / cb[0][1])) * 100
        if d > 0.5:
            wins += 1
        elif d < -0.5:
            loss += 1
    return wins, loss


# 基线 = 生产配置
NAV0, _ = build(lambda c: [1.0] * len(DATA[c]["b4"]))
CV0 = curve(NAV0)
_, CUM0, DD0, _ = metrics(CV0)
print(f"基线（MA50 闸门，无新规则）：{CUM0 / 1e4:.0f} 万，回撤 {DD0 * 100:.1f}%\n")

# ================= 【A】恒定折价 =================
print("=" * 100)
print("【A】恒定折价反事实：不放任何信号，只是把底仓长期按住 c（0 次换手）")
print("=" * 100)
print(pad("c", 8) + pad("平均底仓", 11) + pad("累计", 11) + pad("vs基线", 11)
      + pad("回撤", 9) + pad("倍数", 10) + pad("滚动胜/负", 12))
print("-" * 100)
CA = {}
for c_ in (1.0, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.5):
    NAV, avg = build(lambda c, cc=c_: [cc] * len(DATA[c]["b4"]))
    cv = curve(NAV)
    yr, cum, dd, mult = metrics(cv)
    w_, l_ = rolling(cv, CV0) if c_ != 1.0 else (0, 0)
    CA[c_] = (cum, dd, avg)
    print(pad(c_, 8) + pad("%.3f" % avg, 11) + pad("%.0f 万" % (cum / 1e4), 11)
          + pad("%+.0f 万" % ((cum - CUM0) / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f×" % mult, 10) + pad(f"{w_}/{l_}" if c_ != 1.0 else "—", 12))

# ================= 【B】ATR 吊灯 =================
print("\n" + "=" * 100)
print("【B】ATR 吊灯减半（含 k 细化，看单调性是否延续到 k<1）")
print("=" * 100)
print(pad("k", 8) + pad("平均底仓", 11) + pad("累计", 11) + pad("vs基线", 11)
      + pad("回撤", 9) + pad("倍数", 10) + pad("滚动胜/负", 12) + pad("翻转/年", 10))
print("-" * 100)
CB = {}
for k in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0):
    NAV, avg = build(lambda c, kk=k: trail(DATA[c], kk))
    cv = curve(NAV)
    yr, cum, dd, mult = metrics(cv)
    w_, l_ = rolling(cv, CV0)
    nf = 0
    for c in ALL12:
        s = trail(DATA[c], k)
        nf += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    CB[k] = (cum, dd, avg, nf)
    print(pad(k, 8) + pad("%.3f" % avg, 11) + pad("%.0f 万" % (cum / 1e4), 11)
          + pad("%+.0f 万" % ((cum - CUM0) / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f×" % mult, 10) + pad(f"{w_}/{l_}", 12) + pad("%.0f" % (nf / 6.2), 10))

# ================= 【C】正面对决 =================
print("\n" + "=" * 100)
print("【C】正面对决：同样的平均底仓，恒定折价 vs ATR 择时")
print("     读法：ATR 那一行的「平均底仓」落在哪两个 c 之间，就在那两个 c 之间插值")
print("=" * 100)
print(pad("方法", 18) + pad("平均底仓", 11) + pad("累计", 10) + pad("回撤", 9)
      + pad("同敞口下应得", 13) + pad("超额", 10))
print("-" * 100)
for k, (cum, dd, avg, nf) in CB.items():
    ks = sorted(CA)
    lo = max([c for c in ks if CA[c][2] >= avg], default=ks[0])
    hi = min([c for c in ks if CA[c][2] <= avg], default=ks[-1])
    if hi == lo or CA[lo][2] == CA[hi][2]:
        exp = CA[lo][0]
    else:
        t = (avg - CA[lo][2]) / (CA[hi][2] - CA[lo][2])
        exp = CA[lo][0] + t * (CA[hi][0] - CA[lo][0])
    print(pad(f"trail k={k}", 18) + pad("%.3f" % avg, 11) + pad("%.0f 万" % (cum / 1e4), 10)
          + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.0f 万（c≈%.2f）" % (exp / 1e4,
                                      lo if abs(exp - CA[lo][0]) < abs(exp - CA[hi][0]) else hi), 13)
          + pad("%+.0f 万" % ((cum - exp) / 1e4), 10))

# ================= 【D】换手抑制 =================
print("\n" + "=" * 100)
print("【D】换手抑制：减半后强制保持 N 根 4H 才允许恢复（N=6 即 1 天）")
print("=" * 100)
print(pad("k", 8) + pad("保持N", 8) + pad("平均底仓", 11) + pad("累计", 11)
      + pad("回撤", 9) + pad("滚动胜/负", 12) + pad("翻转/年", 10))
print("-" * 100)
for k in (1.0, 1.5):
    for hold in (0, 6, 18, 42):
        NAV, avg = build(lambda c, kk=k, hh=hold: trail(DATA[c], kk, hh))
        cv = curve(NAV)
        yr, cum, dd, mult = metrics(cv)
        w_, l_ = rolling(cv, CV0)
        nf = 0
        for c in ALL12:
            s = trail(DATA[c], k, hold)
            nf += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
        print(pad(k, 8) + pad(hold, 8) + pad("%.3f" % avg, 11)
              + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
              + pad(f"{w_}/{l_}", 12) + pad("%.0f" % (nf / 6.2), 10))
