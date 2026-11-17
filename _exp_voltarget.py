#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_voltarget.py —— 「等资金」vs「等风险」对照

现状（等资金）：每个币固定权重（10~11%），sleeve 内仓位固定 {0, 1.0, 1.5, -0.5}。
   问题：CRV 年化波动 120%+、BTC 50%，同样 10% 的钱承担的风险差 2~3 倍。

等风险（vol target，本实验）：
   每个 sleeve 的仓位乘一个系数 s_c = clip(target / σ_c, lo, hi)，
   σ_c 是该币日收益的滚动年化波动（严格用当日之前的窗口，无未来函数）。
   再平衡周期可调（默认 7 天）—— 每天调会被手续费和滑点吃光，这一点必须先固定。

**公平性设计**：把两臂的「时间平均总敞口」用同一个 κ 对齐，
否则等风险版只是杠杆不同，收益差异说明不了任何事。

五节：
  1) 敞口对齐校验 + 换手（对不齐后面全部作废）
  2) 逐年 + 全样本（50 万本金，含手续费/资金费/滑点）
  3) 分币 sleeve 变化（钱是从谁挪给谁的）
  4) 滚动 365 天窗口胜负
  5) 波动缩放的分布（系数有没有被 clip 卡住）
"""
import json
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
W = dict(PC.WEIGHTS)
F4 = BT.FUND_DAILY / 6.0
CAP = 500_000
TARGET = 0.60          # 目标年化波动 60%
LO, HI = 0.30, 2.00    # 缩放系数上下限
SPOT_BASE = True       # 先保持现货底仓口径，避免与上一问的成本混在一起

ARMS = [("等资金", None, 0), ("VT20周", 20, 7), ("VT60周", 60, 7), ("VT60月", 60, 30)]


def pad(s, n, right=False):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


cf = json.load(open("_cap_fut.json", encoding="utf-8"))
QV = {r["c"]: r["fut_min"] for r in cf["rows"]}
for c in COINS:
    if c not in QV and c in PC.CAP_4H_WORST:
        QV[c] = float(PC.CAP_4H_WORST[c])

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()
RAW = {}
for c in BIN9:
    b = V2.load_all()[c][0]
    b4 = agg4h(b)
    rg4 = align_regime_safe(b4, regime_series(to_daily(b)))
    sig, _tr = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q, coin=c)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    RAW[c] = (b4, base, [base[k] + BT.ADD * sig[k] for k in range(len(sig))])
hb = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
hrg = align_regime_safe(hb, regime_series(to_daily(hb)))
hsig, _ht = BT.signals(hb, hrg, atr_wilder(hb, 14), vol_q=BT.VOL_Q, coin="HYPE")
hbase = [BT.BASE.get(r, 0.0) for r in hrg]
RAW["HYPE"] = (hb, hbase, [hbase[k] + BT.ADD * hsig[k] for k in range(len(hsig))])


def idx_of(b4, d):
    return next((i for i, x in enumerate(b4) if x["date"] >= d), len(b4))


def daily_vol(b4, lb):
    """日收盘 → 滚动年化波动。回看窗口严格取「当日之前」，避免未来函数。"""
    dc = []
    for b in b4:
        if not dc or dc[-1][0] != b["date"]:
            dc.append([b["date"], b["close"]])
        else:
            dc[-1][1] = b["close"]
    rets = [None] + [math.log(dc[i][1] / dc[i - 1][1]) for i in range(1, len(dc))]
    out = {}
    for i in range(len(dc)):
        seg = [r for r in rets[max(0, i - lb):i] if r is not None]
        if len(seg) >= max(8, lb // 3):
            m = sum(seg) / len(seg)
            sd = math.sqrt(sum((x - m) ** 2 for x in seg) / len(seg))
            out[dc[i][0]] = sd * math.sqrt(365)
    return out


def apply_vt(b4, pos, lb, rebal):
    """按 vol 缩放仓位；只在再平衡日更新系数，其余时间保持。"""
    vm = daily_vol(b4, lb)
    out = list(pos)
    cur, last = 1.0, None
    for k, b in enumerate(b4):
        d = b["date"]
        dt = date(*map(int, d.split("-")))
        if last is None or (dt - last).days >= rebal:
            v = vm.get(d)
            if v and v > 1e-6:
                cur = min(HI, max(LO, TARGET / v))
            last = dt
        out[k] = pos[k] * cur
    return out


def avg_gross(legs):
    """时间平均总敞口：Σ_c W_c × |pos|。"""
    tot, n = 0.0, 0
    for c, pos in legs:
        for p in pos:
            tot += W[c] * abs(p)
            n += 1
    return tot / max(n, 1)


def nav_daily(b4, base, pos, k0, coin, k1=None, slip_a=100.0):
    nav, out, w = 1.0, {}, W[coin]
    qv = QV.get(coin)
    for k in range(k0 + 1, (k1 if k1 is not None else len(b4))):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if SPOT_BASE else pos[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)
            notional = nav * CAP * w * d
            nav *= (1 - slip_a / 1e4 * math.sqrt(notional / qv) * d)
        out[b4[k]["date"]] = nav
    return out


def fixed_share(navs, weights):
    dates = sorted(set().union(*[set(n) for n in navs]))
    last = [1.0] * len(navs)
    out = []
    for d in dates:
        for i, n in enumerate(navs):
            if d in n:
                last[i] = n[d]
        out.append((d, sum(weights[i] * last[i] for i in range(len(navs)))))
    return out


def stat(cur):
    vals = [x[1] for x in cur]
    v0 = vals[0]
    pk, dd = v0, 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    fin = vals[-1] / v0
    return fin - 1, dd


# ---------- 构造四臂仓位 ----------
POS = {}
for vname, lb, reb in ARMS:
    POS[vname] = {}
    for c in COINS:
        b4, base, pos = RAW[c]
        POS[vname][c] = list(pos) if lb is None else apply_vt(b4, pos, lb, reb)
    g = avg_gross([(c, POS[vname][c]) for c in COINS])
    POS[vname]["__gross__"] = g

g0 = POS["等资金"]["__gross__"]
print("【1】敞口对齐与换手")
print(pad("臂", 12) + pad("原始平均敞口", 14, 1) + pad("κ 归一化", 11, 1) +
      pad("对齐后敞口", 12, 1) + pad("年换手(次)", 12, 1))
for vname, lb, reb in ARMS:
    g = POS[vname]["__gross__"]
    kappa = g0 / g if g else 1.0
    for c in COINS:
        POS[vname][c] = [p * kappa for p in POS[vname][c]]
    # 换手：|Δpos| 之和 / 2 / 年数
    turn = 0.0
    for c in COINS:
        p = POS[vname][c]
        turn += sum(abs(p[k] - p[k - 1]) for k in range(1, len(p)))
    years = 6.2
    print(pad(vname, 12) + pad(f"{g:.3f}×", 14, 1) + pad(f"{kappa:.3f}", 11, 1) +
          pad(f"{g * kappa:.3f}×", 12, 1) + pad(f"{turn / 2 / years:.0f}", 12, 1))

# ---------- 2. 收益 ----------
print("\n【2】组合表现（50 万，含手续费/资金费/滑点，平均敞口已对齐）")
print(pad("臂", 12) + pad("2024", 10, 1) + pad("2025", 10, 1) + pad("2026YTD", 10, 1) +
      pad("全样本", 11, 1) + pad("最大回撤", 10, 1) + pad("Calmar", 9, 1))
RES = {}
for vname, _lb, _reb in ARMS:
    navs = []
    for c in COINS:
        b4, base, _p = RAW[c]
        navs.append(nav_daily(b4, base, POS[vname][c], idx_of(b4, "2020-09-01"), c))
    r, dd = stat(fixed_share(navs, [W[c] for c in COINS]))
    RES[vname] = (r, dd)
    line = pad(vname, 12)
    for d0, d1 in (("2024-01-01", "2025-01-01"), ("2025-01-01", "2026-01-01"),
                   ("2026-01-01", None)):
        nv = []
        for c in COINS:
            b4, base, _p = RAW[c]
            nv.append(nav_daily(b4, base, POS[vname][c], idx_of(b4, d0), c,
                                idx_of(b4, d1) if d1 else None))
        rr, _dd2 = stat(fixed_share(nv, [W[c] for c in COINS]))
        line += pad(f"{rr:+.1%}", 10, 1)
    line += pad(f"{r:+.1%}", 11, 1) + pad(f"{dd:.1%}", 10, 1) + \
        pad(f"{r / abs(dd):.2f}", 9, 1)
    print(line)

# ---------- 3. 分币 ----------
print("\n【3】分币 sleeve 变化（等资金 → VT60周，倍数）")
print(pad("币", 7) + pad("平均|仓位| 等资金", 16, 1) + pad("VT60周", 10, 1) +
      pad("资金迁移", 10, 1) + pad("sleeve 倍数 等资金", 18, 1) + pad("VT60周", 10, 1))
for c in COINS:
    b4, base, _p = RAW[c]
    a = sum(abs(x) for x in POS["等资金"][c]) / len(POS["等资金"][c])
    b = sum(abs(x) for x in POS["VT60周"][c]) / len(POS["VT60周"][c])
    n1 = nav_daily(b4, base, POS["等资金"][c], 0, c)
    n2 = nav_daily(b4, base, POS["VT60周"][c], 0, c)
    print(pad(c, 7) + pad(f"{a:.2f}", 16, 1) + pad(f"{b:.2f}", 10, 1) +
          pad(f"{(b / a - 1) * 100:+.0f}%", 10, 1) +
          pad(f"{list(n1.values())[-1]:.1f}×", 18, 1) +
          pad(f"{list(n2.values())[-1]:.1f}×", 10, 1))

# ---------- 4. 滚动窗口 ----------
print("\n【4】滚动 365 天窗口（每 90 天起一窗，2021 起）")
print(pad("臂", 12) + pad("胜", 6, 1) + pad("负", 6, 1) +
      pad("中位差", 10, 1) + pad("均值差", 10, 1))
for vname, _lb, _reb in ARMS[1:]:
    diffs = []
    t = date(2021, 1, 1)
    while True:
        t1 = t.replace(year=t.year + 1)
        if t1 > date(2026, 9, 1):
            break
        rr = {}
        for arm in ("等资金", vname):
            nv = []
            for c in COINS:
                b4, base, _p = RAW[c]
                nv.append(nav_daily(b4, base, POS[arm][c], idx_of(b4, t.isoformat()), c,
                                    idx_of(b4, t1.isoformat())))
            rr[arm] = stat(fixed_share(nv, [W[c] for c in COINS]))[0]
        diffs.append((rr[vname] - rr["等资金"]) * 100)
        t = t.replace(year=t.year + 1)
    w = sum(1 for x in diffs if x > 0.2)
    n = sum(1 for x in diffs if x < -0.2)
    ds = sorted(diffs)
    print(pad(vname, 12) + pad(w, 6, 1) + pad(n, 6, 1) +
          pad(f"{ds[len(ds) // 2]:+.1f}pp", 10, 1) +
          pad(f"{sum(diffs) / len(diffs):+.1f}pp", 10, 1))

# ---------- 5. 缩放分布 ----------
print("\n【5】波动缩放系数分布（VT60周，未归一化前）")
for c in COINS:
    b4 = RAW[c][0]
    vm = daily_vol(b4, 60)
    vs = sorted(vm.values())
    if not vs:
        continue
    sc = [min(HI, max(LO, TARGET / v)) for v in vs]
    sc.sort()
    hit_hi = sum(1 for x in sc if x >= HI - 1e-9) / len(sc)
    hit_lo = sum(1 for x in sc if x <= LO + 1e-9) / len(sc)
    print(f"  {pad(c, 6)} 年化波动 中位 {vs[len(vs) // 2]:>5.0%}  系数中位 "
          f"{sc[len(sc) // 2]:>4.2f}  触上限 {hit_hi:>5.0%}  触下限 {hit_lo:>5.0%}")
