"""商品连续合约「换月跳空」A/B 检验。

问题
----
Yahoo 的期货连续合约（`XX=F`）是**简单拼接**、不做价差调整。换月那天，新合约
的价格直接接在旧合约后面，于是产生一个**与该商品基本面无关的水平位移**。
本项目用它跑 MA60/MA120 + RSI 的趋势状态机，一次假跳空就足以制造一次假穿越，
把状态翻转并错上几十天。

2026-11-26 实测的指纹（见下方 CAND 全量跑出的表）：
  玉米 ZC  07-15 -17% / 07-17 -17% / 07-15 -13%   ← 多个年份同一日期附近
  大豆 ZS  08-15 -10% / 07-15 -10%
  豆粕 ZM  07-15 -13% / 08-15 -13%（两次）
  瘦肉猪   08-15 -24% / 08-16 -19%
  活牛 LE  09-01 +7%  / 07-01 -7%
真实行情不长这样：天然气的 -47%/+46%/+29% 是连着三天的同向大动（寒潮），
铜 2025-07-31 的 -22% 是 COMEX 关税基差崩塌。**换月跳空的特征是「孤立 + 每年
同一日期反复出现」**，这也是本脚本的识别判据。

方法
----
1. 识别：`|r_t| > max(6 × rolling20σ_{t-1}, 5%)` 且**孤立**（前一日与后一日
   的绝对涨跌都不到当日的一半）。σ 用截至 t-1 的 20 日已实现波动，避免跳空
   自身抬高阈值把后面的跳空藏掉。
2. 调整：对数空间做**后向比率复权**（back-adjust）。把跳空日的收益截断到阈值，
   差额以后缀和的形式平移到**该日之前**的全部历史价格上，t 之后的价格一律不动。
   这样调整后的最后一个价格与原始一致（页面展示的收盘价不会被改），只有历史
   路径被抹平 —— 这正是期货连续序列的标准处理方式。
3. 对照：两臂各跑一次 `daily_states`（同一 s0=120、同一套参数），比较
   2024-01-01 以来的状态差异天数、区间数、当前状态。

判据
----
影响大 = 差异天数占比高 **或** 当前状态不同 **或** 区间数明显变化。
只影响 1~2 天的路径抖动不值得引入一套复权逻辑；翻转当前状态则必须处理。

用法
----
    python _ab_rollgap.py            # 全量候选
    python _ab_rollgap.py ZC=F ZS=F  # 指定品种
"""

import sys
from datetime import datetime, timezone

from visualize_btc_regime import daily_states
from daily_regime_alert import fetch_yahoo_daily, WARM_START, START

# 商品候选：代码 → (中文名, 单位)。单位按各合约的实际报价口径，不是「美元」一刀切：
# 谷物/软商品/畜牧都以美分计价，写成「美元/蒲式耳」会让 456 这个数字被读成 456 美元。
CAND = {
    "ZC=F":  ("玉米",     "美分/蒲式耳"),
    "ZS=F":  ("大豆",     "美分/蒲式耳"),
    "ZW=F":  ("小麦",     "美分/蒲式耳"),
    "ZM=F":  ("豆粕",     "美元/短吨"),
    "SB=F":  ("糖",       "美分/磅"),
    "KC=F":  ("咖啡",     "美分/磅"),
    "CT=F":  ("棉花",     "美分/磅"),
    "CC=F":  ("可可",     "美元/吨"),
    "LE=F":  ("活牛",     "美分/磅"),
    "HE=F":  ("瘦肉猪",   "美分/磅"),
    "OJ=F":  ("橙汁",     "美分/磅"),
    "HO=F":  ("取暖油",   "美元/加仑"),
    "RB=F":  ("汽油",     "美元/加仑"),
    "ALI=F": ("铝",       "美元/吨"),
    "PA=F":  ("钯金",     "美元/盎司"),
    # 对照：已在页面上的两个，用来校准判据有没有误伤真实行情
    "NG=F":  ("天然气(对照)", "美元/MMBtu"),
    "HG=F":  ("铜(对照)",     "美元/磅"),
}

SIG_K = 6.0        # 跳空阈值 = 6 倍滚动 20 日波动
FLOOR = 0.05       # 绝对下限 5%：低波动期 6σ 可能只有 2%，那不叫跳空
ISO = 0.5          # 孤立性：前后两日不到当日幅度的一半


def rolling_sigma(rets, win=20):
    """截至 t-1 的滚动标准差，长度与 rets 对齐；前 win 个为 None。"""
    out = [None] * len(rets)
    for i in range(win, len(rets)):
        seg = rets[i - win:i]
        m = sum(seg) / win
        out[i] = (sum((x - m) ** 2 for x in seg) / (win - 1)) ** 0.5
    return out


def find_gaps(closes):
    """返回 [(index, ret, threshold)]，index 为跳空日在下标中的位置。"""
    rets = [0.0] + [(closes[i] / closes[i - 1] - 1) if closes[i - 1] else 0.0
                    for i in range(1, len(closes))]
    sig = rolling_sigma(rets)
    gaps = []
    for t in range(1, len(closes) - 1):
        if sig[t] is None:
            continue
        thr = max(SIG_K * sig[t], FLOOR)
        if abs(rets[t]) <= thr:
            continue
        # 孤立性：真实行情会连着走（NG 2021-01 三天都大），换月只跳一天
        if max(abs(rets[t - 1]), abs(rets[t + 1])) > ISO * abs(rets[t]):
            continue
        gaps.append((t, rets[t], thr))
    return rets, gaps


def back_adjust(closes, gaps):
    """后向比率复权：只改历史，末端价格不动。"""
    import math
    lp = [math.log(c) if c > 0 else 0.0 for c in closes]
    diff = {}
    for t, r, thr in gaps:
        clamped = math.copysign(thr, r)
        diff[t] = math.log((1 + r) / (1 + clamped))   # >0：跳空比阈值更小 → 历史上移
    # 后缀和：位置 i 上的调整量 = 所有发生在 i 之后的跳空的 diff 之和
    suffix = [0.0] * len(lp)
    acc = 0.0
    for i in range(len(lp) - 1, -1, -1):
        suffix[i] = acc
        if i in diff:
            acc += diff[i]
    return [math.exp(lp[i] + suffix[i]) for i in range(len(lp))]


def compare(closes, dates, adj):
    """两臂跑同一状态机，统计 2024 起的状态差异。"""
    s0 = 120
    if len(closes) <= s0 + 10:
        return None
    st_a, _, _ = daily_states(closes, s0, 70, 30, True, 2, True)
    st_b, _, _ = daily_states(adj, s0, 70, 30, True, 2, True)
    cut = START
    idx = [i for i, d in enumerate(dates) if d >= cut and i >= s0]
    diff = sum(1 for i in idx if st_a[i] != st_b[i])
    ratio = diff / len(idx) if idx else 0.0

    def n_seg(st):
        n, prev = 0, None
        for i in idx:
            if st[i] != prev:
                n += 1
                prev = st[i]
        return n

    return {
        "n_days": len(idx),
        "diff_days": diff,
        "diff_ratio": ratio,
        "seg_a": n_seg(st_a),
        "seg_b": n_seg(st_b),
        "st_a": st_a[len(closes) - 1],
        "st_b": st_b[len(closes) - 1],
    }


def main():
    syms = [s for s in sys.argv[1:] if s in CAND] or list(CAND)
    sd = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    print(f"{'品种':8s}{'代码':8s}{'跳空数':>5s}  {'跳空日（幅度）':46s}"
          f"{'差异日':>7s}{'占比':>7s}{'区间':>9s}  当前状态")
    print("-" * 118)
    rows = []
    for sym in syms:
        name, unit = CAND[sym]
        try:
            data = fetch_yahoo_daily(sym, sd)
        except Exception as e:                              # noqa: BLE001
            print(f"{name:8s}{sym:8s}  FAIL {type(e).__name__}: {str(e)[:40]}")
            continue
        closes = [r["close"] for r in data]
        dates = [r["date"] for r in data]
        rets, gaps = find_gaps(closes)
        adj = back_adjust(closes, gaps)
        cmp_ = compare(closes, dates, adj)
        gap_s = ", ".join(f"{dates[t][5:]}{r * 100:+.0f}%" for t, r, _ in gaps[:4])
        if len(gaps) > 4:
            gap_s += f" …+{len(gaps) - 4}"
        if cmp_:
            flag = "" if cmp_["st_a"] == cmp_["st_b"] else "  ← 状态反转"
            print(f"{name:8s}{sym:8s}{len(gaps):5d}  {gap_s:46s}"
                  f"{cmp_['diff_days']:7d}{cmp_['diff_ratio'] * 100:6.1f}%"
                  f"{cmp_['seg_a']:4d}→{cmp_['seg_b']:<4d}  "
                  f"{cmp_['st_a']}→{cmp_['st_b']}{flag}")
            rows.append((name, sym, len(gaps), cmp_))
        else:
            print(f"{name:8s}{sym:8s}{len(gaps):5d}  {gap_s:46s}   数据不足")

    print("\n=== 影响排序（按差异天数占比）===")
    for name, sym, n, c in sorted(rows, key=lambda x: -x[3]["diff_ratio"]):
        mark = "必须处理" if (c["diff_ratio"] > 0.10 or c["st_a"] != c["st_b"]) else \
               ("建议处理" if c["diff_ratio"] > 0.03 else "可忽略")
        print(f"  {name:6s} {sym:7s} 跳空{n:2d}次  差异 {c['diff_days']:3d}/{c['n_days']} 天"
              f" ({c['diff_ratio'] * 100:4.1f}%)  区间 {c['seg_a']}→{c['seg_b']}  {mark}")


if __name__ == "__main__":
    main()
