#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合仓位配置 —— 唯一真源。

改这里就等于改实盘，不需要动 live_signals.py / gen_signal_html.py。

三条设计依据（都不来自「回测择优」）：
  1. 权重基准等权。唯一的偏离是 CRV 降到 7%，理由是**容量**：
     5000 U 下它单笔峰值 525 U，对最差的 4H 成交额（3.27 万）占 1.6%，
     属于「必须拆单但可接受」的上沿；再高就打盘口了。
  2. HYPE **不降权**。曾按「样本短 + 日线状态机在它身上失效」把它降到 5%，
     回测显示降权后收益降、回撤反而升（-15.4% → -17.8%）——
     它与 AAVE 的相关性只有 0.06，是组合里少见的真分散来源，砍掉得不偿失。
  3. 峰值敞口 1.0×（不借钱、不做空）。2026 单年看 1.5× 更赚，
     但把起点拉到 2025-01，1.5× 的回撤是 -34.9%、1.0× 是 -23.1%，
     单位回撤收益 1.34 vs 1.61 —— 放大敞口在跨周期上是亏的。

回测与全部敏感性检验见 _bt_5000.py（产物 _bt_5000.html）。
"""
import math

# ---------- 总资金 ----------
CAPITAL = 5000.0          # 起始本金（USDT）；实盘请传入当前实际权益

# ---------- 权重（占总权益，合计 1.00）----------
WEIGHTS = {
    "BTC": 0.11, "ETH": 0.11, "SOL": 0.11,
    "BNB": 0.10, "XRP": 0.10, "LINK": 0.10,
    "UNI": 0.10, "AAVE": 0.10, "HYPE": 0.10,
    "CRV": 0.07,
}

# ---------- 敞口结构 ----------
PEAK_EXPOSURE = 1.0       # 单币峰值名义 / 单币分到的钱。1.0 = 最多用满，不借钱
BASE_SHARE = 2.0 / 3.0    # 峰值里底仓占 2/3，剩 1/3 留给突破加仓

# ---------- 容量红线 ----------
# 近 90 天最差的单根 4H 成交额（USD）。单笔超过它的 1% 就要拆单，
# 超过 3% 直接判超标（滑点会吃掉这笔交易的大半 R）。
CAP_4H_WORST = {
    "BTC": 23834165, "ETH": 6416149, "SOL": 2596253, "BNB": 1563138,
    "XRP": 1157296, "LINK": 123570, "CRV": 32654, "UNI": 486247,
    "AAVE": 275452, "HYPE": 6826117,
}

# 交易所最小下单额（USDT）。低于此值不下单，避免碎单被拒。
MIN_NOTIONAL = 5.0
# 单币低于这个金额就不建仓了 —— 5000 U × 7% = 350，再低的品种不值得占一个位置
MIN_POSITION = 50.0


def target(capital, coin, state, adding=False):
    """该币当前应持有的**名义金额**（USDT），以及一句人话说明。

    state: "UP" / "RANGE" / "DOWN"（日线 G 状态机）
    adding: 4H 是否已突破前 20 根极值、可以上到满峰值
    """
    w = WEIGHTS.get(coin)
    if w is None:
        return 0.0, f"{coin} 不在配置表里"
    peak = capital * w * PEAK_EXPOSURE
    if state == "DOWN":
        return 0.0, "日线 DOWN → 清仓"
    base = peak * BASE_SHARE
    if state == "RANGE":
        # RANGE 只做多且需突破，底仓照持，加仓层等触发
        return (peak if adding else base,
                "日线 RANGE｜" + ("已突破 → 满峰值" if adding else "未突破 → 仅底仓"))
    return (peak if adding else base,
            "日线 UP｜" + ("已突破 → 满峰值" if adding else "未突破 → 仅底仓"))


def split(capital, coin):
    """把单币额度拆成 (底仓, 加仓层)。用于下单时预留下单资金。"""
    peak = capital * WEIGHTS.get(coin, 0.0) * PEAK_EXPOSURE
    return peak * BASE_SHARE, peak * (1 - BASE_SHARE)


def cap_check(amount, coin):
    """返回 (冲击占比, 判定)。判定：宽松 / 可接受 / 需拆单 / 超标。"""
    worst = CAP_4H_WORST.get(coin)
    if not worst or amount <= 0:
        return 0.0, "—"
    imp = amount / worst
    if imp < 0.005:
        return imp, "宽松"
    if imp < 0.010:
        return imp, "可接受"
    if imp < 0.030:
        return imp, "需拆单"
    return imp, "超标"


def orders_needed(amount, coin):
    """单笔要拆成几份。以「每份不超过最差 4H 额的 0.8%」为准，至少 1 份。"""
    worst = CAP_4H_WORST.get(coin)
    if not worst or amount <= MIN_NOTIONAL:
        return 1
    return max(1, math.ceil(amount / (worst * 0.008)))


def table(capital=None):
    """配置全表，供终端 / HTML / 邮件复用。"""
    cap = capital if capital is not None else CAPITAL
    rows = []
    for c, w in sorted(WEIGHTS.items(), key=lambda kv: -kv[1]):
        base, add = split(cap, c)
        peak = base + add
        imp, flag = cap_check(peak, c)
        rows.append({"c": c, "w": w, "base": base, "add": add, "peak": peak,
                     "imp": imp, "flag": flag, "slices": orders_needed(peak, c)})
    return rows


if __name__ == "__main__":
    tot = sum(WEIGHTS.values())
    print(f"权重合计 {tot:.4f}  本金 {CAPITAL:,.0f} U  峰值敞口 {PEAK_EXPOSURE:.1f}×")
    print("-" * 76)
    print(f"{'币':<6}{'权重':>7}{'底仓':>10}{'加仓层':>10}{'峰值':>10}{'冲击':>9}{'判定':>8}{'拆单':>6}")
    print("-" * 76)
    for r in table():
        print(f"{r['c']:<6}{r['w'] * 100:>6.0f}%{r['base']:>10,.0f}{r['add']:>10,.0f}"
              f"{r['peak']:>10,.0f}{r['imp'] * 100:>8.2f}%{r['flag']:>8}{r['slices']:>6}")
    print("-" * 76)
    b = sum(r["base"] for r in table())
    p = sum(r["peak"] for r in table())
    print(f"底仓合计 {b:,.0f} U（{b / CAPITAL * 100:.0f}% 本金）"
          f"   峰值合计 {p:,.0f} U（{p / CAPITAL * 100:.0f}% 本金）")
