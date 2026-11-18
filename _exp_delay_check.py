#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_delay_check.py —— 刚上线的 MA50 闸门扛不扛得住执行延迟？

起因：_exp_risk2c.py 发现 ATR 吊灯「晚一根 4H 执行」就从 471 万掉到 217 万，
甚至低于基线。而 MA50 闸门同样是「价格穿越均线就立刻动底仓」，机制上同源。
闸门一年翻转 359 次（12 币），如果延迟同样吃掉全部收益，那 6c4fc16 的 +68 万
就是纸面数字，必须现在就知道。

四组对照（全部口径 A，本金恒 50 万）：
  ① 无闸门（老行为）                      ← 对照组，310 万
  ② 闸门，即时执行                        ← 生产现状，378 万
  ③ 闸门，晚 1 根 4H                      ← 人工延迟 4 小时的上界
  ④ 闸门，晚 2 根
  ⑤ 闸门 + ATR 吊灯，即时
  ⑥ 闸门 + ATR 吊灯，都晚 1 根

同时给出「闸门相对无闸门」的超额在延迟下还剩多少 —— 这才是判断闸门值不值得留的依据。
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
GATE = {c: BG.series(DATA[c]["b4"], DATA[c]["d1"], DATA[c]["rg4"]) for c in ALL12}
print("加载完成", flush=True)


def delay(s, n=1):
    return [s[0]] * n + s[:-n]


def trail_atr(D, k):
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    out, peak, st = [1.0] * len(b4), None, 1.0
    for i in range(len(b4)):
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


def build(scaler):
    NAVS = {}
    for c in ALL12:
        s = scaler(c)
        base = [BT.BASE.get(DATA[c]["rg4"][k], 0.0) * s[k] for k in range(len(s))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
        if c not in FIRST:
            FIRST[c] = min(NAVS[c])
    return NAVS


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
    return yr, cum, dd


ARMS = [
    ("① 无闸门（老行为）", lambda c: [1.0] * len(DATA[c]["b4"])),
    ("② 闸门 即时（生产）", lambda c: GATE[c]),
    ("③ 闸门 晚1根", lambda c: delay(GATE[c], 1)),
    ("④ 闸门 晚2根", lambda c: delay(GATE[c], 2)),
    ("⑤ 闸门+ATR 即时", lambda c: [a * b for a, b in zip(GATE[c], trail_atr(DATA[c], 1.5))]),
    ("⑥ 闸门+ATR 都晚1根", lambda c: [a * b for a, b in
                                     zip(delay(GATE[c], 1), delay(trail_atr(DATA[c], 1.5), 1))]),
]

print("\n" + "=" * 104)
print("口径 A：本金恒 50 万、每年提走。逐年收益 %")
print("=" * 104)
print(pad("臂", 24) + "".join(pad(y, 9) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9))
print("-" * 104)
R = {}
for nm, fn in ARMS:
    cv = curve(build(fn))
    yr, cum, dd = stat(cv)
    R[nm] = (cum, dd, yr, cv)
    print(pad(nm, 24) + "".join(pad("%+.1f" % (yr.get(y, 0.0) * 100), 9) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9))

print("\n" + "=" * 104)
print("判断：闸门相对「无闸门」的超额，在延迟下还剩多少")
print("=" * 104)
base0 = R["① 无闸门（老行为）"][0]
EX0 = R["② 闸门 即时（生产）"][0] - base0
for nm in ("② 闸门 即时（生产）", "③ 闸门 晚1根", "④ 闸门 晚2根"):
    cum = R[nm][0]
    ex = cum - base0
    keep = (ex / EX0 * 100) if EX0 else 0
    print(f"  {pad(nm, 22)} 累计 {cum / 1e4:>4.0f} 万   "
          f"超额 {ex / 1e4:>+5.0f} 万   相对即时执行保留 {keep:>4.0f}%")

print("\n" + "=" * 104)
print("逐年：闸门即时 vs 晚1根（差异出在哪年）")
print("=" * 104)
print(pad("年", 8) + pad("无闸门", 10) + pad("即时", 10) + pad("晚1根", 10)
      + pad("即时超额", 11) + pad("晚1根超额", 11) + pad("衰减", 9))
print("-" * 104)
for y in YEARS:
    a = R["① 无闸门（老行为）"][2].get(y, 0) * 100
    b = R["② 闸门 即时（生产）"][2].get(y, 0) * 100
    c_ = R["③ 闸门 晚1根"][2].get(y, 0) * 100
    print(pad(y, 8) + pad("%+.1f" % a, 10) + pad("%+.1f" % b, 10) + pad("%+.1f" % c_, 10)
          + pad("%+.1f" % (b - a), 11) + pad("%+.1f" % (c_ - a), 11)
          + pad("%.0f%%" % ((c_ - a) / (b - a) * 100 if abs(b - a) > 1e-9 else 0), 9))
