#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HYPE 行情拉取（Hyperliquid 公开接口，无需 API key）。

为什么单开一个文件：币安没有 HYPE（2026-11 实测 spot / futures/um 的归档目录都是空的），
而 HYPE 的原生交易场所就是 Hyperliquid，那边的 K 线才是「真身」。

输出格式刻意对齐 fetch_binance_vision.load_bars：
    [{"t": 毫秒, "date": "YYYY-MM-DD HH:MM", "open": .., "high": .., "low": .., "close": ..}]
这样下游（agg4h / atr_wilder / 状态机）一行都不用改。

注意：HYPE 2024-11 才上线，到 2026-11 只有约两年、且**全部在牛市里**，
所以日线状态机（MA120/70/30）在它身上没有经过熊市检验。信号可以出，权重别按老币给。
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"
CACHE = ".cache_hl"
# HYPE 上线（Hyperliquid 发币）在 2024-11-29，往前多留一个月，404 不丢样本
START = (2024, 11, 1)
CHUNK_DAYS = 200          # 单次请求跨度，太大接口会截断


def _post(body, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(
                API, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def _fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def load_hype(interval="4h", force=False):
    """返回 HYPE 的 K 线列表，按时间升序。带磁盘缓存，增量补齐。"""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"HYPE_{interval}.json")
    t0 = int(datetime(*START, 1, tzinfo=timezone.utc).timestamp() * 1000)
    now = int(time.time() * 1000)
    bars = {}
    if os.path.exists(path) and not force:
        try:
            bars = {b["t"]: b for b in json.load(open(path, encoding="utf-8"))}
        except Exception:
            bars = {}
    # 从「已有数据的最后一个整点」或起点开始补
    cur = (max(bars) + 1) if bars else t0
    if cur > now - 3600 * 1000:
        return [bars[k] for k in sorted(bars)]
    while cur < now:
        end = min(cur + CHUNK_DAYS * 86400 * 1000, now)
        r = _post({"type": "candleSnapshot",
                   "req": {"coin": "HYPE", "interval": interval,
                           "startTime": cur, "endTime": end}})
        if not r:
            break
        for x in r:
            bars[x["t"]] = {"t": x["t"], "date": _fmt(x["t"]),
                            "open": float(x["o"]), "high": float(x["h"]),
                            "low": float(x["l"]), "close": float(x["c"]),
                            "volume": float(x["v"])}
        cur = max(x["t"] for x in r) + 1
        if cur < end and len(r) < 2:
            break
    out = [bars[k] for k in sorted(bars)]
    json.dump(out, open(path, "w", encoding="utf-8"), separators=(",", ":"))
    return out


if __name__ == "__main__":
    for iv in ("4h", "1d"):
        b = load_hype(iv)
        print(f"HYPE {iv}: {len(b)} 根  {b[0]['date']} → {b[-1]['date']}  收盘 {b[-1]['close']}")
