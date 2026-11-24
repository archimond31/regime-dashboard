#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdings_watch.py —— 现货持仓维护监控（只出建议，绝不下单）

它做什么：
  把 holdings.json 里的每个币，套上**与本策略同一套日线 G 状态机**，
  给出「该不该继续持有」的判断，并把账户层级的回本路径算清楚。

它不做什么：
  · 不算「目标仓位金额」—— 你的持仓是固定数量、成本价很高，
    与策略的「按本金百分比建仓」不是一回事，硬套会得出荒谬结论。
  · 不预测价格。所有判断只有两个来源：① 状态机方向 ② 成本价这个历史事实。

三类建议：
  HOLD   持有   —— 日线 UP，或 RANGE（底仓按来路：UP→1.0 / DOWN→0.5）
  TRIM   减配   —— 日线 DOWN 且血统可纠错（方向可信）
  WATCH  观察   —— 日线 DOWN 但血统是 init（不可纠错，可能误判）

外加一个跨状态标签（与趋势无关，纯数学）：
  SWITCH 换仓候选 —— 回本需涨 > 200% 且市值占比 < 5%
  依据：这类仓位需要涨 3 倍以上才回本，等它回本等于把资金锁死在低概率事件上。

用法：
  python holdings_watch.py            # 出 holdings_now.json + holdings_now.html
  python holdings_watch.py --mail     # 再发一封邮件
"""
import json
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_binance_vision import load_bars
from strategy_1h_rsi import regime_and_blood
import portfolio_config as PC

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, "holdings.json")
OUTJ = os.path.join(HERE, "holdings_now.json")
OUTH = os.path.join(HERE, "holdings_now.html")

BLOOD_LABEL = {
    "init_ma120": "初始化·不可纠错",
    "divergence_bottom": "底背离",
    "divergence_top": "顶背离",
    "ma60_cross_up": "MA60上穿",
    "ma60_cross_down": "MA60下穿",
    "rsi_retrigger_up": "RSI再触发↑",
    "rsi_retrigger_down": "RSI再触发↓",
    "rsi_overbought_exit": "超买退出",
    "rsi_oversold_exit": "超卖退出",
    "cross_fix_up": "MA60纠错↑",
    "cross_fix_down": "MA60纠错↓",
    "div_fail_top": "顶背离失败",
    "div_fail_bottom": "底背离失败",
}


def load_daily(c):
    """取日线收盘序列（只保留已收盘的），RENDER 需要把 RNDR 的历史拼上。"""
    def _get(name):
        b = load_bars(name, "1d")
        return [{"date": x["date"][:10], "close": x["close"]} for x in b]

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if c == "RENDER":
        old = _get("RNDR")      # RNDRUSDT：2020-05 ~ 2024-07-22
        new = _get("RENDER")    # RENDERUSDT：2024-07-23 ~
        seen = {x["date"] for x in old}
        merged = old + [x for x in new if x["date"] not in seen]
    else:
        merged = _get(c)
    merged.sort(key=lambda x: x["date"])
    # 剔除未收盘的那根：UTC 当天 00:00 的日线是「前一天」的收盘，
    # 但 load_bars 的 tail 会把当天的进行中 bar 也带进来，必须过滤。
    return [x for x in merged if x["date"] < today]


def analyze_one(h):
    c, qty, cost = h["c"], h["qty"], h["cost"]
    d1 = load_daily(c)
    if len(d1) < 130:
        return dict(c=c, error=f"日线只有 {len(d1)} 根，不足 121 根")
    reg, blood = regime_and_blood(d1)
    dates = [x["date"] for x in d1]
    # 从后往前找「当前这段状态」的起点，而不是最后一根 bar：
    # 先定位最后一个有状态的 bar 拿到当前状态，再往回扫到状态发生变化为止。
    # （2026-11-19 修：原版直接 break 在最后一根，导致所有币 state_since 都是昨天）
    cur_i = None
    for i in range(len(d1) - 1, -1, -1):
        if reg.get(dates[i]):
            cur_i = i
            break
    if cur_i is None:
        return dict(c=c, error="状态机未产出任何状态")
    state = reg[dates[cur_i]]
    st_i = cur_i
    while st_i - 1 >= 0 and reg.get(dates[st_i - 1]) == state:
        st_i -= 1
    blood_v, st_date = blood.get(dates[cur_i]), dates[st_i]

    px = d1[-1]["close"]
    mv = qty * px
    tc = qty * cost
    pnl = mv - tc
    need = (-pnl / mv) if mv > 0 else 0.0     # 回本还需涨多少（相对当前市值）

    # ---- 建议 ----
    if state == "UP":
        rec, why = "HOLD", "日线 UP，顺趋势持有"
    elif state == "RANGE":
        # 与 portfolio_config.RANGE_RATIO_BY_SRC 同源：UP→RANGE 1.0 / DOWN→RANGE 0.0（当前）。
        # 两侧口径必须一致，否则会出现「策略让你清仓、面板让你满仓」的自相矛盾。
        prev, j = None, st_i - 1
        while j >= 0 and reg.get(dates[j]) is None:   # 跳过空洞日
            j -= 1
        if j >= 0:
            prev = reg.get(dates[j])
        size = PC.RANGE_RATIO_BY_SRC.get(prev, PC.RANGE_RATIO_DEFAULT)
        if size <= 0:
            # 系数已经是 0（DOWN→RANGE 当前就是 0）→ 策略要的是清空，不能说 HOLD。
            # 2026-11-20 把 DOWN 从 0.5 改回 0.0 时才发现这里会自相矛盾。
            rec = "TRIM"
            why = (f"日线 RANGE（由 {prev} 转入），按策略清空底仓（系数 0）"
                   if prev else "日线 RANGE，按策略清空底仓（系数 0）")
        else:
            rec = "HOLD"
            why = (f"日线 RANGE（由 {prev} 转入），按策略持有 {size * 100:.0f}% 底仓"
                   if prev else
                   f"日线 RANGE，按策略持有 {size * 100:.0f}% 底仓")
    else:  # DOWN
        if blood_v == "init_ma120":
            rec, why = "WATCH", "日线 DOWN 但血统为初始化（不可纠错，可能误判），先观察"
        elif blood_v in ("divergence_top", "divergence_bottom"):
            rec, why = "TRIM", "日线 DOWN（背离血统），逆势信号已确认，建议减配"
        else:
            rec, why = "TRIM", "日线 DOWN（MA60 血统，方向可信），建议减配"

    return dict(c=c, qty=qty, cost=cost, px=px, mv=mv, total_cost=tc, pnl=pnl,
                pnl_pct=pnl / tc if tc else 0, need=need,
                state=state, blood=blood_v,
                blood_label=BLOOD_LABEL.get(blood_v, blood_v or "—"),
                state_since=st_date, rec=rec, why=why,
                bars=len(d1), first=dates[0])


def main():
    with open(CFG, encoding="utf-8") as f:
        cfg = json.load(f)
    rows = [analyze_one(h) for h in cfg["holdings"]]

    ok = [r for r in rows if "error" not in r]
    MV = sum(r["mv"] for r in ok)
    TC = sum(r["total_cost"] for r in ok)
    PNL = MV - TC
    NEED = (-PNL / MV) if MV > 0 else 0.0

    for r in ok:
        r["w"] = r["mv"] / MV if MV else 0
        r["loss_share"] = (-r["pnl"] / -PNL) if PNL < 0 else 0
        # 换仓候选：仓位已不重要（<5%）+ 回本要翻倍以上 → 留着只是占位
        r["switch"] = bool(r["need"] > 1.0 and r["w"] < 0.05)

    rows.sort(key=lambda r: -(r.get("mv") or 0))

    # 结构分组
    core = [r for r in ok if r["w"] >= 0.05]
    tail = [r for r in ok if r["w"] < 0.05]
    out = dict(
        ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        as_of=cfg.get("as_of"),
        market_value=MV, total_cost=TC, pnl=PNL,
        pnl_pct=PNL / TC if TC else 0, need=NEED,
        core_mv=sum(r["mv"] for r in core),
        core_loss=sum(r["pnl"] for r in core),
        tail_mv=sum(r["mv"] for r in tail),
        tail_loss=sum(r["pnl"] for r in tail),
        core_need_min=min((r["need"] for r in core), default=0),
        core_need_max=max((r["need"] for r in core), default=0),
        tail_need_min=min((r["need"] for r in tail), default=0),
        tail_need_max=max((r["need"] for r in tail), default=0),
        core_names="/".join(r["c"] for r in sorted(core, key=lambda x: -x["mv"])),
        tail_names="/".join(r["c"] for r in sorted(tail, key=lambda x: -x["mv"])),
        n_hold=sum(1 for r in ok if r["rec"] == "HOLD"),
        n_trim=sum(1 for r in ok if r["rec"] == "TRIM"),
        n_watch=sum(1 for r in ok if r["rec"] == "WATCH"),
        n_switch=sum(1 for r in ok if r["switch"]),
        rows=rows,
    )
    with open(OUTJ, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # ---------- 终端输出 ----------
    print("=" * 96)
    print(f"现货持仓监控　{out['ts']}　（快照 {out['as_of']}）")
    print("=" * 96)
    print(f"  市值 {MV:,.0f} U　成本 {TC:,.0f} U　浮亏 {PNL:,.0f} U"
          f"（{out['pnl_pct']*100:.2f}%）　账户回本需涨 {NEED*100:.1f}%")
    print(f"  核心（占比≥5%）市值 {out['core_mv']:,.0f} U / 亏损 {out['core_loss']:,.0f} U")
    print(f"  尾部（占比<5%） 市值 {out['tail_mv']:,.0f} U / 亏损 {out['tail_loss']:,.0f} U")
    print(f"  建议分布：持有 {out['n_hold']}　减配 {out['n_trim']}　"
          f"观察 {out['n_watch']}　换仓候选 {out['n_switch']}")
    print(f"  核心（≥5%）亏损 {abs(out['core_loss']):,.0f} U 占 {abs(out['core_loss'])/abs(PNL)*100:.1f}%"
          f"　｜　尾部（<5%）亏损 {abs(out['tail_loss']):,.0f} U 占"
          f" {abs(out['tail_loss'])/abs(PNL)*100:.1f}%"
          f"（仅占市值 {out['tail_mv']/MV*100:.1f}%）")
    print()
    print(f"{'币':<8}{'状态':<8}{'血统':<16}{'市值':>11}{'浮亏':>10}"
          f"{'亏损率':>9}{'回本需涨':>10}{'占比':>8}  建议")
    print("-" * 96)
    for r in rows:
        if "error" in r:
            print(f"{r['c']:<8}ERROR {r['error']}")
            continue
        tag = r["rec"] + ("+换仓" if r["switch"] else "")
        print(f"{r['c']:<8}{r['state']:<8}{r['blood_label'][:14]:<16}"
              f"{r['mv']:>11,.0f}{r['pnl']:>10,.0f}"
              f"{r['pnl_pct']*100:>8.1f}%{r['need']*100:>9.0f}%"
              f"{r['w']*100:>7.1f}%  {tag}")
    print("-" * 96)
    print("  回本需涨 = 浮亏 / 当前市值。这个数越大，等它回本越接近买彩票。")

    render_html(out)
    print(f"\n  已生成 {OUTH}")

    if "--mail" in sys.argv:
        send_mail(out)


def render_html(o):
    def cls(r):
        return {"HOLD": "hold", "TRIM": "trim", "WATCH": "watch"}[r["rec"]]

    body = []
    for r in o["rows"]:
        if "error" in r:
            body.append(f"<tr><td>{r['c']}</td><td colspan='8' class='err'>{r['error']}</td></tr>")
            continue
        tag = {"HOLD": "持有", "TRIM": "减配", "WATCH": "观察"}[r["rec"]]
        sw = " <span class='sw'>换仓</span>" if r["switch"] else ""
        needc = "hot" if r["need"] > 1.0 else ""
        body.append(
            f"<tr>"
            f"<td class='coin'>{r['c']}</td>"
            f"<td><span class='st {cls(r)}'>{r['state']}</span>"
            f"<div class='why'>{r['state_since']} 起</div></td>"
            f"<td class='dim'>{r['blood_label']}</td>"
            f"<td class='num'>{r['mv']:,.0f}</td>"
            f"<td class='num neg'>{r['pnl']:,.0f}</td>"
            f"<td class='num neg'>{r['pnl_pct']*100:.1f}%</td>"
            f"<td class='num {needc}'>{r['need']*100:.0f}%</td>"
            f"<td class='num dim'>{r['w']*100:.1f}%</td>"
            f"<td><b>{tag}</b>{sw}<div class='why'>{r['why']}</div></td>"
            f"</tr>")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>现货持仓监控 {o['ts']}</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f4f6f8;font-family:"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
color:#1c2026;font-size:14px;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 20px 50px}}
h1{{font-size:21px;margin:0 0 4px;font-weight:600}}
.sub{{color:#6d7683;font-size:12.5px;margin-bottom:20px}}
.kpi{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:8px}}
.card{{background:#fff;border:1px solid #e3e7ec;border-radius:8px;padding:14px 16px}}
.card .k{{font-size:11.5px;color:#77808c;letter-spacing:.3px;margin-bottom:6px}}
.card .v{{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}}
.card .s{{font-size:11.5px;color:#8b949f;margin-top:4px}}
.neg{{color:#c0392b}} .pos{{color:#1e8449}}
.path{{background:#fff;border:1px solid #e3e7ec;border-radius:8px;padding:16px 18px;margin:14px 0 20px}}
.phead{{font-size:14px;font-weight:600;margin-bottom:3px}}
.psub{{font-size:12.5px;color:#6d7683;margin-bottom:14px}}
.pcols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.pc{{border:1px solid #e8ecf1;border-radius:6px;padding:12px 14px;background:#fafbfc}}
.pc.good{{border-left:3px solid #2f9e5f}}
.pc.bad{{border-left:3px solid #c0392b}}
.ph{{font-size:13px;font-weight:600;margin-bottom:6px}}
.pv{{font-weight:400;font-size:11.5px;color:#8b949f;margin-left:6px}}
.pn{{font-size:11.5px;color:#77808c;margin-bottom:8px;word-break:break-all;line-height:1.5}}
.pk{{font-size:12.5px;margin-bottom:6px}}
.pc.good .pk b{{color:#1e8449}} .pc.bad .pk b{{color:#c0392b}}
.ps{{font-size:12px;color:#5a636e;line-height:1.65}}
.pfoot{{margin-top:14px;padding-top:12px;border-top:1px solid #eef1f5;
font-size:12.5px;color:#4a525c;line-height:1.75}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e7ec;
border-radius:8px;overflow:hidden;font-size:13px}}
th{{background:#f7f9fb;text-align:left;padding:10px 12px;font-weight:600;font-size:12px;
color:#5a636e;border-bottom:1px solid #e3e7ec;white-space:nowrap}}
td{{padding:10px 12px;border-bottom:1px solid #f0f3f6;vertical-align:top}}
tr:last-child td{{border-bottom:none}}
.coin{{font-weight:600}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.dim{{color:#8b949f;font-size:12.5px}}
.hot{{color:#c0392b;font-weight:600}}
.st{{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;font-weight:600}}
.st.hold{{background:#e8f5ec;color:#1e8449}}
.st.trim{{background:#fdecea;color:#c0392b}}
.st.watch{{background:#fdf5e3;color:#a1781f}}
.sw{{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:4px;
background:#eef1f5;color:#5a636e;font-size:11px;font-weight:500}}
.why{{color:#8b949f;font-size:11.5px;margin-top:1px}}
.err{{color:#c0392b}}
.legend{{margin-top:16px;font-size:12px;color:#77808c;line-height:1.8}}
.foot{{margin-top:26px;padding-top:14px;border-top:1px solid #e3e7ec;
font-size:11.5px;color:#98a1ac}}
</style></head><body><div class="wrap">
<h1>现货持仓监控</h1>
<div class="sub">生成 {o['ts']}　·　持仓快照 {o['as_of']}　·　方向来自日线 G 状态机，与交易策略同源</div>

<div class="kpi">
  <div class="card"><div class="k">当前市值</div><div class="v">{o['market_value']:,.0f} U</div>
    <div class="s">成本 {o['total_cost']:,.0f} U</div></div>
  <div class="card"><div class="k">浮动盈亏</div><div class="v neg">{o['pnl']:,.0f} U</div>
    <div class="s neg">{o['pnl_pct']*100:.1f}%</div></div>
  <div class="card"><div class="k">账户回本需涨</div><div class="v">{o['need']*100:.1f}%</div>
    <div class="s">全部持仓同时回到成本</div></div>
  <div class="card"><div class="k">核心仓（≥5%）</div><div class="v">{o['core_mv']:,.0f}</div>
    <div class="s">亏损 {abs(o['core_loss']):,.0f} U · 占亏损 {abs(o['core_loss'])/abs(o['pnl'])*100:.0f}%</div></div>
  <div class="card"><div class="k">尾部仓（&lt;5%）</div><div class="v">{o['tail_mv']:,.0f}</div>
    <div class="s">亏损 {abs(o['tail_loss']):,.0f} U · 占亏损 {abs(o['tail_loss'])/abs(o['pnl'])*100:.0f}%</div></div>
</div>

<div class="path">
  <div class="phead">回本路径拆解</div>
  <div class="psub">账户回本需要整体上涨 <b>{o['need']*100:.1f}%</b>。但这不是一个数字，是两件性质完全不同的事。</div>
  <div class="pcols">
    <div class="pc good">
      <div class="ph">核心 {len([r for r in o['rows'] if r.get('w',1)>=0.05])} 币
        <span class="pv">{o['core_mv']:,.0f} U · 市值 {o['core_mv']/o['market_value']*100:.1f}%</span></div>
      <div class="pn">{o['core_names']}</div>
      <div class="pk">需涨 <b>{o['core_need_min']*100:.0f}% ~ {o['core_need_max']*100:.0f}%</b></div>
      <div class="ps">承担 {abs(o['core_loss'])/abs(o['pnl'])*100:.1f}% 的亏损。这个幅度在加密牛市里一年内够得着
        —— <b>这部分有戏</b>。</div>
    </div>
    <div class="pc bad">
      <div class="ph">尾部 {len([r for r in o['rows'] if r.get('w',1)<0.05])} 币
        <span class="pv">{o['tail_mv']:,.0f} U · 市值 {o['tail_mv']/o['market_value']*100:.1f}%</span></div>
      <div class="pn">{o['tail_names']}</div>
      <div class="pk">需涨 <b>{o['tail_need_min']*100:.0f}% ~ {o['tail_need_max']*100:.0f}%</b></div>
      <div class="ps">承担 {abs(o['tail_loss'])/abs(o['pnl'])*100:.1f}% 的亏损。需要 2~14 倍
        —— <b>这不是时间问题，是概率问题</b>。</div>
    </div>
  </div>
  <div class="pfoot">
    <b>所以答案是分两半的：</b>核心 4 币全部回到成本，亏损从 {abs(o['pnl']):,.0f} U 降到
    {abs(o['tail_loss']):,.0f} U —— <b>只回一半</b>。剩下的一半，靠等是等不到的；
    但把尾部 {o['tail_mv']:,.0f} U 挪到有趋势的标的上、赚一倍，也只能补回
    {o['tail_mv']/abs(o['pnl'])*100:.1f}%。<br>
    真实路径只有一条：<b>核心 4 币跟着趋势走（AAVE 已在 UP），尾部换成趋势标的，
    然后把新赚的钱交给策略的复利，而不是交给等待。</b>
  </div>
</div>

<table>
<thead><tr>
<th>币</th><th>状态</th><th>血统</th><th style="text-align:right">市值 U</th>
<th style="text-align:right">浮亏 U</th><th style="text-align:right">亏损率</th>
<th style="text-align:right">回本需涨</th><th style="text-align:right">占比</th><th>建议</th>
</tr></thead>
<tbody>
{''.join(body)}
</tbody></table>

<div class="legend">
  <b>持有</b> 日线 UP，或日线 RANGE（底仓按来路：UP→满仓 / DOWN→半仓）　·　
  <b>减配</b> 日线 DOWN 且血统可纠错（方向可信）　·　
  <b>观察</b> 日线 DOWN 但血统是「初始化」（不可纠错，可能误判）<br>
  <b>换仓</b> 回本需涨 &gt;100% 且市值占比 &lt;5% —— 纯数学判据，与趋势无关<br>
  <span class="dim">状态日期为日线 bar 的开盘时刻（UTC），与实盘面板的 4H 口径相差 1 天，是同一天的状态切换。</span>
</div>

<div class="foot">
本页只给判断依据，不下单、不预测价格。状态机参数为 MA120/MA60 + RSI(14) 70/30 + 两日确认，
与主策略完全一致；持仓数量与成本来自 holdings.json，请自行核对。
</div>
</div></body></html>"""
    with open(OUTH, "w", encoding="utf-8") as f:
        f.write(html)


def send_mail(o):
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.header import Header
    cfg_path = os.path.join(HERE, "_mail.json")
    if not os.path.exists(cfg_path):
        print("  未找到 _mail.json，跳过邮件")
        return
    with open(cfg_path, encoding="utf-8") as f:
        m = json.load(f)
    with open(OUTH, encoding="utf-8") as f:
        html = f.read()
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(
        f"持仓监控｜市值 {o['market_value']:,.0f} U　浮亏 {o['pnl']:,.0f} U　"
        f"回本需涨 {o['need']*100:.0f}%", "utf-8")
    msg["From"] = m["user"]
    msg["To"] = ",".join(m["to"])
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(m["host"], m["port"], context=ctx) as s:
        s.login(m["user"], m["pass"])
        s.sendmail(m["user"], m["to"], msg.as_string())
    print(f"  邮件已发至 {', '.join(m['to'])}")


if __name__ == "__main__":
    main()
