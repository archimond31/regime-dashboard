#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""base_gate.py —— 底仓闸门（趋势过滤）

== 它解决什么 ==
底仓 base=1.0 在 UP/RANGE 恒定，唯一退出通道是「日线状态翻 DOWN」。
但状态机认错有延迟：2022 年信号层 +23R、资金端 −51.9%；2025 年 +122R、−9.7%。
两次亏损都出在底仓 —— 它没有任何独立的风控通道，只能等状态机自己改口。

本模块给底仓加一条**独立于状态机**的趋势闸门：
  当前 4H 收盘 < 前一交易日 MA50 → 底仓留 25%
  当前 4H 收盘 > 前一交易日 MA50 × (1+1%) → 底仓恢复 100%
  中间地带维持上一次的取值（滞回带，防均线附近抖动）

== 为什么是「当前 4H 收盘 vs 前一日 MA」==
MA 必须用**前一交易日**的，因为当日日线在 4H 日内还没收盘，用当日 MA 是未来函数。
收盘价可以用当前这根 4H 的（它已收盘，信息可得）。

**不要改成日频更新。** `_exp_base.py`【9】测过：用「前一交易日收盘 vs 前一日 MA」、
全天不变，2022 年从 −20.6% 掉回 −43.4%，累计 283 万 < 基线 310 万。
效率全部来自 4H 级的及时反应 —— 2022 是快速崩盘，晚一天就晚了。
要降低操作量只能用滞回带，不能降频。

== 验证结果（_exp_base.py，口径 A：本金恒 50 万、每年提走）==
                           累计      2022      2025    滚动胜/负
  基线（无闸门）           310 万    -51.9%    -9.7%      —
  MA50/cv.25/4H+1%滞回     378 万    -20.6%   +10.6%    16/1
  最大回撤 -60.7% → -30.6%
  唯一变差：2026 年 -12.9pp（牛市里底仓被砍，是设计取舍）
  换手 359 次/年（日均 1.4 次，12 币合计）

== 别加码 ==
参数网格是单调的（MA 越短、cv 越小越好，最优点 MA40/cv=0 落在网格边界）。
单调意味着「没有最优点，只有方向」，暗示底仓整体可能是净负 alpha。
但完全取消底仓会把总敞口从 1.5× 压到 0.5×，那是另一个结构问题。
所以这里取网格内部点（MA50/cv=0.25），不取边界解。

== 开关 ==
SKEL_BASEGATE=0 关闭（回到 base 恒定 1.0 的旧行为）。
"""
import bisect
import os

ENABLED = os.environ.get("SKEL_BASEGATE", "1") != "0"
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
    """逐 4H bar 的闸门序列（回测用）。与实盘 analyze() 同一套逻辑。

    rg4 传入时，状态为 DOWN 的那段会重置滞回状态 —— DOWN 时底仓本就是 0，
    闸门值无意义；重置是为了「回到 UP/RANGE 时按当时的价位重新判一次」，
    否则会把上一轮的滞回残留带进来。
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
    return out


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
