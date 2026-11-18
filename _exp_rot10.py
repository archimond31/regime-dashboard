#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_rot10.py —— 年度轮动：仍是 10 个币，但每年重选一次

问题：固定 10 币等于「2021 年凭直觉选一次，然后用 5 年」。本脚本检验
「每年按规则重选 10 个」是否比固定更好，以及哪条规则真的有信息含量。

选币规则（全部只用选币日之前 365 天的数据，零前视）：
  MOM      价格涨幅
  RISKADJ  涨幅 / 年化波动（夏普式）
  TREND    收盘在 MA120 上方天数占比 —— 衡量趋势连续性，最贴策略逻辑
  SLEEVE   该币跑本策略的 sleeve 净值涨幅 —— 策略适配度
  AVGR     该币每笔交易平均 R
  MIX      TREND + RISKADJ 排名均值

基准：
  FIX10    现状固定 10 币
  RANDOM   每年在该年可选池内随机抽 10 个（200 次，报分布）

口径：季度再平衡、含手续费/资金费/滑点、叠 VT60(κ=1.22)、等权 1/10。
换币成本按实际换手计（双边 7bp，与季度再平衡同口径）。

已知偏差：候选池是「2026 年仍在交易」的币 → 幸存者偏差，2022 年选不到
LUNA/FTT 这类后来归零的币。结论要带着这个折扣看。
"""
import bisect
import csv
import io
import math
import os
import random
import sys
import zipfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import CACHE, SYMBOLS, load_bars, to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h

CAP = 500_000
F4 = BT.FUND_DAILY / 6.0
CACHE_BIN = ".cache_binance"
W = 0.1                      # 每年 10 币等权
RT = 7e-4                    # 双边成本（再平衡 / 换币共用）
START_EVAL = "2022-01-01"
FIX10 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE", "HYPE"]
POOL = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE",
        "DOGE", "NEAR", "ADA", "SAND", "AVAX", "LTC", "XLM", "TRX", "ATOM",
        "ALGO", "DOT", "HYPE"]


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


# ---------------- 数据 ----------------
def load_coin(c):
    if c == "HYPE":
        b4 = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
        d1 = [dict(x, date=x["date"][:10]) for x in load_hype("1d")]
    else:
        b = load_bars(c, "1h", tail=False)
        b4 = agg4h(b)
        d1 = to_daily(b)
    if "date" not in b4[0]:
        raise SystemExit("b4 缺 date 字段")
    reg, blood = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg)
    bl4 = align_regime_safe(b4, blood)
    atr = atr_wilder(b4, 14)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]

    # 日线：同一天多根时取最后一根；同时算 MA120
    byday = {}
    for x in d1:
        byday[x["date"][:10]] = x["close"]
    ds = sorted(byday)
    px = [byday[d] for d in ds]
    ma, run = [], 0.0
    for i in range(len(px)):
        run += px[i]
        if i >= 120:
            run -= px[i - 120]
        ma.append(run / 120 if i >= 119 else None)
    daily = [dict(date=ds[i], close=px[i], ma=ma[i]) for i in range(len(ds))]
    return b4, base, pos, trades, daily


def worst_4h_qv(sym, months=3):
    files = sorted(f for f in os.listdir(CACHE_BIN) if f.startswith(sym + "-1h-"))
    if not files:
        return None
    buckets = {}
    for fn in files[-months:]:
        try:
            with zipfile.ZipFile(os.path.join(CACHE_BIN, fn)) as z:
                with z.open(z.namelist()[0]) as fh:
                    for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                        if not row or not row[0].isdigit():
                            continue
                        ts = int(row[0])
                        if ts > 1e14:
                            ts //= 1000
                        q = float(row[7]) if len(row) > 7 else 0.0
                        buckets[ts // (4 * 3600 * 1000)] = \
                            buckets.get(ts // (4 * 3600 * 1000), 0.0) + q
        except Exception:                       # noqa: BLE001
            continue
    if not buckets:
        return None
    v = sorted(buckets.values())
    return v[max(int(len(v) * 0.01), 0)]


_QVM = {}


def month_qv(sym, ym):
    """该月 1H 数据的 quote volume 合计（USD）。没有该月文件返回 None。"""
    key = (sym, ym)
    if key in _QVM:
        return _QVM[key]
    path = os.path.join(CACHE_BIN, f"{sym}-1h-{ym}.zip")
    if not os.path.exists(path):
        _QVM[key] = None
        return None
    tot = 0.0
    try:
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as fh:
                for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                    if not row or not row[0].isdigit():
                        continue
                    tot += float(row[7]) if len(row) > 7 else 0.0
    except Exception:                           # noqa: BLE001
        _QVM[key] = None
        return None
    _QVM[key] = tot
    return tot


def adv_before(c, as_of, months=3):
    """选币日之前 months 个完整月的日均成交额。无前视。"""
    if c not in SYMBOLS:                   # HYPE 走 Hyperliquid，没有 Binance 归档
        return None
    y, m = int(as_of[:4]), int(as_of[5:7])
    tot, n = 0.0, 0
    for k in range(1, months + 1):
        mm = m - k
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        q = month_qv(SYMBOLS[c], f"{yy:04d}-{mm:02d}")
        if q:
            tot += q
            n += 1
    return tot / (n * 30.0) if n else None


print("加载 21 币 …", flush=True)
P, QV = {}, {}
for c in POOL:
    b4, base, pos, trades, daily = load_coin(c)
    P[c] = dict(b4=b4, base=base, pos=pos, trades=trades, d1=daily)
    QV[c] = PC.CAP_4H_WORST.get("HYPE") if c == "HYPE" else worst_4h_qv(SYMBOLS[c])
    print(f"  {pad(c,5)} bars {len(b4):6d}  trades {len(trades):4d}  "
          f"daily {len(daily):5d}  qv {QV[c] or 0:,.0f}", flush=True)

COEF = {c: VT.coef_series(P[c]["b4"]) for c in POOL}
POSV = {c: [p * k * VT.KAPPA for p, k in zip(P[c]["pos"], COEF[c])] for c in POOL}


def get_nav(c):
    """该币在恒定 10% 权重下的 sleeve 净值（VT60 后）。只算一次，所有规则共用。"""
    b4, base = P[c]["b4"], P[c]["base"]
    pos, qv = POSV[c], QV[c] or 1e12
    out, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= "2020-09-01"), len(b4))
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = pos[k] - base[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)
            nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * W * d / qv) * d)
        out[b4[k]["date"]] = nav
    return out


NAV = {c: get_nav(c) for c in POOL}
NAVK = {c: sorted(NAV[c]) for c in POOL}


def nav_before(c, d):
    ks = NAVK[c]
    i = bisect.bisect_right(ks, d) - 1
    return NAV[c][ks[i]] if i >= 0 else 1.0


# ---------------- 选币指标 ----------------
def metrics(c, as_of):
    D = P[c]
    ad = date.fromisoformat(as_of)
    lo = (ad - timedelta(days=365)).isoformat()
    win = [x for x in D["d1"] if lo < x["date"] <= as_of]
    if len(win) < 300 or win[0]["close"] <= 0:
        return None
    mom = win[-1]["close"] / win[0]["close"] - 1
    rets = [math.log(win[i]["close"] / win[i - 1]["close"]) for i in range(1, len(win))]
    mu = sum(rets) / len(rets)
    var = sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)
    vol = math.sqrt(var) * math.sqrt(365)
    above = sum(1 for x in win if x["ma"] and x["close"] > x["ma"])
    n0, n1 = nav_before(c, lo), nav_before(c, as_of)
    tr = [t for t in D["trades"] if lo < t["dx"] <= as_of]
    return dict(mom=mom, vol=vol, riskadj=(mom / vol if vol > 1e-9 else -9.9),
                trend=above / len(win), sleeve=n1 / n0 - 1,
                avgr=(sum(t["r"] for t in tr) / len(tr) if tr else 0.0), n=len(tr),
                adv=adv_before(c, as_of) or 0.0)


YEARS = [2022, 2023, 2024, 2025, 2026]
MET = {}
for y in YEARS:
    asof = f"{y - 1}-12-31"
    MET[y] = {c: metrics(c, asof) for c in POOL}
    MET[y] = {c: m for c, m in MET[y].items() if m}

KEY = {"MOM": "mom", "RISKADJ": "riskadj", "TREND": "trend",
       "SLEEVE": "sleeve", "AVGR": "avgr"}
RULES = ["MOM", "RISKADJ", "TREND", "SLEEVE", "AVGR", "MIX", "REV", "STICKY", "LIQ", "FLIQ"]


def _sticky_series(topn=10, buffer=15):
    """流动性选币 + 缓冲区：已持有的币只要还在前 buffer 名就不换。降低无谓换手。"""
    picks, prev = {}, None
    for y in YEARS:
        order = [c for c, _m in sorted(MET[y].items(), key=lambda kv: -kv[1]["adv"])]
        if prev is None:
            cur = order[:topn]
        else:
            keep = [c for c in prev if c in order[:buffer]]
            rest = [c for c in order if c not in keep]
            cur = (keep + rest)[:topn]
        picks[y] = cur
        prev = cur
    return picks


STICKY = _sticky_series()


def pick(rule, y):
    pool = MET[y]
    if not pool:
        return []
    if rule == "STICKY":
        return STICKY[y]
    if rule == "MIX":
        rt = {c: r for r, (_c, _m) in enumerate(sorted(pool.items(), key=lambda kv: -kv[1]["trend"])) for c in [_c]}
        rr = {c: r for r, (_c, _m) in enumerate(sorted(pool.items(), key=lambda kv: -kv[1]["riskadj"])) for c in [_c]}
        order = sorted(pool, key=lambda c: rt[c] + rr[c])
        return order[:10]
    if rule == "FLIQ":                     # 2021 年底按流动性选一次，之后永不换
        pool0 = MET[YEARS[0]]
        return [c for c, _m in sorted(pool0.items(), key=lambda kv: -kv[1]["adv"])][:10]
    if rule == "LIQ":                      # 每年选「过去 90 天日均成交额」前 10（无前视）
        return [c for c, _m in sorted(pool.items(), key=lambda kv: -kv[1]["adv"])][:10]
    if rule == "REV":                      # 反向：选去年跌得最多的 10 个
        return [c for c, _m in sorted(pool.items(), key=lambda kv: kv[1]["mom"])][:10]
    k = KEY[rule]
    return [c for c, _m in sorted(pool.items(), key=lambda kv: -kv[1][k])][:10]


# ---------------- 组合 ----------------
ALLD = sorted(set().union(*[set(NAVK[c]) for c in POOL]))


def rot_curve(picks, start=START_EVAL, end=None):
    """picks: {year: [10 coins]}。按年切换，年内季度再平衡。

    返回 (总收益, 最大回撤, 换手日志, 逐日净值序列)。
    注意：val 必须每个 bar 跟着 units 一起更新，否则换年时会把净值打回 1.0。
    """
    dates = [d for d in ALLD if d >= start and (end is None or d < end)]
    if not dates:
        return None, None, [], []
    ys = sorted(picks)
    units, last = {}, {}
    cy, cq, turnover_log = None, None, []
    out, val = [], 1.0
    for d in dates:
        y = int(d[:4])
        for c in units:                       # 先用当日价格给持仓估值
            if d in NAV[c]:
                last[c] = NAV[c][d]
        if y != cy:
            tgt = picks.get(y) or picks[max(k for k in ys if k <= y)]
            cur = (sum(units[c] * last[c] for c in units) if units else val)
            # 必须无条件刷新新持仓的价格：last 会残留上一年（甚至更早）的旧价，
            # 一个币被踢出又选回时若沿用旧价，持仓单位会被放大几倍。
            for c in tgt:
                last[c] = nav_before(c, d)
            old = {c: units[c] * last[c] / cur for c in units} if units else {}
            new = {c: W for c in tgt}
            oneway = sum(abs(new.get(c, 0.0) - old.get(c, 0.0))
                         for c in set(old) | set(new)) / 2
            if units:
                cur *= (1 - oneway * RT)
                turnover_log.append((y, oneway))
            units = {c: W * cur / last[c] for c in tgt}
            val = cur
            cy, cq = y, (y, (int(d[5:7]) - 1) // 3)
            out.append((d, val))
            continue
        v = sum(units[c] * last[c] for c in units)
        q = (y, (int(d[5:7]) - 1) // 3)
        if q != cq:
            g = sum(abs(v * W - units[c] * last[c]) for c in units) / v / 2
            v *= (1 - g * RT)
            for c in units:
                units[c] = v * W / last[c]
            cq = q
        val = v
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd, turnover_log, out


def _slice_years(out, years):
    """用「下一年首个 bar」当年末，避免跨年那一个 bar 的收益丢失。"""
    firsts = {}
    for d, v in out:
        y = int(d[:4])
        firsts.setdefault(y, v)
    res = {}
    for y in years:
        if y not in firsts:
            continue
        end = firsts.get(y + 1, out[-1][1])
        res[y] = end / firsts[y] - 1
    return res


def year_rets(picks, years):
    """从同一条净值路径切年度收益，避免各年独立重跑导致漂移权重丢失。"""
    _r, _dd, _tl, out = rot_curve(picks)
    return _slice_years(out, years)


def fix_curve(start=START_EVAL, end=None):
    coins = [c for c in FIX10 if c in NAV]
    dates = [d for d in ALLD if d >= start and (end is None or d < end)]
    last = {c: nav_before(c, start) for c in coins}
    units = {c: W * 1.0 / last[c] for c in coins}
    cq, out = None, []
    for d in dates:
        for c in coins:
            if d in NAV[c]:
                last[c] = NAV[c][d]
        v = sum(units[c] * last[c] for c in coins)
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if cq is not None and q != cq:
            g = sum(abs(v * W - units[c] * last[c]) for c in coins) / v / 2
            v *= (1 - g * RT)
            for c in coins:
                units[c] = v * W / last[c]
        cq = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd, out


def fix_year_rets(years):
    _r, _dd, out = fix_curve()
    return _slice_years(out, years)


# ---------------- 输出 ----------------
print("\n【0】各年可选池（有 ≥365 天历史且数据足够的币数）")
for y in YEARS:
    print(f"  {y}（用 {y-1}-12-31 前数据）可选 {len(MET[y]):2d} 个 → 选 10 个")

print("\n【1】每年选出的 10 币")
for rule in RULES:
    rows = {y: pick(rule, y) for y in YEARS}
    for y in YEARS:
        print(f"  {pad(rule if y == YEARS[0] else '', 9)}{y}: {' '.join(rows[y])}")

print("\n【2】分年度收益（同一条净值路径切分，含 VT60、成本、换币成本）")
print(pad("方案", 10) + "".join(pad(y, 11) for y in YEARS) + pad("合计", 10))
res = {}
for rule in RULES:
    picks = {y: pick(rule, y) for y in YEARS}
    yr = year_rets(picks, YEARS)
    line, prod = pad(rule, 10), 1.0
    for y in YEARS:
        r = yr.get(y)
        line += pad(f"{r:+.1%}" if r is not None else "—", 11)
        prod *= 1 + (r or 0)
    print(line + pad(f"{prod:.2f}×", 10))
yr = fix_year_rets(YEARS)
line, prod = pad("FIX10", 10), 1.0
for y in YEARS:
    r = yr.get(y)
    line += pad(f"{r:+.1%}" if r is not None else "—", 11)
    prod *= 1 + (r or 0)
print(line + pad(f"{prod:.2f}×", 10))

print("\n【3】2022-01 起全段：收益 / 年化 / 回撤 / Calmar")
print(pad("方案", 10) + pad("累计", 9) + pad("年化", 9) + pad("回撤", 9) + pad("Calmar", 8))
yrs = 4.88
rows3 = []
r_fix, dd_fix, _o = fix_curve()
rows3.append(("FIX10", 1 + r_fix, dd_fix))
for rule in RULES:
    picks = {y: pick(rule, y) for y in YEARS}
    r, dd, _tl, _o = rot_curve(picks)
    rows3.append((rule, 1 + r, dd))
for name, m, dd in rows3:
    ann = m ** (1 / yrs) - 1
    print(pad(name, 10) + pad(f"{m:.2f}×", 9) + pad(f"{ann:+.1%}", 9) +
          pad(f"{dd:.1%}", 9) + pad(f"{ann / abs(dd):.1f}", 8))

print("\n【4】随机基准：每年随机抽 10 个，200 次")
random.seed(20261118)
rand_r, rand_m = [], []
for _it in range(200):
    picks = {}
    for y in YEARS:
        c = list(MET[y])
        picks[y] = random.sample(c, min(10, len(c)))
    rr, _d, _t, _o = rot_curve(picks)
    rand_r.append(rr)
    rand_m.append(1 + rr)
rs = sorted(rand_r)
print(f"  200 次：中位 {rs[100]:+.1%}　均值 {sum(rs)/len(rs):+.1%}　"
      f"5% {rs[10]:+.1%}　95% {rs[190]:+.1%}　最好 {rs[-1]:+.1%}　最差 {rs[0]:+.1%}")
print(pad("方案", 10) + pad("累计", 9) + pad("超越随机比例", 14))
for name, m, dd in rows3:
    pct = sum(1 for x in rand_m if x < m) / len(rand_m)
    print(pad(name, 10) + pad(f"{m:.2f}×", 9) + pad(f"{pct:.0%}", 14))

print("\n【5】换手与选中币流动性（50 万本金、单币 5 万）")
print(pad("方案", 10) + pad("年度换手", 11) + pad("最薄币", 9) + pad("单笔占比", 10) + pad("判定", 8))
for rule in RULES:
    picks = {y: pick(rule, y) for y in YEARS}
    _r, _d, tl, _o = rot_curve(picks)
    tv = [t for _y, t in tl]
    avg_t = sum(tv) / len(tv) if tv else 0.0
    thin, thin_share = None, 0.0
    for y in YEARS:
        for c in picks[y]:
            if QV.get(c):
                sh = CAP * W * VT.KAPPA * 1.5 / QV[c]
                if sh > thin_share:
                    thin, thin_share = c, sh
    flag = "超标" if thin_share > 0.03 else "需拆单" if thin_share > 0.01 else "可接受"
    print(pad(rule, 10) + pad(f"{avg_t:.0%}", 11) + pad(thin or "—", 9) +
          pad(f"{thin_share:.1%}", 10) + pad(flag, 8))

print("\n【6】滚动 365 天窗口（每 90 天，2022 起）· 各规则 − FIX10")
T_END = date(2026, 9, 1).toordinal()
for rule in ["FLIQ", "LIQ", "REV", "AVGR", "TREND", "MIX", "SLEEVE", "MOM", "RISKADJ"]:
    picks = {y: pick(rule, y) for y in YEARS}
    diffs, t = [], date(2022, 1, 1)
    while t.toordinal() + 365 <= T_END:
        t1 = date.fromordinal(t.toordinal() + 365)
        a = fix_curve(t.isoformat(), t1.isoformat())[0]
        b = rot_curve(picks, t.isoformat(), t1.isoformat())[0]
        if a is not None and b is not None:
            diffs.append((b - a) * 100)
        t = date.fromordinal(t.toordinal() + 90)
    ds = sorted(diffs)
    print(f"  {pad(rule, 9)}窗 {len(diffs)}　胜 {sum(1 for x in diffs if x > 0.2)}　"
          f"负 {sum(1 for x in diffs if x < -0.2)}　中位 {ds[len(ds)//2]:+.1f}pp　"
          f"均值 {sum(diffs)/len(diffs):+.1f}pp　最差 {ds[0]:+.1f}pp　最好 {ds[-1]:+.1f}pp")


# ---------------- IC 诊断 ----------------
def _rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0] * len(v)
    for pos, i in enumerate(order):
        r[i] = pos
    return r


def spearman(a, b):
    ra, rb = _rank(a), _rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((x - mb) ** 2 for x in rb))
    return num / den if den else 0.0


print("\n【7】指标 IC：选币指标 vs 次年 sleeve 收益的秩相关（>0 = 方向正确）")
FY = [2022, 2023, 2024, 2025]              # 预测 2023~2026
FKEYS = ["mom", "riskadj", "trend", "sleeve", "avgr", "vol"]
print(pad("指标", 10) + "".join(pad(f"{y}→{y+1}", 11) for y in FY) + pad("均值", 9) + pad("同号", 8))
for k in FKEYS:
    ics = []
    for y in FY:
        cs = list(MET[y])
        mv = [MET[y][c][k] for c in cs]
        nx = []
        for c in cs:
            a = nav_before(c, f"{y}-01-01")
            b = nav_before(c, f"{y + 1}-01-01")
            nx.append(b / a - 1)
        ics.append(spearman(mv, nx))
    mu = sum(ics) / len(ics)
    same = sum(1 for x in ics if (x > 0) == (mu > 0))
    print(pad(k, 10) + "".join(pad(f"{x:+.2f}", 11) for x in ics) +
          pad(f"{mu:+.2f}", 9) + pad(f"{same}/{len(ics)}", 8))

print("\n【8】被轮动剔除的币，当年（持有时段）表现如何（检验「砍掉的是不是赢家」）")
tot_i, tot_o = [], []
for y in FY:
    cs = set(MET[y])
    keep = set(pick("MOM", y))
    out = cs - keep
    def yr(c):
        a = nav_before(c, f"{y}-01-01"); b = nav_before(c, f"{y + 1}-01-01")
        return b / a - 1
    kin = sorted(((yr(c), c) for c in keep), reverse=True)
    kout = sorted(((yr(c), c) for c in out), reverse=True)
    mi = sum(x for x, _ in kin) / len(kin)
    mo = sum(x for x, _ in kout) / len(kout)
    tot_i += [x for x, _ in kin]
    tot_o += [x for x, _ in kout]
    print(f"  {y} 年（用 {y-1} 数据选）　MOM 选入均值 {mi:+.1%}（最好 {kin[0][1]} {kin[0][0]:+.0%}）"
          f"　剔除均值 {mo:+.1%}（最好 {kout[0][1]} {kout[0][0]:+.0%}）　差 {(mi - mo) * 100:+.1f}pp")
mi, mo = sum(tot_i) / len(tot_i), sum(tot_o) / len(tot_o)
print(f"  四年合计　选入 {mi:+.1%}　剔除 {mo:+.1%}　差 {(mi - mo) * 100:+.1f}pp")
