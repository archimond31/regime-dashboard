#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘信号生成（只出信号，绝不下单）。

品种：组合 11 币 BTC ETH SOL BNB XRP HYPE LINK UNI AAVE ZEC NEAR
      2026-11-18 去 CRV 加 ZEC/DOGE/NEAR；2026-11-27 去 DOGE（meme 币）。
      观察名单 4 币 SNX PENDLE RENDER ONDO（2026-11-27 加）：只算状态、只发信号，
      **不持仓、不出工单**（`portfolio_config.WATCH_ONLY`，权重为 0 且不在权重表里）。
      **PORTFOLIO 与 WEIGHTS_SMALL 必须同集**：多一个 → 该币算出 target=0 却被
      打印成「空仓」；少一个 → 权重表里有钱没处放（静默变现金）。
      **WATCH 反过来必须与 PORTFOLIO 不相交**：同时出现就是既想要仓位又不想执行。

复刻回测的三层骨架：
  L1  日线 G 状态机定方向与底仓（UP/RANGE → 1.0，DOWN → 0）
  L2  4H 收盘突破前 20 根极值 → +0.5×，且 ATR 滚动分位 >85% 时拦截
  L3  出场：状态翻转 / 1.5×ATR(14) 硬止损 / 满 120 根 4H 兜底

与回测的两处必要差异（不改就是未来函数）：
  ① 剔除**未收盘**的 4H 与日线。回测里的 bar 天然都是收盘的，实盘不是。
  ② 日线状态只用「严格早于当日」的那根，和 align_regime_safe 的口径保持一致。

用法：
  python live_signals.py            # 只生成 _signal.json
  python gen_signal_html.py         # 渲染 signal_now.html
  python notify_mail.py             # 发邮件（有动作才发）
  python run_signal.py              # 上面三步一次跑完，定时任务用这个
"""
import json
import os
import sys
import time
import datetime as dt
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_binance_vision import load_bars, to_daily
from fetch_hyperliquid import load_hype
from test_regime_fit import agg4h
from _proto_4h import atr_wilder
from strategy_1h_rsi import regime_series, regime_and_blood, align_regime_safe
import bt_skeleton as BT
import base_gate as BG
import portfolio_config as PC
import vol_target as VT

# 状态血统的中文标签。血统决定这段状态能不能被纠错：
#   init_ma120 —— 状态机刚预热完的初始猜测，既不受 MA60 纠错、也不受背离失败约束，
#                 出口只剩「RSI 破 os 再升破」，上涨行情中永不触发（HYPE 2025-04 锁死 48 天）。
#   其余 —— 都有对应的退出通道。
BLOOD_LABEL = {
    "init_ma120": "初始化·不可纠错",
    "divergence_bottom": "底背离",
    "divergence_top": "顶背离",
    "ma60_cross_up": "MA60上穿",
    "ma60_cross_down": "MA60下穿",
    "rsi_retrigger_up": "RSI再触发↑",
    "rsi_retrigger_down": "RSI再触发↓",
    "rsi_overbought_exit": "超买退出",
    "rsi_oversold_exit": "超卖退出",
    "cross_fix_up": "MA60纠错↑",
    "cross_fix_down": "MA60纠错↓",
    "div_fail_top": "顶背离失败",
    "div_fail_bottom": "底背离失败",
    "trend_flat_up": "盘整识别↑",
    "trend_flat_down": "盘整识别↓",
}

PORTFOLIO = ["BTC", "ETH", "SOL", "BNB", "XRP", "HYPE", "LINK", "UNI", "AAVE",
             "ZEC", "NEAR"]
# 观察名单：状态机照跑、信号照发，但没有仓位、不出工单。
# **COINS 仍然包含它们** —— 分析、页面、邮件都要看到；区别只体现在
# action（固定「观察」）与 exec（固定 False）上，而不是「算不算」。
WATCH = [c for c in PC.WATCH_ONLY]
COINS = PORTFOLIO + WATCH
SECTOR = {"BTC": "储值", "ETH": "L1", "SOL": "L1", "BNB": "交易所", "XRP": "支付",
          "HYPE": "DEX/永续", "LINK": "预言机", "UNI": "DeFi", "AAVE": "DeFi",
          "ZEC": "隐私币", "NEAR": "L1",
          # ---- 观察名单（2026-11-27）
          "SNX": "衍生品", "PENDLE": "利率", "RENDER": "算力", "ONDO": "RWA"}
LOOKBACK, ATR_K, MAX_BARS, VOL_Q = BT.LOOKBACK, BT.ATR_K, BT.MAX_BARS, BT.VOL_Q


def _now_utc():
    return dt.datetime.now(timezone.utc)


def _fmt_ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_bj(ms):
    """毫秒时间戳 → 北京时间「月-日 时:分」。

    表里统一用北京时间：用户在国内看盘，看到 11-11 08:00 能直接对上自己那根 4H，
    比让他自己 +8 换算靠谱。
    """
    t = dt.datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + dt.timedelta(hours=8)
    return t.strftime("%m-%d %H:%M")


def load_coin(c, now):
    """返回已收盘的 (b4, daily)，且两者都带统一的 ts（收盘毫秒时间戳）。

    口径坑：agg4h 的 date 取的是「该 4H 段最后一根 1H 的 date」，而 1H 的 date
    只有日期没有时分 —— 于是 4H 的「几点收盘」信息丢了。Hyperliquid 那边又带着
    "00:00" 后缀，两边字符串一比就错位（"2026-11-10 00:00" > "2026-11-10"），
    会把还没收盘的日线当成已收盘。这里统一补 ts、日线统一截成 YYYY-MM-DD。
    """
    now_ms = now.timestamp() * 1000
    if c == "HYPE":
        b4 = load_hype("4h")
        d1 = load_hype("1d")
        for x in b4:
            x["ts"] = x["t"] + 4 * 3600 * 1000      # 开盘时刻 + 4h = 收盘
        for x in d1:
            x["date"] = x["date"][:10]
            x["ts"] = x["t"] + 86400 * 1000
    else:
        b = load_bars(c, "1h")
        b4 = agg4h(b)          # agg4h 自带 ts（桶起点 +4h，严格落在 00/04/08/…/20）
        d1 = to_daily(b)
        for x in d1:
            x["date"] = x["date"][:10]
    b4 = [x for x in b4 if x["ts"] <= now_ms]
    # 日线：今天那根要到次日 00:00 UTC 才收盘，必须先剔掉
    today = now.strftime("%Y-%m-%d")
    d1 = [x for x in d1 if x["date"] < today]
    return b4, d1


def analyze(c, now, prev_gate=None):
    b4, d1 = load_coin(c, now)
    n = len(b4)
    reg_map, blood_map = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg_map)
    blood4 = align_regime_safe(b4, blood_map)
    atr = atr_wilder(b4, 14)
    pct = BT.atr_percentile(b4, atr, coin=c)
    # 显式传 blood4：生产走 d1 → 血统的精确路径，与 rg4 完全同源。
    # BT.BLOCK_INIT 打开时，init 血统（状态机刚预热完的初始猜测）不开新仓。
    # allow_short 必须显式传：BT 里默认 True（保历史实验口径），
    # 生产不传就会继续在 DOWN 段生成「空头持有中」，与纯现货账户对不上。
    sig, trades = BT.signals(b4, rg4, atr, vol_q=VOL_Q, coin=c, blood4=blood4,
                             allow_short=PC.ALLOW_SHORT)

    last = b4[-1]
    px = last["close"]
    state = rg4[-1]
    pctv = pct[-1]
    atrp = (atr[-1] / px) if atr[-1] else None

    hi = max(x["high"] for x in b4[n - 1 - LOOKBACK:n - 1])
    lo = min(x["low"] for x in b4[n - 1 - LOOKBACK:n - 1])

    o = {"c": c, "sector": SECTOR.get(c, "—"), "px": px,
         "bar": _fmt_ts(last["ts"]), "bar_ts": last["ts"],
         "state": state, "n_bars": n, "atr": atr[-1], "atrp": atrp,
         "gate": pctv, "gate_blocked": bool(pctv is not None and pctv > VOL_Q),
         "hi20": hi, "lo20": lo,
         "dist_up": (hi / px - 1) * 100, "dist_dn": (lo / px - 1) * 100}

    # 当前这段状态是从哪天开始的（往回找连续同状态的第一根）。
    # 用途：底仓清仓提示去重 —— 同一轮 DOWN 只提示一次，翻转后再翻空才重新提示。
    k_seg = len(rg4) - 1
    while k_seg > 0 and rg4[k_seg - 1] == state:
        k_seg -= 1
    o["regime_start"] = b4[min(k_seg, len(b4) - 1)]["date"]

    # RANGE 段的「来路」：进入 RANGE 之前那段是什么。
    # 2026-11-20 盘后起 UP→1.0 / DOWN→0.5，所以 range_ratio 可能不是 1.0，
    # 面板与台账都要显示实际比例（否则会和策略自相矛盾）。
    # 与回测 bt_skeleton.range_ratio_series() 完全同源（都取 rg4[k_seg-1]），
    # 且只依赖历史 bar，无前视。
    prev_st = rg4[k_seg - 1] if k_seg > 0 else None
    o["range_src"] = (prev_st if state == "RANGE" and prev_st in ("UP", "DOWN")
                      else None)
    o["range_ratio"] = (PC.RANGE_RATIO_BY_SRC.get(o["range_src"],
                                                  PC.RANGE_RATIO_DEFAULT)
                        if state == "RANGE" else 1.0)

    # 血统：这段状态是怎么来的，以及能不能被纠错。
    # init 血统 = 不可纠错，实盘在 BT.BLOCK_INIT 打开时不据此开新仓。
    o["blood"] = blood4[-1] if blood4 else None
    o["blood_label"] = BLOOD_LABEL.get(o["blood"], o["blood"] or "—")
    o["blood_locked"] = o["blood"] == "init_ma120"

    # ---- 当前是否持仓：最后一笔的出场点是否就是序列末尾（且没到 120 根）----
    holding = None
    if trades:
        t = trades[-1]
        if t["j"] == n - 1 and t["reason"] == "timeout" and (t["j"] - t["i"]) < MAX_BARS:
            held = t["j"] - t["i"]
            upnl_r = t["side"] * (px - t["entry"]) / t["risk"]
            # 入场时刻必须从 4H 的收盘时间戳算，不能直接用 t["d"]：
            # t["d"] 是「该 4H 段里最后一根 1H 的日期」，而 00:00 UTC 收盘的那根 4H
            # 装的是前一天 20:00~23:00 —— 于是 11-11 00:00 入场的仓会被记成 11-10，差一天。
            ets = b4[t["i"]]["ts"]
            o["pos"] = dict(
                side=t["side"], entry=t["entry"], stop=t["stop"],
                stop_pct=t["stop_pct"] * 100, regime=t["regime"],
                entry_ts=ets,
                entry_bj=_fmt_bj(ets),          # 11-11 08:00（北京时间，便于直接对照）
                entry_utc=_fmt_ts(ets)[5:],     # 11-11 00:00（4H 收盘时刻，UTC）
                entry_date=_fmt_ts(ets)[:10],   # 修正后的日期（原为 t["d"]，会差一天）
                held=held, left=MAX_BARS - held, upnl_r=upnl_r,
                upnl_px=(px / t["entry"] - 1) * 100 * (1 if t["side"] > 0 else -1),
                dist_stop=abs(px - t["stop"]) / px * 100,
                flipped=(state != t["regime"]))
            holding = o["pos"]
    if holding is None:
        o["pos"] = None

    # ---- 若空仓，当前是否满足入场 ----
    # 空仓时「当前是否满足入场」：
    #   纯现货模式下 DOWN 段的 want 是 0（不是 -1）—— 显示成「需跌 X% 才触发」
    #   等于在提示一个不会执行的做空动作，那正是这次改动要消灭的东西。
    if holding is None:
        want = 1 if state == "UP" else (
            -1 if (state == "DOWN" and PC.ALLOW_SHORT) else 0)
        trig = False
        if want == 1:
            trig = px > hi
        elif want == -1:
            trig = px < lo
        o["want"] = want
        o["triggered"] = bool(trig and not o["gate_blocked"])
        # 距触发还差多少（正数=还要涨/跌多少才触发）
        if want == 1:
            o["need"] = (hi / px - 1) * 100
        elif want == -1:
            o["need"] = (px / lo - 1) * 100
        else:
            o["need"] = None

    # ---- 结论一句话 ----
    if holding:
        if holding["flipped"]:
            o["action"] = "平仓"
            o["note"] = f"日线已翻转为 {state}，与入场时 {holding['regime']} 相反，按规则应平仓"
        elif holding["dist_stop"] < 2:
            o["action"] = "止损临近"
            o["note"] = f"距止损 {holding['dist_stop']:.2f}%"
        elif holding["left"] <= 6:
            o["action"] = "即将到期"
            o["note"] = f"兜底只剩 {holding['left']} 根 4H（{holding['left']*4} 小时）"
        else:
            o["action"] = "持有"
            o["note"] = f"浮盈 {holding['upnl_r']:+.2f}R，兜底剩 {holding['left']} 根"
    else:
        if o["state"] == "RANGE":
            o["action"] = "空仓"
            o["note"] = "日线 RANGE，只能做多但需突破；当前无方向"
        elif o["state"] == "DOWN" and not PC.ALLOW_SHORT:
            # 纯现货：DOWN 段唯一该做的动作是「清掉已有底仓」，没有底仓就什么都不做。
            # 不写成「等跌 X% 触发」—— 那是做空语言，账户里没有对应仓位。
            o["action"] = "空仓"
            o["note"] = "日线 DOWN｜现货模式不做空，无 4H 仓位可动"
        elif o["gate_blocked"]:
            o["action"] = "闸门拦截"
            o["note"] = f"波动分位 {pctv*100:.0f}% > {VOL_Q*100:.0f}%，不开新仓"
        elif o["triggered"]:
            o["action"] = "开仓"
            o["note"] = ("4H 收盘突破前 20 根高点" if o["want"] > 0 else "4H 收盘跌破前 20 根低点")
        else:
            o["action"] = "空仓"
            o["note"] = (f"需{'涨' if o['want']>0 else '跌'} {o['need']:.2f}% 才触发"
                         if o["need"] is not None else "无方向")

    # ---- 按 portfolio_config 折算建议持仓金额（负数=空头）----
    # 持仓中一律按满峰值算（已经在场，没有「只建一半」的问题）；
    # 空仓时才区分「只底仓」与「已触发上满峰值」。
    # 日线已翻转的仓不反手，先归零 —— 反手要等下一根 4H 的突破确认。
    at_peak = bool(holding) or bool(o.get("triggered"))
    closing = bool(holding and holding.get("flipped"))

    # 波动目标化：σ 用**已收盘**的 4H 序列算（load_coin 已剔除未收盘 bar），
    # 系数每 7 天由日历桶刷新一次，无状态、可复现。
    # scale 是最终乘数（系数 × κ），作用在金额上，正负号不变。
    cf, sg = VT.coef_now(b4)
    scale = cf * VT.KAPPA if VT.ENABLED else 1.0
    o["vt"] = dict(on=VT.ENABLED, lb=VT.LB, sigma=sg, coef=cf,
                   kappa=VT.KAPPA, scale=scale)

    # ---- 底仓闸门：当前 4H 收盘 vs 前一日日线 MA50 ----
    # 只作用于底仓，不影响加仓层（加仓层由 4H 突破触发）。
    # DOWN 时重置滞回状态：底仓本来就是 0，闸门值没有意义；
    # 不重置会把上一轮的残留带进下一段 UP。
    gma = BG.ma_lookup(d1)(last["date"])
    if state == "DOWN":
        gate_val = 1.0
    else:
        gate_val = BG.next_gate(px, gma, prev_gate)
    o["base_gate"] = gate_val
    # 落盘的滞回状态：DOWN 时写 None（= 重置），与 base_gate.series() 的口径一致。
    # 若写 1.0，下一段 UP/RANGE 的第一根就会走「原先是全额」分支、要求跌破 MA×0.99 才砍，
    # 而回测（series 里 cur=None）是「跌破 MA×1.01 就砍」—— 两者会在均线附近分歧。
    o["gate_state"] = None if state == "DOWN" else gate_val
    o["gate_ma"] = gma
    o["gate_note"] = BG.describe(gate_val, state)

    # 底仓的两个金额口径，给工单（ticket.py）用：
    #   base_full = 不打折的底仓额度（含 VT scale，不含闸门）
    #   base_held = 按闸门打折后**应该在场**的底仓金额
    # 为什么要 base_held：状态翻 DOWN 时 target 被强制归零，拿不到「要平多少」；
    # 而如果闸门把底仓砍到 25%，手上只有 25%，按全额去清就是 4 倍超卖。
    # DOWN 时闸门值强制 1.0（无意义），真实在仓比例只能取上一轮的 prev_gate。
    # 局限：只在「翻 DOWN 的第一根」准 —— 之后 prev_gate 已被写成 None，回落到全额。
    # 但清底仓提示与工单都只在第一根生成（按 regime_start 去重），所以不影响指令。
    _bw = PC.CAPITAL * PC.WEIGHTS.get(c, 0.0) * PC.BASE_RATIO * scale
    o["base_full"] = round(_bw)
    o["base_held"] = round(_bw * (gate_val if state != "DOWN"
                                  else (prev_gate if prev_gate is not None else 1.0)))

    tgt, why = PC.target(PC.CAPITAL, c, state, adding=at_peak, closing=closing,
                         scale=scale, base_gate=gate_val,
                         range_src=o.get("range_src"))
    # 被闸门拦住时 target 恒为 0，但原因不是「没跌破」，得说清楚
    if o.get("gate_blocked") and tgt == 0 and not holding:
        why = f"波动闸门拦截（分位 {pctv * 100:.0f}% > {VOL_Q * 100:.0f}%）→ 不开新仓"
    if VT.ENABLED:
        why = f"{why}｜{VT.describe(cf, sg)}"
    if BG.ENABLED and state != "DOWN":
        why = f"{why}｜{o['gate_note']}"
    o["target"] = round(tgt)
    o["side"] = "多" if tgt > 0 else ("空" if tgt < 0 else "—")
    o["target_note"] = why
    # o["exec"] 不在这里定：它还要看「这笔空头到底建没建过」，
    # 而那是台账层面的事，逐币 analyze 看不到。统一在 main() 里算一次。
    o["cap_flag"] = PC.cap_check(tgt, c)[1]
    o["slices"] = PC.orders_needed(tgt, c)

    # ---- 最小下单额检查 ----
    # 小本金（2000U）下这不是理论问题：BTC 的最小可下单是 61.3U，
    # 而「RANGE 来路 DOWN 的底仓」和「空头」= 本金×9.2%×0.5×scale，
    # 2000U 时只有 92×scale，scale < 0.67 就挂不出去。
    # 不去静默跳过（那等于暗中减币），而是把「会被拒单」写在工单 note 上让人看见。
    _mo = PC.min_order_value(c)
    o["min_order"] = _mo
    o["below_min"] = bool(0 < abs(o["target"]) < _mo)
    if o["below_min"] and o["action"] in ("开仓", "建底仓"):
        o["note"] = (f"{o['note']}｜⚠ 金额 {abs(o['target'])}U < 该合约最小 "
                     f"{_mo:.0f}U，直接挂会被拒单")
    return o


BASE_CLEAR = "_base_clear.json"


def _load_bc():
    """底仓清仓台账：{币: 该轮 DOWN 的起始日期}。

    同一轮 DOWN 只提示一次；日线翻回去、之后再翻空，才重新提示。
    """
    try:
        with open(BASE_CLEAR, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_bc(d):
    try:
        with open(BASE_CLEAR, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        pass


GATE_STATE = "_base_gate.json"


def _load_gate():
    """底仓闸门的滞回状态：{币: 上一次的闸门值(1.0 / 0.25)}。

    滞回需要「上一次的取值」才能判「是否越过 ±1% 的带子」。没有它也能跑
    （首次按当前价 vs MA 直接判），但同一根 4H 反复重跑会得到不同结果，
    所以必须落盘。
    """
    try:
        with open(GATE_STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_gate(d):
    try:
        with open(GATE_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    except Exception:
        pass


TICKETS = "_tickets.json"


def spot_held(cur_tgt=None, pending=True):
    """各币「手上真实持有的现货底仓」金额（U）—— 从工单台账推，不另建一份持仓账。

    为什么不新建一份：多一份账就多一个会失同步的地方。台账已经记了每张
    建底仓/清底仓单及其结算状态，这就是现货仓位的完整历史。

    口径：
      · 已结算(done)的建底仓 − 已结算(done)的清底仓 − 已结算(done)的减底仓；
      · 金额优先取 `fill_notional`（实际成交额），没有才退回 `notional`（建单建议额）；
      · 该币只有未结算的建底仓 → 看 pending 参数（见下）；
      · 从没建过 → 0（不出现在返回里）。

    cur_tgt：**已废弃，仅为兼容调用方保留。**
    它原来把持有量收窄到 `min(快照, 当前目标)`，本意是防「5000 本金时代的旧快照高估」。
    在「人按新本金口径折算后买入」的情形下，这个收窄**碰巧给出正确答案**；
    但那是猜对，不是记对 —— 一旦实际成交额与当前目标不相等（加仓、
    部分成交、或按别的口径下单），它就会给出一个看起来精确、其实凭空捏造的持仓。
    2026-11-26 的教训：同一批工单，按面值读是 1,731 U、按 2000 口径读是 1,004 U，
    两种读法算出的敞口差 36 个百分点，而页面在两种情形下都显示得很自信。
    → 现在只认台账里的**事实字段**（fill_notional / notional），不再用目标去裁剪。

    pending：是否把「未结算的建底仓」也算作持有。
      True （默认）→ 用于**要不要提示清底仓**：宁可多提示，也不要因为「买了他没
                      标记」而漏掉一次该有的清仓提示。多提示的代价是看一眼，漏提示
                      的代价是裸仓过夜。
      False        → 用于**页面展示**：`open` 工单只是待办，不是持仓。
                      把它算进「持有」会让「已持有」和「该持有」永远看起来平衡，
                      从而掩盖「这一笔其实还没下」。
    """
    held = {}
    est = set()
    try:
        with open(TICKETS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return held
    for i in d.get("items", []):
        k = i.get("coin")
        # **减底仓必须算进来。** 漏掉它的后果不是数字偏一点，而是死循环：
        # 减仓单执行完（done）之后，这里仍按 300 算持有，而 ticket._held_from_ledger
        # 已经按 141 算了 → live_signals 认为还超配、继续发减仓单，永不收敛。
        # 这是本项目第六次「同一件事两处各算一遍」，前五次全是静默分叉。
        if not k or i.get("kind") not in ("建底仓", "清底仓", "减底仓"):
            continue
        # 先按状态过滤，再算金额。顺序反了会把 void/drop 的旧单也算进「推定」——
        # XRP 有一张 void 的 560 U 建仓单，它一旦参与计算，XRP 就会被误标「推定」，
        # 而 XRP 其实有一张金额完全对得上的 done 单（254 = 254）。
        status = i.get("status")
        if status not in ("done", "open"):
            continue
        # 金额优先级：实际成交额 > 建单建议额（收窄到当前目标，并标为**推定**）。
        #
        # 为什么建议额要收窄并标记：`notional` 是建单那一刻的建议值，会随本金口径
        # （5000→2000）与波动率目标化漂移。人真实下单的金额与它并不总相等 ——
        # 2026-11-26 AAVE 建单建议 167（5000 口径），实际按 2000 本金口径买了 71。
        # 没有成交记录时，只能拿 `min(建议额, 当前目标)` 去逼近，但这**是推定不是事实**，
        # 所以单独记进 est，页面必须标出来 —— 一个看起来精确的假数字，
        # 比一个明确标注的估计值危险得多。
        fn = i.get("fill_notional")
        if fn is not None:
            amt = abs(float(fn))
        else:
            amt = abs(i.get("notional") or 0)
            # 收窄**只对建底仓**成立。减底仓的 notional 本身就是「多出来的差额」，
            # 拿当前目标去收窄会把它压成目标值（减 300 被读成 141），
            # 等于把「要卖多少」换成「该持有多少」，方向完全错了。
            # 清底仓从来不该收窄（它卖的是手上全额，与目标无关）。
            if i.get("kind") == "建底仓":
                t = cur_tgt.get(k)
                # 只在「建议额明显高于当前目标」时才收窄并标记。差额不到 5%、也不到 20 U
                # 的属于取整噪声（VT 系数或权重微调），既不该改数，也不该打推定标 ——
                # 把 1 U 的差都标成「推定」，等于让标记失去信息量。
                if t is not None and t > 0 and amt - t > max(20.0, 0.05 * amt):
                    amt = t
                    est.add(k)
        sgn = 1 if i.get("kind") == "建底仓" else -1
        if i.get("status") == "done":
            held[k] = held.get(k, 0.0) + sgn * amt
        elif pending and i.get("status") == "open" and sgn > 0:
            # 未结算的**减底仓**不能当成「已经减了」—— sgn < 0 自然落在这里外面，
            # 所以「手上有多少」在减仓单执行前保持不变，这是对的。
            held[k] = max(held.get(k, 0.0), amt)
    _EST.clear()
    _EST.update({k for k in est if k in held})
    return {k: v for k, v in held.items() if v > 0}


# 上一次 spot_held 里「金额是推定而非成交记录」的币。页面据此打标，
# 让人一眼看出哪个数字经不起追问。
_EST = set()


def spot_estimated():
    return set(_EST)


def _ticket_items():
    """台账里的工单列表。读不到就返回空 —— 信号不能因为台账坏了而整轮不出。"""
    try:
        with open(TICKETS, encoding="utf-8") as f:
            return json.load(f).get("items", [])
    except Exception:
        return []


def short_opened():
    """哪些币**真的被执行过做空开仓** —— 从工单台账的 done 记录推。

    为什么需要它：状态机在 2026-11-09 就给 BTC/ETH/BNB/DOGE 挂了空头，
    而系统到 11-24 才上线，那几笔从来没有对应的「开仓·空」工单被执行过。
    策略书里它们在场（浮盈 +12.5R），账户里没有。

    没有这层区分，它们进入平仓/止损时会生成「平掉 X U 空头」的单 ——
    平的是一个不存在的仓。而**新出现的开仓·空 不受影响，照发**
    （用户的原话：「有空信号出现肯定是要空的」）。
    """
    out = set()
    try:
        with open(TICKETS, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return out
    for i in d.get("items", []):
        if (i.get("status") == "done" and i.get("kind") == "开仓"
                and i.get("side", 0) < 0):
            out.add(i["coin"])
    return out


def main():
    now = _now_utc()
    gs = _load_gate()
    rows, errs = [], []
    for c in COINS:
        try:
            rows.append(analyze(c, now, prev_gate=gs.get(c)))
        except Exception as e:
            errs.append({"c": c, "err": f"{type(e).__name__}: {e}"})
            print(f"  {c:<5} 失败 {errs[-1]['err']}")
    # 只更新成功的币；失败的保留旧状态，避免下一根从零开始判
    gs.update({o["c"]: o.get("gate_state") for o in rows})
    _save_gate(gs)

    # ---- 执行口径：这条动作实盘要不要真下单 ----
    # 必须等所有币都算完再统一判一次：判据里含 has_pos（这笔仓建没建过），
    # 那是台账层面的事，逐币 analyze() 看不到。
    _so = short_opened()
    for o in rows:
        _ps = (o.get("pos") or {}).get("side", 0)
        _has = True if _ps >= 0 else (o["c"] in _so)
        o["exec"] = PC.executable(
            o["action"],
            1 if o["target"] > 0 else (-1 if o["target"] < 0 else 0),
            _ps, _has)
        # 为什么不下单 —— 页面要分开显示，否则「没这功能」和「这笔没建过」
        # 长得一样，而处理方式完全不同。
        o["paper_why"] = (None if o["exec"] else
                          ("short_off" if not PC.EXECUTE_SHORT else "no_pos"))
        # ---- 观察名单：在执行口径上**再盖一次章** ----
        # 不能只靠 target=0 来保证「不执行」：`analyze()` 若判出 4H 突破会把 action
        # 改成「开仓」，而 `PC.executable("开仓", side=1, ...)` 对多头恒返回 True。
        # 也就是说「恰好没执行」只是因为它这一轮没突破 —— 那不是保证，是运气。
        # 这里显式置 action/exec/watch，后面 ticket.sync 再按 watch 挡一次。
        if o["c"] in PC.WATCH_ONLY:
            o["watch"] = True
            o["action"] = "观察"
            o["exec"] = False
            o["paper_why"] = "watch"
            o["note"] = (PC.WATCH_ONLY[o["c"]].split("｜")[-1]
                         + "｜只跟踪状态，不持仓、不出工单")

    # ---- 已经执行过的动作，别再叫一次 ----
    # 判断用工单**键**（币|动作|regime_start），不是「有没有 open 单」：
    # 执行完的单状态是 done，不在 open 列表里，于是页面继续写「本轮新触发：LINK 开仓」
    # —— 2026-11-27 用户刚买完 40 U 永续，下一轮系统还在催同一次突破。
    # 这是「看到了、以为做了」的镜像错误：**做完了、系统还说要做**。
    # 代价同样是人开始不信这封邮件。
    _done_keys = {i.get("key") for i in _ticket_items() if i.get("status") == "done"}
    for o in rows:
        if o.get("watch"):
            o["settled"] = False
            o["base_settled"] = False
            continue
        o["settled"] = (f'{o["c"]}|{o["action"]}|{o.get("regime_start") or ""}'
                        in _done_keys)
        # 这一段的**底仓**动作是否已执行过（建/减/清任一）。
        # 页面靠它区分两种「差额」：
        #   · 底仓还没建 → 差额是**待办**，下一轮会变成工单；
        #   · 底仓已经建过 → 差额是**偏差**（VT scale 每 7 天重算、权重也调过），
        #     本段内不会再补，因为底仓「一段一次」。
        # 不区分的话，用户会一直看到「+28 待买」却永远等不到买入提醒 ——
        # 2026-11-27 就是这个困惑：XRP +28、HYPE +22 都是已建过底仓后的 scale 漂移。
        _rs = o.get("regime_start") or ""
        o["base_settled"] = (f'{o["c"]}|建底仓|{_rs}' in _done_keys
                             or f'{o["c"]}|减底仓|{_rs}' in _done_keys
                             or f'{o["c"]}|清底仓|{_rs}' in _done_keys)

    # ---- 底仓动作提示（建 / 清 / 减）----
    # 底仓的进出此前只体现为 target 数字从正变零或反过来 ——
    # 不点开 JSON 根本看不见。下跌初期最容易「看着信号觉得没事、底仓还挂着」，
    # 上涨初期同理「该建底仓却一直空着」。这里补成显式指令。
    # 去重：按「动作 + 该轮状态的起始日」记台账，同一轮只提示一次。
    bc = _load_bc()
    # 收窄基准传**现货层额度**（base_held × range_ratio），不是 target：
    # target 含永续加仓层，拿它当现货的收窄基准会系统性偏大。
    held = spot_held({o["c"]: (o.get("base_held") or 0) * o.get("range_ratio", 1.0)
                      for o in rows})
    for o in rows:
        if o.get("watch"):
            continue          # 观察名单不参与底仓：target=0，任何「建/清」都是噪音
        if o["pos"] is not None:
            continue                      # 已有 4H 持仓，底仓动作不单独提示
        rs = o.get("regime_start") or ""
        if not rs:
            continue
        st = o["state"]
        # 底仓金额直接取 analyze() 算好的 base_held —— 它已经含 scale 与底仓闸门。
        # 「清底仓」要平掉的是**手上真实持有的量**，不是打折前的额度：
        # 闸门把底仓砍到 25% 时，按全额去清就是 4 倍超卖。
        bg = o.get("base_gate", 1.0)
        amt = o.get("base_held") or 0
        rr = o.get("range_ratio", 1.0)
        # 「该清多少」= min(策略认为该在的量, 手上真实有的量)。
        # 后者更重要：有底仓而策略要清，才发卖单；没货还发卖单就是让人卖他没买的东西
        # （BTC/ETH/BNB/NEAR 这 4 个 DOWN 币手上是零现货，就是这种情形）。
        # DOGE 2026-11-27 已移出组合，但它那笔历史空头还在 _signal.json 里留档，
        # 只是不会再出现在 COINS 循环里。
        real = held.get(o["c"], 0.0)
        if st == "DOWN":
            if real <= 0:
                # 无货可清。空头信号本身照常算（策略要它判翻转、算净敞口），
                # 但现货账户不执行 —— 由 PC.EXECUTE_SHORT 在 ticket 那层挡掉。
                continue
            act = "清底仓"
            amt = min(amt, round(real))
            tail = ("" if PC.EXECUTE_SHORT
                    else "；空头信号只记在策略书里，本账户不下单")
            note = f"日线 {rs} 起转 DOWN，撤掉现货底仓 {amt} U{tail}"
        elif st in ("UP", "RANGE") and not o.get("triggered"):
            if rr <= 0:
                # DOWN→RANGE 且系数为 0：按规则不留底仓。
                # 这与「日线翻 DOWN」是两回事：状态没变差，只是来路决定这一段不值得押。
                if real <= 0:
                    continue
                act = "清底仓"
                amt = min(amt, round(real))
                note = (f"日线 {rs} 起为 RANGE（来路 {o.get('range_src')}），"
                        f"按规则底仓归零，撤掉现货 {amt} U")
            else:
                # 「该持有」取 target 而不是 base_held 全额：target 多含了
                # **RANGE 来路系数**（rr），base_held 只含 VT scale 与底仓闸门。
                # 2026-11-27 之前两者在实盘恒等（每币都正好在目标上，分支早被
                # `delta <= 0` 挡掉），所以这个区别看不出来；权重从 9.2% 抬到
                # 10.22% 后 SOL 是 RANGE(来路 DOWN)：base_held 282 / target 141，
                # 拿 base_held 去算会让人多买一倍。
                #
                # **不要写回 o["target"]**：2026-11-27 之前这里写的是
                # `o["target"] = delta`，把「该持有多少」覆盖成了「这一单买多少」。
                # 后果是页面「该持有」列变成单笔金额、spot_held 的收窄基准也跟着错
                # （XRP 真实持有 254 U，被显示成 28 U）。差额走独立字段。
                hld = round(o.get("target") or amt)
                delta = hld - round(real)
                if abs(delta) < PC.min_order_value(o["c"]):
                    continue
                gtail = ("" if bg >= 1.0
                         else f"（价格已在 MA{BG.GATE_MA} 下方，建仓只给 {round(bg * 100)}%）")
                rtail = ("" if rr >= 1.0
                         else f"（来路 {o.get('range_src')}，只给 {round(rr * 100)}%）")
                if delta > 0:
                    act = "建底仓"
                    amt = delta
                    o["order_amount"] = delta
                    if real > 0:
                        # 手上已有的不重复买。base_held 是「该持有多少」，
                        # 差额才是这一单要买的。缺了这一步，UP→RANGE 会让
                        # regime_start 变化 → 再发一张全额建仓单 → 同一份底仓买两遍。
                        note = (f"日线 {rs} 起为 {st}，底仓应在场 {hld} U；"
                                f"已持有 {round(real)} U，本单补 {delta} U"
                                f"{gtail}{rtail}（4H 突破后再加到峰值）")
                    else:
                        # 手上是空的：建多少 = target（含 rr），不是 base_held 全额。
                        note = (f"日线 {rs} 起为 {st}，底仓 {amt} U 应在场"
                                f"{gtail}{rtail}（4H 突破后再加到峰值）")
                else:
                    # ---- 减底仓（2026-11-27 新增）----
                    # 走到这里说明「该持有」比手上少。最常见的是 UP→RANGE(来路 DOWN)：
                    # rr 从 1.0 掉到 0.5，target 腰斩，而手上还是全额。
                    #
                    # 为什么必须减、而不是「建过就算了」：
                    #   1) 回测 bt_skeleton.base_series() 是**逐 bar** 返回系数的，
                    #      RANGE 段一进入就按 0.5 算净值。实盘不减 = 跑的是 1.0 档，
                    #      那份 291 万 / −54.9% / Calmar 1.29 就不适用于实际持仓 ——
                    #      风控读数和预期收益全部失效，而这**不会有任何报错**。
                    #   2) 结构上这是唯一一处单边缺口：建底仓有分支、清底仓有分支，
                    #      唯独「目标变小、要卖一半」没有，`delta <= 0` 直接 continue。
                    #
                    # 【收益上的理由很弱，别拿它当动机】来路 DOWN 的 RANGE 段 185 段里
                    #   41% 五个交易日内就结束，减了要马上买回：摩擦约 0.85%/年，
                    #   而不减的 beta 成本约 1.08%/年 —— 净差 0.23pp，几乎打平。
                    #   这条改动的正当性是**口径一致**，不是多赚。
                    act = "减底仓"
                    amt = min(-delta, round(real))   # 不能卖超过手上真有的
                    o["order_amount"] = amt
                    note = (f"日线 {rs} 起为 {st}，底仓应在场 {hld} U；"
                            f"已持有 {round(real)} U，超配 {amt} U 减回"
                            f"{rtail}（现货卖出，无价格止损）")
        else:
            continue                      # 已触发 = action 已是「开仓」，不重复
        # 去重键必须带 rr：系数从 1.0 调到 0.5 时底仓金额跟着变，
        # 若只用 act:rs，会被误判成「这轮已提示过」而对已有持仓不提示减仓。
        key = f"{act}:{rs}:{rr:.2f}"
        if bc.get(o["c"]) == key:
            continue                      # 这一轮已经提示过
        o["action"] = act
        o["clear_amount"] = amt
        o["note"] = note
        # 建/清底仓都是现货，与做空开关无关，实盘一律执行。
        # 必须在这里重算：上面的 exec 是按 analyze() 时的 action（空仓/闸门拦截）
        # 判的，到这里 action 才被改写成底仓动作。
        o["exec"] = True
        o["paper_why"] = None
        bc[o["c"]] = key
    _save_bc(bc)

    for r in rows:
        amt = r["target"]
        astr = f"空 {abs(amt):,.0f}" if amt < 0 else f"{amt:,.0f}"
        print(f"  {r['c']:<5} {r['state']:<6} {r['action']:<8}{astr:>10} U  {r['note']}")

    _ACT = ("开仓", "平仓", "清底仓", "建底仓", "减底仓", "止损临近", "即将到期")
    # 分成两组，不能混：urgent 会变成工单，paper 只进策略书。
    # 「需要动作 N 个」如果连纸面空头也数进去，等于每封信都在叫醒你去做不会做的事。
    urgent = [o for o in rows if o["action"] in _ACT and o.get("exec", True)
              and not o.get("settled")]
    paper = [o for o in rows if o["action"] in _ACT and not o.get("exec", True)]
    # 策略书记录了空头、但账户从没建过的那几个：它们不是"不做的空头"，
    # 是"错过了入场的空头"。页面上要单列，否则会跟"没这功能"混成一件事。
    unopened = [o["c"] for o in rows
                if (o.get("pos") or {}).get("side", 0) < 0
                and o.get("paper_why") == "no_pos"]
    # 以「所有品种里最新的那根已收盘 4H」为准，各币数据源不同、可能差一根
    bar = max((o["bar"] for o in rows), default=None)
    if PC.ALLOW_SHORT and PC.EXECUTE_SHORT:
        _mode = "合约（含做空·执行）"
    elif PC.ALLOW_SHORT:
        _mode = "策略含做空 · 执行纯现货"
    else:
        _mode = "纯现货（策略不做空）"
    pay = {"asof": now.strftime("%Y-%m-%d %H:%M UTC"),
           "asof_bj": (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
           "bar4h": bar, "capital": PC.CAPITAL, "allow_short": PC.ALLOW_SHORT,
           "execute_short": PC.EXECUTE_SHORT, "mode": _mode,
           # 手上真实持有的现货底仓（从工单台账推），给 hold 页与邮件做「有货才提示卖」。
           # pending=False：页面上的「持有」必须是**已经买了的**，不能把还没执行的
           # open 工单算进去 —— 那会让「该持有/差额」自我平衡，把「这一笔还没下」藏起来。
           # spot_est：这几行的金额没有成交记录、是按「建单建议额收窄到现货层额度」
           # 推出来的，页面要标「推定」。收窄基准必须是 base_held × rr 而不是 target ——
           # target 含永续加仓层，拿它当基准会让推定判据系统性偏松。
           "spot_held": {k: round(v) for k, v in
                         spot_held({o["c"]: (o.get("base_held") or 0)
                                    * o.get("range_ratio", 1.0) for o in rows},
                                   pending=False).items()},
           "spot_est": sorted(spot_estimated()),
           # 生效配置留痕：三个月后回看「当时到底开没开」不用翻代码
           "vt": {"on": VT.ENABLED, "lb": VT.LB, "target": VT.TARGET,
                  "lo": VT.LO, "hi": VT.HI, "kappa": VT.KAPPA,
                  "rebal": VT.REBAL_DAYS},
           "basegate": {"on": BG.ENABLED, "ma": BG.GATE_MA, "cv": BG.GATE_CV,
                        "hyst": BG.GATE_HYST},
           "coins": rows, "urgent": [o["c"] for o in urgent],
           "paper": [o["c"] for o in paper], "unopened": unopened,
           "errors": errs}
    json.dump(pay, open("_signal.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"), default=str)

    print(f"\n数据截止 4H：{pay['bar4h']}   生成于 {pay['asof']}"
          f"   资金基准：{PC.CAPITAL:,.0f} U   模式：{pay['mode']}")
    print(f"需要动作：{len(urgent)} 个" + (("  → " + " ".join(pay['urgent'])) if urgent else "（全部无需动作）"))
    if paper:
        print(f"纸面动作 {len(paper)} 个（策略算到但本账户不执行）：{' '.join(pay['paper'])}")
    if unopened:
        print(f"空头未建仓 {len(unopened)} 个（策略书在场、账户没有）："
              f"{' '.join(unopened)} —— 只影响平仓，不影响新开空信号")
    if errs:
        print(f"取数失败 {len(errs)} 个：{[e['c'] for e in errs]}")
    return pay


if __name__ == "__main__":
    main()
