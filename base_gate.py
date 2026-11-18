#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_gate.py —— 底仓闸门（趋势过滤）**已默认关闭，结论见文末**

== 它本来想解决什么 ==
底仓 base=1.0 在 UP/RANGE 恒定，唯一退出通道是「日线状态翻 DOWN」。
但状态机认错有延迟：2022 年信号层 +23R、资金端 −51.9%；2025 年 +122R、−9.7%。
两次亏损都出在底仓 —— 它没有任何独立的风控通道，只能等状态机自己改口。

规则：
  当前 4H 收盘 < 前一交易日 MA50 → 底仓留 25%
  当前 4H 收盘 > 前一交易日 MA50 × (1+1%) → 底仓恢复 100%
  中间地带维持上一次的取值（滞回带，防均线附近抖动）

== 2026-11-18 的翻案：那 +68 万是「一格前视」 ==
最初的实验（`_exp_base.py`）报出口径 A 310 万 → 378 万、回撤 -60.7% → -30.6%、
滚动 16 胜 1 负。这个结果**不成立**。

原因：回测的收益约定是 `nav *= 1 + pos[k] × r[k]`，r[k] = close[k]/close[k-1]，
所以 pos[k] 是「在 bar k 期间持有的仓位」，必须在 close[k] **之前**定下来。
而闸门用 `b4[k]["close"]` 与 MA 比较，却乘在「到该 close 为止」的收益上 ——
让价格先跌完、再回头决定「这根该减仓」，白拿了一格。

去掉前视（gate 平移一格）后的真实数字（`_verify_lookahead.py`）：
                          累计        2021      2022      2025     回撤
  不开闸门                310 万     +145.3%   -51.9%    -9.7%   -60.7%
  闸门 含前视（错）        378 万     +213.9%   -20.6%   +10.6%   -30.6%
  闸门 去前视（对）        277 万     +149.6%   -41.6%    -1.8%   -52.5%

前视贡献 101 万，真实代价 -33 万。**去前视后闸门比不开还差**，回撤也只改善 8pp。
所以默认关闭。留着代码是为了不再重犯同一类错，不是为了用。

== 这一格前视污染了整条「底仓退出」研究线 ==
`_exp_base.py` 的五臂对照（MA120/MA60 减半、RANGE 打折、3ATR 止损）用的是同一个写法
（用当根 close 判、乘当根收益），数值全部虚高。
`_exp_risk2*.py` 的 ATR 吊灯（即时 548 万）同理，去前视后是 187 万，也低于不开。
**教训：任何「用 close[k] 算出来的仓位」在回测里都必须作用于 r[k+1]，
且必须跑一遍「平移一格」的对照 —— 两者差多少，就是白拿的 alpha。**

== 为什么平移之后还是不行（这才是真正的结论）==
不是前视的技术问题，是经济问题：MA50 穿越是个**连续跟随**规则，
晚一根就晚一次、次次都晚，而错过的正是「跌得最快那一根」。
`_exp_gate_delay.py` 试了 MA ∈ {50,80,120} × 滞回 ∈ {1..8%} 共 15 个组合，
**没有一个**能在晚 1 根执行下优于不开闸门；加宽滞回、放慢均线都救不回来。
`_exp_risk2d.py` 把它改成「触发后锁死」的一次性事件，同样全灭（188~228 万）。

== 开关 ==
默认关。SKEL_BASEGATE=1 才启用（研究用，不要在生产开）。
"""
import bisect
import os

ENABLED = os.environ.get("SKEL_BASEGATE", "0") == "1"
GATE_MA = int(os.environ.get("SKEL_BASEGATE_MA", "50"))
GATE_CV = float(os.environ.get("SKEL_BASEGATE_CV", "0.25"))
GATE_HYST = float(os.environ.get("SKEL_BASEGATE_HYST", "0.01"))


def ma_lookup(d1, n=None):
    """返回 lookup(4H 日期) → **严格早于该日期的最后一根日线**的 MA_n。

    为什么不用「日期精确匹配」：实盘最新的 4H 收盘日期常常就是今天，
    而日线序列只到昨天（当日日线尚未收盘），精确查表会返回 None，
    闸门就悄悄失效了 —— 这个坑 2026-11-18 踩过一次，表现为 ma50 全 None。
    用 bisect 找「前方最近的已收盘日线」才是正确语义。

    早于第一根日线时返回 None，调用方按「不干预」处理。
    """
    n = GATE_MA if n is None else n
    dc = [(x["date"][:10], x["close"]) for x in d1]
    dates = [x[0] for x in dc]
    ma = {}
    for i in range(n - 1, len(dc)):
        ma[dc[i][0]] = sum(x[1] for x in dc[i - n + 1:i + 1]) / n

    def get(d):
        i = bisect.bisect_left(dates, d[:10]) - 1
        return ma.get(dates[i]) if i >= 0 else None

    return get


def next_gate(close, ma, prev):
    """滞回状态机：给定当前价、前一日 MA、上一次的闸门值，返回本次闸门。

    取值只有 1.0（底仓全额）与 GATE_CV（底仓打折）两种，中间靠滞回保持。
    """
    if not ENABLED:
        return 1.0
    if ma is None or not ma:
        return 1.0 if prev is None else prev
    if prev is None:
        return GATE_CV if close < ma * (1 + GATE_HYST) else 1.0
    if prev >= 1.0:
        return GATE_CV if close < ma * (1 - GATE_HYST) else 1.0
    return 1.0 if close > ma * (1 + GATE_HYST) else GATE_CV


def series(b4, d1, rg4=None):
    """逐 4H bar 的闸门序列（回测用）。下标 k 的含义 = **在 bar k 期间生效**的闸门。

    rg4 传入时，状态为 DOWN 的那段会重置滞回状态 —— DOWN 时底仓本就是 0，
    闸门值无意义；重置是为了「回到 UP/RANGE 时按当时的价位重新判一次」，
    否则会把上一轮的滞回残留带进来。

    **末尾必须左移一格（`out[j-1] = out[j]`）**，这是本模块 2026-11-18 翻案的核心：
    gate(k) 是拿 close[k] 算出来的，只能作用于「close[k] 之后的收益」，
    也就是 bar k+1。回测里若直接把它乘在 r[k] = close[k]/close[k-1] 上，
    等于先让价格跌完、再回头决定这根该减仓 —— 一格前视，白拿 101 万。
    详见文件头。
    """
    mm = ma_lookup(d1)
    out, cur = [], None
    for k, b in enumerate(b4):
        if rg4 is not None and rg4[k] == "DOWN":
            cur = None
            out.append(1.0)
            continue
        cur = next_gate(b["close"], mm(b["date"]), cur)
        out.append(cur)
    # 左移一格：把「bar k 收盘时算出的闸门」改成「bar k+1 期间生效的闸门」
    return [1.0] + out[:-1]


def describe(gate, state=None):
    """一句人话，给信号表用。"""
    if not ENABLED:
        return "底仓闸门已关闭"
    if state == "DOWN":
        return "日线 DOWN，底仓本来为 0"
    if gate >= 1.0:
        return "价格在 MA%d 上方 → 底仓全额" % GATE_MA
    return "价格在 MA%d 下方 → 底仓 %d%%" % (GATE_MA, round(gate * 100))


if __name__ == "__main__":
    print(f"底仓闸门{'开' if ENABLED else '关'}｜MA{GATE_MA} 打折到 {GATE_CV:.2f} "
          f"滞回 ±{GATE_HYST:.1%}")
