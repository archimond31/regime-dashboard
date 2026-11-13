#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 5000 U 配置回放报告 _bt_5000.html（合约口径，含做空）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_config as PC

D = json.load(open("_bt_5000.json", encoding="utf-8"))
CAP = D["cap"]
BEST = D["best"]
F = D["plans"][BEST]
FCUR = D["b_curve"]
FMON = F["months"]
SPOT = next(r for r in D["spot_rows"] if "常规" in r["lab"])
SX = D["spot_x"]
ST = D["start_rows"]
FR = D["fund_rows"]

RED, GRN, NAVY, GREY = "#c0392b", "#1e8449", "#16324f", "#7b8794"


def money(v):
    return f"{CAP * v:,.0f}"


def pct(v, sign=True):
    return f"{v * 100:+.1f}%" if sign else f"{v * 100:.1f}%"


def spark(curve, w=860, h=250, pad=38):
    vals = [v for _, v in curve]
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1
    lo -= rng * 0.06
    hi += rng * 0.04
    rng = hi - lo
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = pad + i / (n - 1) * (w - pad - 12)
        y = h - pad - (v - lo) / rng * (h - pad - 18)
        pts.append((x, y))
    path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = path + f" L{pts[-1][0]:.1f},{h - pad} L{pts[0][0]:.1f},{h - pad} Z"
    y1 = h - pad - (1.0 - lo) / rng * (h - pad - 18)
    grid = ""
    for g in range(5):
        v = lo + rng * g / 4
        y = h - pad - (v - lo) / rng * (h - pad - 18)
        grid += f'<line x1="{pad}" y1="{y:.1f}" x2="{w - 12}" y2="{y:.1f}" stroke="#eceff1"/>'
        grid += (f'<text x="{pad - 6}" y="{y + 3.5:.1f}" text-anchor="end" font-size="9.5" '
                 f'fill="{GREY}">{v:.2f}</text>')
    dates = [d for d, _ in curve]
    xs = ""
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        i = min(int(frac * (n - 1)), n - 1)
        x = pad + i / (n - 1) * (w - pad - 12)
        xs += (f'<text x="{x:.1f}" y="{h - pad + 15}" text-anchor="middle" '
               f'font-size="9.5" fill="{GREY}">{dates[i][5:]}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto">'
            f'{grid}{xs}'
            f'<line x1="{pad}" y1="{y1:.1f}" x2="{w - 12}" y2="{y1:.1f}" '
            f'stroke="{GREY}" stroke-dasharray="3,3" stroke-width="1" opacity=".7"/>'
            f'<path d="{area}" fill="url(#g1)" opacity=".85"/>'
            f'<path d="{path}" fill="none" stroke="{NAVY}" stroke-width="1.9"/>'
            f'<defs><linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0%" stop-color="{NAVY}" stop-opacity=".22"/>'
            f'<stop offset="100%" stop-color="{NAVY}" stop-opacity="0"/>'
            f'</linearGradient></defs></svg>')


def month_bars(months, w=860, h=200, pad=40):
    n = len(months)
    step = (w - pad - 12) / n
    bw = step * 0.46
    mx = max(abs(r) for _, r, _ in months) or 0.01
    zero = pad + (h - 2 * pad) / 2
    out = [f'<line x1="{pad}" y1="{zero:.1f}" x2="{w - 12}" y2="{zero:.1f}" stroke="#cfd8dc"/>']
    for i, (k, r, _v) in enumerate(months):
        cx = pad + step * (i + 0.5)
        bh = abs(r) / mx * (h - 2 * pad) / 2 * 0.90
        col = RED if r >= 0 else GRN
        if r >= 0:
            out.append(f'<rect x="{cx - bw / 2:.1f}" y="{zero - bh:.1f}" width="{bw:.1f}" '
                       f'height="{bh:.1f}" fill="{col}" rx="2"/>')
            ty = zero - bh - 6
        else:
            out.append(f'<rect x="{cx - bw / 2:.1f}" y="{zero:.1f}" width="{bw:.1f}" '
                       f'height="{bh:.1f}" fill="{col}" rx="2"/>')
            ty = zero + bh + 13
        out.append(f'<text x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle" font-size="9" '
                   f'font-weight="700" fill="{col}" stroke="#fff" stroke-width="2.6" '
                   f'style="paint-order:stroke">{r * 100:+.1f}%</text>')
        out.append(f'<text x="{cx:.1f}" y="{h - 10}" text-anchor="middle" font-size="9.5" '
                   f'fill="{GREY}">{k[2:]}</text>')
    return f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto">{"".join(out)}</svg>'


# ---------- 配置表 ----------
cfg_rows = ""
for r in PC.table(CAP):
    badge = {"宽松": "#e8f5e9|#1e8449", "可接受": "#fff8e1|#8d6e63",
             "需拆单": "#fff3e0|#d97706", "超标": "#ffebee|#c0392b"}.get(r["flag"], "#eee|#666")
    bg, fg = badge.split("|")
    cfg_rows += (
        f'<tr><td class="c"><b>{r["c"]}</b></td>'
        f'<td class="r">{r["w"] * 100:.0f}%</td>'
        f'<td class="r">{r["base"]:,.0f}</td>'
        f'<td class="r" style="color:#999">{r["add"]:,.0f}</td>'
        f'<td class="r"><b>{r["peak"]:,.0f}</b></td>'
        f'<td class="r" style="color:{GRN}">空 {abs(r["short"]):,.0f}</td>'
        f'<td class="r">{r["imp"] * 100:.2f}%</td>'
        f'<td class="c"><span style="background:{bg};color:{fg};padding:1px 7px;'
        f'border-radius:9px;font-size:11px;font-weight:600">{r["flag"]}</span></td>'
        f'<td class="r">{r["slices"]}</td></tr>')

plan_order = ["A 等权（现状）", "B CRV6% + HYPE5%", "C 只降 HYPE 到 5%",
              "D 只降 CRV 到 6%", "E 只降 CRV 到 7%", BEST]
plan_rows = ""
for k in plan_order:
    v = D["plans"][k]
    hi = ' class="hl"' if k == BEST else ""
    mark = " ★" if k == BEST else ""
    plan_rows += (f'<tr{hi}><td>{k}{mark}</td><td class="r">{money(v["final"])} U</td>'
                  f'<td class="r" style="color:{RED}">{pct(v["ret"])}</td>'
                  f'<td class="r" style="color:{GRN}">{pct(v["dd"])}</td>'
                  f'<td class="r">{v["ret"] / abs(v["dd"]):.2f}</td></tr>')

contrib_rows = ""
for c, w, s0, e0, gain, ret, share in D["contrib"]:
    contrib_rows += (f'<tr><td class="c"><b>{c}</b></td><td class="r">{w * 100:.0f}%</td>'
                     f'<td class="r">{s0:,.0f}</td><td class="r">{e0:,.0f}</td>'
                     f'<td class="r" style="color:{RED if gain >= 0 else GRN}">{gain:+,.0f}</td>'
                     f'<td class="r">{ret * 100:+.1f}%</td>'
                     f'<td class="r">{share * 100:.0f}%</td></tr>')

start_rows = ""
for row in ST:
    cells = ""
    for k in ("1.5", "1.0", "0.5"):
        st = row[k]
        cells += (f'<td class="r">{money(st["final"])} U</td>'
                  f'<td class="r" style="color:{RED}">{pct(st["ret"])}</td>'
                  f'<td class="r" style="color:{GRN}">{pct(st["dd"])}</td>')
    start_rows += f'<tr><td><b>{row["d0"]}</b></td>{cells}</tr>'

spot_rows_html = ""
for r in D["spot_rows"]:
    hi = ' class="hl"' if "常规" in r["lab"] else ""
    mark = " ★" if "常规" in r["lab"] else ""
    spot_rows_html += (f'<tr{hi}><td>{r["lab"]}{mark}</td><td class="r">{money(r["final"])} U</td>'
                       f'<td class="r" style="color:{RED}">{pct(r["ret"])}</td>'
                       f'<td class="r" style="color:{GRN}">{pct(r["dd"])}</td>'
                       f'<td class="r">{r["ret"] / abs(r["dd"]):.2f}</td></tr>')

fund_rows_html = ""
for r in FR:
    hi = ' class="hl"' if r["bp"] == 3 else ""
    fund_rows_html += (f'<tr{hi}><td class="r">{r["bp"]:+d} bp</td>'
                       f'<td class="r">{money(r["final"])} U</td>'
                       f'<td class="r" style="color:{RED}">{pct(r["ret"])}</td>'
                       f'<td class="r" style="color:{GRN}">{pct(r["dd"])}</td></tr>')

crv_rows = ""
for r in D["crv_scan"]:
    crv_rows += (f'<tr><td class="r">{r["cw"] * 100:.0f}%</td>'
                 f'<td class="r">{money(r["final"])} U</td>'
                 f'<td class="r" style="color:{RED}">{pct(r["ret"])}</td>'
                 f'<td class="r" style="color:{GRN}">{pct(r["dd"])}</td></tr>')

sx_rows = ""
for r in SX:
    sx_rows += (f'<tr><td><b>{r["d0"]}</b></td><td class="r">{r["n"]}</td>'
                f'<td class="r">{money(r["final"])} U</td>'
                f'<td class="r" style="color:{RED}">{pct(r["ret"])}</td>'
                f'<td class="r" style="color:{GRN}">{pct(r["dd"])}</td></tr>')

HTML = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>5000 U · 合约做空配置 · 2026 回放</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;padding:34px 30px 60px;background:#f4f6f8;color:#1c2b36;
font:14px/1.72 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1040px;margin:0 auto}}
h1{{font-size:25px;margin:0 0 6px;letter-spacing:-.3px}}
.lead{{color:{GREY};font-size:13px;margin-bottom:26px}}
h2{{font-size:16px;margin:34px 0 12px;padding-left:9px;border-left:3px solid {NAVY}}}
h3{{font-size:13.5px;margin:20px 0 8px;color:{NAVY}}}
.card{{background:#fff;border:1px solid #e3e8ec;border-radius:9px;padding:18px 20px;margin:14px 0}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0 6px}}
.kpi{{background:#fff;border:1px solid #e3e8ec;border-radius:9px;padding:14px 16px}}
.kpi .t{{font-size:11.5px;color:{GREY};margin-bottom:3px}}
.kpi .v{{font-size:22px;font-weight:700;letter-spacing:-.5px}}
.kpi .s{{font-size:11.5px;color:{GREY};margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:8px 0}}
th{{background:#eef2f5;font-weight:600;text-align:left;padding:7px 9px;
border-bottom:1px solid #dde4ea;font-size:11.5px;color:#44586a;white-space:nowrap}}
td{{padding:6px 9px;border-bottom:1px solid #f0f3f5}}
td.r,th.r{{text-align:right;font-variant-numeric:tabular-nums}}
td.c,th.c{{text-align:center}}
tr.hl td{{background:#fffdf2}}
tr:hover td{{background:#fafcfd}}
.warn{{background:#fff6f5;border-left:3px solid {RED};padding:13px 16px;border-radius:0 6px 6px 0;margin:14px 0}}
.ok{{background:#f4faf6;border-left:3px solid {GRN};padding:13px 16px;border-radius:0 6px 6px 0;margin:14px 0}}
.note{{background:#f7f9fb;border-left:3px solid {GREY};padding:12px 15px;border-radius:0 6px 6px 0;
margin:12px 0;font-size:12.5px;color:#41525f}}
code{{background:#eef2f5;padding:1px 5px;border-radius:3px;font-size:12px;
font-family:ui-monospace,Consolas,monospace}}
.foot{{margin-top:36px;padding-top:14px;border-top:1px solid #dfe5ea;color:{GREY};font-size:11.5px}}
</style></head><body><div class="wrap">

<h1>5000 U · 合约做空配置 · 2026 回放</h1>
<div class="lead">数据区间 {D["dates"][0]} → {D["dates"][-1]} · {len(D["dates"])} 个交易日 ·
10 个品种 · 三层骨架 + 空头层 · 峰值敞口 多头 1.5× / 空头 0.5×</div>

<div class="kpis">
  <div class="kpi"><div class="t">期末（合约 1.5×）</div>
    <div class="v">{money(F["final"])} U</div>
    <div class="s">2026 单年</div></div>
  <div class="kpi"><div class="t">收益</div>
    <div class="v" style="color:{RED}">{pct(F["ret"])}</div>
    <div class="s">跨 2025 起 +46.8%</div></div>
  <div class="kpi"><div class="t">最大回撤</div>
    <div class="v" style="color:{GRN}">{pct(F["dd"])}</div>
    <div class="s">跨 2025 起 <b>-34.9%</b></div></div>
  <div class="kpi"><div class="t">单位回撤收益</div>
    <div class="v">{F["ret"] / abs(F["dd"]):.2f}</div>
    <div class="s">10 币等权满仓买入持有：-12.9% / -43%</div></div>
</div>

<div class="warn"><b>做空是有代价的，三件事先看清楚。</b>
<ol style="margin:8px 0 0;padding-left:22px">
<li><b>2026 的回撤不可外推。</b>单年 -15.2%，但把起点拉到 2025-01（682 天）就是
<b>-34.9%</b>。5000 U 有可能先走到 3,250 U。心理预算按 -35% 备，不是 -15%。</li>
<li><b>空头是 2026 的主要收益来源，而这来源会随行情反转。</b>
同期空头 +69.1R、多头全层只有 +30.2R。这是单边下跌市的产物，
2027 若是牛市，空头层会持续放血 —— 它的角色是保险，不是引擎。</li>
<li><b>但资金费不是主要风险。</b>回测假设空头收 3bp/日；把资金费一路扫到
-10bp/日（年化 -36%，极端），收益也只从 +75.3% 掉到 +72.0%，损失 3.3pp。
真正咬人的是方向，不是持有成本。</li>
</ol></div>

<h2>一、最终配置（5000 U · 合约）</h2>
<div class="note">费率结构是<b>不对称</b>的，这是策略的核心特征：
UP 突破 → 多头 1.5×；RANGE → 底仓 1.0×（不开新仓）；DOWN 突破 → 空头 0.5×。
DOWN 时底仓归零，只剩加仓层能做空，所以空头峰值只有多头的三分之一。<br>
资金占用：多头满峰值 7,500 U（真金 5,000 + 借 2,500），空头满峰值 2,500 U
（永续按 10% 保证金约 250 U）。</div>
<table>
<tr><th class="c">品种</th><th class="r">权重</th><th class="r">底仓(U)</th>
<th class="r">加仓层(U)</th><th class="r">多头峰值</th><th class="r">空头峰值</th>
<th class="r">冲击占最差4H</th><th class="c">容量</th><th class="r">拆单</th></tr>
{cfg_rows}
<tr style="background:#eef2f5;font-weight:600"><td class="c">合计</td>
<td class="r">100%</td><td class="r">5,000</td><td class="r">2,500</td>
<td class="r">7,500</td><td class="r" style="color:{GRN}">空 2,500</td>
<td class="r">—</td><td class="c">—</td><td class="r">11</td></tr>
</table>
<div class="note">CRV 权重 7% 是唯一偏离等权的地方，理由是<b>容量</b>不是回测：
它的多头峰值 525 U 对最差 4H 成交额（3.27 万）占 1.6%，属于「必须拆单但可接受」的上沿。
HYPE 保持 10% —— 曾想降到 5%，回测显示收益 -4.2pp、回撤反而 -2.4pp。</div>

<h2>二、收益曲线与月度（2026）</h2>
<div class="card">{spark(FCUR)}
<div style="font-size:11.5px;color:{GREY};margin-top:6px">
虚线为 1.0 净值线。2-4 月连亏，5 月起转正，8-9 两个月贡献了绝大部分涨幅。</div></div>

<div class="card"><h3 style="margin-top:0">月度</h3>{month_bars(FMON)}</div>

<h2>三、资金费敏感性</h2>
<table>
<tr><th class="r">资金费/日</th><th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th></tr>
{fund_rows_html}
</table>
<div class="ok">从 +3bp 扫到 -10bp/日，收益只从 +75.3% 掉到 +72.0%（-3.3pp）。
原因是空头名义本来就小（峰值 0.5×、平均占用更低）。
<b>做空的风险在方向，不在持有成本</b> —— 别因为担心资金费而不敢做空，也别因为资金费便宜就放大空头。</div>

<h2>四、六个权重方案对比（2026）</h2>
<table>
<tr><th>方案</th><th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th>
<th class="r">单位回撤</th></tr>
{plan_rows}
</table>
<div class="ok"><b>结论：</b>降 HYPE 是双重变差（收益↓回撤↑）—— 它与 AAVE 相关性仅 0.06，
是组合里的减震器；降 CRV 是双重变好，但幅度在噪声内（+1.1pp / -0.2pp）。
所以 CRV 的 7% 按容量定，回测只用来确认「没有明显损害」。</div>

<h2>五、起点 × 敞口矩阵（为什么不是更高杠杆）</h2>
<table>
<tr><th rowspan="2">起点</th><th colspan="3" class="c">1.5×</th>
<th colspan="3" class="c">1.0×</th><th colspan="3" class="c">0.5×</th></tr>
<tr><th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th>
<th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th>
<th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th></tr>
{start_rows}
</table>
<div class="warn">2026 单年内回撤几乎严格线性（<code>回撤 ≈ 10.15% × 总名义</code>）、收益还略超线性，
看起来「放大敞口很划算」。但跨周期就反转：2025 起，单位回撤收益
1.5× 是 <b>1.34</b>、1.0× 是 1.61、0.5× 是 1.85。<br>
<b>单年回测挑杠杆，一定会挑到最激进的那个。</b>这就是为什么必须在多个起点上看。</div>

<h2>六、分币贡献</h2>
<table>
<tr><th class="c">币</th><th class="r">权重</th><th class="r">起点</th><th class="r">期末</th>
<th class="r">盈亏</th><th class="r">该币收益</th><th class="r">占总盈利</th></tr>
{contrib_rows}
</table>
<div class="note">UNI 一个品种占总盈利 39%、HYPE 占 20%，合计 59%。
这是<b>集中度风险</b>，不是可复制的收益来源。UNI 的 +295.6% 来自一笔
8/21 在 3.821 做多、持有 120 根 4H、9/10 在 6.080 出场的交易（单笔 +15.7R）。
这种单子一年可能就一两笔，别把它当常态。</div>

<h2>七、如果哪天不想做空了（现货对照）</h2>
<table>
<tr><th>档位</th><th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th>
<th class="r">单位回撤</th></tr>
{spot_rows_html}
</table>
<h3>现货常规档跨起点</h3>
<table>
<tr><th>起点</th><th class="r">天数</th><th class="r">期末</th><th class="r">收益</th>
<th class="r">回撤</th></tr>
{sx_rows}
</table>
<div class="note">现货常规档在 2026 是 +37.6%/-12.7%，跨 2025 是 +25.7%/-25.8%。
把它和上面的合约档比：合约多赚 ~38pp，代价是回撤从 -25.8% 到 -34.9%。
<b>这是做空层真实的定价。</b></div>

<h2>八、CRV 权重扫描（证明这条证据不能用）</h2>
<table>
<tr><th class="r">CRV 权重</th><th class="r">期末</th><th class="r">收益</th><th class="r">回撤</th></tr>
{crv_rows}
</table>
<div class="note">如果是结构性原因，曲线应该在某个点出现拐点或平台。
这里一路单调到底、权重 0（彻底剔除）最好 —— 这只能说明「2026 年 CRV 是最差的品种」，
不能说明「CRV 权重应该更低」。任何单一年份挑掉当年最差的品种都会让回测变好。</div>

<h2>九、执行规则</h2>
<table>
<tr><th style="width:120px">环节</th><th>规则</th></tr>
<tr><td><b>多头底仓</b></td><td>日线 UP / RANGE 时持有 100% 权重额度；RANGE 不加仓也不减仓</td></tr>
<tr><td><b>多头加仓</b></td><td>UP 且 4H 收盘突破前 20 根高点 → 加到底仓的 150%</td></tr>
<tr><td><b>做空</b></td><td>日线 DOWN 且 4H 收盘跌破前 20 根低点 → 空 50% 权重额度；DOWN 未跌破时是空仓，不是做空</td></tr>
<tr><td><b>止损</b></td><td>1.5×ATR(14) 硬止损；日线状态翻转即平（不反手，反手等下一根 4H 确认）；满 120 根 4H 兜底</td></tr>
<tr><td><b>波动闸门</b></td><td>ATR 滚动分位 &gt;85% 不开新仓（当前 SOL 就是被这条拦住的）</td></tr>
<tr><td><b>拆单</b></td><td>只有 CRV 需要拆 2 笔，其余 9 个币 1 笔。限价单，1 小时内完成</td></tr>
<tr><td><b>回撤应对</b></td><td>回撤触及 -20% 时把敞口整体降到 0.7×，<b>不收紧止损、不改阈值</b></td></tr>
</table>

<div class="warn"><b>当前实盘仓位（2026-11-13 08:00 UTC 那根 4H）</b>：
9 个品种日线 DOWN，其中 <b>8 个</b>已跌破、持有空头 —— BTC/ETH 各 -275 U，
BNB/XRP/LINK/UNI/AAVE/HYPE 各 -250 U，合计 <b>空 2,050 U</b>。<br>
<b>SOL 没有空头</b>：它虽然也是 DOWN，但 ATR 滚动分位 88% &gt; 85%，被波动闸门拦在门外，
target 是 0 而不是 -250。这是闸门的正常动作，不是漏单。<br>
CRV 日线 RANGE，持有多头底仓 350 U。净名义 <b>-1,700 U（-34% 本金）</b>，
距空头峰值 2,500 U 还有 450 U 空间 —— 但只有 SOL 解除闸门才用得上，
而它一旦解除，说明波动已经回落，届时空头性价比也下降了。</div>

<div class="foot">
回测脚本 <code>_bt_5000.py</code> · 配置真源 <code>portfolio_config.py</code> ·
实盘信号 <code>live_signals.py</code>（输出 <code>target</code>，负数代表做空）<br>
口径：0.04% 单边手续费 · 空头按 3bp/日<b>收</b>资金费（见第四节敏感性）·
所有 K 线均为已收盘 bar（无未来函数）
</div>
</div></body></html>"""

open("_bt_5000.html", "w", encoding="utf-8").write(HTML)
print(f"已写出 _bt_5000.html  ({len(HTML):,} 字节)")
