#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_dashboard.py —— 市场趋势监控面板（自包含 HTML，含交互）

内容：
  1) 状态统计卡片：上升/震荡/下降数量 + 四组（加密/贵金属/商品能源/宏观）分布
  2) 当前状态总览：18 品种分四组，含状态、收盘、RSI14、MA60、MA120、触发日期、原因、持续
  3) 各品种 2024 年以来趋势区间图：状态色带 + 价格折线 + 背离标记 + 可折叠区间明细
  4) 交互：分组 tab 筛选、状态筛选（原生 JS，无需外部依赖）

复用 G 方案状态机（daily_states）与可视化函数（gen_svg / seg_table）。
数据源：Binance 现货 / Yahoo ETF·期货 / Hyperliquid（HYPE）。
代理开关：本地默认走 127.0.0.1:10809；云端设 REGIME_PROXY=none 直连。
用法：python build_dashboard.py  →  生成 regime_dashboard.html
"""
import bisect
from collections import Counter
from datetime import datetime, timezone

from visualize_btc_regime import (
    daily_states, build, merge_short_segments, gen_svg, seg_table, fetch_binance_daily,
)
from visualize_eth_gold_regime import fetch_yahoo_daily
from daily_regime_alert import (
    ASSETS, GROUP_ORDER, fetch_hyperliquid_daily, compute_regime,
    STATE_CN, STATE_COLOR, TRIGGER_CN, fmt_price, WARM_START, START, drop_unclosed,
)

MIN_DAYS = 3
OUT = "regime_dashboard.html"


def fetch_asset(a, warm_dt):
    if a["source"] == "binance":
        return fetch_binance_daily(a["symbol"], warm_dt)
    if a["source"] == "hyperliquid":
        return fetch_hyperliquid_daily(a["symbol"])
    return fetch_yahoo_daily(a["symbol"], warm_dt)


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
            trs += (
                f"<tr><td>{s['label']}</td>"
                f"<td style='background:{color};color:#fff;font-weight:bold;text-align:center;'>"
                f"{STATE_CN.get(state, state)}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('close'))}</td>"
                f"<td style='text-align:right;'>{s['regime'].get('rsi') or 0:.1f}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('ma60'))}</td>"
                f"<td style='text-align:right;'>{fmt_price(s['regime'].get('ma120'))}</td>"
                f"<td style='white-space:nowrap;color:#666;'>{s['regime'].get('trigger_date', '—')}</td>"
                f"<td>{TRIGGER_CN.get(s['regime'].get('trigger', ''), s['regime'].get('trigger', ''))}</td>"
                f"<td style='text-align:center;'>{s['regime'].get('since_days', '-')}</td></tr>")
        blocks += (
            f"<h3 class='group'>{g}（{len(groups[g])}）</h3>"
            f"<table class='sum'><tr><th>品种</th><th>状态</th><th>收盘</th><th>RSI14</th>"
            f"<th>MA60</th><th>MA120</th><th>触发日期</th><th>最近触发</th><th>持续(日)</th></tr>"
            f"{trs}</table>")
    return blocks


def render_stats(summary):
    """状态统计卡片：整体 + 四组分布。"""
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


def main():
    warm_dt = datetime.strptime(WARM_START, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    summary = []
    cards = []

    for a in ASSETS:
        try:
            rows = drop_unclosed(fetch_asset(a, warm_dt))
            dates = [r["date"] for r in rows]
            closes = [r["close"] for r in rows]
            reg = compute_regime(closes, dates)
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
                "svg": gen_svg(pts, segs, marks), "table": seg_table(segs), "n_seg": len(segs),
                "raw_n": raw_n, "merged": raw_n - len(segs), "as_of": dates[-1],
            })
            summary.append({"label": a["label"], "group": a["group"], "regime": reg})
            print(f"[{a['key']:5}] {STATE_CN.get(state, state):3}  {len(segs)} 个区间"
                  f"（合并 {raw_n - len(segs)} 个碎片）  {dates[-1]}")
        except Exception as e:  # noqa: BLE001
            print(f"[{a['key']:5}] 失败: {e}")
            cards.append({"label": a["label"], "group": a["group"], "state": "N/A",
                          "color": "#888", "svg": "<p style='color:#999'>取数/计算失败</p>",
                          "table": "", "n_seg": 0, "raw_n": 0, "merged": 0, "as_of": "-"})
            summary.append({"label": a["label"], "group": a["group"],
                            "regime": {"state": "N/A", "reason": str(e)}})

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
            card_blocks += (
                f"<div class='card' data-group='{c['group']}' data-state='{c['state']}'>"
                f"<div class='card-head'><span class='name'>{c['label']}</span>"
                f"<span class='badge' style='background:{c['color']};'>{STATE_CN.get(c['state'], c['state'])}</span>"
                f"<span class='muted' style='margin-left:8px;'>截至 {c['as_of']}{merged_note}</span></div>"
                f"{c['svg']}"
                f"<details><summary>查看区间明细（{c['n_seg']} 个区间）</summary>{c['table']}</details>"
                f"</div>")

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    n_total = len(ASSETS)
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>市场趋势监控面板</title>
<style>
  body {{ font-family:'Microsoft YaHei',Arial,sans-serif; color:#222; margin:0; background:#f5f6f8; }}
  .wrap {{ max-width:1240px; margin:0 auto; padding:20px 24px 60px; }}
  h1 {{ margin:0 0 4px; font-size:24px; }}
  .sub {{ color:#888; margin:0 0 16px; font-size:13px; }}
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
  <p class="sub">生成时间：{run_at}（北京时间）｜监控 {n_total} 个品种｜红=上升 / 黄=震荡 / 绿=下降（国内配色）</p>

  {render_stats(summary)}
  {render_tabs()}

  <h2 class="sec">当前状态总览</h2>
  {render_summary(summary)}

  <h2 class="sec">各品种 2024 年以来趋势区间</h2>
  <p class="sub">色带为每日状态区间；▲=底背离(切上升) ▼=顶背离(切下降)；持续 &lt;{MIN_DAYS} 天碎片已合并。</p>
  <div id="cardwrap">{card_blocks}</div>
  <div class="empty" id="empty">没有符合筛选条件的品种</div>

  <div class="legend">
    判定规则（日线状态机 G 方案）：MA120 初始化定方向 → 趋势中 RSI 连续 2 日站上 70/30 后回落离开极值转震荡 →
    震荡中 MA60 穿越或 RSI 再触发回趋势；叠加 RSI 背离（最高优先级）、反向穿越纠错、背离失败检测。
    短历史品种（如 HYPE）自动用「数据起点 + 120 根热身」动态起点。<br>
    本面板为规则化市场状态分类，非投资建议。数据源：Binance / Yahoo Finance / Hyperliquid。
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


if __name__ == "__main__":
    main()
