#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_12coins.py —— 去 CRV，加入 ZEC + DOGE + NEAR

用户原话「去 crv 换成 zec，再加 doge 和 near，换成 11 币组合」。
实际枚数：10 − 1 + 3 = **12 币**。所以同时跑三个臂：
  A 10 币现状
  B 12 币（去 CRV + ZEC + DOGE + NEAR）        ← 用户原话
  C 11 币（B 再去掉 LINK，LINK 2022 起 sleeve 仅 +25.1%，最弱）← 严格 11

关键：ZEC 2024 年末 56 → 2025 年末 512（+810%）→ 2026 最高 1653。
把它加进组合等于「把去年的赢家加进来」，与上一轮被证伪的动量选币同构。
所以必须做**暴涨前检验**：2022-01 ~ 2025-01 这段 ZEC 还没起飞时，它有没有贡献。

口径与生产一致：季度再平衡、50 万、含手续费/资金费/滑点、叠 VT60(κ=1.22)。
"""
import csv
import io
import math
import os
import sys
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import CACHE, SYMBOLS, load_bars, to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h

CAP = 500_000
F4 = BT.FUND_DAILY / 6.0
OLD10 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE", "HYPE"]
NEW12 = [c for c in OLD10 if c != "CRV"] + ["ZEC", "DOGE", "NEAR"]
NEW11 = [c for c in NEW12 if c != "LINK"]
COINS = OLD10 + ["ZEC", "DOGE", "NEAR"]


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
    return b4, base, pos, trades


def worst_4h_qv(sym, months=3):
    files = sorted(f for f in os.listdir(CACHE) if f.startswith(sym + "-1h-"))
    if not files:
        return None
    buckets = {}
    for fn in files[-months:]:
        try:
            with zipfile.ZipFile(os.path.join(CACHE, fn)) as z:
                with z.open(z.namelist()[0]) as fh:
                    for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                        if not row or not row[0].isdigit():
                            continue
                        ts = int(row[0])
                        if ts > 1e14:
                            ts //= 1000
                        q = float(row[7]) if len(row) > 7 else 0.0
                        buckets[ts // (4 * 3600 * 1000)] = \
                            buckets.get(ts // (4 * 3600 * 1000), 0.0) + q
        except Exception:                       # noqa: BLE001
            continue
    if not buckets:
        return None
    v = sorted(buckets.values())
    return v[max(int(len(v) * 0.01), 0)]


print("加载 13 币 …", flush=True)
DATA = {}
for c in COINS:
    DATA[c] = load_coin(c)
    print(f"  {pad(c,5)} bars {len(DATA[c][0]):6d}  trades {len(DATA[c][3]):4d}", flush=True)

QV = {c: worst_4h_qv(SYMBOLS[c]) for c in COINS if c != "HYPE"}
QV["HYPE"] = PC.CAP_4H_WORST.get("HYPE")
COEF = {c: VT.coef_series(DATA[c][0]) for c in COINS}
POSV = {c: [p * k * VT.KAPPA for p, k in zip(DATA[c][2], COEF[c])] for c in COINS}

W = {}
_NAVC = {}


def set_w(coins, ws):
    global W
    W = dict(zip(coins, ws))


def get_nav(c, w):
    key = (c, round(w, 5))
    if key in _NAVC:
        return _NAVC[key]
    b4, base, pos = DATA[c][0], DATA[c][1], POSV[c]
    qv = QV.get(c) or 1e12
    out, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= "2020-09-01"), len(b4))
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = pos[k] - base[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)
            nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * w * d / qv) * d)
        out[b4[k]["date"]] = nav
    _NAVC[key] = out
    return out


def portfolio(coins, d0, d1=None):
    nv = {c: get_nav(c, W[c]) for c in coins}
    dates = sorted(set().union(*[set(n) for n in nv.values()]))
    dates = [d for d in dates if d >= d0 and (d1 is None or d < d1)]
    if not dates:
        return None, None
    start = dates[0]
    last, units = {}, {}
    for c in coins:
        ks = sorted(nv[c])
        prev = [d for d in ks if d <= start]
        last[c] = nv[c][prev[-1]] if prev else 1.0
        units[c] = W[c] / last[c]
    cur_q, out = None, []
    for d in dates:
        for c in coins:
            if d in nv[c]:
                last[c] = nv[c][d]
        v = sum(units[c] * last[c] for c in coins)
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if cur_q is not None and q != cur_q:
            g = sum(abs(v * W[c] - units[c] * last[c]) for c in coins) / v / 2
            v *= (1 - g * 7e-4)
            for c in coins:
                units[c] = v * W[c] / last[c]
        cur_q = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd


W10 = [PC.WEIGHTS[c] for c in OLD10]
W12 = [1 / 12] * 12
W11b = [1 / 11] * 11
ARMS = [("10 币（现状）", OLD10, W10),
        ("12 币（去CRV+ZEC+DOGE+NEAR）", NEW12, W12),
        ("11 币（12 币再去 LINK）", NEW11, W11b)]

print("\n【1】分年度（含成本与 VT60）")
YR = [(2022, "2022-01-01", "2023-01-01"), (2023, "2023-01-01", "2024-01-01"),
      (2024, "2024-01-01", "2025-01-01"), (2025, "2025-01-01", "2026-01-01"),
      (2026, "2026-01-01", None)]
print(pad("臂", 28) + "".join(pad(y, 10) for y, _a, _b in YR) + pad("合计", 10))
for name, coins, ws in ARMS:
    set_w(coins, ws)
    line, prod = pad(name, 28), 1.0
    for _y, d0, d1 in YR:
        r, _dd = portfolio(coins, d0, d1)
        line += pad(f"{r:+.1%}" if r is not None else "—", 10)
        prod *= 1 + (r or 0)
    print(line + pad(f"{prod:.2f}×", 10))

print("\n【1b】关键分段：ZEC 暴涨前 vs 暴涨期")
SEG = [("2022-01 ~ 2024-12（ZEC 蛰伏）", "2022-01-01", "2025-01-01"),
       ("2025-01 ~ 2026-11（ZEC 暴涨）", "2025-01-01", None),
       ("2022-01 ~ 2026-11（全段）", "2022-01-01", None)]
print(pad("臂", 28) + "".join(pad(s[0][:12], 14) for s in SEG) + pad("—", 3))
for name, coins, ws in ARMS:
    set_w(coins, ws)
    line = pad(name, 28) + f"{'':14}"
    for _lbl, d0, d1 in SEG:
        r, _dd = portfolio(coins, d0, d1)
        line += pad(f"{r:+.1%}", 14)
    print(line)

print("\n【1c】全段 2022-01 起：收益 / 年化 / 回撤 / Calmar")
print(pad("臂", 28) + pad("累计", 9) + pad("年化", 9) + pad("回撤", 9) + pad("Calmar", 8))
for name, coins, ws in ARMS:
    set_w(coins, ws)
    r, dd = portfolio(coins, "2022-01-01")
    ann = (1 + r) ** (1 / 4.88) - 1
    print(pad(name, 28) + pad(f"{(1 + r):.2f}×", 9) + pad(f"{ann:+.1%}", 9) +
          pad(f"{dd:.1%}", 9) + pad(f"{ann / abs(dd):.1f}", 8))

print("\n【2】滚动 365 天窗口（每 90 天，2022 起）· 对照臂 − 10 币")
T_END = date(2026, 9, 1).toordinal()
for name, coins, ws in ARMS[1:]:
    diffs, t = [], date(2022, 1, 1)
    while t.toordinal() + 365 <= T_END:
        t1 = date.fromordinal(t.toordinal() + 365)
        set_w(OLD10, W10)
        a = portfolio(OLD10, t.isoformat(), t1.isoformat())[0]
        set_w(coins, ws)
        b = portfolio(coins, t.isoformat(), t1.isoformat())[0]
        diffs.append((b - a) * 100)
        t = date.fromordinal(t.toordinal() + 90)
    ds = sorted(diffs)
    print(f"  {pad(name, 28)}窗 {len(diffs)}　胜 {sum(1 for x in diffs if x > 0.2)}　"
          f"负 {sum(1 for x in diffs if x < -0.2)}　中位 {ds[len(ds)//2]:+.1f}pp　"
          f"均值 {sum(diffs)/len(diffs):+.1f}pp　最差 {ds[0]:+.1f}pp　最好 {ds[-1]:+.1f}pp")

print("\n【3】选币窗口之外：2020-11 ~ 2021-12（2021 牛+顶，VT 最不利）")
print(pad("臂", 28) + pad("区间收益", 11) + pad("回撤", 10) + pad("Calmar", 8))
for name, coins, ws in ARMS:
    set_w(coins, ws)
    r, dd = portfolio(coins, "2020-11-01", "2022-01-01")
    print(pad(name, 28) + pad(f"{r:+.1%}", 11) + pad(f"{dd:.1%}", 10) +
          pad(f"{r / abs(dd):.1f}", 8))

print("\n【4】ZEC 贡献拆解（12 币臂，1/12 权重）")
set_w(NEW12, W12)
for lbl, d0, d1 in [("2022", "2022-01-01", "2023-01-01"), ("2023", "2023-01-01", "2024-01-01"),
                    ("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
                    ("2026", "2026-01-01", None)]:
    nv = get_nav("ZEC", W["ZEC"])
    ks = [d for d in sorted(nv) if d >= d0 and (d1 is None or d < d1)]
    if len(ks) < 2:
        continue
    m = nv[ks[-1]] / nv[ks[0]] - 1
    print(f"  ZEC {lbl}  sleeve {m:+8.1%}　贡献 {m * W['ZEC'] * 100:+7.1f}pp")

print("\n【5】12 币臂分币贡献（2022 起）")
rows, tot = [], 0.0
for c in NEW12:
    nv = get_nav(c, W[c])
    ks = [d for d in sorted(nv) if d >= "2022-01-01"]
    m = nv[ks[-1]] / nv[ks[0]] - 1
    rows.append((m * W[c] * 100, c, m))
    tot += m * W[c] * 100
print(pad("币", 6) + pad("sleeve", 11) + pad("贡献pp", 10) + pad("占比", 8))
for d, c, m in sorted(rows, reverse=True):
    print(pad(c, 6) + pad(f"{m:+.1%}", 11) + pad(f"{d:+.1f}", 10) + pad(f"{d / tot:+.0%}", 8))
top2 = sum(d for d, _c, _m in sorted(rows, reverse=True)[:2]) / tot
print(f"  合计 {tot:+.1f}pp　前 2 币占 {top2:.0%}")

print("\n【6】敞口与容量（12 币等权 8.33%，VT60 后，50 万本金）")
gr = sum(W[c] * sum(abs(p) for p in POSV[c]) / len(POSV[c]) for c in NEW12)
print(f"  平均总敞口（逐币均值加总）{gr:.3f}×　（10 币现状 0.686×）")
print(pad("币", 6) + pad("最差4H现货额", 14) + pad("5万", 9) + pad("20万", 9) +
      pad("50万", 9) + pad("判定(50万)", 11))
peak_w = (1 / 12) * (PC.BASE_RATIO + PC.ADD_RATIO) * VT.KAPPA
for c in NEW12:
    qv = QV.get(c)
    if not qv:
        continue
    cells = [cap * peak_w / qv for cap in (50_000, 200_000, 500_000)]
    flag = ("超标" if cells[-1] > 0.03 else "需拆单" if cells[-1] > 0.01
            else "可接受" if cells[-1] > 0.005 else "宽松")
    print(pad(c, 6) + pad(f"{qv / 1e4:.0f}万", 14) +
          "".join(pad(f"{x * 100:.2f}%", 9) for x in cells) + pad(flag, 11))
