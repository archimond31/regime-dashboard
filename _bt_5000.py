#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""5000 U · 分层权重配置 · 2026 年回放。

配置逻辑（来自容量/相关性/敞口三组检验）：
  1. 基准等权 10%，但 CRV 受 4H 流动性硬约束 → 降到 6%
  2. HYPE 样本只有 ~1 年、且日线状态机在它身上失效（价格 +262% 却判 DOWN 830 根）
     → 降到 5%
  3. 省下的 7% 匀给 BTC / ETH / SOL 三个流动性最深的品种（各 13%）
  4. UNI / AAVE / BNB / XRP / LINK 保持 10%

口径：固定份额（每个币拿到的钱独立复利，不每日归位）—— 这才是「把 5000 分成 10 份」的真实形态。
每日等权再平衡作对照。

底仓 1.0×（现货真金）+ 加仓层 0.5×（合约/杠杆）→ 总名义峰值 1.5×
"""
import json
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bt_skeleton as BT
import test_1h_v2 as V2
from _proto_4h import atr_wilder
from fetch_binance_vision import to_daily
from fetch_hyperliquid import load_hype
from strategy_1h_rsi import align_regime_safe, regime_series
from test_regime_fit import agg4h

BIN9 = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]
COINS = BIN9 + ["HYPE"]
D0 = "2026-01-01"
CAP = 5000.0
F4 = BT.FUND_DAILY / 6.0

# ---------- 方案 ----------
def spread(fixed):
    """指定的币给固定权重，剩下的钱平分给其余品种（保证总和 = 1）。"""
    rest = [c for c in COINS if c not in fixed]
    r = (1.0 - sum(fixed.values())) / len(rest)
    return {c: fixed.get(c, r) for c in COINS}


PLANS = {
    "A 等权（现状）": {c: 0.10 for c in COINS},
    "B CRV6% + HYPE5%": spread({"CRV": 0.06, "HYPE": 0.05}),
    "C 只降 HYPE 到 5%": spread({"HYPE": 0.05}),
    "D 只降 CRV 到 6%": spread({"CRV": 0.06}),
    "E 只降 CRV 到 7%": spread({"CRV": 0.07}),
    "F 最终（三主流11% / CRV7%）": {
        "BTC": 0.11, "ETH": 0.11, "SOL": 0.11,
        "BNB": 0.10, "XRP": 0.10, "LINK": 0.10,
        "UNI": 0.10, "AAVE": 0.10, "HYPE": 0.10, "CRV": 0.07,
    },
}
BEST = "F 最终（三主流11% / CRV7%）"

for _k in list(V2.SYMBOLS):
    if _k not in BIN9:
        del V2.SYMBOLS[_k]
V2.load_all()


def nav_daily(b4, base, pos, k0, f4=None):
    nav, out = 1.0, {}
    fd = F4 if f4 is None else f4
    for k in range(k0 + 1, len(b4)):
        r = b4[k]["close"] / b4[k - 1]["close"] - 1
        x = 1 + pos[k] * r
        nav *= x if x > 0 else 0.0
        sl = (pos[k] - base[k]) if BT.SPOT_BASE else pos[k]
        if fd and sl:
            nav *= (1 - fd * sl)
        if pos[k] != pos[k - 1]:
            nav *= (1 - BT.COST * abs(pos[k] - pos[k - 1]))
        out[b4[k]["date"]] = nav
    return out


def fixed_share(navs, weights):
    """固定份额：每个 sleeve 用初始分到的钱独立复利，权重随盈亏自然漂移。"""
    dates = sorted(set().union(*[set(n) for n in navs]))
    last = [1.0] * len(navs)
    out = []
    for d in dates:
        v = 0.0
        for i, n in enumerate(navs):
            if d in n:
                last[i] = n[d]
            v += weights[i] * last[i]
        out.append((d, v))
    return out


def rebalance(navs, weights):
    """每日等权再平衡，且**按各币目标权重**加权（不是纯等权）。

    固定份额版本里权重会随盈亏漂移（赚得多的越占越大）；
    这个版本每天把权重拉回目标值，用来量化「再平衡」值多少钱。
    """
    dates = sorted(set().union(*[set(n) for n in navs]))
    prev, v, out = {}, 1.0, []
    for d in dates:
        rs, wsum = [], 0.0
        for i, n in enumerate(navs):
            if d not in n:
                continue
            p0 = prev.get(i)
            if p0:
                rs.append((n[d] / p0, weights[i]))
                wsum += weights[i]
            prev[i] = n[d]
        if rs and wsum > 0:
            v *= sum(r * w for r, w in rs) / wsum
        out.append((d, v))
    return out


def stats(cur):
    vals = [x[1] for x in cur]
    pk, dd = vals[0], 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return {"final": vals[-1], "ret": vals[-1] - 1, "dd": dd,
            "cagr": vals[-1] ** (365.0 / max(len(vals), 1)) - 1,
            "n": len(vals)}


def monthly(cur):
    m, base = {}, []
    for d, v in cur:
        m.setdefault(d[:7], []).append(v)
    out = []
    keys = sorted(m)
    for i, k in enumerate(keys):
        vs = m[k]
        p0 = m[keys[i - 1]][-1] if i else 1.0
        out.append((k, vs[-1] / p0 - 1, vs[-1]))
    return out


# ---------- 载入 ----------
raw, trs = {}, {}
for c in BIN9:
    b, rg, _a, _b2 = V2.load_all()[c]
    b4 = agg4h(b)
    rg4 = [rg[x["src"]] for x in b4]
    atr = atr_wilder(b4, 14)
    sig, trades = BT.signals(b4, rg4, atr, vol_q=BT.VOL_Q, coin=c)
    base = [BT.BASE.get(r, 0.0) for r in rg4]
    raw[c] = (b4, base, [base[k] + BT.ADD * sig[k] for k in range(len(sig))])
    trs[c] = trades
    print(f"  {c:5s} {len(b4):>6} 根  {len(trades):>3} 笔", flush=True)

hb = load_hype("4h")
hb = [dict(x, date=x["date"][:10]) for x in hb]
hrg = align_regime_safe(hb, regime_series(to_daily(hb)))
hsig, htrades = BT.signals(hb, hrg, atr_wilder(hb, 14), vol_q=BT.VOL_Q, coin="HYPE")
hbase = [BT.BASE.get(r, 0.0) for r in hrg]
raw["HYPE"] = (hb, hbase, [hbase[k] + BT.ADD * hsig[k] for k in range(len(hsig))])
trs["HYPE"] = htrades
print(f"  {'HYPE':5s} {len(hb):>6} 根  {len(htrades):>3} 笔（Hyperliquid）", flush=True)

# ---------- 主结果 ----------
print("\n" + "=" * 82, flush=True)
print(f"{'方案':<28}{'期末':>11}{'收益':>10}{'回撤':>10}{'年化':>9}{'单位回撤':>10}", flush=True)
print("-" * 82, flush=True)

res = {}
for name, W in PLANS.items():
    ds, navs, ws = [], [], []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0 = next(k for k in range(len(b4c)) if b4c[k]["date"] >= D0)
        navs.append(nav_daily(b4c, basec, posc, k0))
        ws.append(W[c])
    cur = fixed_share(navs, ws)
    st = stats(cur)
    st["eq"] = stats(rebalance(navs, ws))
    st["curve"] = cur
    st["months"] = monthly(cur)
    st["w"] = W
    # 分币贡献
    contrib = {}
    for c, n, w in zip(COINS, navs, ws):
        lastd = max(n)
        contrib[c] = w * n[lastd]
    st["contrib"] = contrib
    res[name] = st
    tag = "B" if name.startswith("B") else ("A" if name.startswith("A") else name[0])
    print(f"{name:<28}{CAP * st['final']:>10,.0f} U{st['ret'] * 100:>9.1f}%"
          f"{st['dd'] * 100:>9.1f}%{st['cagr'] * 100:>8.1f}%"
          f"{st['ret'] / abs(st['dd']):>9.2f}", flush=True)

# ---------- 纯现货三档（无杠杆、无做空，最大占用不超过本金） ----------
# 上面所有方案都含空头（DOWN 时 pos 会变负）。现货做不到，必须单独算。
# 做法：把「底仓 + 多头加仓」整体缩到峰值 = 1.0，空头一律截成 0。
print(f"\n纯现货三档（{BEST}，不许做空、不许超买，峰值敞口 = 下方标注）")
print("-" * 86)
print(f"{'档位':<26}{'期末':>11}{'收益':>10}{'回撤':>10}{'年化':>9}{'单位回撤':>10}")
print("-" * 86)
spot_rows = []
for lab, lv in (("现货满仓（底仓1.0，无加仓）", 1.0),
                ("现货常规（底仓.67+加仓.33）", 1.0),
                ("现货保守（底仓.33+加仓.17）", 0.5)):
    navs = []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0 = next(k for k in range(len(b4c)) if b4c[k]["date"] >= D0)
        if lab.startswith("现货满仓"):
            p = list(basec)
            bs = basec
        else:
            # 峰值 = 1.5 缩放后必须 ≤ lv，因此缩放系数 = lv/1.5
            s = lv / 1.5
            bs = [x * s for x in basec]
            p = [s * (basec[k] + BT.ADD * max((posc[k] - basec[k]) / BT.ADD, 0.0))
                 for k in range(len(posc))]
        navs.append(nav_daily(b4c, bs, p, k0))
    st = stats(fixed_share(navs, [PLANS[BEST][c] for c in COINS]))
    cur = fixed_share(navs, [PLANS[BEST][c] for c in COINS])
    st["curve"] = cur
    st["months"] = monthly(cur)
    spot_rows.append({"lab": lab, "final": st["final"], "ret": st["ret"],
                      "dd": st["dd"], "cagr": st["cagr"],
                      "curve": cur, "months": st["months"]})
    print(f"{lab:<26}{CAP * st['final']:>10,.0f} U{st['ret'] * 100:>9.1f}%"
          f"{st['dd'] * 100:>9.1f}%{st['cagr'] * 100:>8.1f}%"
          f"{st['ret'] / abs(st['dd']):>9.2f}", flush=True)

spot_st, spot2 = spot_rows[0], spot_rows[1]

b = res[BEST]
print(f"\n{'=' * 82}")
print(f"{BEST} · 每日再平衡口径对照：{CAP * b['eq']['final']:,.0f} U"
      f"（{b['eq']['ret'] * 100:+.1f}%，回撤 {b['eq']['dd'] * 100:.1f}%）", flush=True)

# ---------- 分币贡献 ----------
print(f"\n分币贡献（{BEST}，起点各按权重分配，期末归还原金额）")
print("-" * 82)
print(f"{'币':<6}{'权重':>7}{'起点':>10}{'期末':>10}{'盈亏':>10}{'该币sleeve收益':>16}{'占总盈利':>10}")
contrib_rows = []
tot_gain = CAP * b["final"] - CAP
for c in sorted(COINS, key=lambda x: -b["contrib"][x]):
    start = CAP * b["w"][c]
    end = CAP * b["contrib"][c]
    gain = end - start
    r_ = end / start - 1
    share = gain / tot_gain if tot_gain else 0.0
    contrib_rows.append((c, b["w"][c], start, end, gain, r_, share))
    print(f"{c:<6}{b['w'][c] * 100:>6.0f}%{start:>10,.0f}{end:>10,.0f}{gain:>+10,.0f}"
          f"{r_ * 100:>15.1f}%{share * 100:>9.0f}%")

# ---------- 容量检查 ----------
LIQ_WORST = {"BTC": 23834165, "ETH": 6416149, "SOL": 2596253, "BNB": 1563138,
             "XRP": 1157296, "LINK": 123570, "CRV": 32654, "UNI": 486247,
             "AAVE": 275452, "HYPE": 6826117}
print(f"\n容量检查（5000 U 下各币单笔最大金额 vs 近 90 天最差 4H 成交额）")
print("-" * 82)
print(f"{'币':<6}{'权重':>7}{'底仓':>9}{'加仓后':>10}{'最差4H额':>12}{'冲击占比':>10}{'判定':>8}")
cap_rows = []
for c in sorted(COINS, key=lambda x: -CAP * b["w"][x] * 1.5 / LIQ_WORST[x]):
    base_amt = CAP * b["w"][c]
    peak = base_amt * 1.5
    worst = LIQ_WORST[c]
    imp = peak / worst * 100
    flag = "宽松" if imp < 0.5 else ("可接受" if imp < 1.0 else ("需拆单" if imp < 3 else "超标"))
    cap_rows.append((c, b["w"][c], base_amt, peak, worst, imp, flag))
    print(f"{c:<6}{b['w'][c] * 100:>6.0f}%{base_amt:>9,.0f}{peak:>10,.0f}{worst:>12,.0f}"
          f"{imp:>9.2f}%{flag:>8}")

# ---------- 月度 ----------
print(f"\n月度（{BEST}）")
print("-" * 82)
for k, r_, v in b["months"]:
    bar = "#" * max(0, int(abs(r_) * 300))
    print(f"{k}  {r_ * 100:>+7.1f}%  {CAP * v:>8,.0f} U  {bar}")

# ---------- CRV 权重连续性检验 ----------
# 单个方案好不说明问题，可能是噪声。看整条曲线是不是单调的：
# 如果「CRV 权重越低越好」一路成立，说明降权有结构原因；如果来回跳，就是过拟合。
print("\nCRV 权重扫描（其余 9 币等权分摊）—— 检验降权是不是过拟合")
print("-" * 82)
print(f"{'CRV 权重':<10}{'期末':>11}{'收益':>10}{'回撤':>10}{'单位回撤':>10}")
print("-" * 82)
crv_scan = []
for cw in (0.10, 0.08, 0.06, 0.04, 0.02, 0.0):
    W = spread({"CRV": cw}) if cw > 0 else spread({})
    if cw == 0:
        W = spread({c: 1.0 / 9 for c in COINS if c != "CRV"})
    navs, ws = [], []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0 = next(k for k in range(len(b4c)) if b4c[k]["date"] >= D0)
        navs.append(nav_daily(b4c, basec, posc, k0))
        ws.append(W[c])
    st = stats(fixed_share(navs, ws))
    crv_scan.append({"cw": cw, "final": st["final"], "ret": st["ret"], "dd": st["dd"]})
    print(f"{cw * 100:>6.0f}%{'':<4}{CAP * st['final']:>10,.0f} U{st['ret'] * 100:>9.1f}%"
          f"{st['dd'] * 100:>9.1f}%{st['ret'] / abs(st['dd']):>9.2f}")

# ---------- 起点敏感性 ----------
# 只在 2026 一年上择优，权重结论可能是那一年的产物。换个起点还成不成立？
# 同时扫三档敞口 —— 2026 的 -15% 回撤不代表跨周期也这么浅。
print(f"\n起点 × 敞口（{BEST}）：2026 的浅回撤是不是运气")
print("-" * 88)
print(f"{'起点':<12}{'1.5×':>13}{'回撤':>9}{'1.0×':>13}{'回撤':>9}{'0.5×':>13}{'回撤':>9}")
print("-" * 88)
start_rows = []
for d0 in ("2025-01-01", "2026-01-01", "2026-07-01"):
    row = {"d0": d0}
    for lab, s in (("1.5", 1.0), ("1.0", 2 / 3), ("0.5", 1 / 3)):
        navs, ws = [], []
        for c in COINS:
            b4c, basec, posc = raw[c]
            k0s = [k for k in range(len(b4c)) if b4c[k]["date"] >= d0]
            if not k0s:
                continue
            navs.append(nav_daily(b4c, [x * s for x in basec],
                                  [x * s for x in posc], k0s[0]))
            ws.append(PLANS[BEST][c])
        st = stats(fixed_share(navs, ws))
        row[lab] = st
    start_rows.append(row)
    def fmt(k):
        st = row[k]
        return f"{CAP * st['final']:>9,.0f} U{st['ret'] * 100:>+8.1f}%{st['dd'] * 100:>8.1f}%"
    print(f"{d0:<12}{fmt('1.5')}{fmt('1.0')}{fmt('0.5')}", flush=True)

# 现货常规档单独做跨起点（这是最终推荐档，必须有跨周期回撤数字）
print("\n现货常规（底仓.67 + 加仓.33，无空头，峰值 1.0×）· 跨起点")
print("-" * 78)
print(f"{'起点':<12}{'期末':>12}{'收益':>11}{'回撤':>10}{'天数':>8}")
print("-" * 78)
spot_x = []
for d0 in ("2025-01-01", "2026-01-01", "2026-07-01"):
    navs = []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0s = [k for k in range(len(b4c)) if b4c[k]["date"] >= d0]
        if not k0s:
            continue
        s = 1.0 / 1.5
        bs = [x * s for x in basec]
        p = [s * (basec[k] + BT.ADD * max((posc[k] - basec[k]) / BT.ADD, 0.0))
             for k in range(len(posc))]
        navs.append(nav_daily(b4c, bs, p, k0s[0]))
    st = stats(fixed_share(navs, [PLANS[BEST][c] for c in COINS]))
    spot_x.append({"d0": d0, "final": st["final"], "ret": st["ret"],
                   "dd": st["dd"], "n": st["n"]})
    print(f"{d0:<12}{CAP * st['final']:>11,.0f} U{st['ret'] * 100:>+10.1f}%"
          f"{st['dd'] * 100:>9.1f}%{st['n']:>7}", flush=True)

# ---------- 资金费敏感性（做空的人必须看这个）----------
# 回测口径是「空头按 3bp/日 收钱」（sl 为负 → 1-F4*sl > 1）。
# 这在正资金费环境成立；一旦市场转负（熊末常见），空头反过来付钱。
# 所以这里把资金费从 +3bp 一路扫到 -10bp，看空头层还剩多少。
print(f"\n资金费敏感性（{BEST}，1.5× 合约口径）")
print("-" * 82)
print(f"{'资金费/日':<14}{'期末':<14}{'收益':>11}{'回撤':>10}{'相对基准':>12}")
print("-" * 82)
fund_rows = []
base_ret = None
for fd_bp in (3, 1, 0, -1, -3, -10):
    navs, ws = [], []
    for c in COINS:
        b4c, basec, posc = raw[c]
        k0 = next(k for k in range(len(b4c)) if b4c[k]["date"] >= D0)
        navs.append(nav_daily(b4c, basec, posc, k0, f4=fd_bp / 1e4 / 6.0))
        ws.append(PLANS[BEST][c])
    st = stats(fixed_share(navs, ws))
    if base_ret is None:
        base_ret = st["ret"]
    fund_rows.append({"bp": fd_bp, "final": st["final"], "ret": st["ret"], "dd": st["dd"]})
    print(f"{fd_bp:>+4d} bp{'':<7}{CAP * st['final']:>10,.0f} U{st['ret'] * 100:>10.1f}%"
          f"{st['dd'] * 100:>9.1f}%{(st['ret'] - base_ret) * 100:>+10.1f}pp", flush=True)

json.dump({
    "cap": CAP, "coins": COINS, "plans": {k: {kk: vv for kk, vv in v.items() if kk != "curve"}
                                          for k, v in res.items()},
    "b_curve": b["curve"], "spot": spot_st, "spot_add": spot2,
    "contrib": contrib_rows, "capcheck": cap_rows, "crv_scan": crv_scan,
    "months": b["months"], "dates": [d for d, _ in b["curve"]], "best": BEST,
    "start_rows": start_rows, "spot_rows": spot_rows, "spot_x": spot_x,
    "fund_rows": fund_rows,
}, open("_bt_5000.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n已写出 _bt_5000.json", flush=True)