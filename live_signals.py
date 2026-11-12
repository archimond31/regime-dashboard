#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实盘信号生成（只出信号，绝不下单）。

品种：BTC ETH SOL BNB XRP **HYPE**(替换 ADA) LINK CRV UNI AAVE

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
from strategy_1h_rsi import regime_series, align_regime_safe
import bt_skeleton as BT

COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "HYPE", "LINK", "CRV", "UNI", "AAVE"]
SECTOR = {"BTC": "储值", "ETH": "L1", "SOL": "L1", "BNB": "交易所", "XRP": "支付",
          "HYPE": "DEX/永续", "LINK": "预言机", "CRV": "DeFi", "UNI": "DeFi", "AAVE": "DeFi"}
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


def analyze(c, now):
    b4, d1 = load_coin(c, now)
    n = len(b4)
    reg_map = regime_series(d1)
    rg4 = align_regime_safe(b4, reg_map)
    atr = atr_wilder(b4, 14)
    pct = BT.atr_percentile(b4, atr, coin=c)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=VOL_Q, coin=c)

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
    return o


def main():
    now = _now_utc()
    rows, errs = [], []
    for c in COINS:
        try:
            rows.append(analyze(c, now))
            print(f"  {c:<5} {rows[-1]['state']:<6} {rows[-1]['action']:<8} {rows[-1]['note']}")
        except Exception as e:
            errs.append({"c": c, "err": f"{type(e).__name__}: {e}"})
            print(f"  {c:<5} 失败 {errs[-1]['err']}")

    urgent = [o for o in rows if o["action"] in ("开仓", "平仓", "止损临近", "即将到期")]
    # 以「所有品种里最新的那根已收盘 4H」为准，各币数据源不同、可能差一根
    bar = max((o["bar"] for o in rows), default=None)
    pay = {"asof": now.strftime("%Y-%m-%d %H:%M UTC"),
           "asof_bj": (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M"),
           "bar4h": bar,
           "coins": rows, "urgent": [o["c"] for o in urgent], "errors": errs}
    json.dump(pay, open("_signal.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"), default=str)

    print(f"\n数据截止 4H：{pay['bar4h']}   生成于 {pay['asof']}")
    print(f"需要动作：{len(urgent)} 个" + (("  → " + " ".join(pay['urgent'])) if urgent else "（全部无需动作）"))
    if errs:
        print(f"取数失败 {len(errs)} 个：{[e['c'] for e in errs]}")
    return pay


if __name__ == "__main__":
    main()
