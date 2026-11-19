#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_range_ch.py —— 给底仓加一条 RANGE 段的独立风险通道

问题：底仓在 UP/RANGE 都是 1.0，唯一出口是「日线翻 DOWN」。
而 RANGE 段占 25.7% 的时间，其中 192 段（占 48%）随后翻 DOWN，
这些段的平均跌幅 -8.51%，底仓全程 1.0 无保护地吃下去。

设计：**RANGE 段内，用「4H 收盘跌破前 N 根区间下沿」作为独立减仓线。**
  理由 1：与加仓层同构（都是前 LOOKBACK 根极值），不引入新的参数族
  理由 2：经济含义清楚 —— 区间被向下突破 = 支撑失效，不必等日线确认
  理由 3：只作用于 RANGE，不动 UP（趋势里不该砍）和 DOWN（底仓本来就 0）

===== 本轮事先定好的判定标准（避免事后拟合）=====
  ① 前视检查：左移一格后收益不能垮（上一轮 base_gate 就死在这里）
  ② Calmar 必须显著优于基线
  ③ 必须赢过「同等平均底仓水平的恒定折扣」（否则只是变相降仓）
  ④ 时间切分（2020-2023 / 2024-2026）两段都要赢
  ⑤ 滚动 250 日窗口 ≥ 60%
  ⑥ 延迟一根 4H 后不能崩（4H 级风控的真实瓶颈是执行通道）

六段输出：
  【1】主对照：6 臂参数网格
  【2】触发统计：RANGE 内实际触发了多少、平均底仓系数降到多少
  【3】变相降仓对照（按【2】的实测平均值匹配）
  【4】前视检查：平移一格
  【5】延迟敏感度：晚 1 根 4H
  【6】时间切分 + 滚动窗口

口径 A（本金恒 50 万、每年提走、收益率简单相加），与全部历史结论一致。
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
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
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


print("加载 12 币 …", flush=True)
DATA = {c: load_coin(c) for c in ALL12}
COEF = {c: VT.coef_series(DATA[c]["b4"]) for c in ALL12}
print("加载完成\n", flush=True)


def range_channel(b4, rg4, lb=20, cut=0.5, hyst=0.01):
    """逐 bar 的底仓系数（RANGE 段独立通道）。

    只在 RANGE 状态内生效；UP/DOWN 恒为 1.0（UP 不该砍，DOWN 底仓本来 0）。

    规则：RANGE 内 4H 收盘 < 前 lb 根 4H 最低价 → 系数降至 cut；
          收盘 > 下沿 × (1+hyst) → 恢复 1.0（滞回，避免均线附近反复抽动）。

    **末尾必须左移一格**：系数是拿 close[k] 算出来的，只能作用于 bar k+1
    的收益。这一步不做就是前视 —— base_gate 上一轮就死在这里。
    """
    out, cur = [], 1.0
    for k in range(len(b4)):
        if rg4[k] != "RANGE":
            cur = 1.0                    # 离开 RANGE 就复位，下一段重新开始
            out.append(1.0)
            continue
        if k < lb + 1:
            out.append(1.0)
            continue
        lo = min(x["low"] for x in b4[k - lb:k])
        px = b4[k]["close"]
        if px < lo:
            cur = cut
        elif px > lo * (1 + hyst):
            cur = 1.0
        out.append(cur)
    return [1.0] + out[:-1]


def const_rng(b4, rg4, cut=0.75):
    """对照：RANGE 段恒定折扣（无择时），用于「变相降仓」检验。"""
    out = [cut if rg4[k] == "RANGE" else 1.0 for k in range(len(b4))]
    return [1.0] + out[:-1]


def run(bseq_of, al=0.5, as_=0.5, delay=0):
    """bseq_of(c) → 逐 bar 底仓系数（已左移一格）。

    delay>0：模拟执行滞后 d 根 4H，把系数序列再右移 d 格。
    """
    out_curves = {}
    avg_b = []
    for c in ALL12:
        b4, rg4, sig = DATA[c]["b4"], DATA[c]["rg4"], DATA[c]["sig"]
        b = bseq_of(c)
        if delay:
            b = [b[0]] * delay + b[:-delay] if delay < len(b) else [b[0]] * len(b)
        pv = []
        for k in range(len(b4)):
            rg = rg4[k]
            coef = COEF[c][k] * VT.KAPPA
            if rg == "UP":
                p = b[k] + al * sig[k]
            elif rg == "RANGE":
                p = b[k]
                avg_b.append(b[k])
            elif rg == "DOWN":
                p = as_ * sig[k]
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
            sl = pv[k] - b[k] * COEF[c][k] * VT.KAPPA if rg4[k] != "DOWN" else pv[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pv[k] != pv[k - 1]:
                d = abs(pv[k] - pv[k - 1])
                nav *= (1 - BT.COST * d)
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * max(w, 1e-6) * d / qv) * d)
            res[b4[k]["date"]] = nav
        out_curves[c] = res
    FIRST = {c: min(out_curves[c]) for c in ALL12}
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(out_curves[c]) for c in ALL12]))
    for d in ALLD:
        live = [c for c in ALL12 if FIRST[c] <= d]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if d in out_curves[c]:
                last[c] = out_curves[c][d]
        v = 1.0 if not units else cash + sum(units.get(c, 0.0) * last.get(c, 1.0)
                                             for c in live)
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
    return out, (sum(avg_b) / len(avg_b) if avg_b else 1.0)


def stat(cv):
    first, lastv = {}, {}
    for d, v in cv:
        y = int(d[:4])
        first.setdefault(y, v)
        lastv[y] = v
    yr = {y: lastv[y] / first[y] - 1 for y in first}
    cum = sum(CAP * yr.get(y, 0.0) for y in YEARS)
    pk, dd = cv[0][1], 0.0
    for _, v in cv:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    yrs = len(cv) / 252.0
    cagr = (cv[-1][1] / cv[0][1]) ** (1 / max(yrs, 1e-9)) - 1
    return yr, cum, dd, cagr


def sub_stat(cv, a, b):
    v = [p for p in cv if a <= p[0] < b]
    if len(v) < 30:
        return None
    pk, dd = v[0][1], 0.0
    for _, x in v:
        pk = max(pk, x)
        dd = min(dd, x / pk - 1)
    return v[-1][1] / v[0][1] - 1, dd


def roll(cv, win=250):
    return [cv[i + win][1] / cv[i][1] - 1
            for i in range(0, len(cv) - win, 21)]


# ============================================================
# 【1】主对照
# ============================================================
ARMS = [
    ("G0 基线（不开通道）", lambda c: [1.0] * len(DATA[c]["b4"])),
    ("G1 lb20 cut0.5", lambda c: range_channel(DATA[c]["b4"], DATA[c]["rg4"], 20, 0.5)),
    ("G2 lb20 cut0.0", lambda c: range_channel(DATA[c]["b4"], DATA[c]["rg4"], 20, 0.0)),
    ("G3 lb10 cut0.5", lambda c: range_channel(DATA[c]["b4"], DATA[c]["rg4"], 10, 0.5)),
    ("G4 lb40 cut0.5", lambda c: range_channel(DATA[c]["b4"], DATA[c]["rg4"], 40, 0.5)),
    ("G5 lb20 cut0.5 hyst3%", lambda c: range_channel(DATA[c]["b4"], DATA[c]["rg4"], 20, 0.5, 0.03)),
]
print("=" * 112)
print("【1】RANGE 段独立通道 · 六臂主对照（口径 A）")
print("=" * 112)
print(pad("臂", 24) + "".join(pad(y, 8) for y in YEARS)
      + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8) + pad("RANGE底仓", 11))
print("-" * 112)
R = {}
for nm, fn in ARMS:
    cv, ab = run(fn)
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    R[nm] = dict(yr=yr, cum=cum, dd=dd, calmar=cal, cv=cv, ab=ab)
    print(pad(nm, 24) + "".join(pad("%+.0f" % (yr.get(y, 0) * 100), 8) for y in YEARS)
          + pad("%.0f 万" % (cum / 1e4), 11) + pad("%.1f%%" % (dd * 100), 9)
          + pad("%.2f" % cal, 8) + pad("%.2f" % ab, 11))

g0 = R["G0 基线（不开通道）"]
print("\n  相对基线：")
print(pad("臂", 24) + pad("累计差额", 12) + pad("回撤变化", 12) + pad("Calmar变化", 13))
print("-" * 112)
for nm in R:
    if nm.startswith("G0"):
        continue
    r = R[nm]
    print(pad(nm, 24) + pad("%+.0f 万" % ((r["cum"] - g0["cum"]) / 1e4), 12)
          + pad("%+.1fpp" % ((r["dd"] - g0["dd"]) * 100), 12)
          + pad("%+.2f" % (r["calmar"] - g0["calmar"]), 13))

# ============================================================
# 【2】触发统计
# ============================================================
print("\n" + "=" * 112)
print("【2】RANGE 内实际触发比例（这条通道到底多久动一次）")
print("=" * 112)
print(pad("臂", 24) + pad("RANGE根数", 12) + pad("触发根数", 12) + pad("触发占比", 11)
      + pad("RANGE底仓均值", 14) + pad("换手次数", 11))
print("-" * 112)
for nm, fn in ARMS:
    if nm.startswith("G0"):
        continue
    tot = hit = flip = 0
    for c in ALL12:
        D = DATA[c]
        b = fn(c)
        prev = None
        for k in range(len(b)):
            if b[k] != prev and prev is not None:
                flip += 1
            prev = b[k]
            if D["rg4"][k] == "RANGE" and D["b4"][k]["date"][:10] >= START_NAV:
                tot += 1
                if b[k] < 0.999:
                    hit += 1
    print(pad(nm, 24) + pad(f"{tot:,}", 12) + pad(f"{hit:,}", 12)
          + pad("%.1f%%" % (hit / max(tot, 1) * 100), 11)
          + pad("%.2f" % R[nm]["ab"], 14) + pad(flip, 11))

# ============================================================
# 【3】变相降仓对照
# ============================================================
print("\n" + "=" * 112)
print("【3】变相降仓对照：同等平均底仓水平下，择时是否还有超额")
print("=" * 112)
print(pad("臂", 24) + pad("累计", 11) + pad("回撤", 9) + pad("Calmar", 8))
print("-" * 112)
for nm in ("G0 基线（不开通道）", "G1 lb20 cut0.5", "G4 lb40 cut0.5"):
    r = R[nm]
    print(pad(nm, 24) + pad("%.0f 万" % (r["cum"] / 1e4), 11)
          + pad("%.1f%%" % (r["dd"] * 100), 9) + pad("%.2f" % r["calmar"], 8))
print("  " + "-" * 70)
for nm, key in (("G1 lb20 cut0.5", "G1 lb20 cut0.5"), ("G4 lb40 cut0.5", "G4 lb40 cut0.5")):
    ab = R[key]["ab"]
    cnm = f"C 恒定 {ab:.2f}"
    cv, _ = run(lambda c, v=ab: const_rng(DATA[c]["b4"], DATA[c]["rg4"], v))
    yr, cum, dd, cagr = stat(cv)
    cal = cagr / abs(dd) if dd < 0 else 0
    R[cnm] = dict(yr=yr, cum=cum, dd=dd, calmar=cal, cv=cv, ab=ab)
    print(pad(cnm, 24) + pad("%.0f 万" % (cum / 1e4), 11)
          + pad("%.1f%%" % (dd * 100), 9) + pad("%.2f" % cal, 8))
    d = R[key]["cum"] - cum
    print(f"    → {key} 相对同水平恒定折扣：{d / 1e4:+.0f} 万，"
          f"Calmar {R[key]['calmar']:.2f} vs {cal:.2f}")

# ============================================================
# 【4】前视检查
# ============================================================
print("\n" + "=" * 112)
print("【4】前视检查：把通道输出整体右移一格（模拟「今天才知道昨天的信号」）")
print("=" * 112)
print(pad("臂", 24) + pad("原样", 11) + pad("右移1格", 11) + pad("差额", 11)
      + pad("判定", 12))
print("-" * 112)
for nm, fn in ARMS[1:]:
    a = R[nm]["cum"]
    cv_b, _ = run(fn, delay=1)
    _yr, b, _dd, _cg = stat(cv_b)
    d = (a - b) / 1e4
    verdict = "前视嫌疑" if d > 30 else ("安全" if abs(d) < 15 else "轻微")
    print(pad(nm, 24) + pad("%.0f 万" % (a / 1e4), 11) + pad("%.0f 万" % (b / 1e4), 11)
          + pad("%+.0f 万" % d, 11) + pad(verdict, 12))
print("  经验：base_gate 上一轮「右移一格」掉 101 万，判定为前视，全部作废。")

# ============================================================
# 【5】延迟敏感度
# ============================================================
print("\n" + "=" * 112)
print("【5】延迟敏感度：执行晚 1 根 4H（4H 级风控的真实瓶颈）")
print("=" * 112)
print(pad("臂", 24) + pad("D=0", 11) + pad("D=1", 11) + pad("差额", 11)
      + pad("Calmar D=1", 12))
print("-" * 112)
for nm, fn in ARMS[1:]:
    cv_b, _ = run(fn, delay=1)
    yr, cum, dd, cagr = stat(cv_b)
    cal = cagr / abs(dd) if dd < 0 else 0
    print(pad(nm, 24) + pad("%.0f 万" % (R[nm]["cum"] / 1e4), 11)
          + pad("%.0f 万" % (cum / 1e4), 11)
          + pad("%+.0f 万" % ((cum - R[nm]["cum"]) / 1e4), 11)
          + pad("%.2f" % cal, 12))

# ============================================================
# 【6】时间切分 + 滚动
# ============================================================
print("\n" + "=" * 112)
print("【6】时间切分与滚动窗口")
print("=" * 112)
SPLIT = "2024-01-01"
print(pad("臂", 24) + pad("2020-2023", 13) + pad("2024-2026", 13)
      + pad("滚动胜基线", 12) + pad("中位差", 11) + pad("最差", 10))
print("-" * 112)
base_roll = roll(g0["cv"])
for nm in R:
    if nm.startswith("G0") or nm.startswith("C "):
        continue
    r = R[nm]
    a = sub_stat(r["cv"], "2020-01-01", SPLIT)
    b = sub_stat(r["cv"], SPLIT, "2027-01-01")
    rs = roll(r["cv"])
    m = min(len(rs), len(base_roll))
    dif = sorted(rs[i] - base_roll[i] for i in range(m))
    win = sum(1 for x in dif if x > 0)
    print(pad(nm, 24)
          + pad("%+.0f%%" % (a[0] * 100) if a else "—", 13)
          + pad("%+.0f%%" % (b[0] * 100) if b else "—", 13)
          + pad(f"{win}/{m}", 12)
          + pad("%+.1fpp" % (dif[m // 2] * 100), 11)
          + pad("%+.1fpp" % (dif[0] * 100), 10))
print("\n  注：2020-2023 含 2022 大熊；2024-2026 含 2024 牛市与 2025 回撤。")
print("  两段都要赢，才算不是「只在一个 regime 里有效」。")
