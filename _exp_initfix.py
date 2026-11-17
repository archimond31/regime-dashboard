#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_initfix.py —— INITFIX（初始血统受 MA60 纠错）的稳健性检验

cross_fix_init 是**零参数布尔开关**，没有可调参数，所以「walk-forward」要回答的
不是「哪个参数最好」，而是：**这个结论是不是只靠 HYPE 一个样本撑着。**

四节：
  1) 扩样本：22 个品种（含黄金/原油/股指/A股，21 个与 HYPE 无关）
     首段「状态与未来 20 日收益是否同向」的对齐率 —— base vs INITFIX
  2) 10 币逐年 sleeve 差值（2020-2026），看是不是某一年撞大运
  3) 10 币滚动 365 天窗口（每 90 天起一窗）胜负分布
  4) 归因：init 段内到底哪几天被 MA60 翻掉，翻得对不对
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe
from test_regime_fit import agg4h
from visualize_btc_regime import daily_states

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
ARMS = [("base", {}), ("INITFIX", {"cross_fix_init": True})]


def pad(s, n, right=False):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def idx_of(b4, d):
    return next((i for i, x in enumerate(b4) if x["date"] >= d), len(b4))


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


F4 = BT.FUND_DAILY / 6.0


def run_states(closes, kw):
    """返回 (st, blood, dts)：st 为逐日状态，blood 为逐日血统。"""
    st, _div, tg = daily_states(closes, 120, 70, 30, True, 2, True, **kw)
    blood, cur = [], None
    for t in tg:
        if t is not None:
            cur = t
        blood.append(cur)
    return st, blood


# ================= 1. 22 品种：首段状态对齐率 =================
print("【1】22 品种首段：状态与未来 20 日收益的同向率（日线）")
from daily_regime_alert import (
    ASSETS, fetch_hyperliquid_daily, fetch_ashare_daily, drop_unclosed, drop_unclosed_cn,
)
from visualize_btc_regime import fetch_binance_daily
from visualize_eth_gold_regime import fetch_yahoo_daily

warm = dt.datetime(2021, 1, 1, tzinfo=dt.timezone.utc)


def fetch(a):
    if a["source"] == "binance":
        return fetch_binance_daily(a["symbol"], warm)
    if a["source"] == "hyperliquid":
        return fetch_hyperliquid_daily(a["symbol"])
    if a["source"] == "ashare":
        return fetch_ashare_daily(a)
    return fetch_yahoo_daily(a["symbol"], warm)


print(pad("品种", 10) + pad("首段长度 base→IF", 16, 1) +
      pad("同向率 base", 11, 1) + pad("IF", 9, 1) + pad("差", 9, 1) + pad("样本日", 8, 1))
tot = {"base": [0, 0], "INITFIX": [0, 0]}
rows = []
for a in ASSETS:
    try:
        rows_all = drop_unclosed_cn(fetch(a)) if a["source"] == "ashare" else drop_unclosed(fetch(a))
    except Exception as e:  # noqa: BLE001
        print(f"{a['key']:10} 取数失败 {e}")
        continue
    dts = [r["date"] for r in rows_all]
    closes = [r["close"] for r in rows_all]
    if len(closes) < 200:
        continue
    R = {}
    for vname, kw in ARMS:
        st, blood = run_states(closes, kw)
        s0 = 120
        end = s0
        for i in range(s0, len(st)):
            if blood[i] == "init_ma120":
                end = i
        R[vname] = (st, blood, end)
    b_end = R["base"][2]
    i_end = R["INITFIX"][2]
    # 必须用**并集**：交集恰好截掉了「一臂已离开 init、另一臂还锁着」的那段，
    # 而那正是两臂唯一有分歧的区间 —— 取交集会让指标恒等于 base（第一版就是这么错的）。
    ov = max(b_end, i_end)
    n_ok = {"base": 0, "INITFIX": 0}
    n_tot = 0
    for i in range(120, ov + 1):
        j = min(i + 20, len(closes) - 1)
        fwd = closes[j] / closes[i] - 1
        if fwd == 0:
            continue
        n_tot += 1
        for vname, _ in ARMS:
            s = R[vname][0][i]
            if s == "UP" and fwd > 0:
                n_ok[vname] += 1
            elif s == "DOWN" and fwd < 0:
                n_ok[vname] += 1
    if n_tot == 0:
        continue
    for vname in ("base", "INITFIX"):
        if n_ok[vname] is not None:
            tot[vname][0] += n_ok[vname]
            tot[vname][1] += n_tot
    pb, pi = n_ok["base"] / n_tot, n_ok["INITFIX"] / n_tot
    rows.append((a["key"], b_end - 120, i_end - 120, pb, pi, n_tot))
    print(pad(a["key"], 10) + pad(f'{b_end - 120}→{i_end - 120}', 16, 1) +
          pad(f"{pb:.0%}", 11, 1) + pad(f"{pi:.0%}", 9, 1) +
          pad(f"{(pi - pb) * 100:+.0f}pp", 9, 1) + pad(n_tot, 8, 1))
if tot["base"][1]:
    print(f"  汇总同向率：base {tot['base'][0]}/{tot['base'][1]} = "
          f"{tot['base'][0] / tot['base'][1]:.1%}   INITFIX {tot['INITFIX'][0]}/"
          f"{tot['INITFIX'][1]} = {tot['INITFIX'][0] / tot['INITFIX'][1]:.1%}")
    up = sum(1 for r in rows if r[4] > r[3] + 1e-9)
    dn = sum(1 for r in rows if r[4] < r[3] - 1e-9)
    print(f"  逐品种：改善 {up} / 变差 {dn} / 不变 {len(rows) - up - dn}")

# ================= 2-4. 10 币 4H 层面 =================
for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()
BARS, B4, DAILY = {}, {}, {}
for c in BIN9:
    BARS[c] = V2.load_all()[c][0]
    B4[c] = agg4h(BARS[c])
    DAILY[c] = to_daily(BARS[c])
HB = [dict(x, date=x["date"][:10]) for x in load_hype("4h")]
B4["HYPE"] = HB
DAILY["HYPE"] = to_daily(HB)

RES = {}
for vname, kw in ARMS:
    for c in COINS:
        daily = DAILY[c]
        b4 = B4[c]
        dts = [x["date"] for x in daily]
        closes = [x["close"] for x in daily]
        st, blood = run_states(closes, kw)
        rm = {d: s for d, s in zip(dts, st) if s in ("UP", "RANGE", "DOWN")}
        bl = {d: b for d, b in zip(dts, blood) if d in rm}
        end = 120
        for i, b in enumerate(blood):
            if b == "init_ma120":
                end = i
        rg4 = align_regime_safe(b4, rm)
        blood4 = align_regime_safe(b4, bl)
        sig, trs = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q,
                              coin=c, blood4=blood4)
        bs = [BT.BASE.get(r, 0.0) for r in rg4]
        pos = [bs[k] + BT.ADD * sig[k] for k in range(len(sig))]
        RES[(vname, c)] = {"b4": b4, "base": bs, "pos": pos, "trs": trs,
                           "lock": end - 120, "dts": dts, "st": st, "closes": closes}

print("\n【2】10 币逐年 sleeve 差值（含成本资金费；负=INITFIX 更差）")
YEARS = [str(y) for y in range(2020, 2027)]
print(pad("品种", 8) + "".join(pad(y[2:], 10, 1) for y in YEARS) + pad("全样本", 11, 1))
wins = {"y": 0, "n": 0}
for c in COINS:
    line = pad(c, 8)
    for y in YEARS:
        d0, d1 = f"{y}-01-01", f"{int(y) + 1}-01-01"
        b4 = RES[("base", c)]["b4"]
        k0, k1 = idx_of(b4, d0), idx_of(b4, d1)
        if k1 - k0 < 60:
            line += pad("—", 10, 1)
            continue
        r = {v: sleeve(b4, RES[(v, c)]["base"], RES[(v, c)]["pos"], k0, k1) for v, _ in ARMS}
        d = (r["INITFIX"] - r["base"]) * 100
        if d > 0.5:
            wins["y"] += 1
        elif d < -0.5:
            wins["n"] += 1
        line += pad(f"{d:+.0f}", 10, 1)
    b4 = RES[("base", c)]["b4"]
    k0 = 0                            # 从数据起点算：各币 init 段都在这里，从 2020 起会漏掉
    r = {v: sleeve(b4, RES[(v, c)]["base"], RES[(v, c)]["pos"], k0, len(b4)) for v, _ in ARMS}
    line += pad(f'{(r["INITFIX"] - r["base"]) * 100:+.0f}pp', 11, 1)
    print(line)
print(f"  品种-年 单元格胜负：改善 {wins['y']} / 变差 {wins['n']}")

print("\n【3】10 币滚动 365 天窗口（每 90 天起一窗）")
print(pad("品种", 8) + pad("窗口数", 8, 1) + pad("胜", 6, 1) + pad("负", 6, 1) +
      pad("中位差", 10, 1) + pad("均值差", 10, 1))
g_w = g_n = g_z = 0
for c in COINS:
    b4 = RES[("base", c)]["b4"]
    d0 = b4[0]["date"]
    d_last = b4[-1]["date"]
    diffs = []
    t = dt.date(*map(int, d0.split("-")))
    end = dt.date(*map(int, d_last.split("-")))
    while True:
        t1 = t + dt.timedelta(days=365)
        if t1 > end:
            break
        k0, k1 = idx_of(b4, t.isoformat()), idx_of(b4, t1.isoformat())
        if k1 - k0 > 1200:
            r = {v: sleeve(b4, RES[(v, c)]["base"], RES[(v, c)]["pos"], k0, k1) for v, _ in ARMS}
            diffs.append((r["INITFIX"] - r["base"]) * 100)
        t += dt.timedelta(days=90)
    if not diffs:
        continue
    w = sum(1 for x in diffs if x > 0.5)
    n = sum(1 for x in diffs if x < -0.5)
    g_w += w
    g_n += n
    g_z += len(diffs) - w - n
    ds = sorted(diffs)
    med = ds[len(ds) // 2]
    print(pad(c, 8) + pad(len(diffs), 8, 1) + pad(w, 6, 1) + pad(n, 6, 1) +
          pad(f"{med:+.1f}pp", 10, 1) + pad(f"{sum(diffs) / len(diffs):+.1f}pp", 10, 1))
print(f"  合计：胜 {g_w} / 负 {g_n} / 平 {g_z}")

print("\n【4】归因：init 段被 MA60 翻掉的日期")
for c in COINS:
    a, b = RES[("base", c)], RES[("INITFIX", c)]
    if a["lock"] == b["lock"]:
        continue
    dts, stb, sti = a["dts"], a["st"], b["st"]
    flips = [i for i in range(121, min(len(stb), max(a["lock"], b["lock"]) + 120))
             if stb[i] != sti[i]]
    seg = f'{dts[120]}→{dts[max(a["lock"], b["lock"]) + 120]}'
    print(f'  {pad(c, 7)} init 段 {pad(seg, 26)} 长度 {a["lock"]}→{b["lock"]} 天  '
          f'翻转 {len(flips)} 次', end="")
    cl = a["closes"]
    if flips:
        ok = bad = 0
        for i in flips:
            j = min(i + 20, len(dts) - 1)
            fwd = cl[j] / cl[i] - 1
            if (sti[i] == "UP" and fwd > 0) or (sti[i] == "DOWN" and fwd < 0):
                ok += 1
            else:
                bad += 1
        i = flips[0]
        j = min(i + 20, len(dts) - 1)
        fwd = cl[j] / cl[i] - 1
        print(f'  | 翻转对/错 {ok}/{bad}  首次 {dts[i]} base={stb[i]}→IF={sti[i]} '
              f'此后20日 {fwd:+.1%}')
    else:
        print("  （无逐日翻转）")
