#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_multi_tf.py —— 多周期（周线 × 日线）共振「风险闸门」验证

回答一个问题：**日线与周线方向相反时，逆势方向的后续收益是否确实更差？**

如果更差 → 闸门有价值（拦掉的确实是坏单）；如果没有差异 → 共振没有信息量，砍掉。

三层验证：
  1) 事件研究：全样本九宫格 × 前瞻 5/10/20 日收益（中位数、胜率、样本数）
  2) 显著性检验：**非重叠子样本**上的 Mann-Whitney U（重叠样本自相关会夸大显著性，
     所以主分析用全样本做描述，显著性用每 h 日取一点的近似独立子样本）
  3) 分段一致性：按半年切 6 段，看「冲突格劣于对角格」这个符号在几段内成立

另附策略口径回测（闸门 vs 纯日线，同一持有规则）：收益、回撤、交易数、胜率、盈亏比。

铁律：
  - 周线参数 = 日线的 ÷5 机械映射（MA24/MA12），自由度为 0，不参与任何搜索
  - 周线状态严格只用「第 i 天已经走完的周线」，无未来函数
  - 全 18 品种全量列出，不做品种挑选
仅用 Python 标准库。
"""
import bisect
import json
import os
import statistics
from datetime import datetime, timezone
from math import erf, sqrt

from visualize_btc_regime import daily_states, to_weekly
from daily_regime_alert import (
    ASSETS, GROUP_ORDER, STATE_CN, WARM_START,
    fetch_hyperliquid_daily, fetch_ashare_daily, drop_unclosed, drop_unclosed_cn,
)
from visualize_eth_gold_regime import fetch_yahoo_daily

BT_START = "2021-07-01"      # 判定起点：保证 2021-01 起的数据有 120 根热身
WEEKLY_MA_SLOW, WEEKLY_MA_FAST = 24, 12   # 120//5, 60//5
HORIZONS = (5, 10, 20)
OB, OS, CONFIRM = 70, 30, 2   # 与实盘完全一致

GRID = [
    ("UP", "UP"), ("UP", "RANGE"), ("UP", "DOWN"),
    ("RANGE", "UP"), ("RANGE", "RANGE"), ("RANGE", "DOWN"),
    ("DOWN", "UP"), ("DOWN", "RANGE"), ("DOWN", "DOWN"),
]
# key = (日线状态, 周线状态)。命名统一为「日X周Y」，避免读反。
GRID_CN = {
    ("UP", "UP"): "日↑周↑ 共振多",
    ("UP", "RANGE"): "日↑周— 试探",
    ("UP", "DOWN"): "日↑周↓ 冲突",
    ("RANGE", "UP"): "日—周↑ 回调",
    ("RANGE", "RANGE"): "日—周— 无向",
    ("RANGE", "DOWN"): "日—周↓ 反弹",
    ("DOWN", "UP"): "日↓周↑ 冲突",
    ("DOWN", "RANGE"): "日↓周— 试探",
    ("DOWN", "DOWN"): "日↓周↓ 共振空",
}


def dir_adj(vals, daily_state):
    """按信号方向调整收益：日线 UP 做多取原始收益，DOWN 做空取相反数，RANGE 空仓为 0。

    这一步是解读的关键——否则「日↓周↓ 中位 -0.03%」会被读成没机会，
    实际上对空头信号它是 +0.03% 的正收益。"""
    if daily_state == "UP":
        return list(vals)
    if daily_state == "DOWN":
        return [-v for v in vals]
    return [0.0 for _ in vals]


# ---------------- 取数 ----------------
def fetch_asset(a, warm_dt):
    if a["source"] == "hyperliquid":
        return fetch_hyperliquid_daily(a["symbol"])
    if a["source"] == "ashare":
        return fetch_ashare_daily(a)
    return fetch_yahoo_daily(a["symbol"], warm_dt)


# ---------------- 状态序列（含周线对齐） ----------------
def build_states(rows, start=BT_START, is_cn=False):
    """返回 (dates, closes, 日线状态序列, 对齐到日线的周线状态序列, 日线起点)。

    周线对齐规则：第 i 天取「最后一根 date <= dates[i] 的周线」的状态。
    周线 bar 的 date 是该周最后一个交易日，所以该状态在该日收盘即已知，无未来函数。
    """
    rows = drop_unclosed_cn(rows) if is_cn else drop_unclosed(rows)
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    n = len(closes)

    s0d = bisect.bisect_left(dates, start)
    if s0d < 120:
        s0d = 120
    if s0d >= n - 25:
        return None
    st_d, _div, _tg = daily_states(closes, s0d, OB, OS, True, CONFIRM, True)

    wrows = to_weekly(rows)
    if len(wrows) < WEEKLY_MA_SLOW + 10:
        return dates, closes, st_d, [None] * n, s0d
    wd = [r["date"] for r in wrows]
    wc = [r["close"] for r in wrows]
    s0w = bisect.bisect_left(wd, start)
    if s0w < WEEKLY_MA_SLOW:
        s0w = WEEKLY_MA_SLOW
    if s0w >= len(wc):
        return dates, closes, st_d, [None] * n, s0d
    st_w, _dw, _tw = daily_states(wc, s0w, OB, OS, True, CONFIRM, True,
                                  ma_slow=WEEKLY_MA_SLOW, ma_fast=WEEKLY_MA_FAST)

    w_aligned = [None] * n
    for i, d in enumerate(dates):
        k = bisect.bisect_right(wd, d) - 1
        if k >= 0:
            w_aligned[i] = st_w[k]
    return dates, closes, st_d, w_aligned, s0d


# ---------------- 事件研究 ----------------
def event_study(dates, closes, st_d, st_w, s0):
    """九宫格 × 前瞻收益。每格保存 [(i, ret), ...] 以便做非重叠抽样。"""
    n = len(closes)
    buckets = {k: {h: [] for h in HORIZONS} for k in GRID}
    for i in range(s0, n):
        ds, ws = st_d[i], st_w[i]
        if ds is None or ws is None:
            continue
        key = (ds, ws)
        if key not in buckets:
            continue
        for h in HORIZONS:
            if i + h < n:
                buckets[key][h].append((i, closes[i + h] / closes[i] - 1))
    return buckets


def non_overlap(samples, gap):
    """贪心抽取近似独立子样本：相邻样本间隔 >= gap 天。"""
    out, last = [], -10 ** 9
    for i, r in samples:
        if i - last >= gap:
            out.append(r)
            last = i
    return out


def mann_whitney_p(a, b):
    """Mann-Whitney U 双尾 p（正态近似 + 并列平均秩）。纯标准库。"""
    if len(a) < 5 or len(b) < 5:
        return 1.0
    combined = sorted([(v, 0) for v in a] + [(v, 1) for v in b], key=lambda x: x[0])
    m = len(combined)
    ranks = [0.0] * m
    i = 0
    while i < m:
        j = i
        while j + 1 < m and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    n1, n2 = len(a), len(b)
    r1 = sum(ranks[k] for k in range(m) if combined[k][1] == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    sigma = sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    if sigma == 0:
        return 1.0
    z = (u1 - mu) / sigma
    return 2 * (1 - 0.5 * (1 + erf(abs(z) / sqrt(2))))


def compare_group(samples_a, samples_b, h, label_a, label_b, sign=1):
    """对比两个格子的前瞻收益：中位数差 + 非重叠子样本 p 值。

    sign=-1 用于空头格（日线 DOWN）：把原始收益取反，变成「按信号做空」的收益，
    这样两格的差值方向才可直接解读（正=冲突格更差）。Mann-Whitney 的双尾 p 不受符号翻转影响。"""
    ra = [sign * r for _i, r in samples_a]
    rb = [sign * r for _i, r in samples_b]
    if not ra or not rb:
        return None
    sa = [sign * r for r in non_overlap(samples_a, h)]
    sb = [sign * r for r in non_overlap(samples_b, h)]
    p = mann_whitney_p(sa, sb)
    return {
        "a": label_a, "b": label_b,
        "n_a": len(ra), "n_b": len(rb),
        "med_a": statistics.median(ra), "med_b": statistics.median(rb),
        "diff": statistics.median(ra) - statistics.median(rb),
        "n_nov_a": len(sa), "n_nov_b": len(sb),
        "p": p,
    }


def frac_positive(vals):
    return sum(1 for v in vals if v > 0) / len(vals) if vals else 0.0


# ---------------- 策略口径回测 ----------------
def make_positions(st_d, st_w, gate_on):
    """闸门口径仓位：日线定方向，周线反向时归零；日线 RANGE 空仓。"""
    pos = []
    for ds, ws in zip(st_d, st_w):
        p = 1 if ds == "UP" else (-1 if ds == "DOWN" else 0)
        if gate_on and p != 0 and ws in ("UP", "DOWN"):
            if (p == 1 and ws == "DOWN") or (p == -1 and ws == "UP"):
                p = 0
        pos.append(p)
    return pos


def perf(pos, closes, dates, s0):
    """按「收盘后定仓、次日收盘计息」口径计算绩效。"""
    eq, rets, curve = 1.0, [], [1.0]
    for i in range(s0 + 1, len(closes)):
        r = pos[i - 1] * (closes[i] / closes[i - 1] - 1)
        rets.append(r)
        eq *= (1 + r)
        curve.append(eq)
    if not rets:
        return None
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    trades = []
    cur, entry = 0, None
    for i in range(s0, len(pos)):
        if pos[i] != cur:
            if cur != 0 and entry is not None:
                trades.append({"dir": cur, "days": i - entry,
                               "ret": cur * (closes[i] / closes[entry] - 1)})
            cur, entry = pos[i], i
    if cur != 0 and entry is not None:
        trades.append({"dir": cur, "days": len(pos) - 1 - entry,
                       "ret": cur * (closes[-1] / closes[entry] - 1)})
    wins = [t["ret"] for t in trades if t["ret"] > 0]
    losses = [t["ret"] for t in trades if t["ret"] <= 0]
    days = len(rets)
    ann = eq ** (252.0 / days) - 1 if days else 0.0
    return {
        "total": eq - 1, "ann": ann, "mdd": mdd, "n_trade": len(trades),
        "win": len(wins) / len(trades) if trades else 0.0,
        "pl": (statistics.mean(wins) / abs(statistics.mean(losses)))
              if wins and losses else None,
        "expo": sum(1 for p in pos[s0:] if p != 0) / max(len(pos) - s0, 1),
        "flips": sum(1 for i in range(s0 + 1, len(pos)) if pos[i] != pos[i - 1]),
        "avg_days": statistics.mean([t["days"] for t in trades]) if trades else 0.0,
    }


def half_year_segments(dates, s0):
    """按 YYYY-H1/H2 分段，返回 [(标签, 起索引, 止索引), ...]。"""
    segs, cur_key, start_i = [], None, s0
    for i in range(s0, len(dates)):
        y, m = dates[i][:4], int(dates[i][5:7])
        key = f"{y}H{1 if m <= 6 else 2}"
        if cur_key is None:
            cur_key = key
        elif key != cur_key:
            segs.append((cur_key, start_i, i))
            cur_key, start_i = key, i
    segs.append((cur_key, start_i, len(dates)))
    return segs


# ---------------- 主流程 ----------------
def main():
    warm_dt = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    results = {}
    rows_cache = {}

    for a in ASSETS:
        rows = fetch_asset(a, warm_dt)
        rows_cache[a["key"]] = rows
        built = build_states(rows, is_cn=(a["source"] == "ashare"))
        if built is None:
            print(f"[{a['key']:5}] 数据不足，跳过")
            continue
        dates, closes, st_d, st_w, s0 = built
        last = len(closes) - 1
        ds, ws = st_d[last], st_w[last]
        if ds is None:
            print(f"[{a['key']:5}] 日线状态为空，跳过")
            continue
        ev = event_study(dates, closes, st_d, st_w, s0)
        pos_g = make_positions(st_d, st_w, True)
        pos_d = make_positions(st_d, st_w, False)
        results[a["key"]] = {
            "label": a["label"], "group": a["group"], "unit": a.get("unit", ""),
            "dates": dates, "closes": closes, "st_d": st_d, "st_w": st_w, "s0": s0,
            "ev": ev, "pos_gate": pos_g, "pos_base": pos_d,
            "perf_gate": perf(pos_g, closes, dates, s0),
            "perf_base": perf(pos_d, closes, dates, s0),
            "last": (ds, ws),
            "n_days": last - s0,
            "start": dates[s0], "end": dates[last],
        }
        pg, pb = results[a["key"]]["perf_gate"], results[a["key"]]["perf_base"]
        print(f"[{a['key']:5}] 样本 {last - s0:5} 日（{dates[s0]}~{dates[last]}）"
              f" | 闸门 {pg['total']*100:7.1f}% vs 纯日线 {pb['total']*100:7.1f}%"
              f" | 交易 {pg['n_trade']:3}/{pb['n_trade']:3}")

    if not results:
        print("无可用样本")
        return

    # ---- 汇总事件研究（按格加权合并所有品种） ----
    pooled = {k: {h: [] for h in HORIZONS} for k in GRID}
    for key, r in results.items():
        for k in GRID:
            for h in HORIZONS:
                pooled[k][h].extend(r["ev"][k][h])

    print("\n" + "=" * 110)
    print("全样本事件研究：九宫格 × 前瞻收益（18 品种合并）")
    print("  原始 = 价格涨跌；方向调整 = 按日线信号交易的实际收益（UP 做多、DOWN 做空、RANGE 空仓）")
    print("=" * 110)
    print(f"{'格子':<16} {'n':>6} | {'5日原始':>9} | {'20日原始':>9} {'20日胜率':>8} | "
          f"{'20日方向调整':>12} {'调整后胜率':>10}")
    print("-" * 110)
    for k in GRID:
        ns = len(pooled[k][HORIZONS[0]])
        v5 = [r for _i, r in pooled[k][5]]
        v20 = [r for _i, r in pooled[k][20]]
        adj = dir_adj(v20, k[0])
        if v20:
            print(f"{GRID_CN[k]:<16} {ns:>6} | {statistics.median(v5)*100:>8.2f}% | "
                  f"{statistics.median(v20)*100:>8.2f}% {frac_positive(v20)*100:>7.1f}% | "
                  f"{statistics.median(adj)*100:>11.2f}% {frac_positive(adj)*100:>9.1f}%")
        else:
            print(f"{GRID_CN[k]:<16} {ns:>6} |            — |         —        — |            —          —")

    # ---- 关键对比：冲突格 vs 对角格 ----
    print("\n" + "=" * 110)
    print("关键对比：冲突格 vs 同向格 —— 周线唱反调时，按日线信号做的收益是否更差？")
    print("=" * 110)
    cmp_rows = []
    for h in (10, 20):
        print(f"\n  前瞻 {h} 日（方向调整后收益，正数=冲突格更差，即闸门有价值）：")
        print(f"    {'对比':<40} {'冲突':>8} {'同向':>8} {'差':>9} {'p值':>7} {'非重叠n':>11}  结论")
        print("    " + "-" * 96)
        for ga, gb, sign in ((("UP", "DOWN"), ("UP", "UP"), 1),
                             (("DOWN", "UP"), ("DOWN", "DOWN"), -1)):
            c = compare_group(pooled[ga][h], pooled[gb][h], h,
                              GRID_CN[ga], GRID_CN[gb], sign=sign)
            if not c:
                continue
            cmp_rows.append({"h": h, **c})
            sig = "显著" if c["p"] < 0.05 else ("边际" if c["p"] < 0.10 else "不显著")
            desc = f"{GRID_CN[ga]} − {GRID_CN[gb]}"
            print(f"    {desc:<40} {c['med_a']*100:>7.2f}% {c['med_b']*100:>7.2f}% "
                  f"{c['diff']*100:>+8.2f}% {c['p']:>7.3f} "
                  f"{c['n_nov_a']:>4}/{c['n_nov_b']:<6}  {sig}")

    # ---- 分段一致性 ----
    print("\n" + "=" * 104)
    print("分段一致性（半年一段，全品种合并）：冲突格 20 日前瞻中位数 − 对角格")
    print("=" * 104)
    seg_buckets = {}
    for key, r in results.items():
        dates, closes, s0 = r["dates"], r["closes"], r["s0"]
        for label, i0, i1 in half_year_segments(dates, s0):
            b = seg_buckets.setdefault(label, {"cu": [], "du": [], "cd": [], "dd": []})
            for i in range(i0, min(i1, len(closes) - 20)):
                ds, ws = r["st_d"][i], r["st_w"][i]
                if ds is None or ws is None:
                    continue
                ret = closes[i + 20] / closes[i] - 1
                if ds == "UP" and ws == "DOWN":
                    b["cu"].append(ret)
                elif ds == "UP" and ws == "UP":
                    b["du"].append(ret)
                elif ds == "DOWN" and ws == "UP":
                    b["cd"].append(ret)
                elif ds == "DOWN" and ws == "DOWN":
                    b["dd"].append(ret)

    print(f"  {'区间':<9} | {'多头侧(做多收益)':^27} | {'空头侧(做空收益)':^27}")
    print(f"  {'':<9} | {'冲突':>8} {'共振':>8} {'差':>8} | {'冲突':>8} {'共振':>8} {'差':>8}")
    print("  " + "-" * 92)
    ok_up = ok_dn = tot = 0
    seg_rows = []
    for label in sorted(seg_buckets):
        b = seg_buckets[label]
        if len(b["cu"]) < 20 or len(b["du"]) < 20:
            continue
        tot += 1
        m_cu, m_du = statistics.median(b["cu"]), statistics.median(b["du"])
        d1 = m_cu - m_du
        ok_up += 1 if d1 < 0 else 0
        m_cd = m_dd = d2 = None
        if len(b["cd"]) >= 20 and len(b["dd"]) >= 20:
            m_cd, m_dd = statistics.median(b["cd"]), statistics.median(b["dd"])
            d2 = -(m_cd - m_dd)          # 空头侧取反，变成做空收益差
            ok_dn += 1 if d2 < 0 else 0
            dn = f"{-m_cd*100:>7.2f}% {-m_dd*100:>7.2f}% {d2*100:>+7.2f}%"
        else:
            dn = "       —        —        —"
        seg_rows.append({"label": label, "m_cu": m_cu, "m_du": m_du, "d1": d1,
                         "m_cd": m_cd, "m_dd": m_dd, "d2": d2})
        print(f"  {label:<9} | {m_cu*100:>7.2f}% {m_du*100:>7.2f}% {d1*100:>+7.2f}% | {dn}")
    print("  " + "-" * 92)
    print(f"  「冲突格更差」成立的段数：多头侧 {ok_up}/{tot}，空头侧 {ok_dn}/{tot}")
    print("  读法：差为负 = 周线唱反调时按日线信号做的收益更差 = 闸门有价值。")

    # ---- 策略口径对比 ----
    print("\n" + "=" * 104)
    print("策略口径：闸门 vs 纯日线（同持有规则，仅周线反向归零）")
    print("=" * 104)
    print(f"{'品种':<16} {'闸门收益':>9} {'纯日线':>9} {'增量':>8} {'闸门MDD':>9} "
          f"{'纯MDD':>8} {'交易(闸/日)':>12} {'闸门胜率':>9}")
    print("-" * 104)
    wins = 0
    for key, r in results.items():
        pg, pb = r["perf_gate"], r["perf_base"]
        d = (pg["total"] - pb["total"]) * 100
        wins += 1 if d > 0 else 0
        print(f"{r['label'][:15]:<16} {pg['total']*100:>8.1f}% {pb['total']*100:>8.1f}% "
              f"{d:>+7.1f}% {pg['mdd']*100:>8.1f}% {pb['mdd']*100:>7.1f}% "
              f"{pg['n_trade']:>5}/{pb['n_trade']:<6} {pg['win']*100:>8.1f}%")
    print("-" * 104)
    avg_g = statistics.mean([r["perf_gate"]["total"] for r in results.values()])
    avg_b = statistics.mean([r["perf_base"]["total"] for r in results.values()])
    print(f"{'等权平均':<16} {avg_g*100:>8.1f}% {avg_b*100:>8.1f}% "
          f"{(avg_g-avg_b)*100:>+7.1f}%")
    print(f"\n闸门跑赢品种数：{wins}/{len(results)}（掷硬币期望 {len(results)/2:.0f}）")

    # ---- HTML 报告 ----
    def pct(v, d=2, sign=False):
        if v is None:
            return "—"
        return (f"{v*100:+.{d}f}%" if sign else f"{v*100:.{d}f}%")

    sig20 = [c for c in cmp_rows if c["h"] == 20]
    agree_rate = (ok_up + ok_dn) / (2 * tot) if tot else 0
    if any(c["p"] < 0.05 for c in sig20) and agree_rate > 0.7:
        verdict, vcolor = "闸门通过：冲突格的逆势收益显著更差", "#1a7f37"
    elif agree_rate >= 0.6:
        verdict, vcolor = "方向大致对但不显著：不足以支撑接入信号", "#b8860b"
    else:
        verdict, vcolor = "闸门假设被证伪：冲突格并无稳定的逆势劣势", "#c00"

    grid_rows = ""
    for k in GRID:
        v5 = [r for _i, r in pooled[k][5]]
        v20 = [r for _i, r in pooled[k][20]]
        adj = dir_adj(v20, k[0])
        is_conf = (k[0] == "UP" and k[1] == "DOWN") or (k[0] == "DOWN" and k[1] == "UP")
        # 高亮样式先算好：不能在 f-string 的表达式里写转义双引号，
        # Python 3.11 及更早会直接报 SyntaxError（PEP 701 到 3.12 才允许）
        conf_style = ' style="background:#fdecec;"' if is_conf else ""
        grid_rows += (
            f"<tr{conf_style}>"
            f"<td style='text-align:left;'>{GRID_CN[k]}</td><td>{len(v20)}</td>"
            f"<td>{pct(statistics.median(v5)) if v5 else '—'}</td>"
            f"<td>{pct(statistics.median(v20)) if v20 else '—'}</td>"
            f"<td>{pct(frac_positive(v20), 1) if v20 else '—'}</td>"
            f"<td>{pct(statistics.median(adj)) if adj else '—'}</td>"
            f"<td>{pct(frac_positive(adj), 1) if adj else '—'}</td></tr>")

    cmp_rows_html = ""
    for c in cmp_rows:
        sig = "显著" if c["p"] < 0.05 else ("边际" if c["p"] < 0.10 else "不显著")
        col = "#1a7f37" if c["p"] < 0.05 else "#888"
        cmp_rows_html += (
            f"<tr><td>{c['h']}日</td><td style='text-align:left;'>{c['a']} − {c['b']}</td>"
            f"<td>{pct(c['med_a'])}</td><td>{pct(c['med_b'])}</td>"
            f"<td style='font-weight:bold;'>{pct(c['diff'], sign=True)}</td>"
            f"<td>{c['p']:.3f}</td>"
            f"<td style='color:{col};'>{sig}</td>"
            f"<td>{c['n_nov_a']}/{c['n_nov_b']}</td></tr>")

    seg_html = ""
    for row in seg_rows:
        d1c = "#c00" if row["d1"] < 0 else "#1a7f37"
        d2c = "#c00" if row["d2"] is not None and row["d2"] < 0 else "#1a7f37"
        seg_html += (
            f"<tr><td>{row['label']}</td>"
            f"<td>{pct(row['m_cu'])}</td><td>{pct(row['m_du'])}</td>"
            f"<td style='color:{d1c};'>{pct(row['d1'], sign=True)}</td>"
            f"<td>{pct(row['m_cd']) if row['m_cd'] is not None else '—'}</td>"
            f"<td>{pct(row['m_dd']) if row['m_dd'] is not None else '—'}</td>"
            f"<td style='color:{d2c};'>"
            f"{pct(row['d2'], sign=True) if row['d2'] is not None else '—'}</td></tr>")

    perf_html = ""
    for key, r in results.items():
        pg, pb = r["perf_gate"], r["perf_base"]
        d = pg["total"] - pb["total"]
        dc = "#1a7f37" if d > 0 else "#c00"
        perf_html += (
            f"<tr><td style='text-align:left;'>{r['label']}</td>"
            f"<td>{pct(pg['total'], 1)}</td><td>{pct(pb['total'], 1)}</td>"
            f"<td style='color:{dc};font-weight:bold;'>{pct(d, 1, True)}</td>"
            f"<td>{pct(pg['mdd'], 1)}</td><td>{pct(pb['mdd'], 1)}</td>"
            f"<td>{pg['n_trade']}/{pb['n_trade']}</td>"
            f"<td>{pct(pg['win'], 1)}</td></tr>")

    as_of = max(r["end"] for r in results.values())
    n_days_med = int(statistics.median([r["n_days"] for r in results.values()]))
    hype_days = results.get("HYPE", {}).get("n_days", "—")
    min_p = min([c["p"] for c in sig20]) if sig20 else float("nan")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>多周期共振·风险闸门验证</title>
<style>
 body{{font-family:'Microsoft YaHei',Arial,sans-serif;color:#222;margin:0;background:#f5f6f8;}}
 .wrap{{max-width:1120px;margin:0 auto;padding:24px 28px 60px;}}
 h1{{font-size:22px;margin:0 0 4px;}} h2{{font-size:17px;margin:26px 0 8px;border-left:5px solid #3a7bd5;padding-left:10px;}}
 .sub{{color:#888;font-size:13px;margin:0 0 16px;}}
 .verdict{{background:#fff;border-left:6px solid {vcolor};border-radius:8px;padding:14px 18px;margin:0 0 18px;
   box-shadow:0 1px 3px rgba(0,0,0,.08);}}
 .verdict b{{color:{vcolor};font-size:16px;}}
 table{{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;
   box-shadow:0 1px 3px rgba(0,0,0,.08);font-size:13px;margin-bottom:10px;}}
 th,td{{padding:7px 10px;border-bottom:1px solid #eee;text-align:right;}}
 th{{background:#f0f2f5;text-align:right;font-weight:normal;color:#555;}}
 td:first-child,th:first-child{{text-align:left;}}
 .note{{font-size:12px;color:#777;line-height:1.7;background:#fff;border-radius:8px;padding:14px 18px;}}
</style></head><body><div class="wrap">
<h1>多周期共振 · 风险闸门验证</h1>
<p class="sub">全 {len(results)} 品种｜样本中位 {n_days_med} 个交易日/品种｜数据截至 {as_of}｜
周线参数 = 日线 ÷5 机械映射（MA24/MA12，零自由度）</p>

<div class="verdict"><b>{verdict}</b><br>
冲突格更差成立率：多头侧 {ok_up}/{tot}、空头侧 {ok_dn}/{tot}（合计 {agree_rate*100:.0f}%）；
20 日对比中最小的 p 值为 {min_p:.3f}；
闸门跑赢品种 {wins}/{len(results)}。</div>

<h2>一、九宫格事件研究</h2>
<table><tr><th>格子（日线/周线）</th><th>样本</th><th>5日原始</th><th>20日原始</th>
<th>20日胜率</th><th>20日方向调整</th><th>调整后胜率</th></tr>{grid_rows}</table>
<p class="sub">方向调整 = 按日线信号交易的实际收益（UP 做多取原始收益、DOWN 做空取相反数、RANGE 空仓为 0）。
红底行为冲突格。</p>

<h2>二、关键对比（含显著性）</h2>
<table><tr><th>前瞻</th><th>对比</th><th>冲突格</th><th>同向格</th><th>差</th><th>p 值</th><th>结论</th><th>非重叠 n</th></tr>
{cmp_rows_html}</table>
<p class="sub">p 值来自<b>非重叠子样本</b>的 Mann-Whitney U 检验（每 h 日取一点）。日频前瞻收益存在严重自相关，
用全样本做检验会系统性夸大显著性，故只用于描述性中位数。</p>

<h2>三、分段一致性（半年一段）</h2>
<table><tr><th>区间</th><th>多头·冲突</th><th>多头·共振</th><th>差</th>
<th>空头·冲突</th><th>空头·共振</th><th>差</th></tr>{seg_html}</table>
<p class="sub">差为负（红）= 周线唱反调时收益更差 = 闸门有价值。若符号在不同时段来回翻转，说明没有稳定效应。</p>

<h2>四、策略口径回测（闸门 vs 纯日线）</h2>
<table><tr><th>品种</th><th>闸门收益</th><th>纯日线收益</th><th>增量</th>
<th>闸门最大回撤</th><th>纯日线最大回撤</th><th>交易数(闸/日)</th><th>闸门胜率</th></tr>{perf_html}</table>
<p class="sub">口径：日线 UP 做多、DOWN 做空、RANGE 空仓；闸门版额外把「周线反向」的仓位归零。
满仓多空、无杠杆、无成本。<b>这套口径的绝对收益本身不具参考性</b>——状态机是市场分类工具，不是交易系统；
长期上涨品种上做空必然亏损。此处只看<b>相对增量</b>。</p>

<h2>五、方法与局限</h2>
<div class="note">
<b>无未来函数</b>：第 i 天的周线状态取「最后一根 date ≤ 该日的周线」。周线 bar 的日期是该周最后交易日，
该状态在该日收盘即已知。当前未走完的周一律剔除。<br>
<b>参数零自由度</b>：周线 MA24/MA12 是日线 120/60 的 ÷5 机械映射，不参与任何搜索，因此不存在「调参调出来的好结果」。<br>
<b>重叠样本</b>：5/10/20 日前瞻收益在相邻日高度重叠，描述统计用全样本，显著性检验用非重叠子样本。<br>
<b>样本跨度</b>：{min(r['start'] for r in results.values())} ~ {as_of}，覆盖 2021-2022 熊市、2023-2024 牛市、2025-2026 震荡，
跨周期而非单一行情。<br>
<b>已知混杂</b>：商品用期货连续价（未做换月复权），换月跳空会污染周线收盘；HYPE 周线样本仅约
{hype_days} 个交易日。<br>
<b>本报告为规则化验证，非投资建议。</b>
</div>
</div></body></html>"""

    with open("backtest_multi_tf.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\nWrote backtest_multi_tf.html")

    with open("_bt_multi_tf.json", "w", encoding="utf-8") as f:
        json.dump({k: {kk: vv for kk, vv in v.items()
                       if kk not in ("dates", "closes", "st_d", "st_w", "ev",
                                     "pos_gate", "pos_base")}
                   for k, v in results.items()}, f, ensure_ascii=False, indent=1)
    print("\nWrote _bt_multi_tf.json")


if __name__ == "__main__":
    main()
