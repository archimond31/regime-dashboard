#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_ticket_dedup.py —— 工单去重口径的回归检查（2026-11-27）。

== 为什么要这个脚本 ==
ticket.sync 原来只有一条去重：`if any(i["key"] == k) → continue`，注释写着
「开过就不再开（无论 done / drop / void）」。问题在于 key 是
`币|动作|regime_start`，而 regime_start **只在日线翻状态时才变** ——
于是一整段 UP 里，每类动作一辈子只能发一次工单。

只要第一张被 void（行情变化自动作废 / 人工 drop），后续同段的新突破、新止损提醒
就被永久挡掉，而页面和日志照样写「需要动作 N 个」。三次实测：

    2026-11-24 08:05  LINK 止损临近（距止损 1.06%）122 U  → 0 新单
    2026-11-26 00:05  NEAR 开仓·空 54 U                  → 0 新单
    2026-11-27 12:05  LINK 开仓（4H 破前 20 根高点）122 U → 0 新单

== 现行规则（本脚本守的就是它）==
  · 建底仓 / 清底仓：同一状态段**只发一次**。现货不该重复买同一份底仓，
    金额已由 live_signals 的「目标 − 已持有」和 _held_from_ledger 核对。
  · 4H 层（开仓 / 平仓 / 止损临近 / 即将到期）：只挡 **done** 与 **open**；
    void / drop 允许重发 —— 那一次没执行成，同段里的新突破是新机会。
    但要求**换了一根 4H**（同 bar 不重发），否则一轮里多次 sync 会刷屏。

七种情形，任何一种不符合预期就 exit 1。用法：python _verify_ticket_dedup.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="tkdedup_")
LEDGER = os.path.join(TMP, "_tickets.json")
SIGNAL = os.path.join(TMP, "_signal.json")
os.environ["TICKET_LEDGER"] = LEDGER          # ticket.py 支持这个环境变量

import ticket                                  # noqa: E402
ticket.SIG = SIGNAL                            # sync() 默认读 SIG

BAR = "2026-11-27 04:00"
OLD_BAR = "2026-11-23 20:00"
RS = "2026-11-23"                              # regime_start


def make_signal(action):
    """造一枚最小可用的信号。开仓要求 triggered=True，否则会被底仓兜底重复加入。"""
    triggered = action != "建底仓"
    return {"bar4h": BAR, "coins": [{
        "c": "LINK", "action": action, "state": "UP", "regime_start": RS,
        "target": 122, "pos": None, "triggered": triggered,
        "px": 9.50, "atr": 0.30, "note": "验证用", "exec": True,
        "slices": 1, "cap_flag": "", "bar": BAR,
    }]}


def prior(kind, status, bar):
    """造一张「同 key 的旧单」。kind == None 表示台账为空。"""
    return [] if kind is None else [{
        "id": 1, "key": f"LINK|{kind}|{RS}", "coin": "LINK", "kind": kind,
        "side": 1, "notional": 122, "cap": 2000.0, "ref_px": 9.4,
        "stop": None, "state": "UP", "pos_side": 0, "regime_start": RS,
        "bar": bar, "created": bar, "age_bars": 0, "slices": 1,
        "cap_flag": "", "note": "旧单", "status": status,
        "settled_at": None, "fill_px": None, "fill_notional": None,
        "void_note": None, "notified": True,
    }]


CASES = [
    # (动作,     旧单动作, 旧单状态, 旧单 bar,  期望新单数, 说明)
    ("开仓",    None,     None,   None,      1, "没有旧单 → 正常发"),
    ("开仓",    "开仓",  "void", BAR,       0, "同一根 4H 的 void 单 → 不重发"),
    ("开仓",    "开仓",  "void", OLD_BAR,   1, "更早那根的 void 单 → 新突破应重发"),
    ("开仓",    "开仓",  "open", OLD_BAR,   0, "还挂着 → 不重复发"),
    ("开仓",    "开仓",  "done", OLD_BAR,   0, "已执行 → 这一段不再发"),
    ("建底仓",  "建底仓", "void", OLD_BAR,   0, "现货：一段只发一次，void 也不重发"),
    ("建底仓",  None,     None,   None,      1, "没有旧单 → 正常发"),
]


def run_case(action, prior_kind, prior_status, prior_bar):
    json.dump({"next_id": 2, "items": prior(prior_kind, prior_status, prior_bar)},
              open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(make_signal(action), open(SIGNAL, "w", encoding="utf-8"),
              ensure_ascii=False)
    return ticket.sync()


def main():
    bad = 0
    print(f"{'动作':<8}{'旧单':<14}{'期望':>5}{'实际':>5}  判定")
    print("-" * 62)
    for act, pk, ps, pb, want, desc in CASES:
        got = run_case(act, pk, ps, pb)["opened"]
        ok = got == want
        bad += 0 if ok else 1
        label = f"{pk or '—'}/{ps or '—'}"
        print(f"{act:<8}{label:<14}{want:>5}{got:>5}  {'✓' if ok else '✗ ' + desc}")
    print("-" * 62)
    if bad:
        print(f"✗ {bad} 项不符预期 —— 去重口径被改坏了")
        return 1
    print("✓ 七项全过：4H 层只挡 done/open，现货仍是「一段一次」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
