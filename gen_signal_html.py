#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 _signal.json 渲染成 signal_now.html。

刻意用「浅色 + 内联样式 + table 布局 + 零 JS」：
这份文件既要在浏览器里看，也要能直接当邮件正文发出去。
邮件客户端（尤其 QQ 邮箱/Outlook）会吃掉 <style>、丢掉 grid/flex、不执行 JS，
只有 table + 内联样式是稳的。
"""
import json
import os

SRC = "_signal.json"
OUT = "signal_now.html"

d = json.load(open(SRC, encoding="utf-8"))
COINS = d["coins"]
urgent = [o for o in COINS if o["action"] in ("开仓", "平仓", "止损临近", "即将到期")]
holds = [o for o in COINS if o["pos"]]
blocked = [o for o in COINS if o["gate_blocked"]]
errs = d.get("errors") or []

UP, DN, BD, TX, MU = "#c62828", "#2e7d32", "#e0e0e0", "#1a1a1a", "#757575"


def px(v):
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:,.2f}"
    if a >= 1:
        return f"{v:.4f}"
    return f"{v:.5f}"


def pct(v, sign=False):
    if v is None:
        return "—"
    return (f"{v:+.2f}%" if sign else f"{v:.2f}%")


def side_txt(s):
    return ("做多" if s > 0 else "做空")


def state_color(s):
    return UP if s == "UP" else (DN if s == "DOWN" else MU)


# ---------- 动作区 ----------
if urgent:
    act_rows = ""
    for o in urgent:
        c = {"开仓": UP, "平仓": DN, "止损临近": "#ef6c00", "即将到期": "#6a1b9a"}[o["action"]]
        act_rows += (
            f'<tr><td style="padding:8px 10px;border-bottom:1px solid #f0d9d9;font-size:13px">'
            f'<b style="color:{c}">{o["action"]}</b></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f0d9d9;font-size:13px"><b>{o["c"]}</b></td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #f0d9d9;font-size:13px;color:{TX}">{o["note"]}</td></tr>')
    action_block = (
        f'<div style="border:2px solid {UP};border-radius:8px;margin:18px 0;background:#fff5f5">'
        f'<div style="padding:10px 12px;background:{UP};color:#fff;font-size:13px;font-weight:600;'
        f'border-radius:6px 6px 0 0">本次需要动作（{len(urgent)} 个）</div>'
        f'<table style="width:100%;border-collapse:collapse">{act_rows}</table></div>')
else:
    action_block = (
        f'<div style="border:1px solid #c8e6c9;background:#f1f8f4;border-radius:8px;padding:12px 14px;'
        f'margin:18px 0;font-size:13px;color:#1b5e20">'
        f'<b>本轮无需任何动作。</b>10 个品种的日线状态与持仓均保持原状，'
        f'没有触发开仓、平仓或止损。</div>')

# ---------- 持仓表 ----------
if holds:
    hrows = ""
    for o in holds:
        p = o["pos"]
        rc, rt = (UP, "多") if p["side"] > 0 else (DN, "空")
        u = p["upnl_r"]
        hrows += (
            f'<tr><td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px"><b>{o["c"]}</b></td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;color:{rc}">{rt}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(p["entry"])}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(o["px"])}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
            f'color:{UP if u > 0 else DN};font-weight:600">{u:+.2f}R</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(p["stop"])}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
            f'color:{"#ef6c00" if p["dist_stop"] < 3 else MU}">{p["dist_stop"]:.2f}%</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{p["held"]}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
            f'color:{"#6a1b9a" if p["left"] <= 10 else MU}">{p["left"]}</td>'
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:center">'
            # 状态格单独拼：不能在 f-string 的表达式里写转义引号，
            # Python 3.11 及更早会直接报 SyntaxError（PEP 701 到 3.12 才允许）
            + (f'<b style="color:{DN}">已翻转</b>' if p["flipped"] else "持有")
            + '</td></tr>')
    hold_block = (
        f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">当前持仓（{len(holds)} 个）</h2>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<tr style="background:#f5f5f5">'
        + "".join(f'<th style="padding:7px 9px;text-align:right;font-size:12px;color:{MU};font-weight:500">{h}</th>'
                  for h in ["品种", "方向", "入场", "现价", "浮盈", "止损", "距止损", "已持(4H)", "剩兜底", "状态"])
        + f'</tr>{hrows}</table>'
        f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
        f'「剩余兜底」= 距满 {120} 根 4H 强制平仓还剩多少根。到 0 就按收盘价平。</div>')
else:
    hold_block = (f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">当前持仓</h2>'
                  f'<div style="font-size:13px;color:{MU};padding:10px 0">全部空仓。</div>')

# ---------- 全表 ----------
rows = ""
for o in COINS:
    st = o["state"]
    g = o["gate"]
    if o["pos"]:
        act, ac = "持有", MU
        trig, tcol = "—", MU
    else:
        act = o["action"]
        ac = {"开仓": UP, "闸门拦截": "#ef6c00", "空仓": MU}.get(act, MU)
        # need 是「还要走多少百分比」，乘上方向才看得出是涨还是跌：
        # 做多 want=+1 → +5.77%（要涨）；做空 want=-1 → −2.42%（要跌）
        if o.get("need") is not None:
            v = o["need"] * o.get("want", 1)
            trig = f"{v:+.2f}%"
            tcol = UP if v > 0 else DN
        else:
            trig, tcol = "—", MU
    gtxt = f"{g*100:.0f}%" if g is not None else "—"
    gcol = "#ef6c00" if o["gate_blocked"] else (TX if g is not None and g > 0.7 else MU)
    rows += (
        f'<tr><td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px"><b>{o["c"]}</b>'
        f'<span style="color:{MU};font-size:11px"> {o["sector"]}</span></td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;color:{state_color(st)};'
        f'font-weight:600;text-align:center">{st}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(o["px"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">'
        f'{pct(o["atrp"]*100) if o["atrp"] else "—"}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{gcol}">{gtxt}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">{px(o["hi20"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">{px(o["lo20"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
        f'color:{tcol};font-weight:600">{trig}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:center;'
        f'color:{ac};font-weight:{"600" if act in ("开仓", "闸门拦截") else "400"}">{act}</td></tr>')

all_block = (
    f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">全部 10 个品种</h2>'
    f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
    f'<tr style="background:#f5f5f5">'
    + "".join(f'<th style="padding:7px 9px;text-align:right;font-size:12px;color:{MU};font-weight:500">{h}</th>'
              for h in ["品种", "日线状态", "现价", "ATR%", "波动分位", "20根上轨", "20根下轨", "距触发", "动作"])
    + f'</tr>{rows}</table>'
    f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
    f'「距触发」：空仓时还要走多少才突破 20 根极值（正数=需要朝该方向再走这么多）。'
    f'「波动分位」> 85% 会被闸门拦截，不开新仓。</div>')

# ---------- 顶部 KPI ----------
nearest = min((o for o in COINS if not o["pos"] and o.get("need") is not None),
              key=lambda o: abs(o["need"]), default=None)
kpis = [("持仓", f"{len(holds)} / {len(COINS)}", TX),
        ("需动作", f"{len(urgent)}", UP if urgent else TX),
        ("闸门拦截", f"{len(blocked)}", "#ef6c00" if blocked else TX),
        ("最接近触发", f'{nearest["c"]} {abs(nearest["need"]):.2f}%' if nearest else "—", TX)]
kpi_html = "".join(
    f'<td style="padding:10px 12px;border:1px solid {BD};border-radius:6px;width:25%">'
    f'<div style="font-size:19px;font-weight:600;color:{c}">{v}</div>'
    f'<div style="font-size:11px;color:{MU};margin-top:2px">{k}</div></td>' for k, v, c in kpis)

err_block = ""
if errs:
    err_block = (f'<div style="border:1px solid #ffcc80;background:#fff8e1;border-radius:8px;'
                 f'padding:10px 12px;margin:14px 0;font-size:12.5px;color:#e65100">'
                 f'<b>取数失败：</b>{"; ".join(e["c"] + " " + e["err"] for e in errs)}'
                 f'<br>这些品种本轮没有信号，不代表它没有信号。</div>')

html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>加密骨架信号 {d['asof_bj']}</title></head>
<body style="margin:0;padding:0;background:#f0f2f5">
<div style="max-width:940px;margin:0 auto;padding:20px 16px 40px;background:#fff;
font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:{TX}">

<div style="font-size:12px;color:{MU};letter-spacing:.5px">日线 G 状态机 + 4H 突破 · 只出信号，不下单</div>
<h1 style="font-size:20px;margin:6px 0 4px;font-weight:600">加密骨架信号 · {d['asof_bj']}（北京）</h1>
<div style="font-size:12.5px;color:{MU};line-height:1.8;margin-bottom:16px">
数据截止：4H 收盘 <b>{d['bar4h']} UTC</b>（未收盘的 K 线已剔除）　生成于 {d['asof']}<br>
品种：BTC / ETH / SOL / BNB / XRP / <b>HYPE</b> / LINK / CRV / UNI / AAVE（HYPE 替换 ADA）
</div>

<table style="width:100%;border-collapse:separate;border-spacing:8px 0;margin:0 -8px 4px">{kpi_html}</table>
{err_block}
{action_block}
{hold_block}
{all_block}

<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">规则速查</h2>
<div style="font-size:12.5px;color:#444;line-height:1.9;background:#fafafa;border:1px solid {BD};
border-radius:8px;padding:12px 14px">
<b>开仓</b>：日线 UP 且 4H 收盘 &gt; 前 20 根最高 → 做多；日线 DOWN 且 4H 收盘 &lt; 前 20 根最低 → 做空。
日线 RANGE 不做空，只等向上突破。<b>波动分位 &gt; 85% 时一律不开新仓。</b><br>
<b>平仓</b>：① 日线状态翻转　② 价格触及 1.5×ATR(14) 止损　③ 持有满 120 根 4H 强制平仓。<br>
<b>仓位</b>：底仓 1.0 走现货，突破加仓 +0.5 走永续，总名义 1.5×。本页只给方向，不含下单。<br>
<b>HYPE 特别说明</b>：2024-12 才上线、只有约两年数据且全在牛市里，日线状态机没经过熊市检验，
信号可以看，权重别按老币给。
</div>

<div style="font-size:11.5px;color:{MU};margin-top:22px;line-height:1.8;border-top:1px solid {BD};padding-top:12px">
本页由 live_signals.py 自动生成，纯信号、不含任何下单动作。信号基于公开 K 线，
若数据源异常或交易所停机，信号可能失真。实盘请自行核对交易所价格。
</div>
</div></body></html>"""

open(OUT, "w", encoding="utf-8").write(html)
print(f"已写入 {OUT}（{len(html)/1024:.0f} KB）  需动作 {len(urgent)} 个、持仓 {len(holds)} 个")
