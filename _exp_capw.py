#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_capw.py —— 按容量重排权重（目标：50 万本金下每笔冲击 ≤3%）

问题：12 币等权在 50 万下绝大多数币都超出单笔冲击红线，只能靠拆单硬扛。
本脚本做两件事：
  ① 用「容量上限 + 等权水位填充」重排权重（water-filling）
  ② 检验「少币满仓」是不是比「12 币稀释」更好

容量口径（与 portfolio_config.CAP_4H_WORST 一致）：
  近 3 个完整月 4H 现货成交额的 p5 分位。
  峰值敞口 = w × (BASE+ADD) × 波动系数。
  波动系数取**典型值 κ=1.22**，不是极端峰值 2.44 —— VT 本来就会在高波动时
  自动缩仓，用 2.44 等于对同一件事惩罚两次。极端峰值出现在少数日子，靠拆单处理。

关键修正：**renorm**。
  HYPE 2024-12 才上线。若给它 35% 权重，2022~2024 这 35% 会变成空仓，
  把所有权重方案的比较都污染掉。renorm 的做法是：每季度再平衡时只按
  「当天有数据的币」归一化权重 —— 钱始终在场，这才是实盘会做的事。
"""
import bisect
import math
import os
import sys
from datetime import date

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
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
# p5 口径的 4H 成交额（2026-08~2026-10），与 portfolio_config 同步
QV = {"BTC": 12864334, "ETH": 4909230, "SOL": 2004788, "BNB": 1058019,
      "XRP": 936095, "UNI": 206208, "HYPE": 15570095, "ZEC": 723117,
      "DOGE": 360810, "NEAR": 269908, "AAVE": 121981, "LINK": 217625}


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def capacity_weights(coins, capital=CAP, max_impact=0.03, vt_scale=VT.KAPPA):
    """容量上限 + 等权水位填充。返回 {coin: weight}，合计 1.0。

    先用 1/N 等权做起点，把超过容量上限的币封顶，剩下的权重在
    未封顶的币之间重新等分，直到稳定。若所有币都封顶（容量不足满仓），
    则按容量比例归一化 —— 那种情况下满仓本身就不该做。
    """
    peak = (PC.BASE_RATIO + PC.ADD_RATIO) * vt_scale
    wmax = {c: max_impact * QV[c] / (capital * peak) for c in coins}
    w = {c: 1.0 / len(coins) for c in coins}
    for _ in range(100):
        capped = {c for c in coins if w[c] >= wmax[c] - 1e-12}
        rest = [c for c in coins if c not in capped]
        if not rest:
            break
        free = 1.0 - sum(wmax[c] for c in capped)
        if free <= 1e-12:
            break
        share = free / len(rest)
        new = {c: (wmax[c] if c in capped else min(share, wmax[c])) for c in coins}
        if max(abs(new[c] - w[c]) for c in coins) < 1e-12:
            w = new
            break
        w = new
    tot = sum(w.values())
    if tot < 0.999:                       # 容量不够满仓 → 按比例归一
        w = {c: v / tot for c, v in w.items()}
    return w


# ---------------- 数据 ----------------
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


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c][0]) for c in ALL12}
POSV = {c: [p * k * VT.KAPPA for p, k in zip(DATA[c][2], COEF[c])] for c in ALL12}


def get_nav(c, w):
    """该币在固定权重 w 下的 sleeve 净值（VT60 后、含滑点）。"""
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
    return out


NAVS = {c: get_nav(c, 0.09) for c in ALL12}    # 权重只影响滑点量级，0.09 是中性参考
FIRST = {c: min(NAVS[c]) for c in ALL12}
KEYS = {c: sorted(NAVS[c]) for c in ALL12}
ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))


def nav_before(c, d):
    """该币在 d 当天（或之前最近一根）的净值。

    必须用它来初始化持仓价格，不能用 FIRST[c] —— 那会把「币上市以来」的涨幅
    一次性塞进评估起点第一天，凭空造出几十倍收益。
    """
    ks = KEYS[c]
    i = bisect.bisect_right(ks, d) - 1
    return NAVS[c][ks[i]] if i >= 0 else 1.0


def curve(coins, ws, renorm=True, start="2022-01-01", end=None):
    """组合净值。renorm=True 时每季度只按「当天已上线」的币归一化权重。

    Σw < 1 时差额作为**现金**计入（不产生收益）—— 容量约束下这是常态，
    不能像满仓假设那样让现金凭空消失。
    """
    dates = [d for d in ALLD if d >= start and (end is None or d < end)]
    if not dates:
        return None, None, {}
    last, units = {}, {}
    cq = None
    cash = 0.0
    out = []
    for d in dates:
        live = [c for c in coins if FIRST[c] <= d]
        if not live:
            continue
        sw = sum(ws[c] for c in live)
        # 目标仓位 = 方案的权重总和（可能 <1，留现金）。
        # renorm 只负责「补上缺席币的权重」，不负责把仓位推到 100%。
        tgt = min(1.0, sum(ws.values()))
        uw = ({c: ws[c] / sw * tgt for c in live} if renorm
              else {c: ws[c] for c in live})
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
            elif c not in last:
                last[c] = nav_before(c, d)
        inv = sum(units.get(c, 0.0) * last[c] for c in live) if units else 0.0
        v = cash + inv if units else 1.0
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if (cq is None) or (q != cq) or (set(units) != set(live)):
            wsum = sum(uw.values())
            if units:
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c]) for c in live) / v / 2
                adj = sum(uw.values())
                g = min(g, adj)
                v *= (1 - g * 7e-4)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd, dict(zip([x[0] for x in out], vals))


def yr_split(cv, years):
    firsts = {}
    for d, v in cv.items():
        firsts.setdefault(int(d[:4]), v)
    ks = sorted(cv)
    res = {}
    for y in years:
        if y in firsts:
            res[y] = firsts.get(y + 1, cv[ks[-1]]) / firsts[y] - 1
    return res


# ---------------- 权重方案 ----------------
WF12 = capacity_weights(ALL12)
EQ12 = {c: 1 / 12 for c in ALL12}
AB4 = {c: (0.04 if c in ("AAVE", "LINK") else 0.092) for c in ALL12}


def capped_wf(coins, cap_w):
    """容量填充后再加单币权重上限。不做归一化 —— 差额留现金，这是容量约束的诚实结果。"""
    peak = (PC.BASE_RATIO + PC.ADD_RATIO) * VT.KAPPA
    wmax = {c: min(cap_w, 0.03 * QV[c] / (CAP * peak)) for c in coins}
    w = {c: 1.0 / len(coins) for c in coins}
    for _ in range(100):
        over = {c for c in coins if w[c] >= wmax[c] - 1e-12}
        if not over:
            break
        rest = [c for c in coins if c not in over]
        if not rest:
            break
        free = 1.0 - sum(wmax[c] for c in over)
        if free <= 1e-12:
            w = dict(wmax)
            break
        new = {c: (wmax[c] if c in over else min(free / len(rest), wmax[c]))
               for c in coins}
        if max(abs(new[c] - w[c]) for c in coins) < 1e-12:
            w = new
            break
        w = new
    return w


G20 = capped_wf(ALL12, 0.20)
G25 = capped_wf(ALL12, 0.25)
EQ6 = {c: 1 / 6 for c in ["BTC", "ETH", "SOL", "BNB", "XRP", "HYPE"]}
EQ5 = {c: 1 / 5 for c in ["BTC", "ETH", "SOL", "ZEC", "HYPE"]}
EQ4 = {c: 1 / 4 for c in ["BTC", "ETH", "SOL", "HYPE"]}
PLANS = [("A 12币等权", EQ12), ("B 12币 AAVE/LINK 4%", AB4),
         ("C 12币 容量填充", WF12), ("G 容量+单币≤20%", G20),
         ("H 容量+单币≤25%", G25), ("D 6币等权", EQ6),
         ("E 5币等权", EQ5), ("F 4币等权", EQ4)]

print("\n【0】权重方案（50 万本金、单笔冲击上限 3%、VT 典型系数 1.22）")
hdr = pad("方案", 22) + "".join(pad(c, 7) for c in ALL12) + pad("合计", 7)
print(hdr)
for name, w in PLANS:
    print(pad(name, 22) + "".join(pad(f"{w.get(c, 0) * 100:.1f}" if w.get(c) else "—", 7)
                                 for c in ALL12) + pad(f"{sum(w.values()) * 100:.0f}%", 7))

print("\n【1】各方案 50 万下的实际单笔冲击（按典型 κ=1.22；括号内为极端峰值 2.44）")
print(pad("方案", 22) + pad("最大冲击", 10) + pad("超标币数", 10) + pad("最紧的币", 12))
for name, w in PLANS:
    mx, tight, n_bad = 0.0, "", 0
    for c in ALL12:
        if not w.get(c):
            continue
        imp = CAP * w[c] * 1.5 * VT.KAPPA / QV[c]
        imp_x = imp * (VT.HI / 1.0) / 1.0   # 极端峰值 2.44 对 1.22 是 2 倍
        if imp > mx:
            mx, tight = imp, c
        if imp > 0.03:
            n_bad += 1
    print(pad(name, 22) + pad(f"{mx:.1%}（{mx * 2:.0f}%）", 10) +
          pad(n_bad, 10) + pad(tight, 12))

print("\n【2】分年度（renorm：HYPE 缺席期权重按在场币重新归一）")
YEARS = [2022, 2023, 2024, 2025, 2026]
print(pad("方案", 22) + "".join(pad(y, 10) for y in YEARS) + pad("合计", 9))
res = {}
for name, w in PLANS:
    coins = [c for c in ALL12 if w.get(c)]
    r, dd, cv = curve(coins, w)
    ys = yr_split(cv, YEARS)
    res[name] = (r, dd)
    prod = 1.0
    line = pad(name, 22)
    for y in YEARS:
        v = ys.get(y)
        line += pad(f"{v:+.1%}" if v is not None else "—", 10)
        prod *= 1 + (v or 0)
    print(line + pad(f"{prod:.2f}×", 9))

print("\n【3】2022-01 起全段：收益 / 年化 / 回撤 / Calmar")
print(pad("方案", 22) + pad("累计", 9) + pad("年化", 9) + pad("回撤", 9) + pad("Calmar", 8))
for name, (r, dd) in res.items():
    ann = (1 + r) ** (1 / 4.88) - 1
    print(pad(name, 22) + pad(f"{(1 + r):.2f}×", 9) + pad(f"{ann:+.1%}", 9) +
          pad(f"{dd:.1%}", 9) + pad(f"{ann / abs(dd):.1f}", 8))

print("\n【4】只取 2025 起（12 币全在场，检验权重本身而非历史可得性）")
print(pad("方案", 22) + pad("收益", 10) + pad("回撤", 9))
for name, w in PLANS:
    coins = [c for c in ALL12 if w.get(c)]
    r, dd, _cv = curve(coins, w, start="2025-01-01")
    print(pad(name, 22) + pad(f"{r:+.1%}", 10) + pad(f"{dd:.1%}", 9))
