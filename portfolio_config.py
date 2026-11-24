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
# 实盘请改成当前**实际权益**，不要填「打算投入的钱」。
# 2026-11-23 由 5000 下调到 2000（用户改用 2000U 试跑）。
#
# 为什么改本金这件事比看起来危险：
#   平仓类工单的金额是 ticket.py 用 `CAPITAL × w × k` **现算**的，不是读入仓台账。
#   所以「有仓在场时改 CAPITAL」，平仓单会按新本金写金额 —— 5000 建的仓按 2000 平，
#   只平掉 40%。安全切换点 = 空仓时（或先把在仓工单用旧本金结算掉）。
CAPITAL = 2000.0          # 起始本金（USDT）；实盘请传入当前实际权益

# ---------- 模式：策略能力 vs 账户执行，两个开关不能合并 ----------
ALLOW_SHORT = True        # 策略层：算不算空头。False = 连信号都不产生（峰值降到 1.0×）
#
# 2026-11-24 曾一度改成 False，随即回滚。原因：
#   用户的意思是「策略要保持能做空，只是这一个阶段没有做空信号 / 我不执行」，
#   不是「把这个策略改成纯多头」。用 ALLOW_SHORT=False 去表达「不执行空头」
#   等于把信号生成能力也一起砍了 —— 回测口径、空头研究、以及「哪天想做空」
#   全都没了，而且 _q_longonly.py 量到的是另一个策略（年化 121.4%→101.2%）。
#
# 真正的区分在下面这个开关：
EXECUTE_SHORT = True      # 执行层：空头工单要不要真的下到账户。
#
# 2026-11-24 也曾一度设成 False，同样回滚了。用户的表态是
# 「有空信号出现肯定是要空的」—— 空头不是"不做的可选项"，是策略的一半。
# 这个开关保留，只作为「哪天只想跑纯现货」的应急闸门，默认必须是 True。
#
# 那么「历史模拟空头」怎么办（11-09 入场、从未下过单的那几笔）？
#   不归这个开关管，归**持仓基线**管：PC.executable(..., has_pos=)
#   has_pos 由 live_signals 从工单台账推（有没有 done 的「开仓·空」）。
#   没建过的空头，平仓/止损时没有可平的量 —— 那几个动作降级为纸面，
#   而**新出现的开仓·空 一律照发**（那才是"有信号就空"）。
#
# 两条净敞口也必须都印（见 ticket.render 的资金条）：
#   策略书 +424 U(0.21×)（含未建仓的空头）vs 实盘 +1,004 U(0.50×)，差 2.4 倍。
#   风控看实盘，判断策略状态看策略书。

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
# 【三档历史】2026-11-19 按来路区分（UP 1.0 / DOWN 0.0）→ 2026-11-20 上午一律 1.0
# → 同日下午折中 0.5（当前）→ 同日晚间曾试改 0.0，**验证不通过，已回滚**。
# 引用前先看下面这行实际值，别凭印象。
#
# 【为什么试了 0.0 又退回来：它是一个「2022 单事件」】
# 初始证据（_exp_dd15.py，只看了两个窗口的整体指标）：
#   全期 Calmar 2.20→2.73、22 起 1.75→2.14，收益几乎不动 → 看着像白送的降险。
# 但 _verify_dn0.py 的 walk-forward 一做就露了：
#   ① 分窗口 2024 起（不含 2022 的独立样本）：Calmar **4.21→3.73**、年化 **−6.2pp**、
#      回撤反而加深 1.7pp —— 剔除 2022 后是负贡献。
#   ② 分币：12 币里只有 **6 个**回撤改善（BTC +7.5pp / LINK +3.5 / HYPE +3.3 好；
#      NEAR −5.1 / UNI −3.9 / DOGE −2.7 差）。不是普遍改善。
#   ③ 滚动 250 日（22 起）：回撤更浅 **78/154 = 51%**，收益更高 39% —— 硬币。
#   ④ 逐年：改善集中在 2022（回撤 −41.9%→−34.3%），2023 −4.4pp、2024 −1.7pp 全变差。
# → 与 cross_fix_init（BNB 2018 一个窗口占 92% 正收益）完全同构：**单事件驱动，不达标**。
#
# 【口径 A 的数字看着支持 0.0，但那是同一件事的另一面】
#   口径 A：DN0.0 263 万 / −48.2% / Calmar 1.40；DN0.5 291 万 / −54.9% / Calmar 1.29。
#   Calmar 上升是真的，但它同样只在 2022 那一年兑现。**别把「2022 少亏」当成稳定特征。**
#
# 【性质：这是风险偏好选择，不是策略改进】
#   DOWN→RANGE 的 201 段：赢面 53%、赢时 +12.23%、输时 −12.88%，几乎完美对称的随机游走，
#   均值 +0.49% 覆盖不了波动 → DN>0 的收益纯粹来自「敞口更大」，没有信息；
#   反过来 DN=0 的「少亏」也纯粹来自「不参与」，同样没有信息。
#   选哪一档取决于你更怕 2022 那种回撤，还是更在意长期收益 —— **不是哪个更对**。
#   → 无前视（_exp_range_all 【2】：左移一格 0 万差异）。
#
# 【已验证失败的同类尝试（不要重试）】
#   · base_gate MA50+滞回 —— 修正一格前视后 275 万 < 302 万，负贡献
#   · P0-3 ATR 吊灯 / 90 天强制复核 —— 全部否决
#   · RANGE 内「跌破前 20 根低点」减仓 —— 触发仅 5.1%，打不过无脑降仓
#   · 组合级回撤闸门（DD>10% 减半仓）—— 叠在 DN=0 上回撤反而恶化到 −38.8%（_exp_dd15）
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
# 【注意：这一行目前是死配置，代码里没有任何地方读它】
# 故意不接线 —— 一旦按它跳过，2000U 下 AAVE/LINK 会被静默剔除，
# 等于「暗中减币」，而减币正是本策略最不该做的事（见文件头分散化那条）。
# 真实的约束是下面 MIN_ORDER（交易所硬门槛），由 live_signals 提示、不静默跳过。
MIN_POSITION = 50.0

# ---------- 各合约真正的最小可下单金额（USDT）----------
# 口径：`max(MIN_NOTIONAL 过滤器, minQty × 当时价格)` —— 两个都要满足。
# 数据快照：2026-11-23 `fapi.binance.com/fapi/v1/exchangeInfo`（USDT 永续，
# 注意必须精确匹配 `SYMBOL+USDT`，用 `[-4:]` 切会把 BTCUSDC / BTCUSD1 也切进来）。
# 价格取当日 _signal.json，所以 BTC 这一格随价格漂移（0.001 × 价格）；
# 它只是「够不够下单」的量级判据，不参与仓位计算，漂 10% 不影响结论。
MIN_ORDER = {
    "BTC": 61.3,    # minNotional 50，但 minQty 0.001×61271 = 61.3 更紧
    "ETH": 20.0,    # minNotional 20（minQty 只值 1.7）
    "LINK": 20.0,   # minNotional 20
    "AAVE": 9.8,    # minQty 0.1×98.06
    "BNB": 5.6,     # minQty 0.01×559.59
    "SOL": 5.0, "XRP": 5.0, "UNI": 5.0, "HYPE": 5.0,
    "ZEC": 5.0, "DOGE": 5.0, "NEAR": 5.0,
}
MIN_ORDER_ASOF = "2026-11-23"


def min_order_value(coin):
    """该合约能挂出去的最小名义金额（USDT）。不在表里按 MIN_NOTIONAL 处理。"""
    return MIN_ORDER.get(coin, MIN_NOTIONAL)


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


def executable(action, side=0, pos_side=0, has_pos=True):
    """这条动作实盘要不要真的下单。**策略算得出 ≠ 账户要执行。**

    两个判据叠在一起，缺一个都会出错：
      · EXECUTE_SHORT —— 账户整体做不做空（应急闸门，默认 True）
      · has_pos       —— **这笔具体的仓建没建过**。空头在 11-09 入场、而系统
                         到 11-24 才上线，那几笔从未下过单；它们进入平仓 / 止损时
                         没有可平的量，发单就是让人去平一个不存在的仓。

    放在这里而不是各处自己判：live_signals 要标在信号上、ticket 要据此过滤工单，
    两处判据不一致就会出现「信号说不执行、工单还是开了」的静默分叉。

    side     ：这一单是买(+1)还是卖(-1)
    pos_side ：在场仓位的方向（+1 多 / -1 空）。平多也是卖，不能只看 side。
    has_pos  ：这笔在场的仓是否真实存在（多单恒 True；空单看工单台账）
    """
    if action in ("开仓", "持有"):
        if side < 0 or pos_side < 0:
            # 开仓·空：有信号就空 —— 这正是 EXECUTE_SHORT 该管的事。
            # 持有·空：本身不下单，但「有没有这笔仓」决定页面上标不标「未建仓」。
            return EXECUTE_SHORT and has_pos
        return True
    if action in ("平仓", "止损临近", "即将到期"):
        if pos_side < 0:
            return EXECUTE_SHORT and has_pos
        return True
    return True          # 建底仓 / 清底仓 都是现货，照常执行


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
          f"策略 {'含做空' if ALLOW_SHORT else '不做空'} / "
          f"执行 {'含做空' if EXECUTE_SHORT else '纯现货'}")
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
