#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：开仓信号延迟 N 根 4H 才执行，会损失多少（无偏版）。

为什么要有这个「无偏版」：
  第一版直接把入场价换成 close(i+d)、出场沿用原来的 —— 结果延迟越久 R 越高，
  纯属幻觉。因为 `k >= j` 的样本被整批丢掉了：你晚 48 小时才进，
  那些在 48 小时内就被止损打掉的笔根本不会出现在分母里，剩下的都是「活下来的」。
  这是教科书级的生存偏差。

正确做法：延迟入场后，止损按**新的**入场价重设（entry ± 1.5×ATR），
出场规则（日线翻转 / 新止损 / 满 MAX_BARS 根）一字不改，让这笔交易按新路径走完。
这样每一笔原始信号都仍然在分母里，只是路径不同。

三个口径：
  A. 全部信号都跟进（哪怕价格已经跌回突破线内）
  B. 只在「价格仍在突破位之外」时才跟进 —— 邮件里写「已回到 XX 以内则放弃」的依据
  C. 放弃比例 = 1 - B 的覆盖率
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
LOOKBACK, ATR_K, MAX_BARS = BT.LOOKBACK, BT.ATR_K, BT.MAX_BARS
DELAYS = [0, 1, 2, 3, 6, 12, 18, 24]

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()


def signals_at(b4, rg4, atr, delay=0, only_valid=False, pct=None):
    """和 BT.signals 同一套逻辑，只把入场时刻推迟 delay 根。

    only_valid=True 时额外要求：延迟后那根的收盘价仍在突破位之外，否则放弃这笔。
    pct 必须由外部传进来 —— 滚动 500 根分位在 20k 根上算一次要好几秒，
    18 组延迟各算一遍直接跑不动。
    """
    n = len(b4)
    if pct is None:
        pct = BT.atr_percentile(b4, atr, coin=None)
    out = []
    i = max(LOOKBACK + 20, 30)
    while i < n - 1:
        rg = rg4[i]
        if rg not in ("UP", "DOWN") or atr[i] is None:
            i += 1
            continue
        if b4[i]["date"] < BT.START_NAV:
            i += 1
            continue
        if pct[i] is None or pct[i] > BT.VOL_Q:
            i += 1
            continue
        side = 1 if rg == "UP" else -1
        hi = max(x["high"] for x in b4[i - LOOKBACK:i])
        lo = min(x["low"] for x in b4[i - LOOKBACK:i])
        ok = (side == 1 and b4[i]["close"] > hi) or (side == -1 and b4[i]["close"] < lo)
        if not ok:
            i += 1
            continue

        # ---- 这里开始是延迟执行 ----
        e = i + delay
        if e >= n - 1 or rg4[e] != rg:          # 延迟期间日线已翻转 → 这笔不做了
            i += 1
            continue
        if only_valid:
            if (side == 1 and b4[e]["close"] <= hi) or (side == -1 and b4[e]["close"] >= lo):
                i += 1
                continue
        if atr[e] is None:
            i += 1
            continue

        entry = b4[e]["close"]
        stop = entry - side * ATR_K * atr[e]
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue

        exit_px = exit_j = reason = None
        cur_stop, best, mfe = stop, entry, 0.0
        for j in range(e + 1, min(e + MAX_BARS + 1, n)):
            if side == 1 and b4[j]["low"] <= cur_stop:
                exit_px, exit_j, reason = cur_stop, j, "stop"
                break
            if side == -1 and b4[j]["high"] >= cur_stop:
                exit_px, exit_j, reason = cur_stop, j, "stop"
                break
            if rg4[j] != rg:
                exit_px, exit_j, reason = b4[j]["close"], j, "flip"
                break
            if side == 1:
                mfe = max(mfe, (b4[j]["high"] - entry) / risk)
            else:
                mfe = max(mfe, (entry - b4[j]["low"]) / risk)
            if BT.EARLY_N and j == e + BT.EARLY_N and mfe < BT.EARLY_THR:
                exit_px, exit_j, reason = b4[j]["close"], j, "early"
                break
            if BT.TRAIL_K > 0:
                if side == 1:
                    best = max(best, b4[j]["high"])
                    if best - entry >= BT.TRAIL_ARM * risk:
                        cur_stop = max(cur_stop, best - BT.TRAIL_K * atr[e])
                else:
                    best = min(best, b4[j]["low"])
                    if entry - best >= BT.TRAIL_ARM * risk:
                        cur_stop = min(cur_stop, best + BT.TRAIL_K * atr[e])
        if exit_px is None:
            exit_j = min(e + MAX_BARS, n - 1)
            exit_px, reason = b4[exit_j]["close"], "timeout"

        out.append({"i": i, "e": e, "side": side, "r": side * (exit_px - entry) / risk,
                    "reason": reason, "bars": exit_j - e})
        i = exit_j + 1
    return out


DATA = {}
for c in COINS:
    if c == "HYPE":
        raw = load_hype("4h")
        b4 = [dict(x, date=x["date"][:10]) for x in raw]
        rg4 = align_regime_safe(b4, regime_series(to_daily(b4)))
    else:
        b, rg, _a, _b = V2.load_all()[c]
        b4 = agg4h(b)
        rg4 = [rg[x["src"]] for x in b4]
    atr = atr_wilder(b4, 14)
    DATA[c] = (b4, rg4, atr, BT.atr_percentile(b4, atr, coin=c))
print("数据就绪：" + " ".join(f"{c}({len(DATA[c][0])})" for c in COINS), flush=True)


def run(delay, only_valid):
    allr, per = [], {}
    for c in COINS:
        b4, rg4, atr, pct = DATA[c]
        ts = signals_at(b4, rg4, atr, delay=delay, only_valid=only_valid, pct=pct)
        per[c] = ts
        allr += [t["r"] for t in ts]
    return allr, per


n0, _ = run(0, False)
print("开仓信号延迟执行的成本（无偏：延迟后重设止损、出场规则不变）")
print(f"基准 = 当根收盘就执行，共 {len(n0)} 笔")
print("-" * 84)
print(f"{'延迟':<14}{'成交笔数':>9}{'相对基准':>10}{'平均R':>9}{'总R':>10}{'胜率':>8}{'中位持仓':>10}")
print("-" * 84)
base_n, base_sum = len(n0), sum(n0)
base_mean = base_sum / base_n
res = {}
for d in DELAYS:
    rs, per = run(d, False)
    res[d] = (rs, per)
    lab = f"{d} 根（{d * 4}h）" if d else "0（当根收盘）"
    print(f"{lab:<14}{len(rs):>9}{len(rs) / base_n * 100:>9.0f}%"
          f"{statistics.mean(rs):>9.3f}{sum(rs):>10.0f}"
          f"{sum(1 for r in rs if r > 0) / len(rs) * 100:>7.1f}%"
          f"{statistics.median([t['bars'] for c in per for t in per[c]]):>9.0f}根")

print("\n" + "=" * 84)
print("总 R 的变化（这才是钱）：相对基准的衰减")
print("-" * 84)
for d in DELAYS:
    rs = res[d][0]
    d_sum = sum(rs) - base_sum
    print(f"  延迟 {d:>2} 根（{d * 4:>2}h）：总 R {sum(rs):>7.0f}，相对基准 {d_sum:>+7.0f}"
          f"（{d_sum / base_sum * 100:>+6.1f}%）"
          + ("" if d == 0 else f"  ≈ 每笔少 {d_sum / len(rs):+.3f}R"))

print("\n" + "=" * 84)
print("加一条纪律：延迟后价格已回到突破线内就放弃（只跟进仍在突破外的）")
print("-" * 84)
print(f"{'延迟':<14}{'跟进笔数':>9}{'放弃率':>8}{'平均R':>9}{'总R':>10}{'相对基准总R':>12}")
print("-" * 84)
for d in DELAYS:
    rs, per = run(d, True)
    lab = f"{d} 根（{d * 4}h）" if d else "0（当根收盘）"
    drop = 1 - len(rs) / base_n
    print(f"{lab:<14}{len(rs):>9}{drop * 100:>7.0f}%{statistics.mean(rs):>9.3f}"
          f"{sum(rs):>10.0f}{sum(rs) - base_sum:>+12.0f}")
