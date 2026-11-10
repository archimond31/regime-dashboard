#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance 公共归档数据下载器（data.binance.vision，静态 CDN，不走 API）。

为什么走 CDN 而不是 api.binance.com：
  本地/国内网络对 api.binance.com、OKX、Bybit 的公共 API 全部 502（代理拦隧道），
  但 data.binance.vision 是纯静态文件服务，可直连。它按月/按日发布官方 K 线归档，
  正好符合「把 4H 样本拉回 2019 年」的需求——Hyperliquid 只给 5000 根（≈2.3 年）。

文件格式：CSV，12 列
  open_time, open, high, low, close, volume, close_time, quote_volume,
  count, taker_buy_volume, taker_buy_quote_volume, ignore
  2025 年之后的部分文件带表头行，且 open_time 单位由毫秒改为微秒，需兼容。

用法：
  from fetch_binance_vision import load_4h
  bars = load_4h("BTC")          # 有缓存则直接读
  bars = load_4h("BTC", force=True)
"""
import io
import os
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://data.binance.vision/data/spot"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_binance")

# 面板加密组 → Binance 现货对（HYPE 只在 Hyperliquid，无 Binance 现货，单独排除）
SYMBOLS = {
    # ---- 原始 10 个：大盘 + DeFi
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "AAVE": "AAVEUSDT",
    "LINK": "LINKUSDT",
    "CRV": "CRVUSDT",
    "UNI": "UNIUSDT",
    "ADA": "ADAUSDT",
    # ---- 扩展 10 个：支付/老公链、互操作、新 L1、游戏，覆盖原始 10 个没有的板块
    "DOGE": "DOGEUSDT",     # meme
    "LTC": "LTCUSDT",       # 老支付
    "TRX": "TRXUSDT",       # 老公链
    "XLM": "XLMUSDT",       # 支付/跨境
    "ATOM": "ATOMUSDT",     # 互操作
    "ALGO": "ALGOUSDT",     # 老 L1
    "AVAX": "AVAXUSDT",     # 新 L1
    "DOT": "DOTUSDT",       # 互操作/平行链
    "NEAR": "NEARUSDT",     # 新 L1
    "SAND": "SANDUSDT",     # 游戏/元宇宙
}

# 各品种在 Binance 现货的上线月，用来缩短探测范围
# 故意写早一点：404 只浪费一次请求，写晚了会静默丢样本（这是唯一会造成假结论的错误）
LISTED_FROM = {
    "BTCUSDT": (2017, 8),
    "ETHUSDT": (2017, 8),
    "BNBUSDT": (2017, 9),
    "XRPUSDT": (2018, 5),
    "LINKUSDT": (2019, 1),
    "SOLUSDT": (2020, 8),
    "CRVUSDT": (2020, 8),
    "UNIUSDT": (2020, 9),
    "AAVEUSDT": (2020, 10),
    "ADAUSDT": (2018, 4),
    "DOGEUSDT": (2019, 5),
    "LTCUSDT": (2017, 8),
    "TRXUSDT": (2017, 8),
    "XLMUSDT": (2018, 1),
    "ATOMUSDT": (2019, 1),
    "ALGOUSDT": (2019, 4),
    "AVAXUSDT": (2020, 7),
    "DOTUSDT": (2020, 6),
    "NEARUSDT": (2020, 8),
    "SANDUSDT": (2020, 6),
}

_CACHE_TAIL = {}


def _months(start, end):
    y, m = start
    ey, em = end
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_csv(raw, t_from=None, t_to=None):
    """解析一个 kline CSV（zip 内或裸 csv），返回 [(ts_ms, o, h, l, c)]。"""
    rows = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", "ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 6:
            continue
        try:
            ts = int(parts[0])
        except ValueError:
            continue          # 表头行 / 脏行
        if ts > 10 ** 15:     # 微秒 → 毫秒
            ts //= 1000
        if t_from is not None and ts < t_from:
            continue
        if t_to is not None and ts > t_to:
            continue
        try:
            rows.append((ts, float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4])))
        except ValueError:
            continue
    return rows


def _fetch_one(sym, kind, tag, t_from, t_to, interval="4h"):
    """kind: 'monthly' | 'daily' … 统一拼 URL 并缓存。interval 支持 1h/4h/1d。"""
    if kind.startswith("monthly"):
        url = f"{BASE}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{tag}.zip"
    else:
        url = f"{BASE}/daily/klines/{sym}/{interval}/{sym}-{interval}-{tag}.zip"
    fn = os.path.join(CACHE, f"{sym}-{interval}-{tag}.zip")
    if os.path.exists(fn) and os.path.getsize(fn) > 0:
        with open(fn, "rb") as f:
            return _parse_csv(f.read(), t_from, t_to)
    try:
        raw = _get(url)
    except Exception:         # noqa: BLE001  404 = 该月未上线/已下架，正常跳过
        return []
    if not raw.startswith(b"PK"):
        return []
    with open(fn, "wb") as f:
        f.write(raw)
    return _parse_csv(raw, t_from, t_to)


def load_bars(name, interval="4h", force=False, end=None):
    """返回 [{t, date, open, high, low, close}]，UTC 指定周期收盘序列。

    name 用短代码（BTC/ETH/...）。Binance 无现货的品种抛 KeyError。
    interval: "1h" / "4h" / "1d"
    """
    sym = SYMBOLS[name]
    os.makedirs(CACHE, exist_ok=True)
    ck = (name, interval)
    if not force and ck in _CACHE_TAIL:
        return _CACHE_TAIL[ck]

    now = datetime.now(timezone.utc)
    if end is None:
        end = (now.year, now.month)
    start = LISTED_FROM.get(sym, (2020, 1))
    t_from = int(datetime(start[0], start[1], 1, tzinfo=timezone.utc).timestamp() * 1000)

    # 1) 整月归档：到「上一个完整月」
    last_full = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    if end < last_full:
        last_full = end
    months = _months(start, last_full)
    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_one, sym, "monthly", f"{y:04d}-{m:02d}",
                          t_from, None, interval) for y, m in months]
        for f in futs:
            rows += f.result()

    # 2) 当月补齐：用按日归档（月归档要等月结才发布）
    tail_start = datetime(last_full[0], last_full[1], 1, tzinfo=timezone.utc) + timedelta(days=32)
    tail_start = tail_start.replace(day=1)
    days = []
    d = tail_start
    while d <= now:
        days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_one, sym, "daily", tag, None, None, interval)
                for tag in days]
        for f in futs:
            rows += f.result()

    rows.sort(key=lambda r: r[0])
    seen, bars = set(), []
    for ts, o, h, l, c in rows:
        if ts in seen:
            continue
        seen.add(ts)
        bars.append({"t": ts,
                     "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "open": o, "high": h, "low": l, "close": c})
    _CACHE_TAIL[ck] = bars
    return bars


def load_4h(name, force=False, end=None):
    """4H 兼容包装（历史脚本都在用这个名字）。"""
    return load_bars(name, "4h", force=force, end=end)


def to_daily(bars4h):
    """4H → 日线（取每日最后一根 4H 的收盘，等价于 UTC 日收盘）。"""
    out = []
    for b in bars4h:
        if out and out[-1]["date"] == b["date"]:
            out[-1] = {"t": b["t"], "date": b["date"], "open": out[-1]["open"],
                       "high": max(out[-1]["high"], b["high"]),
                       "low": min(out[-1]["low"], b["low"]), "close": b["close"]}
        else:
            out.append(dict(b))
    return out


if __name__ == "__main__":
    for k in SYMBOLS:
        try:
            b = load_4h(k)
            print(f"{k:<5} {len(b):>6} 根  {b[0]['date']} ~ {b[-1]['date']}  "
                  f"close {b[-1]['close']:.4f}")
        except Exception as e:  # noqa: BLE001
            print(f"{k:<5} 失败 {repr(e)[:70]}")
        time.sleep(0.2)
