#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_probe_start.py —— 判定起点对比：s0 = max(bisect(START), 120)  vs  s0 = 120

问题：站点把「展示窗口起点 2024-01-01」当成状态机的起点，于是每个品种都被迫
在 2024-01-01 凭空给一个初始状态（entry_reason="init"），而这一段不受任何纠错。
但交易层（strategy_1h_rsi.regime_and_blood / bt_skeleton.blood_of）用的起点是
ma_slow=120 —— 即「数据累计够 120 根就开始判定」。两套起点不同，是血统标注
在全站泛滥的根因。

本脚本量两件事：
  1) 当前状态会不会变（如果变，站点/日报的「今天是什么状态」就跟着变）
  2) 展示窗口内逐日状态差异多少天（路径依赖到底有多强）
"""
import bisect

from daily_regime_alert import (
    ASSETS, START, fetch_hyperliquid_daily, fetch_ashare_daily, drop_unclosed, drop_unclosed_cn,
)
from visualize_btc_regime import daily_states, fetch_binance_daily, sma
from visualize_eth_gold_regime import fetch_yahoo_daily
from datetime import datetime, timezone


def fetch(a):
    warm = datetime(2021, 1, 1, tzinfo=timezone.utc)
    if a["source"] == "binance":
        return fetch_binance_daily(a["symbol"], warm)
    if a["source"] == "hyperliquid":
        return fetch_hyperliquid_daily(a["symbol"])
    if a["source"] == "ashare":
        return fetch_ashare_daily(a)
    return fetch_yahoo_daily(a["symbol"], warm)


print(f"{'品种':10}{'根数':>6}{'起点A':>12}{'起点B':>12}{'当前A':>6}{'当前B':>6}{'窗口内不一致':>10}  说明")
for a in ASSETS:
    try:
        rows = drop_unclosed_cn(fetch(a)) if a["source"] == "ashare" else drop_unclosed(fetch(a))
    except Exception as e:  # noqa: BLE001
        print(f"{a['key']:10} 取数失败 {e}")
        continue
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    ma120 = sma(closes, 120)

    sA = bisect.bisect_left(dates, START)
    if sA < 120:
        sA = 120
    sB = 120
    if sB >= len(closes):
        print(f"{a['key']:10} 数据不足 120 根")
        continue

    stA, _, _ = daily_states(closes, sA, 70, 30, True, 2, True)
    stB, _, _ = daily_states(closes, sB, 70, 30, True, 2, True)

    # 展示窗口内逐日不一致
    d0 = bisect.bisect_left(dates, START)
    diff = sum(1 for i in range(d0, len(dates)) if stA[i] != stB[i])
    # 展示窗口第一天，B 路线是不是「凭空初始化」
    first_b = "init" if sB >= d0 else "有前史"
    print(f"{a['key']:10}{len(closes):>6}{dates[sA]:>12}{dates[sB]:>12}"
          f"{(stA[-1] or '—'):>6}{(stB[-1] or '—'):>6}{diff:>10}  "
          f"A起点MA120={ma120[sA]:.4g} B起点MA120={ma120[sB]:.4g} {first_b}")
