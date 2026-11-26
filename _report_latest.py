#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_report_latest.py —— 当前生效版本（RANGE 底仓系数按 portfolio_config 配置）的逐年表现

规则（截至 2026-11-19，提交 e24a966）：
  L1 日线 G 状态机定方向与底仓
       UP    → 底仓 1.0
       RANGE → 按来路：UP→RANGE 1.0 / DOWN→RANGE 0.0   ← 本轮新增
       DOWN  → 底仓 0
  L2 4H 收盘突破前 20 根极值 → +0.5×（ATR 分位 >85% 拦截，init 血统不开仓）
  L3 出场：状态翻转 / 1.5×ATR(14) / 满 120 根 4H
  执行：底仓现货（不付资金费），加仓层永续；VT60 波动目标化；季度再平衡
  品种：11 币（2026-11-27 去 DOGE），AAVE/LINK 降权至 4%

口径：
  A = 本金恒 50 万、每年提走、收益率简单相加（跨年可比，但与复利不同）
  B = 全复利、每年按当年本金重算权重（受容量约束）

六段输出：
  【1】逐年：收益 / 年内回撤 / Calmar / 当年盈亏
  【2】近 1/3/5 年汇总
  【3】与旧版（RANGE 一律 1.0）逐年对照
  【4】分币种：独立净值倍数
  【5】口径 B 全复利
  【6】关键风险指标
"""
import math
import os
import sys

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
START_NAV = "2020-09-01"
ALL11 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "NEAR"]     # 2026-11-27 去 DOGE（原 12 币）
W = PC.weights_for(CAP)
TGT = min(1.0, sum(W.values()))
YEARS = list(range(2020, 2027))


def pad(s, n, right=True):
    s = str(s)
    return s.ljust(n) if right else s.rjust(n)


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
    return dict(b4=b4, d1=d1, rg4=rg4, sig=sig, trades=trades, atr=atr, bl4=bl4)


print(f"加载 {len(ALL11)} 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL11}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL11}
print("加载完成\n", flush=True)


def old_bseq(c):
    """旧版：RANGE 一律 1.0"""
    return [BT.BASE.get(DATA[c]["rg4"][k], 0.0) for k in range(len(DATA[c]["rg4"]))]


def new_bseq(c):
    """新版：base_series()（RANGE 系数读配置，闸门关闭）"""
    D = DATA[c]
    return BT.base_series(D["b4"], D["d1"], D["rg4"])


def run(bseq_of, rebal_weights=True):
    """单币独立净值 + 组合净值。返回 (组合曲线, {币: 单币曲线})。"""
    NAVS = {}
    for c in ALL11:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = bseq_of(c)
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = b[k] + BT.ADD * sig[k]
            elif rg == "RANGE":
                p = b[k]
            elif rg == "DOWN":
                p = BT.ADD * sig[k]
            else:
                p = 0.0
            pv.append(p * coef)
        qv = PC.CAP_4H_WORST.get(c) or 1e12
        w = W.get(c, 0.09)
        nav, res = 1.0, {}
        k0 = next((k for k, x in enumerate(b4) if x["date"] >= START_NAV), len(b4))
        for k in range(k0 + 1, len(b4)):
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pv[k] * r
            nav *= x if x > 0 else 0.0
            base_k = b[k] * COEF[c][k] * VT.KAPPA
            sl = pv[k] - base_k if rg4[k] != "DOWN" else pv[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pv[k] != pv[k - 1]:
                d = abs(pv[k] - pv[k - 1])
                nav *= (1 - BT.COST * d)
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * d / qv) * d)
            res[b4[k]["date"]] = nav
        NAVS[c] = res
    FIRST = {c: min(NAVS[c]) for c in ALL11}
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL11]))
    for dt_ in ALLD:
        live = [c for c in ALL11 if FIRST[c] <= dt_]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if dt_ in NAVS[c]:
                last[c] = NAVS[c][dt_]
        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)
        q = (int(dt_[:4]), (int(dt_[5:7]) - 1) // 3)
        if (cq is None) or (q != cq) or (set(units) != set(live)):
            wsum = sum(uw.values())
            if units:
                g = sum(abs(v * uw[c] - units.get(c, 0.0) * last[c])
                        for c in live) / v / 2
                v *= (1 - min(g, 1.0) * 7e-4)
            cash = v * max(0.0, 1.0 - wsum)
            units = {c: v * uw[c] / last[c] for c in live}
            cq = q
        out.append((dt_, v))
    return out, NAVS


def year_stats(cv):
    """逐年：收益率（口径 A）、年内最大回撤、Calmar（用复利年化）。"""
    out = {}
    for y in YEARS:
        v = [p for p in cv if p[0][:4] == str(y)]
        if len(v) < 5:
            continue
        v0, v1 = v[0][1], v[-1][1]
        ret = v1 / v0 - 1
        pk, dd = v0, 0.0
        for _, x in v:
            pk = max(pk, x)
            dd = min(dd, x / pk - 1)
        out[y] = {"ret": ret, "dd": dd, "days": len(v)}
    return out


print("=" * 104)
print("最新策略逐年表现（RANGE 底仓 UP=%.2f / DOWN=%.2f）"
      % (PC.RANGE_RATIO_BY_SRC["UP"], PC.RANGE_RATIO_BY_SRC["DOWN"]))
print("=" * 104)
cv_new, navs_new = run(new_bseq)
cv_old, _ = run(old_bseq)
ys_new = year_stats(cv_new)
ys_old = year_stats(cv_old)

# ---- 【1】逐年 ----
print("\n【1】逐年（口径 A：本金恒 50 万，每年提走，收益率简单相加）")
print("-" * 104)
print(pad("年份", 8) + pad("收益", 10) + pad("年内回撤", 11) + pad("盈亏(万元)", 12)
      + pad("自然日", 9) + pad("备注", 20))
print("-" * 104)
tot = 0.0
for y in YEARS:
    if y not in ys_new:
        continue
    r = ys_new[y]
    pnl = CAP * r["ret"]
    tot += pnl
    note = ""
    if y == 2020:
        note = "仅 4 个月（9 月起）"
    elif y == 2026:
        note = "至 11 月，不完整"
    print(pad(y, 8) + pad("%+.1f%%" % (r["ret"] * 100), 10)
          + pad("%.1f%%" % (r["dd"] * 100), 11)
          + pad("%+.1f" % (pnl / 1e4), 12) + pad(r["days"], 9) + pad(note, 20))
print("-" * 104)
print(pad("合计", 8) + pad("—", 10) + pad("—", 11) + pad("%+.0f" % (tot / 1e4), 12))

# 全期指标
# 坑：cv 是按「12 币日期并集」建的，每天一条 → len(cv) 是**自然日**不是交易日。
# 用 len/252 会把 6.2 年算成 9.0 年，复利年化与 Calmar 全被低估。
# 必须用日期差。（历史实验脚本 _exp_range_src.py 也踩了这个坑，
#   但因为是同一口径横向比较，排序不受影响；绝对值要按这里重算。）
import datetime as _dt
_y0 = _dt.date.fromisoformat(cv_new[0][0])
_y1 = _dt.date.fromisoformat(cv_new[-1][0])
yrs = (_y1 - _y0).days / 365.25
peak = mdd = 0.0
pk = cv_new[0][1]
for _, v in cv_new:
    pk = max(pk, v)
    mdd = min(mdd, v / pk - 1)
cagr = (cv_new[-1][1] / cv_new[0][1]) ** (1 / yrs) - 1
print(f"\n  全期（{cv_new[0][0]} ~ {cv_new[-1][0]}，{yrs:.2f} 年）")
print(f"    ① 口径 A 累计盈亏 {tot / 1e4:+.0f} 万　← 主口径，可与上表逐年相加核对")
print(f"    ② 本曲线（口径 C：权重恒 50 万却跑全复利）")
print(f"       最大回撤 {mdd * 100:.1f}%   复利年化 {cagr * 100:+.1f}%   "
      f"Calmar {cagr / abs(mdd):.2f}")
print(f"       ← **Calmar 与复利年化取自此曲线，仅用于和别的规则横向比较风险效率；")
print(f"         绝对值不可实现**（净值涨到 {cv_new[-1][1] * CAP / 1e4:.0f} 万时单笔仍是 50 万量）")
print(f"    ③ 口径 B（全复利 + 每年按当年本金重算权重，受容量约束）")
print(f"       10.34×  年化 +45.5%   最大回撤 −37.1%   Calmar 1.22　← 诚实口径")
print(f"       （_exp_cum_b.py 可重跑；2026-11-19 的旧值 5.82× 已作废）")

# ---- 【2】近 N 年 ----
print("\n【2】近 N 年（口径 A：各年收益简单相加，与上表同口径）")
print("-" * 104)
print(pad("区间", 18) + pad("累计盈亏", 12) + pad("简单相加收益", 14)
      + pad("区间回撤", 11))
print("-" * 104)
for lab, a, b in (("2026（今年）", 2026, 2026), ("2025-2026", 2025, 2026),
                  ("2024-2026（近3年）", 2024, 2026),
                  ("2022-2026（近5年）", 2022, 2026),
                  ("2021-2026", 2021, 2026)):
    ys_in = [y for y in YEARS if a <= y <= b and y in ys_new]
    if not ys_in:
        continue
    pnl = sum(CAP * ys_new[y]["ret"] for y in ys_in)
    sret = sum(ys_new[y]["ret"] for y in ys_in)
    v = [p for p in cv_new if a <= int(p[0][:4]) <= b]
    pk3, dd3 = v[0][1], 0.0
    for _, x in v:
        pk3 = max(pk3, x)
        dd3 = min(dd3, x / pk3 - 1)
    print(pad(lab, 18) + pad("%+.0f 万" % (pnl / 1e4), 12)
          + pad("%+.1f%%" % (sret * 100), 14) + pad("%.1f%%" % (dd3 * 100), 11))

# ---- 【3】与旧版对照 ----
print("\n【3】与旧版对照（旧版 = RANGE 一律 1.0）")
print("-" * 104)
print(pad("年份", 8) + pad("旧版", 11) + pad("新版", 11) + pad("差额", 11)
      + pad("旧版回撤", 11) + pad("新版回撤", 11))
print("-" * 104)
for y in YEARS:
    if y not in ys_new or y not in ys_old:
        continue
    a, b = ys_old[y], ys_new[y]
    print(pad(y, 8) + pad("%+.1f%%" % (a["ret"] * 100), 11)
          + pad("%+.1f%%" % (b["ret"] * 100), 11)
          + pad("%+.1fpp" % ((b["ret"] - a["ret"]) * 100), 11)
          + pad("%.1f%%" % (a["dd"] * 100), 11) + pad("%.1f%%" % (b["dd"] * 100), 11))
pnl_old = sum(CAP * ys_old[y]["ret"] for y in YEARS if y in ys_old)
print("-" * 104)
print(f"  旧版累计 {pnl_old / 1e4:+.0f} 万   新版 {tot / 1e4:+.0f} 万   "
      f"差 {(tot - pnl_old) / 1e4:+.0f} 万")

# ---- 【4】分币种 ----
print("\n【4】分币种独立净值（各自 1.0 起，同规则同权重口径）")
print("-" * 104)
rows = []
for c in ALL11:
    v = navs_new[c]
    ds = sorted(v)
    mult = v[ds[-1]]
    pk2, dd2 = 1.0, 0.0
    for d in ds:
        pk2 = max(pk2, v[d])
        dd2 = min(dd2, v[d] / pk2 - 1)
    rows.append((c, mult, dd2, ds[0], ds[-1]))
rows.sort(key=lambda x: -x[1])
print(pad("币", 8) + pad("净值倍数", 12) + pad("最大回撤", 11) + pad("数据起点", 13))
print("-" * 104)
for c, m, dd2, d0, d1 in rows:
    print(pad(c, 8) + pad("%.2f×" % m, 12) + pad("%.1f%%" % (dd2 * 100), 11)
          + pad(d0, 13))
pos = sum(1 for c, m, *_ in rows if m > 1)
print(f"\n  正收益 {pos}/{len(rows)} 个；中位倍数 "
      f"{sorted(m for _c, m, *_ in rows)[len(rows) // 2]:.2f}×")

# ---- 【5】口径 B ----
print("\n【5】口径对照（三个数别混）")
print("-" * 104)
print(f"  口径 A（本金恒 50 万、每年提走、收益率简单相加）")
print(f"    7 年累计 {tot / 1e4:+.0f} 万　← 本报告主口径，与全部历史结论一致")
print(f"  口径 C（固定 50 万权重跑全复利）")
print(f"    净值终值 {cv_new[-1][1]:.1f}× —— **这是幻觉，不能用**：")
print(f"    它假设净值涨到 {cv_new[-1][1] * CAP / 1e4:.0f} 万时单笔仍是 50 万的量，")
print(f"    实际冲击会超红线十倍以上（详见 portfolio_config 注释）。")
print(f"  口径 B（全复利 + 每年按当年本金重算权重，受容量约束）")
print(f"    10.34×、年化 +45.5%、最大回撤 −37.1%、Calmar 1.22（2026-11-27 去 DOGE 后）")
print(f"    ← 诚实口径，本脚本未重算，直接引用 _exp_cum_b.py")
print(f"    与口径 C 的唯一差别：净值涨上去后实际投入比例从 2020 的 85.9% 衰减到"
      f" 2026 的 13.9%，")
print(f"    86% 的钱被迫变现金 → 少赚 63.5% 但回撤也浅了三成（−37.1% vs −54.6%）")

# ---- 【6】风险 ----
print("\n【6】关键风险指标")
print("-" * 104)
neg = [(y, ys_new[y]["ret"]) for y in ys_new if ys_new[y]["ret"] < 0]
print(f"  亏损年：{len(neg)} 个" + (("（" + "、".join(f"{y} {r * 100:+.1f}%"
                                                    for y, r in neg) + "）") if neg else ""))
print(f"  最差单年：{min(ys_new.items(), key=lambda kv: kv[1]['ret'])[0]} "
      f"{min(v['ret'] for v in ys_new.values()) * 100:+.1f}%")
print(f"  最好单年：{max(ys_new.items(), key=lambda kv: kv[1]['ret'])[0]} "
      f"{max(v['ret'] for v in ys_new.values()) * 100:+.1f}%")
yrs_pos = sum(1 for v in ys_new.values() if v["ret"] > 0)
print(f"  年度胜率：{yrs_pos}/{len(ys_new)} = {yrs_pos / len(ys_new):.0%}")
# 恢复期：从最大回撤底部到再创新高。用自然日折算，不是 bar 数。
# 坑：不能「边扫边更新 peak_i」—— 那样 peak_i 会永远停在最后一次创新高，
#     回撤起点被算错（曾把 308 天报成 1426 天）。必须锁定「产生最大回撤的那个峰」。
hwm2, mdd2, pk_at2, tr_i2 = cv_new[0][1], 0.0, 0, 0
pk_i2 = 0
for i, (_, v) in enumerate(cv_new):
    if v > hwm2:
        hwm2, pk_i2 = v, i
    if v / hwm2 - 1 < mdd2 + 1e-12:
        mdd2, pk_at2, tr_i2 = v / hwm2 - 1, pk_i2, i
rec = None
for i in range(tr_i2, len(cv_new)):
    if cv_new[i][1] >= cv_new[pk_at2][1] * (1 - 1e-9):
        rec = i
        break
if rec is None:
    print(f"  最大回撤 {mdd2*100:.1f}%（{cv_new[pk_at2][0]} → {cv_new[tr_i2][0]}）：至今未恢复")
else:
    d0 = _dt.date.fromisoformat(cv_new[tr_i2][0])
    d1 = _dt.date.fromisoformat(cv_new[rec][0])
    ddown = (_dt.date.fromisoformat(cv_new[tr_i2][0])
             - _dt.date.fromisoformat(cv_new[pk_at2][0])).days
    print(f"  最大回撤 {mdd2*100:.1f}%：峰 {cv_new[pk_at2][0]} → 谷 {cv_new[tr_i2][0]}"
          f"（{ddown} 天）→ 恢复 {cv_new[rec][0]}（{(d1-d0).days} 天 / {(d1-d0).days/365.25:.1f} 年）")
    print(f"    全程 {ddown + (d1-d0).days} 天 ≈ {((d1-d0).days + ddown)/365.25:.1f} 年")
