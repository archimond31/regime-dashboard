#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1H RSI 均值回归策略引擎（用户自定义规则的实现）。

规则（用户指定）：
  - 框架：日线状态机定方向（与面板同一套，ma_slow=120）
  - UP    → 只开多
  - DOWN  → 只开空
  - RANGE → 多空双开
  - 开多触发：1H RSI 触及超卖
  - 开空触发：1H RSI 触及超买

用户未指定的部分（必须由测试决定，不能拍脑袋）：
  - 出场条件 exit_mode: "rsi"（RSI 回到中轴）/ "ma"（收盘收回 MA20）/ "tp"（固定 R 目标）
  - 止损宽度 atr_k
  - 最长持仓 max_bars

「触及」的实现口径：
  rsi[i] <= 阈值 且 rsi[i-1] > 阈值 —— 即首次下穿阈值的那一根，
  而不是「停留在超卖区的每一根」，否则同一段超卖会连续开仓。
"""
from _proto_4h import rsi_wilder, sma


def regime_series(daily, ma_slow=120):
    """日线状态序列（面板同一套状态机）。"""
    from visualize_btc_regime import daily_states
    dates = [r["date"] for r in daily]
    closes = [r["close"] for r in daily]
    st, _ma, _tg = daily_states(closes, ma_slow, 70, 30, True, 2, True)
    return {d: s for d, s in zip(dates, st) if s in ("UP", "RANGE", "DOWN")}


def align_regime_safe(bars, reg_map):
    """1H/4H bar → 「严格早于其日期的最后一根日线」确认的状态。无未来函数。

    注意：同一 UTC 日期内的 24 根 1H bar 共用前一日的状态——这是正确的，
    因为该日的日线要到当日 24:00 UTC 才收盘。
    """
    import bisect
    days = sorted(reg_map)
    out = []
    for b in bars:
        i = bisect.bisect_left(days, b["date"]) - 1
        out.append(reg_map[days[i]] if i >= 0 else None)
    return out


def precompute_divergence(bars, rsi, div_th=30.0, window=60, pad=2):
    """找 RSI 背离拐点。

    多头背离（底背离）：后一个 RSI 低点比前一个更低，但价格低点比前一个更高。
      注意方向别搞反——底背离是「价格创新低、RSI 没创新低」，
      所以 rsi[k] > rsi[p] 且 lows[k] < lows[p]。
    空头背离（顶背离）：rsi[k] < rsi[p] 且 highs[k] > highs[p]。

    返回 (bull, bear)：两个集合，元素是「背离拐点」的 bar 下标。
    """
    n = len(rsi)
    lows = [b["low"] for b in bars]
    highs = [b["high"] for b in bars]
    troughs, peaks = [], []
    for i in range(pad, n - pad):
        if rsi[i] is None:
            continue
        seg = rsi[i - pad:i + pad + 1]
        if any(v is None for v in seg):
            continue
        if rsi[i] <= div_th and rsi[i] == min(seg):
            troughs.append(i)
        if rsi[i] >= 100 - div_th and rsi[i] == max(seg):
            peaks.append(i)

    bull, bear = set(), set()
    for lst, is_bull in ((troughs, True), (peaks, False)):
        for a, b in zip(lst, lst[1:]):
            if b - a > window:
                continue
            if is_bull:
                if lows[b] < lows[a] and rsi[b] > rsi[a]:
                    bull.add(b)
            else:
                if highs[b] > highs[a] and rsi[b] < rsi[a]:
                    bear.add(b)
    return bull, bear


def align_daily_rsi(bars, daily_rsi_map):
    """1H bar → 「严格早于其日期的最后一根日线」的日线 RSI。无未来函数。"""
    import bisect
    days = sorted(daily_rsi_map)
    out = []
    for b in bars:
        i = bisect.bisect_left(days, b["date"]) - 1
        out.append(daily_rsi_map[days[i]] if i >= 0 else None)
    return out


def run_rsi(bars, regime, allow_regime=True,
            os_th=30.0, ob_th=70.0,
            atr_k=1.5, exit_mode="rsi", rsi_mid=50.0,
            ma_exit=20, max_bars=48, target_r=2.0,
            random_seed=None, entry_mode="revert",
            daily_rsi=None, daily_ob=70.0, div_window=60,
            exit_rsi_mid=50.0, force_side=None, cooldown=0,
            allow_mode="trend"):
    """逐根回放 1H bar。

    entry_mode:
      "revert"     触及超卖就在该根收盘做多（均值回归，原规则）
      "momentum"   反向：超买做多、超卖做空（顺势动量）
      "recover"    破超卖后「重新站回」阈值才做多（等回归确认）
      "divergence" 1H RSI 背离拐点 + 回归确认才入场
      "always"     不设任何信号条件，每根 bar 都可开仓（消融用：
                   检验「止损+长持有」这个结构本身值多少钱，与入场无关）
                   force_side=1 时只做多；None 时按 allow 的第一个方向。

    allow_mode（方向与日线状态的关系）:
      "trend"        顺日线：UP 只多 / DOWN 只空 / RANGE 双开（默认）
      "trend_flat"   顺日线且 RANGE 空仓：UP 只多 / DOWN 只空 / RANGE 不做
                     ——「日线 UP 里 1H 超卖回归做多」正是这一档
      "contrarian"   逆日线：UP 只空 / DOWN 只多 / RANGE 空仓
      "both"         三个状态都双开

    exit_mode:
      "rsi"        1H RSI 回到 rsi_mid
      "ma"         1H 收盘收回 MA20
      "tp"         固定 R 目标
      "daily_rsi"  日线 RSI 触及超买（多单）/ 超卖（空单）——用户提议
    """
    import random
    rnd = random.Random(random_seed)
    n = len(bars)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    rsi = rsi_wilder(closes, 14)
    from _proto_4h import atr_wilder
    atr = atr_wilder(bars, 14)
    ma = sma(closes, ma_exit)
    bull_div, bear_div = (set(), set())
    if entry_mode == "divergence":
        bull_div, bear_div = precompute_divergence(bars, rsi, div_th=os_th,
                                                   window=div_window)

    start = max(ma_exit, 20) + 2
    trades = []
    i = start
    # 记录上一次离场位置，保证同一时刻只持一单
    while i < n - 1:
        rg = regime[i] if allow_regime else "ANY"
        if allow_regime and rg not in ("UP", "RANGE", "DOWN"):
            i += 1
            continue
        if rsi[i] is None or rsi[i - 1] is None or atr[i] is None or ma[i] is None:
            i += 1
            continue

        # ---- 允许的方向 ----
        if allow_mode == "contrarian":
            # 逆日线：UP 里只做空、DOWN 里只做多、RANGE 不做
            if rg == "UP":
                allow = (-1,)
            elif rg == "DOWN":
                allow = (1,)
            elif rg == "RANGE":
                allow = ()
            else:                   # ANY（未启用状态机时）
                allow = (1, -1)
        elif allow_mode == "trend_flat":
            # 顺日线，但 RANGE 不做（用户规则只定义了 UP 与 DOWN 两个区间）
            if rg == "UP":
                allow = (1,)
            elif rg == "DOWN":
                allow = (-1,)
            elif rg == "RANGE":
                allow = ()
            else:
                allow = (1, -1)
        elif allow_mode == "both":
            allow = (1, -1)
        else:
            if rg == "UP":
                allow = (1,)
            elif rg == "DOWN":
                allow = (-1,)
            else:                   # RANGE 或 ANY
                allow = (1, -1)

        if not allow:
            i += 1
            continue

        # 日线 RSI 出场时：已在出场区就别开新仓（否则一开就平）
        if exit_mode == "daily_rsi" and daily_rsi is not None:
            dr = daily_rsi[i]
            if dr is not None:
                if 1 in allow and dr >= daily_ob:
                    allow = tuple(a for a in allow if a != 1)
                if -1 in allow and dr <= 100 - daily_ob:
                    allow = tuple(a for a in allow if a != -1)

        def _near_div(s, idx, k=3):
            """背离拐点必须在 idx 之前「已确认」才能用。

            precompute_divergence 判定拐点 d 是局部极值时，用了 rsi[d-pad:d+pad+1]，
            即需要看到 d+pad 那根。所以最早只能在 idx >= d+pad 时才允许入场，
            否则就是拿未来数据（本脚本最初版本正是错在这里）。
            """
            src = bull_div if s == 1 else bear_div
            return any(2 <= idx - d <= k + 2 for d in src)

        side = 0
        if random_seed is not None:
            # 消融：时机随机，但方向仍受日线状态约束（否则逆日线/顺日线对照不公平）
            if rnd.random() < 0.02:
                side = rnd.choice(list(allow))
        elif entry_mode == "always":
            # 不设信号：只在允许的方向里挑一个，每根 bar 都尝试开仓
            if force_side is not None:
                side = force_side if force_side in allow else 0
            elif allow:
                side = allow[0]
        elif entry_mode == "momentum":
            # 顺势：超买做多、超卖做空
            if 1 in allow and rsi[i] >= ob_th and rsi[i - 1] < ob_th:
                side = 1
            elif -1 in allow and rsi[i] <= os_th and rsi[i - 1] > os_th:
                side = -1
        elif entry_mode == "recover":
            # 破超卖后重新站回阈值（等回归确认，晚一根进）
            if 1 in allow and rsi[i] > os_th >= rsi[i - 1]:
                side = 1
            elif -1 in allow and rsi[i] < ob_th <= rsi[i - 1]:
                side = -1
        elif entry_mode == "divergence":
            # 背离拐点后 3 根内出现「站回阈值」→ 确认入场
            if 1 in allow and rsi[i] > os_th >= rsi[i - 1] and _near_div(1, i):
                side = 1
            elif -1 in allow and rsi[i] < ob_th <= rsi[i - 1] and _near_div(-1, i):
                side = -1
        elif entry_mode == "either":
            # 用户的「OR」语义：回穿阈值 或 背离确认，任一成立即入场。
            # 注意背离不必伴随回穿——顶背离可能发生在 RSI 65 一带，
            # 若强制要求回穿 70，会漏掉一大半背离信号。
            if 1 in allow and ((rsi[i] > os_th >= rsi[i - 1]) or _near_div(1, i)):
                side = 1
            elif -1 in allow and ((rsi[i] < ob_th <= rsi[i - 1]) or _near_div(-1, i)):
                side = -1
        else:
            # 均值回归（原规则）：超卖做多、超买做空
            if 1 in allow and rsi[i] <= os_th and rsi[i - 1] > os_th:
                side = 1
            elif -1 in allow and rsi[i] >= ob_th and rsi[i - 1] < ob_th:
                side = -1

        if side == 0:
            i += 1
            continue

        entry = closes[i]
        stop = entry - side * atr_k * atr[i]
        risk = abs(entry - stop)
        if risk <= 0 or side * (entry - stop) <= 0:
            i += 1
            continue
        target = entry + side * target_r * risk if exit_mode == "tp" else None

        exit_px = exit_j = None
        reason = None
        for j in range(i + 1, min(i + max_bars + 1, n)):
            # 1) 硬止损（盘中）
            if side == 1 and lows[j] <= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            if side == -1 and highs[j] >= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            # 2) 止盈（盘中）
            if target is not None:
                if side == 1 and highs[j] >= target:
                    exit_px, exit_j, reason = target, j, "target"
                    break
                if side == -1 and lows[j] <= target:
                    exit_px, exit_j, reason = target, j, "target"
                    break
            # 3) 形态出场（收盘）
            if exit_mode == "rsi" and rsi[j] is not None:
                if (side == 1 and rsi[j] >= rsi_mid) or (side == -1 and rsi[j] <= 100 - rsi_mid):
                    exit_px, exit_j, reason = closes[j], j, "rsi_mid"
                    break
            elif exit_mode == "daily_rsi" and daily_rsi is not None:
                dr = daily_rsi[j]
                if dr is not None:
                    if side == 1 and dr >= daily_ob:
                        exit_px, exit_j, reason = closes[j], j, "daily_ob"
                        break
                    if side == -1 and dr <= 100 - daily_ob:
                        exit_px, exit_j, reason = closes[j], j, "daily_os"
                        break
            elif exit_mode == "ma":
                if (side == 1 and closes[j] > ma[j]) or (side == -1 and closes[j] < ma[j]):
                    exit_px, exit_j, reason = closes[j], j, "ma"
                    break
            # 4) 时间止损
            if j - i >= max_bars:
                exit_px, exit_j, reason = closes[j], j, "timeout"
                break
        if exit_px is None:
            exit_j = min(i + max_bars, n - 1)
            exit_px, reason = closes[exit_j], "eod"

        trades.append({
            "i": i, "j": exit_j, "side": side, "regime": rg,
            "date": bars[i]["date"], "time": bars[i]["t"],
            "r": side * (exit_px - entry) / risk,
            "reason": reason, "bars_held": exit_j - i,
            "entry_rsi": rsi[i],
            "entry": entry, "risk": risk,
            "stop_pct": risk / entry,
            "daily_rsi_at_entry": (daily_rsi[i] if daily_rsi is not None else None),
        })
        i = exit_j + 1 + cooldown
    return trades


def stats(trades):
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    peak = cum = 0.0
    mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "n": len(rs), "win": len(wins) / len(rs), "sumR": sum(rs),
        "avgR": sum(rs) / len(rs),
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "mddR": mdd,
        "avg_bars": sum(t["bars_held"] for t in trades) / len(trades),
    }
