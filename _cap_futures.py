#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合约 vs 现货的 4H 成交额对比 —— 容量上限到底受什么约束。

之前的容量表用的是**现货** quoteVolume（data-api.binance.vision）。
用户指出「合约也能做」，那永续的深度必须单独量：合约成交额通常显著大于现货，
如果翻倍，同样金额的冲击成本就减半，容量上限随之抬升。

口径：最近 540 根 4H（≈90 天）的 quoteVolume，取中位数与最小值。
注意：成交量不是「可以无冲击吃掉的量」，但作为现货/合约的相对比较与量级判断足够。
"""
import json
import os
import socket
import statistics
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_config as PC

socket.setdefaulttimeout(20)
PROXY = "http://127.0.0.1:7890"
FAPI = "https://fapi.binance.com/fapi/v1/klines"
SPOT = "https://data-api.binance.vision/api/v3/klines"
COINS = ["BTC", "ETH", "SOL", "BNB", "XRP", "LINK", "CRV", "UNI", "AAVE"]


def get(url, proxy=None):
    """直连走 urlopen；要代理才建 opener。混用 ProxyHandler({}) 会踩 urllib 的断言。"""
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        return op.open(url).read().decode()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=20).read().decode()


def qv(url, proxy=None):
    d = json.loads(get(url, proxy))
    return [float(k[7]) for k in d]


rows = []
for c in COINS:
    sym = c + "USDT"
    try:
        fut = qv(f"{FAPI}?symbol={sym}&interval=4h&limit=540", PROXY)
    except Exception as e:
        print(f"  {c} 合约失败 {type(e).__name__}")
        fut = []
    try:
        spo = qv(f"{SPOT}?symbol={sym}&interval=4h&limit=540")
    except Exception as e:
        print(f"  {c} 现货失败 {type(e).__name__}")
        spo = []
    if not fut or not spo:
        continue
    rows.append({
        "c": c,
        "fut_med": statistics.median(fut), "fut_min": min(fut),
        "spo_med": statistics.median(spo), "spo_min": min(spo),
    })

print("=" * 96)
print("4H 成交额（USD）· 最近 540 根 · 合约 vs 现货")
print("-" * 96)
print(f"{'币':<6}{'合约中位':>15}{'现货中位':>15}{'倍数':>7}"
      f"{'合约最差':>14}{'现货最差':>14}{'倍数':>7}")
print("-" * 96)
for r in sorted(rows, key=lambda x: x["fut_min"]):
    print(f"{r['c']:<6}{r['fut_med']:>15,.0f}{r['spo_med']:>15,.0f}"
          f"{r['fut_med'] / r['spo_med']:>6.1f}x{r['fut_min']:>14,.0f}"
          f"{r['spo_min']:>14,.0f}{r['fut_min'] / r['spo_min']:>6.1f}x")

# ---------- 容量反推 ----------
W = PC.WEIGHTS
print("\n" + "=" * 96)
print("容量上限（10 品种，峰值敞口 1.5×，单笔冲击不超过标注比例）")
print("-" * 96)
print(f"{'币':<6}{'权重':>7}{'合约1%':>12}{'合约2%':>12}{'现货1%':>12}{'现货2%':>12}{'提升':>8}")
print("-" * 96)
cap_rows = []
for r in sorted(rows, key=lambda x: x["fut_min"]):
    w = W[r["c"]]
    f1 = r["fut_min"] * 0.01 / (w * 1.5)
    f2 = r["fut_min"] * 0.02 / (w * 1.5)
    s1 = r["spo_min"] * 0.01 / (w * 1.5)
    s2 = r["spo_min"] * 0.02 / (w * 1.5)
    cap_rows.append({"c": r["c"], "w": w, "f1": f1, "f2": f2, "s1": s1, "s2": s2})
    print(f"{r['c']:<6}{w * 100:>6.0f}%{f1:>11,.0f} U{f2:>11,.0f} U"
          f"{s1:>11,.0f} U{s2:>11,.0f} U{f1 / s1:>7.1f}x")

tight_f = min(cap_rows, key=lambda x: x["f1"])
tight_s = min(cap_rows, key=lambda x: x["s1"])
print("-" * 96)
print(f"合约口径短板：{tight_f['c']} → 1% 冲击允许 {tight_f['f1']:,.0f} U、2% 允许 {tight_f['f2']:,.0f} U")
print(f"现货口径短板：{tight_s['c']} → 1% 冲击允许 {tight_s['s1']:,.0f} U、2% 允许 {tight_s['s2']:,.0f} U")
print(f"倍数：{tight_f['f1'] / tight_s['s1']:.1f}x")

json.dump({"rows": rows, "caps": cap_rows},
          open("_cap_fut.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\n已写出 _cap_fut.json")
