#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘信号生成（只出信号，绝不下单）。

品种：BTC ETH SOL BNB XRP HYPE LINK UNI AAVE ZEC DOGE NEAR（12 币，2026-11-18 去 CRV）

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

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "HYPE", "LINK", "UNI", "AAVE",
         "ZEC", "DOGE", "NEAR"]
SECTOR = {"BTC": "储值", "ETH": "L1", "SOL": "L1", "BNB": "交易所", "XRP": "支付",
          "HYPE": "DEX/永续", "LINK": "预言机", "UNI": "DeFi", "AAVE": "DeFi",
          "ZEC": "隐私币", "DOGE": "meme", "NEAR": "L1"}
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
    sig, trades = BT.signals(b4, rg4, atr, vol_q=VOL_Q, coin=c, blood4=blood4)

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
    # 用于 portfolio_config.RANGE_RATIO_BY_SRC —— DOWN→RANGE 的底仓打 0。
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
    if holding is None:
        want = 1 if state == "UP" else (-1 if state == "DOWN" else 0)
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
    o["cap_flag"] = PC.cap_check(tgt, c)[1]
    o["slices"] = PC.orders_needed(tgt, c)
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

    # ---- 底仓动作提示（建 / 清）----
    # 底仓的进出此前只体现为 target 数字从正变零或反过来 ——
    # 不点开 JSON 根本看不见。下跌初期最容易「看着信号觉得没事、底仓还挂着」，
    # 上涨初期同理「该建底仓却一直空着」。这里补成显式指令。
    # 去重：按「动作 + 该轮状态的起始日」记台账，同一轮只提示一次。
    bc = _load_bc()
    for o in rows:
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
        if st == "DOWN":
            act = "清底仓"
            tail = ("；做空仍被波动闸门拦着" if o.get("gate_blocked")
                    else "；做空要等 4H 收盘跌破前 20 根低点")
            note = f"日线 {rs} 起转 DOWN，撤掉现货底仓 {amt} U{tail}"
        elif st in ("UP", "RANGE") and not o.get("triggered"):
            rr = o.get("range_ratio", 1.0)
            if rr <= 0:
                # DOWN→RANGE：暴跌后的筑底期零期望，按规则不留底仓。
                # 这与「日线翻 DOWN」是两回事：状态没变差，只是来路决定这一段不值得押。
                act = "清底仓"
                note = (f"日线 {rs} 起为 RANGE（来路 {o.get('range_src')}），"
                        f"按规则底仓归零，撤掉现货 {amt} U")
            else:
                act = "建底仓"
                gtail = ("" if bg >= 1.0
                         else f"（价格已在 MA{BG.GATE_MA} 下方，只建 {round(bg * 100)}%）")
                rtail = ("" if rr >= 1.0
                         else f"（来路 {o.get('range_src')}，只建 {round(rr * 100)}%）")
                note = (f"日线 {rs} 起为 {st}，底仓 {amt} U 应在场"
                        f"{gtail}{rtail}（4H 突破后再加到峰值）")
        else:
            continue                      # 已触发 = action 已是「开仓」，不重复
        key = act + ":" + rs
        if bc.get(o["c"]) == key:
            continue                      # 这一轮已经提示过
        o["action"] = act
        o["clear_amount"] = amt
        o["note"] = note
        bc[o["c"]] = key
    _save_bc(bc)

    for r in rows:
        amt = r["target"]
        astr = f"空 {abs(amt):,.0f}" if amt < 0 else f"{amt:,.0f}"
        print(f"  {r['c']:<5} {r['state']:<6} {r['action']:<8}{astr:>10} U  {r['note']}")

    urgent = [o for o in rows
              if o["action"] in ("开仓", "平仓", "清底仓", "建底仓",
                                 "止损临近", "即将到期")]
    # 以「所有品种里最新的那根已收盘 4H」为准，各币数据源不同、可能差一根
    bar = max((o["bar"] for o in rows), default=None)
    pay = {"asof": now.strftime("%Y-%m-%d %H:%M UTC"),
           "asof_bj": (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
           "bar4h": bar, "capital": PC.CAPITAL, "allow_short": PC.ALLOW_SHORT,
           # 生效配置留痕：三个月后回看「当时到底开没开」不用翻代码
           "vt": {"on": VT.ENABLED, "lb": VT.LB, "target": VT.TARGET,
                  "lo": VT.LO, "hi": VT.HI, "kappa": VT.KAPPA,
                  "rebal": VT.REBAL_DAYS},
           "basegate": {"on": BG.ENABLED, "ma": BG.GATE_MA, "cv": BG.GATE_CV,
                        "hyst": BG.GATE_HYST},
           "coins": rows, "urgent": [o["c"] for o in urgent], "errors": errs}
    json.dump(pay, open("_signal.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"), default=str)

    print(f"\n数据截止 4H：{pay['bar4h']}   生成于 {pay['asof']}"
          f"   资金基准：{PC.CAPITAL:,.0f} U")
    print(f"需要动作：{len(urgent)} 个" + (("  → " + " ".join(pay['urgent'])) if urgent else "（全部无需动作）"))
    if errs:
        print(f"取数失败 {len(errs)} 个：{[e['c'] for e in errs]}")
    return pay


if __name__ == "__main__":
    main()
