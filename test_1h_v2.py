#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1H RSI 策略 · 第二版验证（用户反驳后的假设检验）。

用户命题拆成 5 条可证伪的子假设：
  H1  破超卖回归 / 底背离 是比「一碰超卖就进」更好的入场（等确认，不接飞刀）
  H2  出场改为「日线 RSI 触及超买」比「1H RSI 回中轴」更好（让利润跑）
  H3  放大止损会改善效果（止损太紧被扫）
  H4  该组合在日线 UP 状态最优、DOWN 状态镜像有效、RANGE 中性
  H5  这套在扣除成本后仍优于 4H 基准（+0.346R/笔）

评价口径（两个都要看，否则会自欺）：
  R 口径   = 每笔以固定风险金额（如净值 1%）下单，r 即账户收益率的倍数
  净值口径 = Π(1 + f·r_net)，f 为单笔风险占比。算术 ΣR 为正 ≠ 复利赚钱。

成本：taker 往返 0.08% → cost_R = 0.0016 / stop_pct。止损越窄，成本占 R 越大。
"""
import sys
import os
import math
import statistics
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_binance_vision import load_bars, to_daily, SYMBOLS
from strategy_1h_rsi import (align_regime_safe, align_daily_rsi,
                             regime_series, run_rsi, stats)
from _proto_4h import rsi_wilder

TAKER = 0.0008
RT_COST = TAKER              # 0.08% 已是「开+平」往返（合约 taker 0.04%/边）
                             # cost_R = RT_COST / stop_pct，止损越窄成本占 R 越大

_CACHE = None


def load_all():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    data = {}
    for c in SYMBOLS:
        b = load_bars(c, "1h")
        daily = to_daily(b)
        rg = align_regime_safe(b, regime_series(daily))
        dc = [r["close"] for r in daily]
        dr = rsi_wilder(dc, 14)
        drm = {r["date"]: v for r, v in zip(daily, dr) if v is not None}
        data[c] = (b, rg, align_daily_rsi(b, drm), drm)
    _CACHE = data
    return data


def collect(data, **kw):
    tr = []
    for c, (b, rg, dr, _drm) in data.items():
        tr += run_rsi(b, rg, daily_rsi=dr, **kw)
    tr.sort(key=lambda t: (t["date"], t["time"]))
    return tr


def decompose(trades):
    """把统计拆成：毛 R、成本 R、净值复利。"""
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    costs = [RT_COST / t["stop_pct"] for t in trades]
    nets = [r - c for r, c in zip(rs, costs)]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    peak = cum = 0.0
    mdd = 0.0
    for r in nets:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    out = {
        "n": len(rs),
        "win": len(wins) / len(rs),
        "sumR": sum(rs),
        "avgR": sum(rs) / len(rs),
        "costR": sum(costs) / len(costs),
        "netR": sum(nets) / len(nets),
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else None,
        "mddR": mdd,
        "avg_bars": sum(t["bars_held"] for t in trades) / len(trades),
        "stop_pct": statistics.median([t["stop_pct"] for t in trades]),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "reasons": Counter(t["reason"] for t in trades),
    }
    # 复利净值
    for f in (0.0025, 0.005, 0.01, 0.02):
        s = 0.0
        ruin = False
        for r in nets:
            x = 1 + f * r
            if x <= 0:
                ruin = True
                break
            s += math.log(x)
        out[f"eq_{f}"] = 0.0 if ruin else math.exp(s)
    return out


def line(lbl, d, w=26):
    if d is None:
        print(f"  {lbl:<{w}} 无交易")
        return
    pf = f"{d['pf']:.2f}" if d["pf"] else " — "
    print(f"  {lbl:<{w}} {d['n']:>5} {d['win']*100:>5.1f}% {d['sumR']:>+8.1f} "
          f"{d['avgR']:>+6.3f} {d['netR']:>+6.3f} {pf:>5} {d['mddR']:>7.1f} "
          f"{d['avg_bars']:>6.0f} {d['eq_0.0025']:>6.2f} {d['eq_0.01']:>6.2f}")


HDR = (f"  {'配置':<26} {'笔数':>5} {'胜率':>6} {'总R':>8} {'毛/笔':>6} {'净/笔':>6} "
       f"{'PF':>5} {'回撤':>7} {'持仓':>6} {'复利':>6} {'复利':>6}")
SUB = (f"  {'':<26} {'':>5} {'':>6} {'':>8} {'':>6} {'':>6} "
       f"{'':>5} {'':>7} {'根':>6} {'.25%':>6} {'1%':>6}")


def head(t):
    print("\n" + "=" * 112)
    print(t)
    print("=" * 112)
    print(HDR)
    print(SUB)
    print("  " + "-" * 108)


def main():
    data = load_all()
    tot = sum(len(v[0]) for v in data.values())
    print(f"载入 {len(data)} 品种，{tot} 根 1H")

    # ---------------- H1+H2 入场 × 出场 矩阵 ----------------
    head("H1/H2 入场模式 × 出场模式（atr1.5，max_bars=480≈20天）")
    grid = {}
    for em, en in (("revert", "触超卖即进"), ("recover", "破超卖后站回"),
                   ("divergence", "底背离+站回"), ("momentum", "顺势反向")):
        for xm, xn in (("rsi", "1H回中轴"), ("daily_rsi", "日线RSI70"), ("ma", "收回MA20")):
            kw = dict(entry_mode=em, exit_mode=xm, atr_k=1.5, max_bars=480, os_th=30, ob_th=70)
            if xm == "daily_rsi":
                kw["daily_ob"] = 70.0
            tr = collect(data, **kw)
            d = decompose(tr)
            grid[(em, xm)] = (tr, d)
            line(f"{en} → {xn}", d, 26)

    # ---------------- H3 放大止损 ----------------
    head("H3 放大止损（入场=破超卖后站回，出场=日线RSI70）")
    best_em = "recover"
    for k in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0):
        tr = collect(data, entry_mode=best_em, exit_mode="daily_rsi",
                     atr_k=k, max_bars=480, os_th=30, ob_th=70, daily_ob=70)
        d = decompose(tr)
        line(f"atr_k = {k}", d, 26)

    head("H3b 放大止损（入场=底背离+站回）")
    for k in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0):
        tr = collect(data, entry_mode="divergence", exit_mode="daily_rsi",
                     atr_k=k, max_bars=480, os_th=30, ob_th=70, daily_ob=70)
        d = decompose(tr)
        line(f"atr_k = {k}", d, 26)

    # ---------------- H2b 日线超买阈值 ----------------
    head("H2b 出场：日线 RSI 超买阈值扫描（入场=破超卖后站回，atr1.5）")
    for ob in (55, 60, 65, 70, 75, 80, 85):
        tr = collect(data, entry_mode=best_em, exit_mode="daily_rsi",
                     atr_k=1.5, max_bars=480, os_th=30, ob_th=70, daily_ob=ob)
        d = decompose(tr)
        line(f"日线超买 {ob}", d, 26)

    # ---------------- max_bars 敏感性 ----------------
    head("H2c 最长持仓上限（出场=日线RSI70，atr1.5，入场=破超卖后站回）")
    for mb in (48, 96, 168, 240, 480, 720):
        tr = collect(data, entry_mode=best_em, exit_mode="daily_rsi",
                     atr_k=1.5, max_bars=mb, os_th=30, ob_th=70, daily_ob=70)
        d = decompose(tr)
        line(f"max_bars = {mb} ({mb/24:.0f}天)", d, 26)

    # ---------------- H4 按日线状态 ----------------
    head("H4 按日线状态拆解（入场=破超卖后站回 → 日线RSI70，atr1.5）")
    tr = collect(data, entry_mode=best_em, exit_mode="daily_rsi",
                 atr_k=1.5, max_bars=480, os_th=30, ob_th=70, daily_ob=70)
    for rg in ("UP", "RANGE", "DOWN"):
        sub = [t for t in tr if t["regime"] == rg]
        line(f"{rg}", decompose(sub), 26)
    print("\n  按方向再拆：")
    for rg in ("UP", "RANGE", "DOWN"):
        for sd, sn in ((1, "多"), (-1, "空")):
            sub = [t for t in tr if t["regime"] == rg and t["side"] == sd]
            if sub:
                line(f"  {rg} · {sn}", decompose(sub), 24)

    # ---------------- 出场原因分布 ----------------
    head("出场原因分布（这套配置到底靠什么离场）")
    d = decompose(tr)
    for rea, cnt in d["reasons"].most_common():
        rr = [t["r"] for t in tr if t["reason"] == rea]
        print(f"  {rea:<12} {cnt:>5} 笔 ({cnt/len(tr)*100:>5.1f}%)  均 {statistics.mean(rr):>+7.2f}R")

    # ---------------- 按年 ----------------
    head("按年（入场=破超卖后站回 → 日线RSI70，atr1.5）")
    years = sorted({t["date"][:4] for t in tr})
    for y in years:
        sub = [t for t in tr if t["date"][:4] == y]
        d2 = decompose(sub)
        if d2:
            line(y, d2, 26)

    # ---------------- 收益集中度 ----------------
    head("收益集中度（剔除最大 N 笔后还剩什么）")
    for em, xn, k in (("recover", "daily_rsi", 1.5), ("divergence", "daily_rsi", 1.5),
                      ("divergence", "daily_rsi", 2.0), ("revert", "daily_rsi", 1.5)):
        t2 = collect(data, entry_mode=em, exit_mode=xn, atr_k=k, max_bars=480,
                     os_th=30, ob_th=70, daily_ob=70)
        rs = sorted([t["r"] for t in t2], reverse=True)
        base = sum(rs) / len(rs)
        out = [f"  {em}+{xn} k={k:<4} n={len(rs):<5} 全样本 {base:>+6.3f}R"]
        for m in (1, 5, 10, 20):
            out.append(f"剔{m}笔 {sum(rs[m:])/(len(rs)-m):>+6.3f}R")
        print("  ".join(out))

    # ---------------- 对照 4H 基准 ----------------
    head("与 4H 基准对照（4H 回调结束形态：2095 笔 +0.346R/笔 PF1.69）")
    for em, en in (("recover", "破超卖后站回"), ("divergence", "底背离+站回")):
        for k in (1.5, 2.0, 3.0):
            t2 = collect(data, entry_mode=em, exit_mode="daily_rsi", atr_k=k,
                         max_bars=480, os_th=30, ob_th=70, daily_ob=70)
            d2 = decompose(t2)
            gap = d2["avgR"] - 0.346
            print(f"  {en} k={k}: 毛 {d2['avgR']:>+6.3f}R  净 {d2['netR']:>+6.3f}R  "
                  f"vs 4H {gap:>+6.3f}R")
    return grid


if __name__ == "__main__":
    main()
