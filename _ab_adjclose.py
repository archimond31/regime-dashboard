#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_ab_adjclose.py —— 数据口径 A/B：复权 vs 未复权、期货 vs ETF

这三组对照决定了 2026-11-25 那次「数据口径修订」到底该改什么、不该改什么。
结论先写在这里，脚本可复现：

  ① ETF 必须用**复权价判定**：分红在除息日造成一步式下跌，未复权序列带系统性
     下行漂移。同一套状态机分别跑 close 与 adjclose，2024 年以来状态不同的交易日：
        HYG 60.7%   TLT 25.4%   IEF 25.8%   QQQ 8.1%   SPY 4.1%   GLD 0%
     最刺眼的是 HYG：未复权判 **DOWN**、复权判 **UP** —— 它年化分配约 6%，
     下行漂移足以把「总回报在涨」读成「在跌」。无分红的 GLD 是 0%，说明这不是
     算法噪声，就是分红造成的。
     → 判定走 adj，展示走 close（见 fetch_yahoo_daily / compute_regime 的 k 系数）。

  ② 商品**不要**换成 ETF 替代：同一资产用期货连续合约与 ETF 跑出的状态只有
     56~78% 一致（铜 77.7%、白银 57.8%、黄金 56.7%、原油 57.6%），而两个完全
     不同的资产（铜 vs 白银）是 47.8% —— 也就是说换过去只是换一个口径，
     并不存在「更正确」的那一个。把 ETF 序列前移一日也不能提高一致性
     （反而 -1.5pp），说明分歧不是取样时点，而是基差/费用/交易时段本身。
     → 保留期货（价格可读、是行业参考价），代价是换月跳空。

  ③ 换月跳空在**贵金属与原油**上不是主要问题，但在**铜**上确实发生过一次：
     2025-07-31 HG=F 单日 -22.25%，而 CPER 只 -2.80%（只复制 13%）—— 那是
     COMEX 关税溢价崩塌，属期货基差事件，不是全球铜价下跌。黄金/白银的最大
     单日在 ETF 上同日可见（-10.78% vs -10.27%），属真实行情。
     早期把「期货与 ETF 的 130 天背离」整体归因于换月是错的 —— 大部分是两条
     不同序列的正常差异。但**铜这一类基差事件是真实存在且会影响判定的**，
     属已知限制，保留在页脚而不做自动修正（自动抹平会连带抹掉真实行情）。

用法：python _ab_adjclose.py
需要本地代理（默认沿用 REGIME_PROXY，否则 127.0.0.1:7890）。
"""
import json
import os
import statistics as st
from datetime import datetime, timezone

os.environ.setdefault("REGIME_PROXY", "http://127.0.0.1:7890")

import visualize_eth_gold_regime as V
from visualize_btc_regime import daily_states

START = datetime(2021, 1, 1, tzinfo=timezone.utc)
SINCE = "2024-01-01"      # 只比较判定窗口内的日子


def _states(pairs):
    d = [p[0] for p in pairs]
    c = [p[1] for p in pairs]
    st, _, _ = daily_states(c, 120, 70, 30, True, 2, True, ma_slow=120, ma_fast=60)
    return {d[i]: st[i] for i in range(len(d))}


def _agree(sa, sb, shift=0):
    """两套状态的一致率。shift>0 表示用 b 的前 shift 天去对 a 的今天。"""
    days = sorted(d for d in set(sa) & set(sb) if d >= SINCE)
    if shift:
        days = days[shift:]
    n = same = 0
    for i, d in enumerate(days):
        other = days[i - shift] if shift else d
        if other not in sb:
            continue
        n += 1
        same += sa[d] == sb[other]
    return (same / n if n else 0.0), n


def ab_adjclose():
    """ETF：未复权 close vs 复权 adjclose。"""
    print("=" * 78)
    print("① ETF 复权 vs 未复权（同一资产，同一状态机，只换价格序列）")
    print("=" * 78)
    print(f"{'品种':<8}{'共同日':>7}{'状态一致':>10}{'不同天数':>9}{'末状态 close/adj':>20}")
    for sym in ["TLT", "QQQ", "SPY", "GLD", "IEF", "HYG"]:
        try:
            rows = V.fetch_yahoo_daily(sym, START)
        except Exception as e:                                  # noqa: BLE001
            print(f"{sym:<8} 取数失败 {e}")
            continue
        s_raw = _states([(r["date"], r["close"]) for r in rows])
        s_adj = _states([(r["date"], r["adj"]) for r in rows])
        rate, n = _agree(s_raw, s_adj)
        last = max(d for d in set(s_raw) & set(s_adj) if d >= SINCE)
        print(f"{sym:<8}{n:>7}{rate * 100:>9.1f}%{(1 - rate) * n:>9.0f}"
              f"{s_raw[last]:>10}/{s_adj[last]:<9}")
    print("\n  → 分配率越高影响越大。HYG 最极端：未复权判 DOWN、复权判 UP。"
          "\n    无分红的 GLD 是 0% —— 说明这不是算法噪声，就是分红造成的漂移。")


def ab_futures_vs_etf():
    """商品：期货连续 vs 对应 ETF。"""
    print("\n" + "=" * 78)
    print("② 商品：期货连续 vs ETF —— 换成 ETF 到底改了什么")
    print("=" * 78)
    pairs = [("GC=F", "GLD", "黄金"), ("SI=F", "SLV", "白银"),
             ("CL=F", "USO", "原油"), ("HG=F", "CPER", "铜"),
             ("HG=F", "SLV", "铜×白银(无关资产对照)")]
    print(f"{'品种':<16}{'共同日':>7}{'同日一致':>9}{'ETF滞后1日':>12}{'末状态':>14}")
    for f, e, name in pairs:
        try:
            sf = _states([(r["date"], r["close"]) for r in V.fetch_yahoo_daily(f, START)])
            se = _states([(r["date"], r["close"]) for r in V.fetch_yahoo_daily(e, START)])
        except Exception as ex:                                 # noqa: BLE001
            print(f"{name:<16} 取数失败 {ex}")
            continue
        a0, n = _agree(sf, se, 0)
        a1, _ = _agree(sf, se, 1)
        last = max(d for d in set(sf) & set(se) if d >= SINCE)
        print(f"{name:<16}{n:>7}{a0 * 100:>8.1f}%{a1 * 100:>11.1f}%"
              f"{sf[last]:>8}/{se[last]:<6}")
    print("\n  → 无关资产对照 47.8%，同资产期货/ETF 只有 56~78%。前移一日不改善，"
          "\n    说明分歧来自基差与交易时段，不是取样错位。换数据源不解决问题。")


def ab_roll_gap():
    """换月跳空：期货的大波动是否在 ETF 上同日出现。"""
    print("\n" + "=" * 78)
    print("③ 换月跳空检验：期货的大单日波动，ETF 当天有没有")
    print("=" * 78)
    for f, e, name in [("GC=F", "GLD", "黄金"), ("SI=F", "SLV", "白银"),
                       ("CL=F", "USO", "原油"), ("HG=F", "CPER", "铜")]:
        try:
            rf = V.fetch_yahoo_daily(f, START)
            re_ = V.fetch_yahoo_daily(e, START)
        except Exception as ex:                                 # noqa: BLE001
            print(f"{name}: 取数失败 {ex}")
            continue
        mf = {rf[i]["date"]: rf[i]["close"] / rf[i - 1]["close"] - 1
              for i in range(1, len(rf))}
        me = {re_[i]["date"]: re_[i]["close"] / re_[i - 1]["close"] - 1
              for i in range(1, len(re_))}
        big = sorted(((abs(v), d) for d, v in mf.items() if d >= SINCE), reverse=True)[:3]
        print(f"\n  {name}（{f} vs {e}）最大单日：")
        for mag, d in big:
            e_v = me.get(d) or 0.0
            ratio = abs(e_v) / mag if mag else 0.0
            # 判据用「期货跌幅有多少被 ETF 复制」。不能用绝对阈值 —— 铜 2025-07-31
            # 期货 -22.25%、ETF -2.80%，绝对跌幅过了 2% 的坎，但只复制了 13%，
            # 那是 COMEX 关税溢价崩塌（期货基差事件），不是全球铜价下跌。
            if ratio < 0.5:
                tag = f"只复制 {ratio * 100:.0f}% → 基差/换月事件，非全市场行情"
            else:
                tag = "同日 ETF 同步 → 真实行情"
            print(f"    {d}  期货 {mf[d] * 100:+6.2f}%   ETF {e_v * 100:+6.2f}%   {tag}")


if __name__ == "__main__":
    ab_adjclose()
    ab_futures_vs_etf()
    ab_roll_gap()
    print("\n完成。")
