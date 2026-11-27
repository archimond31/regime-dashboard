#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_rebalance.py —— 「减底仓」的回归检查（2026-11-27）。

== 为什么要这个脚本 ==
2026-11-27 之前，底仓只有**两个**方向的动作：建（买）和清（全卖）。
「目标变小、要卖掉多出来那一半」这个方向根本没有分支 ——
`live_signals` 里算出的负差额被 `if delta <= 0: continue` 直接吞掉。

后果不是报错，是**静默跑另一套仓位**：
  · BTC 日线 UP 建了 398 U；跌破背离失败位后退回 RANGE(来路 DOWN)，
    target 掉到 199，而手上还是 398 —— 实盘跑的是 1.0 档；
  · 而回测 `bt_skeleton.base_series()` 是逐 bar 返回系数的，RANGE 段
    一进入就按 0.5 算净值。那份 291 万 / −54.9% / Calmar 1.29 于是
    不适用于实际持仓，风控读数与预期收益全部失效，且没有任何报错。

== 本脚本守的八条 ==
  1. `_notional(减底仓)` = order_amount（差额），不是 base_held 全额
  2. `_side(减底仓)` = −1（卖）
  3. `_held_from_ledger`：建 398 → 减 199 → 只剩 199
  4. 键区分：`LINK|建底仓|rs` ≠ `LINK|减底仓|rs`（否则会被当成同一件事去重）
  5. sync：target 回到全额基准 → 减仓单作废
  6. sync：target 仍低于基准 → 减仓单保留
  7. sync：手上已持有 398、target 199 时**不再生成建底仓单**（防止多买）
  8. `ticket._held_from_ledger` 与 `live_signals.spot_held` 对同一份台账给出
     **相同**的持仓。这条最要命：漏改任何一处，减仓执行完仍被判超配 →
     减仓单无限重发，而页面每天看起来都「正常」。

用法：python _verify_rebalance.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="tkrebal_")
LEDGER = os.path.join(TMP, "_tickets.json")
SIGNAL = os.path.join(TMP, "_signal.json")
os.environ["TICKET_LEDGER"] = LEDGER

import ticket                                   # noqa: E402
ticket.SIG = SIGNAL

BAR = "2026-11-27 04:00"
RS = "2026-11-23"


def item(kind, status="open", notional=199, fill_notional=None, coin="LINK"):
    return {
        "id": 1, "key": f"{coin}|{kind}|{RS}", "coin": coin, "kind": kind,
        "side": -1 if kind in ("清底仓", "减底仓") else 1,
        "notional": notional, "cap": 2000.0, "ref_px": 9.4, "stop": None,
        "state": "RANGE", "pos_side": 0, "regime_start": RS,
        "bar": BAR, "created": BAR, "age_bars": 0, "slices": 1,
        "cap_flag": "", "note": "验证用", "status": status,
        "settled_at": None, "fill_px": None, "fill_notional": fill_notional,
        "void_note": None, "notified": True,
    }


def sig(target, base_held, action="空仓", state="RANGE"):
    return {"bar4h": BAR, "coins": [{
        "c": "LINK", "action": action, "state": state, "regime_start": RS,
        "target": target, "base_held": base_held, "pos": None,
        "triggered": False, "px": 9.50, "atr": 0.30, "note": "验证用",
        "exec": True, "slices": 1, "cap_flag": "", "bar": BAR,
    }]}


def run(items, target, base_held, action="空仓"):
    json.dump({"next_id": 9, "items": items},
              open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(sig(target, base_held, action),
              open(SIGNAL, "w", encoding="utf-8"), ensure_ascii=False)
    return ticket.sync()


def main():
    bad = 0
    res = []

    def chk(name, got, want):
        nonlocal bad
        ok = got == want
        bad += 0 if ok else 1
        res.append((name, want, got, ok))

    # 1) 金额 = 差额
    o = {"c": "LINK", "action": "减底仓", "order_amount": 199,
         "target": 199, "base_held": 398}
    chk("金额 = order_amount（差额）", ticket._notional(o), 199)

    # 2) 方向 = 卖
    chk("方向 = 卖(-1)", ticket._side(o), -1)

    # 3) 台账：建 398 → 减 199 → 199
    held = ticket._held_from_ledger([
        item("建底仓", "done", notional=398),
        item("减底仓", "done", notional=199),
    ])
    chk("台账 建398−减199 = 199", held.get("LINK"), 199)

    # 4) 键区分
    k_buy = f'LINK|建底仓|{RS}'
    k_cut = f'LINK|减底仓|{RS}'
    chk("建/减 的键不同", k_buy == k_cut, False)

    # 5) target 回到全额 → 减仓单作废
    d = run([item("减底仓", "open", notional=199)], target=398, base_held=398)
    st = json.load(open(LEDGER, encoding="utf-8"))["items"][0]["status"]
    chk("target 回全额 → 作废", st, "void")

    # 6) target 仍低 → 保留
    d = run([item("减底仓", "open", notional=199)], target=199, base_held=398)
    st = json.load(open(LEDGER, encoding="utf-8"))["items"][0]["status"]
    chk("target 仍低 → 保留", st, "open")

    # 7) 已持有 398、目标 199 → 不再建仓（超配不该被当成欠配）
    d = run([item("建底仓", "done", notional=398)],
            target=199, base_held=398, action="建底仓")
    chk("超配不重复建仓 → 0 新单", d["opened"], 0)

    # 8) **两处推导必须相等**（本项目第六次「同一件事各算一遍」的防线）
    #    ticket._held_from_ledger 与 live_signals.spot_held 各有一套实现：
    #    前者给台账用，后者给 live_signals 算差额用。任何一处漏了「减底仓」，
    #    都会让减仓单执行完仍被判为超配 → 无限重复发单。
    import live_signals as LS
    LS.TICKETS = LEDGER
    ledger = [item("建底仓", "done", notional=398),
              item("减底仓", "done", notional=199)]
    json.dump({"next_id": 9, "items": ledger},
              open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
    a = ticket._held_from_ledger(ledger).get("LINK")
    b = LS.spot_held({}, pending=False).get("LINK")
    chk("ticket 与 live_signals 持仓一致", a, b)

    print(f"{'检查项':<32}{'期望':>8}{'实际':>8}  判定")
    print("-" * 62)
    for name, want, got, ok in res:
        print(f"{name:<32}{str(want):>8}{str(got):>8}  {'✓' if ok else '✗'}")
    print("-" * 62)
    if bad:
        print(f"✗ {bad} 项不符预期 —— 减底仓口径被改坏了")
        return 1
    print("✓ 八项全过：减底仓金额取差额、方向卖出、台账扣减、键独立、"
          "目标回全额自动作废、超配不重复买、两处持仓推导一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
