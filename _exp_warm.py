#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_warm.py —— 热身冷却（warm_cross）A/B 对照

变体：
  base  判定起点 = 第 121 根（凑满 MA120 窗口就当起点）
  WARM  判定起点 = 第 121 根之后、收盘价**首次反穿 MA120** 那天

两臂都保持 BLOCK_INIT 开启（init 血统不开新仓），只是显式传 blood4，
所以两臂对 init 的处理完全一致，差异只来自起点。

五节：
  1) 起点位移：各币原起点 / 新起点 / 延迟天数 / 初始状态 / 首段锁死天数
  2) 组合逐年（每年初重置权重）
  3) 全样本 + 2025 分币 sleeve
  4) HYPE 2025 逐笔
  5) 首段状态的方向准确率（全 10 币，规避「只对 HYPE 好看」）
"""
import json
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
W = dict(PC.WEIGHTS)
F4 = BT.FUND_DAILY / 6.0
ARMS = [("base", {}), ("WARM", {"warm_cross": True}),
        ("GUARD", {"warm_cross": "guard"}), ("INITFIX", {"cross_fix_init": True})]


def pad(s, n, right=False):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


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


def _d(s):
    import datetime as _dt
    return _dt.date(*map(int, s.split("-")))


# ---------- 载入 ----------
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
        st, _div, tg = daily_states(closes, 120, 70, 30, True, 2, True, **kw)
        rm, bl, cur = {}, {}, None
        first_i = None
        for i, (d, s, t) in enumerate(zip(dts, st, tg)):
            if t is not None:
                cur = t
            if s in ("UP", "RANGE", "DOWN"):
                rm[d] = s
                bl[d] = cur
                if first_i is None:
                    first_i = i
        if not rm:
            print(f"[{vname}:{c}] 无判定")
            continue
        # 首段长度：从第一个判定日到下一个状态切换日
        first_i = dts.index(min(rm))
        lock = next((i for i in range(first_i + 1, len(st))
                     if st[i] != st[first_i]), len(st)) - first_i
        rg4 = align_regime_safe(b4, rm)
        blood4 = align_regime_safe(b4, bl)
        sig, trs = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q,
                              coin=c, blood4=blood4)
        base = [BT.BASE.get(r, 0.0) for r in rg4]
        pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
        k0 = idx_of(b4, "2024-01-01")
        ka, kb = idx_of(b4, "2025-01-01"), idx_of(b4, "2026-01-01")
        # 首段方向 vs 之后 60 日收益
        j0 = min(first_i + 60, len(closes) - 1)
        fwd = closes[j0] / closes[first_i] - 1
        agree = (st[first_i] == "UP" and fwd > 0) or (st[first_i] == "DOWN" and fwd < 0)
        RES[(vname, c)] = {
            "b4": b4, "pos": pos, "base": base,
            "all": sleeve(b4, base, pos, k0, len(b4)),
            "y25": sleeve(b4, base, pos, ka, kb),
            "t25": len([t for t in trs if "2025-01-01" <= t["d"] < "2026-01-01"]),
            "r25": sum(t["r"] for t in trs if "2025-01-01" <= t["d"] < "2026-01-01"),
            "w25": sum(1 for t in trs if "2025-01-01" <= t["d"] < "2026-01-01" and t["r"] > 0),
            "start": dts[first_i], "st0": st[first_i], "lock": lock,
            "fwd60": fwd, "agree": agree,
            "trs": trs,
        }

# ---------- 1. 起点位移 ----------
print("\n【1】判定起点位移")
print(pad("品种", 7) + pad("base起点", 12, 1) + pad("WARM起点", 12, 1) +
      pad("GUARD起点", 12, 1) + pad("首段 base→GUARD", 14, 1) +
      pad("天数", 9, 1) + pad("60日", 9, 1))
n_agree = {v: 0 for v, _ in ARMS}
for c in COINS:
    row = {v: RES[(v, c)] for v, _ in ARMS}
    for v in row:
        n_agree[v] += row[v]["agree"]
    print(pad(c, 7) + pad(row["base"]["start"], 12, 1) + pad(row["WARM"]["start"], 12, 1) +
          pad(row["GUARD"]["start"], 12, 1) +
          pad(row["base"]["st0"] + "→" + row["GUARD"]["st0"], 14, 1) +
          pad(f'{row["base"]["lock"]}→{row["GUARD"]["lock"]}', 9, 1) +
          pad(f'{row["base"]["fwd60"]:+.1%}', 9, 1))
print("  首段方向准确率：" + "  ".join(f"{v} {n_agree[v]}/{len(COINS)}" for v, _ in ARMS))

# ---------- 组合 ----------
def fixed_share(navs, weights):
    dates = sorted(set().union(*[set(n) for n in navs]))
    last = [1.0] * len(navs)
    out = []
    for d in dates:
        for i, n in enumerate(navs):
            if d in n:
                last[i] = n[d]
        out.append((d, sum(weights[i] * last[i] for i in range(len(navs)))))
    return out


def navs_of(vname, d0, d1=None):
    out = []
    for c in COINS:
        r = RES[(vname, c)]
        b4, base, pos = r["b4"], r["base"], r["pos"]
        k0 = idx_of(b4, d0)
        k1 = idx_of(b4, d1) if d1 else len(b4)
        nav, m = 1.0, {}
        for k in range(k0 + 1, k1):
            ret = b4[k]["close"] / b4[k - 1]["close"] - 1
            x = 1 + pos[k] * ret
            nav *= x if x > 0 else 0.0
            sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
            if F4 and sl:
                nav *= (1 - F4 * sl)
            if pos[k] != pos[k - 1]:
                nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
            m[b4[k]["date"]] = nav
        out.append(m)
    return out


def stat(cur):
    vals = [x[1] for x in cur]
    v0 = vals[0]
    pk, dd = v0, 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    fin = vals[-1] / v0
    return fin - 1, dd, fin


print("\n【2】组合逐年（每年初重置权重、等权 W）")
print(pad("年份", 10) + pad("base", 11, 1) + pad("WARM", 11, 1) + pad("GUARD", 11, 1) +
      pad("GUARD差", 10, 1) + pad("base回撤", 10, 1) + pad("GUARD回撤", 10, 1))
YEARS = ["2024", "2025", "2026YTD"]
tot = {v: 1.0 for v, _ in ARMS}
for y in YEARS:
    d0 = f"{y[:4]}-01-01" if y != "2026YTD" else "2026-01-01"
    d1 = f"{int(y[:4]) + 1}-01-01" if y != "2026YTD" else None
    row = {}
    for v, _ in ARMS:
        r, dd, fin = stat(fixed_share(navs_of(v, d0, d1), [W[c] for c in COINS]))
        row[v] = (r, dd)
        tot[v] *= fin
    print(pad(y, 10) + pad(f'{row["base"][0]:+.1%}', 11, 1) +
          pad(f'{row["WARM"][0]:+.1%}', 11, 1) + pad(f'{row["GUARD"][0]:+.1%}', 11, 1) +
          pad(f'{(row["GUARD"][0] - row["base"][0]) * 100:+.1f}pp', 10, 1) +
          pad(f'{row["base"][1]:.1%}', 10, 1) + pad(f'{row["GUARD"][1]:.1%}', 10, 1))
print(pad("累计", 10) + pad(f'{tot["base"] - 1:+.1%}', 11, 1) +
      pad(f'{tot["WARM"] - 1:+.1%}', 11, 1) + pad(f'{tot["GUARD"] - 1:+.1%}', 11, 1) +
      pad(f'{(tot["GUARD"] - tot["base"]) * 100:+.1f}pp', 10, 1))

# ---------- 3. 分币 sleeve ----------
print("\n【3】分币 sleeve（含成本与资金费，起点 2024-01-01 / 2025 全年）")
print(pad("品种", 8) + pad("全base", 11, 1) + pad("全WARM", 11, 1) + pad("全GUARD", 11, 1) +
      pad("2025base", 10, 1) + pad("2025GUARD", 10, 1))
for c in COINS:
    a, b, g = RES[("base", c)], RES[("WARM", c)], RES[("GUARD", c)]
    print(pad(c, 8) + pad(f'{a["all"] - 1:+.1%}', 11, 1) + pad(f'{b["all"] - 1:+.1%}', 11, 1) +
          pad(f'{g["all"] - 1:+.1%}', 11, 1) +
          pad(f'{a["y25"] - 1:+.1%}', 10, 1) + pad(f'{g["y25"] - 1:+.1%}', 10, 1))

# ---------- 4. HYPE 2025 逐笔 ----------
print("\n【4】HYPE 2025 逐笔")
for v, _ in ARMS:
    ts = [t for t in RES[(v, "HYPE")]["trs"] if "2025-01-01" <= t["d"] < "2026-01-01"]
    print(f'  {pad(v, 6)} {len(ts)} 笔  总 {sum(t["r"] for t in ts):+.1f}R  '
          f'胜 {sum(1 for t in ts if t["r"] > 0)}  明细: '
          + ", ".join(f'{t["d"]} {"多" if t["side"] > 0 else "空"}@{t["entry"]:.2f}'
                      f'({t["r"]:+.1f}R)' for t in ts))

# ---------- 5. 全样本组合 ----------
print("\n【5】全样本组合（2024-01-01 起，权重固定不重置）")
for v, _ in ARMS:
    r, dd, fin = stat(fixed_share(navs_of(v, "2024-01-01"), [W[c] for c in COINS]))
    calmar = r / abs(dd) if dd else 0
    print(f"  {pad(v, 6)} 收益 {r:+.1%}  最大回撤 {dd:.1%}  Calmar {calmar:.2f}")

# ---------- 6. 样本外：上线初期 400 天 ----------
# 起点效应几乎全部发生在各币生命早期（2018-2022），落在 2024 起的回测窗口之外，
# 所以「全样本组合 +5pp」根本测不出这条规则的价值。真正的问题是：
# **明天上一个新币，这条规则帮不帮忙？** 于是把每个币截断到「上线后 400 天」重跑。
print("\n【6】样本外：各币「上线后 400 天」（= 新币情景，与 HYPE 同类）")
print(pad("品种", 7) + pad("窗口", 24, 1) + pad("base", 11, 1) + pad("WARM", 11, 1) +
      pad("GUARD", 11, 1) + pad("INITFIX", 11, 1) + pad("IF差", 10, 1))
wins = {"base": 0, "GUARD": 0, 0: 0}
winw = {"base": 0, "WARM": 0, 0: 0}
for c in COINS:
    b = BARS.get(c) or HB
    d0 = b[0]["date"][:10]
    import datetime as _dt
    cut = (_d(d0) + _dt.timedelta(days=400)).isoformat()
    bt = [x for x in b if x["date"][:10] <= cut]
    if len(bt) < 200:
        continue
    four = (c == "HYPE")
    b4t = bt if four else agg4h(bt)
    dailyt = to_daily(bt)
    dts = [x["date"] for x in dailyt]
    closes = [x["close"] for x in dailyt]
    if len(closes) < 130:
        continue
    k0 = idx_of(b4t, dts[120])
    row = {}
    for vname, kw in ARMS:
        st, _dv, tg = daily_states(closes, 120, 70, 30, True, 2, True, **kw)
        rm, bl, cur = {}, {}, None
        for d, s, t in zip(dts, st, tg):
            if t is not None:
                cur = t
            if s in ("UP", "RANGE", "DOWN"):
                rm[d], bl[d] = s, cur
        if not rm:
            row[vname] = (0.0, 0)
            continue
        rg4 = align_regime_safe(b4t, rm)
        blood4 = align_regime_safe(b4t, bl)
        sig, _tr = BT.signals(b4t, rg4, atr_wilder(b4t, 14), vol_q=BT.VOL_Q,
                              coin=c, blood4=blood4)
        base = [BT.BASE.get(r, 0.0) for r in rg4]
        pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
        first_s = st[dts.index(min(rm))]
        lock = next((i for i in range(dts.index(min(rm)) + 1, len(st))
                     if st[i] != first_s), len(st)) - dts.index(min(rm))
        row[vname] = (sleeve(b4t, base, pos, k0, len(b4t)), lock)
    d = row["GUARD"][0] - row["base"][0]
    dw = row["WARM"][0] - row["base"][0]
    di = row["INITFIX"][0] - row["base"][0]
    wins["GUARD" if d > 1e-9 else ("base" if d < -1e-9 else 0)] += 1
    winw["WARM" if dw > 1e-9 else ("base" if dw < -1e-9 else 0)] += 1
    print(pad(c, 7) + pad(f"{dts[120]}~{dts[-1]}", 24, 1) +
          pad(f'{row["base"][0] - 1:+.1%}', 11, 1) + pad(f'{row["WARM"][0] - 1:+.1%}', 11, 1) +
          pad(f'{row["GUARD"][0] - 1:+.1%}', 11, 1) + pad(f'{d * 100:+.1f}pp', 10, 1) +
          pad(f'{row["INITFIX"][0] - 1:+.1%}', 11, 1) + pad(f'{di * 100:+.1f}pp', 10, 1))
print(f"  WARM  逐币：改善 {winw['WARM']} / 变差 {winw['base']} / 不变 {winw[0]}")
print(f"  GUARD 逐币：改善 {wins['GUARD']} / 变差 {wins['base']} / 不变 {wins[0]}")
iu = sum(1 for c in COINS if RES[("INITFIX", c)]["y25"] > RES[("base", c)]["y25"] + 1e-9)
idn = sum(1 for c in COINS if RES[("INITFIX", c)]["y25"] < RES[("base", c)]["y25"] - 1e-9)
print(f"  INITFIX 2025 分币：改善 {iu} / 变差 {idn} / 不变 {len(COINS) - iu - idn}")

# ---------- 7. 闸门窗口敏感性（lb 网格） ----------
# GUARD 的 lb 不能看着 HYPE 挑。这里一次跑 10/20/30/60/120 五档，
# 看「改善/变差」与 HYPE 结果是否随窗口稳定 —— 只有一档好看 = 拟合。
print("\n【7】GUARD 闸门窗口敏感性（近 lb 日动量 vs 初始方向）")
import datetime as _dtm
CACHE = {}
for lb in (10, 20, 30, 60, 120):
    res, hyp = {}, None
    for c in COINS:
        b = BARS.get(c) or HB
        d0 = b[0]["date"][:10]
        cut = (_d(d0) + _dtm.timedelta(days=400)).isoformat()
        bt = [x for x in b if x["date"][:10] <= cut]
        four = (c == "HYPE")
        b4t = bt if four else agg4h(bt)
        dt_ = to_daily(bt)
        dts = [x["date"] for x in dt_]
        closes = [x["close"] for x in dt_]
        if len(closes) < 130:
            continue
        k0 = idx_of(b4t, dts[120])
        out = {}
        for tag, wc in (("base", False), ("g", "guard")):
            st, _dv, tg = daily_states(closes, 120, 70, 30, True, 2, True,
                                       warm_cross=wc, warm_guard_lb=lb)
            rm, bl, cur = {}, {}, None
            for d, s, t in zip(dts, st, tg):
                if t is not None:
                    cur = t
                if s in ("UP", "RANGE", "DOWN"):
                    rm[d], bl[d] = s, cur
            if not rm:
                out[tag] = 0.0
                continue
            rg4 = align_regime_safe(b4t, rm)
            blood4 = align_regime_safe(b4t, bl)
            sig, _tr = BT.signals(b4t, rg4, atr_wilder(b4t, 14), vol_q=BT.VOL_Q,
                                  coin=c, blood4=blood4)
            bs = [BT.BASE.get(r, 0.0) for r in rg4]
            pos = [bs[k] + BT.ADD * sig[k] for k in range(len(sig))]
            out[tag] = sleeve(b4t, bs, pos, k0, len(b4t))
        res[c] = out["g"] - out["base"]
        if c == "HYPE":
            _fi = dts.index(min(rm))
            hyp = (res[c], st[_fi], dts[_fi])
    up = sum(1 for v in res.values() if v > 1e-9)
    dn = sum(1 for v in res.values() if v < -1e-9)
    print(f"  lb={lb:3}日  改善 {up} / 变差 {dn} / 不变 {len(res) - up - dn}"
          f"   HYPE {hyp[0] * 100:+.1f}pp  首段 {hyp[2]} {hyp[1]}")
