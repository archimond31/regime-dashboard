#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_vt_rebal.py —— 修正口径：定期再平衡下的等资金 vs 等风险

**为什么必须补这一版**
上一版（_exp_voltarget.py）的 fixed_share 是「各 sleeve 各自复利、权重永久漂移」，
即买入后不再平衡。10 个 sleeve 里 SOL 一档长到 233 倍、CRV 一档长到 0.0 倍，
到 2024 年组合早已不是「每币 10%」，而是「SOL 说了算」。
后果：全样本 +2998% 与后期逐年收益互相矛盾（2024 年 +64.5%，但净值路径
2023-12-31 → 2024-12-31 是 −17.3%），回撤时点也被漂移污染。
真实 20~50 万 U 的账不可能让一个币占八成，必须定期拉回目标权重。

本脚本：4 臂 × 2 口径（不再平衡 / 季度再平衡），
  1) 全样本与逐年（季度再平衡为主口径）
  2) 最大回撤区间（峰值/谷底/修复日）
  3) 再平衡口径下的 2021 拆解 —— 验证「差距是否仍全部来自 SOL」
  4) 滚动 365 天窗口胜负（再平衡口径）
  5) 再平衡换手成本敏感性（0 / 5 / 10 bp 单边）

再平衡成本：每次调仓按「换手金额 × 费率」扣，费率默认 7bp（taker 5bp + 滑点）。
"""
import contextlib
import io
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, POS, RAW = E.COINS, E.W, E.POS, E.RAW
ARMS = ["等资金", "VT20周", "VT60周", "VT60月"]
REBAL_COST = 7e-4          # 单边 7bp


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


_NAV_CACHE = {}


def sleeve_navs(arm):
    """每币 sleeve 净值序列（起点各自的数据起点，统一从 2020-09-01 裁）。"""
    key = arm
    if key in _NAV_CACHE:
        return _NAV_CACHE[key]
    out = {}
    for c in COINS:
        b4, base, _p = RAW[c]
        out[c] = E.nav_daily(b4, base, POS[arm][c], E.idx_of(b4, "2020-09-01"), c)
    _NAV_CACHE[key] = out
    return out


def portfolio(arm, rebal=None, cost=REBAL_COST, d0=None, d1=None):
    """组合净值。rebal=None → 不再平衡；rebal='Q' → 每季首日拉回目标权重。

    窗口起点强制把权重拉回目标（units = w / nav0），否则起始权重会继承前面
    累积的漂移，逐年数字就没法互相比较了。
    返回 [(date, nav)]。
    """
    nv = sleeve_navs(arm)
    dates = sorted(set().union(*[set(n) for n in nv.values()]))
    if d0:
        dates = [d for d in dates if d >= d0]
    if d1:
        dates = [d for d in dates if d < d1]
    if not dates:
        return []
    start = dates[0]
    last, units = {}, {}
    for c in COINS:
        ks = sorted(nv[c])
        prev = [d for d in ks if d <= start]
        last[c] = nv[c][prev[-1]] if prev else 1.0
        units[c] = W[c] / last[c]          # 起点强制等权
    out, cur_q = [], None
    for d in dates:
        for c in COINS:
            if d in nv[c]:
                last[c] = nv[c][d]
        v = sum(units[c] * last[c] for c in COINS)
        if rebal:
            q = (date(*map(int, d.split("-"))).year,
                 (date(*map(int, d.split("-"))).month - 1) // 3)
            if cur_q is not None and q != cur_q:
                # 换手 = Σ |目标市值 - 当前市值| / 2 / V
                gross = sum(abs(v * W[c] - units[c] * last[c]) for c in COINS) / v / 2
                v *= (1 - gross * cost)
                for c in COINS:
                    units[c] = v * W[c] / last[c]
            cur_q = q
        out.append((d, v))
    return out


def stat(cur):
    if not cur:
        return 0.0, 0.0
    vals = [x[1] for x in cur]
    v0 = vals[0]
    pk, dd = v0, 0.0
    for v in vals:
        pk = max(pk, v)
        dd = min(dd, v / pk - 1)
    return vals[-1] / v0 - 1, dd


def dd_window(cur):
    pk, pkd, best = -1e18, cur[0][0], (0.0, None, None)
    for d, v in cur:
        if v > pk:
            pk, pkd = v, d
        if v / pk - 1 < best[0]:
            best = (v / pk - 1, pkd, d)
    peak_v = next(v for d, v in cur if d == best[1])
    rec = next((d for d, v in cur if d > best[2] and v >= peak_v), None)
    return best[0], best[1], best[2], rec


YEARS = [("2021", "2021-01-01", "2022-01-01"),
         ("2022", "2022-01-01", "2023-01-01"),
         ("2023", "2023-01-01", "2024-01-01"),
         ("2024", "2024-01-01", "2025-01-01"),
         ("2025", "2025-01-01", "2026-01-01"),
         ("2026YTD", "2026-01-01", None)]

# ---------- 1 ----------
print("【1】季度再平衡口径（主口径）：全样本与逐年")
print(pad("臂", 10) + pad("全样本", 12) + "".join(pad(y, 10) for y, _a, _b in YEARS) +
      pad("最大回撤", 10) + pad("Calmar", 9))
FULL = {}
for arm in ARMS:
    cur = portfolio(arm, rebal="Q")
    r, dd = stat(cur)
    FULL[arm] = (r, dd, cur)
    line = pad(arm, 10) + pad(f"{1 + r:.1f}×", 12)
    for y, d0, d1 in YEARS:
        rr, _ = stat(portfolio(arm, rebal="Q", d0=d0, d1=d1))
        line += pad(f"{rr:+.1%}", 10)
    line += pad(f"{dd:.1%}", 10) + pad(f"{r / abs(dd):.1f}", 9)
    print(line)

print("\n  （对照）不再平衡口径：全样本")
for arm in ARMS:
    r, dd = stat(portfolio(arm, rebal=None))
    print("  " + pad(arm, 10) + pad(f"{1 + r:.1f}×", 12) + pad(f"{dd:.1%}", 10))

# ---------- 2 ----------
print("\n【2】最大回撤区间（季度再平衡口径）")
print(pad("臂", 10) + pad("最大回撤", 12) + pad("峰值日", 13) + pad("谷底日", 13) +
      pad("修复日", 13))
for arm in ARMS:
    dd, p, t, rec = dd_window(FULL[arm][2])
    print(pad(arm, 10) + pad(f"{dd:.1%}", 12) + pad(p, 13) + pad(t, 13) +
          pad(rec or "未修复", 13))

# ---------- 3 ----------
print("\n【3】2021 年逐币 sleeve（季度再平衡口径，验证差距来源）")
print(pad("币", 6) + pad("等资金", 12) + pad("VT60周", 12) + pad("差", 12))
for c in COINS:
    row = []
    for arm in ("等资金", "VT60周"):
        nv = sleeve_navs(arm)[c]
        seg = {d: v for d, v in nv.items() if "2021-01-01" <= d < "2022-01-01"}
        row.append(list(seg.values())[-1] / list(seg.values())[0] if seg else None)
    if row[0] is None or row[1] is None:
        print(pad(c, 6) + pad("—", 12) + pad("—", 12) + pad("未上线", 12))
        continue
    print(pad(c, 6) + pad(f"{row[0]:.1f}×", 12) + pad(f"{row[1]:.1f}×", 12) +
          pad(f"{row[1] - row[0]:+.1f}×", 12))

# ---------- 4 ----------
print("\n【4】滚动 365 天窗口（季度再平衡口径，每 90 天起一窗）")
print(pad("臂", 10) + pad("胜", 6) + pad("负", 6) + pad("中位差", 10) + pad("均值差", 10))
for arm in ARMS[1:]:
    diffs, t = [], date(2021, 1, 1)
    while True:
        t1 = t.replace(year=t.year + 1)
        if t1 > date(2026, 6, 1):
            break
        a = stat(portfolio("等资金", rebal="Q",
                           d0=t.isoformat(), d1=t1.isoformat()))[0]
        b = stat(portfolio(arm, rebal="Q",
                           d0=t.isoformat(), d1=t1.isoformat()))[0]
        diffs.append((b - a) * 100)
        t = t.replace(year=t.year + 1)
    w = sum(1 for x in diffs if x > 0.2)
    n = sum(1 for x in diffs if x < -0.2)
    ds = sorted(diffs)
    print(pad(arm, 10) + pad(w, 6) + pad(n, 6) + pad(f"{ds[len(ds) // 2]:+.1f}pp", 10) +
          pad(f"{sum(diffs) / len(diffs):+.1f}pp", 10))

# ---------- 5 ----------
print("\n【5】再平衡成本敏感性（VT60周，全样本）")
print(pad("单边费率", 12) + pad("期末倍数", 12) + pad("最大回撤", 12) + pad("年化", 10))
for bp in (0, 5, 10, 20):
    cur = portfolio("VT60周", rebal="Q", cost=bp / 1e4)
    r, dd = stat(cur)
    yrs = 6.2
    print(pad(f"{bp}bp", 12) + pad(f"{1 + r:.1f}×", 12) + pad(f"{dd:.1%}", 12) +
          pad(f"{(1 + r) ** (1 / yrs) - 1:+.1%}", 10))
