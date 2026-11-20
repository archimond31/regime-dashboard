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
  权重不是固定表，而是**按本金自动切换**（weights_for）：
    小额（约 ≤3.6 万 U）：12 币等权 9.2%，AAVE/LINK 降到 4%（盘口最薄的两个）。
    大额：按容量做水位填充，单币权重上限 25%，差额留现金。
  切换点不拍脑袋，是「小额表的最大单笔冲击是否突破 3%」自动判定的。
  当前 5000 U 下小额表最大冲击只有 0.41%，所以仍走小额表。

  50 万本金下的实测（_exp_yearly.py 的【4】，口径 A：本金恒 50 万、每年提走、
  各年收益率简单相加，2020-09 起 7 年，含 2020 的 4 个月不完整年）：
    方案                单笔冲击  7 年累计盈亏   最差年
    只买 BTC              7.1%      +459 万     -72.6%
    12 币 AAVE/LINK 4%   40.8%      +390 万     -43.6%
    容量填充 无上限       3.0%      +386 万     -60.6%
    归一至满仓（100%）    3.5%      +377 万     -59.0%
    12 币 等权           62.5%      +354 万     -45.6%
    容量填充 + 单币 25%   3.0%      +310 万     -51.9%   ← 当前采用
    容量填充 + 单币 20%   3.0%      +262 万     -45.5%

  单调规律：单币上限越松 → 收益越高、回撤越大（262 → 310 → 386 万）。
  25% 这一档**收益排倒数第二**，选它的理由不是收益，是它同时卡住两条线：
    · 单笔冲击 3.0%（≤20% 同档；其余方案有 9~10 个币超标，属纸上收益）
    · 单币集中度可接受（无上限那档 BTC+HYPE 占 64.6%，冲击虽合规但结构不可接受）
  **这是约束的交点，不是收益最优解。** 50 万对 12 币策略本来就偏大 ——
  分散表的经济容量上限约 3.6 万，再往上只能靠集中到深盘口币硬撑。

  == 旧数字作废（2026-11-18 更正）==
  本注释曾写「12 币等权 7.09× / 25% 7.81× / 无上限 9.78×」，出自 _exp_capw.py：
  固定 50 万权重跑全复利、滑点按 0.09 中性参考。它等于假设净值涨到 700 万时
  仍按 50 万的权重下单 —— 单笔冲击是红线的 14 倍，是幻觉。
  _exp_cum_b.py 做了「每年按当年本金重算权重」的修正（口径 B，可重跑）：
  **10.65×（年化 +46.3%、最大回撤 -36.1%、Calmar 1.28）**，50 万 → 533 万。
  三个数别混：10.65×（诚实，口径 B）/ 27.95×（固定权重全复利，口径 C，幻觉）
  / 5.82×（2026-11-19 旧口径 B，RANGE DOWN=0 时代，已作废）。
  **口径 B 与 C 的唯一差别就是「权重是否随净值放大」**：净值涨 → 容量塞不下 →
  实际投入比例从 2020 年的 85.9% 衰减到 2026 年的 13.9%，86% 的钱趴在现金上。
  容量约束吃掉 66.6% 的最终净值（31.92× → 10.65×），但也因此自带一层被动降杠杆，
  回撤从 -54.7% 收窄到 -36.1%。**报收益用 B，比 Calmar 才借 C。**

== 波动目标化（2026-11-17 起，默认开）==
  上面的权重是「钱的分配」，还不等于「风险的分配」：高波动币年化波动 90~160%、
  BTC 40~60%，同样 9% 的钱，风险差 2 倍。target() 多一个 scale 参数，
  由 vol_target.py 按 1/σ 给出（已含 κ，平均总敞口保持约 0.66× 不变）。
  **只缩金额、不改方向**：高波动时底仓、加仓层、空头一起缩。
  关闭：环境变量 SKEL_VT=0。评估过程见 05_波动目标化_VT60.md。

回测与全部敏感性检验见 _bt_5000.py（产物 _bt_5000.html）。
12 币方案评估见 _exp_12coins.py。
逐年表现、口径 A 对照与容量修正见 _exp_yearly.py（产物 _yearly.log）。
"""
import math

# ---------- 总资金 ----------
CAPITAL = 5000.0          # 起始本金（USDT）；实盘请传入当前实际权益

# ---------- 模式 ----------
ALLOW_SHORT = True        # False = 纯现货（DOWN 时清仓不做空，峰值降到 1.0×）

# ---------- 权重 ----------
# 小额表：容量不是约束时用 —— 尽量分散，只对最薄的两个盘口降权。
WEIGHTS_SMALL = {
    "BTC": 0.092, "ETH": 0.092, "SOL": 0.092,
    "BNB": 0.092, "XRP": 0.092, "UNI": 0.092,
    "HYPE": 0.092, "ZEC": 0.092, "DOGE": 0.092,
    "NEAR": 0.092,
    "AAVE": 0.04, "LINK": 0.04,
}

# 容量约束参数（大额表用）
MAX_IMPACT = 0.03         # 单笔冲击红线：超过最差 4H 额（p5）的 3% 判超标
SINGLE_CAP = 0.25         # 单币权重上限：防止容量填充把仓位全堆到 BTC/HYPE 身上
#
# 【2026-11-19 实测：不要再调这个数】(_exp_weight_cap.py / _exp_weight_quality.py)
#   把 SINGLE_CAP 从 0.25 降到 0.15/0.12/0.10/等权，Calmar 恒在 1.40→1.46→1.77，
#   但**从 2023 年初重新起算全部变差**（PEQ 2.24 vs 基线 2.35）—— 又是「只靠 2022」。
#   根因：本函数**不做归一化**，而除 BTC/HYPE 外的 10 个币早已顶在各自容量上限
#   （LINK 0.7% / AAVE 0.4% / UNI 0.7%），所以降 cap 释放的钱无处可去 → 变现金。
#   Σw 从 85.4% 掉到 44.3%，**降 cap 实质是减仓，不是分散**。
#
#   更硬的天花板（_diag_corr.py）：12 币日收益平均相关性
#     2021 牛市 0.47 / 2025 横盘 0.64 / **2022 熊市 0.72**，且 2022 年 12 币全部下跌。
#   ρ=0.72 时 12 币等权的理论分散极限 = √(0.72+0.28/12) = 0.86× 单币波动；
#   实测 −48.2% vs 单币中位 −70% = 0.69×，**已超过理论极限**。
#   → 权重层面没有更多可榨的，改权重是无效功。想再降回撤只有：降总敞口（等比付钱）
#     或引入 ρ<0.3 的跨资产（本策略框架之外）。

# ---------- 敞口结构（相对单币权重）----------
BASE_RATIO = 1.0          # 底仓：UP/RANGE 时持有
ADD_RATIO = 0.5           # 加仓层：突破后追加（多头叠加 / 空头唯一来源）

# ---------- RANGE 段的底仓系数（2026-11-20 折中档 0.5）----------
# 【当前生效值】RANGE 段：UP→RANGE 给满底仓 1.0；DOWN→RANGE 给半底仓 0.5。
#   震荡 → UP    底仓 (1.0 或 0.5) → 1.0，加回满仓（BASE 表 UP=1.0 自动生效）
#   震荡 → DOWN  底仓 (1.0 或 0.5) → 0.0，清掉  （BASE 表 DOWN=0.0 自动生效）
# 这两条都是状态机翻转的自动结果，不需要额外规则。
#
# 【三档历史】2026-11-19 曾按来路区分（UP 1.0 / DOWN 0.0），2026-11-20 先改成一律 1.0
# （用户要求「震荡一律允许开底仓」），再折中到 DOWN=0.5。
#
# 【三臂实测（_exp_range_all.py，12 币 404 段，口径 A 本金恒 50 万）】
#   臂                     累计      回撤      Calmar   年化
#   A0 UP1.0/DN0.0        263 万   −48.2%     0.90     —
#   A1 UP1.0/DN0.5       ~291 万   −54.9%     0.82   +1.61pp   ← 当前
#   A2 UP1.0/DN1.0        319 万   −61.2%     0.75   +2.42pp
#   → 收益与回撤**同步线性放大**（每 0.5 档 ≈ +28 万 / −6.8pp），Calmar 单调下降。
#
# 【性质判定：这是 beta，不是 alpha】
#   DOWN→RANGE 的 201 段：赢面 53%、赢时平均 +12.23%、输时平均 −12.88%，
#   是几乎完美对称的随机游走，均值 +0.49% 小到无法覆盖波动。
#   → 加回来的底仓等同于「在横盘期加杠杆」，不是发现了新信息。
#   → 无前视（_exp_range_all 【2】：左移一格 0 万差异）。
#
# 【2024-2026 段三臂回撤都是 −37%】恶化**全部来自 2022**，晚近区间不受这条规则影响。
# 之前失败的同类尝试（不要重试）：
#   · base_gate MA50+滞回 —— 修正一格前视后 275 万 < 302 万，负贡献
#   · P0-3 ATR 吊灯 / 90 天强制复核 —— 全部否决
#   · RANGE 内「跌破前 20 根低点」减仓 —— 触发仅 5.1%，且打不过无脑降仓
#   三者都是在**预测 RANGE 会往哪边破**；按来路分类不是预测，但也已被证明无 alpha。
RANGE_RATIO_BY_SRC = {"UP": 1.0, "DOWN": 0.5}
RANGE_RATIO_DEFAULT = 1.0   # 来路不明（数据起点/预热期）时不给半仓，按满仓处理

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


def _vt_scale():
    """VT 的典型系数 κ。容量按它算，不用极端峰值 2.44 —— VT 本就会在高波动时缩仓，
    再用峰值等于对同一件事惩罚两次。极端峰值出现在少数日子，靠拆单处理。"""
    import vol_target as _vt
    return _vt.KAPPA


def peak_weight(w, vt_scale=None):
    """权重 w 在「满峰值 + 典型波动系数」下的敞口，相对本金。"""
    s = _vt_scale() if vt_scale is None else vt_scale
    return w * (BASE_RATIO + ADD_RATIO) * s


def max_impact_of(weights, capital):
    """该权重表在给定本金下的最大单笔冲击占比。"""
    best = 0.0
    for c, w in weights.items():
        qv = CAP_4H_WORST.get(c)
        if qv and w > 0:
            best = max(best, capital * peak_weight(w) / qv)
    return best


def capacity_weights(capital, max_impact=MAX_IMPACT, single_cap=SINGLE_CAP):
    """容量约束下的权重：等权起步 → 超限的币封顶 → 余额在未封顶的币之间续分。

    Σ 可能小于 1（容量不够满仓），差额作为现金 —— 不做归一化，
    因为归一化会把每个币重新顶回超标区，等于自欺欺人。
    """
    coins = list(CAP_4H_WORST)
    s = _vt_scale()
    unit = peak_weight(1.0, s)
    cap = {c: min(single_cap, max_impact * CAP_4H_WORST[c] / (capital * unit))
           for c in coins}
    w = {c: 1.0 / len(coins) for c in coins}
    for _ in range(100):
        over = {c for c in coins if w[c] >= cap[c] - 1e-12}
        if not over:
            return w
        rest = [c for c in coins if c not in over]
        free = 1.0 - sum(cap[c] for c in over)
        if not rest or free <= 1e-12:
            return {c: cap[c] for c in coins}
        new = {c: (cap[c] if c in over else min(free / len(rest), cap[c]))
               for c in coins}
        if max(abs(new[c] - w[c]) for c in coins) < 1e-12:
            return new
        w = new
    return w


def weights_for(capital):
    """按本金选权重表：小额分散、大额受容量约束。切换点自动判定，不写死。"""
    if max_impact_of(WEIGHTS_SMALL, capital) <= MAX_IMPACT:
        return dict(WEIGHTS_SMALL)
    return capacity_weights(capital)


WEIGHTS = weights_for(CAPITAL)      # 当前本金的生效权重


def target(capital, coin, state, adding=False, closing=False, scale=1.0,
           base_gate=1.0, range_src=None):
    """该币当前应持有的**名义金额**（USDT，负数=空头），以及一句人话说明。

    state:     "UP" / "RANGE" / "DOWN"（日线 G 状态机）
    adding:    4H 已突破前 20 根极值，可以上到峰值
    closing:   日线已翻转、与原仓位方向相反 —— 此时目标是 0（先平，不反手）
    scale:     波动目标化系数（vol_target.py 给出，含 κ）。1.0 = 不缩放。
               只缩金额、不改方向 —— 所以 DOWN 的空头同样会被缩。
    base_gate: 底仓闸门（base_gate.py 给出）。只作用于**底仓**，不影响加仓层。
               1.0 = 全额；0.25 = 价格已跌破 MA50，底仓只留 25%。
               加仓层不动，是因为它由 4H 突破触发，与日线趋势过滤无关。
    range_src: 当前 RANGE 段的**来路**（"UP" / "DOWN" / None）。
               只有 RANGE 状态才用。查 RANGE_RATIO_BY_SRC 决定底仓比例
               （当前 UP→1.0 / DOWN→0.5）。None（数据起点）取 DEFAULT=1.0。
    """
    w = weights_for(capital).get(coin)
    if w is None:
        return 0.0, f"{coin} 不在配置表里"
    base = capital * w * BASE_RATIO * scale * base_gate
    add = capital * w * ADD_RATIO * scale

    if closing:
        return 0.0, "日线已翻转 → 先平仓（反手等下一根 4H）"

    if state == "UP":
        return (base + add if adding else base,
                "日线 UP｜" + ("4H 已突破 → 多头满峰值" if adding else
                               ("未突破 → 底仓做多" if base_gate >= 1.0
                                else "未突破 → 底仓打折 %d%%" % round(base_gate * 100))))
    if state == "RANGE":
        # RANGE 是**持有期**，不开新仓（回测里 signals 只处理 UP/DOWN 的突破）。
        # 但底仓照给：按来路查 RANGE_RATIO_BY_SRC（当前 UP→1.0 / DOWN→0.5）。
        # 离开 RANGE 时的底仓由状态自动接管 —— 转 UP 沿用 1.0（留着），
        # 转 DOWN 归 BASE 表的 0.0（清掉），不需要在这里写额外规则。
        rr = (RANGE_RATIO_BY_SRC.get(range_src, RANGE_RATIO_DEFAULT)
              if range_src else RANGE_RATIO_DEFAULT)
        src_txt = range_src if range_src else "未知"
        if rr <= 0:                      # 防御性分支：当前配置下不会触发
            return (0.0, f"日线 RANGE（来路 {src_txt}）｜按配置底仓归零，不开新仓")
        return (base * rr,
                f"日线 RANGE（来路 {src_txt}）｜底仓持有 {round(rr * 100)}%"
                + ("" if base_gate >= 1.0
                   else "，且闸门再打 %d%%" % round(base_gate * 100))
                + "，不开新仓")
    # DOWN
    if not ALLOW_SHORT:
        return 0.0, "日线 DOWN → 清仓（现货模式不做空）"
    if adding:
        return (-add, "日线 DOWN｜4H 已跌破 → 做空 %d U" % round(add))
    return (0.0, "日线 DOWN｜未跌破 → 空仓等信号")


def split(capital, coin, scale=1.0, base_gate=1.0):
    """把单币额度拆成 (底仓, 加仓层, 空头峰值)。用于下单时预留资金。"""
    w = weights_for(capital).get(coin, 0.0)
    base = capital * w * BASE_RATIO * scale * base_gate
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


def cap_ceiling(coin, capital=None, max_impact=0.03, vt=True):
    """该币在该本金对应权重下，能把「单笔冲击 ≤ max_impact」的最大本金（USDT）。

    注意这里用**极端峰值系数**（HI×κ≈2.44），比 weights_for 定容量时的典型 κ 更严，
    所以它是「保守参考」而不是「配置依据」。超过它就要拆单 + 分批成交。
    """
    cap0 = capital if capital is not None else CAPITAL
    worst = CAP_4H_WORST.get(coin)
    w = weights_for(cap0).get(coin, 0.0)
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
    for c, w in sorted(weights_for(cap).items(), key=lambda kv: -kv[1]):
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
