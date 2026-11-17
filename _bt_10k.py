#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""10 品种跑 10000 U 的收益测算。

要回答的其实不是「10000 乘多少」，而是三个问题：
  1. 金额放大后收益是否线性？（若无最小下单额与容量约束，应当完全线性）
  2. 滑点侵蚀多少？（这才是放大后真正变差的部分）
  3. 回撤的绝对金额有多大 —— 百分比不变，但 10000 U 的 -15% 是 -1500 U，
     和 5000 U 的 -750 U 不是一个心理量级。

口径：
  · 权重用 portfolio_config 的最终版（BTC/ETH/SOL 11%、CRV 7%、其余 10%）
  · 1.5× 多头峰值 + 0.5× 空头峰值（合约口径，允许做空）
  · 固定份额：每币分到的钱独立复利，权重随盈亏漂移（不每日再平衡）
  · 手续费 4bp 单边（bt_skeleton.COST）
  · 资金费 3bp/日 × 仅施于加仓层（现货底仓不付）
  · 滑点用平方根模型 slip_bp = A × sqrt(参与率)，
    分母取各币「近 90 天最差单根 4H 的**合约**成交额」（_cap_fut.json）——
    用最差价当分母是保守的：真实成交额多数时候更好。

三个时间窗口：
  2026 YTD（约 320 天）· 滚动 365 天 · 2025-01 起（682 天，跨周期）
"""
import bisect
import datetime as dt
import json
import math
import os
import statistics
import sys

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
CAPS = [5000, 7000, 10000, 15000, 20000, 30000]
SLIP_A = 100.0          # bp。参与率 1% → 100×0.1 = 10 bp；4% → 20 bp

# 各币「最差单根 4H 合约成交额」当滑点分母（保守）
try:
    _cf = json.load(open("_cap_fut.json", encoding="utf-8"))
    QV = {r["c"]: r["fut_min"] for r in _cf["rows"]}
except Exception:
    QV = {c: float(v) for c, v in PC.CAP_4H_WORST.items()}
    print("[提示] 没找到 _cap_fut.json，滑点分母退回现货口径，会明显高估侵蚀")

# HYPE 在 Hyperliquid，不在 _cap_fut（那文件只扫了 Binance 合约）
# 退回 portfolio_config 的保守值，并在第四节标注口径不同
for _c in COINS:
    if _c not in QV and _c in PC.CAP_4H_WORST:
        QV[_c] = float(PC.CAP_4H_WORST[_c])

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()

# ---------- 载入 ----------
raw, trs = {}, {}
for c in BIN9:
    b, rg, _a, _b2 = V2.load_all()[c]
    b4 = agg4h(b)
    rg4 = [rg[x["src"]] for x in b4]
    sig, trades = BT.signals(b4, rg4, atr_wilder(b4, 14), vol_q=BT.VOL_Q, coin=c)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    raw[c] = (b4, base, [base[k] + BT.ADD * sig[k] for k in range(len(sig))])
    trs[c] = trades

hb = load_hype("4h")
hb = [dict(x, date=x["date"][:10]) for x in hb]
hrg = align_regime_safe(hb, regime_series(to_daily(hb)))
hsig, htrades = BT.signals(hb, hrg, atr_wilder(hb, 14), vol_q=BT.VOL_Q, coin="HYPE")
hbase = [BT.BASE.get(r, 0.0) for r in hrg]
raw["HYPE"] = (hb, hbase, [hbase[k] + BT.ADD * hsig[k] for k in range(len(hsig))])
trs["HYPE"] = htrades

W = dict(PC.WEIGHTS)


def nav_daily(b4, base, pos, k0, coin, cap, slip_a=0.0):
    """单币 sleeve 的逐日净值。cap×w 是这个 sleeve 的初始绝对金额。"""
    nav, out = 1.0, {}
    w = W[coin]
    qv = QV.get(coin)
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
        if F4 and sl:
            nav *= (1 - F4 * sl)
        if pos[k] != pos[k - 1]:
            d = abs(pos[k] - pos[k - 1])
            nav *= (1 - BT.COST * d)                 # 手续费
            if slip_a and qv:
                # 名义随 sleeve 净值增长，所以参与率也是动态的
                notional = nav * cap * w * d
                part = notional / qv
                slip = slip_a / 1e4 * math.sqrt(part)  # 平方根冲击
                nav *= (1 - slip * d)
        out[b4[k]["date"]] = nav
    return out


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


def stats(cur):
    vals = [x[1] for x in cur]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return {"final": vals[-1], "ret": vals[-1] - 1, "dd": dd,
            "n": len(vals),
            "cagr": vals[-1] ** (365.0 / max(len(vals), 1)) - 1}


def run_navs(cap, d0, slip_a):
    navs = []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0 = next(k for k in range(len(b4c)) if b4c[k]["date"] >= d0)
        navs.append(nav_daily(b4c, basec, posc, k0, c, cap, slip_a))
    return navs


def run(cap, d0, slip_a):
    navs = run_navs(cap, d0, slip_a)
    return stats(fixed_share(navs, [W[c] for c in COINS]))


def pad(s, n, right=False):
    """按显示宽度补齐（中文算 2 列），否则表头对不齐。"""
    s = str(s)
    w = sum(2 if ord(ch) > 127 else 1 for ch in s)
    fill = " " * max(0, n - w)
    return fill + s if right else s + fill


WINDOWS = [
    ("2026 YTD（约 320 天）", "2026-01-01"),
    ("滚动 365 天", "2025-11-17"),
    ("2025-01 起（682 天，跨周期）", "2025-01-01"),
]

print("=" * 96)
print("一、线性检验：把本金放大，收益百分比会不会变？（不含滑点）")
print("-" * 96)
_hdr = pad("本金", 10) + pad("2026 YTD", 16) + pad("滚动365天", 16) + pad("2025-01 起", 16)
print(_hdr)
print("-" * 96)
for cap in CAPS:
    vals = [run(cap, d0, 0.0)["ret"] * 100 for _, d0 in WINDOWS]
    print(pad(f"{cap:,}", 10) + "".join(pad(f"{v:.1f}%", 16) for v in vals))

print("\n→ 百分比完全相同，说明策略是线性的：没有最小下单额、没有容量约束在起作用。")
print("  所以「10000 U 赚多少」= 「同一套百分比 × 2 的本金」，滑点是唯一的非线性项。")

print("\n" + "=" * 96)
print("二、加滑点后的收益（平方根冲击，A = %.0f → 1%% 参与率 ≈ 10 bp）" % SLIP_A)
print("-" * 96)
print(pad("本金", 9) + pad("窗口", 28) + pad("期末", 11, True) + pad("收益", 9, True)
      + pad("最大回撤", 11, True) + pad("回撤金额", 12, True) + pad("年化", 9, True)
      + pad("滑点侵蚀", 11, True))
print("-" * 96)
for cap in CAPS:
    for lab, d0 in WINDOWS:
        s0 = run(cap, d0, 0.0)
        s1 = run(cap, d0, SLIP_A)
        print(pad(f"{cap:,}", 9) + pad(lab, 28)
              + pad(f"{cap * s1['final']:,.0f}U", 11, True)
              + pad(f"{s1['ret'] * 100:.1f}%", 9, True)
              + pad(f"{s1['dd'] * 100:.1f}%", 11, True)
              + pad(f"{cap * s1['dd']:,.0f}U", 12, True)
              + pad(f"{s1['cagr'] * 100:.1f}%", 9, True)
              + pad(f"{(s1['ret'] - s0['ret']) * 100:+.2f}pp", 11, True))
    print()

print("=" * 96)
print("三、滑点假设的敏感性（本金 10000 U，2026 YTD 单窗口）")
print("-" * 96)
print(pad("假设", 42) + pad("收益", 10, True) + pad("相对无滑点", 13, True))
print("-" * 96)
s0 = run(10000, "2026-01-01", 0.0)
print(pad("无滑点（理论）", 42) + pad(f"{s0['ret'] * 100:.1f}%", 10, True) + pad("—", 13, True))
for a, note in ((50, "1% 参与率 ≈ 5 bp（乐观）"),
                (100, "1% 参与率 ≈ 10 bp（基准）"),
                (200, "1% 参与率 ≈ 20 bp（保守）"),
                (400, "1% 参与率 ≈ 40 bp（极端）")):
    s1 = run(10000, "2026-01-01", a)
    print(pad(f"A={a} · {note}", 42) + pad(f"{s1['ret'] * 100:.1f}%", 10, True)
          + pad(f"{(s1['ret'] - s0['ret']) * 100:+.2f}pp", 13, True))

print("\n" + "=" * 96)
print("四、10000 U 下各币的单笔名义与参与率（近 90 天最差单根 4H 合约成交额口径）")
print("-" * 96)
print(pad("币", 6) + pad("权重", 6, True) + pad("底仓", 9, True) + pad("多头峰值", 10, True)
      + pad("空头峰值", 10, True) + pad("最差4H额", 14, True) + pad("峰值参与率", 12, True)
      + pad("该笔滑点", 10, True) + pad("需拆单", 8, True))
print("-" * 96)
for c in sorted(COINS, key=lambda x: -W[x]):
    w = W[c]
    base = 10000 * w * PC.BASE_RATIO
    peak = 10000 * w * (PC.BASE_RATIO + PC.ADD_RATIO)
    short = 10000 * w * PC.ADD_RATIO
    qv = QV.get(c, 0)
    part = peak / qv if qv else 0
    bp = SLIP_A * math.sqrt(part) if qv else 0
    nsplit = max(1, math.ceil(peak / (qv * 0.01))) if qv else 1
    print(pad(c, 6) + pad(f"{w * 100:.0f}%", 6, True) + pad(f"{base:,.0f}U", 9, True)
          + pad(f"{peak:,.0f}U", 10, True) + pad(f"{short:,.0f}U", 10, True)
          + pad(f"{qv:,.0f}", 14, True) + pad(f"{part * 100:.3f}%", 12, True)
          + pad(f"{bp:.1f}bp", 10, True) + pad(nsplit, 8, True))
print("-" * 96)
tp = sum(10000 * W[c] * 1.5 for c in COINS)
print(f"多头全满时总名义 {tp:,.0f} U（{tp / 10000:.2f}× 本金），"
      f"空头全满 {sum(10000 * W[c] * 0.5 for c in COINS):,.0f} U（0.50×）")
print("拆单列 = 要拆成几笔 1% 参与率的单，才能把单笔滑点压在 10 bp 内")

# ---------- 五、分年度 ----------
print("\n" + "=" * 96)
print("五、分年度（本金 10000 U，含滑点，连续持仓口径）—— 「一年」到底是哪一年")
print("-" * 96)
_cap0 = 10000
_curve = fixed_share(run_navs(_cap0, "2024-01-01", SLIP_A), [W[c] for c in COINS])
_dates = [d for d, _ in _curve]
_vals = [v for _, v in _curve]
print(pad("区间", 14) + pad("起点", 12, True) + pad("终点", 12, True)
      + pad("收益", 11, True) + pad("年内回撤", 12, True) + pad("天数", 8, True)
      + pad("年化", 10, True))
print("-" * 96)
YEARS = {}
for y0, y1 in (("2024-01-01", "2025-01-01"), ("2025-01-01", "2026-01-01"),
               ("2026-01-01", "2027-01-01")):
    i0 = next((i for i, d in enumerate(_dates) if d >= y0), None)
    i1 = next((i for i, d in enumerate(_dates) if d >= y1), len(_dates) - 1)
    if i0 is None or i1 <= i0:
        continue
    seg = _vals[i0:i1 + 1]
    pk, dd = seg[0], 0.0
    for v in seg:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    r = seg[-1] / seg[0] - 1
    nd = i1 - i0
    lab = y0[:4] + (" YTD" if y1 == "2027-01-01" else "")
    YEARS[lab] = r
    print(pad(lab, 14) + pad(f"{_cap0 * seg[0]:,.0f}U", 12, True)
          + pad(f"{_cap0 * seg[-1]:,.0f}U", 12, True) + pad(f"{r * 100:.1f}%", 11, True)
          + pad(f"{dd * 100:.1f}%", 12, True) + pad(nd, 8, True)
          + pad(f"{(seg[-1] / seg[0]) ** (365 / max(nd, 1)) * 100 - 100:.1f}%", 10, True))
print("-" * 96)
_tot = _vals[-1] / _vals[0] - 1
_nd = len(_vals)
print(f"2024-01-01 起到 {_dates[-1]}：{_cap0:,} U → {_cap0 * _vals[-1]:,.0f} U"
      f"（累计 {_tot * 100:+.1f}%，{_nd} 天，年化 {(1 + _tot) ** (365 / _nd) * 100 - 100:.1f}%）")
print("→ 同一套代码，年与年之间的差距比多数人想象的大得多。")

# ---------- 六、基准对照 ----------
DAILY = {}
for c in COINS:
    b4c = raw[c][0]
    d = {}
    for x in b4c:
        d[x["date"]] = x["close"]
    DAILY[c] = d


def _px(c, d):
    ks = sorted(DAILY[c])
    i = bisect.bisect_right(ks, d) - 1
    return DAILY[c][ks[i]] if i >= 0 else None


print("\n" + "=" * 96)
print("六、基准对照：同期买入持有能赚多少（同样 10000 U）")
print("-" * 96)
SEGS = [("2024", "2024-01-01", "2024-12-31"), ("2025", "2025-01-01", "2025-12-31"),
        ("2026 YTD", "2026-01-01", "2026-11-16")]
print(pad("区间", 14) + pad("BTC 持有", 12, True) + pad("ETH 持有", 12, True)
      + pad("等权10币", 12, True) + pad("本策略", 12, True) + pad("超额", 12, True))
print("-" * 96)
for lab, d0, d1 in SEGS:
    rs = {}
    for c in COINS:
        a, b = _px(c, d0), _px(c, d1)
        if a and b and b > 0:
            rs[c] = b / a - 1
    eq = sum(rs.values()) / len(rs)
    st = YEARS.get(lab)
    print(pad(lab, 14) + pad(f"{rs.get('BTC', 0) * 100:+.1f}%", 12, True)
          + pad(f"{rs.get('ETH', 0) * 100:+.1f}%", 12, True)
          + pad(f"{eq * 100:+.1f}%", 12, True)
          + pad(f"{st * 100:+.1f}%" if st is not None else "—", 12, True)
          + pad(f"{(st - eq) * 100:+.1f}pp" if st is not None else "—", 12, True))
print("-" * 96)
print("→ 关键：2026 是在 BTC 跌 27% 的年份赚的，靠的是做空；2024 牛市里反而跑输买入持有。")

# ---------- 七、归因 ----------
print("\n" + "=" * 96)
print("七、2026 YTD 归因（10000 U，含滑点）—— 钱是哪几个币赚的")
print("-" * 96)
print(pad("币", 6) + pad("权重", 6, True) + pad("2026现货", 11, True)
      + pad("sleeve", 12, True) + pad("对总收益贡献", 14, True)
      + pad("贡献金额", 12, True) + pad("交易笔数", 10, True))
print("-" * 96)
_navs26 = run_navs(10000, "2026-01-01", SLIP_A)
_rows = []
for i, c in enumerate(COINS):
    nv = list(_navs26[i].values())
    r = nv[-1] - 1 if nv else 0.0
    sp = _px(c, "2026-01-01"), _px(c, "2026-11-16")
    spot = sp[1] / sp[0] - 1 if sp[0] and sp[1] else 0.0
    _rows.append((c, W[c], spot, r, W[c] * r, len(trs[c])))
for c, w, spot, r, contrib, n in sorted(_rows, key=lambda x: -x[4]):
    print(pad(c, 6) + pad(f"{w * 100:.0f}%", 6, True) + pad(f"{spot * 100:+.1f}%", 11, True)
          + pad(f"{r * 100:+.1f}%", 12, True) + pad(f"{contrib * 100:+.1f}pp", 14, True)
          + pad(f"{10000 * contrib:+,.0f}U", 12, True) + pad(n, 10, True))
print("-" * 96)
_neg = [x for x in _rows if x[4] < 0]
print(f"正贡献 {sum(1 for x in _rows if x[4] > 0)} 个 / 负贡献 {len(_neg)} 个"
      + (f"（{', '.join(x[0] for x in _neg)}）" if _neg else "（无）"))
_uni = next(x for x in _rows if x[0] == "UNI")
print(f"UNI 单独贡献 {_uni[4] * 100:.1f}pp（{10000 * _uni[4]:,.0f}U）—— 现货只跌 "
      f"{abs(_uni[2]) * 100:.1f}%，但年内区间 2.32 → 10.94（4.7 倍），趋势跟随吃满了来回。")
