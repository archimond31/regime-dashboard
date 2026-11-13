#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance 行情数据：主路径是静态归档（data.binance.vision），尾段用 REST 补齐。

两条腿为什么都要：
  ① **历史**只能走归档。data.binance.vision 是纯静态文件服务，国内可直连，
     按月/按日发布官方 K 线归档，正好符合「把 4H 样本拉回 2019 年」的需求
     —— Hyperliquid 只给 5000 根（≈2.3 年）。
     （api.binance.com / OKX / Bybit 的公共 API 在国内是 502，代理拦隧道）
  ② **最近几根**必须走 REST。归档按日发布，当日那个 zip 要到**次日约 09:44
     （北京）**才出现，所以一天里归档末端几乎不动。只靠归档的话，一个 4H 策略
     每根 4H 都醒、却每次都拿到同一根 K 线，实际退化成「每天看一次」，
     开仓/止损时点最多晚 24 小时。
     data-api.binance.vision 与静态归档**同源、同一交易所、同一口径**，
     且国内可直连（实测 9 个品种各 0.8s 内到手），只补「归档末端 → 现在」这一小段。

文件格式：CSV，12 列
  open_time, open, high, low, close, volume, close_time, quote_volume,
  count, taker_buy_volume, taker_buy_quote_volume, ignore
  2025 年之后的部分文件带表头行，且 open_time 单位由毫秒改为微秒，需兼容。

用法：
  from fetch_binance_vision import load_4h
  bars = load_4h("BTC")          # 有缓存则直接读
  bars = load_4h("BTC", force=True)
  bars = load_4h("BTC", tail=False)   # 只要归档，不补 REST 尾段（复现旧结果用）
"""
import io
import json
import os
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://data.binance.vision/data/spot"
REST = "https://data-api.binance.vision/api/v3/klines"
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache_binance")

INTERVAL_MS = {"1h": 3600 * 1000, "4h": 4 * 3600 * 1000, "1d": 86400 * 1000}

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


def _fetch_rest(sym, interval, start_ms, now_ms, timeout=15):
    """补 start_ms 之后、到「最后一根已收盘」为止的 K 线，返回 [(ts,o,h,l,c)]。

    只收**已收盘**的 bar（open_time + 周期 <= 现在）。未收盘那根的 close 还在
    跳，拿它判信号就是未来函数 —— 这是这条腿最容易埋进去的错。

    任何异常都吞掉并返回已拿到的部分：REST 挂了就退回纯归档，宁可数据旧，
    不能让整轮信号跑不出来。
    """
    step = INTERVAL_MS[interval]
    out = []
    cur = start_ms
    for _ in range(12):                    # 12 × 1000 根 = 够补 41 天 1h
        url = (f"{REST}?symbol={sym}&interval={interval}"
               f"&startTime={cur}&limit=1000")
        try:
            data = json.loads(_get(url, timeout=timeout))
        except Exception:                   # noqa: BLE001
            break
        if not data:
            break
        for k in data:
            ts = int(k[0])
            if ts + step > now_ms:
                continue                    # 还没收盘
            out.append((ts, float(k[1]), float(k[2]), float(k[3]), float(k[4])))
        if len(data) < 1000:
            break
        cur = int(data[-1][0]) + step
    return out


def load_bars(name, interval="4h", force=False, end=None, tail=True):
    """返回 [{t, date, open, high, low, close}]，UTC 指定周期收盘序列。

    name 用短代码（BTC/ETH/...）。Binance 无现货的品种抛 KeyError。
    interval: "1h" / "4h" / "1d"
    tail: 是否用 REST 补「归档末端 → 现在」（默认补；复现旧回测结果时置 False）
    """
    sym = SYMBOLS[name]
    os.makedirs(CACHE, exist_ok=True)
    ck = (name, interval, tail, end)   # tail/end 必须进 key，否则同一进程里先补过的会被复用
    if not force and ck in _CACHE_TAIL:
        return _CACHE_TAIL[ck]

    now = datetime.now(timezone.utc)
    auto_end = end is None          # 必须在给 end 填默认值之前记下来，下面判补尾要用
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

    # 3) 归档末端 → 现在：补上还没进归档的那几根（理由见模块 docstring 的 ②）
    #    end 有值说明调用方在切历史窗口（回测/复现），这时不该掺实时数据。
    if tail and auto_end and bars:
        step = INTERVAL_MS.get(interval)
        now_ms = now.timestamp() * 1000
        if step and bars[-1]["t"] + step <= now_ms:
            add = _fetch_rest(sym, interval, bars[-1]["t"] + step, now_ms)
            if add:
                have = {b["t"] for b in bars}
                for ts, o, h, l, c in add:
                    if ts in have:          # startTime 是闭区间，首根可能重叠
                        continue
                    have.add(ts)
                    bars.append({"t": ts,
                                 "date": datetime.fromtimestamp(
                                     ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                                 "open": o, "high": h, "low": l, "close": c})
                bars.sort(key=lambda b: b["t"])

    _CACHE_TAIL[ck] = bars
    return bars


def load_4h(name, force=False, end=None, tail=True):
    """4H 兼容包装（历史脚本都在用这个名字）。"""
    return load_bars(name, "4h", force=force, end=end, tail=tail)


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
