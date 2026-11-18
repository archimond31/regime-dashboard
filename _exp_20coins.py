#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_20coins.py —— 币种从 10 个扩到 20 个：加哪些、收益怎么变

**为什么要重跑而不是直接用 skeleton20.json**
那份产物是 R 口径（每笔交易的平均 R），不含权重、资金费、滑点、再平衡，
也没接 VT60。要回答「收益表现如何」必须用组合口径。

口径（与 _exp_vt_long.py 完全一致，便于横向比较）：
  季度再平衡、50 万本金、含手续费(0.04%/边)/资金费(0.03%/日 × 净永续敞口)/滑点(A=100)、
  仓位按 portfolio_config 的不对称结构（UP 1.0~1.5 / RANGE 1.0 / DOWN 0~-0.5），
  叠加 VT60（vol_target.coef_series × κ=1.22）。

共同窗口 2022-01-01 起：
  新加的 10 个里最晚上线的是 NEAR(2020-08) / AVAX(2020-07) / SAND(2020-06)，
  2022 年起都有 ≥3.8 年；HYPE 2024-12 才上线，之前只按 0 处理（不占权重收益）。

五节：
  1) 数据覆盖 + 单币独立表现（自上线起，含 VT）
  2) 组合对比：10 / 15 / 20 币
  3) 逐年
  4) 20 币臂的分币贡献 + 相关性/集中度
  5) 容量：5% 权重下各币峰值占「最差单根 4H 现货成交额」
"""
import csv
import io
import json
import math
import os
import sys
import zipfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import CACHE, SYMBOLS, load_bars, to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h

OLD10 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE", "HYPE"]
NEW10 = ["DOGE", "LTC", "TRX", "XLM", "ATOM", "AVAX", "DOT", "NEAR", "SAND", "ALGO"]
ALT = ["ADA"]                      # 备选：与 SAND/ALGO 争最后两个名额
UNI20 = OLD10 + NEW10
ALL = UNI20 + ALT
CAP = 500_000
F4 = BT.FUND_DAILY / 6.0


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
    reg, blood = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg)
    bl4 = align_regime_safe(b4, blood)
    atr = atr_wilder(b4, 14)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    pos = [base[k] + BT.ADD * sig[k] for k in range(len(sig))]
    return b4, base, pos, trades, rg4


DATA = {}
for c in ALL:
    try:
        DATA[c] = load_coin(c)
    except Exception as e:                      # noqa: BLE001
        print(f"  !! {c} 加载失败 {type(e).__name__}: {e}")

# VT60 系数
COEF = {c: VT.coef_series(DATA[c][0]) for c in DATA}
POSV = {c: [p * k * VT.KAPPA for p, k in zip(DATA[c][2], COEF[c])] for c in DATA}

print("【1】数据覆盖与单币独立表现（自上线起，含 VT60）")
print(pad("币", 6) + pad("首日", 12) + pad("末日", 12) + pad("笔数", 6) +
      pad("netR", 8) + pad("PF", 7) + pad("倍数", 9) + pad("年化", 9) + pad("回撤", 9))
SOLO = {}
for c in ALL:
    b4, base, pos, trades, _rg = DATA[c]
    pv = POSV[c]
    nav, k0 = 1.0, 0
    for k in range(1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pv[k] * r
        nav *= x if x > 0 else 0.0
    # agg4h 的输出没有 t 字段（见 MEMORY），只能从日期字符串算年数
    _d0 = date(*map(int, b4[0]["date"][:10].split("-")))
    _d1 = date(*map(int, b4[-1]["date"][:10].split("-")))
    yrs = max((_d1 - _d0).days / 365, 0.1)
    nets = [t.get("r", 0.0) for t in trades]
    wins = [x for x in nets if x > 0]
    pf = (sum(wins) / abs(sum(x for x in nets if x <= 0))
          if any(x < 0 for x in nets) else float("inf"))
    # 回撤（按 4H 净值序列）
    nv, pk, dd = 1.0, 1.0, 0.0
    for k in range(1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        nv *= max(1 + pv[k] * r, 0.0)
        pk = max(pk, nv)
        dd = min(dd, nv / pk - 1)
    SOLO[c] = dict(mult=nv, cagr=nv ** (1 / yrs) - 1, dd=dd, n=len(nets),
                   netR=sum(nets) / max(len(nets), 1), pf=pf,
                   first=b4[0]["date"], last=b4[-1]["date"])
    s = SOLO[c]
    print(pad(c, 6) + pad(s["first"], 12) + pad(s["last"], 12) + pad(s["n"], 6) +
          pad(f"{s['netR']:+.2f}", 8) + pad(f"{s['pf']:.2f}", 7) +
          pad(f"{s['mult']:.1f}×", 9) + pad(f"{s['cagr']:+.1%}", 9) +
          pad(f"{s['dd']:.1%}", 9))


# ---------------- 组合 ----------------
def nav_series(c, w, d0="2020-01-01"):
    b4 = DATA[c][0]
    base, pos = DATA[c][1], POSV[c]
    out, nav = {}, 1.0
    k0 = next((k for k, b in enumerate(b4) if b["date"] >= d0), len(b4))
    qv = QV.get(c) or 1e12
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = pos[k] - base[k]                     # 现货底仓口径：资金费只对永续部分
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)
            notional = nav * CAP * w * d
            nav *= (1 - 100 / 1e4 * math.sqrt(notional / qv) * d)
        out[b4[k]["date"]] = nav
    return out


_NAVC = {}


def get_nav(c, w):
    key = (c, round(w, 4))
    if key not in _NAVC:
        _NAVC[key] = nav_series(c, w)
    return _NAVC[key]


def portfolio(coins, d0, d1=None):
    nv = {c: get_nav(c, W[c]) for c in coins}
    dates = sorted(set().union(*[set(n) for n in nv.values()]))
    dates = [d for d in dates if d >= d0 and (d1 is None or d < d1)]
    if not dates:
        return None, None
    start = dates[0]
    last, units = {}, {}
    for c in coins:
        ks = sorted(nv[c])
        prev = [d for d in ks if d <= start]
        last[c] = nv[c][prev[-1]] if prev else 1.0
        units[c] = W[c] / last[c]
    cur_q, out = None, []
    for d in dates:
        for c in coins:
            if d in nv[c]:
                last[c] = nv[c][d]
        v = sum(units[c] * last[c] for c in coins)
        q = (int(d[:4]), (int(d[5:7]) - 1) // 3)
        if cur_q is not None and q != cur_q:
            g = sum(abs(v * W[c] - units[c] * last[c]) for c in coins) / v / 2
            v *= (1 - g * 7e-4)
            for c in coins:
                units[c] = v * W[c] / last[c]
        cur_q = q
        out.append((d, v))
    vals = [x[1] for x in out]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / vals[0] - 1, dd


# ---------------- 容量：从 zip 里取 quote_volume ----------------
def worst_4h_qv(sym, months=3):
    """近 N 个月「最差单根 4H 现货成交额」。load_bars 丢掉了 volume，只能自己读 zip。"""
    files = sorted(f for f in os.listdir(CACHE) if f.startswith(sym + "-1h-"))
    if not files:
        return None
    buckets = {}
    for fn in files[-months:]:
        try:
            with zipfile.ZipFile(os.path.join(CACHE, fn)) as z:
                name = z.namelist()[0]
                with z.open(name) as fh:
                    for row in csv.reader(io.TextIOWrapper(fh, "utf-8")):
                        if not row or not row[0].isdigit():
                            continue
                        ts = int(row[0])
                        if ts > 1e14:              # 微秒 → 毫秒
                            ts //= 1000
                        q = float(row[7]) if len(row) > 7 else 0.0
                        b = ts // (4 * 3600 * 1000)
                        buckets[b] = buckets.get(b, 0.0) + q
        except Exception:                          # noqa: BLE001
            continue
    if not buckets:
        return None
    vals = sorted(buckets.values())
    return vals[max(int(len(vals) * 0.01), 0)]


QV = {c: worst_4h_qv(SYMBOLS[c]) for c in ALL if c != "HYPE"}
QV["HYPE"] = PC.CAP_4H_WORST.get("HYPE")

W = {}


def set_w(coins, weights):
    """设定本臂的权重。NAV 与滑点金额都依赖 w，所以按 (币, 权重) 缓存。"""
    global W
    W = dict(zip(coins, weights))
    return coins


D0 = "2022-01-01"
print("\n【2】组合对比（2022-01-01 起，50 万，含成本，已叠 VT60）")
print(pad("臂", 16) + pad("期末", 10) + pad("年化", 9) + pad("回撤", 10) +
      pad("Calmar", 8) + pad("2023", 9) + pad("2024", 9) + pad("2025", 9) + pad("2026", 9))
TOP3 = ["DOGE", "NEAR", "ADA"]
SATE = 0.2 / 3          # 卫星仓：新币合计 20%
NOCRV = [c for c in OLD10 if c != "CRV"]
PLAN = [
    ("10 币（现状）", OLD10, [PC.WEIGHTS[c] for c in OLD10]),
    ("9 币（去 CRV）", NOCRV, [1 / 9] * 9),
    ("11 币 去CRV+DOGE+NEAR", NOCRV + ["DOGE", "NEAR"], [1 / 11] * 11),
    ("12 币(+DOGE+NEAR)", OLD10 + ["DOGE", "NEAR"], [1 / 12] * 12),
    ("13 币(+3 卫星等权)", OLD10 + TOP3, [1 / 13] * 13),
    ("10币80%+3卫星", OLD10 + TOP3,
     [PC.WEIGHTS[c] * 0.8 for c in OLD10] + [SATE] * 3),
    ("15 币", OLD10 + NEW10[:5], [1 / 15] * 15),
    ("20 币等权", UNI20, [0.05] * 20),
    ("21 币(含ADA)", ALL, [1 / 21] * 21),
]
RES = {}
for name, coins, ws in PLAN:
    set_w(coins, ws)
    r, dd = portfolio(coins, D0)
    yrs = 4.88
    RES[name] = (r, dd)
    line = pad(name, 16) + pad(f"{1 + r:.2f}×", 10) + pad(f"{(1 + r) ** (1 / yrs) - 1:+.1%}", 9) + \
        pad(f"{dd:.1%}", 10) + pad(f"{r / abs(dd):.1f}", 8)
    for y in (2023, 2024, 2025, 2026):
        rr, _ = portfolio(coins, f"{y}-01-01", f"{y + 1}-01-01" if y < 2026 else None)
        line += pad(f"{rr:+.0%}", 9)
    print(line)

print("\n【2b】事前检验：只用 2022 年之前的数据，新币排第几？")
print("      （上面 TOP3 = DOGE/NEAR/ADA 是按全样本选的，这里看有没有前视）")
print(pad("币", 6) + pad("2022前笔数", 11) + pad("2022前netR", 12) +
      pad("2022前PF", 10) + pad("2022起netR", 12) + pad("名次变化", 10))
pre = []
for c in NEW10 + ALT:
    b4, _b, _p, trades, _r = DATA[c]
    a = [t.get("r", 0) for t in trades if t["d"] < "2022-01-01"]
    b = [t.get("r", 0) for t in trades if t["d"] >= "2022-01-01"]
    if not a or not b:
        continue
    wa = [x for x in a if x > 0]
    pf = (sum(wa) / abs(sum(x for x in a if x <= 0))
          if any(x <= 0 for x in a) else 99)
    pre.append((sum(a) / len(a), c, len(a), pf, sum(b) / len(b)))
pre.sort(reverse=True)
rk = {c: i + 1 for i, (_m, c, _n, _p2, _b) in enumerate(pre)}
post = sorted(pre, key=lambda x: -x[4])
rk2 = {c: i + 1 for i, (_m, c, _n, _p2, _b) in enumerate(post)}
for m, c, n, pf, b in pre:
    print(pad(c, 6) + pad(n, 11) + pad(f"{m:+.2f}", 12) + pad(f"{pf:.2f}", 10) +
          pad(f"{b:+.2f}", 12) + pad(f"{rk[c]}→{rk2[c]}", 10))

print("\n【3】20 币臂：各币贡献（2022 起累计，W×sleeve 收益，加总近似）")
set_w(UNI20, [0.05] * 20)
rows = []
for c in UNI20:
    nv = get_nav(c, W[c])
    seg = {d: v for d, v in nv.items() if d >= D0}
    if len(seg) < 2:
        continue
    vs = list(seg.values())
    rows.append(((vs[-1] / vs[0] - 1) * W[c] * 100, c, vs[-1] / vs[0] - 1))
tot = sum(r[0] for r in rows)
print(pad("币", 6) + pad("sleeve", 11) + pad("贡献pp", 10) + pad("占比", 9))
for d, c, m in sorted(rows, reverse=True):
    print(pad(c, 6) + pad(f"{m:+.1%}", 11) + pad(f"{d:+.1f}", 10) +
          pad(f"{d / tot:+.0%}" if tot else "—", 9))
print(pad("合计", 6) + " " * 11 + pad(f"{tot:+.1f}", 10))

# 相关性：用**原始价格**日收益，不用 sleeve 净值
# 为什么：sleeve 空仓时收益恒为 0，会把相关系数系统性稀释（实测 0.28 vs 价格 0.6+）
DC = {}
for c in ALL:
    m = {}
    for b in DATA[c][0]:
        m[b["date"][:10]] = b["close"]
    DC[c] = m


def _rets(coins, d0):
    ks = sorted(set.intersection(*[set(DC[c]) for c in coins]))
    ks = [d for d in ks if d >= d0]
    out = {c: [] for c in coins}
    for i in range(1, len(ks)):
        for c in coins:
            p0, p1 = DC[c][ks[i - 1]], DC[c][ks[i]]
            out[c].append(p1 / p0 - 1 if p0 else 0.0)
    return out, ks[1:]


def _pair(v):
    pa = []
    for i, a in enumerate(v):
        for b in list(v)[i + 1:]:
            x, y = v[a], v[b]
            n = len(x)
            mx, my = sum(x) / n, sum(y) / n
            cov = sum((x[k] - mx) * (y[k] - my) for k in range(n)) / n
            sx = math.sqrt(sum((q - mx) ** 2 for q in x) / n)
            sy = math.sqrt(sum((q - my) ** 2 for q in y) / n)
            pa.append((cov / (sx * sy) if sx and sy else 0.0, a, b))
    return sorted(pa)


print("\n【4】相关性（原始价格日收益，2022 起）")
for label, coins in (("现有 10 币", OLD10), ("扩展 20 币", UNI20)):
    v, ds = _rets(coins, "2022-01-01")
    pa = _pair(v)
    alldn = sum(1 for i in range(len(ds))
                if all(v[c][i] < 0 for c in coins))
    print(pad(label, 12) + f"样本 {len(ds)} 天  平均相关 {sum(q[0] for q in pa) / len(pa):.2f}"
          f"   最高 {pa[-1][1]}~{pa[-1][2]} {pa[-1][0]:.2f}"
          f"   同日全跌 {alldn} 天（{alldn / len(ds):.1%}）")
v, ds = _rets(OLD10, "2022-01-01")
pa = _pair(v)
print("  对照：10 币内部相关性最高的三对：" +
      "、".join(f"{a}~{b} {s:.2f}" for s, a, b in pa[-3:]))
v20, _ = _rets(UNI20, "2022-01-01")
p20 = _pair(v20)
print("  20 币里与现有 10 币最不相关的新币（越低越能分散）：")
old = set(OLD10)
cross = [(s, a, b) for s, a, b in p20 if (a in old) ^ (b in old)]
for s, a, b in cross[:5]:
    print(f"    {a}~{b} {s:+.2f}")

print("\n【5】容量：5% 权重下的峰值名义，占「最差单根 4H 现货成交额」")
print(pad("币", 6) + pad("最差4H额", 13) + pad("5万本金", 10) + pad("20万", 10) +
      pad("50万", 10) + pad("判定(50万)", 12))
for c in UNI20:
    qv = QV.get(c)
    if not qv:
        print(pad(c, 6) + pad("—", 13) + pad("—", 10) + pad("—", 10) + pad("—", 10) +
              pad("无数据", 12))
        continue
    peak_w = 0.05 * (PC.BASE_RATIO + PC.ADD_RATIO) * VT.KAPPA      # 满峰值相对本金
    cells = []
    for cap in (50_000, 200_000, 500_000):
        imp = cap * peak_w / qv
        cells.append(imp)
    flag = ("超标" if cells[-1] > 0.03 else "需拆单" if cells[-1] > 0.01
            else "可接受" if cells[-1] > 0.005 else "宽松")
    print(pad(c, 6) + pad(f"{qv / 1e4:.0f}万", 13) +
          "".join(pad(f"{x * 100:.2f}%", 10) for x in cells) + pad(flag, 12))

# ---------------- 起点敏感性 ----------------
print("\n【6】起点敏感性：不同起点下，扩容方案还成立吗")
KEY = [("10 币（现状）", OLD10, [PC.WEIGHTS[c] for c in OLD10]),
       ("9 币（去 CRV）", NOCRV, [1 / 9] * 9),
       ("11 币 去CRV+2新", NOCRV + ["DOGE", "NEAR"], [1 / 11] * 11),
       ("20 币等权", UNI20, [0.05] * 20)]
for d0 in ("2022-01-01", "2023-01-01", "2024-01-01"):
    print(pad(f"起点 {d0}", 18) + pad("期末", 10) + pad("年化", 9) + pad("回撤", 10) +
          pad("Calmar", 8))
    yrs = { "2022-01-01": 4.88, "2023-01-01": 3.88, "2024-01-01": 2.88 }[d0]
    for name, coins, ws in KEY:
        set_w(coins, ws)
        r, dd = portfolio(coins, d0)
        if r is None:
            continue
        print(pad(name, 18) + pad(f"{1 + r:.2f}×", 10) +
              pad(f"{(1 + r) ** (1 / yrs) - 1:+.1%}", 9) + pad(f"{dd:.1%}", 10) +
              pad(f"{r / abs(dd):.1f}", 8))
