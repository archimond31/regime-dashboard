#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：这套策略到底有多「低频」—— 用来决定邮件提醒该怎么设计。

要回答五个问题：
  1. 一年下来，10 个品种合计需要动手多少次？按类型怎么分布？
  2. 其中多少必须「当下人工处理」，多少能被预挂单接管？
  3. 这些动作摊在多少根 4H 上（= 多少封邮件）、集中在多少天？
  4. 同一根 4H 里有多少个品种会同时动作（一封邮件管几个单）？
  5. 有没有「整月无事」和「一天好几个」的极端不均衡？

「动作」= 一次需要人动的手，按 (币, 4H) 去重：
  · 开仓    —— 4H 收盘突破，底仓加仓或新建空头
  · 平仓    —— 止损 / 日线翻转 / 满 120 根兜底
  · 清底仓  —— 日线由 UP|RANGE 转 DOWN，撤掉现货多头
  · 建底仓  —— 日线由 DOWN 转 UP|RANGE，重新建现货多头
同一根 bar 上「日线翻转平仓 + 清/建底仓」是同一次操作，合并算一次。

关键分类（决定邮件该不该「叫醒」你）：
  · stop 止损 —— 入场时就该挂好止损单，理论上是**零人工**
  · flip 翻转 —— 必须人工，且不可预挂（要看日线状态才知道）
  · timeout   —— 必须人工（到点平仓，价格任意）
  · 开仓      —— 必须人工决策，但可以预挂突破单
"""
import datetime as dt
import os
import statistics
import sys
from collections import defaultdict

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
H4_MS = 4 * 3600 * 1000


def _open_key(ms):
    """归一到所在 4H 的开盘时刻，做成跨币可对齐的唯一 key。

    agg4h 给的 `date` 只有日期没有小时，6 根 4H 会塌成同一个 key；
    HYPE 的 `t` 是开盘时刻、Binance 的 `ts` 是收盘时刻 —— 两个口径先归一再比。
    """
    k = int(ms) // H4_MS * H4_MS
    return dt.datetime.fromtimestamp(k / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()

per_coin = {}                     # coin -> {bar_key -> set(kinds)}
bar_times = {}                    # coin -> [bar_key]
year_bars = defaultdict(set)      # year -> {bar_key}（该年出现过的 4H）

for c in COINS:
    if c == "HYPE":
        raw = load_hype("4h")
        b4 = [dict(x, date=x["date"][:10]) for x in raw]
        rg4 = align_regime_safe(b4, regime_series(to_daily(b4)))
        keys = [_open_key(x["t"]) for x in raw]
    else:
        b, rg, _a, _b = V2.load_all()[c]
        b4 = agg4h(b)
        rg4 = [rg[x["src"]] for x in b4]
        keys = [_open_key(x["ts"] - H4_MS) for x in b4]   # ts 是收盘时刻

    atr = atr_wilder(b4, 14)
    _sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c)

    ev = defaultdict(set)
    for t in trades:
        ev[keys[t["i"]]].add("开仓")
        ev[keys[t["j"]]].add("平:" + t["reason"])
    for k in range(1, len(rg4)):
        a, b_ = rg4[k - 1], rg4[k]
        if a == b_:
            continue
        if b_ == "DOWN" and a != "DOWN":
            ev[keys[k]].add("清底仓")
        elif a == "DOWN" and b_ != "DOWN":
            ev[keys[k]].add("建底仓")

    per_coin[c] = ev
    bar_times[c] = keys
    for k in keys:
        year_bars[k[:4]].add(k)

years = sorted(y for y in year_bars if len(year_bars[y]) > 300)


def needs_human(kinds):
    """这一格动作里有没有必须当下人工处理的。stop 视为可预挂。"""
    if "开仓" in kinds or "清底仓" in kinds or "建底仓" in kinds:
        return True
    return any(k in ("平:flip", "平:timeout", "平:early") for k in kinds)


# ---------- 1. 分年动作总量 + 必须人工 ----------
print("组合层面：一年总共要动几次手")
print("-" * 84)
print(f"{'年份':<8}{'动作次数':>10}{'必须人工':>10}{'可预挂占':>10}"
      f"{'有动作4H':>11}{'该年4H':>9}{'占比':>8}")
print("-" * 84)
combo = defaultdict(dict)         # bar -> {coin: kinds}
for c in COINS:
    for k, kinds in per_coin[c].items():
        if kinds:
            combo[k][c] = kinds

for y in years:
    nb = len(year_bars[y])
    act = [k for k in combo if k[:4] == y]
    n_ev = sum(len(v) for k in act for v in combo[k].values())
    n_h = sum(1 for k in act for v in combo[k].values() if needs_human(v))
    print(f"{y:<8}{n_ev:>10}{n_h:>10}{(1 - n_h / max(n_ev, 1)) * 100:>9.0f}%"
          f"{len(act):>11}{nb:>9}{len(act) / max(nb, 1) * 100:>7.1f}%")

# ---------- 2. 类型分解（近年） ----------
y_last = years[-1]
kind_cnt = defaultdict(int)
for k in combo:
    if k[:4] != y_last:
        continue
    for kinds in combo[k].values():
        for t in kinds:
            kind_cnt[t] += 1
print(f"\n{y_last} 年动作构成（同一格里的多类合并计）")
print("-" * 44)
LAB = {"开仓": "开仓（突破触发）", "清底仓": "清底仓（日线转 DOWN）",
       "建底仓": "建底仓（日线离开 DOWN）", "平:stop": "止损（可预挂）",
       "平:flip": "日线翻转平仓", "平:timeout": "满 120 根到期", "平:early": "早期淘汰"}
for t, n in sorted(kind_cnt.items(), key=lambda kv: -kv[1]):
    print(f"  {LAB.get(t, t):<24}{n:>6}")

# ---------- 3. 并发度 ----------
print("\n" + "=" * 84)
print("并发度：同一根 4H 上有几个品种要同时动手")
print("-" * 84)
dist = defaultdict(int)
for k, d in combo.items():
    dist[len(d)] += 1
tb = sum(dist.values())
for n in sorted(dist):
    print(f"  {n} 个品种 {dist[n]:>6} 根（{dist[n] / tb * 100:>5.1f}%） "
          + "█" * round(dist[n] / max(dist.values()) * 40))
print(f"\n  合计 {tb} 根 4H 上有动作；平均每根 {sum(len(v) for v in combo.values()) / tb:.2f} 个品种")

# ---------- 4. 时间分布 ----------
print("\n" + "=" * 84)
print("时间分布：动作是均匀铺开还是扎堆")
print("-" * 84)
by_day = defaultdict(int)
for k, d in combo.items():
    by_day[k[:10]] += len(d)
days = sorted(by_day)
epd = sorted(by_day.values())
print(f"有动作的交易日 {len(days)} 天，平均每天 {statistics.mean(epd):.2f} 次动作")
print(f"  中位 {statistics.median(epd):.0f} 次 · 最多 {max(epd)} 次/天")
print(f"  单日 ≥3 次：{sum(1 for v in epd if v >= 3)} 天"
      f"（{sum(1 for v in epd if v >= 3) / len(days) * 100:.0f}%）")
print(f"  单日 1 次：{sum(1 for v in epd if v == 1)} 天"
      f"（{sum(1 for v in epd if v == 1) / len(days) * 100:.0f}%）")

dts = [dt.date.fromisoformat(d) for d in days]
gaps = [(dts[i] - dts[i - 1]).days for i in range(1, len(dts))]
if gaps:
    print(f"\n两次「有动作」的间隔：中位 {statistics.median(gaps):.0f} 天"
          f" · 均值 {statistics.mean(gaps):.1f} 天 · 最长 {max(gaps)} 天")
    print(f"  空窗 > 7 天：{sum(1 for g in gaps if g > 7)} 次"
          f"（{sum(1 for g in gaps if g > 7) / len(gaps) * 100:.0f}%）")

# ---------- 5. 邮件负担 ----------
print("\n" + "=" * 84)
print("邮件负担对照（按 %s 年的节奏外推）" % y_last)
print("-" * 84)
nb = len(year_bars[y_last])
act_bars = len([k for k in combo if k[:4] == y_last])
human_bars = len([k for k in combo if k[:4] == y_last
                  and any(needs_human(v) for v in combo[k].values())])
print(f"  每 4H 都发               {nb:>5} 封/年（{nb / 365:.1f} 封/天）→ 空信占 {(1 - act_bars / nb) * 100:.0f}%")
print(f"  有动作就发               {act_bars:>5} 封/年（{act_bars / 365:.1f} 封/天）")
print(f"  只发「必须人工」的        {human_bars:>5} 封/年（{human_bars / 365:.1f} 封/天）")
print(f"  必须人工 + 每周一封巡检   {human_bars + 52:>5} 封/年 → 每周 1 封打底，故障立刻可辨")
