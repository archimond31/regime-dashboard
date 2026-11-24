#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三层骨架回测（BTC / ETH）：L1 日线定方向与底仓，L2 4H 突破加仓，L3 状态翻转/止损/时限出场。

规则（用户指定，2026-11-10）：
  L1 日线 G 状态机（零参数可调）
     UP    → 允许做多，底仓 1.0
     RANGE → 允许做多，底仓 1.0
     DOWN  → 只允许做空，底仓 0.0
  L2 4H 趋势延续触发
     做多：4H 收盘 > 前 20 根 4H 最高价
     做空：4H 收盘 < 前 20 根 4H 最低价
     幅度：+0.5× 仓位（叠加在底仓上，不是替代）
  L3 出场
     主出场：日线状态翻转
     风控：1.5×ATR(14) 硬止损
     兜底：持有满 120 根 4H（20 天）
     不设盈利目标

口径：
  - 4H bar 由 1H 聚合（每 4 根），状态取该 4H bar 末根的 1H regime，
    而该值已对齐到「严格早于当日」的日线，无未来函数。
  - 组合仓位 pos = 底仓(状态) + 0.5 × 信号方向(只在持仓期间)
  - 净值按 4H 复利，调仓按 |Δpos| 收单边 0.04% 成本
  - 收益分解：纯底仓曲线 vs 完整曲线 vs buy&hold
  - **执行层（2026-11-10 口径修正）**：底仓 1.0 走**现货**，不吃资金费；
    只有 L2 的 0.5× 加仓走永续，按 |pos - base| 收资金费。
    DOWN 区间 base=0，空头整仓来自加仓层，funding 符号自动反转（空头收费）。
    对照：若底仓也挂永续，f=3bp/日 会把组合 Calmar 从 1.45 压到 1.18
    （底仓有 70%+ 时间满仓多头，3bp × 0.7 ≈ 7.7%/年的确定拖累）。

输出 skeleton_2025.json，供 gen_skeleton_html.py 画图。
"""
import sys
import os
import json
import statistics
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_1h_v2 as V2
from test_regime_fit import agg4h, expo4
from _proto_4h import atr_wilder
from strategy_1h_rsi import align_regime_safe, regime_and_blood

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "LINK", "CRV", "UNI", "AAVE"]
EXTRA = ["DOGE", "LTC", "TRX", "XLM", "ATOM", "ALGO", "AVAX", "DOT", "NEAR", "SAND"]
# SKEL_N=10 → 原始 10 品种；SKEL_N=20 → 加 10 个扩展品种（保持原始 10 个顺序不变，便于逐币对照）
N = int(os.environ.get("SKEL_N", "20"))
COINS = (COINS + EXTRA)[:N]
OUT_JSON = os.environ.get("SKEL_OUT") or ("skeleton_2025.json" if N <= 10 else f"skeleton{N}.json")
# 板块标签只用于展示与分组解读，不参与任何计算
SECTOR = {
    "BTC": "储值", "ETH": "L1", "SOL": "L1", "BNB": "交易所", "XRP": "支付",
    "ADA": "L1", "LINK": "预言机", "CRV": "DeFi", "UNI": "DeFi", "AAVE": "DeFi",
    "DOGE": "Meme", "LTC": "支付", "TRX": "L1/支付", "XLM": "支付",
    "ATOM": "互操作", "ALGO": "L1", "AVAX": "L1", "DOT": "互操作",
    "NEAR": "L1", "SAND": "游戏",
}
# 只有这两个品种导出逐日 K 线与净值曲线（控制 JSON 体积），其余只出统计与成交
DETAIL = ("BTC", "ETH")
BASE = {"UP": 1.0, "RANGE": 1.0, "DOWN": 0.0}
LOOKBACK = 20
ATR_K = 1.5
MAX_BARS = int(os.environ.get("SKEL_MAXBARS", "120"))
# 追踪止损：0 = 关闭（止损固定不动）；>0 = 止损随「有利方向的最优价」按 K×ATR 上移
# 只在盈利后被激活（TRAIL_ARM 倍初始风险的浮盈才启动），避免一开仓就被噪声扫掉
TRAIL_K = float(os.environ.get("SKEL_TRAIL_K", "0"))
TRAIL_ARM = float(os.environ.get("SKEL_TRAIL_ARM", "1.0"))
# 早期淘汰：入场后第 EARLY_N 根 4H 收盘时，若期间最大浮盈仍 < EARLY_THR×初始风险，
# 就按收盘价平仓（reason="early"），不等 −1R 止损。
# 依据是单笔归因：681 笔在 10 根内被止损、合计 −676R，占着仓位什么都没换来。
# 这些仓位能提前释放给下一笔，而每笔期望是 +0.62R —— 翻台率本身就是收益来源。
EARLY_N = int(os.environ.get("SKEL_EARLY_N", "0"))
EARLY_THR = float(os.environ.get("SKEL_EARLY_THR", "0.4"))
ADD = 0.5          # L2 加仓倍数
# 波动率过滤：只剔除 ATR 分位最高的 15%（S7 平台 75~90%，取中点）
VOL_Q = 0.85
COST = 0.0004      # 单边
START_NAV = "2018-01-01"
# 执行层：底仓走现货（不吃资金费），只有 L2 加仓那 0.5× 走永续
# 3bp/日 ≈ 11%/年，是 BTC 永续牛市 funding 均值附近
FUND_DAILY = 0.0003
SPOT_BASE = True
# init 血统期间禁止开新仓（底仓仍按状态，只是不吃 L2 突破单）。
# 依据：2026-11-17 查 HYPE 2025-04-04~05-22 被判 DOWN 达 48 天（价格 +181.5%），
#   根因是 init_ma120 血统的 DOWN 出口只剩「RSI 破 os 后再升破 os」，上涨行情永不触发；
#   策略在该段反向做空 4 次全部止损（−4R）。
# 对照实验（_exp_hype_fix.py 变体 D）：10 币「上线初期 200 天」样本外 6 改善 / 1 变差，
#   组合 Calmar 0.82→0.84，HYPE 2025 −33.7%→−26.7%，唯一变差是 SOL 上线初期 −44.4pp
#   （2020-12 起的超级牛市，禁开仓错过加仓层）。
# 可用环境变量 SKEL_BLOCK_INIT=0 关掉，便于做 A/B 对照。
BLOCK_INIT = os.environ.get("SKEL_BLOCK_INIT", "1") == "1"
_BLOOD_CACHE = {}


def range_ratio_series(rg4):
    """逐 bar 的「RANGE 底仓系数」。**与闸门正交**，两者相乘。

    【当前配置（2026-11-20 折中档）】UP→RANGE 取 1.0，DOWN→RANGE 取 0.5。
    这个函数本身不写死任何数字，全部查 portfolio_config.RANGE_RATIO_BY_SRC，
    所以改配置就能切换行为，不需要动 base_series / build / live_signals。

    【一天内改过四轮，别只记住其中一版】
      2026-11-19  UP=1.0 / DOWN=0.0   口径A 263 万，回撤 −48.2%，Calmar(C) 1.40
      2026-11-20  UP=1.0 / DOWN=1.0   口径A 319 万，回撤 −61.2%，Calmar(C) 1.18
      2026-11-20  UP=1.0 / DOWN=0.5   口径A 291 万，回撤 −54.9%，Calmar(C) 1.29  ← 当前
      2026-11-20  UP=1.0 / DOWN=0.0   试改后**验证不通过，已回滚**（见下）
    每 0.5 档 ≈ +28 万 / −6.8pp，严格线性 —— 这是**加 beta 不是加 alpha**。
    最干净的证据：RANGE 段自身贡献 0→0.5→1.0 对应 −11.0% / −19.7% / −30.3%，
    与系数严格成比例，而同期 UP 段恒 −30.9%。三臂全表见 _exp_range_all.py。

    【为什么 0.0 看着好、实际不成立（_verify_dn0.py 的 walk-forward 结论）】
      只看两个窗口的整体指标，0.0 像白送的降险：全期 Calmar 2.20→2.73、
      22 起 1.75→2.14 且收益不动。但拆开就不成立：
        · 2024 起（不含 2022 的独立样本）：Calmar 4.21→3.73、年化 −6.2pp；
        · 分币 12 个只有 6 个改善；
        · 滚动 250 日（22 起）回撤更浅仅 78/154 = 51%，收益更高 39%；
        · 逐年：改善全在 2022（−41.9%→−34.3%），2023 −4.4pp、2024 −1.7pp 全变差。
      → **单事件驱动**，与 cross_fix_init（BNB 2018 一个窗口占 92%）同构，不达标。
      → 这属于风险偏好选择，不是策略改进。别再拿「全期 Calmar」当依据。

    **无前视**：来路取 rg4[k-1]，是 bar k 开始前就已知的历史事实，
    所以 out[k] 直接作用于 bar k 的收益是对的，不需要左移一格。
    （_verify_range_src.py【5】把 rg4 整体右移一格，290 万 → 283 万，差 8 万，噪声级。）

    注意 RANGE 段内部必须**沿用进入时的来路**，不能每根重新看前一根 ——
    否则第二根起前一根已是 RANGE，来路就丢了，整段会退回 DEFAULT=1.0。
    _verify_range_src.py 为此专门有一项「临时配置 UP=1.0 / DOWN=0.25 重跑」的
    加固测试：配置两边相等时原有检查会退化成恒真，测不出这个 bug。
    """
    import portfolio_config as PC
    out = [1.0] * len(rg4)
    src = None
    for k in range(len(rg4)):
        s = rg4[k]
        if s == "RANGE":
            if src is None:                       # 段的第一根：记录来路
                prev = rg4[k - 1] if k > 0 else None
                src = prev if prev in ("UP", "DOWN") else None
            out[k] = PC.RANGE_RATIO_BY_SRC.get(src, PC.RANGE_RATIO_DEFAULT)
        else:
            src = None                            # 离开 RANGE 就复位
    return out


def base_series(b4, d1, rg4):
    """底仓系数序列 = 状态基础值 × 底仓闸门 × RANGE 系数。**回测与实盘的唯一入口**。

    · 状态基础值：UP/RANGE = 1.0，DOWN = 0.0（BASE 表）
    · 底仓闸门：价格跌破 MA50 时底仓打折（base_gate.py，默认已关闭）
    · RANGE 系数：按来路查 RANGE_RATIO_BY_SRC（当前 UP→1.0 / DOWN→0.5）

    下标语义：返回值 k 是「在 bar k 期间生效」的系数，即乘在 r[k]=close[k]/close[k-1] 上。
    闸门内部已经左移一格（gate 是拿 close[k] 算的，只能作用于 bar k+1），
    这一步不做就是前视 —— 2026-11-18 为此把整条「底仓退出」结论推翻过一次，见 base_gate.py。
    RANGE 系数不需要左移（只用 rg4[k-1]），所以两者可以安全相乘。

    实盘 live_signals.analyze() 走的是同一个 base_gate.next_gate()，
    只是它只需要最后一根的值（下一根才生效，天然无前视）。
    """
    import base_gate as BG
    gate = BG.series(b4, d1, rg4)
    rr = range_ratio_series(rg4)
    return [BASE.get(rg4[k], 0.0) * gate[k] * rr[k] for k in range(len(rg4))]


def _daily_closes(b4):
    """4H → 日线收盘序列（只保留状态机需要的最小字段）。

    不用 to_daily：agg4h 的输出没有 "t" 字段，而且状态机只看 close。
    语义与 to_daily 一致 —— 取每个交易日的最后一根 4H 收盘。
    """
    out, cur = [], None
    for b in b4:
        d = b["date"]
        if cur is None or cur["date"] != d:
            if cur:
                out.append(cur)
            cur = {"date": d, "close": b["close"]}
        else:
            cur["close"] = b["close"]
    if cur:
        out.append(cur)
    return out


def blood_of(b4, coin=None):
    """每根 4H bar 所属的日线状态血统（用 b4 自己的日线反推，无需原始 1H）。

    与生产口径一致：同一交易日的最后一根 4H 收盘 = 该日 UTC 收盘，
    再经 align_regime_safe 取「严格早于当日」的状态，无未来函数。
    """
    key = (coin, len(b4), b4[0]["date"], b4[-1]["date"])
    hit = _BLOOD_CACHE.get(key)
    if hit is not None:
        return hit
    _s, bm = regime_and_blood(_daily_closes(b4))
    out = align_regime_safe(b4, bm)
    _BLOOD_CACHE[key] = out
    return out


def atr_percentile(b4, atr, window=500, coin=None):
    """ATR/收盘价 的滚动分位（0~1）。用于过滤「波动已经过热」的入场时段。

    test_stop_opt 的 S4/S7 显示：剔除 ATR 分位最高的 10~25% 的入场，
    组合 Calmar 从 1.24 升到 1.35，且 8/10 品种改善、75~90% 是连续平台。

    这一段是 O(bar × window) 的纯 Python 循环（每币约 870 万次比较，跑一次 1 分多钟），
    而它对给定 (coin, 数据版本, window) 是确定性的 —— 消融实验要跑十几遍，
    所以结果落盘缓存，用「首末日期 + 根数」当指纹，数据一变自动失效。
    """
    ratio = [(atr[k] / b4[k]["close"]) if atr[k] else None for k in range(len(b4))]
    cpath = f".cache_pct_{coin}.json" if coin else None
    if cpath and os.path.exists(cpath):
        try:
            cc = json.load(open(cpath, encoding="utf-8"))
            if (cc.get("n") == len(b4) and cc.get("d0") == b4[0]["date"]
                    and cc.get("d1") == b4[-1]["date"] and cc.get("w") == window
                    and len(cc.get("pct", [])) == len(b4)):
                return cc["pct"]
        except Exception:
            pass
    pct = [None] * len(b4)
    for k in range(len(b4)):
        win = [r for r in ratio[max(0, k - window):k] if r is not None]
        if len(win) >= 100 and ratio[k] is not None:
            pct[k] = sum(1 for r in win if r < ratio[k]) / len(win)
    if cpath:
        try:
            json.dump({"n": len(b4), "d0": b4[0]["date"], "d1": b4[-1]["date"],
                       "w": window, "pct": pct}, open(cpath, "w", encoding="utf-8"))
        except Exception:
            pass
    return pct


def signals(b4, rg4, atr, vol_q=VOL_Q, coin=None, blood4=None, allow_short=True):
    """逐 4H bar 跑 L1+L2+L3，返回 trades 与逐 bar 信号方向。

    blood4=None 时按 BLOCK_INIT 自动推算血统；传空列表 [] 可显式关闭屏蔽。
    allow_short=False：不做空（纯现货模式）。此时 DOWN 段不产生任何 trade，
                       sig 恒 >= 0，持仓判定里也就不会冒出「空头持有中」。
                       默认 True 是为了不动历史实验脚本的数字 —— 20 多个
                       _exp_* / _verify_* 都按含做空的基线写过结论。
    """
    n = len(b4)
    sig = [0] * n            # 每根 bar 的目标信号方向（+1 多 / -1 空 / 0 无）
    trades = []
    pct = atr_percentile(b4, atr, coin=coin) if vol_q is not None else None
    if blood4 is None and BLOCK_INIT:
        blood4 = blood_of(b4, coin)
    i = max(LOOKBACK + 20, 30)
    while i < n - 1:
        rg = rg4[i]
        if rg not in ("UP", "DOWN") or atr[i] is None:
            i += 1
            continue
        if not allow_short and rg == "DOWN":
            i += 1
            continue
        # init 血统 = 状态机刚预热完的初始猜测，没有均线/背离纠错通道，
        # 拿到方向也靠不住 —— 不许据此开新仓（底仓仍按状态，见 pos = base + sig）。
        if blood4 and blood4[i] == "init_ma120":
            i += 1
            continue
        if b4[i]["date"] < START_NAV:
            i += 1
            continue
        if pct is not None and (pct[i] is None or pct[i] > vol_q):
            i += 1
            continue
        side = 1 if rg == "UP" else -1
        hi = max(x["high"] for x in b4[i - LOOKBACK:i])
        lo = min(x["low"] for x in b4[i - LOOKBACK:i])
        ok = (side == 1 and b4[i]["close"] > hi) or \
             (side == -1 and b4[i]["close"] < lo)
        if not ok:
            i += 1
            continue

        entry = b4[i]["close"]
        stop = entry - side * ATR_K * atr[i]
        risk = abs(entry - stop)
        if risk <= 0:
            i += 1
            continue

        exit_px = exit_j = reason = None
        cur_stop = stop
        best = entry
        mfe = 0.0
        for j in range(i + 1, min(i + MAX_BARS + 1, n)):
            # 先用「上一根结束时已确定的止损」判死，再在本根结束后才移动止损。
            # 顺序反了会偷到「同一根里既创新高、又能按新止损价平仓」的乐观偏差。
            if side == 1 and b4[j]["low"] <= cur_stop:
                exit_px, exit_j, reason = cur_stop, j, "stop"
                break
            if side == -1 and b4[j]["high"] >= cur_stop:
                exit_px, exit_j, reason = cur_stop, j, "stop"
                break
            if rg4[j] != rg:
                exit_px, exit_j, reason = b4[j]["close"], j, "flip"
                break
            if side == 1:
                mfe = max(mfe, (b4[j]["high"] - entry) / risk)
            else:
                mfe = max(mfe, (entry - b4[j]["low"]) / risk)
            if EARLY_N and j == i + EARLY_N and mfe < EARLY_THR:
                exit_px, exit_j, reason = b4[j]["close"], j, "early"
                break
            if TRAIL_K > 0:
                if side == 1:
                    best = max(best, b4[j]["high"])
                    if best - entry >= TRAIL_ARM * risk:
                        cur_stop = max(cur_stop, best - TRAIL_K * atr[i])
                else:
                    best = min(best, b4[j]["low"])
                    if entry - best >= TRAIL_ARM * risk:
                        cur_stop = min(cur_stop, best + TRAIL_K * atr[i])
        if exit_px is None:
            exit_j = min(i + MAX_BARS, n - 1)
            exit_px, reason = b4[exit_j]["close"], "timeout"

        for k in range(i + 1, exit_j + 1):
            sig[k] = side
        trades.append({
            "i": i, "j": exit_j, "side": side, "regime": rg,
            "entry": entry, "exit": exit_px, "risk": risk,
            "stop": stop, "stop_pct": risk / entry,
            "r": side * (exit_px - entry) / risk,
            "bars_held": exit_j - i, "reason": reason,
            "d": b4[i]["date"], "dx": b4[exit_j]["date"],
            "entry_atr": atr[i],
            # 突破幅度：收盘价越过前 LOOKBACK 根极值多少倍 ATR。
            # 这是「信号强度」而不是「标的属性」——ATR% 高只说明这币波动大（杠杆效应），
            # 突破幅度大才说明这一根真的冲出去了。入场那一刻完全可测。
            "brk": (entry - hi) / atr[i] if side == 1 else (lo - entry) / atr[i],
        })
        i = exit_j + 1
    return sig, trades


def build(coin):
    data = V2.load_all()
    b, rg, dr, drm = data[coin]
    b4 = agg4h(b)
    rg4 = [rg[x["src"]] for x in b4]
    atr = atr_wilder(b4, 14)
    sig, trades = signals(b4, rg4, atr, vol_q=VOL_Q, coin=coin)

    n = len(b4)
    # 底仓 = 状态基础值 × RANGE 系数（按来路查配置，见 range_ratio_series）。
    # 闸门已关闭，这里不叠加；若将来开启闸门，必须改用 base_series(b4, d1, rg4)
    # 以免两处口径分叉（2026-11-19 踩过这个坑）。
    rr = range_ratio_series(rg4)
    base = [BASE.get(rg4[k], 0.0) * rr[k] for k in range(n)]
    pos = [base[k] + ADD * sig[k] for k in range(n)]

    nav = nav_base = nav_bh = 1.0
    curve = []
    f4 = FUND_DAILY / 6.0          # 每 4H 根的资金费（8h 结算 / 6 根）
    for k in range(1, n):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        # 资金费只作用于永续那一层：底仓现货 + 加仓永续
        sleeve = (pos[k] - base[k]) if SPOT_BASE else pos[k]
        if f4 and sleeve:
            nav *= (1 - f4 * sleeve)
        xb = 1 + base[k] * r
        nav_base *= xb if xb > 0 else 0.0
        nav_bh *= (1 + r)
        if pos[k] != pos[k - 1]:
            nav *= (1 - COST * abs(pos[k] - pos[k - 1]))
        if base[k] != base[k - 1]:
            nav_base *= (1 - COST * abs(base[k] - base[k - 1]))
        if b4[k]["date"] >= START_NAV:
            curve.append((b4[k]["date"], nav, nav_base, nav_bh, pos[k]))

    return b4, rg4, trades, curve, pos


def nav_from(b4, pos, base, fund_daily=FUND_DAILY, spot_base=SPOT_BASE):
    """由已算好的仓位序列重建净值。只用于执行层对照（spot_base=False 即全永续）。"""
    f4 = fund_daily / 6.0
    nav, out = 1.0, []
    for k in range(1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sleeve = (pos[k] - base[k]) if spot_base else pos[k]
        if f4 and sleeve:
            nav *= (1 - f4 * sleeve)
        if pos[k] != pos[k - 1]:
            nav *= (1 - COST * abs(pos[k] - pos[k - 1]))
        if b4[k]["date"] >= START_NAV:
            out.append((b4[k]["date"], nav))
    return out


def compose_any(by_coin):
    """等权日频合成（曲线数量少时用的通用版）。"""
    import statistics as st
    days = sorted({d for s in by_coin.values() for d, _ in s})
    M = {c: {d: v for d, v in s} for c, s in by_coin.items()}
    prev, nav, out = {}, 1.0, []
    for d in days:
        rs = []
        for c, m in M.items():
            v = m.get(d)
            if v is None:
                continue
            p0 = prev.get(c)
            if p0:
                rs.append(v / p0)
            prev[c] = v
        if rs:
            nav *= st.mean(rs)
        out.append((d, nav))
    return out


def summ(cur):
    import datetime as dt
    v0 = cur[0][1]
    yrs = (dt.date.fromisoformat(cur[-1][0]) - dt.date.fromisoformat(cur[0][0])).days / 365.25
    cagr = (cur[-1][1] / v0) ** (1 / yrs) - 1
    peak = mdd = 0.0
    for _d, v in cur:
        q = v / v0
        peak = max(peak, q)
        mdd = min(mdd, q / peak - 1)
    return {"cagr": cagr, "mdd": mdd, "calmar": cagr / abs(mdd) if mdd else 0}


def daily_ohlc(b4, rg4):
    out, cur = [], None
    for k, x in enumerate(b4):
        if cur is None or cur["d"] != x["date"]:
            if cur:
                out.append(cur)
            cur = {"d": x["date"], "o": x["open"], "h": x["high"],
                   "l": x["low"], "c": x["close"], "s": rg4[k]}
        else:
            cur["h"] = max(cur["h"], x["high"])
            cur["l"] = min(cur["l"], x["low"])
            cur["c"] = x["close"]
            cur["s"] = rg4[k]
    if cur:
        out.append(cur)
    return out


def tstats(trades):
    if not trades:
        return None
    rs = [t["r"] for t in trades]
    costs = [COST * 2 / t["stop_pct"] for t in trades]
    nets = [r - c for r, c in zip(rs, costs)]
    w = [x for x in nets if x > 0]
    l = [x for x in nets if x <= 0]
    srt = sorted(nets, reverse=True)
    return {
        "n": len(nets), "win": len(w) / len(nets),
        "sumR": sum(rs), "netR": sum(nets) / len(nets),
        "pf": (sum(w) / abs(sum(l))) if l and sum(l) else None,
        "bars": sum(t["bars_held"] for t in trades) / len(trades),
        "ex3": sum(srt[3:]) / len(srt[3:]) if len(srt) > 3 else 0,
        "top1": srt[0],
        "top1_share": (srt[0] / sum(nets)) if sum(nets) > 0 else None,
        "reasons": dict(Counter(t["reason"] for t in trades)),
        "long": len([t for t in trades if t["side"] == 1]),
        "short": len([t for t in trades if t["side"] == -1]),
    }


def _daily_curve(curve):
    """4H 曲线压成日频（每日最后一根），归一化到 1，缩小体积。"""
    last = {}
    for p in curve:
        last[p[0]] = p
    days = sorted(last)
    if not days:
        return []
    n0, b0, h0 = last[days[0]][1], last[days[0]][2], last[days[0]][3]
    out = []
    for d in days:
        p = last[d]
        out.append([d, round(p[1] / n0, 4), round(p[2] / b0, 4),
                    round(p[3] / h0, 4), round(p[4], 3)])
    return out


def met(curve, idx=1):
    """curve 元素 = (date, nav, nav_base, nav_bh, pos)。"""
    import datetime as dt
    v0, v1 = curve[0][idx], curve[-1][idx]
    days = (dt.date.fromisoformat(curve[-1][0]) - dt.date.fromisoformat(curve[0][0])).days
    yrs = days / 365.25
    cagr = (v1 / v0) ** (1 / yrs) - 1
    peak = mdd = 0.0
    for p in curve:
        v = p[idx] / v0
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"cagr": cagr, "mdd": mdd, "calmar": cagr / abs(mdd) if mdd else 0, "yrs": yrs}


def _yearly(curve, idxs):
    """逐年 CAGR/MDD/Calmar。curve 元素首列是日期，idxs 指定要算哪几列。"""
    import datetime as dt
    out = []
    for y in range(2018, 2027):
        a, b = f"{y}-01-01", f"{y + 1}-01-01"
        v = [p for p in curve if a <= p[0] < b]
        if len(v) < 2:
            continue
        yrs = (dt.date.fromisoformat(v[-1][0]) - dt.date.fromisoformat(v[0][0])).days / 365.25
        if yrs < 0.2:
            continue
        row = {"y": y}
        for name, ix in idxs.items():
            v0 = v[0][ix]
            if v0 <= 0:
                continue
            cagr = (v[-1][ix] / v0) ** (1 / yrs) - 1
            peak = mdd = 0.0
            for p in v:
                q = p[ix] / v0
                peak = max(peak, q)
                mdd = min(mdd, q / peak - 1)
            row[name] = {"cagr": cagr, "mdd": mdd,
                         "calmar": cagr / abs(mdd) if mdd else 0}
        out.append(row)
    return out


def main():
    rep = {}
    curves = {}
    ALT = {}
    print("=" * 108)
    print(f"三层骨架 · {len(COINS)} 品种 · L1 日线方向 + L2 4H突破加仓 + L3 状态翻转/止损/时限")
    print("=" * 108)
    print(f"  L2 幅度 +{ADD}×　止损 {ATR_K}×ATR(14)　时限 {MAX_BARS} 根 4H　不设盈利目标")
    print(f"  执行层：底仓 1.0 走现货（不付资金费）｜L2 加仓 {ADD}× 走永续，"
          f"资金费 {FUND_DAILY*1e4:.0f}bp/日（≈{FUND_DAILY*365*100:.0f}%/年）\n")
    print(f"  {'品种':<6} {'笔数':>5} {'多/空':>9} {'胜率':>6} {'净/笔':>7} {'PF':>5} "
          f"{'持仓':>5} {'剔3笔':>8} {'暴露':>6} {'CAGR':>8} {'MDD':>8} {'Calmar':>7} {'B&H':>6}")
    print("  " + "-" * 106)

    for c in COINS:
        b4, rg4, trades, curve, pos = build(c)
        st = tstats(trades)
        curves[c] = curve
        # ALT 用于「底仓也挂永续」的执行层对照，需要与 build() 完全同口径的 base
        rr4 = range_ratio_series(rg4)
        ALT[c] = (b4, pos,
                  [BASE.get(rg4[k], 0.0) * rr4[k] for k in range(len(rg4))])
        m = met(curve, 1)
        mb = met(curve, 2)
        mbh = met(curve, 3)
        expo = statistics.mean(pos) if pos else 0
        pf = f"{st['pf']:.2f}" if st["pf"] else "  — "
        print(f"  {c:<6} {st['n']:>5} {st['long']:>3}/{st['short']:<5} "
              f"{st['win']*100:>5.1f}% {st['netR']:>+7.3f} {pf:>5} "
              f"{st['bars']:>5.0f} {st['ex3']:>+8.3f} {expo*100:>5.1f}% "
              f"{m['cagr']*100:>+7.1f}% {m['mdd']*100:>7.1f}% {m['calmar']:>7.2f} "
              f"{mbh['calmar']:>6.2f}")

        rep[c] = {
            "n": st["n"], "long": st["long"], "short": st["short"],
            "win": st["win"], "netR": st["netR"], "pf": st["pf"],
            "bars": st["bars"], "ex3": st["ex3"], "expo": expo,
            "stats": st,
            "m": m, "m_base": mb, "m_bh": mbh,
            "sector": SECTOR.get(c, "—"),
            "grp": "原始" if COINS.index(c) < 10 else "新增",
            "first": b4[0]["date"], "last": b4[-1]["date"],
            "trades": [{k: t[k] for k in ("d", "dx", "side", "entry", "exit",
                                         "r", "bars_held", "reason", "regime",
                                         "stop_pct", "stop", "entry_atr", "brk")}
                       for t in trades],
            "yearly": _yearly(_daily_curve(curve),
                              {"full": 1, "base": 2, "bh": 3}),
        }
        if c in DETAIL:
            rep[c]["daily"] = daily_ohlc(b4, rg4)
            rep[c]["curve"] = _daily_curve(curve)
        else:
            # 其余品种只导出周频曲线（体积小 7 倍），仍可点开看净值与仓位
            rep[c]["curve"] = _daily_curve(curve)[::7]

    # 等权组合（用内存里的曲线，不依赖是否序列化）
    days = sorted({p[0] for c in COINS for p in curves[c]})
    MAPS = {}   # 预先建好 {idx: {coin: {date: value}}}，否则循环里重复建字典会极慢
    for idx in (1, 2, 3):
        MAPS[idx] = {c: {p[0]: p[idx] for p in curves[c]} for c in COINS}

    def compose(idx):
        prev, nav, out = {}, 1.0, []
        M = MAPS[idx]
        for d in days:
            rs = []
            for c in COINS:
                v = M[c].get(d)
                if v is None:
                    continue
                p0 = prev.get(c)
                if p0:
                    rs.append(v / p0)
                prev[c] = v
            if rs:
                nav *= statistics.mean(rs)
            out.append((d, nav))
        return out

    for nm, idx in (("full", 1), ("base", 2), ("bh", 3)):
        cur = compose(idx)
        import datetime as dt
        v0, v1 = cur[0][1], cur[-1][1]
        yrs = (dt.date.fromisoformat(cur[-1][0]) - dt.date.fromisoformat(cur[0][0])).days / 365.25
        cagr = (v1 / v0) ** (1 / yrs) - 1
        peak = mdd = 0.0
        for _d, v in cur:
            vv = v / v0
            peak = max(peak, vv)
            mdd = min(mdd, vv / peak - 1)
        print(f"\n  等权组合 {nm:<5} CAGR {cagr*100:+7.1f}%  MDD {mdd*100:7.1f}%  "
              f"Calmar {cagr/abs(mdd):.2f}")
        rep.setdefault("_portfolio", {})[nm] = {
            "cagr": cagr, "mdd": mdd, "calmar": cagr / abs(mdd),
            "curve": [[d, round(v, 4)] for d, v in cur],
        }
        if nm == "full":
            rep["_portfolio"]["_full_curve"] = cur

    PF = rep["_portfolio"]
    PF["yearly"] = []
    for row in _yearly(PF["_full_curve"], {"full": 1}):
        y0, y1 = f"{row['y']}-01-01", f"{row['y']+1}-01-01"
        for nm2 in ("base", "bh"):
            seg = [(d, v) for d, v in PF[nm2]["curve"] if y0 <= d < y1]
            if len(seg) > 1:
                st = _yearly(seg + [("9999-12-31", seg[-1][1])], {nm2: 1})
                if st and nm2 in st[0]:
                    row[nm2] = st[0][nm2]
        PF["yearly"].append(row)

    # 全部品种同时存在的子区间——唯一的严格同口径对照
    # 起点不写死：取「最晚那个品种的上市日（或回测起点）＋120 天状态机预热」，再向上取整到月初
    import datetime as dt
    starts = {c: max(rep[c]["first"], START_NAV) for c in COINS}
    late_c = max(starts, key=lambda c: starts[c])
    sub_d = dt.date.fromisoformat(starts[late_c]) + dt.timedelta(days=120)
    SUB = sub_d.replace(day=1)
    if sub_d.day > 1:
        SUB = (SUB + dt.timedelta(days=32)).replace(day=1)
    SUB = SUB.isoformat()
    print("\n" + "=" * 108)
    print(f"全 {len(COINS)} 品种齐全期（{SUB} 起；最晚上线的是 {late_c} "
          f"{starts[late_c]}，再加 120 天状态机预热）")
    print("=" * 108)
    def sub(cur):
        v = [p for p in cur if p[0] >= SUB]
        if len(v) < 30:
            return None
        v0 = v[0][1]
        yrs = (dt.date.fromisoformat(v[-1][0]) - dt.date.fromisoformat(v[0][0])).days / 365.25
        cagr = (v[-1][1] / v0) ** (1 / yrs) - 1
        peak = mdd = 0.0
        for _d, x in v:
            q = x / v0
            peak = max(peak, q)
            mdd = min(mdd, q / peak - 1)
        return {"cagr": cagr, "mdd": mdd, "calmar": cagr / abs(mdd) if mdd else 0}
    PF["sub"] = {}
    for nm in ("full", "base", "bh"):
        s = sub([(d, v) for d, v in PF[nm]["curve"]])
        if s:
            PF["sub"][nm] = s
            print(f"  {nm:<5} CAGR {s['cagr']*100:+7.1f}%  MDD {s['mdd']*100:7.1f}%  "
                  f"Calmar {s['calmar']:>6.2f}")
    if "full" in PF["sub"] and "base" in PF["sub"]:
        imp = (PF["sub"]["full"]["calmar"] / PF["sub"]["base"]["calmar"] - 1) * 100
        print(f"  → L2 加仓层相对纯底仓改善 {imp:+.0f}%")

    # ---------- 分组：原始 10 / 新增 10 / 全部（同窗口、同规则）
    # 新增的那批**从未参与过任何参数选择**，是本轮唯一干净的样本外证据
    def compose_keys(keys, idx=1):
        M = MAPS[idx]
        prev, nav, out = {}, 1.0, []
        for d in days:
            rs = []
            for c in keys:
                v = M[c].get(d)
                if v is None:
                    continue
                p0 = prev.get(c)
                if p0:
                    rs.append(v / p0)
                prev[c] = v
            if rs:
                nav *= statistics.mean(rs)
            out.append((d, nav))
        return out

    GRP = [("原始 10 品种", COINS[:10]), ("新增 10 品种（样本外）", COINS[10:]),
           (f"全部 {len(COINS)} 品种", COINS)]
    print("\n" + "=" * 108)
    print(f"分权重组（同窗口 {SUB} 起，同规则；新增那批从未参与参数选择）")
    print("=" * 108)
    print(f"  {'组':<22}{'CAGR':>9}{'MDD':>9}{'Calmar':>8}{'纯底仓':>8}{'B&H':>7}"
          f"{'净R>0':>7}{'跑赢B&H':>9}")
    print("  " + "-" * 78)
    PF["groups"] = []
    for nm, keys in GRP:
        if not keys:      # SKEL_N=10 时「新增 10」是空组，median([]) 会直接抛异常
            continue
        row = {"name": nm, "n": len(keys), "keys": list(keys)}
        for tag, idx in (("full", 1), ("base", 2), ("bh", 3)):
            s = sub([(d, v) for d, v in compose_keys(keys, idx)])
            if s:
                row[tag] = s
        pos = len([c for c in keys if rep[c]["netR"] > 0])
        win = len([c for c in keys if rep[c]["m"]["calmar"] > rep[c]["m_bh"]["calmar"]])
        row["posR"] = pos
        row["winBH"] = win
        row["medNetR"] = statistics.median([rep[c]["netR"] for c in keys])
        row["medCal"] = statistics.median([rep[c]["m"]["calmar"] for c in keys])
        PF["groups"].append(row)
        print(f"  {nm:<22}{row['full']['cagr']*100:>+8.1f}%{row['full']['mdd']*100:>8.1f}%"
              f"{row['full']['calmar']:>8.2f}{row['base']['calmar']:>8.2f}"
              f"{row['bh']['calmar']:>7.2f}{pos:>5}/{len(keys)}"
              f"{win:>6}/{len(keys)}")

    # ---------- 日收益相关性（共同窗口，用 B&H 序列当资产收益代理）
    import math
    rets = {}
    for c in COINS:
        ser = {p[0]: p[3] for p in curves[c] if p[0] >= SUB}
        ds = sorted(ser)
        rets[c] = {ds[k]: ser[ds[k]] / ser[ds[k - 1]] - 1 for k in range(1, len(ds))}
    common = sorted(set.intersection(*[set(v) for v in rets.values()])) if len(rets) > 1 else []

    def corr(a, b):
        xa = [rets[a][d] for d in common]
        xb = [rets[b][d] for d in common]
        ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
        cov = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
        va = sum((x - ma) ** 2 for x in xa)
        vb = sum((y - mb) ** 2 for y in xb)
        return cov / math.sqrt(va * vb) if va and vb else 0.0

    MU = {c: {} for c in COINS}
    pairs = []
    for i, a in enumerate(COINS):
        for b in COINS[i + 1:]:
            rho = corr(a, b)
            MU[a][b] = rho
            pairs.append(rho)
    rho_avg = statistics.mean(pairs) if pairs else 0.0
    n_eff = 1 + (len(COINS) - 1) * (1 - rho_avg)
    print(f"\n  日收益两两平均相关 {rho_avg:.2f}（{len(pairs)} 对，共同交易日 {len(common)}）"
          f" → 有效独立押注 ≈ {n_eff:.1f} / {len(COINS)}")
    PF["corr"] = MU
    PF["corr_avg"] = rho_avg
    PF["corr_days"] = len(common)
    PF["n_eff"] = n_eff

    # ---------- 显著性：跑赢 B&H 的个数 vs 抛硬币
    def binom_p(k, n, p=0.5):
        """双侧二项检验。"""
        def c(nn, kk):
            return math.comb(nn, kk)
        tail = sum(c(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k, n + 1))
        return min(1.0, 2 * tail)
    winN = len([c for c in COINS if rep[c]["m"]["calmar"] > rep[c]["m_bh"]["calmar"]])
    p_nom = binom_p(winN, len(COINS))
    print(f"  跑赢 B&H 的品种 {winN}/{len(COINS)}，名义二项 p = {p_nom:.3f}")
    print(f"  但 {len(COINS)} 个币同涨同跌，按有效独立押注 {n_eff:.1f} 折算，"
          f"显著性会大幅下降——不要按名义 p 值下结论")
    PF["signif"] = {"win": winN, "n": len(COINS), "p_nominal": p_nom,
                    "n_eff": n_eff}

    # 执行层对照：若底仓也挂永续（旧的隐含假设），资金费会吃掉多少
    print("\n" + "=" * 108)
    print("执行层对照：资金费到底吃多少")
    print("=" * 108)
    alt = compose_any({c: nav_from(ALT[c][0], ALT[c][1], ALT[c][2], spot_base=False)
                       for c in ALT})
    s_alt = summ(alt)
    s_now = summ([(d, v) for d, v in PF["_full_curve"]])
    print(f"  全永续（底仓也付资金费，f={FUND_DAILY*1e4:.0f}bp/日）  CAGR {s_alt['cagr']*100:+7.1f}%  "
          f"MDD {s_alt['mdd']*100:7.1f}%  Calmar {s_alt['calmar']:>6.2f}")
    print(f"  底仓现货 + 加仓永续（现行）              CAGR {s_now['cagr']*100:+7.1f}%  "
          f"MDD {s_now['mdd']*100:7.1f}%  Calmar {s_now['calmar']:>6.2f}")
    print(f"  → 改现货带来 {(s_now['calmar']/s_alt['calmar']-1)*100:+.0f}% 的 Calmar 改善，"
          f"零规则改动")
    PF["exec"] = {"perp_all": s_alt, "spot_base": s_now,
                  "fund_daily": FUND_DAILY}

    PF.pop("_full_curve", None)
    PF["sub_start"] = SUB

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, separators=(",", ":"))
    print(f"\n已写入 {OUT_JSON}（{os.path.getsize(OUT_JSON)/1024:.0f} KB）")


if __name__ == "__main__":
    main()
