#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""4H 策略原型验证（临时脚本，不入库）

在日线趋势监控的环境约束下设计 4H 策略：
  UP   → 只开多（趋势回调买入）
  RANGE→ 均值回归（区间边界反转）
  DOWN → 只开空（趋势反弹卖出）

同时做三组对照，回答两个问题：
  1) 这套设计本身能不能赚钱？
  2) 「日线环境约束」到底有没有用？（V0 有约束 vs V1 无约束 vs V2 随机）

关键：日线状态对 4H 的对齐必须无未来函数——4H bar 在日期 D 时，
只能使用 D-1 日收盘后确认的日线状态。
"""
import json
import statistics
import time
import urllib.request
from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HL = "https://api.hyperliquid.xyz/info"


def _post(payload):
    req = urllib.request.Request(HL, data=json.dumps(payload).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode())


def fetch_hl(coin, interval):
    d = _post({"type": "candleSnapshot",
               "req": {"coin": coin, "interval": interval,
                       "startTime": 0, "endTime": int(time.time() * 1000)}})
    out = []
    for c in d:
        out.append({
            "t": c["t"], "date": datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": float(c["o"]), "high": float(c["h"]), "low": float(c["l"]), "close": float(c["c"]),
        })
    return out


def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    g = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    if len(g) < n:
        return out
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr_wilder(bars, n=14):
    out = [None] * len(bars)
    if len(bars) < n + 1:
        return out
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    out[n] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i + 1] = a
    return out


def daily_regime_series(daily):
    """日线状态序列（复用项目状态机）。"""
    from visualize_btc_regime import daily_states
    dates = [r["date"] for r in daily]
    closes = [r["close"] for r in daily]
    import bisect
    s0 = bisect.bisect_left(dates, "2022-01-01")
    if s0 < 120:
        s0 = 120
    st, _div, _tg = daily_states(closes, s0, 70, 30, True, 2, True)
    return dict(zip(dates, st))


def align_regime(bars4h, reg_map):
    """4H bar → 可用的日线状态：取「上一日」收盘确认的状态（无未来函数）。"""
    out = []
    for b in bars4h:
        prev = datetime.strptime(b["date"], "%Y-%m-%d").toordinal() - 1
        prev_d = datetime.fromordinal(prev).strftime("%Y-%m-%d")
        out.append(reg_map.get(prev_d))
    return out


def run_strategy(bars, regime, allow_regime=True, random_seed=None,
                 rsi_pull=45, atr_k=1.5, ma_trend=60, ma_exit=20,
                 range_lookback=60, timing="signal",
                 stop_mode="atr", swing_lb=10, swing_buf=0.25):
    """逐根回放。返回交易列表，每笔含 R 倍数、方向、所属环境。

    timing="signal"  按 4H 形态入场（正式策略）
    timing="random"  方向听日线环境、入场时点随机（消融对照）

    stop_mode="atr"    止损 = 进场价 ∓ atr_k × ATR（默认，与历史结果一致）
    stop_mode="swing"  止损 = 回调段极值 ∓ swing_buf × ATR（结构性止损，
                       放在「回调低点被跌破则形态失效」的位置）
    """
    import random
    rnd = random.Random(random_seed)
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    rsi = rsi_wilder(closes)
    atr = atr_wilder(bars)
    ma_t = sma(closes, ma_trend)
    ma_e = sma(closes, ma_exit)

    start = max(ma_trend, range_lookback) + 20
    trades = []
    i = start
    n = len(bars)
    while i < n - 1:
        rg = regime[i] if allow_regime else "ANY"
        if allow_regime and rg not in ("UP", "RANGE", "DOWN"):
            i += 1
            continue
        if rsi[i] is None or atr[i] is None or ma_t[i] is None or ma_e[i] is None:
            i += 1
            continue

        side, kind = 0, None
        if timing == "random":
            # 消融：方向仍听日线环境，但入场时点随机（风控/离场规则照搬）
            if rnd.random() < 0.02:
                if rg == "UP":
                    side, kind = 1, "trend"
                elif rg == "DOWN":
                    side, kind = -1, "trend"
                else:
                    side, kind = rnd.choice([1, -1]), "revert"
        elif random_seed is not None:
            # 随机基准：方向与入场点都随机，风控照搬
            if rnd.random() < 0.02:
                side = rnd.choice([1, -1])
                kind = "rnd"
        elif not allow_regime:
            # 无环境约束：4H 自身信号双向（与 V0 同一触发逻辑，只是不认日线环境）
            if (ma_e[i] is not None and ma_e[i - 1] is not None
                    and closes[i] > ma_e[i] and closes[i - 1] <= ma_e[i - 1]
                    and closes[i] > ma_t[i] and rsi[i] > rsi_pull):
                side, kind = 1, "trend"
            elif (ma_e[i] is not None and ma_e[i - 1] is not None
                    and closes[i] < ma_e[i] and closes[i - 1] >= ma_e[i - 1]
                    and closes[i] < ma_t[i] and rsi[i] < 100 - rsi_pull):
                side, kind = -1, "trend"
        elif rg == "UP":
            # 回调结束：4H 收盘由下向上穿越 MA20，且仍在 MA60 之上（趋势未破）
            if (ma_e[i] is not None and ma_e[i - 1] is not None
                    and closes[i] > ma_e[i] and closes[i - 1] <= ma_e[i - 1]
                    and closes[i] > ma_t[i] and rsi[i] > rsi_pull):
                side, kind = 1, "trend"
        elif rg == "DOWN":
            if (ma_e[i] is not None and ma_e[i - 1] is not None
                    and closes[i] < ma_e[i] and closes[i - 1] >= ma_e[i - 1]
                    and closes[i] < ma_t[i] and rsi[i] < 100 - rsi_pull):
                side, kind = -1, "trend"
        elif rg == "RANGE":
            hh = max(highs[i - range_lookback:i])
            ll = min(lows[i - range_lookback:i])
            rng = hh - ll
            if rng > 0:
                posn = (closes[i] - ll) / rng
                if posn < 0.12 and rsi[i] < 32:
                    side, kind = 1, "revert"
                elif posn > 0.88 and rsi[i] > 68:
                    side, kind = -1, "revert"

        if side == 0:
            i += 1
            continue

        entry = closes[i]
        if stop_mode == "swing":
            # 结构性止损：放在「回调段极值被跌破 = 形态失效」处，再留一点缓冲
            if side == 1:
                stop = min(lows[max(0, i - swing_lb):i]) - swing_buf * atr[i]
            else:
                stop = max(highs[max(0, i - swing_lb):i]) + swing_buf * atr[i]
        else:
            stop = entry - side * atr_k * atr[i]
        risk = abs(entry - stop)
        if risk <= 0 or side * (entry - stop) <= 0:
            i += 1
            continue
        # 目标：趋势用追踪（跌破 MA20 出），回归用区间中轴
        target = None
        if kind == "revert":
            hh = max(highs[i - range_lookback:i])
            ll = min(lows[i - range_lookback:i])
            target = (hh + ll) / 2

        exit_px, exit_j, reason = None, None, None
        for j in range(i + 1, min(i + 120, n)):
            if side == 1 and lows[j] <= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            if side == -1 and highs[j] >= stop:
                exit_px, exit_j, reason = stop, j, "stop"
                break
            if kind == "revert" and target is not None:
                if (side == 1 and highs[j] >= target) or (side == -1 and lows[j] <= target):
                    exit_px, exit_j, reason = target, j, "target"
                    break
                if j - i >= 8:      # 时间止损：8 根 4H
                    exit_px, exit_j, reason = closes[j], j, "timeout"
                    break
            else:
                # 趋势单：跌破/站上 MA60（趋势失效）才离场，让利润跑
                if ma_t[j] is not None and ((side == 1 and closes[j] < ma_t[j]) or
                                            (side == -1 and closes[j] > ma_t[j])):
                    exit_px, exit_j, reason = closes[j], j, "trend_exit"
                    break
        if exit_px is None:
            exit_px, exit_j, reason = closes[min(i + 120, n - 1)], min(i + 120, n - 1), "eod"
        r = side * (exit_px - entry) / risk
        trades.append({"i": i, "j": exit_j, "side": side, "kind": kind, "regime": rg,
                       "date": bars[i]["date"], "r": r, "days": (exit_j - i) / 6.0,
                       "reason": reason})
        i = exit_j + 1
    return trades


def stats(trades):
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    sum_r = sum(rs)
    peak = cum = 0.0
    mdd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {
        "n": len(rs), "win": len(wins) / len(rs), "sumR": sum_r, "avgR": sum_r / len(rs),
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "mddR": mdd, "avg_bars": statistics.mean([t["days"] for t in trades]) if trades else 0,
    }


def main():
    COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "AAVE", "LINK", "HYPE", "CRV", "UNI"]
    print("拉取数据（10 品种 × 4H/1D）…")
    data = {}
    for c in COINS:
        try:
            d4 = fetch_hl(c, "4h")
            d1 = fetch_hl(c, "1d")
            reg = daily_regime_series(d1)
            rg4 = align_regime(d4, reg)
            data[c] = (d4, rg4)
            n_rg = sum(1 for x in rg4 if x in ("UP", "RANGE", "DOWN"))
            print(f"  {c:<5} 4H {len(d4):>5} 根（{d4[0]['date']} ~ {d4[-1]['date']}）"
                  f"  有效环境 {n_rg/len(d4)*100:>5.1f}%")
        except Exception as e:  # noqa: BLE001
            print(f"  {c:<5} 取数失败: {repr(e)[:60]}")
        time.sleep(0.4)

    print("\n" + "=" * 104)
    print("V0 全量（UP 只多 / RANGE 回归 / DOWN 只空）")
    print("=" * 104)
    print(f"{'品种':<6} {'笔数':>5} {'胜率':>7} {'总R':>8} {'均R':>7} {'盈亏比':>7} "
          f"{'回撤R':>7} {'趋势R':>8} {'回归R':>8}")
    print("-" * 104)
    all_v0 = []
    per_kind = {}
    per_coin = {}
    for c in COINS:
        if c not in data:
            continue
        d4, rg4 = data[c]
        tr = run_strategy(d4, rg4, allow_regime=True)
        all_v0 += tr
        per_coin[c] = tr
        for t in tr:
            per_kind.setdefault((t["regime"], t["kind"]), []).append(t["r"])
        s = stats(tr)
        if s:
            pf = f"{s['pf']:.2f}" if s["pf"] else "—"
            tr_r = sum(t["r"] for t in tr if t["kind"] == "trend")
            rv_r = sum(t["r"] for t in tr if t["kind"] == "revert")
            print(f"{c:<6} {s['n']:>5} {s['win']*100:>6.1f}% {s['sumR']:>+7.1f}"
                  f" {s['avgR']:>+6.2f} {pf:>7} {s['mddR']:>6.1f} {tr_r:>+7.1f} {rv_r:>+7.1f}")
    s = stats(all_v0)
    if s:
        pf = f"{s['pf']:.2f}" if s["pf"] else "—"
        print("-" * 104)
        print(f"{'合计':<6} {s['n']:>5} {s['win']*100:>6.1f}% {s['sumR']:>+7.1f}"
              f" {s['avgR']:>+6.2f} {pf:>7} {s['mddR']:>6.1f}")

    # ---- 纯趋势版（砍掉 RANGE 回归）----
    v0_trend = [t for t in all_v0 if t["kind"] == "trend"]
    st = stats(v0_trend)
    print("\n" + "=" * 104)
    print("V0-趋势 only（砍掉 RANGE 回归）vs V0 全量")
    print("=" * 104)
    if st:
        pf = f"{st['pf']:.2f}" if st["pf"] else "—"
        print(f"  纯趋势  {st['n']:>4} 笔  胜率 {st['win']*100:>5.1f}%  总R {st['sumR']:>+7.1f}"
              f"  均R {st['avgR']:>+5.2f}  盈亏比 {pf:>5}  回撤 {st['mddR']:>6.1f}R")
    if s:
        pf = f"{s['pf']:.2f}" if s["pf"] else "—"
        print(f"  全量    {s['n']:>4} 笔  胜率 {s['win']*100:>5.1f}%  总R {s['sumR']:>+7.1f}"
              f"  均R {s['avgR']:>+5.2f}  盈亏比 {pf:>5}  回撤 {s['mddR']:>6.1f}R")
        print(f"  → 砍掉回归的净影响：{(st['sumR']-s['sumR']):+.1f}R，"
              f"交易数减少 {s['n']-st['n']} 笔")

    print("\n按环境/类型拆分（全 10 品种）：")
    print(f"  {'环境-类型':<18} {'笔数':>5} {'胜率':>7} {'总R':>9} {'均R':>7}")
    for k in sorted(per_kind, key=lambda x: -len(per_kind[x])):
        rs = per_kind[k]
        w = sum(1 for r in rs if r > 0) / len(rs)
        print(f"  {k[0]+'-'+k[1]:<18} {len(rs):>5} {w*100:>6.1f}% {sum(rs):>+8.1f} {sum(rs)/len(rs):>+6.2f}")

    # ---- 三组对照 ----
    print("\n" + "=" * 104)
    print("三组对照（全 10 品种合计）")
    print("=" * 104)
    for label, kwargs, filt in (("V0 有环境约束", dict(allow_regime=True), None),
                                ("V0-趋势only", dict(allow_regime=True), "trend"),
                                ("V1 无环境约束", dict(allow_regime=False), None),
                                ("V2 随机入场", dict(allow_regime=False, random_seed=42), None)):
        allt = []
        for c in COINS:
            if c not in data:
                continue
            d4, rg4 = data[c]
            tr = run_strategy(d4, rg4, **kwargs)
            if filt:
                tr = [t for t in tr if t["kind"] == filt]
            allt += tr
        ss = stats(allt)
        if not ss:
            continue
        pf = f"{ss['pf']:.2f}" if ss["pf"] else "—"
        print(f"  {label:<14} {ss['n']:>5} 笔  胜率 {ss['win']*100:>5.1f}%  "
              f"总R {ss['sumR']:>+8.1f}  均R {ss['avgR']:>+5.2f}  "
              f"盈亏比 {pf:>5}  回撤 {ss['mddR']:>6.1f}R")

    # ---- 消融：入场时机到底值不值钱 ----
    print("\n" + "=" * 104)
    print("消融对照：方向听日线环境，但入场时点随机（5 个随机种子）")
    print("=" * 104)
    print("  若此项接近 V0，说明「4H 形态入场」没贡献，收益全部来自日线环境本身")
    abl, abl_t = [], []
    for seed in (1, 7, 42, 101, 2024):
        allt = []
        for c in COINS:
            if c not in data:
                continue
            d4, rg4 = data[c]
            allt += run_strategy(d4, rg4, allow_regime=True, random_seed=seed,
                                 timing="random")
        for label, sub in (("全量", allt), ("趋势only", [t for t in allt if t["kind"] == "trend"])):
            ss = stats(sub)
            if not ss:
                continue
            (abl if label == "全量" else abl_t).append(ss["sumR"])
            pf = f"{ss['pf']:.2f}" if ss["pf"] else "—"
            print(f"  seed={seed:<5} {label:<8} {ss['n']:>5} 笔  胜率 {ss['win']*100:>5.1f}%  "
                  f"总R {ss['sumR']:>+8.1f}  均R {ss['avgR']:>+5.2f}  盈亏比 {pf:>5}")
    if abl_t:
        print(f"  → 趋势only 均值 {statistics.mean(abl_t):+.1f}R，"
              f"区间 [{min(abl_t):+.1f}, {max(abl_t):+.1f}]")
        print(f"  → 对比 V0-趋势only {st['sumR']:+.1f}R（同一入场笔数口径）")

    # ---- 分段稳定性 ----
    print("\n" + "=" * 104)
    print("分段稳定性（半年一段，纯趋势）")
    print("=" * 104)
    seg = {}
    for t in v0_trend:
        y, m = t["date"][:4], int(t["date"][5:7])
        seg.setdefault(f"{y}H{1 if m <= 6 else 2}", []).append(t)
    print(f"  {'区间':<9} {'笔数':>5} {'胜率':>7} {'总R':>8} {'均R':>7}")
    print("  " + "-" * 44)
    for k in sorted(seg):
        rs = [t["r"] for t in seg[k]]
        w = sum(1 for r in rs if r > 0) / len(rs)
        print(f"  {k:<9} {len(rs):>5} {w*100:>6.1f}% {sum(rs):>+7.1f} {sum(rs)/len(rs):>+6.2f}")
    pos_seg = sum(1 for k in seg if sum(t["r"] for t in seg[k]) > 0)
    print(f"  → 盈利段数 {pos_seg}/{len(seg)}")

    # ---- 参数平台检验 ----
    print("\n" + "=" * 104)
    print("参数平台检验：纯趋势总R（27 组 × 10 品种）")
    print("=" * 104)
    grid = []
    for rp in (40, 45, 50):
        for ak in (1.2, 1.5, 2.0):
            for mt in (40, 60, 80):
                tot = cnt = 0.0
                for c in COINS:
                    if c not in data:
                        continue
                    d4, rg4 = data[c]
                    tr = [t for t in run_strategy(d4, rg4, allow_regime=True,
                                                  rsi_pull=rp, atr_k=ak, ma_trend=mt)
                          if t["kind"] == "trend"]
                    tot += sum(t["r"] for t in tr)
                    cnt += len(tr)
                grid.append((rp, ak, mt, tot, int(cnt)))
    grid.sort(key=lambda x: -x[3])
    print(f"  {'RSI':>5} {'ATR':>5} {'MA':>5} {'总R':>9} {'笔数':>6}")
    print("  " + "-" * 34)
    for rp, ak, mt, tot, cnt in grid[:5]:
        print(f"  {rp:>5} {ak:>5.1f} {mt:>5} {tot:>+8.1f} {cnt:>6}")
    print("  …")
    for rp, ak, mt, tot, cnt in grid[-2:]:
        print(f"  {rp:>5} {ak:>5.1f} {mt:>5} {tot:>+8.1f} {cnt:>6}")
    pos = sum(1 for g in grid if g[3] > 0)
    med = statistics.median([g[3] for g in grid])
    print(f"  → {len(grid)} 组中 {pos} 组为正 ({pos/len(grid)*100:.0f}%)，中位总R {med:+.1f}")


if __name__ == "__main__":
    main()
