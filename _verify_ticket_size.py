#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_verify_ticket_size.py —— 工单金额口径的回归检查（2026-11-27）。

== 守的是什么 ==
永续那条腿**只承担加仓层**（ADD_RATIO=0.5），底仓 1.0× 走现货
（portfolio_config: 「加仓层：突破后追加（多头叠加 / 空头唯一来源）」）。

旧实现在「开仓·多」上直接拿 `target` 当金额，而 target 是**峰值 = 底仓 + 加仓层**，
于是把已在现货的底仓在永续上又买了一遍。用 LINK 实测：

    现货已持 82 U ＋ 开仓单 122 U ＝ 204 U ＝ 本金 10.2%（2.5× 权重）
    策略峰值 1.5× 权重 ＝ 123 U  →  **多买 66%**

「平仓·多」对称地错：旧式 `CAPITAL*w*(BASE+ADD)` 会给 LINK 算出 120 U，
而实际只开了 41 U —— 照它平仓会在永续上卖成 79 U 的净空头。

这个 bug 一直存在但从未造成损失：所有「开仓」工单在 2026-11-27 之前都被 void
（用户当时没有合约账户，也没执行过）。**一旦开始执行永续，它就是第一笔真钱。**
用法：python _verify_ticket_size.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="tksize_")
os.environ["TICKET_LEDGER"] = os.path.join(TMP, "_tickets.json")

import ticket                                   # noqa: E402
import portfolio_config as PC                   # noqa: E402


def coin(**kw):
    base = {"c": "LINK", "state": "UP", "regime_start": "2026-11-23",
            "target": 122, "base_held": 82, "pos": None,
            "px": 9.501, "vt": {"scale": 1.0206}}
    base.update(kw)
    return base


def set_ledger(items):
    json.dump({"next_id": len(items) + 1, "items": items},
              open(ticket.TICKETS, "w", encoding="utf-8"), ensure_ascii=False)


def done_open(notional):
    return {"id": 1, "key": "LINK|开仓|2026-11-23", "coin": "LINK", "kind": "开仓",
            "side": 1, "notional": notional, "cap": PC.CAPITAL, "ref_px": 9.4,
            "stop": None, "state": "UP", "pos_side": 0, "regime_start": "2026-11-23",
            "bar": "2026-11-23 20:00", "created": "2026-11-23 20:00", "age_bars": 0,
            "slices": 1, "cap_flag": "", "note": "", "status": "done",
            "settled_at": "2026-11-24 04:00", "fill_px": 9.4,
            "fill_notional": notional, "void_note": None, "notified": True}


CASES = []


def case(name, sig, items, want):
    set_ledger(items)
    got = ticket._notional(sig)
    CASES.append((name, want, got))


# 1) 开仓·多 = 峰值 − 底仓（只算加仓层）
case("开仓·多 = 122−82", dict(coin(), action="开仓"), [], 40)
# 2) 开仓·空：DOWN 底仓为 0，target 本身就是加仓层，不减
case("开仓·空 = 199", dict(coin(c="BTC", state="DOWN", target=-199, base_held=398),
                          action="开仓"), [], 199)
# 3) 平仓·多：用当时实际开了多少，不用 1.5× 重算
case("平仓·多 用已执行额 40", dict(coin(pos={"side": 1, "regime": "UP"}),
                               action="平仓"), [done_open(40)], 40)
# 4) 平仓·多：没有执行记录 → 回落到 加仓层（w×0.5×scale），不是 1.5×
def fallback_expect():
    w = PC.WEIGHTS.get("LINK", 0.0)
    return round(PC.CAPITAL * w * PC.ADD_RATIO * 1.0206)


case("平仓·多 回落=加仓层", dict(coin(pos={"side": 1, "regime": "UP"}),
                             action="止损临近"), [], fallback_expect())
# 5) 建底仓：优先 order_amount
case("建底仓 用 order_amount", dict(coin(action="建底仓", order_amount=28)), [], 28)
# 6) 建底仓：没有差额时用 target
case("建底仓 回落到 target", dict(coin(action="建底仓")), [], 122)
# 7) 清底仓：用手上真实持有的 base_held
case("清底仓 = base_held", dict(coin(action="清底仓", base_held=82)), [], 82)


def done_open_short(notional):
    d = done_open(notional)
    d.update({"coin": "BTC", "key": "BTC|开仓|2026-10-25", "side": -1,
              "notional": notional, "fill_notional": notional})
    return d


# 8) 平仓·空：用当时实际开了多少
#    空头的 target 本身等于加仓层（DOWN 段底仓为 0），所以这里不该出现「减 base_held」
#    以外的任何差；真正要守的是**别拿今天的 scale 重算**。
case("[空]平仓 用已执行额 199",
     dict(coin(c="BTC", state="DOWN", target=-199, base_held=398,
               pos={"side": -1, "regime": "DOWN"}), action="平仓"),
     [done_open_short(199)], 199)


# ---- 守卫：空头没建过仓时，平仓单**根本不该被生成** ----
# BTC/ETH/BNB 11-09 那三笔是「未建仓」（系统 11-24 才上线）。
# 它们的 平仓/止损 若走到 _notional，会算出一个金额去平一个不存在的仓 ——
# 所以第一道闸在 PC.executable，不在 _notional。这里把这道闸钉住。
GUARDS = [
    ("[空]无建仓记录 → 不发平仓单", False, PC.executable("平仓", -1, -1, False)),
    ("[空]无建仓记录 → 不发止损单", False, PC.executable("止损临近", -1, -1, False)),
    ("[空]有建仓记录 → 照发", True, PC.executable("平仓", -1, -1, True)),
    ("[空]新空头 → 照发（pos=0）", True, PC.executable("开仓", -1, 0, True)),
]


# ---- 补录（adopt）：账户里有、台账里没有 ----
# 这是 8) 的前提：不补录，_opened_amount 恒为 None，空头的平仓金额只能靠今日
# scale 重算 —— 而 VT 系数每 7 天变一次，平出来就不是原仓位。
def adopt_case():
    set_ledger([])
    rc = ticket.adopt("BTC", 199, side=-1)
    return rc, ticket._opened_amount("BTC"), ticket._opened_amount("ETH")


# ---- 显示层：_expect/_disp_amount 必须与 _notional 同源 ----
# 2026-11-27：render() 里原来自己算 capital*w*(BASE+ADD)，
# #20 LINK 开仓 40 U 会被显示成「买入 122 U」并把 40 标成「已换算」——
# 刚在 _notional 修掉的 66% 多买，被显示层原样搬回来。
def display_case():
    set_ledger([done_open(40)])
    item = {"coin": "LINK", "kind": "开仓", "notional": 40,
            "cap": PC.CAPITAL, "regime": "UP", "side": 1}
    return ticket._disp_amount(item, {"LINK": coin()})


def display_base_case():
    """建底仓的补仓单（差额 28）不能被显示成全额 282。

    这是 _expect 里最危险的一条：拿 target 当基准，一张「再补 28 U」的单
    会显示成「买入 282 U」。方向上高估建仓 10 倍，比少买危险得多。
    """
    set_ledger([])
    item = {"coin": "XRP", "kind": "建底仓", "notional": 28,
            "cap": PC.CAPITAL, "regime": "UP", "side": 1}
    cur = {"XRP": coin(c="XRP", target=282, base_held=282, order_amount=28)}
    return ticket._disp_amount(item, cur)


def main():
    bad = 0
    print(f"{'情形':<26}{'期望':>7}{'实际':>7}  判定")
    print("-" * 54)
    for name, want, got in CASES:
        ok = want == got
        bad += 0 if ok else 1
        print(f"{name:<26}{want:>7}{got:>7}  {'✓' if ok else '✗'}")
    print("-" * 54)
    rc, btc, eth = adopt_case()
    for name, want, got in [("补录后 → BTC 199", 199, btc),
                            ("补录后 → ETH 无记录", None, eth),
                            ("补录返回码 0", 0, rc)]:
        ok = want == got
        bad += 0 if ok else 1
        print(f"{name:<26}{str(want):>7}{str(got):>7}  {'✓' if ok else '✗'}")
    got = display_case()
    for name, want, g in [("显示层金额与 _notional 同源", 40, got[0]),
                          ("显示层不标「已换算」", None, got[1])]:
        ok = want == g
        bad += 0 if ok else 1
        print(f"{name:<26}{str(want):>7}{str(g):>7}  {'✓' if ok else '✗'}")
    got = display_base_case()
    for name, want, g in [("建底仓补仓单不被放大", 28, got[0]),
                          ("建底仓补仓单不标「已换算」", None, got[1])]:
        ok = want == g
        bad += 0 if ok else 1
        print(f"{name:<26}{str(want):>7}{str(g):>7}  {'✓' if ok else '✗'}")
    print("-" * 54)
    for name, want, got in GUARDS:
        ok = want == got
        bad += 0 if ok else 1
        print(f"{name:<26}{str(want):>7}{str(got):>7}  {'✓' if ok else '✗'}")
    print("-" * 54)
    if bad:
        print(f"✗ {bad} 项不符 —— 工单金额口径被改坏了，别拿去下单")
        return 1
    print("✓ 全过：永续只算加仓层；空头没建仓不发平仓单；补录后按实际额平；"
          "显示层与 _notional 同源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
