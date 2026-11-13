#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合仓位配置 —— 唯一真源。

改这里就等于改实盘，不需要动 live_signals.py / gen_signal_html.py。

== 费率结构（不对称，这是这套策略的核心特征）==
  日线 UP   + 4H 突破前 20 根高点 → 名义 +1.5 × 权重（多头）
  日线 UP   + 未突破              → 名义 +1.0 × 权重（底仓）
  日线 RANGE                      → 名义 +1.0 × 权重（底仓持有，不开新仓）
  日线 DOWN + 未突破              → 0
  日线 DOWN + 4H 跌破前 20 根低点 → 名义 **-0.5** × 权重（空头）

  多头峰值 1.5×，空头峰值 0.5× —— 不是对称的。原因：
  DOWN 时底仓(base)归零，只剩加仓层(ADD=0.5)能做空。
  这个不对称是有意的：现货能做的只有多头，策略先用满；空头只在上层补一刀。
  2026 年这一刀贡献了 +69.1R，而多头全层只有 +30.2R。

== 权重 ==
  基准等权。唯一偏离是 CRV 降到 7%，理由是**容量**：
  5000 U 下它空头峰值 175 U / 多头峰值 525 U，对最差的 4H 成交额（3.27 万）占 1.6%，
  属于「必须拆单但可接受」的上沿；再高就打盘口了。
  HYPE 保持 10% —— 曾按「样本短 + 状态机失效」降到 5%，回测显示收益 -4.2pp、
  回撤反而 -2.4pp（-15.4% → -17.8%）。它与 AAVE 的相关性只有 0.06，是组合里的减震器。

回测与全部敏感性检验见 _bt_5000.py（产物 _bt_5000.html）。
"""
import math

# ---------- 总资金 ----------
CAPITAL = 5000.0          # 起始本金（USDT）；实盘请传入当前实际权益

# ---------- 模式 ----------
ALLOW_SHORT = True        # False = 纯现货（DOWN 时清仓不做空，峰值降到 1.0×）

# ---------- 权重（占总权益，合计 1.00）----------
WEIGHTS = {
    "BTC": 0.11, "ETH": 0.11, "SOL": 0.11,
    "BNB": 0.10, "XRP": 0.10, "LINK": 0.10,
    "UNI": 0.10, "AAVE": 0.10, "HYPE": 0.10,
    "CRV": 0.07,
}

# ---------- 敞口结构（相对单币权重）----------
BASE_RATIO = 1.0          # 底仓：UP/RANGE 时持有
ADD_RATIO = 0.5           # 加仓层：突破后追加（多头叠加 / 空头唯一来源）

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
# 单币低于这个金额就不建仓了
MIN_POSITION = 50.0


def target(capital, coin, state, adding=False, closing=False):
    """该币当前应持有的**名义金额**（USDT，负数=空头），以及一句人话说明。

    state:   "UP" / "RANGE" / "DOWN"（日线 G 状态机）
    adding:  4H 已突破前 20 根极值，可以上到峰值
    closing: 日线已翻转、与原仓位方向相反 —— 此时目标是 0（先平，不反手）
    """
    w = WEIGHTS.get(coin)
    if w is None:
        return 0.0, f"{coin} 不在配置表里"
    base = capital * w * BASE_RATIO
    add = capital * w * ADD_RATIO

    if closing:
        return 0.0, "日线已翻转 → 先平仓（反手等下一根 4H）"

    if state == "UP":
        return (base + add if adding else base,
                "日线 UP｜" + ("4H 已突破 → 多头满峰值" if adding else "未突破 → 底仓做多"))
    if state == "RANGE":
        # 回测里 RANGE 不进新仓（signals 只处理 UP/DOWN），底仓照持
        return (base, "日线 RANGE｜底仓持有，不开新仓")
    # DOWN
    if not ALLOW_SHORT:
        return 0.0, "日线 DOWN → 清仓（现货模式不做空）"
    if adding:
        return (-add, "日线 DOWN｜4H 已跌破 → 做空 %d U" % round(add))
    return (0.0, "日线 DOWN｜未跌破 → 空仓等信号")


def split(capital, coin):
    """把单币额度拆成 (底仓, 加仓层, 空头峰值)。用于下单时预留资金。"""
    w = WEIGHTS.get(coin, 0.0)
    base = capital * w * BASE_RATIO
    add = capital * w * ADD_RATIO
    return base, add, (-add if ALLOW_SHORT else 0.0)


def cap_check(amount, coin):
    """返回 (冲击占比, 判定)。amount 取绝对值。"""
    worst = CAP_4H_WORST.get(coin)
    amount = abs(amount)
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
    """单笔要拆成几份。以「每份不超过最差 4H 额的 1%」为准，至少 1 份。

    1% 对应上面 cap_check 里「需拆单」档的下沿，两边口径保持一致。
    """
    worst = CAP_4H_WORST.get(coin)
    amount = abs(amount)
    if not worst or amount <= MIN_NOTIONAL:
        return 1
    return max(1, math.ceil(amount / (worst * 0.01)))


def margin_need(capital=None):
    """多头满峰值时的**杠杆占用**（相对权益）。

    多头峰值 = Σ w × (BASE + ADD) = 1.5 × 本金，其中 1.0 是真金、0.5 是借的。
    空头峰值 0.5×，永续按 10% 保证金算只占 0.05×。
    """
    cap = capital if capital is not None else CAPITAL
    return {"long_peak": cap * (BASE_RATIO + ADD_RATIO),
            "long_own": cap * BASE_RATIO,
            "long_borrow": cap * ADD_RATIO,
            "short_peak": cap * ADD_RATIO if ALLOW_SHORT else 0.0}


def table(capital=None):
    """配置全表，供终端 / HTML / 邮件复用。"""
    cap = capital if capital is not None else CAPITAL
    rows = []
    for c, w in sorted(WEIGHTS.items(), key=lambda kv: -kv[1]):
        base, add, short = split(cap, c)
        peak = base + add
        imp, flag = cap_check(peak, c)
        rows.append({"c": c, "w": w, "base": base, "add": add, "peak": peak,
                     "short": short, "imp": imp, "flag": flag,
                     "slices": orders_needed(peak, c),
                     "slices_short": orders_needed(short, c)})
    return rows


if __name__ == "__main__":
    tot = sum(WEIGHTS.values())
    mg = margin_need()
    print(f"权重合计 {tot:.4f}   本金 {CAPITAL:,.0f} U   "
          f"模式 {'合约（含做空）' if ALLOW_SHORT else '纯现货'}")
    print(f"多头峰值 {mg['long_peak']:,.0f} U（真金 {mg['long_own']:,.0f} + 借 "
          f"{mg['long_borrow']:,.0f}）   空头峰值 {mg['short_peak']:,.0f} U")
    print("-" * 84)
    print(f"{'币':<6}{'权重':>7}{'底仓':>9}{'加仓层':>9}{'多头峰值':>10}{'空头峰值':>10}"
          f"{'冲击':>9}{'判定':>8}{'拆单':>6}")
    print("-" * 84)
    for r in table():
        print(f"{r['c']:<6}{r['w'] * 100:>6.0f}%{r['base']:>9,.0f}{r['add']:>9,.0f}"
              f"{r['peak']:>10,.0f}{r['short']:>10,.0f}{r['imp'] * 100:>8.2f}%"
              f"{r['flag']:>8}{r['slices']:>6}")
    print("-" * 84)
    t = table()
    b = sum(r["base"] for r in t)
    p = sum(r["peak"] for r in t)
    s = sum(r["short"] for r in t)
    print(f"底仓合计 {b:,.0f} U（{b / CAPITAL * 100:.0f}% 本金）"
          f"   多头峰值 {p:,.0f} U（{p / CAPITAL * 100:.0f}%）"
          f"   空头峰值 {s:,.0f} U（{abs(s) / CAPITAL * 100:.0f}%）")
