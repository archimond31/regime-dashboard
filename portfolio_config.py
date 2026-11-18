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

== 权重（2026-11-18 起：12 币，去 CRV 加 ZEC/DOGE/NEAR）==
  基准 9.2% 等权，两个例外降到 4%：AAVE、LINK。
  理由全部是**容量**，不是看空：
    AAVE p5 单根 4H 成交额只有 12.2 万、LINK 21.8 万，是 12 币里最薄的两个；
    50 万本金下 9.2% 权重的单笔峰值要占掉 30% / 17% 的盘口。
  降权同时改善收益：回测 12 币等权 6.95× → 降权 7.41×，回撤 -47.3% → -45.6%
  （_exp_12coins.py）。原因是薄盘口的滑点本来就该少碰。
  注：50 万本金下 12 币整体已超出单笔 3% 冲击红线，只能靠拆单执行，
  回测数字已经含这份滑点。各币的「可执行资金上限」见 cap_ceiling()。

== 波动目标化（2026-11-17 起，默认开）==
  上面的权重是「钱的分配」，还不等于「风险的分配」：高波动币年化波动 90~160%、
  BTC 40~60%，同样 9% 的钱，风险差 2 倍。target() 多一个 scale 参数，
  由 vol_target.py 按 1/σ 给出（已含 κ，平均总敞口保持约 0.66× 不变）。
  **只缩金额、不改方向**：高波动时底仓、加仓层、空头一起缩。
  关闭：环境变量 SKEL_VT=0。评估过程见 05_波动目标化_VT60.md。

回测与全部敏感性检验见 _bt_5000.py（产物 _bt_5000.html）。
12 币方案评估见 _exp_12coins.py。
"""
import math

# ---------- 总资金 ----------
CAPITAL = 5000.0          # 起始本金（USDT）；实盘请传入当前实际权益

# ---------- 模式 ----------
ALLOW_SHORT = True        # False = 纯现货（DOWN 时清仓不做空，峰值降到 1.0×）

# ---------- 权重（占总权益，合计 1.00）----------
WEIGHTS = {
    "BTC": 0.092, "ETH": 0.092, "SOL": 0.092,
    "BNB": 0.092, "XRP": 0.092, "UNI": 0.092,
    "HYPE": 0.092, "ZEC": 0.092, "DOGE": 0.092,
    "NEAR": 0.092,
    "AAVE": 0.04, "LINK": 0.04,
}

# ---------- 敞口结构（相对单币权重）----------
BASE_RATIO = 1.0          # 底仓：UP/RANGE 时持有
ADD_RATIO = 0.5           # 加仓层：突破后追加（多头叠加 / 空头唯一来源）

# ---------- 容量红线 ----------
# 口径（2026-11-18 统一定义，可复现）：
#   **近 3 个完整自然月**的 4H 现货成交额（按 UTC 4H 桶汇总 quote volume）
#   取 **5% 分位** —— 即「每 20 根 4H 就有 1 根比这更差」，不是百年一遇的极值。
# 单笔超过它的 1% 就要拆单，超过 3% 直接判超标（滑点会吃掉这笔交易的大半 R）。
# 历史坑：2026-11-18 之前这组数是不同时点各测各的快照，
#   和当前实值的比值在 1.7x~9.2x 之间跳动，用它算容量会错好几倍。
CAP_4H_WORST = {
    "BTC": 12864334, "ETH": 4909230, "SOL": 2004788, "BNB": 1058019,
    "XRP": 936095, "UNI": 206208, "HYPE": 15570095, "ZEC": 723117,
    "DOGE": 360810, "NEAR": 269908, "AAVE": 121981, "LINK": 217625,
}
CAP_WINDOW = "2026-08 ~ 2026-10"

# 交易所最小下单额（USDT）。低于此值不下单，避免碎单被拒。
MIN_NOTIONAL = 5.0
# 单币低于这个金额就不建仓了
MIN_POSITION = 50.0


def target(capital, coin, state, adding=False, closing=False, scale=1.0):
    """该币当前应持有的**名义金额**（USDT，负数=空头），以及一句人话说明。

    state:   "UP" / "RANGE" / "DOWN"（日线 G 状态机）
    adding:  4H 已突破前 20 根极值，可以上到峰值
    closing: 日线已翻转、与原仓位方向相反 —— 此时目标是 0（先平，不反手）
    scale:   波动目标化系数（vol_target.py 给出，含 κ）。1.0 = 不缩放。
             只缩金额、不改方向 —— 所以 DOWN 的空头同样会被缩。
    """
    w = WEIGHTS.get(coin)
    if w is None:
        return 0.0, f"{coin} 不在配置表里"
    base = capital * w * BASE_RATIO * scale
    add = capital * w * ADD_RATIO * scale

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


def split(capital, coin, scale=1.0):
    """把单币额度拆成 (底仓, 加仓层, 空头峰值)。用于下单时预留资金。"""
    w = WEIGHTS.get(coin, 0.0)
    base = capital * w * BASE_RATIO * scale
    add = capital * w * ADD_RATIO * scale
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


def cap_ceiling(coin, max_impact=0.03, vt=True):
    """该币在当前权重下，能把「单笔冲击 ≤ max_impact」的最大本金（USDT）。

    峰值敞口 = 本金 × w × (BASE+ADD) × 波动系数上限。
    开了 VT 时系数上限是 HI×κ ≈ 2.44，所以上限比关掉时低 2.44 倍。
    超过这个本金不是不能做，而是要拆单 + 分批成交，滑点会明显变差。
    """
    worst = CAP_4H_WORST.get(coin)
    w = WEIGHTS.get(coin, 0.0)
    if not worst or w <= 0:
        return 0.0
    peak = w * (BASE_RATIO + ADD_RATIO)
    if vt:
        import vol_target as _vt
        peak *= _vt.HI * _vt.KAPPA
    return max_impact * worst / peak


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
          f"{'冲击':>9}{'判定':>8}{'拆单':>6}{'上限':>10}")
    print("-" * 94)
    for r in table():
        ceil = cap_ceiling(r["c"]) / 1e4
        print(f"{r['c']:<6}{r['w'] * 100:>6.0f}%{r['base']:>9,.0f}{r['add']:>9,.0f}"
              f"{r['peak']:>10,.0f}{r['short']:>10,.0f}{r['imp'] * 100:>8.2f}%"
              f"{r['flag']:>8}{r['slices']:>6}{ceil:>9.0f}万")
    print("-" * 94)
    t = table()
    b = sum(r["base"] for r in t)
    p = sum(r["peak"] for r in t)
    s = sum(r["short"] for r in t)
    print(f"底仓合计 {b:,.0f} U（{b / CAPITAL * 100:.0f}% 本金）"
          f"   多头峰值 {p:,.0f} U（{p / CAPITAL * 100:.0f}%）"
          f"   空头峰值 {s:,.0f} U（{abs(s) / CAPITAL * 100:.0f}%）")
