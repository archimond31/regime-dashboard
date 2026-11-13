#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
visualize_btc_regime.py —— BTC 2024 年以来行情区间可视化（含背离信号方案）

方案（MA120 均已在策略起点 2024-01-01 初始化）：
  A  基线 v0.3：方向绑定 RSI 25/75
  B  方案 a：方向绑定 RSI 30/70
  E  方案 a + 背离：30/70 基础上，增加独立背离信号（最高优先级）

背离信号（超买超卖区间 70/30，RSI 破区间之后）：
  底背离：价格创新低、但 RSI 没有创新低 → 无论当前状态，直接切 UP
  顶背离：价格创新高、但 RSI 没有创新高 → 无论当前状态，直接切 DOWN
实现：追踪每次 RSI 进入超卖(<30)/超买(>70)再离开的「摆动」，比较相邻两次摆动的
     价格极值与 RSI 极值。仅用 Python 标准库。
"""
import os
import time
import json
import socket
import bisect
import urllib.request
from datetime import datetime, timezone, timedelta

# 代理：默认「自动探测」，不再硬编码端口。
# 起因是 2026-11 代理客户端把 HTTP 端口从 10809 换成了 7890，
# 硬编码导致 git 和日报/看板取数全部静默失败 —— 端口本来是用户可改的配置，不该钉死在代码里。
#   REGIME_PROXY=none / direct / off  → 强制直连（GitHub Actions 云端必须这样）
#   REGIME_PROXY=http://ip:port       → 强制指定
#   不设 或 =auto                      → 按候选端口探测，第一个通的就是它
_PROXY_CANDIDATES = ("http://127.0.0.1:7890",    # Clash / mihomo 系默认
                     "http://127.0.0.1:10809",   # v2rayN 系默认
                     "http://127.0.0.1:7897",    # Clash Verge
                     "http://127.0.0.1:10808",
                     "http://127.0.0.1:1080")    # 通用 SOCKS/HTTP


def detect_proxy(cands=_PROXY_CANDIDATES, timeout=0.4):
    """按顺序探测本地代理端口，返回第一个能建立 TCP 连接的 URL；都不通返回 None。

    只做 TCP 连通的握手，不发 HTTP 请求，所以即使探到的是 SOCKS 端口也不会误判成可用。
    探测失败不抛异常 —— 交给调用方走直连（国内源可直连，海外源会给出明确报错）。
    """
    for url in cands:
        try:
            host, port = url.split("//", 1)[1].split(":")
            with socket.create_connection((host, int(port)), timeout=timeout):
                return url
        except (OSError, ValueError):
            continue
    return None


_p = os.environ.get("REGIME_PROXY", "auto").strip()
if _p.lower() in ("", "none", "direct", "off"):
    PROXY = None
elif _p.lower() == "auto":
    PROXY = detect_proxy()
else:
    PROXY = _p
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
START = "2024-01-01"


def _build_opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


def http_get(url, retries=3):
    opener = _build_opener()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    last = None
    for i in range(retries):
        try:
            with opener.open(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = repr(e)
        time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET failed: {url} -> {last}")


def fetch_binance_daily(symbol, start_dt):
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows, cursor = [], start_ms
    while cursor < end_ms:
        # data-api.binance.vision = 币安官方公开数据镜像，免地域封锁（api.binance.com 对美国 IP 返回 451）
        url = (f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval=1d"
               f"&startTime={cursor}&limit=1000")
        data = json.loads(http_get(url))
        if not data:
            break
        for k in data:
            if k[0] >= end_ms:
                break
            rows.append({"date": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                         "close": float(k[4])})
        if len(data) < 1000:
            break
        cursor = data[-1][0] + 1
        time.sleep(0.3)
    seen = {}
    for r in rows:
        seen.setdefault(r["date"], r)
    return [seen[d] for d in sorted(seen)]


def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    g = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    l = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    if len(g) < n:
        return out
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + l[i]) / n
        out[i + 1] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def divergence_signals(closes, rsi, os=30, ob=70):
    """返回 (bull, bear, bull_level, bear_level)。
    bull/bear：底/顶背离触发日（bool 列表）。
    bull_level/bear_level：底/顶背离所在摆动的价格极值（用于「背离失败」检测）——
      顶背离失败 = 价格后续突破该摆动最高价（说明动能减弱但趋势未反转）；
      底背离失败 = 价格后续跌破该摆动最低价。"""
    n = len(closes)
    bull, bear = [False] * n, [False] * n
    bull_level, bear_level = [None] * n, [None] * n
    # 底背离追踪（超卖摆动）
    prev_plow = prev_rlow = None
    in_os = False
    cur_plow = cur_rlow = None
    # 顶背离追踪（超买摆动）
    prev_phigh = prev_rhigh = None
    in_ob = False
    cur_phigh = cur_rhigh = None

    for i in range(1, n):
        if rsi[i] is None or rsi[i - 1] is None:
            continue
        # 超卖摆动
        if rsi[i] < os:
            if not in_os:
                in_os, cur_plow, cur_rlow = True, closes[i], rsi[i]
            else:
                cur_plow = min(cur_plow, closes[i])
                cur_rlow = min(cur_rlow, rsi[i])
        else:
            if in_os:
                in_os = False
                if prev_plow is not None and cur_plow < prev_plow and cur_rlow > prev_rlow:
                    bull[i] = True
                    bull_level[i] = cur_plow
                prev_plow, prev_rlow = cur_plow, cur_rlow
                cur_plow = cur_rlow = None
        # 超买摆动
        if rsi[i] > ob:
            if not in_ob:
                in_ob, cur_phigh, cur_rhigh = True, closes[i], rsi[i]
            else:
                cur_phigh = max(cur_phigh, closes[i])
                cur_rhigh = max(cur_rhigh, rsi[i])
        else:
            if in_ob:
                in_ob = False
                if prev_phigh is not None and cur_phigh > prev_phigh and cur_rhigh < prev_rhigh:
                    bear[i] = True
                    bear_level[i] = cur_phigh
                prev_phigh, prev_rhigh = cur_phigh, cur_rhigh
                cur_phigh = cur_rhigh = None
    return bull, bear, bull_level, bear_level


def daily_states(closes, start_idx, ob=75, os=25, use_div=False, confirm=1, cross_fix=False,
                 range_retrigger=True, div_fail=True, div_expiry_days=None, div_expiry_ret=0.0,
                 div_fail_buf=None, trend_flat=False, trend_flat_ext=0.03, trend_flat_ret=0.03,
                 trend_flat_days=10, ma_slow=120, ma_fast=60):
    """confirm=1 单日事件触发；confirm>=2 需 RSI 连续 N 日站上 ob（或下 os）才切换。
    趋势→震荡：RSI 连续 confirm 日站上 ob（确认进入超买）后，再跌破 ob（离开超买）才切 RANGE；
      DOWN 对称（确认进入超卖后，再升破 os 才切 RANGE）。强趋势中 RSI 持续超买不离场则不切，
      避免「RSI 进入超买即切震荡」在单边暴涨/暴跌时过早转震荡、并与 RANGE 再触发自激振荡。
    cross_fix=True 时，趋势态加「反向穿越 MA60 连续 confirm 日站稳」纠错：
      UP 价格连续 confirm 日跌破 MA60 → DOWN；DOWN 价格连续 confirm 日站上 MA60 → UP。
      仅对「由 RANGE 经 MA60 穿越进入趋势」的状态持续纠错（range_cross 血统），
      不覆盖背离信号与 MA120 初始态。
    range_retrigger=True 时，RANGE 态加「RSI 再触发」退出：震荡中 RSI 连续 confirm 日
      重新站上 ob（或跌破 os）→ 切 UP（DOWN），解决价格在 MA60 单侧暴涨/暴跌、永不穿越
      导致的 RANGE 锁死。
    div_fail=True 时，背离态加「背离失败」检测：顶背离（DOWN）后价格连续 confirm 日突破
      背离摆动最高价、或底背离（UP）后连续 confirm 日跌破摆动最低价 → 背离失败，纠错回 RANGE。
      解决 RSI 背离在长期单边趋势品种（黄金/ETH）上的假信号 + 粘性锁死问题。
    div_expiry_days / div_expiry_ret：背离「过期」检测（可选，None=关闭）。背离触发后，
      若连续 div_expiry_days 个交易日内价格极值**从未**朝背离方向走出 div_expiry_ret 幅度——
      顶背离（DOWN）触发后最低价从未跌破 X%、底背离（UP）触发后最高价从未涨破 X%——
      则判定背离从未兑现，过期回 RANGE。      用「极值」而非「第 N 日收盘」判定，避免真背离
      触发后先兑现再反弹被误判为未走出。解决背离触发后价格长期横盘（既未兑现也未失败）
      导致的「假趋势」粘性。
    div_fail_buf=None 时，背离失败阈值用「摆动极值价」（顶背离=摆动最高价、底背离=摆动
      最低价，默认行为）。设数值（如 0.01）时，阈值收紧为「触发日收盘 ± 缓冲」——
      顶背离用 触发价*(1+buf)、底背离用 触发价*(1-buf)——让背离「兑现后反转」更快被
      纠错回 RANGE，解决摆动极值价过高导致纠错滞后（黄金横盘假 DOWN 粘 61 天）的问题。
    trend_flat=True 时，趋势态（UP/DOWN）加「盘整识别」退出：进入趋势态后，价格沿方向
      走出 trend_flat_ext 幅度（兑现）后，若从方向极值回撤超过 trend_flat_ret 且连续
      trend_flat_days 日维持，则判定趋势走完进入盘整，切 RANGE。解决趋势兑现后底部/顶部
      长期窄幅盘整仍粘在趋势态的问题（黄金 61 天假 DOWN、69 天假 UP 的根因）。
    ma_slow / ma_fast：慢线（定方向初始化 + 背离态的参照）与快线（震荡中穿越触发）长度。
      日线默认 120/60；周线按「÷5 机械映射」传 24/12（120 交易日≈24 周、60 交易日≈12 周），
      物理含义不变，只是采样频率降低。自由度为 0，不允许为周线单独调参（避免过拟合）。"""
    n = len(closes)
    st = [None] * n
    triggers = [None] * n
    if start_idx >= n or start_idx < ma_slow:
        return st, None, triggers
    rsi = rsi_wilder(closes)
    # 变量名沿用 ma120/ma60 以免大范围改动，语义即「慢线 / 快线」
    ma120, ma60 = sma(closes, ma_slow), sma(closes, ma_fast)
    if ma120[start_idx] is None:
        return st, None, triggers
    bull = bear = None
    if use_div:
        bull, bear, bull_level, bear_level = divergence_signals(closes, rsi, os, ob)
    state = "UP" if closes[start_idx] >= ma120[start_idx] else "DOWN"
    # entry_reason: init(粘性) / divergence(可失败) / range_cross(可纠错)
    entry_reason = "init"
    st[start_idx] = state
    triggers[start_idx] = "init_ma120"
    above = below = cross_up = cross_down = rng_ob = rng_os = 0
    ob_confirmed = os_confirmed = False
    fail_level = None
    fail_cnt = 0
    div_entry_idx = None    # 背离触发日（用于过期计时）
    div_entry_close = None  # 背离触发日收盘价（用于过期幅度判定）
    div_extreme = None      # 触发后价格极值：顶背离记最低、底背离记最高（判定是否兑现过）
    t_entry = None          # 趋势态进入日收盘价（盘整识别）
    t_extreme = None        # 趋势态方向极值（UP 记最高、DOWN 记最低）
    t_fulfilled = False     # 是否已沿方向走出 trend_flat_ext（兑现）
    t_pullback = 0          # 从极值回撤的持续天数

    def reset():
        nonlocal above, below, cross_up, cross_down, rng_ob, rng_os
        nonlocal ob_confirmed, os_confirmed
        above = below = cross_up = cross_down = rng_ob = rng_os = 0
        ob_confirmed = os_confirmed = False

    def enter_trend(i):
        nonlocal t_entry, t_extreme, t_fulfilled, t_pullback
        t_entry = closes[i]
        t_extreme = closes[i]
        t_fulfilled = False
        t_pullback = 0

    if trend_flat:
        enter_trend(start_idx)

    for i in range(start_idx + 1, n):
        if use_div and bear[i]:
            state = "DOWN"; entry_reason = "divergence"; reset()
            triggers[i] = "divergence_top"
            fail_level = (closes[i] * (1 + div_fail_buf) if div_fail_buf is not None
                          else bear_level[i])
            fail_cnt = 0
            div_entry_idx = i; div_entry_close = closes[i]; div_extreme = closes[i]
            if trend_flat:
                enter_trend(i)
        elif use_div and bull[i]:
            state = "UP"; entry_reason = "divergence"; reset()
            triggers[i] = "divergence_bottom"
            fail_level = (closes[i] * (1 - div_fail_buf) if div_fail_buf is not None
                          else bull_level[i])
            fail_cnt = 0
            div_entry_idx = i; div_entry_close = closes[i]; div_extreme = closes[i]
            if trend_flat:
                enter_trend(i)
        elif rsi[i] is None or ma60[i] is None:
            pass
        elif state == "UP":
            if trend_flat:
                t_extreme = max(t_extreme, closes[i])
                if not t_fulfilled and closes[i] >= t_entry * (1 + trend_flat_ext):
                    t_fulfilled = True
            # 底背离失败：价格跌破背离摆动最低价
            if div_fail and entry_reason == "divergence":
                if fail_level is not None and closes[i] < fail_level:
                    fail_cnt += 1
                    if fail_cnt >= confirm:
                        state = "RANGE"; entry_reason = None; reset()
                        triggers[i] = "div_fail_bottom"
                        fail_level = None; fail_cnt = 0
                        div_entry_idx = None; div_entry_close = None; div_extreme = None
                        st[i] = state
                        continue
                else:
                    fail_cnt = 0
            # 底背离过期：N 日内最高价从未涨够 div_expiry_ret（背离从未兑现），失效回震荡
            if div_expiry_days and entry_reason == "divergence" and div_entry_idx is not None:
                div_extreme = max(div_extreme, closes[i])
                if (i - div_entry_idx >= div_expiry_days
                        and div_extreme <= div_entry_close * (1 + div_expiry_ret)):
                    state = "RANGE"; entry_reason = None; reset()
                    triggers[i] = "div_expiry_bottom"
                    fail_level = None; fail_cnt = 0
                    div_entry_idx = None; div_entry_close = None; div_extreme = None
                    st[i] = state
                    continue
            if rsi[i] >= ob:
                above += 1
                if above >= confirm:
                    ob_confirmed = True
            else:
                above = 0
            if cross_fix and entry_reason == "range_cross":
                cross_down = cross_down + 1 if closes[i] < ma60[i] else 0
            if ob_confirmed and rsi[i] < ob:
                state = "RANGE"; entry_reason = None; reset()
                triggers[i] = "rsi_overbought_exit"
            elif cross_fix and entry_reason == "range_cross" and cross_down >= confirm:
                state = "DOWN"; entry_reason = "range_cross"; reset()
                triggers[i] = "cross_fix_down"
                if trend_flat:
                    enter_trend(i)
            elif trend_flat and t_fulfilled:
                if closes[i] < t_extreme * (1 - trend_flat_ret):
                    t_pullback += 1
                    if t_pullback >= trend_flat_days:
                        state = "RANGE"; entry_reason = None; reset()
                        triggers[i] = "trend_flat_up"
                else:
                    t_pullback = 0
        elif state == "DOWN":
            if trend_flat:
                t_extreme = min(t_extreme, closes[i])
                if not t_fulfilled and closes[i] <= t_entry * (1 - trend_flat_ext):
                    t_fulfilled = True
            # 顶背离失败：价格突破背离摆动最高价
            if div_fail and entry_reason == "divergence":
                if fail_level is not None and closes[i] > fail_level:
                    fail_cnt += 1
                    if fail_cnt >= confirm:
                        state = "RANGE"; entry_reason = None; reset()
                        triggers[i] = "div_fail_top"
                        fail_level = None; fail_cnt = 0
                        div_entry_idx = None; div_entry_close = None; div_extreme = None
                        st[i] = state
                        continue
                else:
                    fail_cnt = 0
            # 顶背离过期：N 日内最低价从未跌破 div_expiry_ret（背离从未兑现），失效回震荡
            if div_expiry_days and entry_reason == "divergence" and div_entry_idx is not None:
                div_extreme = min(div_extreme, closes[i])
                if (i - div_entry_idx >= div_expiry_days
                        and div_extreme >= div_entry_close * (1 - div_expiry_ret)):
                    state = "RANGE"; entry_reason = None; reset()
                    triggers[i] = "div_expiry_top"
                    fail_level = None; fail_cnt = 0
                    div_entry_idx = None; div_entry_close = None; div_extreme = None
                    st[i] = state
                    continue
            if rsi[i] <= os:
                below += 1
                if below >= confirm:
                    os_confirmed = True
            else:
                below = 0
            if cross_fix and entry_reason == "range_cross":
                cross_up = cross_up + 1 if closes[i] > ma60[i] else 0
            if os_confirmed and rsi[i] > os:
                state = "RANGE"; entry_reason = None; reset()
                triggers[i] = "rsi_oversold_exit"
            elif cross_fix and entry_reason == "range_cross" and cross_up >= confirm:
                state = "UP"; entry_reason = "range_cross"; reset()
                triggers[i] = "cross_fix_up"
                if trend_flat:
                    enter_trend(i)
            elif trend_flat and t_fulfilled:
                if closes[i] > t_extreme * (1 + trend_flat_ret):
                    t_pullback += 1
                    if t_pullback >= trend_flat_days:
                        state = "RANGE"; entry_reason = None; reset()
                        triggers[i] = "trend_flat_down"
                else:
                    t_pullback = 0
        else:  # RANGE
            if range_retrigger:
                rng_ob = rng_ob + 1 if rsi[i] >= ob else 0
                rng_os = rng_os + 1 if rsi[i] <= os else 0
            if closes[i - 1] <= ma60[i - 1] and closes[i] > ma60[i]:
                state = "UP"; entry_reason = "range_cross"; reset()
                triggers[i] = "ma60_cross_up"
                if trend_flat:
                    enter_trend(i)
            elif closes[i - 1] >= ma60[i - 1] and closes[i] < ma60[i]:
                state = "DOWN"; entry_reason = "range_cross"; reset()
                triggers[i] = "ma60_cross_down"
                if trend_flat:
                    enter_trend(i)
            elif range_retrigger and rng_ob >= confirm:
                state = "UP"; entry_reason = "range_cross"; reset()
                triggers[i] = "rsi_retrigger_up"
                if trend_flat:
                    enter_trend(i)
            elif range_retrigger and rng_os >= confirm:
                state = "DOWN"; entry_reason = "range_cross"; reset()
                triggers[i] = "rsi_retrigger_down"
                if trend_flat:
                    enter_trend(i)
        st[i] = state
    return st, (bull, bear) if use_div else None, triggers


def build(dates, closes, states):
    pts = [(dates[i], closes[i], states[i]) for i in range(len(dates))
           if states[i] is not None and dates[i] >= START]
    segs = []
    for d, c, s in pts:
        if segs and segs[-1]["state"] == s:
            segs[-1]["end"] = d
            segs[-1]["end_close"] = c
            segs[-1]["days"] += 1
        else:
            segs.append({"state": s, "start": d, "end": d,
                         "start_close": c, "end_close": c, "days": 1})
    return pts, segs


def merge_short_segments(segs, min_days=3, keep_last=True):
    """把持续天数 < min_days 的过渡碎片合并到相邻段：
      左右同状态 → 三段并一；否则并入天数更多的邻居。用于消除擦边抖动/假穿越纠错
      产生的 1~2 天碎区间。返回新列表，不改原数据。

    keep_last=True（默认）：**最后一段永不合并**。
      它是「进行中」的当前状态，天数少不是因为它短命，而是因为今天才第 N 天。
      把它并进前一个状态，等于让色带末端宣称「当前处于上升」，而抬头/总览写的
      是状态机给的「下降」—— 同一套状态机、两个结论，看图的人只会以为数据错位。
      （2026-11-13 SOL 实例：DOWN 起始 11-11，到 11-12 才 2 天 < 3，被并进前面的
       UP，色带尾部显「上升 11-06→11-12、-9.3%」，与总览「下降」直接冲突。）
      碎片合并的目的是抹平**已完成的**历史抖动，不该替状态机改写现在。
    """
    segs = [dict(s) for s in segs]
    while len(segs) > 1:
        idx = None
        limit = len(segs) - 1 if keep_last else len(segs)
        for i, s in enumerate(segs[:limit]):
            if s["days"] < min_days:
                idx = i
                break
        if idx is None:
            return segs
        s = segs[idx]
        if idx == 0:
            nxt = segs[1]
            nxt["start"] = s["start"]
            nxt["start_close"] = s["start_close"]
            nxt["days"] += s["days"]
            segs.pop(0)
        elif idx == len(segs) - 1:
            prv = segs[idx - 1]
            prv["end"] = s["end"]
            prv["end_close"] = s["end_close"]
            prv["days"] += s["days"]
            segs.pop()
        else:
            prv, nxt = segs[idx - 1], segs[idx + 1]
            if prv["state"] == nxt["state"]:
                prv["end"] = nxt["end"]
                prv["end_close"] = nxt["end_close"]
                prv["days"] += s["days"] + nxt["days"]
                segs.pop(idx)
                segs.pop(idx)
            elif prv["days"] >= nxt["days"]:
                prv["end"] = s["end"]
                prv["end_close"] = s["end_close"]
                prv["days"] += s["days"]
                segs.pop(idx)
            else:
                nxt["start"] = s["start"]
                nxt["start_close"] = s["start_close"]
                nxt["days"] += s["days"]
                segs.pop(idx)
    return segs


STATE_CN = {"UP": "上升", "RANGE": "震荡", "DOWN": "下降"}
STATE_COLOR = {"UP": "#d23c3c", "RANGE": "#b8860b", "DOWN": "#1a7f37"}
STATE_HEX = {"UP": "#e8b4b4", "RANGE": "#e8d9a8", "DOWN": "#b4d8b4"}


def _px(v):
    """价格自适应小数位。

    原来一律 `{:,.0f}`，于是 CRV(0.24)、UNI(5.9) 这一类在区间表和纵轴刻度上
    全被打印成「0」—— 表格里「起始价 0 / 结束价 0 / +0.1%」看着像数据坏了。
    """
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 1:
        return f"{v:,.2f}"
    return f"{v:,.4f}"


def iso_week_key(date_str):
    """'2026-11-09' → (2026, 45)，ISO 周（周一起算）分组键。跨市场统一。"""
    y, w, _ = datetime.strptime(date_str, "%Y-%m-%d").isocalendar()
    return (y, w)


def to_weekly(rows, min_bars=3, drop_current=True, today=None):
    """日线 [{date, close}] → 周线 bar [{date, close, n_bars}]（ISO 周聚合，取每周最后收盘）。

    G 方案的判定与背离都基于收盘价摆动，所以周线同样只需收盘序列，不需要 OHLC。
    规则：
      - 按 ISO 周（周一起算）分组，加密 7×24 与 ETF 周一~周五通用
      - 每周取该周最后一个交易日的收盘为周收盘，date 用该真实交易日
      - 交易日数 < min_bars 的残缺周丢弃（ETF 感恩节/圣诞等长假周，周收盘代表性弱）
      - drop_current=True 丢弃「尚未走完的当前周」：否则周线状态会在周一~周五之间逐日
        抖动，慢变量语义失效——这是多周期共振最容易踩的坑。
    """
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur_key = iso_week_key(today)
    buckets, order = {}, []
    for r in rows:
        k = iso_week_key(r["date"])
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(r)
    out = []
    for k in order:
        bars = buckets[k]
        if drop_current and k == cur_key:
            continue
        if len(bars) < min_bars:
            continue
        last = bars[-1]
        out.append({"date": last["date"], "close": last["close"], "n_bars": len(bars)})
    return out


def gen_svg(pts, segs, div_marks=None):
    W, LEFT, RIGHT, TOP, BOT = 1180, 74, 26, 44, 44
    plot_w = W - LEFT - RIGHT
    plot_h = 360
    H = TOP + plot_h + BOT
    d0 = datetime.strptime(pts[0][0], "%Y-%m-%d")
    d1 = datetime.strptime(pts[-1][0], "%Y-%m-%d")
    total_days = max((d1 - d0).days, 1)
    prices = [p[1] for p in pts]
    pmin, pmax = min(prices), max(prices)
    pad = (pmax - pmin) * 0.06
    pmin, pmax = pmin - pad, pmax + pad

    def x(s):
        return LEFT + (datetime.strptime(s, "%Y-%m-%d") - d0).days / total_days * plot_w

    def y(p):
        return TOP + (1 - (p - pmin) / (pmax - pmin)) * plot_h

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
         f'font-family="Microsoft YaHei,Arial,sans-serif">']
    for s in segs:
        x0, x1 = x(s["start"]), x(s["end"])
        p.append(f'<rect x="{x0:.1f}" y="{TOP}" width="{max(x1 - x0, 1):.1f}" height="{plot_h}" '
                 f'fill="{STATE_HEX[s["state"]]}" opacity="0.55"/>')
    for i in range(5):
        yy = TOP + plot_h * i / 4
        val = pmax - (pmax - pmin) * i / 4
        p.append(f'<line x1="{LEFT}" y1="{yy:.1f}" x2="{W - RIGHT}" y2="{yy:.1f}" stroke="#e0e0e0"/>')
        p.append(f'<text x="{LEFT - 8}" y="{yy + 4:.1f}" text-anchor="end" font-size="10" fill="#666">{_px(val)}</text>')
    cur = datetime(d0.year, d0.month, 1)
    while cur <= d1:
        if cur.month in (1, 4, 7, 10):
            xx = x(cur.strftime("%Y-%m-%d"))
            p.append(f'<text x="{xx:.1f}" y="{H - 18}" text-anchor="middle" font-size="10" fill="#666">{cur.strftime("%Y-%m")}</text>')
        cur = (cur.replace(day=28) + timedelta(days=5)).replace(day=1)
    poly = " ".join(f"{x(pt[0]):.1f},{y(pt[1]):.1f}" for pt in pts)
    p.append(f'<polyline points="{poly}" fill="none" stroke="#333" stroke-width="1.5"/>')
    # 背离标记
    if div_marks:
        for dm in div_marks:
            xx, yy = x(dm["date"]), y(dm["price"])
            color = "#1a7f37" if dm["kind"] == "bull" else "#d23c3c"
            shape = "▲" if dm["kind"] == "bull" else "▼"
            p.append(f'<text x="{xx:.1f}" y="{yy - 6}" text-anchor="middle" font-size="13" fill="{color}">{shape}</text>')
    for s in segs:
        xx = x(s["start"])
        p.append(f'<line x1="{xx:.1f}" y1="{TOP}" x2="{xx:.1f}" y2="{TOP + plot_h}" '
                 f'stroke="{STATE_COLOR[s["state"]]}" stroke-width="1" stroke-dasharray="3,3"/>')
        mid = x(s["start"]) + (x(s["end"]) - x(s["start"])) / 2
        p.append(f'<text x="{mid:.1f}" y="{TOP - 10}" text-anchor="middle" font-size="11" font-weight="bold" '
                 f'fill="{STATE_COLOR[s["state"]]}">{STATE_CN[s["state"]]}</text>')
    lx = LEFT + 10
    for k, lab in (("UP", "上升"), ("RANGE", "震荡"), ("DOWN", "下降")):
        p.append(f'<rect x="{lx}" y="{H - 12}" width="12" height="12" fill="{STATE_HEX[k]}" stroke="{STATE_COLOR[k]}"/>')
        p.append(f'<text x="{lx + 16}" y="{H - 1}" font-size="10" fill="#333">{lab}</text>')
        lx += 70
    if div_marks:
        p.append(f'<text x="{W - RIGHT - 20}" y="{H - 1}" text-anchor="end" font-size="10" fill="#333">▲底背离(切上升) ▼顶背离(切下降)</text>')
    p.append("</svg>")
    return "".join(p)


def seg_table(segs):
    rows = ""
    for i, s in enumerate(segs):
        chg = (s["end_close"] / s["start_close"] - 1) * 100
        # 最后一段是「进行中」的当前状态：它会随行情继续延长，也可能明天就翻转，
        # 和上面那些已经封口的历史段不是一回事，标出来免得被当成定论。
        live = (" <span style='font-weight:normal;color:#888;font-size:10px;'>进行中</span>"
                if i == len(segs) - 1 else "")
        rows += (f"<tr><td style='color:{STATE_COLOR[s['state']]};font-weight:bold;'>{STATE_CN[s['state']]}{live}</td>"
                 f"<td>{s['start']}</td><td>{s['end']}</td><td style='text-align:center;'>{s['days']}</td>"
                 f"<td style='text-align:right;'>{_px(s['start_close'])}</td>"
                 f"<td style='text-align:right;'>{_px(s['end_close'])}</td>"
                 f"<td style='text-align:right;color:{'#d23c3c' if chg >= 0 else '#1a7f37'};'>{chg:+.1f}%</td></tr>")
    return f"""<table border="0" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:11px;">
<tr style="background:#f0f0f0;text-align:left;"><th>状态</th><th>起始</th><th>结束</th><th>天数</th><th>起始价</th><th>结束价</th><th>涨跌</th></tr>{rows}</table>"""


def main():
    rows = fetch_binance_daily("BTCUSDT", datetime(2023, 6, 1, tzinfo=timezone.utc))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [r for r in rows if r["date"] < today]
    dates = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    s0 = bisect.bisect_left(dates, START)

    MIN_DAYS = 3  # 合并持续天数 < 3 的过渡碎片

    plans = [
        ("B", "方案 a：方向绑定 RSI 30/70", 70, 30, False, 1, False),
        ("F", "方案 a + 背离 + 两日确认", 70, 30, True, 2, False),
        ("G", "最终版：a + 背离 + 两日确认 + 反向穿越纠错(仅纠错 RANGE 穿越)", 70, 30, True, 2, True),
    ]
    results = []
    for key, label, ob, os_, use_div, confirm, cross_fix in plans:
        st, div, _trig = daily_states(closes, s0, ob, os_, use_div, confirm, cross_fix)
        pts, segs = build(dates, closes, st)
        raw_n = len(segs)
        segs = merge_short_segments(segs, MIN_DAYS)
        merged = raw_n - len(segs)
        marks = []
        if div:
            bull, bear = div
            for i in range(s0, len(dates)):
                if bull[i]:
                    marks.append({"date": dates[i], "price": closes[i], "kind": "bull"})
                if bear[i]:
                    marks.append({"date": dates[i], "price": closes[i], "kind": "bear"})
        disp_label = label + (f"｜合并 <{MIN_DAYS}天碎片 {merged} 个" if merged else "")
        results.append((key, disp_label, pts, segs, marks))
        print(f"\n===== {key} {label}：{raw_n} 个区间 → 过滤 <{MIN_DAYS}天后 {len(segs)} 个 =====")
        if div:
            bs = [dates[i] for i in range(s0, len(dates)) if div[0][i]]
            br = [dates[i] for i in range(s0, len(dates)) if div[1][i]]
            print(f"  底背离(切上升) {len(bs)} 次: {bs}")
            print(f"  顶背离(切下降) {len(br)} 次: {br}")
        for s in segs:
            chg = (s["end_close"] / s["start_close"] - 1) * 100
            print(f"  [{STATE_CN[s['state']]:3}] {s['start']} ~ {s['end']}  {s['days']:>3}天  "
                  f"{_px(s['start_close']):>10} -> {_px(s['end_close']):>10}  ({chg:+.1f}%)")

    blocks = []
    for key, label, pts, segs, marks in results:
        blocks.append(f"<h3 style='margin:14px 0 2px;'>{key} · {label}</h3>{gen_svg(pts, segs, marks)}{seg_table(segs)}")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BTC 2024以来行情区间 含背离</title></head>
<body style="font-family:'Microsoft YaHei',Arial,sans-serif;color:#222;margin:16px;">
<h2 style="margin:4px 0;">BTC 2024 年以来行情区间 · 方案 a 与背离信号对比</h2>
<p style="color:#888;margin-top:0;">数据源：Binance BTCUSDT 日线（截至 {rows[-1]['date']}）。红=上升 / 黄=震荡 / 绿=下降。MA120 均在策略起点 {START} 初始化。<br>规则：趋势→震荡 = RSI 连续 2 日站上极值(70/30)后回落离开极值；震荡→趋势 = MA60 穿越或 RSI 再触发；G 加反向穿越纠错 + 背离失败检测（价格突破背离摆动极值→纠错回震荡）。持续 &lt;{MIN_DAYS} 天的过渡碎片已合并。</p>
{''.join(blocks)}
<p style="font-size:12px;color:#999;">▲=底背离(切上升) ▼=顶背离(切下降)。状态为规则化分类，非投资建议。</p>
</body></html>"""

    with open("btc_regime_2024.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("\nWrote btc_regime_2024.html")


if __name__ == "__main__":
    main()
