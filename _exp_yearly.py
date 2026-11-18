#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_yearly.py —— 生产配置在 50 万本金下的逐年表现

生产配置（portfolio_config.weights_for(500_000)）：
  12 币 · 容量水位填充 · 单币上限 25% · Σw = 0.8544（14.6% 留现金）
  BTC 25.0 / HYPE 25.0 / ETH 16.1 / SOL 6.6 / BNB 3.5 / XRP 3.1
  ZEC 2.4 / DOGE 1.2 / NEAR 0.9 / LINK 0.7 / UNI 0.7 / AAVE 0.4（%）

本脚本要回答的问题：
  ① 这套配置放在 50 万上，每一年分别赚多少、亏多少、最多回撤多少
  ② 每一年的收益是哪几个币贡献的
  ③ 换成别的权重表（等权 / 小额表 / 只买 BTC），同样的信号能跑成什么样
  ④ 滑点系数翻倍会毁掉多少收益（A=100 是假想值，尚未用真实成交校准）

关键口径（与上轮 _exp_capw.py 的差异，务必注意）：
  · 滑点金额改用**该币真实权重**（上一轮统一用 0.09 中性参考，
    生产权重从 25% 到 0.4% 差 60 倍，sqrt 后仍差 7.7 倍，不能再共用）
  · renorm 期（HYPE/ZEC/DOGE/NEAR 缺席）实际金额比原始权重更高，
    滑点在这里被低估 —— 影响很小（早期成交额也小，绝对金额低）
  · Σw<1 的差额计为现金，不产生收益，不归一化
  · 逐年回撤是**年内**回撤（每年 1 月 1 日重新起算峰值），不是全段回撤

运行：python _exp_yearly.py
"""
import bisect
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import load_bars, to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h

CAP = 500_000
F4 = BT.FUND_DAILY / 6.0
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
QV = PC.CAP_4H_WORST
START_NAV = "2020-09-01"

# 生产权重（与实盘同一函数，不复制粘贴）
W_PROD = PC.weights_for(CAP)

# 对照臂
W_EQ = {c: 1 / 12 for c in ALL12}
W_SMALL = dict(PC.WEIGHTS_SMALL)
W_BTC = {"BTC": 1.0}


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def pct(v, n=8, sign=True):
    if v is None:
        return pad("—", n)
    return pad(("%+.1f%%" % (v * 100)) if sign else ("%.1f%%" % (v * 100)), n)


# ---------------- 数据 ----------------
def load_coin(c):
    if c == "HYPE":
        b4 = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
        d1 = [dict(x, date=x["date"][:10]) for x in load_hype("1d")]
    else:
        b = load_bars(c, "1h", tail=False)
        b4 = agg4h(b)
        d1 = to_daily(b)
    reg, blood = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg)
    bl4 = align_regime_safe(b4, blood)
    atr = atr_wilder(b4, 14)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
    return b4, base, pos, trades


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c][0]) for c in ALL12}
POSV = {c: [p * k * VT.KAPPA for p, k in zip(DATA[c][2], COEF[c])] for c in ALL12}


def get_nav(c, w, slip_a=100.0, log=None, w_sched=None):
    """该币在**真实权重 w** 下的 sleeve 净值（VT60 后、含资金费与滑点）。

    log     传入 list 时，逐笔记录 (年, 成本类型, 占净值比例)，用于年费拆解。
    w_sched 传入 {年份: 权重} 时，滑点按该年权重算（净值增长后容量会变）。
    """
    b4, base, pos = DATA[c][0], DATA[c][1], POSV[c]
    qv = QV.get(c) or 1e12
    out, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= START_NAV), len(b4))
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        y4 = b4[k]["date"][:4]
        sl = pos[k] - base[k]
        if F4 and sl:
            cf = F4 * sl
            nav *= (1 - cf)
            if log is not None:
                log.append((y4, "fund", cf))
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            cb = BT.COST * d
            nav *= (1 - cb)
            if log is not None:
                log.append((y4, "fee", cb))
            wy = (w_sched.get(y4) or {}).get(c, w) if w_sched else w
            cs = slip_a / 1e4 * math.sqrt(nav * CAP * max(wy, 1e-6) * d / qv) * d
            nav *= (1 - cs)
            if log is not None:
                log.append((y4, "slip", cs))
        out[b4[k]["date"]] = nav
    return out


NAVS = {c: get_nav(c, W_PROD.get(c, 0.09)) for c in ALL12}
FIRST = {c: min(NAVS[c]) for c in ALL12}
KEYS = {c: sorted(NAVS[c]) for c in ALL12}
ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))


def nav_before(c, d):
    ks = KEYS[c]
    i = bisect.bisect_right(ks, d) - 1
    return NAVS[c][ks[i]] if i >= 0 else 1.0


def curve(coins, ws, start=START_NAV, end=None, renorm=True, w_sched=None):
    """组合净值序列 [(date, nav)]。w_sched 可按年覆盖权重表。"""
    dates = [d for d in ALLD if d >= start and (end is None or d < end)]
    if not dates:
        return []
    last, units = {}, {}
    cq, cash = None, 0.0
    out = []
    for d in dates:
        wv = (w_sched.get(d[:4]) if w_sched else None) or ws
        tgt = min(1.0, sum(wv.values()))
        live = [c for c in coins if FIRST[c] <= d and c in wv]
        if not live:
            continue
        sw = sum(wv[c] for c in live)
        uw = ({c: wv[c] / sw * tgt for c in live} if renorm
              else {c: wv[c] for c in live})
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
            elif c not in last:
                last[c] = nav_before(c, d)
        v = cash + sum(units.get(c, 0.0) * last[c] for c in live) if units else 1.0
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if (cq is None) or (q != cq) or (set(units) != set(live)):
            wsum = sum(uw.values())
            if units:
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c]) for c in live) / v / 2
                v *= (1 - min(g, 1.0) * 7e-4)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((d, v))
    return out


def yr_ret(cv):
    """逐年收益 + 年末净值。"""
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    return {y: lastv[y] / first[y] - 1 for y in first}


def yr_dd(cv):
    """逐年**年内**最大回撤（每年 1 月 1 日重新起算峰值）。"""
    pk, dd = {}, {}
    for d, v in cv:
        y = int(d[:4])
        pk[y] = max(pk.get(y, v), v)
        dd[y] = min(dd.get(y, 0.0), v / pk[y] - 1)
    return dd


def yr_end_equity(cv):
    e = {}
    for d, v in cv:
        e[int(d[:4])] = v
    return e


def trade_stats(trades):
    """信号层统计（不含权重）：笔数 / 胜率 / 平均 R / PF。"""
    n = len(trades)
    if not n:
        return dict(n=0, win=None, avg=None, pf=None)
    win = sum(1 for t in trades if t["r"] > 0) / n
    avg = sum(t["r"] for t in trades) / n
    gp = sum(t["r"] for t in trades if t["r"] > 0)
    gl = -sum(t["r"] for t in trades if t["r"] < 0)
    return dict(n=n, win=win, avg=avg, pf=(gp / gl if gl > 0 else float("inf")))


# ================= 输出 =================
print("\n" + "=" * 96)
print("【0】50 万本金 · 生产权重表（portfolio_config.weights_for(500000)）")
print("=" * 96)
rows = PC.table(CAP)
print(pad("币", 6) + pad("权重", 8) + pad("底仓U", 11) + pad("加仓U", 10)
      + pad("多头峰值U", 12) + pad("空头U", 10) + pad("冲击", 8)
      + pad("判定", 8) + pad("拆单", 6) + pad("峰值上限万", 11))
print("-" * 96)
for r in rows:
    print(pad(r["c"], 6) + pad("%.1f%%" % (r["w"] * 100), 8)
          + pad("%.0f" % r["base"], 11) + pad("%.0f" % r["add"], 10)
          + pad("%.0f" % r["peak"], 12) + pad("%.0f" % r["short"], 10)
          + pad("%.2f%%" % (r["imp"] * 100), 8) + pad(r["flag"], 8)
          + pad(r["slices"], 6) + pad("%.0f" % (PC.cap_ceiling(r["c"], CAP) / 1e4), 11))
print("-" * 96)
b = sum(r["base"] for r in rows)
p_ = sum(r["peak"] for r in rows)
s = sum(r["short"] for r in rows)
print(f"权重合计 {sum(W_PROD.values()) * 100:.1f}%   "
      f"底仓 {b:,.0f} U   多头峰值 {p_:,.0f} U   空头峰值 {abs(s):,.0f} U   "
      f"现金 {CAP * (1 - sum(W_PROD.values())):,.0f} U")
print(f"总拆单需求：多头满峰值 {sum(r['slices'] for r in rows)} 张，"
      f"空头 {sum(r['slices_short'] for r in rows)} 张")

YEARS = sorted({int(d[:4]) for d in ALLD if d >= START_NAV})
TGT_W = min(1.0, sum(W_PROD.values()))

print("\n" + "=" * 96)
print("【1】逐年表现（生产配置，50 万起步，复利）")
print("=" * 96)
cv = curve(ALL12, W_PROD)
rr = yr_ret(cv)
dd = yr_dd(cv)
eq = yr_end_equity(cv)

print(pad("年份", 8) + pad("收益", 10) + pad("年末权益", 13) + pad("年内回撤", 10)
      + pad("币数", 6) + "  缺失")
print("-" * 96)
for y in YEARS:
    if y not in rr:
        continue
    prev = [c for c in ALL12 if FIRST[c] < f"{y}-01-01"]
    new = [c for c in ALL12 if f"{y}-01-01" <= FIRST[c] <= f"{y}-12-31"]
    miss = [c for c in ALL12 if FIRST[c] > f"{y}-12-31"]
    tag = "*" if cv[0][0][:4] == str(y) else " "
    extra = ""
    if new and cv[0][0][:4] != str(y):
        extra = "   年内新增 " + "/".join(new)
    print(pad(f"{y}{tag}", 8) + pct(rr[y], 10) + pad("%.0f" % (CAP * eq[y]), 13)
          + pct(dd[y], 10) + pad(len(prev) + len(new), 6)
          + "  " + ("缺 " + "/".join(miss) if miss else "满员") + extra)
print("-" * 96)
tot = cv[-1][1] / cv[0][1] - 1
d0 = date(*map(int, cv[0][0].split("-")))
d1 = date(*map(int, cv[-1][0].split("-")))
yrs = (d1 - d0).days / 365.25
ann = (1 + tot) ** (1 / yrs) - 1 if yrs > 0 else 0
pk, mdd = cv[0][1], 0.0
for _, v in cv:
    pk = max(pk, v)
    mdd = min(mdd, v / pk - 1)
print(f"全段 {cv[0][0]} ~ {cv[-1][0]}（{yrs:.2f} 年）：{1 + tot:.2f}×  **年化 {ann:+.1%}**  "
      f"最大回撤 {mdd:.1%}  Calmar {ann / abs(mdd):.2f}")
print(f"50 万 → {CAP * cv[-1][1] / cv[0][1]:,.0f} U（净赚 {CAP * cv[-1][1] / cv[0][1] - CAP:,.0f} U）")
print("* 2020 为不完整年（数据起点 2020-09，仅 4 个月），不参与年化以外的判断。")
print(f"仓位列恒为 {TGT_W * 100:.1f}%：renorm 把缺席币的权重分给在场币，")
print("  所以即使 2021 年只有 11 个币，钱也是按 85.4% 投出去的（余 14.6% 现金）。")

print("\n" + "=" * 96)
print("【2】逐年交易统计（信号层，不含权重：R 已按风险归一）")
print("=" * 96)
by_year = {}
for c in ALL12:
    for t in DATA[c][3]:
        y = int(t["d"][:4])
        by_year.setdefault(y, []).append(t)
print(pad("年份", 7) + pad("笔数", 7) + pad("胜率", 8) + pad("平均R", 8)
      + pad("PF", 7) + pad("总R", 9) + pad("多头笔", 8) + pad("空头笔", 8))
print("-" * 96)
for y in YEARS:
    ts = by_year.get(y, [])
    st = trade_stats(ts)
    nl = sum(1 for t in ts if t["side"] > 0)
    ns = len(ts) - nl
    print(pad(y, 7) + pad(st["n"], 7)
          + pad("%.0f%%" % (st["win"] * 100) if st["win"] is not None else "—", 8)
          + pad("%+.2f" % st["avg"] if st["avg"] is not None else "—", 8)
          + pad("%.2f" % st["pf"] if st["pf"] not in (None, float("inf")) else "—", 7)
          + pad("%+.0f" % sum(t["r"] for t in ts), 9)
          + pad(nl, 8) + pad(ns, 8))
print("-" * 96)
allt = [t for ts in by_year.values() for t in ts]
st = trade_stats(allt)
print(f"合计 {st['n']} 笔  胜率 {st['win']:.1%}  平均 {st['avg']:+.2f}R  PF {st['pf']:.2f}  "
      f"总 {sum(t['r'] for t in allt):+.0f}R")

print("\n" + "=" * 96)
print("【3】逐年收益归因（单币 sleeve 收益 × 当年生效权重 → 近似 pp）")
print("=" * 96)
print("说明：复利组合无法严格线性分解，这里给一阶近似，残差 = 组合实际 − Σ贡献。")
print("      「生效权重」= 原始权重 / 在场合计 × 0.8544，与 curve 的 renorm 同一口径。")
for y in YEARS:
    if y not in rr:
        continue
    lv = [c for c in ALL12 if FIRST[c] <= f"{y}-12-31"]
    sw = sum(W_PROD[c] for c in lv)
    contrib = {}
    for c in lv:
        ks = [k for k in KEYS[c] if k[:4] == str(y)]
        if len(ks) < 2:
            continue
        r_c = NAVS[c][ks[-1]] / NAVS[c][ks[0]] - 1
        contrib[c] = r_c * (W_PROD[c] / sw * TGT_W if sw > 0 else 0)
    if not contrib:
        continue
    items = sorted(contrib.items(), key=lambda kv: -kv[1])
    tot_c = sum(contrib.values())
    print(f"\n{y}  组合 {rr[y] * 100:+.1f}%   Σ贡献 {tot_c * 100:+.1f}%  "
          f"残差 {(rr[y] - tot_c) * 100:+.1f}pp")
    seg = "  ".join(f"{c}{v * 100:+.1f}" for c, v in items if abs(v) >= 0.005)
    print("   " + seg)

print("\n" + "=" * 96)
print("【4】对照：同样信号、不同权重表")
print("=" * 96)
print("「归一满仓」= 把生产的 85.4% 按比例放大到 100%，用来分离两件事：")
print("  仓位高低（85% vs 100%）与权重结构（堆 BTC/HYPE vs 均匀分散）。")
W_FULL = {c: v / TGT_W for c, v in W_PROD.items()}
W_G20 = PC.capacity_weights(CAP, single_cap=0.20)
W_NOCAP = PC.capacity_weights(CAP, single_cap=1.0)
ARMS = [("生产（容量填充≤25%）", W_PROD), ("  └ 归一至满仓", W_FULL),
        ("容量填充 ≤20%", W_G20), ("容量填充 无上限", W_NOCAP),
        ("12币 AAVE/LINK 4%", W_SMALL), ("12币 等权", W_EQ), ("只买 BTC", W_BTC)]
print(pad("方案", 22) + pad("冲击", 8) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计盈亏U", 12) + pad("最差年", 9) + pad("复利倍数*", 10))
print("-" * 96)
print("「累计盈亏U」= 口径 A（本金恒 50 万、每年提走、收益率简单相加）")
print("「复利倍数*」= 固定权重跑全复利的旧口径，带 * 者为幻觉，只留作对照")
print("-" * 96)
for name, w in ARMS:
    coins = [c for c in ALL12 if w.get(c)]
    c2 = curve(coins, w)
    r2 = yr_ret(c2)
    t2 = c2[-1][1] / c2[0][1] - 1
    mi = PC.max_impact_of(w, CAP)
    cum_u = sum(CAP * r2.get(y, 0.0) for y in YEARS)
    worst = min(r2.values()) if r2 else 0.0
    line = (pad(name, 22) + pad("%.1f%%" % (mi * 100), 8))
    for y in YEARS:
        line += pct(r2.get(y), 8)
    print(line + pad("%+.0f" % cum_u, 12) + pct(worst, 9)
          + pad("%.2f×" % (1 + t2), 10))

print("\n" + "=" * 96)
print("【5】滚动入场：在每年 1 月 1 日投 50 万，一直拿到 2026-11-18")
print("=" * 96)
print("回答的是「我明年入场会怎样」，而不是「历史上最好的一年有多好」。")
print(pad("入场日", 12) + pad("持有年数", 10) + pad("累计", 9) + pad("年化", 9)
      + pad("期间最大回撤", 14) + pad("Calmar", 8) + pad("期末权益", 13))
print("-" * 96)
for y in YEARS:
    s = f"{y}-01-01"
    if s < START_NAV or s >= cv[-1][0]:
        continue
    c3 = curve(ALL12, W_PROD, start=s)
    if len(c3) < 30:
        continue
    r3 = c3[-1][1] / c3[0][1] - 1
    hd = (date(*map(int, c3[-1][0].split("-"))) - date(*map(int, c3[0][0].split("-")))).days / 365.25
    pk3, mdd3 = c3[0][1], 0.0
    for _, v in c3:
        pk3 = max(pk3, v)
        mdd3 = min(mdd3, v / pk3 - 1)
    a3 = (1 + r3) ** (1 / hd) - 1 if hd > 0.2 else 0
    print(pad(s, 12) + pad("%.2f" % hd, 10) + pad("%.2f×" % (1 + r3), 9)
          + pct(a3, 9) + pct(mdd3, 14) + pad("%.2f" % (a3 / abs(mdd3)), 8)
          + pad("%.0f" % (CAP * c3[-1][1] / c3[0][1]), 13))

print("\n" + "=" * 96)
print("【6】敏感性：滑点系数 A 从 100 翻到 200（A 尚未用真实成交校准）")
print("=" * 96)
NAV200 = {c: get_nav(c, W_PROD.get(c, 0.09), slip_a=200.0) for c in ALL12}
NAVS_BACKUP = dict(NAVS)
NAVS.update(NAV200)
cv200 = curve(ALL12, W_PROD)
rr200 = yr_ret(cv200)
NAVS.clear()
NAVS.update(NAVS_BACKUP)
print(pad("年份", 7) + pad("A=100", 10) + pad("A=200", 10) + pad("差额pp", 10))
print("-" * 96)
for y in YEARS:
    if y not in rr:
        continue
    a, b2 = rr.get(y, 0), rr200.get(y, 0)
    print(pad(y, 7) + pct(a, 10) + pct(b2, 10) + pad("%+.1f" % ((b2 - a) * 100), 10))
t200 = cv200[-1][1] / cv200[0][1] - 1
print("-" * 96)
print(f"全段 {1 + tot:.2f}× → {1 + t200:.2f}×：累计收益从 {tot * 100:+.0f}% 降到 "
      f"{t200 * 100:+.0f}%（少 {abs(t200 - tot) * 100:.0f} 个百分点）")
print(f"年化从 {ann:+.1%} 降到 {(1 + t200) ** (1 / yrs) - 1:+.1%}")

print("\n" + "=" * 96)
print("【7】年费拆解（资金费 + 手续费 + 滑点，占本金比例）")
print("=" * 96)
print(f"资金费率参数 FUND_DAILY = {BT.FUND_DAILY:.4%}/日 → 名义年化 "
      f"{BT.FUND_DAILY * 365:.1%}，但**只对加仓层**计息（底仓在现货，不付资金费）。")
print(f"手续费 COST = {BT.COST:.2%}/边。滑点 A = 100（假想值，等真实成交校准）。")
CLOG = {}
for c in ALL12:
    lg = []
    get_nav(c, W_PROD.get(c, 0.09), log=lg)
    CLOG[c] = lg
print(pad("年份", 8) + pad("资金费", 10) + pad("手续费", 10) + pad("滑点", 10)
      + pad("合计", 10) + pad("占|收益|", 10))
print("-" * 96)
tot_cost = 0.0
for y in YEARS:
    if y not in rr:
        continue
    lv = [c for c in ALL12 if FIRST[c] <= f"{y}-12-31"]
    sw = sum(W_PROD[c] for c in lv)
    agg = {"fund": 0.0, "fee": 0.0, "slip": 0.0}
    for c in lv:
        eff = W_PROD[c] / sw * TGT_W
        for (yy, kind, x) in CLOG[c]:
            if yy == str(y):
                agg[kind] += x * eff
    s_ = sum(agg.values())
    tot_cost += s_
    print(pad(y, 8) + pct(-agg["fund"], 10) + pct(-agg["fee"], 10) + pct(-agg["slip"], 10)
          + pct(-s_, 10) + pad("%.1f%%" % (s_ / max(abs(rr[y]), 1e-9) * 100), 10))
print("-" * 96)
print(f"{len(YEARS)} 年累计成本 {tot_cost * 100:.1f}%（本金口径），"
      f"年均 {tot_cost / len(YEARS) * 100:.1f}%/年")
print("合计列是各年独立口径，不可直接相加成复利；年均值仅作量级参考。")
print("注：滑点随净值增长而上升（平方根冲击 ∝ 金额），所以后几年占比更高。")

print("\n" + "=" * 96)
print("【8】修正口径：净值长大以后，容量会不够 —— 每年初按当年本金重算权重")
print("=" * 96)
print("【1】～【7】全程用「50 万的权重表」。但净值涨到 700 万时，同样的 25% 权重")
print("等于单币 175 万，早已越过 3% 冲击红线 —— 那是自欺欺人。")
print("这里每年初按当年本金档位调 weights_for()，迭代 3 轮到收敛。")
print("（实盘含义：钱越多，能碰的币越少、仓位越轻。这是容量的物理约束，不是参数选择。）")

SCHED = {}
caps = {}


def cap_weights_sub(coins, capital, max_impact=PC.MAX_IMPACT, single_cap=PC.SINGLE_CAP):
    """与 portfolio_config.capacity_weights 同一算法，但只对**当时已上线**的币分配。

    生产函数用的是 CAP_4H_WORST 全 12 币，会给 2021 年还没出生的 HYPE 留 19% 权重，
    然后被 renorm 排掉 —— 等于白占额度。这里按年初在场币重算。
    """
    unit = PC.peak_weight(1.0, PC._vt_scale())
    wmax = {c: min(single_cap, max_impact * QV[c] / (capital * unit)) for c in coins}
    w = {c: 1.0 / len(coins) for c in coins}
    for _ in range(100):
        over = {c for c in coins if w[c] >= wmax[c] - 1e-12}
        if not over:
            return w
        rest = [c for c in coins if c not in over]
        free = 1.0 - sum(wmax[c] for c in over)
        if not rest or free <= 1e-12:
            return {c: wmax[c] for c in coins}
        new = {c: (wmax[c] if c in over else min(free / len(rest), wmax[c]))
               for c in coins}
        if max(abs(new[c] - w[c]) for c in coins) < 1e-12:
            return new
        w = new
    return w


def weights_at(cap_y, live):
    """当年本金 + 当年在场币 → 该用哪张权重表（与 weights_for 同一判定逻辑）。"""
    sm = {c: PC.WEIGHTS_SMALL[c] for c in live}
    if PC.max_impact_of(sm, cap_y) <= PC.MAX_IMPACT:
        return sm
    return cap_weights_sub(live, cap_y)


for it in range(4):
    cvd = curve(ALL12, W_PROD, w_sched=SCHED or None)
    eqs = yr_end_equity(cvd)
    new_sched = {}
    caps = {}
    for y in YEARS:
        if y not in eqs:
            continue
        cap_y = CAP * eqs[y]
        caps[y] = cap_y
        live = [c for c in ALL12 if FIRST[c] < f"{y}-01-01"] or \
               [c for c in ALL12 if FIRST[c] <= f"{y}-12-31"]
        new_sched[str(y)] = weights_at(cap_y, live)
    nv = {c: get_nav(c, W_PROD.get(c, 0.09), w_sched=new_sched) for c in ALL12}
    NAVS.clear()
    NAVS.update(nv)
    SCHED = new_sched
    print(f"  第 {it + 1} 轮  " + "  ".join(
        f"{y}:{caps[y] / 1e4:.0f}万/{sum(SCHED[str(y)].values()) * 100:.0f}%"
        for y in YEARS if y in caps))

cvd = curve(ALL12, W_PROD, w_sched=SCHED)
rd = yr_ret(cvd)
dd_d = yr_dd(cvd)
eq_d = yr_end_equity(cvd)
print()
print(pad("年份", 8) + pad("年初本金", 12) + pad("前三大权重", 30)
      + pad("仓位", 8) + pad("收益", 10) + pad("年内回撤", 10) + pad("年末权益", 13))
print("-" * 96)
for y in YEARS:
    if y not in rd:
        continue
    w = SCHED[str(y)]
    top = sorted(w.items(), key=lambda kv: -kv[1])[:3]
    topstr = " ".join(f"{c}{v * 100:.0f}" for c, v in top)
    cap_y = caps.get(y, CAP)
    print(pad(y, 8) + pad("%.0f万" % (cap_y / 1e4), 12) + pad(topstr, 30)
          + pad("%.0f%%" % (sum(w.values()) * 100), 8) + pct(rd[y], 10)
          + pct(dd_d[y], 10) + pad("%.0f" % (CAP * eq_d[y]), 13))
t_d = cvd[-1][1] / cvd[0][1] - 1
pk_d, mdd_d = cvd[0][1], 0.0
for _, v in cvd:
    pk_d = max(pk_d, v)
    mdd_d = min(mdd_d, v / pk_d - 1)
a_d = (1 + t_d) ** (1 / yrs) - 1
print("-" * 96)
print(f"全段 {1 + t_d:.2f}×  年化 {a_d:+.1%}  最大回撤 {mdd_d:.1%}  "
      f"Calmar {a_d / abs(mdd_d):.2f}   50 万 → {CAP * cvd[-1][1] / cvd[0][1]:,.0f} U")
print(f"对比【1】固定权重口径 {1 + tot:.2f}× / 年化 {ann:+.1%}："
      f"差 {(t_d - tot) * 100:+.0f} 个百分点")
print("这个差就是「不管容量、一路满仓复利」的幻觉有多大。")

print("\n" + "=" * 96)
print("【9】第三种口径：每年年底结算提走、次年重新投 50 万（本金恒定）")
print("=" * 96)
print("适合「每年分红 / 客户赎回」的运营方式：金额永远是 50 万，滑点不随净值漂。")
print(pad("年份", 8) + pad("收益", 10) + pad("年内回撤", 10) + pad("赚/亏 U", 13)
      + pad("累计盈亏 U", 13))
print("-" * 96)
NAVS.clear()
NAVS.update({c: get_nav(c, W_PROD.get(c, 0.09)) for c in ALL12})
cum = 0.0
for y in YEARS:
    s = START_NAV if cv[0][0][:4] == str(y) else f"{y}-01-01"
    e = f"{y + 1}-01-01"
    c4 = curve(ALL12, W_PROD, start=s, end=e)
    if len(c4) < 30:
        continue
    r4 = c4[-1][1] / c4[0][1] - 1
    pk4, mdd4 = c4[0][1], 0.0
    for _, v in c4:
        pk4 = max(pk4, v)
        mdd4 = min(mdd4, v / pk4 - 1)
    cum += CAP * r4
    print(pad(f"{y}{'*' if s == START_NAV else ' '}", 8) + pct(r4, 10)
          + pct(mdd4, 10) + pad("%+.0f" % (CAP * r4), 13)
          + pad("%+.0f" % cum, 13))
print("-" * 96)
print(f"6 年累计盈亏 {cum:+,.0f} U（本金恒为 50 万，收益率简单相加）")
print("这是最保守的口径：不享受复利，但也不受容量恶化拖累 —— 适合作为下行参照。")
