#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""什么策略才适配这套日线框架？

前几轮已经确认的三件事：
  1. 1H RSI 择时没有信息量（随机入场在四个格子里全部 >= 真实信号）
  2. 4H 形态策略有真 alpha（4H 随机 -0.121R / PF 0.77 vs 真实 +0.346R）
  3. 不带信号的「止损+持有」结构，Calmar 天花板 0.73 ≈ buy&hold 0.72
  4. 唯一跑赢基准的是「日线状态机仓位映射」UP1/RANGE1/DOWN0 → Calmar 0.87

本脚本回答「框架适配性」这个设计问题，三问三答：

  S1  框架的状态持续多久？—— 决定入场信号该用多长的周期（周期匹配律）
  S2  仓位管理能不能再挖？—— 波动率目标化（risk parity 式缩放的单资产版）
  S3  4H 趋势延续信号 + 日线定方向，还有 alpha 吗？—— 与随机入场对照

S3 的口径刻意做成「最适配框架」的样子：
  入场 = 日线状态允许 + 4H 突破 N 根极值（趋势延续，不是均值回归）
  出场 = **日线状态翻转即平**（状态机作为主出场，不设盈利目标）
  这样如果它还不如随机，就说明该放弃在这个框架上挂择时信号。

用法：python test_regime_fit.py
"""
import sys
import os
import math
import random
import statistics
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_1h_struct as ST
import test_1h_v2 as V2
from _proto_4h import atr_wilder, sma

COST = 0.0004          # 单边
DAYS_YR = 365


# ---------------------------------------------------------------- S1
def state_spans(regime1h):
    """把逐 bar 状态压成 (状态, 持续 bar 数) 序列。"""
    out = []
    cur, n = None, 0
    for r in regime1h:
        if r not in ("UP", "RANGE", "DOWN"):
            continue
        if r == cur:
            n += 1
        else:
            if cur:
                out.append((cur, n))
            cur, n = r, 1
    if cur:
        out.append((cur, n))
    return out


def s1_duration(data):
    print("=" * 108)
    print("S1  日线状态的持续期 —— 决定「入场信号应该用多长的周期」")
    print("=" * 108)
    allsp = []
    for c, (b, rg, _dr, _d) in data.items():
        sp = state_spans(rg)
        allsp += sp
    by = defaultdict(list)
    for s, n in allsp:
        by[s].append(n / 24.0)      # 1H bar → 天
    print(f"  {'状态':<8} {'段数':>6} {'中位(天)':>10} {'均值(天)':>10} "
          f"{'P25':>7} {'P75':>7} {'P90':>7} {'最长':>8}")
    print("  " + "-" * 84)
    for s in ("UP", "RANGE", "DOWN"):
        v = sorted(by[s])
        if not v:
            continue
        q = lambda p: v[min(len(v) - 1, int(len(v) * p))]
        print(f"  {s:<8} {len(v):>6} {statistics.median(v):>10.1f} "
              f"{statistics.mean(v):>10.1f} {q(.25):>7.1f} {q(.75):>7.1f} "
              f"{q(.90):>7.1f} {max(v):>8.1f}")
    allv = sorted(n for _s, n in allsp)
    med = statistics.median(allv) / 24.0
    print(f"\n  全部状态段中位持续 **{med:.1f} 天**（{med*24:.0f} 根 1H）")
    print(f"  → 一个状态里有多少个「决策点」：1H 有 {med*24:.0f} 个、4H 有 {med*6:.0f} 个、"
          f"日线只有 {med:.0f} 个")
    print(f"    决策点越多，越容易在状态内部拟合噪音 —— 这正是 1H 择时失效的机制。")
    print(f"  → 实测持仓长度：1H 信号 39 根 ≈ 1.6 天（占状态段 {1.6/med*100:.0f}%）；"
          f"4H 信号 31 根 ≈ 5.2 天（占 {5.2/med*100:.0f}%）")


# ---------------------------------------------------------------- S2
def s2_vol_target(data):
    """日线状态定方向，波动率目标定杠杆。"""
    print("\n" + "=" * 108)
    print("S2  日线状态 × 波动率目标仓位（能不能突破 0.87 的 Calmar）")
    print("=" * 108)

    tb = sum(len(v[0]) for v in data.values())
    bh = ST.metrics(ST.bh_curve(data))
    print(f"  基准：等权 buy&hold  CAGR {bh['cagr']*100:+.1f}%  "
          f"MDD {bh['mdd']*100:.1f}%  Calmar {bh['calmar']:.2f}\n")
    print(f"  {'映射':<40} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} {'暴露':>7} {'杠杆均值':>9}")
    print("  " + "-" * 92)

    CASES = [
        ("UP1/RANGE1/DOWN0  固定仓位",     {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}, None, 0),
        ("UP1/RANGE1/DOWN0  vol目标 40%",  {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}, 0.40, 3),
        ("UP1/RANGE1/DOWN0  vol目标 50%",  {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}, 0.50, 3),
        ("UP1/RANGE1/DOWN0  vol目标 60%",  {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}, 0.60, 3),
        ("UP1/RANGE1/DOWN0  vol目标 60% 无上限", {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}, 0.60, 99),
        ("UP1/RANGE0.5/DOWN0 vol目标 50%", {"UP": 1.0, "RANGE": 0.5, "DOWN": 0.0}, 0.50, 3),
    ]

    for lbl, mp, tgt, cap in CASES:
        navs, held, lev_sum, lev_n, turns = {}, 0, 0.0, 0, 0
        for c, (b, rg, _dr, _d) in data.items():
            # 按真实 UTC 日期收盘聚合（不能每 24 根取一根，否则日期会漂）
            days, closes_d, rg_d = [], [], []
            for i, x in enumerate(b):
                if not days or days[-1] != x["date"]:
                    days.append(x["date"])
                    closes_d.append(x["close"])
                    rg_d.append(rg[i])
                else:
                    closes_d[-1] = x["close"]
            n = len(days)
            rets, pos = [0.0], []
            for k in range(n):
                if k:
                    rets.append(closes_d[k] / closes_d[k - 1] - 1)
                w = mp.get(rg_d[k], 0.0)
                if tgt is None:
                    pos.append(w)
                else:
                    win = rets[max(0, k - 20):k + 1]
                    if len(win) >= 10:
                        v = statistics.pstdev(win) * math.sqrt(DAYS_YR)
                        lev = min(cap, tgt / v) if v > 0 else 0.0
                    else:
                        lev = 0.0
                    pos.append(w * lev)
            nav, curve = 1.0, []
            for k in range(1, n):
                x = 1 + pos[k] * rets[k]
                nav *= x if x > 0 else 0.0
                if pos[k] != pos[k - 1]:
                    nav *= (1 - COST * abs(pos[k] - pos[k - 1]))
                    turns += 1
                held += pos[k]
                lev_sum += pos[k]
                lev_n += 1
                curve.append((days[k], nav))
            navs[c] = ST.daily(curve)
        m = ST.metrics(ST.combine(navs))
        expo = held / (lev_n or 1)
        levm = lev_sum / (lev_n or 1)
        print(f"  {lbl:<40} {m['cagr']*100:>+7.1f}% {m['mdd']*100:>7.1f}% "
              f"{m['calmar']:>7.2f} {expo*100:>6.1f}% {levm:>9.2f}")


# ---------------------------------------------------------------- S3
def agg4h(bars):
    out = []
    for i in range(0, len(bars) - 3, 4):
        seg = bars[i:i + 4]
        out.append({
            "date": seg[-1]["date"], "src": i + 3,
            "open": seg[0]["open"],
            "high": max(x["high"] for x in seg),
            "low": min(x["low"] for x in seg),
            "close": seg[-1]["close"],
        })
    return out


def run4h(bars, regime, lookback=20, atr_k=1.5, mode="breakout",
          random_seed=None, exit_on_flip=True, max_bars=120, cooldown=0):
    """4H 回测：日线状态定方向，4H 突破入场，状态翻转出场。

    mode:
      "breakout" 4H 收盘突破前 lookback 根极值（趋势延续）
      "always"   不设信号，状态允许就进（纯结构对照）
      "random"   同状态约束下随机时点（同频率对照）
    """
    b4 = agg4h(bars)
    # 4H bar 的日线状态：取它最后一根 1H 的 regime（已是对齐到「前一日」的值）
    rg4 = [regime[x["src"]] if x["src"] < len(regime) else None for x in b4]
    atr = atr_wilder(b4, 14)
    rnd = random.Random(random_seed)
    n = len(b4)

    mode = "random" if random_seed is not None else mode
    trades = []
    i = max(lookback + 20, 30)
    # 随机模式：先匹配真实信号的入场频率
    if mode == "random":
        n_sig = 0
        for k in range(i, n):
            if rg4[k] not in ("UP", "DOWN"):
                continue
            if k < lookback:
                continue
            hi = max(x["high"] for x in b4[k - lookback:k])
            lo = min(x["low"] for x in b4[k - lookback:k])
            if (rg4[k] == "UP" and b4[k]["close"] > hi) or \
               (rg4[k] == "DOWN" and b4[k]["close"] < lo):
                n_sig += 1
        n_elig = sum(1 for k in range(i, n) if rg4[k] in ("UP", "DOWN"))
        p_enter = min(1.0, n_sig / n_elig) if n_elig else 0.0
    else:
        p_enter = 1.0

    while i < n - 1:
        rg = rg4[i]
        if rg not in ("UP", "DOWN"):
            i += 1
            continue
        if atr[i] is None:
            i += 1
            continue
        side = 1 if rg == "UP" else -1
        if b4[i]["date"] < "2018-01-01":
            i += 1
            continue

        if mode == "breakout":
            hi = max(x["high"] for x in b4[i - lookback:i])
            lo = min(x["low"] for x in b4[i - lookback:i])
            ok = (side == 1 and b4[i]["close"] > hi) or \
                 (side == -1 and b4[i]["close"] < lo)
            if not ok:
                i += 1
                continue
        elif mode == "random":
            if rnd.random() > p_enter:
                i += 1
                continue

        entry = b4[i]["close"]
        stop = entry - side * atr_k * atr[i]
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue

        exit_px = exit_j = None
        reason = None
        for j in range(i + 1, min(i + max_bars + 1, n)):
            if side == 1 and b4[j]["low"] <= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            if side == -1 and b4[j]["high"] >= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            if exit_on_flip and rg4[j] != rg:
                exit_px, exit_j, reason = b4[j]["close"], j, "flip"
                break
        if exit_px is None:
            exit_j = min(i + max_bars, n - 1)
            exit_px, reason = b4[exit_j]["close"], "timeout"

        trades.append({
            "i": i, "j": exit_j, "side": side, "regime": rg,
            "r": side * (exit_px - entry) / risk,
            "bars_held": exit_j - i,
            "stop_pct": risk / entry,
            "reason": reason,
        })
        i = exit_j + 1 + cooldown
    return trades, n


def agg(trades, bars4_n):
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    costs = [COST * 2 / t["stop_pct"] for t in trades]
    nets = [r - c for r, c in zip(rs, costs)]
    w = [x for x in nets if x > 0]
    l = [x for x in nets if x <= 0]
    srt = sorted(nets, reverse=True)
    bars = sum(t["bars_held"] for t in trades)
    return {
        "n": len(nets), "win": len(w) / len(nets),
        "sumR": sum(rs), "netR": sum(nets) / len(nets),
        "pf": (sum(w) / abs(sum(l))) if l and sum(l) else None,
        "bars": bars / len(nets),
        "eff": sum(nets) / bars * 100 if bars else 0,
        "ex3": sum(srt[3:]) / len(srt[3:]) if len(srt) > 3 else 0,
        "top1_share": (srt[0] / sum(nets)) if sum(nets) > 0 else None,
        "reasons": dict(Counter(t["reason"] for t in trades)),
    }


def s3_breakout(data):
    print("\n" + "=" * 108)
    print("S3  4H 趋势延续 + 日线定方向 + 状态翻转出场 —— 还有 alpha 吗")
    print("=" * 108)
    print(f"  {'变体':<34} {'笔数':>5} {'胜率':>6} {'净/笔':>7} {'PF':>5} "
          f"{'持仓':>5} {'每100根':>8} {'剔3笔':>8} {'出场':<22}")
    print("  " + "-" * 106)

    LAB = [
        ("突破20 · 状态翻转出场", "breakout", None, True),
        ("突破10 · 状态翻转出场", "breakout", None, True),   # lookback 单独传
        ("无信号 · 状态翻转出场", "always",   None, True),
        ("随机入场 · 状态翻转出场", "random", 42,   True),
    ]
    alltr = {}
    for lbl, mode, seed, flip in LAB:
        lb = 10 if lbl.startswith("突破10") else 20
        total = []
        for c, (b, rg, _dr, _d) in data.items():
            tr, _n4 = run4h(b, rg, lookback=lb, mode=mode, random_seed=seed,
                            exit_on_flip=flip)
            total += tr
        a = agg(total, 0)
        alltr[lbl] = a
        if not a:
            print(f"  {lbl:<34} 无交易")
            continue
        pf = f"{a['pf']:.2f}" if a["pf"] else "  — "
        share = f"{a['top1_share']*100:>3.0f}%" if a["top1_share"] else "   —"
        rs = {k: v for k, v in a["reasons"].items()}
        print(f"  {lbl:<34} {a['n']:>5} {a['win']*100:>5.1f}% {a['netR']:>+7.3f} "
              f"{pf:>5} {a['bars']:>5.0f} {a['eff']:>+8.3f} {a['ex3']:>+8.3f} {str(rs):<22}")

    # 关键对照
    rk = "突破20 · 状态翻转出场"
    ok = "随机入场 · 状态翻转出场"
    nk = "无信号 · 状态翻转出场"
    if alltr.get(rk) and alltr.get(ok):
        real, rnd = alltr[rk]["netR"], alltr[ok]["netR"]
        print(f"\n  **决定性对照**：真实突破 {real:+.3f}R  vs  随机入场 {rnd:+.3f}R  "
              f"→ {'真实信号有 alpha' if real > rnd else '随机更好，突破信号无增量'}")
    if alltr.get(nk):
        print(f"  **结构对照**：无信号 {alltr[nk]['netR']:+.3f}R  "
              f"（每100根 {alltr[nk]['eff']:+.3f}R）")

    # 逐品种
    print(f"\n  逐品种（突破20 / 状态翻转出场）—— 看是不是靠 1-2 个币撑起来的：")
    per = {}
    for c, (b, rg, _dr, _d) in data.items():
        tr, _n4 = run4h(b, rg, lookback=20, mode="breakout", exit_on_flip=True)
        a = agg(tr, 0)
        per[c] = a
    print(f"    {'品种':<6} {'笔数':>5} {'净/笔':>7} {'PF':>5} {'每100根':>8} {'剔3笔':>8}")
    pos_n = 0
    for c in sorted(per, key=lambda x: -(per[x]["netR"] if per[x] else -99)):
        a = per[c]
        if not a:
            continue
        pf = f"{a['pf']:.2f}" if a["pf"] else "  — "
        if a["netR"] > 0:
            pos_n += 1
        print(f"    {c:<6} {a['n']:>5} {a['netR']:>+7.3f} {pf:>5} "
              f"{a['eff']:>+8.3f} {a['ex3']:>+8.3f}")
    print(f"    → {pos_n}/{len(per)} 个品种为正")

    # 分年（按出场年份归集）
    print(f"\n  分年（突破20 / 状态翻转出场）：")
    yr = defaultdict(list)
    for lbl_i, (b, rg, _dr, _d) in data.items():
        b4 = agg4h(b)
        tr, _n4 = run4h(b, rg, lookback=20, mode="breakout", exit_on_flip=True)
        for t in tr:
            yr[b4[t["j"]]["date"][:4]].append(t)
    print(f"    {'年份':<6} {'笔数':>5} {'净/笔':>7} {'PF':>5}")
    for y in sorted(yr):
        a = agg(yr[y], 0)
        if not a or a["n"] < 5:
            continue
        pf = f"{a['pf']:.2f}" if a["pf"] else "  — "
        print(f"    {y:<6} {a['n']:>5} {a['netR']:>+7.3f} {pf:>5}")


def s4_surface(data):
    """参数面扫描：判断是「平台」还是「孤峰」。孤峰一律视为过拟合。"""
    print("\n" + "=" * 108)
    print("S4  参数面扫描（突破 N 根 × 止损 ATR 倍数）—— 找平台，不找峰值")
    print("=" * 108)
    LBS = [10, 20, 40]
    AKS = [1.0, 1.5, 2.0, 2.5, 3.0]
    print(f"  {'止损\\回看':<10}" + "".join(f"{'N='+str(l):>12}" for l in LBS))
    print("  " + "-" * 46)
    grid = {}
    for ak in AKS:
        cells = []
        for lb in LBS:
            total = []
            for c, (b, rg, _dr, _d) in data.items():
                tr, _n4 = run4h(b, rg, lookback=lb, atr_k=ak,
                                mode="breakout", exit_on_flip=True)
                total += tr
            a = agg(total, 0)
            grid[(ak, lb)] = a
            cells.append(f"{a['netR']:>+7.3f}/{a['n']:>4}" if a else "      —")
        print(f"  {ak:<10.1f}" + "".join(f"{c:>12}" for c in cells))
    print("  （单元格 = 净R/笔 / 笔数）")

    flat = [(v["netR"], k) for k, v in grid.items() if v and v["n"] >= 200]
    if flat:
        flat.sort(reverse=True)
        best = flat[0]
        pos_n = sum(1 for v, _k in flat if v > 0)
        print(f"\n  {len(flat)} 个组合中 {pos_n} 个为正；最好 {best[0]:+.3f}R "
              f"(ATR {best[1][0]}×回看{best[1][1]})，最差 {flat[-1][0]:+.3f}R")
        print(f"  正组合占比 {pos_n/len(flat)*100:.0f}% —— "
              f"{'存在连续平台，不是孤峰' if pos_n/len(flat) > 0.6 else '大面积失效，需谨慎'}")


def expo4(trades, n_bars_1h):
    """4H 持仓根数 ×4 折算成 1H 暴露。"""
    return sum(t["bars_held"] * 4 for t in trades) / n_bars_1h if n_bars_1h else 0


def nav4h(bars, trades, cost=COST):
    """4H 交易的满仓净值（持仓区间按 4H 收盘价复利，调仓扣成本）。"""
    b4 = agg4h(bars)
    n = len(b4)
    pos = [0] * n
    for t in trades:
        for k in range(t["i"] + 1, min(t["j"] + 1, n)):
            pos[k] = t["side"]
    nav, curve = 1.0, []
    for k in range(1, n):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        if pos[k] != pos[k - 1]:
            nav *= (1 - cost * abs(pos[k] - pos[k - 1]))
        curve.append((b4[k]["date"], nav))
    return ST.daily(curve)


def s5_netvalue(data):
    """把 R 口径翻成组合净值口径，与 buy&hold / 日线仓位映射对表。

    这一步不能省：净 R/笔 越高不代表净值越好——止损越窄笔数越多，
    总 R 会变大，但单笔占用时间变短、单位时间效率可能下降，
    而且实盘要按 R 风险预算折算仓位，必须看复利曲线。
    """
    print("\n" + "=" * 108)
    print("S5  组合净值口径（每品种满仓 → 日频等权），与两个基准对表")
    print("=" * 108)
    tb = sum(len(v[0]) for v in data.values())
    bh = ST.metrics(ST.bh_curve(data))
    print(f"  {'方案':<44} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} {'暴露':>7}")
    print("  " + "-" * 82)
    print(f"  {'等权 buy&hold':<44} {bh['cagr']*100:>+7.1f}% {bh['mdd']*100:>7.1f}% "
          f"{bh['calmar']:>7.2f} {100.0:>6.1f}%")

    # 基准：纯日线仓位映射
    navs = {}
    held = 0
    ndays_all = 0
    for c, (b, rg, _dr, _d) in data.items():
        days, cd, rd = [], [], []
        for i, x in enumerate(b):
            if not days or days[-1] != x["date"]:
                days.append(x["date"]); cd.append(x["close"]); rd.append(rg[i])
            else:
                cd[-1] = x["close"]
        pos = [{"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}.get(r, 0.0) for r in rd]
        ndays_all += len(pos)
        nav, curve = 1.0, []
        for k in range(1, len(days)):
            r = cd[k] / cd[k - 1] - 1
            x = 1 + pos[k] * r
            nav *= x if x > 0 else 0.0
            if pos[k] != pos[k - 1]:
                nav *= (1 - COST * abs(pos[k] - pos[k - 1]))
            held += pos[k]
            curve.append((days[k], nav))
        navs[c] = ST.daily(curve)
    m = ST.metrics(ST.combine(navs))
    print(f"  {'日线仓位映射 UP1/RANGE1/DOWN0':<44} {m['cagr']*100:>+7.1f}% "
          f"{m['mdd']*100:>7.1f}% {m['calmar']:>7.2f} {held/(ndays_all or 1)*100:>6.1f}%")

    # 4H 突破策略，几个止损档
    for ak in (1.0, 1.5, 2.0):
        navs, tr_all = {}, []
        for c, (b, rg, _dr, _d) in data.items():
            tr, _n4 = run4h(b, rg, lookback=20, atr_k=ak,
                            mode="breakout", exit_on_flip=True)
            tr_all += tr
            navs[c] = nav4h(b, tr)
        m = ST.metrics(ST.combine(navs))
        a = agg(tr_all, 0)
        print(f"  {'4H突破20·状态翻转·'+str(ak)+'ATR':<44} {m['cagr']*100:>+7.1f}% "
              f"{m['mdd']*100:>7.1f}% {m['calmar']:>7.2f} "
              f"{expo4(tr_all, tb)*100:>6.1f}%"
              f"   (净{a['netR']:+.3f}R/笔, {a['n']}笔)")

    # 随机入场对照的净值
    navs, tr_all = {}, []
    for c, (b, rg, _dr, _d) in data.items():
        tr, _n4 = run4h(b, rg, lookback=20, mode="breakout", random_seed=42,
                        exit_on_flip=True)
        tr_all += tr
        navs[c] = nav4h(b, tr)
    m = ST.metrics(ST.combine(navs))
    print(f"  {'[对照] 4H随机入场·1.5ATR':<44} {m['cagr']*100:>+7.1f}% "
          f"{m['mdd']*100:>7.1f}% {m['calmar']:>7.2f} "
          f"{expo4(tr_all, tb)*100:>6.1f}%")


def s6_overlay(data):
    """把 4H 突破当成「叠加层」而不是「替代品」。

    S5 显示 4H 突破单干时 Calmar 只有 0.45（暴露 33%，大半时间空仓），
    而日线仓位映射是 0.88。但 4H 突破确实有 alpha（+0.478 vs 随机 +0.292）。
    两者不该二选一：日线给底仓（beta + 尾部过滤），4H 给战术加减。

      A 型：底仓 + L × 信号仓（突破成立时加杠杆）
      B 型：底仓 × 系数（没突破时降仓，只在突破时满仓）
    """
    print("\n" + "=" * 108)
    print("S6  4H 突破作为叠加层（底仓 = 日线状态映射）")
    print("=" * 108)
    BASE = {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}

    print(f"  {'方案':<46} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} {'暴露':>7} {'杠杆':>6}")
    print("  " + "-" * 88)

    def run(lbl, kind, L):
        navs, expo_s, expo_n, lev_s = {}, 0.0, 0, 0.0
        for c, (b, rg, _dr, _d) in data.items():
            tr, _n4 = run4h(b, rg, lookback=20, atr_k=1.5,
                            mode="breakout", exit_on_flip=True)
            b4 = agg4h(b)
            n4 = len(b4)
            rg4 = [rg[x["src"]] for x in b4]
            sig = [0] * n4
            for t in tr:
                for k in range(t["i"] + 1, min(t["j"] + 1, n4)):
                    sig[k] = t["side"]
            pos = []
            for k in range(n4):
                w = BASE.get(rg4[k], 0.0)
                if kind == "add":
                    pos.append(w + L * sig[k])
                else:                       # "cut"：无信号时把底仓打折扣
                    pos.append(w * (L if sig[k] == 0 else 1.0))
            nav, curve = 1.0, []
            for k in range(1, n4):
                r = b4[k]["close"] / b4[k - 1]["close"] - 1
                x = 1 + pos[k] * r
                nav *= x if x > 0 else 0.0
                if pos[k] != pos[k - 1]:
                    nav *= (1 - COST * abs(pos[k] - pos[k - 1]))
                expo_s += abs(pos[k]); expo_n += 1
                lev_s += abs(pos[k])
                curve.append((b4[k]["date"], nav))
            navs[c] = ST.daily(curve)
        m = ST.metrics(ST.combine(navs))
        print(f"  {lbl:<46} {m['cagr']*100:>+7.1f}% {m['mdd']*100:>7.1f}% "
              f"{m['calmar']:>7.2f} {expo_s/expo_n*100:>6.1f}% {lev_s/expo_n:>6.2f}")
        return m

    run("底仓（日线映射，无 4H）", "add", 0.0)
    for L in (0.5, 1.0, 1.5, 2.0):
        run(f"A 底仓 + {L}×突破加仓", "add", L)
    for L in (0.5, 0.3):
        run(f"B 底仓 × {L}（无突破时降仓）", "cut", L)
    print("\n  读法：Calmar > 0.88 才算真的改善了风险调整收益，"
          "只把 CAGR 做高不算（那是加杠杆买风险）。")


def main():
    data = ST.load()
    print(f"品种数 {len(data)}：{' '.join(sorted(data))}\n")
    s1_duration(data)
    s2_vol_target(data)
    s3_breakout(data)
    s4_surface(data)
    s5_netvalue(data)
    s6_overlay(data)


if __name__ == "__main__":
    main()
