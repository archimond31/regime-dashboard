#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_cum_b.py —— 重算「口径 B」：全复利 + 每年按当年本金重算权重

为什么要这个脚本：
    口径 B 一直是 STRATEGY.md 里对外报收益的那一档，但原值是 5.82×，
    出自 2026-11-19 版（RANGE 按来路、DOWN=0），且原计算脚本没有保留。
    之后 RANGE 底仓系数改到 0.5，口径 A 从 261 万变成 291 万，口径 B 必须同步重算。
    **本脚本就是那次重算**，结果是 10.65× / +46.3% / −36.1% / Calmar 1.28，
    已写回 STRATEGY.md §0 与 §5.4。以后改任何影响收益的配置，重跑这个脚本即可。

口径定义（严格按 STRATEGY.md §5.4 的表述）：
    · 全复利：净值滚存，不每年提走
    · 每年 1 月 1 日（首个交易日）按**当年净值**重新调用 weights_for()
      → 净值越大，容量约束越紧，Σw 越小，差额变成现金
    · 权重重算产生换手，按 COST 量级计费

三臂（用来隔离「容量约束」到底值多少）：
    B_full  年度再平衡 + 权重随净值重算   ← 这就是口径 B
    B_fix   年度再平衡 + 权重恒按 50 万算  ← 只差「权重是否随净值变」
    B_q     季度再平衡 + 权重恒按 50 万算  ← 对照，应与现有口径 C 复现

自检：B_q 的结果必须接近 _report_latest.py 输出的口径 C（11 币约 25.1×/−54.6%，
      12 币时代是 28.0×/−54.9%）。改币种组合后这个对照值必须同步改。
      对不上说明本脚本的组合层写错了，不能相信 B_full。

用法：python _exp_cum_b.py
"""
import math
import os
import sys
import datetime as _dt

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
# 2026-11-27 去 DOGE：12 币改为 11 币。**自检基准必须同步改**——
# 口径 C 的对照值也跟着变（28.0× → 25.1×），否则本脚本会永远报「✗ 偏离」，
# 而你无法分辨那是币种变少导致的、还是组合层真的写坏了。
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "NEAR"]
SWAP_COST = 7e-4          # 与 _report_latest.py 口径 C 的再平衡成本一致


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


print(f"加载 {len(ALL12)} 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
print("加载完成\n", flush=True)


def coin_navs():
    """每个币的独立净值曲线（含资金费、成本、冲击）。与 _report_latest.py 同源。"""
    NAVS = {}
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = BT.base_series(b4, DATA[c]["d1"], rg4)
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
        w50 = PC.weights_for(CAP).get(c, 0.09)
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
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w50, 1e-6) * d / qv) * d)
            res[b4[k]["date"]] = nav
        NAVS[c] = res
    return NAVS


NAVS = coin_navs()
FIRST = {c: min(NAVS[c]) for c in ALL12}
ALLD = sorted(set().union(*[set(NAVS[c]) for c in ALL12]))


def run(dynamic_w, freq):
    """组合净值曲线。

    dynamic_w: True = 每次再平衡按当前净值重算权重（口径 B）
               False = 权重恒按 50 万算（口径 C 的行为）
    freq:      "Y" 年度 / "Q" 季度
    """
    last, units, cash, out = {}, {}, 0.0, []
    cur_bucket = None
    log = []

    for d in ALLD:
        live = [c for c in ALL12 if FIRST[c] <= d]
        if not live:
            continue
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]

        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)

        if freq == "Y":
            bucket = int(d[:4])
        else:
            bucket = (int(d[:4]), (int(d[5:7]) - 1) // 3)

        need = (cur_bucket is None) or (bucket != cur_bucket) or (set(units) != set(live))
        if need:
            # 用「当前净值对应的本金」查权重；固定模式恒用 50 万
            cap_now = CAP * v if dynamic_w else CAP
            W_now = PC.weights_for(cap_now)
            # TGT 必须取**全量 12 币**的 Σw，不能取 live 子集的和 ——
            # 否则早期只有 3~5 个币上市时，权重不会按比例放大，大量资金空转。
            # _report_latest.py 用的就是这个口径（TGT 全局、sw 只算 live、商放大）。
            tgt = min(1.0, sum(W_now.values()))
            sw = sum(W_now.get(c, 0.0) for c in live)
            uw = {c: W_now.get(c, 0.0) / sw * tgt for c in live} if sw > 0 else {}
            if units:
                g = sum(abs(v * uw.get(c, 0.0) - units.get(c, 0.0) * last[c])
                        for c in live) / v / 2
                v_before = v
                v *= (1 - min(g, 1.0) * SWAP_COST)
                log.append(dict(date=d, v_before=v_before, v_after=v, turnover=g,
                                cap=cap_now, sum_w=sw, cash=1 - tgt))
            else:
                log.append(dict(date=d, v_before=v, v_after=v, turnover=0.0,
                                cap=cap_now, sum_w=sw, cash=1 - tgt))
            cash = v * max(0.0, 1.0 - tgt)
            # 不要写 `if uw.get(c)` —— 权重变成 0 的币会被跳过，旧仓位残留不清。
            units = {c: v * uw.get(c, 0.0) / last[c] for c in live}
            cur_bucket = bucket

        out.append((d, v))
    return out, log


def stat(cv):
    d0 = _dt.date.fromisoformat(cv[0][0])
    d1 = _dt.date.fromisoformat(cv[-1][0])
    yrs = (d1 - d0).days / 365.25
    mult = cv[-1][1] / cv[0][1]
    cagr = mult ** (1 / max(yrs, 1e-9)) - 1
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return dict(mult=mult, cagr=cagr, dd=dd, calmar=cagr / abs(dd) if dd else 0,
                yrs=yrs, end=cv[-1][1])


def yearly(cv):
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    return {y: lastv[y] / first[y] - 1 for y in first}


# ============================================================
print("=" * 104)
print("【1】三臂对照")
print("=" * 104)
ARMS = [
    ("B_full 年度+动态权重（口径B）", True, "Y"),
    ("B_fix  年度+固定50万权重", False, "Y"),
    ("B_q    季度+固定50万权重（自检）", False, "Q"),
]
RES = {}
print(pad("臂", 32) + pad("倍数", 11) + pad("年化", 10) + pad("最大回撤", 11)
      + pad("Calmar", 9) + pad("期末净值", 13))
print("-" * 104)
for nm, dyn, fq in ARMS:
    cv, lg = run(dyn, fq)
    s = stat(cv)
    RES[nm] = dict(cv=cv, log=lg, **s)
    print(pad(nm, 32) + pad("%.2f×" % s["mult"], 11)
          + pad("%+.1f%%" % (s["cagr"] * 100), 10)
          + pad("%.1f%%" % (s["dd"] * 100), 11)
          + pad("%.2f" % s["calmar"], 9)
          + pad("%.0f 万" % (CAP * s["end"] / 1e4), 13))

print("\n  自检：B_q 应接近 _report_latest.py 的口径 C（11 币约 25.1× / −54.6%）")
bq = RES["B_q    季度+固定50万权重（自检）"]
# 对照值 = _report_latest.py 的口径 C，随币种组合变化。2026-11-27 去 DOGE 后 28.0 → 25.1。
_REF = 25.1
ok = abs(bq["mult"] - _REF) / _REF < 0.10
print(f"    B_q = {bq['mult']:.2f}× / {bq['dd'] * 100:.1f}% → "
      f"{'✓ 复现，脚本可信' if ok else '✗ 偏离，先别信 B_full'}")

# ============================================================
print("\n" + "=" * 104)
print("【2】口径 B 逐年：净值、权重总量、现金比例（这是 B 与 C 的唯一差别）")
print("=" * 104)
bf = RES["B_full 年度+动态权重（口径B）"]
yr = yearly(bf["cv"])
print(pad("年", 7) + pad("年收益", 10) + pad("年末净值", 12) + pad("本金(万)", 12)
      + pad("Σw", 8) + pad("现金", 8) + pad("换手", 9))
print("-" * 104)
by_year = {}
for e in bf["log"]:
    by_year[int(e["date"][:4])] = e
for y in sorted(yr):
    e = by_year.get(y)
    end_v = None
    for d, v in bf["cv"]:
        if int(d[:4]) == y:
            end_v = v
    if e and end_v:
        print(pad(y, 7) + pad("%+.1f%%" % (yr[y] * 100), 10)
              + pad("%.2f" % end_v, 12)
              + pad("%.1f" % (e["cap"] / 1e4), 12)
              + pad("%.1f%%" % (e["sum_w"] * 100), 8)
              + pad("%.1f%%" % (e["cash"] * 100), 8)
              + pad("%.0f%%" % (e["turnover"] * 100), 9))

print("\n  口径 B 的权重衰减（净值涨 → 容量塞不下 → 越来越多钱变现金）：")
print(pad("    年", 10) + pad("本金(万)", 12) + pad("Σw", 9) + pad("现金占比", 11)
      + "说明")
for y in sorted(by_year):
    e = by_year[y]
    note = "满仓分散" if e["sum_w"] >= 0.999 else (
        "容量开始约束" if e["sum_w"] >= 0.5 else "大部分空仓")
    print(pad("    " + str(y), 10) + pad("%.1f" % (e["cap"] / 1e4), 12)
          + pad("%.1f%%" % (e["sum_w"] * 100), 9)
          + pad("%.1f%%" % (e["cash"] * 100), 11) + note)

# ============================================================
print("\n" + "=" * 104)
print("【3】容量约束值多少钱：B_full vs B_fix（唯一差别是权重是否随净值放大）")
print("=" * 104)
bfx = RES["B_fix  年度+固定50万权重"]
print(f"  B_full（真实容量）  {bf['mult']:>7.2f}×   年化 {bf['cagr'] * 100:+.1f}%   "
      f"回撤 {bf['dd'] * 100:.1f}%   Calmar {bf['calmar']:.2f}")
print(f"  B_fix （忽略容量）  {bfx['mult']:>7.2f}×   年化 {bfx['cagr'] * 100:+.1f}%   "
      f"回撤 {bfx['dd'] * 100:.1f}%   Calmar {bfx['calmar']:.2f}")
print(f"  → 容量约束吃掉 {(1 - bf['mult'] / bfx['mult']) * 100:.1f}% 的最终净值，"
      f"但回撤从 {bfx['dd'] * 100:.1f}% 变成 {bf['dd'] * 100:.1f}%")
print("     （规模变大后仓位被动缩小，等于自带一层降杠杆，所以回撤更浅）")

# ============================================================
print("\n" + "=" * 104)
print("【4】与旧值对照")
print("=" * 104)
print(f"  旧口径 B（2026-11-19 版，RANGE DOWN=0）   5.82×   年化 +32.8%   回撤 −41.6%   Calmar 0.79")
print(f"  新口径 B（2026-11-20 版，RANGE DOWN=0.5） {bf['mult']:.2f}×   "
      f"年化 {bf['cagr'] * 100:+.1f}%   回撤 {bf['dd'] * 100:.1f}%   Calmar {bf['calmar']:.2f}")
print(f"  倍数变化 {bf['mult'] / 5.82 - 1:+.1%}")
print("\n  注：旧值的回撤 −41.6% 小于口径 A 的 −48.2%，因为净值变大后仓位被动缩小；")
print("      新值同理，所以 B 的回撤会比 A 的口径 C 回撤（−54.9%）更浅。")
