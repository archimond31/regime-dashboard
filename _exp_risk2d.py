#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_risk2d.py —— 最后一条路：把风控做成「一次性事件」而不是「连续跟随」

已知：
  · MA 闸门（连续跟随）/ ATR 吊灯（连续跟随）都是即时执行才有效，晚 1 根归零
  · 加宽滞回、放慢均线都救不回来（_exp_gate_delay.py，15 个组合全不过）

为什么「连续跟随」这么脆：它每 4H 都可能动，晚了这一次，下一次还是晚；
误差逐次累加，而且错过的正是「跌得最快那一根」。开仓类规则不脆，是因为它只动一次，
且有 24h 的 TTL 窗口（_diag_delay.py 的结论：开仓延迟 1 根约 -7.8%）。

所以最后一招：**把风控改成一次性事件** ——
  底仓从峰值（或入场价）回撤超 k×ATR → 减半/清零，然后**锁死**，
  直到日线状态翻转才重置。全程只动一次，延迟只影响那一次。

三条臂 × 三种延迟：
  T1 回撤超 k×ATR → 减半，锁死到状态翻转
  T2 回撤超 k×ATR → 清零，锁死到状态翻转
  T3 回撤超 k×ATR → 减半，锁死 30 天（6 次在这里等价于半个纪律，用于折中）

及格线（写死）：晚 1 根仍 ≥ 310 万（不开闸门）且回撤优于 -60.7%。
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
print("加载完成", flush=True)


def once(D, k, cv, hold_bars=None):
    """回撤超 k×ATR → 置 cv 并锁死；hold_bars=None 表示锁到状态翻转。"""
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    out, peak, dead, left = [1.0] * len(b4), None, False, 0
    for i in range(len(b4)):
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            peak, dead, left = None, False, 0
            continue
        px = b4[i]["close"]
        if peak is None:
            peak = px
        peak = max(peak, px)
        a = atr[i] or px * 0.02
        if not dead and px < peak - k * a:
            dead = True
            left = hold_bars if hold_bars else 0
        if dead and hold_bars:
            left -= 1
            if left <= 0:
                dead = False
        out[i] = cv if dead else 1.0
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


def run(scaler):
    NAVS = {}
    for c in ALL12:
        s = scaler(c)
        base = [BT.BASE.get(DATA[c]["rg4"][k], 0.0) * s[k] for k in range(len(s))]
        NAVS[c] = get_nav(c, W.get(c, 0.09), base)
        if c not in FIRST:
            FIRST[c] = min(NAVS[c])
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


_, CUM_NO, DD_NO = stat(run(lambda c: [1.0] * len(DATA[c]["b4"])))
print(f"对照（不开任何规则）：{CUM_NO / 1e4:.0f} 万，回撤 {DD_NO * 100:.1f}%")

CASES = []
for k in (1.5, 2.0, 3.0):
    CASES.append((f"减半锁死 k={k}", lambda c, kk=k: once(DATA[c], kk, 0.5)))
for k in (1.5, 2.0, 3.0):
    CASES.append((f"清零锁死 k={k}", lambda c, kk=k: once(DATA[c], kk, 0.0)))
for k in (1.5, 2.0):
    CASES.append((f"减半锁 30 天 k={k}",
                  lambda c, kk=k: once(DATA[c], kk, 0.5, hold_bars=180)))

print("\n" + "=" * 112)
print("一次性事件型风控：即时 vs 晚 1 根 vs 晚 2 根")
print("=" * 112)
print(pad("形态", 18) + pad("即时", 9) + pad("晚1根", 9) + pad("保留率", 9)
      + pad("晚2根", 9) + pad("即时回撤", 10) + pad("晚1根回撤", 11)
      + pad("翻转/年", 9) + pad("判定", 8))
print("-" * 112)
OK = []
for nm, fn in CASES:
    _, c0, d0 = stat(run(fn))
    _, c1, d1 = stat(run(lambda c, f=fn: delay(f(c), 1)))
    _, c2, d2 = stat(run(lambda c, f=fn: delay(f(c), 2)))
    ex0, ex1 = c0 - CUM_NO, c1 - CUM_NO
    keep = ex1 / ex0 * 100 if abs(ex0) > 1 else 0
    nf = 0
    for c in ALL12:
        s = fn(c)
        nf += sum(1 for i in range(1, len(s)) if abs(s[i] - s[i - 1]) > 1e-9)
    ok = (c1 >= CUM_NO) and (d1 > DD_NO)
    if ok:
        OK.append((nm, c0, c1, d0, d1, keep))
    print(pad(nm, 18) + pad("%.0f 万" % (c0 / 1e4), 9) + pad("%.0f 万" % (c1 / 1e4), 9)
          + pad("%.0f%%" % keep, 9) + pad("%.0f 万" % (c2 / 1e4), 9)
          + pad("%.1f%%" % (d0 * 100), 10) + pad("%.1f%%" % (d1 * 100), 11)
          + pad("%.0f" % (nf / 6.2), 9) + pad("✓" if ok else "✗", 8))

print("\n" + "=" * 112)
if OK:
    OK.sort(key=lambda x: -x[2])
    nm, c0, c1, d0, d1, keep = OK[0]
    print(f"过线形态：{nm}")
    print(f"  即时 {c0 / 1e4:.0f} 万（{(c0 - CUM_NO) / 1e4:+.0f} 万），回撤 {d0 * 100:.1f}%")
    print(f"  晚1根 {c1 / 1e4:.0f} 万（{(c1 - CUM_NO) / 1e4:+.0f} 万），回撤 {d1 * 100:.1f}%"
          f"，保留 {keep:.0f}%")
    print("  → 这条可以在人工执行下落地：只动一次，晚一根仍有正超额。")
else:
    print("全灭：即使改成一次性事件，晚 1 根仍不如不开规则。")
    print("→ 4H 级风控在本策略的人工执行路径上不具备可执行性。")
print("=" * 112)
