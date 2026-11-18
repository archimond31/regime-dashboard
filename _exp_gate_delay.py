#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_gate_delay.py —— 救闸门：能不能靠「加宽滞回 + 放慢均线」换回抗延迟能力

_exp_delay_check.py 的结论很硬：
  闸门即时  378 万（+68 万），晚 1 根 4H → 277 万（比不开闸门的 310 万还差）
  保留率 -49%。2021 年即时超额 +68.6pp，晚 1 根只剩 +4.4pp（保留 6%）。

机制不神秘：延迟损失的期望 ≈ Σ 每次翻转 × 那根 4H 的位移。
所以抗延迟只有两条路：
  a) 减少翻转次数（更慢的均线、更宽的滞回带）
  b) 让每次翻转的「位移」更大 —— 也就是阈值离价格更远（同样靠宽滞回）

两者都是同一件事：把阈值推到「单根 4H 噪声」之外。代价是反应变慢、收益下降。
本脚本就是找这个权衡点。

网格：MA ∈ {50, 80, 120} × HYST ∈ {1%, 2%, 3%, 5%, 8%}
每格输出：即时累计、晚 1 根累计、保留率、翻转次数/年、回撤
及格线（写死）：**晚 1 根仍 ≥ 310 万（不开闸门）**，且回撤优于 -52.5%。
"""
import bisect
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
CV_DOWN = 0.25


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


def ma_lookup(d1, n):
    dc = [(x["date"][:10], x["close"]) for x in d1]
    dates = [x[0] for x in dc]
    ma = {}
    for i in range(n - 1, len(dc)):
        ma[dc[i][0]] = sum(x[1] for x in dc[i - n + 1:i + 1]) / n

    def get(d):
        i = bisect.bisect_left(dates, d[:10]) - 1
        return ma.get(dates[i]) if i >= 0 else None

    return get


def gate_series(D, n, hyst):
    b4, d1, rg4 = D["b4"], D["d1"], D["rg4"]
    mm = ma_lookup(d1, n)
    out, cur = [], None
    for k in range(len(b4)):
        if BT.BASE.get(rg4[k], 0.0) <= 0:
            cur = None
            out.append(1.0)
            continue
        m = mm(b4[k]["date"])
        px = b4[k]["close"]
        if m is None or not m:
            cur = 1.0 if cur is None else cur
        elif cur is None:
            cur = CV_DOWN if px < m * (1 + hyst) else 1.0
        elif cur >= 1.0:
            cur = CV_DOWN if px < m * (1 - hyst) else 1.0
        else:
            cur = 1.0 if px > m * (1 + hyst) else CV_DOWN
        out.append(cur)
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


def curve_of(gates):
    NAVS = {}
    for c in ALL12:
        base = [BT.BASE.get(DATA[c]["rg4"][k], 0.0) * gates[c][k]
                for k in range(len(gates[c]))]
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


# 对照：完全不开闸门
NOG = curve_of({c: [1.0] * len(DATA[c]["b4"]) for c in ALL12})
_, CUM_NO, DD_NO = stat(NOG)
print(f"对照（不开闸门）：{CUM_NO / 1e4:.0f} 万，回撤 {DD_NO * 100:.1f}%\n")

print("=" * 110)
print("网格：MA × 滞回带。及格线 = 晚 1 根仍 ≥ 不开闸门，且回撤优于不开")
print("=" * 110)
print(pad("MA", 6) + pad("滞回", 7) + pad("即时", 9) + pad("晚1根", 9)
      + pad("保留率", 9) + pad("即时回撤", 10) + pad("晚1根回撤", 11)
      + pad("翻转/年", 9) + pad("判定", 10))
print("-" * 110)
BEST = None
for n in (50, 80, 120):
    for h in (0.01, 0.02, 0.03, 0.05, 0.08):
        G = {c: gate_series(DATA[c], n, h) for c in ALL12}
        G1 = {c: delay(G[c], 1) for c in ALL12}
        _, cum0, dd0 = stat(curve_of(G))
        _, cum1, dd1 = stat(curve_of(G1))
        ex0 = cum0 - CUM_NO
        ex1 = cum1 - CUM_NO
        keep = (ex1 / ex0 * 100) if abs(ex0) > 1 else 0
        nf = sum(sum(1 for i in range(1, len(G[c])) if abs(G[c][i] - G[c][i - 1]) > 1e-9)
                 for c in ALL12) / 6.2
        ok = (cum1 >= CUM_NO) and (dd1 > DD_NO)
        print(pad(n, 6) + pad("%.0f%%" % (h * 100), 7) + pad("%.0f 万" % (cum0 / 1e4), 9)
              + pad("%.0f 万" % (cum1 / 1e4), 9) + pad("%.0f%%" % keep, 9)
              + pad("%.1f%%" % (dd0 * 100), 10) + pad("%.1f%%" % (dd1 * 100), 11)
              + pad("%.0f" % nf, 9) + pad("✓ 过" if ok else "✗", 10))
        if ok and (BEST is None or cum1 > BEST[3]):
            BEST = (n, h, cum0, cum1, dd0, dd1, keep)

print("\n" + "=" * 110)
if BEST:
    n, h, cum0, cum1, dd0, dd1, keep = BEST
    print(f"最优可行点：MA{n} / 滞回 ±{h * 100:.0f}%")
    print(f"  即时   {cum0 / 1e4:.0f} 万（超额 {(cum0 - CUM_NO) / 1e4:+.0f} 万），回撤 {dd0 * 100:.1f}%")
    print(f"  晚1根  {cum1 / 1e4:.0f} 万（超额 {(cum1 - CUM_NO) / 1e4:+.0f} 万），回撤 {dd1 * 100:.1f}%")
    print(f"  保守取值（只用延迟后仍成立的部分）：{(cum1 - CUM_NO) / 1e4:+.0f} 万")
else:
    print("没有任何参数点能在「晚 1 根执行」下仍优于不开闸门。")
    print("→ 结论：MA 穿越类闸门的收益完全绑定在 4H 收盘瞬间的事实执行上。")
    print("→ 人工看邮件下单的执行方式拿不到这部分收益，应当撤回或改为自动执行。")
print("=" * 110)
