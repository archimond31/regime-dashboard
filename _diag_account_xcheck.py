#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_diag_account_xcheck.py —— 台账 vs 账户，逐币交叉核对（**只读，不改任何状态**）

## 为什么先做这个，而不是直接把 `spot_held` 换成账户实测

本项目有一条硬规矩：**新口径先观察，观察结果和旧口径对得上，才切换。**
（`DN=0.0`、`cross_fix_init`、底仓闸门这几次翻车，全都是"改完直接上"。）

换成账户余额这件事看起来是纯粹的升级 —— 事实当然比推定准。但有三件事只有先看一眼才知道：

  1. **我们的「目标」是不是真的等于账户里的东西**。`target` 是名义金额（U），
     账户里是币（数量）。换算要乘价，而换算用的价比策略用的收盘价晚 0~4 小时。
     偏差如果在 1% 量级，那是价格时点，不是错；如果在 20%，那是我们把某个口径搞错了。
  2. **哪些币的台账是"推定"的**。`spot_held` 在没有 `fill_notional` 时会拿建议额猜，
     页面标「推定」。这些币换上实测之后数字会跳 —— 跳多少，得先量出来。
  3. **账户里有没有台账不知道的仓**。`has_pos` 只认台账，所以「账户有、台账没有」的仓
     系统永不对它止损（BTC/ETH/BNB 11-09 那三笔）。这类差异必须显式列出来，
     而不是让换口径时"顺带"吸收掉。

输出三张表：
  【1】现货层：信号目标 / 台账持有 / 账户实测 / 两个差
  【2】永续层：信号目标 / 台账净额 / 账户名义 / 两个差
  【3】结论：换口径会改动几个币、合计差多少 U；以及可作为 `ticket.py adopt` 依据的孤儿仓

用法：
    python binance_account.py snap      # 先拉一次真实快照
    python _diag_account_xcheck.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import binance_account as BA


def pad(s, n, right=True):
    s = str(s)
    return s.rjust(n) if right else s.ljust(n)


def load_signal():
    try:
        return json.load(open("_signal.json", encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None


def ledger_perp(items):
    """逐币永续净名义，从 done 的台账推（与 ticket.perp_net 同口径，只是拆到币）。

    不直接复用 `perp_net()`：它只返回全局汇总。这里要逐币，所以把同一段逻辑展开 ——
    **展开就得保证判据一模一样**：只认 done、只看 开仓/平仓、平仓方向看 pos_side。
    改 `perp_net` 的时候这里必须跟着改，否则两个"永续净额"会慢慢分叉。
    """
    out = {}
    for i in items:
        if i.get("status") != "done":
            continue
        if i.get("kind") not in ("开仓", "平仓", "止损临近", "即将到期"):
            continue
        amt = i.get("fill_notional")
        amt = abs(float(amt)) if amt is not None else abs(float(i.get("notional") or 0))
        if amt <= 0:
            continue
        c = i.get("coin")
        if i.get("kind") == "开仓":
            out[c] = out.get(c, 0.0) + (1 if i.get("side", 1) > 0 else -1) * amt
        else:
            ps = i.get("pos_side") or 0
            out[c] = out.get(c, 0.0) - (1 if ps >= 0 else -1) * amt
    return out


def main():
    sig = load_signal()
    if sig is None:
        print("读不到 _signal.json —— 先跑一次 run_signal.py")
        return 1
    snap = BA.read_snapshot(max_age_min=0)
    if snap is None:
        print("没有账户快照。先跑：python binance_account.py snap")
        return 1

    import live_signals as LS
    import ticket as TK

    rows = [o for o in sig["coins"] if not o.get("watch")]
    # 传给 spot_held 的必须是**生产代码原样的那个 dict**（含 DOWN 币的 base_held）——
    # 这个脚本的用处就是"和生产口径对账"，自己先改一遍基准就没有对照意义了。
    cur_tgt = {o["c"]: (o.get("base_held") or 0) * o.get("range_ratio", 1.0)
               for o in rows}
    held = LS.spot_held(cur_tgt, pending=False)
    est = set(getattr(LS, "_EST", set()) or ())

    def spot_target(o):
        """现货层该持有多少。**DOWN 是 0，不是 base_held。**

        `base_held` 在 DOWN 时不是目标，而是「翻下去之前的额度」——它的用途是回答
        「要清多少」，不是「该拿多少」。直接拿它当目标，会让 BTC/DOWN 显示
        「应持 398 U」，读起来像是要在下跌趋势里满仓。
        """
        if o.get("state") == "DOWN":
            return 0.0
        return (o.get("base_held") or 0) * o.get("range_ratio", 1.0)
    lperp = ledger_perp(TK.load()["items"])
    aspot = snap.get("spot") or {}
    aperp = (snap.get("perp") or {}).get("pos") or {}

    print(f"快照 {snap['ts']}（{snap.get('age_min', 0):.0f} 分钟前）　"
          f"权益 {snap['equity']:,.0f} U　信号 bar {sig.get('bar4h')}")
    print(f"账户 = 现货 {snap['spot_value']:,.0f} + 合约钱包 "
          f"{(snap.get('perp') or {}).get('wallet', 0):,.0f}"
          f" + 浮盈 {(snap.get('perp') or {}).get('upnl', 0):+,.0f}")

    # ---------------- 【1】现货层 ----------------
    print("\n【1】现货层（目标：UP/RANGE = base_held × range_ratio；**DOWN 目标为 0**）")
    print("-" * 104)
    print(pad("币", 8, False) + pad("状态", 8, False) + pad("目标", 9)
          + pad("台账", 9) + pad("推定", 6) + pad("账户实测", 11)
          + pad("台账−账户", 11) + pad("台账−目标", 11) + pad("账户−目标", 11))
    print("-" * 104)
    tot = {"tgt": 0.0, "led": 0.0, "acct": 0.0, "dl": 0.0}
    moved, orphan = [], []
    for o in rows:
        c = o["c"]
        t = spot_target(o)
        l = held.get(c, 0.0)
        a = float(aspot.get(c, {}).get("usd", 0.0))
        tot["tgt"] += t
        tot["led"] += l
        tot["acct"] += a
        if a > max(5.0, 0.05 * max(t, a)) and l <= 0 and t <= 0:
            orphan.append((c, a))
        flag = ""
        if abs(a - l) > max(20.0, 0.10 * max(a, l)):
            flag = " ←"
            moved.append((c, l, a))
        print(pad(c, 8, False) + pad(o.get("state", ""), 8, False)
              + pad(f"{t:,.0f}", 9) + pad(f"{l:,.0f}", 9)
              + pad("是" if c in est else "—", 6) + pad(f"{a:,.0f}", 11)
              + pad(f"{l - a:+,.0f}", 11) + pad(f"{l - t:+,.0f}", 11)
              + pad(f"{a - t:+,.0f}", 11) + flag)
    print("-" * 104)
    print(pad("合计", 8, False) + pad("", 8, False) + pad(f"{tot['tgt']:,.0f}", 9)
          + pad(f"{tot['led']:,.0f}", 9) + pad("", 6) + pad(f"{tot['acct']:,.0f}", 11)
          + pad(f"{tot['led'] - tot['acct']:+,.0f}", 11)
          + pad(f"{tot['led'] - tot['tgt']:+,.0f}", 11)
          + pad(f"{tot['acct'] - tot['tgt']:+,.0f}", 11))

    # ---------------- 【2】永续层 ----------------
    print("\n【2】永续层（目标 = target − 现货目标）")
    print("-" * 104)
    print(pad("币", 8, False) + pad("目标", 10) + pad("台账净额", 11)
          + pad("账户名义", 11) + pad("台账−账户", 11) + pad("账户−目标", 11)
          + "  说明")
    print("-" * 104)
    coins = sorted(set(list(lperp.keys()) + list(aperp.keys())))
    bad = []
    for c in coins:
        o = next((x for x in rows if x["c"] == c), None)
        t = 0.0
        if o:
            # 永续目标 = 峰值 − 现货那层。DOWN 时现货层是 0，所以整个 target
            # （负数=空头）都归永续；UP 时只剩加仓层。
            t = float(o.get("target") or 0) - spot_target(o)
        l = lperp.get(c, 0.0)
        a = (aperp.get(c) or {}).get("notional", 0.0)
        note = ""
        if abs(a) < 1 and abs(l) > 0.5:
            note = "台账有仓、账户没有 → 没执行/已手动平"
        elif abs(l) < 0.5 and abs(a) > 0.5:
            note = "账户有仓、台账没有 → has_pos=False，永不发平仓/止损"
        elif abs(a - l) > max(20.0, 0.10 * max(abs(a), abs(l))):
            note = "两边都在但对不上"
        if note and "台账没有" in note:
            bad.append((c, a))
        print(pad(c, 8, False) + pad(f"{t:+,.0f}", 10) + pad(f"{l:+,.0f}", 11)
              + pad(f"{a:+,.0f}", 11) + pad(f"{l - a:+,.0f}", 11)
              + pad(f"{a - t:+,.0f}", 11) + "  " + note)
    if not coins:
        print("  两边都没有永续持仓")
    print("-" * 104)

    # ---------------- 【3】结论 ----------------
    print("\n【3】结论")
    print("-" * 104)
    if moved:
        print(f"  现货口径若切到账户实测，会改动 {len(moved)} 个币"
              f"（阈值 max(20U, 10%)）：")
        for c, l, a in moved:
            print(f"    {c:6} 台账 {l:>8,.0f} → 账户 {a:>8,.0f}　"
                  f"（{a - l:+,.0f} U，{(a / l - 1) * 100:+.0f}%）" if l else
                  f"    {c:6} 台账 {l:>8,.0f} → 账户 {a:>8,.0f}　（新增）")
    else:
        print("  现货两层口径一致（差都在 max(20U, 10%) 以内）→ "
              "可以放心把 spot_held 切到实测，数字不会跳。")
    print(f"  现货合计：台账 {tot['led']:,.0f} vs 账户 {tot['acct']:,.0f}"
          f"（差 {tot['led'] - tot['acct']:+,.0f} U）")
    if orphan:
        print("  账户有现货、台账与目标都没有（疑似漏记或手动买入）：")
        for c, a in orphan:
            print(f"    {c:6} {a:,.0f} U　→ python ticket.py adopt {c} {a:.0f} 多")
    if bad:
        print("  ⚠ 账户有永续、台账没有 —— 这些仓系统不会止损：")
        for c, a in bad:
            print(f"    {c:6} {a:+,.0f} U　→ python ticket.py adopt {c} {abs(a):.0f} "
                  f"{'多' if a > 0 else '空'}")
    print("\n  本脚本只读。切换口径前请把上面的差异逐条确认，再改 live_signals。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
