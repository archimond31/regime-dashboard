#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dashboard.py —— 市场趋势监控面板（自包含 HTML，含交互）

内容：
  1) 状态统计卡片：上升/震荡/下降数量 + 分组分布
  2) 今日状态切换提示（对比上一次部署的 monitor_state.json）
  3) 当前状态总览：全部品种分四组（加密/商品/宏观/A股），含 状态/收盘(带单位)/RSI14/MA60/MA120/触发日期/原因/持续
  4) 各品种 2024 年以来趋势区间图：状态色带 + 价格折线 + 背离标记 + 可折叠区间明细
  5) 交互：分组 tab + 状态筛选（原生 JS，无外部依赖）
  6) 容错：某品种取数失败时沿用上次成功数据（monitor_state.json），并标注「沿用旧数据」

数据源：Hyperliquid 永续（加密）/ Yahoo 期货·ETF（商品、宏观）。
代理开关：本地默认 127.0.0.1:10809；云端设 REGIME_PROXY=none 直连。
快照：构建时读 {Pages}/monitor_state.json（上次部署）做「今日切换」对比与失败降级，
      构建后写 monitor_state.json，由 workflow 一并部署到 gh-pages。
用法：python build_dashboard.py  →  生成 regime_dashboard.html + monitor_state.json
"""
import json
import os
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from visualize_btc_regime import (
    daily_states, build, merge_short_segments, gen_svg, seg_table, fetch_binance_daily,
    BLOOD_CN,
)
from visualize_eth_gold_regime import fetch_yahoo_daily
from daily_regime_alert import (
    ASSETS, GROUP_ORDER, fetch_hyperliquid_daily, fetch_ashare_daily,
    compute_regime, compute_regime_weekly,
    resonance_gate, GATE_CN, trig_cn,
    STATE_CN, STATE_COLOR, TRIGGER_CN, fmt_price, WARM_START, START,
    drop_unclosed, drop_unclosed_cn,
    US_CLOSE_HOUR_UTC, FUTURES_CLOSE_HOUR_UTC,
)

MIN_DAYS = 3
OUT = "regime_dashboard.html"
STATE_FILE = "monitor_state.json"


def _snapshot_url():
    """上次部署的 monitor_state.json 地址。云端用 GITHUB_REPOSITORY 推导，本地可用 PAGES_SNAPSHOT_URL 覆盖。"""
    explicit = os.environ.get("PAGES_SNAPSHOT_URL")
    if explicit:
        return explicit
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner}.github.io/{name}/{STATE_FILE}"
    return ""


def fetch_prev_state():
    """读取上次部署的状态快照；失败/首跑返回 {}（不阻断构建）。"""
    url = _snapshot_url()
    if not url:
        # 本地无 GITHUB_REPOSITORY：回退读同目录快照，便于本地验证「今日切换」
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                assets = data.get("assets", {})
                print(f"[快照] 本地快照：{len(assets)} 个品种（更新于 {data.get('updated', '?')}）")
                return assets
            except Exception as e:  # noqa: BLE001
                print(f"[快照] 本地快照解析失败：{e}")
        return {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "regime-dashboard"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        assets = data.get("assets", {})
        print(f"[快照] 已读取上次状态：{len(assets)} 个品种（更新于 {data.get('updated', '?')}）")
        return assets
    except Exception as e:  # noqa: BLE001
        print(f"[快照] 读取上次状态失败（首跑属正常）：{e}")
        return {}


def fetch_asset(a, warm_dt):
    if a["source"] == "binance":
        return fetch_binance_daily(a["symbol"], warm_dt)
    if a["source"] == "hyperliquid":
        return fetch_hyperliquid_daily(a["symbol"])
    if a["source"] == "ashare":
        return fetch_ashare_daily(a)
    return fetch_yahoo_daily(a["symbol"], warm_dt)


def close_hour_of(a):
    """该品种所在市场的收盘时刻（UTC 小时）。0 = 加密 UTC 日界。

    A 股不用这个（走 drop_unclosed_cn 的北京时间 15:00 逻辑），返回 None。
    商品用 22（CME Globex 日结算），美股 ETF 用 21（16:00 ET）。
    """
    if a["source"] == "ashare":
        return None
    if a["source"] in ("binance", "hyperliquid"):
        return 0
    return FUTURES_CLOSE_HOUR_UTC if a["symbol"].endswith("=F") else US_CLOSE_HOUR_UTC


def trim_unclosed(a, rows):
    """按各市场自己的收盘规则剔除未收盘 bar。

    不能用统一的 `date < today(UTC)`：构建落在 21:00Z 之后时，美股当天已收盘的
    bar 会被误删，页面于是出现「A 股截至 11-24、美股/商品截至 11-23」的错位。"""
    if a["source"] == "ashare":
        return drop_unclosed_cn(rows)
    return drop_unclosed(rows, close_hour_utc=close_hour_of(a) or 0)


def judge_series(rows):
    """状态判定用的价格序列：有复权价就用复权价。

    加密 / A 股 / 商品期货没有分红，adj 恒等于 close，这里等于原样返回。
    ETF（TLT/SPY/QQQ…）必须走 adj —— 未复权序列带分红造成的下行漂移，
    实测 TLT 有 25% 的交易日会被判成不同状态。展示仍用 close（真实市价）。"""
    return [r.get("adj") or r["close"] for r in rows]


def _blood_cell(b):
    """总览表里的「血统」格：当前状态是被哪类事件带进来的。

    这一列不是装饰 —— 它决定该状态有没有纠错通道：
    均线穿越进来的能被 MA60 送回去，背离进来的能被「背离失败」推翻，
    初始化进来的两条都不管（见 build_dashboard 页脚与 visualize_btc_regime._cf_ok）。"""
    if b == "init_ma120":
        return ("<td style='text-align:center;color:#b02a2a;font-weight:bold;' "
                "title='当前状态来自状态机预热后的初始均线判断：不受 MA60 纠错、不受背离失败约束，"
                "只有「RSI 破极值后再回升」一个出口；交易层此段不开新仓'>⚠初始化</td>")
    if b:
        return f"<td style='text-align:center;color:#666;'>{BLOOD_CN.get(b, b)}</td>"
    return "<td style='text-align:center;color:#bbb;'>—</td>"


def render_summary(summary):
    groups = {}
    for s in summary:
        groups.setdefault(s["group"], []).append(s)
    blocks = ""
    for g in GROUP_ORDER:
        if g not in groups:
            continue
        trs = ""
        for s in groups[g]:
            state = s["regime"]["state"]
            color = STATE_COLOR.get(state, "#888")
            unit = s.get("unit", "")
            stale = (" <span style='color:#c00;font-size:11px;'>⚠️沿用旧数据</span>"
                     if s.get("stale") else "")
            wstate = s.get("weekly_state")
            gate = s.get("gate", "na")
            if wstate:
                wcell = (f"<span style='color:{STATE_COLOR.get(wstate, '#888')};'>"
                         f"{STATE_CN.get(wstate, wstate)}</span>")
            else:
                wcell = "<span style='color:#bbb;'>—</span>"
            if gate == "block":
                wcell += (" <span style='color:#b8860b;font-weight:bold;font-size:11px;' "
                          f"title='{s.get('gate_note', '')}'>⚠反向</span>")
            trs += (
                f"<tr><td>{s['label']} <span style='color:#999;font-size:11px;'>{unit}</span>{stale}</td>"
                f"<td style='background:{color};color:#fff;font-weight:bold;text-align:center;'>"
                f"{STATE_CN.get(state, state)}</td>"
                f"<td style='text-align:center;'>{wcell}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('close'))}</td>"
                f"<td style='text-align:right;'>{s['regime'].get('rsi') or 0:.1f}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('ma60'))}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('ma120'))}</td>"
                f"<td style='white-space:nowrap;color:#666;'>{s['regime'].get('trigger_date', '—')}</td>"
                f"<td>{trig_cn(s['regime'].get('trigger', ''))}</td>"
                + _blood_cell(s.get("cur_blood"))
                + f"<td style='text-align:center;'>{s['regime'].get('since_days', '-')}</td></tr>")
        blocks += (
            f"<h3 class='group'>{g}（{len(groups[g])}）</h3>"
            f"<table class='sum'><tr><th>品种</th><th>日线</th><th>周线</th><th>收盘</th><th>RSI14</th>"
            f"<th>MA60</th><th>MA120</th><th>触发日期</th><th>最近触发</th><th>血统</th><th>持续(日)</th></tr>"
            f"{trs}</table>")
    return blocks


def render_stats(summary):
    """状态统计卡片：整体 + 分组分布。"""
    total = Counter()
    grp = {}
    for s in summary:
        st = s["regime"]["state"]
        total[st] += 1
        grp.setdefault(s["group"], Counter())[st] += 1
    n = len(summary)

    def cell(c):
        up = c.get("UP", 0); rng = c.get("RANGE", 0); dn = c.get("DOWN", 0)
        return (f"<span class='c-up'>▲{up}</span> "
                f"<span class='c-rng'>—{rng}</span> "
                f"<span class='c-dn'>▼{dn}</span>")

    grp_cells = ""
    for g in GROUP_ORDER:
        if g in grp:
            grp_cells += (f"<div class='sc'><div class='sc-name'>{g}</div>"
                          f"<div class='sc-val'>{cell(grp[g])}</div></div>")
    return (
        f"<div class='stats'>"
        f"<div class='sc main'><div class='sc-name'>全部品种（{n}）</div>"
        f"<div class='sc-val big'>{cell(total)}</div></div>"
        f"{grp_cells}</div>")


def render_tabs():
    groups = [("all", "全部")] + [(g, g) for g in GROUP_ORDER]
    states = [("all", "全部"), ("UP", "上升"), ("RANGE", "震荡"), ("DOWN", "下降")]
    gbtns = "".join(f"<button class='tab' data-g='{k}' onclick=\"setGroup('{k}')\">{v}</button>"
                    for k, v in groups)
    sbtns = "".join(f"<button class='tab' data-s='{k}' onclick=\"setState('{k}')\">{v}</button>"
                    for k, v in states)
    return (f"<div class='tabbar'>分组：{gbtns}</div>"
            f"<div class='tabbar'>状态：{sbtns}</div>")


def render_changes(changes, blocked=None):
    """今日状态切换提示条 + 今日「日线与周线反向」提示条（中性描述，不算信号）。"""
    blocked = blocked or []
    out = []
    if changes:
        items = "".join(
            f"<span class='chg'>{c['label']} <b>{STATE_CN.get(c['from'], c['from'])}→{STATE_CN.get(c['to'], c['to'])}</b></span>"
            for c in changes)
        out.append(f"<div class='changes'><b>今日状态切换（{len(changes)}）</b> {items}</div>")
    elif not blocked:
        out.append("<div class='changes none'>今日无状态切换，日周方向一致</div>")
    if blocked:
        items = "".join(
            f"<span class='chg block'>{b['label']} <b>日{STATE_CN.get(b['daily'], b['daily'])}"
            f"／周{STATE_CN.get(b['weekly'], b['weekly'])}</b></span>"
            for b in blocked)
        out.append("<div class='changes blocked'><b>日线与周线反向（%d）</b> "
                   "方向冲突，仅作状态描述、不构成交易建议 %s</div>" % (len(blocked), items))
    return "".join(out)


def main():
    warm_dt = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    prev_assets = fetch_prev_state()
    summary = []
    cards = []
    changes = []
    blocked = []
    new_state = {}

    for a in ASSETS:
        try:
            rows = trim_unclosed(a, fetch_asset(a, warm_dt))
            dates = [r["date"] for r in rows]
            closes = [r["close"] for r in rows]
            jd = judge_series(rows)          # 判定用（ETF 是复权价）；展示仍用 closes
            reg = compute_regime(closes, dates, judge=jd)
            wreg = compute_regime_weekly(rows)
            wstate = wreg.get("state")
            gate, gate_note = resonance_gate(reg["state"], wstate)
            if gate == "block":
                blocked.append({"label": a["label"], "daily": reg["state"], "weekly": wstate})
            # 今日状态切换对比（仅对有上次记录且状态变化的品种）
            prev = prev_assets.get(a["key"])
            if prev and prev.get("state") and prev["state"] != reg["state"]:
                changes.append({"label": a["label"], "from": prev["state"], "to": reg["state"]})

            # 判定起点 = 数据累计够 MA120 那一刻（=交易层 start_idx），**不是**展示窗口起点。
            # 曾经用 max(bisect(START),120)：2024-01-01 之前本就有完整前史的品种，
            # 会在窗口首日被凭空塞一个 init 状态 —— 于是 22 个品种全都长出一个
            # 「初始化血统」（WTI 挂了 319 天）。起点统一后，只有 HYPE 这种
            # 上线不满 120 根的品种才真正有 init 段。色带仍从 START 开始画。
            s0 = 120
            # 状态用判定序列（复权），价格图与背离标记仍用 closes（真实市价）。
            st, div, trig = daily_states(jd, s0, 70, 30, True, 2, True)
            # daily_states 的第三个返回值是「稀疏触发点」，展开成逐日血统：
            # 某个区间的状态是「被哪类事件带进来的」，决定它有没有纠错通道。
            blood = []
            _b = None
            for i in range(len(dates)):
                if i < len(trig) and trig[i]:
                    _b = trig[i]
                blood.append(_b)
            pts, segs = build(dates, closes, st, blood)
            raw_n = len(segs)
            segs = merge_short_segments(segs, MIN_DAYS)
            init_days = sum(s["days"] for s in segs if s.get("blood") == "init_ma120")
            # 每个品种的初始化段只有一段，且必然落在 s0（状态机只能从一根均线上开始猜）；
            # 22 张图全都挂红标签等于没标签，所以只挑真正扎眼的两类标：
            #   contra —— 判「下降」却在涨（HYPE 2025 那种），或判「上升」却在跌
            #   stuck  —— 一段初始猜测挂了 90 天以上，没有任何事件推翻过它
            init_flag = None
            init_seg_ = next((s_ for s_ in segs if s_.get("blood") == "init_ma120"), None)
            if init_seg_:
                _chg = init_seg_["end_close"] / init_seg_["start_close"] - 1
                if ((init_seg_["state"] == "DOWN" and _chg >= 0.10)
                        or (init_seg_["state"] == "UP" and _chg <= -0.10)):
                    init_flag = {"kind": "contra", "chg": _chg, "seg": init_seg_}
                elif init_seg_["days"] >= 90:
                    init_flag = {"kind": "stuck", "chg": _chg, "seg": init_seg_}
            marks = []
            if div:
                bull, bear = div
                for i in range(s0, len(dates)):
                    if bull[i]:
                        marks.append({"date": dates[i], "price": closes[i], "kind": "bull"})
                    if bear[i]:
                        marks.append({"date": dates[i], "price": closes[i], "kind": "bear"})
            state = reg["state"]
            color = STATE_COLOR.get(state, "#888")
            cards.append({
                "label": a["label"], "group": a["group"], "state": state, "color": color,
                "weekly_state": wstate, "gate": gate,
                "svg": gen_svg(pts, segs, marks), "table": seg_table(segs), "n_seg": len(segs),
                "raw_n": raw_n, "merged": raw_n - len(segs), "as_of": dates[-1], "stale": False,
                "init_days": init_days, "init_flag": init_flag,
                "now_init": bool(blood and blood[-1] == "init_ma120"),
            })
            summary.append({"key": a["key"], "label": a["label"], "group": a["group"],
                            "unit": a.get("unit", ""), "regime": reg, "stale": False,
                            "weekly_state": wstate, "gate": gate, "gate_note": gate_note,
                            "cur_blood": blood[-1] if blood else None})
            new_state[a["key"]] = dict(reg, weekly_state=wstate, gate=gate)
            print(f"[{a['key']:5}] 日{STATE_CN.get(state, state):3} "
                  f"周{STATE_CN.get(wstate, wstate or '—'):3} {GATE_CN.get(gate, gate):2} "
                  f"{len(segs)} 个区间（合并 {raw_n - len(segs)} 个碎片）  {dates[-1]}"
                  + (f"  ⚠初始化血统 {init_days} 天" if init_days else ""))
        except Exception as e:  # noqa: BLE001
            prev = prev_assets.get(a["key"])
            if prev:
                # 降级：沿用上次成功数据（不纳入今日切换）
                st_state = prev.get("state", "N/A")
                pw, pg = prev.get("weekly_state"), prev.get("gate", "na")
                summary.append({"key": a["key"], "label": a["label"], "group": a["group"],
                                "unit": a.get("unit", ""), "regime": prev, "stale": True,
                                "weekly_state": pw, "gate": pg, "gate_note": "沿用上次结论"})
                new_state[a["key"]] = prev
                cards.append({"label": a["label"], "group": a["group"], "state": st_state,
                              "color": STATE_COLOR.get(st_state, "#888"),
                              "weekly_state": pw, "gate": pg,
                              "svg": f"<p style='color:#c00;padding:8px;'>本次抓取失败，沿用 {prev.get('as_of', '')} 的数据</p>",
                              "table": "", "n_seg": 0, "raw_n": 0, "merged": 0,
                              "as_of": prev.get("as_of", "-"), "stale": True})
                print(f"[{a['key']:5}] 失败→沿用旧数据({prev.get('as_of')}): {e}")
            else:
                print(f"[{a['key']:5}] 失败(无历史): {e}")
                cards.append({"label": a["label"], "group": a["group"], "state": "N/A",
                              "color": "#888", "weekly_state": None, "gate": "na",
                              "svg": "<p style='color:#999'>取数/计算失败</p>",
                              "table": "", "n_seg": 0, "raw_n": 0, "merged": 0, "as_of": "-", "stale": False})
                summary.append({"key": a["key"], "label": a["label"], "group": a["group"],
                                "unit": a.get("unit", ""), "weekly_state": None, "gate": "na",
                                "regime": {"state": "N/A", "reason": str(e)}, "stale": False})

    # 跨资产比较基准日 = 所有品种最后交易日的**最小值**。
    # 各市场交易日历不同（加密 7×24、A 股有长假、美股有自己的假日），
    # 同一页出现两个截止日很正常；但读者横向比较时不看日期，就会把「差一天」
    # 当成「结构分化」。所以把公共基准日写在头上，而不是只写在每张卡片里。
    _asofs = [s["regime"].get("as_of") for s in summary if s["regime"].get("as_of")]
    as_of_common = min(_asofs) if _asofs else ""
    as_of_latest = max(_asofs) if _asofs else ""
    asof_note = ""
    if as_of_common and as_of_latest and as_of_common != as_of_latest:
        asof_note = (f"｜跨资产比较基准日 <b>{as_of_common}</b>"
                     f"（最新已到 {as_of_latest}，各市场交易日历不同）")

    card_groups = {}
    for c in cards:
        card_groups.setdefault(c["group"], []).append(c)
    card_blocks = ""
    for g in GROUP_ORDER:
        if g not in card_groups:
            continue
        card_blocks += f"<h2 class='sec'>{g}</h2>"
        for c in card_groups[g]:
            merged_note = (f" <span class='muted'>（合并 {c['merged']} 个 &lt;{MIN_DAYS}天碎片）</span>"
                           if c["merged"] else "")
            stale_badge = " <span style='color:#c00;font-size:12px;'>⚠️沿用旧数据</span>" if c.get("stale") else ""
            wst = c.get("weekly_state")
            wchip = ""
            if wst:
                wcol = STATE_COLOR.get(wst, "#888")
                wchip = (f"<span class='wk' style='color:{wcol};border-color:{wcol};'>"
                         f"周线 {STATE_CN.get(wst, wst)}</span>")
            if c.get("gate") == "block":
                wchip += "<span class='wk blk'>日周反向</span>"
            fl = c.get("init_flag")
            if fl:
                sg = fl["seg"]
                base = ("状态机预热后的初始均线猜测：既不受 MA60 纠错、也不受背离失败约束，"
                        "只有「RSI 破极值后再回升」一个出口 —— 上涨行情中该出口永不触发，会长期锁死。"
                        "交易层此段不开新仓（既有底仓照常持有、按翻转平）。")
                if fl["kind"] == "contra":
                    wchip += (f"<span class='wk bad' title=\"{base} 本例：{sg['start']}→{sg['end']} "
                              f"判「{STATE_CN.get(sg['state'], sg['state'])}」而价格 {fl['chg']:+.1%}，"
                              f"{sg['days']} 天。\">⚠ 初始化段反向 {sg['days']} 天</span>")
                else:
                    wchip += (f"<span class='wk bad' title=\"{base} 本例：{sg['start']}→{sg['end']} 挂了 "
                              f"{sg['days']} 天，期间价格 {fl['chg']:+.1%}。\">"
                              f"⚠ 初始化段 {sg['days']} 天未纠错</span>")
            card_blocks += (
                f"<div class='card' data-group='{c['group']}' data-state='{c['state']}'>"
                f"<div class='card-head'><span class='name'>{c['label']}</span>"
                f"<span class='badge' style='background:{c['color']};'>{STATE_CN.get(c['state'], c['state'])}</span>"
                f"{wchip}"
                f"<span class='muted' style='margin-left:8px;'>截至 {c['as_of']}{merged_note}</span>{stale_badge}</div>"
                f"{c['svg']}"
                + (f"<details><summary>查看区间明细（{c['n_seg']} 个区间）</summary>{c['table']}</details>"
                   if c["n_seg"] else "")
                + "</div>")

    # 必须显式 +8：CI runner 是 UTC，`datetime.now()` 拿到的是 UTC，
    # 但下面一行标的是「（北京时间）」—— 于是线上一直少 8 小时。
    # 本地跑反而看不出问题（本机就是东八区，naive now 恰好等于北京时间）。
    run_at = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    n_total = len(ASSETS)
    n_stale = sum(1 for s in summary if s.get("stale"))
    n_block = sum(1 for s in summary if s.get("gate") == "block")
    stale_note = (f"｜<span style='color:#c00;'>{n_stale} 个品种沿用旧数据</span>"
                  if n_stale else "")
    block_note = (f"｜<span style='color:#b8860b;'>{n_block} 个品种日线与周线反向</span>"
                  if n_block else "")
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场趋势监控面板</title>
<style>
  body {{ font-family:'Microsoft YaHei',Arial,sans-serif; color:#222; margin:0; background:#f5f6f8; }}
  .wrap {{ max-width:1240px; margin:0 auto; padding:20px 24px 60px; }}
  h1 {{ margin:0 0 4px; font-size:24px; }}
  .sub {{ color:#888; margin:0 0 16px; font-size:13px; }}
  .changes {{ background:#fff7e6; border-left:5px solid #d98a00; border-radius:8px; padding:10px 14px;
    margin:0 0 14px; font-size:13px; }}
  .changes.none {{ background:#f2f6f2; border-left-color:#4a9e4a; color:#666; }}
  .changes.blocked {{ background:#fff8ec; border-left-color:#d98a00; color:#7a5200; }}
  .changes .chg {{ display:inline-block; background:#fff; border:1px solid #e6c98a; border-radius:12px;
    padding:2px 10px; margin:3px 4px 0 0; }}
  .changes .chg.block {{ border-color:#e0b060; }}
  .wk {{ display:inline-block; margin-left:8px; padding:1px 9px; border:1px solid #ccc;
    border-radius:10px; font-size:12px; background:#fff; }}
  .wk.blk {{ background:#fff8ec; border-color:#e0b060; color:#8a5a00; }}
  .wk.bad {{ background:#fdecec; border-color:#b02a2a; color:#b02a2a; font-weight:bold; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:10px; margin:4px 0 16px; }}
  .sc {{ background:#fff; border-radius:10px; padding:10px 16px; box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .sc.main {{ border-left:5px solid #3a7bd5; }}
  .sc-name {{ font-size:12px; color:#888; }}
  .sc-val {{ font-size:15px; margin-top:2px; }}
  .sc-val.big {{ font-size:20px; font-weight:bold; }}
  .c-up {{ color:#d23c3c; font-weight:bold; }}
  .c-rng {{ color:#b8860b; font-weight:bold; }}
  .c-dn {{ color:#1a7f37; font-weight:bold; }}
  .tabbar {{ margin:8px 0; font-size:13px; color:#666; }}
  .tab {{ background:#fff; border:1px solid #ddd; border-radius:16px; padding:4px 14px; margin:0 4px 0 0;
    cursor:pointer; font-size:13px; color:#444; }}
  .tab.active {{ background:#3a7bd5; border-color:#3a7bd5; color:#fff; }}
  .group {{ margin:18px 0 6px; color:#444; font-size:15px; border-left:4px solid #888; padding-left:8px; }}
  .sec {{ margin:28px 0 8px; color:#333; font-size:18px; border-left:5px solid #3a7bd5; padding-left:10px; }}
  table.sum {{ border-collapse:collapse; width:100%; background:#fff; border-radius:8px; overflow:hidden;
    box-shadow:0 1px 3px rgba(0,0,0,.08); font-size:13px; }}
  table.sum th, table.sum td {{ padding:8px 10px; border-bottom:1px solid #eee; text-align:left; }}
  table.sum th {{ background:#f0f2f5; }}
  .card {{ background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08);
    padding:14px 16px 10px; margin-bottom:16px; }}
  .card-head {{ display:flex; align-items:center; margin-bottom:6px; flex-wrap:wrap; }}
  .name {{ font-weight:bold; font-size:16px; margin-right:10px; }}
  .badge {{ color:#fff; font-weight:bold; padding:2px 12px; border-radius:12px; font-size:13px; }}
  .muted {{ color:#999; font-size:12px; }}
  .card svg {{ width:100%; height:auto; display:block; }}
  details {{ margin-top:8px; }}
  summary {{ cursor:pointer; color:#3a7bd5; font-size:13px; }}
  .legend {{ background:#fff; border-radius:8px; padding:12px 16px; margin-top:16px; font-size:13px; color:#555; }}
  .empty {{ color:#999; padding:20px; text-align:center; display:none; }}
</style></head>
<body><div class="wrap">
  <h1>市场趋势监控面板</h1>
  <p class="sub">生成时间：{run_at}（北京时间）｜监控 {n_total} 个品种｜红=上升 / 黄=震荡 / 绿=下降（国内配色）{block_note}{stale_note}</p>
  <p class="sub" style="margin-top:-10px;">{asof_note}</p>

  {render_changes(changes, blocked)}
  {render_stats(summary)}
  {render_tabs()}

  <h2 class="sec">当前状态总览</h2>
  {render_summary(summary)}

  <h2 class="sec">各品种 2024 年以来趋势区间</h2>
  <p class="sub">色带为每日状态区间；<b style="color:#b02a2a;">红色斜纹段 = 初始化血统</b>
  （状态机起点的均线猜测，不可纠错，详见页脚）；▲=底背离(切上升) ▼=顶背离(切下降)；持续 &lt;{MIN_DAYS} 天碎片已合并，
  <b>但最后一段（进行中的当前状态）不合并</b> —— 它天数少是因为还没走完，不是因为它短命；
  并掉它会让色带尾部与上方总览的「当前状态」互相矛盾。</p>
  <div id="cardwrap">{card_blocks}</div>
  <div class="empty" id="empty">没有符合筛选条件的品种</div>

  <div class="legend">
    判定规则（日线状态机 G 方案）：MA120 初始化定方向 → 趋势中 RSI 连续 2 日站上 70/30 后回落离开极值转震荡 →
    震荡中 MA60 穿越或 RSI 再触发回趋势；叠加 RSI 背离（最高优先级）、反向穿越纠错、背离失败检测。
    <b>判定起点 = 数据累计够 MA120 那一刻，不是这张图的左边界。</b>
    色带从 2024-01 开始画只是展示需要；状态机本身从该品种第 121 根日线就开始跑，
    与交易层（日线状态机 → 4H 突破）用的是同一个起点、同一套参数。
    早先版本把展示窗口起点当成判定起点，于是每个「2024 年之前就有完整前史」的品种
    都在 2024-01-01 被凭空塞了一个初始状态，而初始血统不受任何纠错 ——
    WTI 原油那段「初始化 319 天」就是这么来的，不是数据不够，是起点被截断。<br>
    <b style="color:#b02a2a;">血统（斜纹段 = 初始化·不可纠错）</b>：每个状态区间都记着它是<b>被哪类事件带进来的</b>，
    而这决定了它能不能被纠错 —— 「均线穿越」进来的可以再被 MA60 反向穿越送回去；
    「背离」进来的可以被「背离失败」推翻（但豁免 MA60，因为逆势信号经不起均线纠错）；
    「初始化」进来的<b>两条通道都不管</b>，出口只剩「RSI 破超卖后再升破超卖」这一个。
    上涨行情里 RSI 永不破 30，于是该出口永不触发 —— 状态就此锁死。<br>
    全部 {n_total} 个品种里只有 HYPE 还会真正出现这一段：它 2024-12 才上线，日线到 2025-04-04
    才凑满 120 根，判定只能从那天开始。而那时的 MA120=20.87 仍在消化上线后 $34→$7 的暴跌，
    收盘 11.89 在均线下方四成 → 判「下降」共 50 天，价格却 <b>+178.1%</b>。
    这是<b>「累计够 120 根」这张门票的最低门槛：数学上够了，经济上还没洗干净</b>，
    状态机无法自我识别。<br>
    修复落在交易层而不是画面上：该血统下<b>不开新仓</b>（既有底仓仍按状态持有、按翻转平）。
    因为实测把「初始化」交给 MA60 纠错会在真下跌行情里把你踢出空头再骗你做多
    （10 币样本外 2 改善 / 3 变差），得不偿失。面板仍如实画出当时的状态判断，
    只在图上打斜纹标注、并在区间明细里给出「⚠ 初始化」——
    免得把「判错了」和「当时只能这么想」混为一谈。<br>
    <b>多周期（周线）状态</b>：周线跑<b>同一套</b>状态机，参数为日线的 ÷5 机械映射
    （MA120→MA24、MA60→MA12，自由度为 0，不为周线单独调参）；只取已走完的周，
    当前周未收盘前不参与判定。日线与周线方向相反时标「日周反向」，<b>仅作状态描述</b>——
    把它当作「风险闸门」的假设已在 2021-2026 全 22 品种样本上验证并证伪：冲突格的前瞻收益
    没有显著劣势（20 日 p=0.59/0.75），闸门版策略仅跑赢 9/22 品种（=掷硬币），故不参与信号过滤。<br>
    <b>数据口径（2026-11-25 修订）</b>：数据源 Hyperliquid（加密永续）/ Yahoo Finance（商品期货·ETF）/
    腾讯行情（A 股指数，东方财富备源）。<br>
    ① <b>收盘判定按各市场自己的时钟</b>，不再一律用 UTC 日期：A 股北京时间 15:00、美股 ETF 21:00Z、
    CME 期货 22:00Z、加密 UTC 日界。早先统一用 UTC，构建若落在 21:00Z 之后，美股当天已收盘的 bar
    会被当成「未收盘」丢掉，同一页出现「A 股截至 11-24、美股截至 11-23」的错位。<br>
    ② <b>ETF 用复权价判定、原价展示</b>。分红在除息日造成一步式下跌，未复权序列因此带系统性下行漂移。
    实测同一套状态机分别跑 close 与 adjclose，2024 年以来状态不同的交易日占比：HYG <b>60.7%</b>
    （未复权判「下降」、复权判「上升」）、TLT 25.4%、IEF 25.8%、QQQ 8.1%、SPY 4.1%、
    无分红的 GLD 0%。所以判定走复权、展示走市价；表里的 MA 已按复权因子还原回市价尺度，
    可以直接和收盘价对比。商品期货与加密无分红，两者恒等。<br>
    ③ <b>商品保留期货连续合约</b>（GC/SI/HG/CL/BZ），不换成对应 ETF。原因不是懒：实测同一资产
    用期货与 ETF 跑出的状态只有 56~78% 一致（铜 77.7%、白银 57.8%），而两个无关资产（铜 vs 白银）
    是 47.8%，且把 ETF 序列前移一日并不改善 —— 换过去只是换一个口径，还会丢掉
    「黄金 3,500 美元/盎司」这种可读的参考价。<b>已知代价</b>：期货存在基差/换月事件，
    2025-07-31 铜 HG=F 单日 -22.25% 而 CPER 只 -2.80%（COMEX 关税溢价崩塌），
    这类日子会影响铜的趋势判定。不做自动抹平 —— 抹平会连带抹掉真实行情。<br>
    ④ <b>跨资产比较请用页头的「比较基准日」</b>：各市场交易日历不同，最新日期不一致是正常的，
    横向比较时统一截到公共基准日。<br>
    本面板为规则化市场状态分类，非投资建议。
  </div>
</div>
<script>
  let curGroup = 'all', curState = 'all';
  function applyFilter() {{
    let shown = 0;
    document.querySelectorAll('#cardwrap .card').forEach(function(card) {{
      const g = card.dataset.group, s = card.dataset.state;
      const okG = (curGroup === 'all' || g === curGroup);
      const okS = (curState === 'all' || s === curState);
      const show = okG && okS;
      card.style.display = show ? '' : 'none';
      if (show) shown++;
    }});
    document.getElementById('empty').style.display = shown ? 'none' : 'block';
    document.querySelectorAll('.tab[data-g]').forEach(function(b) {{
      b.classList.toggle('active', b.dataset.g === curGroup);
    }});
    document.querySelectorAll('.tab[data-s]').forEach(function(b) {{
      b.classList.toggle('active', b.dataset.s === curState);
    }});
  }}
  function setGroup(g) {{ curGroup = g; applyFilter(); }}
  function setState(s) {{ curState = s; applyFilter(); }}
  applyFilter();
</script>
</body></html>"""

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nWrote {OUT}")

    # 写状态快照（供下次构建做今日切换对比与失败降级）
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                       "assets": new_state}, f, ensure_ascii=False, indent=1)
        print(f"Wrote {STATE_FILE}（{len(new_state)} 个品种）")
    except Exception as e:  # noqa: BLE001
        print(f"[快照] 写入失败：{e}")


if __name__ == "__main__":
    main()
