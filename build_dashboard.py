#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dashboard.py —— 市场趋势监控面板（自包含 HTML，含交互）

内容：
  1) 状态统计卡片：上升/震荡/下降数量 + 分组分布
  2) 今日状态切换提示（对比上一次部署的 monitor_state.json）
  3) 当前状态总览：18 品种分三组，含 状态/收盘(带单位)/RSI14/MA60/MA120/触发日期/原因/持续
  4) 各品种 2024 年以来趋势区间图：状态色带 + 价格折线 + 背离标记 + 可折叠区间明细
  5) 交互：分组 tab + 状态筛选（原生 JS，无外部依赖）
  6) 容错：某品种取数失败时沿用上次成功数据（monitor_state.json），并标注「沿用旧数据」

数据源：Hyperliquid 永续（加密）/ Yahoo 期货·ETF（商品、宏观）。
代理开关：本地默认 127.0.0.1:10809；云端设 REGIME_PROXY=none 直连。
快照：构建时读 {Pages}/monitor_state.json（上次部署）做「今日切换」对比与失败降级，
      构建后写 monitor_state.json，由 workflow 一并部署到 gh-pages。
用法：python build_dashboard.py  →  生成 regime_dashboard.html + monitor_state.json
"""
import bisect
import json
import os
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from visualize_btc_regime import (
    daily_states, build, merge_short_segments, gen_svg, seg_table, fetch_binance_daily,
)
from visualize_eth_gold_regime import fetch_yahoo_daily
from daily_regime_alert import (
    ASSETS, GROUP_ORDER, fetch_hyperliquid_daily, fetch_ashare_daily,
    compute_regime, compute_regime_weekly,
    resonance_gate, GATE_CN, trig_cn,
    STATE_CN, STATE_COLOR, TRIGGER_CN, fmt_price, WARM_START, START,
    drop_unclosed, drop_unclosed_cn,
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


def trim_unclosed(a, rows):
    """按市场各自的收盘规则剔除未收盘 bar（A 股按北京时间 15:00，其余按 UTC 日线）。"""
    return drop_unclosed_cn(rows) if a["source"] == "ashare" else drop_unclosed(rows)


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
                f"<td style='text-align:center;'>{s['regime'].get('since_days', '-')}</td></tr>")
        blocks += (
            f"<h3 class='group'>{g}（{len(groups[g])}）</h3>"
            f"<table class='sum'><tr><th>品种</th><th>日线</th><th>周线</th><th>收盘</th><th>RSI14</th>"
            f"<th>MA60</th><th>MA120</th><th>触发日期</th><th>最近触发</th><th>持续(日)</th></tr>"
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
            reg = compute_regime(closes, dates)
            wreg = compute_regime_weekly(rows)
            wstate = wreg.get("state")
            gate, gate_note = resonance_gate(reg["state"], wstate)
            if gate == "block":
                blocked.append({"label": a["label"], "daily": reg["state"], "weekly": wstate})
            # 今日状态切换对比（仅对有上次记录且状态变化的品种）
            prev = prev_assets.get(a["key"])
            if prev and prev.get("state") and prev["state"] != reg["state"]:
                changes.append({"label": a["label"], "from": prev["state"], "to": reg["state"]})

            s0 = bisect.bisect_left(dates, START)
            if s0 < 120:
                s0 = 120
            st, div, _trig = daily_states(closes, s0, 70, 30, True, 2, True)
            pts, segs = build(dates, closes, st)
            raw_n = len(segs)
            segs = merge_short_segments(segs, MIN_DAYS)
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
            })
            summary.append({"key": a["key"], "label": a["label"], "group": a["group"],
                            "unit": a.get("unit", ""), "regime": reg, "stale": False,
                            "weekly_state": wstate, "gate": gate, "gate_note": gate_note})
            new_state[a["key"]] = dict(reg, weekly_state=wstate, gate=gate)
            print(f"[{a['key']:5}] 日{STATE_CN.get(state, state):3} "
                  f"周{STATE_CN.get(wstate, wstate or '—'):3} {GATE_CN.get(gate, gate):2} "
                  f"{len(segs)} 个区间（合并 {raw_n - len(segs)} 个碎片）  {dates[-1]}")
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

  {render_changes(changes, blocked)}
  {render_stats(summary)}
  {render_tabs()}

  <h2 class="sec">当前状态总览</h2>
  {render_summary(summary)}

  <h2 class="sec">各品种 2024 年以来趋势区间</h2>
  <p class="sub">色带为每日状态区间；▲=底背离(切上升) ▼=顶背离(切下降)；持续 &lt;{MIN_DAYS} 天碎片已合并，
  <b>但最后一段（进行中的当前状态）不合并</b> —— 它天数少是因为还没走完，不是因为它短命；
  并掉它会让色带尾部与上方总览的「当前状态」互相矛盾。</p>
  <div id="cardwrap">{card_blocks}</div>
  <div class="empty" id="empty">没有符合筛选条件的品种</div>

  <div class="legend">
    判定规则（日线状态机 G 方案）：MA120 初始化定方向 → 趋势中 RSI 连续 2 日站上 70/30 后回落离开极值转震荡 →
    震荡中 MA60 穿越或 RSI 再触发回趋势；叠加 RSI 背离（最高优先级）、反向穿越纠错、背离失败检测。
    短历史品种（如 HYPE）自动用「数据起点 + 120 根热身」动态起点。<br>
    <b>多周期（周线）状态</b>：周线跑<b>同一套</b>状态机，参数为日线的 ÷5 机械映射
    （MA120→MA24、MA60→MA12，自由度为 0，不为周线单独调参）；只取已走完的周，
    当前周未收盘前不参与判定。日线与周线方向相反时标「日周反向」，<b>仅作状态描述</b>——
    把它当作「风险闸门」的假设已在 2021-2026 全 22 品种样本上验证并证伪：冲突格的前瞻收益
    没有显著劣势（20 日 p=0.59/0.75），闸门版策略仅跑赢 9/22 品种（=掷硬币），故不参与信号过滤。<br>
    本面板为规则化市场状态分类，非投资建议。数据源：Hyperliquid（加密永续）/ Yahoo Finance（商品期货·ETF）/
    腾讯行情（A 股指数，东方财富备源）。A 股按北京时间 15:00 判定当日是否已收盘，其余按 UTC 日线。
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
