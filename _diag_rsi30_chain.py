"""「明天三币收盘涨 1%」的完整信号链推演（状态 → 工单 → 金额 → 资金）。

上一版 _diag_rsi30_flip.py 只跑到状态切换。这里把下游也跑完：
把一根假设日线注入 load_coin，然后走 live_signals.analyze 的完整链路，
拿到 action / target / base_held，再折成建底仓金额。

结论方向（2026-11-27 实测）：BTC 涨 1% 就翻 **UP**（走 divergence_bottom，
因为超卖摆动结束时价格创新低而 RSI 未创新低），ETH/BNB 翻 **RANGE**
（走 rsi_oversold_exit）。三者底仓比例不同 —— BTC 全额、ETH/BNB 打五折。

只读，不写任何文件。
"""
import json
from datetime import datetime, timezone

import live_signals as LS
import portfolio_config as PC


def make_patch(extra_pct):
    """返回一个 load_coin 替身：在日线尾部追加一根 close=昨收×(1+pct) 的 bar。"""
    orig = LS.load_coin

    def patched(c, now):
        b4, d1 = orig(c, now)
        last = d1[-1]
        px = last["close"] * (1 + extra_pct / 100)
        # 用最后一根的日期 +1 天；日线只用到 date/close，其余字段不参与状态机
        from datetime import date, timedelta
        d = date.fromisoformat(last["date"]) + timedelta(days=1)
        fake = dict(last)
        fake["date"] = d.isoformat()
        fake["close"] = px
        fake["open"] = last["close"]
        fake["high"] = max(px, last["close"])
        fake["low"] = min(px, last["close"])
        return b4, d1 + [fake]

    return patched


def main():
    sig = json.load(open("_signal.json", encoding="utf-8"))
    now = datetime.now(timezone.utc)
    orig = LS.load_coin
    total = 0
    rows = []
    try:
        LS.load_coin = make_patch(1.0)
        for c in ("BTC", "ETH", "BNB", "SOL", "XRP", "HYPE", "LINK", "UNI",
                  "AAVE", "ZEC", "NEAR"):
            o = LS.analyze(c, now)
            base = o.get("base_full") or 0
            tgt = o.get("target") or 0
            prev = [x for x in sig["coins"] if x["c"] == c][0]
            held = prev.get("target") or 0
            rows.append((c, prev["state"], o["state"], o.get("blood"),
                         o.get("action"), tgt, base, tgt - held))
    finally:
        LS.load_coin = orig

    print(f"{'币':<5}{'今状态':<8}{'明状态':<8}{'血统':<20}{'动作':<9}{'target':>9}{'底仓基准':>9}{'变化':>9}")
    for c, s0, s1, bl, act, tgt, base, d in rows:
        mark = "  ← 翻" if s0 != s1 else ""
        print(f"{c:<5}{s0:<8}{s1:<8}{(bl or '—'):<20}{act:<9}{tgt:9.0f}{base:9.0f}{d:+9.0f}{mark}")

    print(f"\n三币（BTC/ETH/BNB）target 增量合计："
          f"{sum(d for c, _, _, _, _, _, _, d in rows if c in ('BTC', 'ETH', 'BNB')):,.0f} U")

    # 资金对账
    spot = sum(sig.get("spot_held", {}).values())
    cap = PC.CAPITAL
    d_btc = [x for x in rows if x[0] == "BTC"][0]
    d_eth = [x for x in rows if x[0] == "ETH"][0]
    d_bnb = [x for x in rows if x[0] == "BNB"][0]
    need = d_btc[7] + d_eth[7] + d_bnb[7]
    print(f"\n现有现货 {spot:,.0f} U　本金 {cap:,.0f} U　"
          f"可用现金 {cap - spot:,.0f} U　需买入 {need:,.0f} U　"
          f"余 {cap - spot - need:+,.0f} U")

    # 4H 层下一次开仓需要突破的价格
    print("\n4H 层（突破前 20 根高点才开多）：")
    for c in ("BTC", "ETH", "BNB"):
        o = [x for x in sig["coins"] if x["c"] == c][0]
        print(f"  {c:<5} hi20 {o['hi20']:>10,.2f}  现价 {o['px']:>10,.2f}  "
              f"需涨 {o['dist_up']:+.2f}%　峰值 {o['base_full']:,.0f} U")


if __name__ == "__main__":
    main()
