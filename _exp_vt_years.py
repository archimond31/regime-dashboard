#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_exp_vt_years.py —— 补充：等资金 vs 等风险 逐年拆解（含 2020-2023）

主实验 _exp_voltarget.py 只打了 2024/2025/2026，全样本 +2999% vs +1707% 的
差距到底落在哪一年没交代。这里把 2020H2 ~ 2026 全部年份摊开。

复用 _exp_voltarget 的构造（导入时它会打印五节，这里屏蔽掉）。
"""
import contextlib
import io
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

with contextlib.redirect_stdout(io.StringIO()):
    import _exp_voltarget as E

COINS, W, POS = E.COINS, E.W, E.POS
ARMS = ["等资金", "VT20周", "VT60周", "VT60月"]


def pad(s, n, right=True):
    w = sum(2 if ord(ch) > 127 else 1 for ch in str(s))
    sp = " " * max(n - w, 0)
    return sp + str(s) if right else str(s) + sp


def year_ret(arm, d0, d1):
    nv = []
    for c in COINS:
        b4, base, _p = E.RAW[c]
        nv.append(E.nav_daily(b4, base, POS[arm][c], E.idx_of(b4, d0), c,
                              E.idx_of(b4, d1) if d1 else None))
    return E.stat(E.fixed_share(nv, [W[c] for c in COINS]))[0]


YEARS = [("2020H2", "2020-09-01", "2021-01-01"),
         ("2021", "2021-01-01", "2022-01-01"),
         ("2022", "2022-01-01", "2023-01-01"),
         ("2023", "2023-01-01", "2024-01-01"),
         ("2024", "2024-01-01", "2025-01-01"),
         ("2025", "2025-01-01", "2026-01-01"),
         ("2026YTD", "2026-01-01", None)]

print("【A】逐年收益（50 万，平均敞口已对齐）")
print(pad("臂", 10) + "".join(pad(y, 11) for y, _a, _b in YEARS))
TAB = {}
for arm in ARMS:
    row = []
    for y, d0, d1 in YEARS:
        r = year_ret(arm, d0, d1)
        row.append(r)
        TAB[(arm, y)] = r
    print(pad(arm, 10) + "".join(pad(f"{r:+.1%}", 11) for r in row))

print("\n【B】每年差值（等风险 − 等资金，pp）")
print(pad("臂", 10) + "".join(pad(y, 11) for y, _a, _b in YEARS))
for arm in ARMS[1:]:
    print(pad(arm, 10) +
          "".join(pad(f"{(TAB[(arm, y)] - TAB[('等资金', y)]) * 100:+.1f}", 11)
                  for y, _a, _b in YEARS))

print("\n【C】2021 那一年各币收益贡献（只看 2021）")
print(pad("币", 6) + pad("等资金", 12) + pad("VT60周", 12) + pad("倍数差", 14))
for c in COINS:
    b4, base, _p = E.RAW[c]
    a = E.nav_daily(b4, base, POS["等资金"][c], E.idx_of(b4, "2021-01-01"), c,
                    E.idx_of(b4, "2022-01-01"))
    b = E.nav_daily(b4, base, POS["VT60周"][c], E.idx_of(b4, "2021-01-01"), c,
                    E.idx_of(b4, "2022-01-01"))
    if not a or not b:      # HYPE 2024-12 才上线，2021 没有数据
        print(pad(c, 6) + pad("—", 12) + pad("—", 12) + pad("未上线", 14))
        continue
    fa, fb = list(a.values())[-1], list(b.values())[-1]
    print(pad(c, 6) + pad(f"{fa:.1f}×", 12) + pad(f"{fb:.1f}×", 12) +
          pad(f"{fb - fa:+.1f}×", 14))

# ---------- D. 最大回撤发生在什么时候 ----------
print("\n【D】最大回撤区间（全样本 2020-09 起）")
print(pad("臂", 10) + pad("最大回撤", 12) + pad("峰值日", 13) + pad("谷底日", 13) +
      pad("修复日", 13))


def dd_window(arm):
    nv = []
    for c in COINS:
        b4, base, _p = E.RAW[c]
        nv.append(E.nav_daily(b4, base, POS[arm][c], E.idx_of(b4, "2020-09-01"), c))
    cur = E.fixed_share(nv, [W[c] for c in COINS])
    pk, pkd, best = -1e18, cur[0][0], (0.0, None, None)
    for d, v in cur:
        if v > pk:
            pk, pkd = v, d
        if v / pk - 1 < best[0]:
            best = (v / pk - 1, pkd, d)
    peak_v = next(v for d, v in cur if d == best[1])
    rec = next((d for d, v in cur if d > best[2] and v >= peak_v), None)
    return best[0], best[1], best[2], rec


for arm in ARMS:
    dd, p, t, rec = dd_window(arm)
    print(pad(arm, 10) + pad(f"{dd:.1%}", 12) + pad(p, 13) + pad(t, 13) +
          pad(rec or "未修复", 13))

# ---------- E. 剔除 2021 后的可比区间 ----------
print("\n【E】2023-01-01 起累计（剔除 2021 暴利年）")
print(pad("臂", 10) + pad("2023起累计", 13) + pad("年化", 10) + pad("区间回撤", 12))
for arm in ARMS:
    nv = []
    for c in COINS:
        b4, base, _p = E.RAW[c]
        nv.append(E.nav_daily(b4, base, POS[arm][c], E.idx_of(b4, "2023-01-01"), c))
    cur = E.fixed_share(nv, [W[c] for c in COINS])
    r, dd = E.stat(cur)
    yrs = 3.7
    print(pad(arm, 10) + pad(f"{(1 + r):.2f}×", 13) +
          pad(f"{(1 + r) ** (1 / yrs) - 1:+.1%}", 10) + pad(f"{dd:.1%}", 12))
