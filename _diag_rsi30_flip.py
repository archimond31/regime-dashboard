"""BTC/ETH/BNB「日线 RSI 回到 30 以上」的后果推演。

背景（2026-11-27）：用户问「明天这三币日线 RSI 回到 30 以上会触发什么信号」。

关键事实：三币的 RSI 已经连续 14~17 天 ≤ 30，`os_confirmed` 早就成立
（daily_states 里只需连续 confirm=2 日）。所以**不需要再破一次 30** ——
只要某天收盘 RSI > 30，立刻走 `rsi_oversold_exit`，DOWN → RANGE。

本脚本做三件事：
  1. 反解「RSI 刚好 = 30」对应的收盘价（二分，Wilder 递推是单调的）
  2. 追加一根假设日线，重跑整台状态机，看实际切换成什么
  3. 按 portfolio_config 算切换后的 target / 建底仓金额

只读，不写任何文件。
"""
import json
from datetime import datetime, timezone

import live_signals as LS
import portfolio_config as PC
from visualize_btc_regime import rsi_wilder, sma, daily_states
from strategy_1h_rsi import WARM_CROSS


def rsi_with_extra(closes, extra_close):
    """在 closes 尾部追加一个收盘价后的 RSI(14)。"""
    return rsi_wilder(list(closes) + [extra_close])[-1]


def solve_close_for_rsi(closes, target_rsi=30.0, lo_pct=-10.0, hi_pct=25.0):
    """二分找使 RSI(14) 刚好等于 target_rsi 的收盘价。RSI 对收盘价单调递增。"""
    base = closes[-1]
    lo, hi = base * (1 + lo_pct / 100), base * (1 + hi_pct / 100)
    if rsi_with_extra(closes, hi) < target_rsi:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if rsi_with_extra(closes, mid) < target_rsi:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main():
    now = datetime.now(timezone.utc)
    out = {}
    for c in ("BTC", "ETH", "BNB"):
        b4, d1 = LS.load_coin(c, now)
        closes = [r["close"] for r in d1]
        px = closes[-1]
        m60, m120 = sma(closes, 60)[-1], sma(closes, 120)[-1]
        rsi_now = rsi_wilder(closes)[-1]

        need = solve_close_for_rsi(closes, 30.0)
        st, _ma, tg = daily_states(closes, 120, 70, 30, True, 2, True,
                                   warm_cross=WARM_CROSS)
        st_now = st[-1]
        print("=" * 66)
        print(f"{c}  日线 {d1[-1]['date']}  收盘 {px:,.2f}  RSI {rsi_now:.1f}  状态 {st_now}")
        print(f"  MA60 {m60:,.2f}（距 {(m60 / px - 1) * 100:+.1f}%）　"
              f"MA120 {m120:,.2f}（距 {(m120 / px - 1) * 100:+.1f}%）")
        if need is None:
            print("  即使大涨 25% 也到不了 RSI 30 —— 不成立")
            continue
        print(f"  → 收盘价 {need:,.2f}（{(need / px - 1) * 100:+.2f}%）时 RSI=30")

        # 逐档推演：追加一根 bar 后重跑状态机
        cand = []
        for pct in (-2, 0, 1, 2, 3, 4, 5, 6, 8, 10, 15, 20):
            cpx = px * (1 + pct / 100)
            cc = closes + [cpx]
            st2, _, tg2 = daily_states(cc, 120, 70, 30, True, 2, True,
                                       warm_cross=WARM_CROSS)
            r2 = rsi_wilder(cc)[-1]
            cand.append((pct, cpx, r2, st2[-1], tg2[-1]))
        print(f"  {'涨幅':>6}{'收盘':>11}{'RSI':>7}  状态        触发")
        for pct, cpx, r2, s2, t2 in cand:
            print(f"  {pct:+5.1f}%{cpx:11,.2f}{r2:7.1f}  {s2:<10}  {t2 or '—'}")

        # 翻 RANGE 后按 portfolio_config 算底仓
        sig = json.load(open("_signal.json", encoding="utf-8"))
        o = [x for x in sig["coins"] if x["c"] == c][0]
        base = o["base_full"]
        rr = PC.RANGE_RATIO_BY_SRC.get("DOWN", PC.RANGE_RATIO_DEFAULT)
        print(f"  → 若翻 RANGE（来路 DOWN）：range_ratio={rr}  "
              f"底仓 {base}×{rr} = {base * rr:,.0f} U（原 UP 满配 {base} U）")
        out[c] = dict(px=px, need=need, base=base, tgt_range=base * rr)

    print("\n" + "=" * 66)
    print("三币同时翻 RANGE 的建底仓合计：",
          f"{sum(v['tgt_range'] for v in out.values()):,.0f} U")
    print("（若各自 4H 再突破，target 会再各加一层加仓层，"
          "峰值回到 base×1.0）")


if __name__ == "__main__":
    main()
