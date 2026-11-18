#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_gate.py —— 落地后的一致性核验

三件事：
  ① `bt_skeleton.base_series()`（新入口）与 `_exp_base.py` 里的实验实现逐 bar 一致
  ② 用新入口跑口径 A，确认结果 = 实验的 378 万（若不等，说明落地走样）
  ③ `portfolio_config.target(base_gate=...)` 的金额确实只缩底仓、不动加仓层

前两项对应「回测与实盘同源」，第三项对应「生产配置没改错」。
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 闸门已默认关闭（去前视后不如不开）。本脚本是「机制核验」，必须显式打开，
# 否则 series() 会返回全 1.0，[2] 直接等于基线、什么也没验到。
os.environ["SKEL_BASEGATE"] = "1"

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
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig)


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
print(f"底仓闸门：{'开' if BG.ENABLED else '关'}  MA{BG.GATE_MA}  "
      f"打折 {BG.GATE_CV:.2f}  滞回 ±{BG.GATE_HYST:.1%}")

# ---------- ① 入口一致性 ----------
print("\n[1] base_series() 与实验实现逐 bar 比对")
bad = 0
for c in ALL12:
    D = DATA[c]
    gate = BG.series(D["b4"], D["d1"], D["rg4"])
    bs = BT.base_series(D["b4"], D["d1"], D["rg4"])
    ref = [BT.BASE.get(D["rg4"][k], 0.0) * gate[k] for k in range(len(gate))]
    diff = sum(1 for x, y in zip(bs, ref) if abs(x - y) > 1e-9)
    tot = len(bs)
    up = sum(1 for x in bs if x >= 1.0)
    qu = sum(1 for x in bs if 1e-9 < x < 1.0)
    z = sum(1 for x in bs if x <= 1e-9)
    bad += diff
    print(f"  {c:<5} {tot:>6} 根   满仓 {up / tot:>5.0%}  打折 {qu / tot:>5.0%}  "
          f"空仓 {z / tot:>5.0%}   不一致 {diff}")
print(f"  合计不一致 {bad} 根 → {'✓ 一致' if bad == 0 else '✗ 有分歧'}")

# ---------- ② 口径 A 复算 ----------
print("\n[2] 用新入口跑口径 A（应约 378 万，对比基线 310 万）")
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}


def run():
    NAVS = {}
    for c in ALL12:
        D = DATA[c]
        base = BT.base_series(D["b4"], D["d1"], D["rg4"])
        pos = [base[k] + BT.ADD * D["sig"][k] for k in range(len(base))]
        pv = [p * k * VT.KAPPA for p, k in zip(pos, COEF[c])]
        b4 = D["b4"]
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        nav = 1.0
        out = {}
        w = W.get(c, 0.09)
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
            out[b4[k]["date"]] = nav
        NAVS[c] = out
    FIRST = {c: min(NAVS[c]) for c in ALL12}
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))
    tot = 0.0
    for y in range(2020, 2027):
        s = START_NAV if y == 2020 else f"{y}-01-01"
        e = f"{y + 1}-01-01"
        last, units, cq, cash, out = {}, {}, None, 0.0, []
        for d in [x for x in ALLD if x >= s and x < e]:
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
                    g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c])
                            for c in live) / v / 2
                    v *= (1 - min(g, 1.0) * 7e-4)
                cash = v * max(0.0, 1.0 - wsum)
                units = {c: v * uw[c] / last[c] for c in live}
                cq = q
            out.append((d, v))
        if len(out) > 20:
            r = out[-1][1] / out[0][1] - 1
            tot += CAP * r
            print(f"  {y}  {r * 100:+7.1f}%")
    return tot


cum = run()
print(f"\n  口径 A 累计：{cum / 1e4:.0f} 万")
print("  应对上 277 万（去前视的正确值）。历史数字 378 万含一格前视，已作废。")
print("  基线（不开闸门）310 万 → 去前视后闸门不如不开，故默认关闭"
      "（SKEL_BASEGATE=1 才启用）。见 base_gate.py 文件头。")

# ---------- ③ 生产金额核对 ----------
print("\n[3] portfolio_config.target(base_gate=...) 金额核对（BTC, 5000 本金）")
for st in ("UP", "RANGE", "DOWN"):
    for add in (False, True):
        a0, _ = PC.target(5000, "BTC", st, adding=add, base_gate=1.0)
        a1, n1 = PC.target(5000, "BTC", st, adding=add, base_gate=0.25)
        print(f"  {st:<6} adding={str(add):<5}  gate=1.00 → {a0:>8.0f} U   "
              f"gate=0.25 → {a1:>8.0f} U   {n1[:38]}")
print("\n  期望：UP 未突破/底仓、RANGE 的金额按 25% 缩；"
      "UP 已突破（含加仓层）只缩底仓那部分；DOWN 做空完全不变。")

# ---------- ④ 实盘链路 vs 回测链路 ----------
# 这一项是给「滞回状态怎么落盘」把关：实盘靠 _base_gate.json 记住上一次的取值，
# 回测靠 series() 内部的 cur 变量。DOWN 时两边都必须重置成「无记忆」，
# 否则下一段 UP/RANGE 的第一根就会走错分支（曾在这里差出一个 1% 的带子）。
# 注意：实盘算出的 gate 作用于**下一根**，所以比对前要左移一格（同 series()）。
print("\n[4] 实盘链路（next_gate + 落盘状态）与回测 base_series() 逐 bar 比对")
bad2 = 0
for c in ALL12:
    D = DATA[c]
    b4, d1, rg4 = D["b4"], D["d1"], D["rg4"]
    mm = BG.ma_lookup(d1)
    saved = None                      # = _base_gate.json 里存的 gate_state
    gs = []
    for k, b in enumerate(b4):
        if rg4[k] == "DOWN":
            g, saved = 1.0, None      # analyze() 里 gate_state = None
        else:
            g = BG.next_gate(b["close"], mm(b["date"]), saved)
            saved = g
        gs.append(g)
    # 平移闸门本身，**不是**平移乘完 BASE 的结果 —— 两者在状态切换点会不等
    gs = [gs[0]] + gs[:-1]
    live = [BT.BASE.get(rg4[k], 0.0) * gs[k] for k in range(len(gs))]
    ref = BT.base_series(b4, d1, rg4)
    diff = sum(1 for x, y in zip(live, ref) if abs(x - y) > 1e-9)
    bad2 += diff
    print(f"  {c:<5} {len(live):>6} 根   不一致 {diff}")
print(f"  合计不一致 {bad2} 根 → {'✓ 实盘与回测同源' if bad2 == 0 else '✗ 有分歧'}")
