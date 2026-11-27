"""4H 加仓层的同时在场分布 —— 回答「满配 1,190 U 是理论峰值，还是会经常撞到」。

背景（2026-11-27）：用户问「底仓 1,714 U 吃满之后，永续加仓层的保证金还够不够」。
「够不够」不能拿满配峰值 1,190 U 去比 —— 满配要求 11 个币**同时**突破。
真正要问的是：历史上同时有几个币在加仓层里、名义合计多少。

做法：对每个币跑同一套状态机（regime → BT.signals），拿 sig 序列（4H 方向），
按**当前** base_full × ADD_RATIO 折算成今天的名义（不复现历史 scale，因为问的是
「今天这个账本，历史上这种局面要多少钱」）。然后逐 bar 求同时在场数与名义合计。

只读，不写台账。
"""
import json
import statistics as st
from datetime import datetime, timezone

import live_signals as LS
import portfolio_config as PC
import bt_skeleton as BT


def main():
    sig = json.load(open("_signal.json", encoding="utf-8"))
    base = {}
    for o in sig["coins"]:
        if o.get("watch"):
            continue
        base[o["c"]] = o.get("base_full") or 0.0
    coins = [c for c in base]

    print(f"本金 {PC.CAPITAL:,.0f}　组合 {len(coins)} 币　ADD_RATIO {PC.ADD_RATIO}")
    print(f"加仓层满配名义 Σbase×ADD = {sum(base.values()) * PC.ADD_RATIO:,.0f} U")
    print()

    series = {}
    now = datetime.now(timezone.utc)
    for c in coins:
        b4, d1 = LS.load_coin(c, now)
        reg, blood = LS.regime_and_blood(d1)
        rg4 = LS.align_regime_safe(b4, reg)
        blood4 = LS.align_regime_safe(b4, blood)
        atr = LS.atr_wilder(b4, 14)
        sg, tr = BT.signals(b4, rg4, atr, vol_q=LS.VOL_Q, coin=c, blood4=blood4,
                            allow_short=PC.ALLOW_SHORT)
        series[c] = {b["ts"]: s for b, s in zip(b4, sg)}
        nz = sum(1 for s in sg if s)
        print(f"  {c:<5} 4H n={len(b4):<6} 起点 {b4[0]['date']}  "
              f"加仓层在场 {nz / len(sg) * 100:5.1f}%  "
              f"单币上限 {base[c] * PC.ADD_RATIO:6.0f} U")

    # 取所有币都有的 bar 交集，避免早期品种不全把「在场数」压低
    common = set.intersection(*[set(s) for s in series.values()])
    common = sorted(common)
    print(f"\n共同区间 {len(common)} 根 4H")

    rows = []
    for ts in common:
        pos = {c: series[c][ts] for c in coins}
        cnt = sum(1 for v in pos.values() if v)
        gross = sum(base[c] * PC.ADD_RATIO * abs(pos[c]) for c in coins)
        net = sum(base[c] * PC.ADD_RATIO * pos[c] for c in coins)
        rows.append((ts, cnt, gross, net))

    def pct(vals, q):
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * q))]

    cnts = [r[1] for r in rows]
    gross = [r[2] for r in rows]
    nets = [abs(r[3]) for r in rows]
    print("\n=== 同时在场币数 ===")
    print(f"  均值 {st.mean(cnts):.2f}  中位 {st.median(cnts):.0f}  "
          f"P90 {pct(cnts, .90)}  P99 {pct(cnts, .99)}  最大 {max(cnts)}")
    hist = {}
    for v in cnts:
        hist[v] = hist.get(v, 0) + 1
    for k in sorted(hist):
        print(f"  {k} 个在场 {hist[k] / len(cnts) * 100:5.1f}%")

    print("\n=== 加仓层毛名义（U）===")
    print(f"  均值 {st.mean(gross):7.0f}  中位 {st.median(gross):7.0f}  "
          f"P90 {pct(gross, .90):7.0f}  P95 {pct(gross, .95):7.0f}  "
          f"P99 {pct(gross, .99):7.0f}  最大 {max(gross):7.0f}")
    print("\n=== 加仓层净名义（|多−空|，U）===")
    print(f"  均值 {st.mean(nets):7.0f}  中位 {st.median(nets):7.0f}  "
          f"P90 {pct(nets, .90):7.0f}  P95 {pct(nets, .95):7.0f}  "
          f"P99 {pct(nets, .99):7.0f}  最大 {max(nets):7.0f}")

    print("\n=== 各杠杆下所需保证金（按毛名义，取 P95 与最大）===")
    g95, gmax, gmean = pct(gross, .95), max(gross), st.mean(gross)
    for lev in (2, 3, 5, 10):
        print(f"  {lev:2d}×  均值 {gmean / lev:6.0f}  P95 {g95 / lev:6.0f}  "
              f"最大 {gmax / lev:6.0f}")


if __name__ == "__main__":
    main()
