#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_delay_1h.py —— 用 1H 数据把「延迟」的空档量出来

前面的延迟检验只有 4H 粒度（晚 1 根 = 4 小时），那是**上界**，不是现实。
人工执行的真实延迟是「看到邮件 → 登录 → 下单」，20 分钟到 2 小时。
4 小时的上界只能说明「很脆」，推不出「30 分钟也不行」—— 必须用 1H 数据量中间。

做法：净值逐 **1H** bar 计算。系数（闸门 / ATR）在 4H 收盘那一刻判定，
但**生效时刻往后推 D 小时**，D ∈ {0, 1, 2, 3, 4, 6}。
  D=0 → 收盘价瞬间成交（回测里的乐观假设）
  D=6 → 晚 1 根 4H 再加 2 小时（最坏的人工情形）
判据：从哪个 D 开始，闸门相对「不开闸门」的超额转负 —— 那就是可执行边界。

注：HYPE 无 1H 归档，本检验只含 11 币（控制组 302 万，12 币口径是 310 万）。
**只有闸门做进了延迟**；`extra`（ATR 吊灯）仍取当桶，未延迟 —— ATR 的延迟敏感性
由 4H 粒度的 `_exp_risk2c.py`【3】覆盖（548 万 → 晚一根 187 万），不在本脚本重复。

结论（2026-11-18）：
  · 闸门 D=0 → 275 万，D≥1 → 270 万，控制组 302 万
    → 与 4H 口径的 277 万 / 310 万 完全一致，两种粒度下都证实「去前视后闸门是负贡献」
  · 4H 采样规则没有「延迟 1 小时」这个档位：错过桶边界就等于等下一个边界，
    所以 D=1/2/3/4 结果完全相同。想要亚 4H 精度，只能把规则本身降到 1H 判定。
"""
import math
import os
import sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 闸门已默认关闭；本脚本要测它，必须显式打开（否则 series() 全是 1.0，测了个寂寞）
os.environ["SKEL_BASEGATE"] = "1"

import base_gate as BG
import bt_skeleton as BT
import portfolio_config as PC
import vol_target as VT
from _proto_4h import atr_wilder
from fetch_binance_vision import load_bars, to_daily
from strategy_1h_rsi import align_regime_safe, regime_and_blood
from test_regime_fit import agg4h, H4_MS

CAP = 500_000
F4 = BT.FUND_DAILY / 6.0
HOUR_MS = 3600_000
START_NAV = "2020-09-01"
ALL12 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "UNI", "AAVE",
         "HYPE", "ZEC", "DOGE", "NEAR"]
W = PC.weights_for(CAP)
TGT = min(1.0, sum(W.values()))
YEARS = list(range(2020, 2027))


def pad(s, n, right=True):
    s = str(s)
    return s.ljust(n) if right else s.rjust(n)


print("加载 11 币 1H + 派生（HYPE 无 1H 归档，不进本检验）…", flush=True)
DATA = {}
for c in ALL12:
    if c == "HYPE":
        continue
    b1 = load_bars(c, "1h", tail=False)
    b4 = agg4h(b1)
    d1 = to_daily(b1)
    reg, blood = regime_and_blood(d1)
    rg4 = align_regime_safe(b4, reg)
    bl4 = align_regime_safe(b4, blood)
    atr = atr_wilder(b4, 14)
    sig, _ = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c, blood4=bl4)
    gate = BG.series(b4, d1, rg4)
    coef = VT.coef_series(b4)
    # 4H 桶 → 索引
    idx = {(b["ts"] - H4_MS) // H4_MS: k for k, b in enumerate(b4)}
    k4s, offs, cs, ds = [], [], [], []
    for b in b1:
        k = idx.get(b["t"] // H4_MS)
        if k is None:
            continue
        k4s.append(k)
        offs.append((b["t"] + HOUR_MS) - b4[k]["ts"])   # -3h..0
        cs.append(b["close"])
        ds.append(b["date"][:10])
    DATA[c] = dict(b4=b4, rg4=rg4, sig=sig, atr=atr, gate=gate, coef=coef,
                   k4=k4s, off=offs, cs=cs, ds=ds)
COINS = list(DATA)
print(f"完成（{len(COINS)} 币）", flush=True)


def trail_atr(D, k):
    b4, rg4, atr = D["b4"], D["rg4"], D["atr"]
    out, peak, st = [1.0] * len(b4), None, 1.0
    for i in range(len(b4)):
        px = b4[i]["close"]
        if BT.BASE.get(rg4[i], 0.0) <= 0:
            peak, st = None, 1.0
            continue
        if peak is None:
            peak, st = px, 1.0
        peak = max(peak, px)
        a = atr[i] or px * 0.02
        if px < peak - k * a:
            st = 0.5
        elif st < 1.0 and px > peak - 0.5 * k * a:
            st = 1.0
        out[i] = st
    return out


TRAIL = {c: trail_atr(DATA[c], 1.5) for c in COINS}


def nav_1h(c, extra, delay_h, use_gate=True):
    """D 小时延迟的执行曲线。

    两个索引必须分清（第一版就是在这里错的）：
      · 日线状态 rg4 → 用**当前** 4H 桶 k4。regime 是「严格早于该日期的日线」，
        本来就是因果的，不能跟着延迟一起移。
      · 闸门 gate → 只能取「已经发生过的决策」。gate(j) 在 bucket j 收盘时才成立，
        推迟 D 小时意味着 tc >= ts(j) + D 才可用。
    第一版把 BASE 也跟着 ke 走，等于把状态一起晚一格，结果失真。
    """
    D = DATA[c]
    k4s, offs, cs, ds = D["k4"], D["off"], D["cs"], D["ds"]
    rg4, sig, gate, coef = D["rg4"], D["sig"], D["gate"], D["coef"]
    n4 = len(rg4)
    base_state = [BT.BASE.get(rg4[k], 0.0) for k in range(n4)]
    thr_ms = delay_h * HOUR_MS
    qv = PC.CAP_4H_WORST.get(c) or 1e12
    w = max(W.get(c, 0.09), 1e-6)
    nav, prev, out = 1.0, None, OrderedDict()
    started = False
    for i in range(len(k4s)):
        # 净值从 START_NAV 当天起算 —— 第一版漏了这一步，把 2018~2020 也算进去，
        # 控制组直接变成 432 万（正确是 310 万量级），所有比较都失去意义。
        if not started:
            if ds[i] < START_NAV:
                continue
            started = True
            prev = None
        k4 = k4s[i]
        # 闸门索引：D=0 时最后一小时就能用当根决策；D>0 时退到上一根
        kg = k4 if (use_gate and offs[i] >= thr_ms) else (k4 - 1 if k4 > 0 else 0)
        base = base_state[k4] * (gate[kg] if use_gate else 1.0) * extra[k4]
        pv = (base + BT.ADD * sig[k4]) * coef[k4] * VT.KAPPA
        if i:
            r = cs[i] / cs[i - 1] - 1
            x = 1 + pv * r
            nav *= x if x > 0 else 0.0
            sl = pv - base
            if F4 and sl:
                nav *= (1 - F4 / 4.0 * sl)
            if prev is not None and pv != prev:
                d = abs(pv - prev)
                nav *= (1 - BT.COST * d)
                nav *= (1 - 100 / 1e4 * math.sqrt(nav * CAP * w * d / qv) * d)
        prev = pv
        out[ds[i]] = nav
    return out


def curve(NAVS):
    FIRST = {c: min(NAVS[c]) for c in COINS}
    last, units, cq, cash, out = {}, {}, None, 0.0, []
    ALLD = sorted(set().union(*[set(NAVS[c]) for c in COINS]))
    for d in ALLD:
        live = [c for c in COINS if FIRST[c] <= d]
        if not live:
            continue
        sw = sum(W[c] for c in live)
        uw = {c: W[c] / sw * TGT for c in live}
        for c in live:
            if d in NAVS[c]:
                last[c] = NAVS[c][d]
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
    return out


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
    return yr, cum, dd


ONE = {c: [1.0] * len(DATA[c]["b4"]) for c in COINS}
NOG_NAVS = {c: nav_1h(c, ONE[c], 0, use_gate=False) for c in COINS}
_, CUM_NO, DD_NO = stat(curve(NOG_NAVS))
print(f"\n对照（完全不开闸门，同 1H 口径）：{CUM_NO / 1e4:.0f} 万，回撤 {DD_NO * 100:.1f}%")

for nm, extra, ds, ug in (("只 MA50 闸门", ONE, (0, 1, 2, 3, 4), True),
                          ("闸门 + ATR 吊灯 k=1.5", TRAIL, (0, 1, 2), True)):
    print("\n" + "=" * 98)
    print(f"{nm}：延迟 D 小时后执行")
    print("=" * 98)
    print(pad("延迟", 9) + pad("累计", 10) + pad("超额(对无规则)", 16)
          + pad("保留率", 9) + pad("回撤", 9))
    print("-" * 98)
    ex0 = None
    for dh in ds:
        NAVS = {c: nav_1h(c, extra[c], dh, use_gate=ug) for c in COINS}
        yr, cum, dd = stat(curve(NAVS))
        ex = cum - CUM_NO
        if ex0 is None:
            ex0 = ex
        keep = (ex / ex0 * 100) if abs(ex0) > 1 else 0
        print(pad(f"{dh} 小时", 9) + pad("%.0f 万" % (cum / 1e4), 10)
              + pad("%+.0f 万" % (ex / 1e4), 16) + pad("%.0f%%" % keep, 9)
              + pad("%.1f%%" % (dd * 100), 9))
    print(f"  （无规则基准 {CUM_NO / 1e4:.0f} 万 / 回撤 {DD_NO * 100:.1f}%）")
