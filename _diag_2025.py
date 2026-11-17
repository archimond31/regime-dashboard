#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2025 年亏损归因。

亏损可以拆成四个互相独立的部分，逐个量化：
  1. 方向判断：底仓 1.0 只在 UP/RANGE 持有。若这些天标的在跌，就是状态机判错。
     底仓贡献 = Σ_{k∈UP∪RANGE} r_k，这一项为负即「该空仓时满仓多头」。
  2. 加仓层：0.5× 的突破单。拆多头/空头、按出场原因看是止损还是翻转亏的。
  3. 成本：手续费（4bp 单边 × 换手）+ 资金费（3bp/日 仅加仓层）。
  4. 集中度：哪个币亏最多，是不是整体性亏损。

口径与 _bt_10k.py 完全一致（固定份额、1.5×/0.5×、现货底仓不付资金费）。
"""
import bisect
import datetime as dt
import math
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
F4 = BT.FUND_DAILY / 6.0

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()

raw = {}
for c in BIN9:
    b, rg, _a, _b2 = V2.load_all()[c]
    b4 = agg4h(b)
    rg4 = [rg[x["src"]] for x in b4]
    sig, trades = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q, coin=c)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
    raw[c] = (b4, rg4, base, pos, trades)

hb = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
hrg = align_regime_safe(hb, regime_series(to_daily(hb)))
hsig, htrades = BT.signals(hb, hrg, atr_wilder(hb, 14), vol_q=BT.VOL_Q, coin="HYPE")
hbase = [BT.BASE.get(r, 0.0) for r in hrg]
raw["HYPE"] = (hb, hrg, hbase, [hbase[k] + BT.ADD * hsig[k] for k in range(len(hsig))], htrades)

W = dict(PC.WEIGHTS)


def pad(s, n, right=False):
    s = str(s)
    w = sum(2 if ord(ch) > 127 else 1 for ch in s)
    fill = " " * max(0, n - w)
    return fill + s if right else s + fill


def seg(b4, k0, k1):
    """取 b4 中日期落在 [k0, k1) 的索引区间。"""
    i0 = next(i for i in range(len(b4)) if b4[i]["date"] >= k0)
    i1 = next((i for i in range(len(b4)) if b4[i]["date"] >= k1), len(b4))
    return i0, i1


# ---------- 一、2025 全景 ----------
print("=" * 100)
print("一、2025 全景：策略 vs 基准（本金 10000 U）")
print("-" * 100)

Y2025 = ("2025-01-01", "2026-01-01")
DAILY = {}
for c in COINS:
    b4, rg4, base, pos, trs = raw[c]
    d = {}
    for k, x in enumerate(b4):
        d[x["date"]] = (x["close"], rg4[k], base[k], pos[k])
    DAILY[c] = d

_nav = {}
for c in COINS:
    b4, rg4, base, pos, trs = raw[c]
    i0, i1 = seg(b4, *Y2025)
    nav, out = 1.0, {}
    for k in range(i0 + 1, i1):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
        out[b4[k]["date"]] = nav
    _nav[c] = out


def combine(navs):
    days = sorted(set().union(*[set(v) for v in navs.values()]))
    out, last = [], {c: 1.0 for c in navs}
    for d in days:
        for c in navs:
            if d in navs[c]:
                last[c] = navs[c][d]
        out.append((d, sum(W[c] * last[c] for c in navs)))
    return out


CUR = combine(_nav)
v0, v1 = CUR[0][1], CUR[-1][1]
pk, mdd, mdd_at = v0, 0.0, None
for d, v in CUR:
    pk = max(pk, v)
    if v / pk - 1 < mdd:
        mdd, mdd_at = v / pk - 1, d
print(f"  策略 2025：{v0 * 10000:,.0f} U → {v1 * 10000:,.0f} U  收益 {(v1 / v0 - 1) * 100:+.1f}%"
      f"  年内最大回撤 {mdd * 100:.1f}%（谷底 {mdd_at}）")

_spot = {}
for c in COINS:
    ks = sorted(DAILY[c])
    a = next((DAILY[c][k][0] for k in ks if k >= Y2025[0]), None)
    b = next((DAILY[c][k][0] for k in reversed(ks) if k < Y2025[1]), None)
    if a and b:
        _spot[c] = b / a - 1
eq = sum(_spot.values()) / len(_spot)
print(f"  BTC 持有 {_spot['BTC'] * 100:+.1f}%   ETH 持有 {_spot['ETH'] * 100:+.1f}%"
      f"   等权 {len(_spot)} 币 {eq * 100:+.1f}%   策略超额 {(v1 / v0 - 1 - eq) * 100:+.1f}pp")

# 月度
print("\n  月度（策略 vs 等权持有）：")
print("  " + pad("月份", 9) + pad("策略", 10, True) + pad("等权持有", 11, True)
      + pad("差", 10, True) + pad("BTC", 10, True))
print("  " + "-" * 50)
_m0 = {c: DAILY[c][sorted(DAILY[c])[0]][0] for c in COINS}
for m in range(1, 13):
    a, b = f"2025-{m:02d}-01", (f"2025-{m + 1:02d}-01" if m < 12 else "2026-01-01")
    segp = [(d, v) for d, v in CUR if a <= d < b]
    if len(segp) < 2:
        continue
    sr = segp[-1][1] / segp[0][1] - 1
    rrs = []
    for c in COINS:
        ks = sorted(DAILY[c])
        p0 = next((DAILY[c][k][0] for k in ks if k >= a), None)
        p1 = next((DAILY[c][k][0] for k in reversed(ks) if k < b), None)
        if p0 and p1:
            rrs.append(p1 / p0 - 1)
    er = sum(rrs) / len(rrs)
    bt = None
    ks = sorted(DAILY["BTC"])
    p0 = next((DAILY["BTC"][k][0] for k in ks if k >= a), None)
    p1 = next((DAILY["BTC"][k][0] for k in reversed(ks) if k < b), None)
    if p0 and p1:
        bt = p1 / p0 - 1
    mark = "  ←" if sr < -0.03 else ""
    print("  " + pad(f"2025-{m:02d}", 9) + pad(f"{sr * 100:+.1f}%", 10, True)
          + pad(f"{er * 100:+.1f}%", 11, True) + pad(f"{(sr - er) * 100:+.1f}pp", 10, True)
          + pad(f"{bt * 100:+.1f}%" if bt is not None else "—", 10, True) + mark)

# ---------- 二、四层分解 ----------
print("\n" + "=" * 100)
print("二、亏损拆成五层（对数口径，可精确闭合；各币按初始权重加权）")
print("-" * 100)


def decompose(y0, y1):
    out = {"base": 0.0, "add": 0.0, "drag": 0.0, "cost": 0.0, "fund": 0.0}
    for c in COINS:
        b4, rg4, base, pos, trs = raw[c]
        i0, i1 = seg(b4, y0, y1)
        for k in range(i0 + 1, i1):
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            out["base"] += W[c] * base[k] * r
            out["add"] += W[c] * (pos[k] - base[k]) * r
            if pos[k] > -1:
                out["drag"] += W[c] * (math.log(1 + pos[k] * r) - pos[k] * r)
            if pos[k] != pos[k - 1]:
                out["cost"] += W[c] * math.log(1 - BT.COST * abs(pos[k] - pos[k - 1]))
            sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
            if F4 and sl:
                out["fund"] += W[c] * math.log(1 - F4 * sl)
    return out


D25 = decompose(*Y2025)
print("  " + pad("层", 28) + pad("2025 贡献", 13, True) + "  " + pad("说明", 44))
print("  " + "-" * 90)
rows = [
    ("① 底仓方向（UP/RANGE）", D25["base"], "状态机判 UP/RANGE 的日子，标的实际涨跌"),
    ("② 加仓层方向（0.5× 突破）", D25["add"], "4H 突破单的方向性损益"),
    ("③ 波动拖累（复利效应）", D25["drag"], "杠杆下的平方项：pos²r² 被扣掉一半"),
    ("④ 手续费", D25["cost"], "4bp 单边 × 每次换手"),
    ("⑤ 资金费", D25["fund"], "3bp/日 × 仅加仓层（空头为正则收）"),
]
for nm, v, note in rows:
    print("  " + pad(nm, 28) + pad(f"{v * 100:+.2f}pp", 13, True) + "  " + pad(note, 44))
print("  " + "-" * 90)
_tot = sum(D25.values())
print("  " + pad("合计（按初始权重）", 28) + pad(f"{_tot * 100:+.2f}pp", 13, True) + "  "
      + pad(f"实际组合 {math.log(v1 / v0) * 100:+.2f}%（对数）", 44))
print(f"  差额 {(math.log(v1 / v0) - _tot) * 100:+.2f}pp = 不调仓红利：亏钱的 sleeve 自动缩水，"
      f"赚钱的自动变大，不需要任何判断")

print("\n  三年对照（同一套分解，看拖累是不是每年都在扣）：")
print("  " + pad("年", 8) + pad("① 底仓", 13, True) + pad("② 加仓", 13, True)
      + pad("③ 拖累", 13, True) + pad("④⑤ 成本", 13, True)
      + pad("合计", 13, True) + pad("实际组合", 14, True))
print("  " + "-" * 92)
for lab, y0, y1 in [("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
                    ("2026", "2026-01-01", "2027-01-01")]:
    dd = decompose(y0, y1)
    # 实际组合（固定份额，含漂移）
    navs = {}
    for c in COINS:
        b4, rg4, base, pos, trs = raw[c]
        i0, i1 = seg(b4, y0, y1)
        nav, o = 1.0, {}
        for k in range(i0 + 1, i1):
            r = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pos[k] * r
            nav *= x if x > 0 else 0.0
            sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pos[k] != pos[k - 1]:
                nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
            o[b4[k]["date"]] = nav
        navs[c] = o
    cc = combine(navs)
    real = math.log(cc[-1][1] / cc[0][1])
    print("  " + pad(lab, 8) + pad(f"{dd['base'] * 100:+.1f}pp", 13, True)
          + pad(f"{dd['add'] * 100:+.1f}pp", 13, True)
          + pad(f"{dd['drag'] * 100:+.1f}pp", 13, True)
          + pad(f"{(dd['cost'] + dd['fund']) * 100:+.1f}pp", 13, True)
          + pad(f"{sum(dd.values()) * 100:+.1f}pp", 13, True)
          + pad(f"{real * 100:+.1f}%", 14, True))
print("  " + "-" * 92)
print("  → ③ 那一列每年都是 −20pp 上下：它是「仓位在就要交」的固定税，与涨跌无关。")
print("  → 2025 的问题不是方向判错（①②合计为正），而是方向赚的不够交这笔税。")

# ---------- 三、状态机在 2025 的判断质量 ----------
print("\n" + "=" * 100)
print("三、状态机 2025 年的判断质量：判 UP/RANGE 的那些天，标的在涨还是在跌")
print("-" * 100)
print("  " + pad("币", 6) + pad("UP天", 8, True) + pad("RANGE天", 10, True)
      + pad("DOWN天", 10, True) + pad("翻转", 7, True)
      + pad("UP∪RG段涨跌", 15, True) + pad("DOWN段涨跌", 15, True)
      + pad("做多占比", 11, True))
print("  " + "-" * 96)
TOT = {"up": 0, "rng": 0, "down": 0, "flip": 0}
BULL_WRONG = []
for c in COINS:
    b4, rg4, base, pos, trs = raw[c]
    i0, i1 = seg(b4, *Y2025)
    cnt = Counter(rg4[i0:i1])
    flip = sum(1 for k in range(i0 + 1, i1) if rg4[k] != rg4[k - 1])
    # 该状态下标的的累计涨跌（连续复利）
    ret_long = ret_short = 0.0
    nl = ns = 0
    for k in range(i0 + 1, i1):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        if rg4[k] in ("UP", "RANGE"):
            ret_long += math.log(1 + r) if r > -1 else 0
            nl += 1
        elif rg4[k] == "DOWN":
            ret_short += math.log(1 + r) if r > -1 else 0
            ns += 1
    rl = (math.exp(ret_long) - 1) * 100 if nl else 0
    rs = (math.exp(ret_short) - 1) * 100 if ns else 0
    TOT["up"] += cnt["UP"] * W[c]
    TOT["rng"] += cnt["RANGE"] * W[c]
    TOT["down"] += cnt["DOWN"] * W[c]
    TOT["flip"] += flip * W[c]
    if rl < 0:
        BULL_WRONG.append((c, rl, cnt["UP"] + cnt["RANGE"], flip))
    print("  " + pad(c, 6) + pad(cnt["UP"], 8, True) + pad(cnt["RANGE"], 10, True)
          + pad(cnt["DOWN"], 10, True) + pad(flip, 7, True)
          + pad(f"{rl:+.1f}%", 15, True) + pad(f"{rs:+.1f}%", 15, True)
          + pad(f"{(cnt['UP'] + cnt['RANGE']) / (i1 - i0) * 100:.0f}%", 11, True))
print("  " + "-" * 96)
_tt = TOT["up"] + TOT["rng"] + TOT["down"]
print(f"  加权平均：UP {TOT['up'] / _tt * 100:.0f}% / RANGE {TOT['rng'] / _tt * 100:.0f}%"
      f" / DOWN {TOT['down'] / _tt * 100:.0f}%　→ 允许做多的时间占 "
      f"{(TOT['up'] + TOT['rng']) / _tt * 100:.0f}%")
print(f"  加权平均翻转 {TOT['flip']:.0f} 次/年（每个币平均每年换 {(TOT['flip'] / len(COINS)):.0f} 次状态）")
print(f"  「判多头但标的下跌」的币：{len(BULL_WRONG)}/{len(COINS)} 个"
      + ("（" + ", ".join(f"{c}{r:+.0f}%" for c, r, _n, _f in BULL_WRONG) + "）" if BULL_WRONG else ""))

# ---------- 四、交易层面 ----------
print("\n" + "=" * 100)
print("四、交易层面：2025 的每一笔到底怎么亏的")
print("-" * 100)


def tstat(trades, y0, y1):
    ts = [t for t in trades if y0 <= t["d"] < y1]
    if not ts:
        return None
    rs = [t["r"] for t in ts]
    ld = [t for t in ts if t["side"] == 1]
    sh = [t for t in ts if t["side"] == -1]
    return {
        "n": len(ts), "w": len([r for r in rs if r > 0]),
        "long": len(ld), "short": len(sh),
        "win": len([r for r in rs if r > 0]) / len(rs),
        "sumR": sum(rs),
        "Rl": sum(t["r"] for t in ld), "Rs": sum(t["r"] for t in sh),
        "winl": (len([t for t in ld if t["r"] > 0]) / len(ld)) if ld else 0,
        "wins": (len([t for t in sh if t["r"] > 0]) / len(sh)) if sh else 0,
        "why": dict(Counter(t["reason"] for t in ts)),
        "whyR": {k: sum(t["r"] for t in ts if t["reason"] == k)
                 for k in set(t["reason"] for t in ts)},
        "bars": sum(t["bars_held"] for t in ts) / len(ts),
    }


YEARS = [("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
         ("2026", "2026-01-01", "2027-01-01")]
print("  " + pad("年", 7) + pad("笔数", 7, True) + pad("多/空", 10, True)
      + pad("胜率", 8, True) + pad("加权总R", 10, True) + pad("多头R", 10, True)
      + pad("空头R", 10, True) + pad("多头胜率", 11, True) + pad("空头胜率", 11, True)
      + pad("持仓根", 9, True))
print("  " + "-" * 93)
for lab, y0, y1 in YEARS:
    n = nw = nl = ns = 0
    rl = rs = 0.0
    wl, ws, bars = [], [], []
    for c in COINS:
        st = tstat(raw[c][4], y0, y1)
        if not st:
            continue
        n += st["n"]; nw += st["w"]; nl += st["long"]; ns += st["short"]
        rl += st["Rl"] * W[c]; rs += st["Rs"] * W[c]
        if st["long"]:
            wl.append(st["winl"])
        if st["short"]:
            ws.append(st["wins"])
        bars.append(st["bars"])
    print("  " + pad(lab, 7) + pad(n, 7, True) + pad(f"{nl}/{ns}", 10, True)
          + pad(f"{nw / max(n, 1) * 100:.0f}%", 8, True)
          + pad(f"{rl + rs:+.1f}R", 10, True) + pad(f"{rl:+.1f}R", 10, True)
          + pad(f"{rs:+.1f}R", 10, True)
          + pad(f"{sum(wl) / len(wl) * 100:.0f}%" if wl else "—", 11, True)
          + pad(f"{sum(ws) / len(ws) * 100:.0f}%" if ws else "—", 11, True)
          + pad(f"{sum(bars) / len(bars):.0f}" if bars else "—", 9, True))

print("\n  2025 出场原因分布（按 R 汇总，加权）：")
agg_why = defaultdict(lambda: [0, 0.0])
for c in COINS:
    st = tstat(raw[c][4], "2025-01-01", "2026-01-01")
    if not st:
        continue
    for k, v in st["why"].items():
        agg_why[k][0] += v
        agg_why[k][1] += st["whyR"].get(k, 0.0) * W[c]
print("  " + pad("原因", 12) + pad("笔数", 9, True) + pad("加权R", 12, True) + pad("每笔R", 10, True))
print("  " + "-" * 43)
for k, (n0, r0) in sorted(agg_why.items(), key=lambda x: x[1][1]):
    print("  " + pad(k, 12) + pad(n0, 9, True) + pad(f"{r0:+.1f}R", 12, True)
          + pad(f"{r0 / max(n0, 1):+.2f}", 10, True))

# ---------- 五、哪个币亏最多 ----------
print("\n" + "=" * 100)
print("五、2025 分币：策略亏多少 vs 现货跌多少")
print("-" * 100)
print("  " + pad("币", 6) + pad("权重", 6, True) + pad("策略", 10, True)
      + pad("现货", 10, True) + pad("对组合贡献", 12, True) + pad("贡献金额", 12, True))
print("  " + "-" * 56)
_rows = []
for c in COINS:
    nv = list(_nav[c].values())
    r = nv[-1] - 1 if nv else 0.0
    _rows.append((c, W[c], r, _spot.get(c, 0.0), W[c] * r))
for c, w, r, sp, contrib in sorted(_rows, key=lambda x: x[4]):
    print("  " + pad(c, 6) + pad(f"{w * 100:.0f}%", 6, True) + pad(f"{r * 100:+.1f}%", 10, True)
          + pad(f"{sp * 100:+.1f}%", 10, True) + pad(f"{contrib * 100:+.1f}pp", 12, True)
          + pad(f"{10000 * contrib:+,.0f}U", 12, True))
print("  " + "-" * 56)
print(f"  策略亏的 {(1 - v1 / v0) * 100:.1f}pp 里，现货本身跌了 {abs(eq) * 100:.1f}%（等权口径）"
      f"→ 策略比死拿少跌 {abs(eq - (v1 / v0 - 1)) * 100:.1f}pp")

# ---------- 六、回撤解剖 ----------
print("\n" + "=" * 100)
print("六、回撤解剖：−31.5% 是一次砸下来的，还是一路磨下来的")
print("-" * 100)
_pk, _pkd, _tr, _trd = CUR[0][1], CUR[0][0], 0.0, None
_best = (0.0, None, None)
for d, v in CUR:
    if v > _pk:
        _pk, _pkd = v, d
    if v / _pk - 1 < _best[0]:
        _best = (v / _pk - 1, _pkd, d)
print(f"  峰 {_best[1]}（{_best[1] and 10000 * next(v for x, v in CUR if x == _best[1]):,.0f}U）"
      f" → 谷 {_best[2]}（{10000 * next(v for x, v in CUR if x == _best[2]):,.0f}U）"
      f"  幅度 {_best[0] * 100:.1f}%")
days = (dt.date.fromisoformat(_best[2]) - dt.date.fromisoformat(_best[1])).days
print(f"  历时 {days} 天 —— " + ("一次急跌" if days < 30 else "慢性阴跌，不是单一事件"))

print("\n  回撤期间分币贡献：")
print("  " + pad("币", 6) + pad("区间涨跌", 11, True) + pad("策略", 11, True)
      + pad("贡献", 11, True) + pad("说明", 30))
print("  " + "-" * 72)
_seg_rows = []
for c in COINS:
    b4, rg4, base, pos, trs = raw[c]
    i0 = next((i for i in range(len(b4)) if b4[i]["date"] >= _best[1]), 0)
    i1 = next((i for i in range(len(b4)) if b4[i]["date"] >= _best[2]), len(b4) - 1)
    if i1 <= i0:
        continue
    nav = 1.0
    for k in range(i0 + 1, i1 + 1):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
    sp = b4[i1]["close"] / b4[i0]["close"] - 1
    _seg_rows.append((c, sp, nav - 1, W[c] * (nav - 1)))
for c, sp, r, contrib in sorted(_seg_rows, key=lambda x: x[3]):
    note = "方向对，只是幅度不够" if (sp < 0 < r) else ("同向放大" if r < sp else "")
    print("  " + pad(c, 6) + pad(f"{sp * 100:+.1f}%", 11, True) + pad(f"{r * 100:+.1f}%", 11, True)
          + pad(f"{contrib * 100:+.1f}pp", 11, True) + pad(note, 30))
print("  " + "-" * 72)
print("  " + pad("合计", 6) + pad("", 11) + pad("", 11)
      + pad(f"{sum(x[3] for x in _seg_rows) * 100:+.1f}pp", 11, True))

# ---------- 七、暴露水平 ----------
print("\n" + "=" * 100)
print("七、平均仓位暴露：拖累 ∝ pos²，所以仓位水平本身就是成本")
print("-" * 100)
print("  " + pad("年", 8) + pad("平均|仓位|", 13, True) + pad("多头占比", 12, True)
      + pad("空头占比", 12, True) + pad("空仓占比", 12, True)
      + pad("波动拖累", 13, True))
print("  " + "-" * 73)
for lab, y0, y1 in [("2024", "2024-01-01", "2025-01-01"), ("2025", "2025-01-01", "2026-01-01"),
                    ("2026", "2026-01-01", "2027-01-01")]:
    sa = sl_ = ss = sz = 0.0
    tot = 0
    for c in COINS:
        b4, rg4, base, pos, trs = raw[c]
        i0, i1 = seg(b4, y0, y1)
        for k in range(i0, i1):
            sa += W[c] * abs(pos[k])
            if pos[k] > 0.01:
                sl_ += W[c]
            elif pos[k] < -0.01:
                ss += W[c]
            else:
                sz += W[c]
            tot += W[c]
    dd = decompose(y0, y1)
    print("  " + pad(lab, 8) + pad(f"{sa / tot:.2f}×", 13, True)
          + pad(f"{sl_ / tot * 100:.0f}%", 12, True) + pad(f"{ss / tot * 100:.0f}%", 12, True)
          + pad(f"{sz / tot * 100:.0f}%", 12, True) + pad(f"{dd['drag'] * 100:+.1f}pp", 13, True))
print("  " + "-" * 73)
print("  → 2025 平均仓位并不低（多头+空头占了绝大多数时间），拖累自然照收。")
