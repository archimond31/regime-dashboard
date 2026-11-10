#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1H 优化 · 第三轮：既然入场信号无用，就优化「止损 + 持有」这个结构本身。

前两轮的结论链条：
  ① 入场信号（RSI 超卖/背离/站回）全部无效，越不筛选越好
  ② 把 1H 当加仓器也不行——超卖点后 768 根平均涨 +6.03%，随机时点 +9.38%
  ③ 卖出加仓点之后，剩下的就只有「1.5ATR 止损 + 长持有」这个壳子

那么问题变成：**这个壳子本身值多少钱？能不能优化它？**

方法：完全不设入场信号（每根 bar 都可开仓，平仓后下一根立刻再进），
只扫「止损宽度 × 持有周期」。这就把「结构」和「信号」彻底解耦。

口径升级（重要）：这里不再用 R，改用**组合净值**。
  每品种独立满仓复利 → 日频等权合成 → CAGR / MDD / Calmar。
  这样可以直接和等权 buy&hold（Calmar 0.65）对比，而不是在 R 的抽象单位里自说自话。
"""
import sys
import os
import math
import statistics
import datetime as dt
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_binance_vision import SYMBOLS
from strategy_1h_rsi import run_rsi

COST = 0.0004            # 单边 taker 0.04%
_CACHE = None


def load():
    global _CACHE
    if _CACHE is None:
        import test_1h_v2 as V2
        _CACHE = V2.load_all()
    return _CACHE


def nav_from_trades(bars, trades, cost=COST):
    """从交易列表构造 bar 级净值（持仓期满仓 notional，含调仓成本）。"""
    n = len(bars)
    if n < 2:
        return []
    pos = [0.0] * n
    for t in trades:
        for k in range(t["i"], min(t["j"], n)):
            pos[k] = 1.0
    closes = [b["close"] for b in bars]
    nav = 1.0
    curve = []
    for i in range(1, n):
        r = closes[i] / closes[i - 1] - 1
        if pos[i - 1] > 0:
            x = 1 + r
            nav *= x if x > 0 else 0.0
        if pos[i] != pos[i - 1]:
            nav *= (1 - cost * abs(pos[i] - pos[i - 1]))
        curve.append((bars[i]["date"], nav))
    return curve


def daily(curve):
    out = {}
    for d, v in curve:
        out[d] = v
    return out


def combine(navs):
    days = sorted({d for v in navs.values() for d in v})
    prev = {}
    nav = 1.0
    curve = []
    for d in days:
        rs = []
        for c, s in navs.items():
            if d in s:
                p0 = prev.get(c)
                if p0:
                    rs.append(s[d] / p0)
                prev[c] = s[d]
        if rs:
            nav *= statistics.mean(rs)
        curve.append((d, nav))
    return curve


def metrics(curve):
    if len(curve) < 200:
        return None
    v0, v1 = curve[0][1], curve[-1][1]
    days = (dt.date.fromisoformat(curve[-1][0]) - dt.date.fromisoformat(curve[0][0])).days
    yrs = days / 365.25
    cagr = (v1 / v0) ** (1 / yrs) - 1 if yrs > 0.5 else 0
    peak = mdd = 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"nav": v1 / v0, "yrs": yrs, "cagr": cagr, "mdd": mdd,
            "calmar": cagr / abs(mdd) if mdd else 0}


def bh_curve(data):
    navs = {}
    for c, (b, rg, dr, _d) in data.items():
        navs[c] = {x["date"]: x["close"] / b[0]["close"] for x in b}
    return combine(navs)


def exposure(trades, n_bars):
    return sum(t["bars_held"] for t in trades) / n_bars if n_bars else 0


def main():
    data = load()
    tb = sum(len(v[0]) for v in data.values())
    bh = metrics(bh_curve(data))
    print(f"等权 buy&hold 基准：CAGR {bh['cagr']*100:+.1f}%  "
          f"MDD {bh['mdd']*100:.1f}%  Calmar {bh['calmar']:.2f}  ({bh['yrs']:.1f} 年)\n")

    print("=" * 116)
    print("S9 结构扫描：无任何入场信号（每根 bar 都可开仓，只做多）+ 止损 × 持有")
    print("=" * 116)
    print(f"  {'止损':>8} {'持有':>8} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} "
          f"{'暴露':>6} {'笔数':>6} {'胜率':>6} {'换手/年':>8}")
    print("  " + "-" * 112)

    rows = []
    K_LIST = [0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 999]
    H_LIST = [96, 240, 480, 768, 1152, 1920, 999999]
    for k in K_LIST:
        for h in H_LIST:
            navs = {}
            tr_all = []
            for c, (b, rg, dr, _d) in data.items():
                tr = run_rsi(b, rg, entry_mode="always", force_side=1,
                             exit_mode="none", atr_k=k, max_bars=h,
                             allow_regime=False)
                tr = [t for t in tr if t["side"] == 1]
                tr_all += tr
                navs[c] = daily(nav_from_trades(b, tr))
            m = metrics(combine(navs))
            if not m:
                continue
            wins = sum(1 for t in tr_all if t["r"] > 0) / len(tr_all)
            turns = len(tr_all) / m["yrs"]
            rows.append((k, h, m, exposure(tr_all, tb), len(tr_all), wins, turns))
            kl = "无止损" if k > 100 else f"{k}ATR"
            hl = "不限期" if h > 10000 else f"{h}"
            print(f"  {kl:>8} {hl:>8} {m['cagr']*100:>+7.1f}% {m['mdd']*100:>7.1f}% "
                  f"{m['calmar']:>7.2f} {exposure(tr_all, tb)*100:>5.1f}% "
                  f"{len(tr_all):>6} {wins*100:>5.1f}% {turns:>8.0f}")

    # ---------- 最优 ----------
    print("\n" + "=" * 116)
    print("按 Calmar 排序 Top 10（Calmar = 风险调整后的效率，最适合跨杠杆比较）")
    print("=" * 116)
    print(f"  {'止损':>8} {'持有':>8} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} {'暴露':>7}")
    print("  " + "-" * 60)
    for k, h, m, ex, n, w, t in sorted(rows, key=lambda r: -r[2]["calmar"])[:10]:
        kl = "无止损" if k > 100 else f"{k}ATR"
        hl = "不限期" if h > 10000 else f"{h}"
        print(f"  {kl:>8} {hl:>8} {m['cagr']*100:>+7.1f}% {m['mdd']*100:>7.1f}% "
              f"{m['calmar']:>7.2f} {ex*100:>6.1f}%")
    print(f"  {'buy&hold':>8} {'—':>8} {bh['cagr']*100:>+7.1f}% {bh['mdd']*100:>7.1f}% "
          f"{bh['calmar']:>7.2f} {'100.0%':>7}")

    # ---------- 加入信号是否还有增量 ----------
    print("\n" + "=" * 116)
    print("S10 在最优结构上重新加回 RSI 信号，看还有没有增量")
    print("=" * 116)
    bk, bh_ = sorted(rows, key=lambda r: -r[2]["calmar"])[0][0], \
              sorted(rows, key=lambda r: -r[2]["calmar"])[0][1]
    print(f"  最优结构：止损 {bk}ATR × 持有 {bh_}\n")
    for lbl, kw in (
        ("无信号（随时进）", dict(entry_mode="always", force_side=1)),
        ("RSI<30 才进", dict(entry_mode="revert", os_th=30, ob_th=70)),
        ("RSI<20 才进", dict(entry_mode="revert", os_th=20, ob_th=80)),
        ("RSI<40 才进", dict(entry_mode="revert", os_th=40, ob_th=60)),
    ):
        navs = {}
        tr_all = []
        for c, (b, rg, dr, _d) in data.items():
            tr = run_rsi(b, rg, exit_mode="none", atr_k=bk, max_bars=bh_,
                         allow_regime=True, **kw)
            tr = [t for t in tr if t["side"] == 1]
            tr_all += tr
            navs[c] = daily(nav_from_trades(b, tr))
        m = metrics(combine(navs))
        if m:
            print(f"  {lbl:<20} CAGR {m['cagr']*100:>+7.1f}%  MDD {m['mdd']*100:>7.1f}%  "
                  f"Calmar {m['calmar']:>5.2f}  暴露 {exposure(tr_all, tb)*100:>5.1f}%  "
                  f"笔数 {len(tr_all):>5}")


if __name__ == "__main__":
    main()
