#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""init 血统纠错 / 同向背离去重 —— 四组对照实验。

背景（2026-11-17 发现）：
  HYPE 2025-04-04~05-22 被判 DOWN 达 48 天，期间价格 +181.5%，策略反向做空 4 次全止损。
  根因是 init 血统的 DOWN 出口只剩「RSI 破 os 再升破 os」，上涨行情中永不触发。

四个变体（全部默认关闭，只为对照）：
  base  : 现状
  A     : cross_fix_init=True  → init 血统也受 MA60 反向穿越纠错
  B     : div_no_same=True     → 同向背离不重复触发（不重设 fail_level）
  A+B   : 两者同时

评价口径：
  · 每币 sleeve：全可用样本 + 2025 单年
  · 组合：固定份额（与 _bt_10k 一致）+ 手续费 4bp + 资金费 3bp/日（仅加仓层）
  · 关键判据不是「总收益变高」，而是「修好 HYPE 的同时，有没有把 BTC/ETH 改坏」
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h
from visualize_btc_regime import daily_states

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
F4 = BT.FUND_DAILY / 6.0
START = "2018-01-01"

VARIANTS = [
    ("base", {}),
    ("A  init 受 MA60 纠错", {"cross_fix_init": True}),
    ("B  同向背离去重", {"div_no_same": True}),
    ("A+B", {"cross_fix_init": True, "div_no_same": True}),
]


def pad(s, n, right=False):
    s = str(s)
    w = sum(2 if ord(ch) > 127 else 1 for ch in s)
    fill = " " * max(0, n - w)
    return fill + s if right else s + fill


def reg_with(daily, **kw):
    """按变体参数重算日线状态 + 触发点。"""
    closes = [x["close"] for x in daily]
    st, _div, tg = daily_states(closes, 120, 70, 30, True, 2, True, **kw)
    m = {d: s for d, s in zip([x["date"] for x in daily], st)
         if s in ("UP", "RANGE", "DOWN")}
    return m, st, tg, [x["date"] for x in daily]


def sleeve(b4, base, pos, k0, k1):
    nav = 1.0
    for k in range(k0 + 1, k1):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
    return nav


def idx_of(b4, d):
    return next((i for i, x in enumerate(b4) if x["date"] >= d), len(b4))


def blood_of(dts, tg):
    """每个日线日期所属的「血统」（最近一次非 None 触发的类型）。"""
    out, cur = {}, None
    for i, d in enumerate(dts):
        if tg[i] is not None:
            cur = tg[i]
        out[d] = cur
    return out


# ---------- 载入原始 bars ----------
for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()
BARS = {}
for c in BIN9:
    BARS[c] = V2.load_all()[c][0]                      # 1H
BARS["HYPE"] = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]   # 已是 4H
isfour = {c: (c == "HYPE") for c in COINS}

# ---------- 逐变体、逐币 ----------
RES = {}          # (variant, coin) -> dict
LOCK = {}         # (variant, coin) -> init 血统停留天数
BLOOD = {}        # (variant, coin) -> {date: 血统}
for vname, kw in VARIANTS:
    for c in COINS:
        b = BARS[c]
        daily = to_daily(b)
        rm, st, tg, dts = reg_with(daily, **kw)
        BLOOD[(vname, c)] = blood_of(dts, tg)
        rg = align_regime_safe(b, rm)
        b4 = b if isfour[c] else agg4h(b)
        if isfour[c]:
            rg4 = align_regime_safe(b4, rm)
        else:
            rg4 = [rg[x["src"]] for x in b4]
        sig, trs = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q, coin=c)
        base = [BT.BASE.get(r, 0.0) for r in rg4]
        pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
        # D 变体：init 血统期间禁止开新仓（sig 归零，底仓仍按状态）
        bl = BLOOD[(vname, c)]
        sig_d = list(sig)
        initmask = [False] * len(b4)
        prev_day = None
        for k in range(len(b4)):
            d = b4[k]["date"]
            if d != prev_day:
                prev_day = d
                # 与 align_regime_safe 同口径：用严格早于当日的最后一根日线
                ks = sorted(bl)
                import bisect as _bs
                j = _bs.bisect_left(ks, d) - 1
                cur_b = bl[ks[j]] if j >= 0 else None
            if cur_b == "init_ma120":
                sig_d[k] = 0
                initmask[k] = True
        pos_d = [base[k] + BT.ADD * sig_d[k] for k in range(len(sig_d))]
        k_all0 = idx_of(b4, START)
        k25a, k25b = idx_of(b4, "2025-01-01"), idx_of(b4, "2026-01-01")
        RES[(vname, c)] = {
            "all": sleeve(b4, base, pos, k_all0, len(b4)),
            "all_d": sleeve(b4, base, pos_d, k_all0, len(b4)),
            "y25": sleeve(b4, base, pos, k25a, k25b),
            "y25_d": sleeve(b4, base, pos_d, k25a, k25b),
            "tr25": len([t for t in trs if "2025-01-01" <= t["d"] < "2026-01-01"]),
            "r25": sum(t["r"] for t in trs if "2025-01-01" <= t["d"] < "2026-01-01"),
            "down25": sum(1 for x in rg4[k25a:k25b] if x == "DOWN"),
            "veto": [t for t in trs if "2025-01-01" <= t["d"] < "2026-01-01"
                     and initmask[t["i"]]],
            "b4": b4, "pos": pos, "pos_d": pos_d, "base": base,
        }
        LOCK[(vname, c)] = {"init_days": 0}

# ---------- 组合 ----------
VARIANTS = VARIANTS + [("D  init 期禁开仓", {})]
BASE_V = ["base", "A  init 受 MA60 纠错", "B  同向背离去重", "A+B"]


def getpos(v, c):
    r = RES[(v if v in BASE_V else v, c)] if v in RES else RES[("base", c)]
    return r["pos_d"] if v.startswith("D") else r["pos"]


def combine(v):
    navs = {}
    for c in COINS:
        if v.startswith("D"):
            r = RES[("base", c)]
            pos = r["pos_d"]
        else:
            r = RES[(v, c)]
            pos = r["pos"]
        b4, base = r["b4"], r["base"]
        k0 = idx_of(b4, START)
        nav, out = 1.0, {}
        for k in range(k0 + 1, len(b4)):
            rr = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pos[k] * rr
            nav *= x if x > 0 else 0.0
            sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pos[k] != pos[k - 1]:
                nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
            out[b4[k]["date"]] = nav
        navs[c] = out
    days = sorted(set().union(*[set(x) for x in navs.values()]))
    last = {c: 1.0 for c in navs}
    cur = []
    for d in days:
        for c in navs:
            if d in navs[c]:
                last[c] = navs[c][d]
        cur.append((d, sum(PC.WEIGHTS[c] * last[c] for c in navs)))
    return cur


def met(cur, a=None, b=None):
    v = [(d, x) for d, x in cur if (a is None or d >= a) and (b is None or d < b)]
    if len(v) < 2:
        return None
    v0 = v[0][1]
    pk, mdd = v[0][1], 0.0
    for _d, x in v:
        pk = max(pk, x)
        mdd = min(mdd, x / pk - 1)
    import datetime as dt
    yrs = (dt.date.fromisoformat(v[-1][0]) - dt.date.fromisoformat(v[0][0])).days / 365.25
    return {"ret": v[-1][1] / v0 - 1, "mdd": mdd,
            "cagr": (v[-1][1] / v0) ** (1 / yrs) - 1 if yrs > 0 else 0}


ALLV = [v for v, _ in VARIANTS]
PORT = {v: combine(v) for v in ALLV}


def sv(v, c, k="y25"):
    if v.startswith("D"):
        return RES[("base", c)]["y25_d"]
    return RES[(v, c)][k]


print("=" * 108)
print("一、HYPE 2025：DOWN 天数 / 交易 / sleeve 收益")
print("-" * 108)
print(pad("变体", 24) + pad("DOWN根数", 11, True) + pad("笔数", 8, True)
      + pad("总R", 10, True) + pad("2025 sleeve", 14, True) + pad("全样本 sleeve", 15, True))
print("-" * 108)
for v in ALLV:
    r = RES[("base", "HYPE")] if v.startswith("D") else RES[(v, "HYPE")]
    y25 = r["y25_d"] if v.startswith("D") else r["y25"]
    al = r["all_d"] if v.startswith("D") else r["all"]
    ntr, rr = r["tr25"], r["r25"]
    if v.startswith("D"):
        ntr -= len(r["veto"]); rr -= sum(x["r"] for x in r["veto"])
    print(pad(v, 24) + pad(r["down25"], 11, True) + pad(ntr, 8, True)
          + pad(f"{rr:+.1f}R", 10, True)
          + pad(f"{(y25 - 1) * 100:+.1f}%", 14, True)
          + pad(f"{(al - 1) * 100:+.1f}%", 15, True))

print("\n" + "=" * 108)
print("二、全部 10 币：2025 单年 sleeve（看有没有被改坏）")
print("-" * 108)
print(pad("币", 6) + "".join(pad(v.split()[0], 12, True) for v in ALLV)
      + pad("D−base", 11, True))
print("-" * 108)
for c in COINS:
    row = pad(c, 6)
    for v in ALLV:
        row += pad(f"{(sv(v, c) - 1) * 100:+.1f}%", 12, True)
    row += pad(f"{(sv('D  init 期禁开仓', c) - sv('base', c)) * 100:+.1f}pp", 11, True)
    print(row)

print("\n" + "=" * 108)
print("三、组合层（固定份额，全样本 2018 起 + 2025 单年）")
print("-" * 108)
print(pad("变体", 24) + pad("全样本收益", 13, True) + pad("全样本MDD", 13, True)
      + pad("Calmar", 10, True) + pad("2025收益", 12, True) + pad("2025MDD", 12, True))
print("-" * 108)
for v in ALLV:
    a = met(PORT[v])
    b = met(PORT[v], "2025-01-01", "2026-01-01")
    print(pad(v, 24) + pad(f"{a['ret'] * 100:+.1f}%", 13, True)
          + pad(f"{a['mdd'] * 100:.1f}%", 13, True)
          + pad(f"{a['cagr'] / abs(a['mdd']):.2f}", 10, True)
          + pad(f"{b['ret'] * 100:+.1f}%", 12, True) + pad(f"{b['mdd'] * 100:.1f}%", 12, True))

print("\n" + "=" * 108)
print("四、样本外检验：每个币「刚上线那 200 天」（状态机预热刚结束的一段）")
print("-" * 108)
print("  最接近 HYPE 2025-04 的情形：历史短、MA120 被上线初期暴跌主导。")
print("-" * 108)
print(pad("币", 6) + pad("窗口起点", 13) + pad("base", 11, True) + pad("A", 11, True)
      + pad("D", 11, True) + pad("A−b", 10, True) + pad("D−b", 10, True))
print("-" * 108)
sA = sD = wA = wD = 0
for c in COINS:
    b = BARS[c]
    daily = to_daily(b)
    if len(daily) < 140:
        continue
    d0 = daily[120]["date"]
    d1 = daily[min(320, len(daily) - 1)]["date"]
    r0 = RES[("base", c)]
    b4 = r0["b4"]
    i0, i1 = idx_of(b4, d0), idx_of(b4, d1)
    v0 = sleeve(b4, r0["base"], r0["pos"], i0, i1)
    vA = sleeve(b4, RES[("A  init 受 MA60 纠错", c)]["base"],
                RES[("A  init 受 MA60 纠错", c)]["pos"], i0, i1)
    vD = sleeve(b4, r0["base"], r0["pos_d"], i0, i1)
    print(pad(c, 6) + pad(d0, 13) + pad(f"{(v0 - 1) * 100:+.1f}%", 11, True)
          + pad(f"{(vA - 1) * 100:+.1f}%", 11, True) + pad(f"{(vD - 1) * 100:+.1f}%", 11, True)
          + pad(f"{(vA - v0) * 100:+.1f}pp", 10, True) + pad(f"{(vD - v0) * 100:+.1f}pp", 10, True))
    dA, dD = (vA - v0) * 100, (vD - v0) * 100
    if dA > 0.5: sA += 1
    elif dA < -0.5: wA += 1
    if dD > 0.5: sD += 1
    elif dD < -0.5: wD += 1
print("-" * 108)
print(f"  A：改善 {sA} / 变差 {wA}　　D：改善 {sD} / 变差 {wD}")
