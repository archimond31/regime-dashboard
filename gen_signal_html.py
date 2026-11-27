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
import datetime as _dt

import ticket as TK      # 工单台账：把「本轮动作」升级成「跨轮可结算的待办」
# 这个文件底部有个变量叫作 html（整页 HTML），所以模块不能 `import html` —— 会被遮住。
from html import escape as _esc

SRC = "_signal.json"
OUT = "signal_now.html"

d = json.load(open(SRC, encoding="utf-8"))
COINS = d["coins"]
# 观察名单与组合必须**分开渲染**（2026-11-27 加的 SNX/PENDLE/RENDER/ONDO）。
# 混在同一张表里的后果是「权重 0 / 底仓 0 / 峰值 0」被读成「规则要求持有 0」，
# 而事实是「不参与组合」—— 同一个数字，两种完全不同的含义。
PORT_ROWS = [o for o in COINS if not o.get("watch")]
WATCH_ROWS = [o for o in COINS if o.get("watch")]
# 底仓闸门默认关闭（去前视后不如不开，见 base_gate.py 文件头）。
# 关着的时候不该在表里摆一列「100%」—— 那会让人以为它在工作。
BG_ON = bool((d.get("basegate") or {}).get("on"))
BG_MA = (d.get("basegate") or {}).get("ma", 50)
# 模式从信号文件读，不 import portfolio_config —— 页面要和「生成这份信号时的
# 配置」一致，而不是和「此刻的代码」一致（两者可能因为重新部署而不同步）。
SHORT_ON = bool(d.get("allow_short", True))
EXEC_SHORT = bool(d.get("execute_short", True))
# 策略书记录了空头、但账户从没建过的那几个（11-09 入场，系统 11-24 才上线）。
# 与"不做空"是两件事：这几个只影响「平掉一个不存在的仓」，不影响新开空信号。
UNOPENED = d.get("unopened") or []
MODE_TXT = d.get("mode") or ("合约（含做空）" if SHORT_ON else "纯现货（不做空）")
if SHORT_ON and EXEC_SHORT:
    MODE_SHORT = "含做空·执行"
elif SHORT_ON:
    MODE_SHORT = "含做空·纸面"
else:
    MODE_SHORT = "不做空"
# 「策略能不能做空」和「这个账户做不做空」是两件事，速查里必须写清是哪一层，
# 否则看的人会把「策略没做空信号」和「我不执行空头」混成一个判断。
if SHORT_ON and not EXEC_SHORT:
    SHORT_RULE = ("日线 DOWN 且 4H 收盘 &lt; 前 20 根最低 → 做空"
                  "<b>（只记策略书，本账户不下单）</b>。日线 RANGE 不做空，只等向上突破。")
elif SHORT_ON:
    SHORT_RULE = ("日线 DOWN 且 4H 收盘 &lt; 前 20 根最低 → 做空。"
                  "日线 RANGE 不做空，只等向上突破。")
else:
    SHORT_RULE = "日线 DOWN → <b>只清现货底仓，策略本身不做空</b>。"
# 多头峰值 1.5× 里 1.0 是现货、0.5 是永续加仓；后者与做空与否无关。
PEAK_TXT = "峰值 1.5×（空头另计 0.5×）" if SHORT_ON else "峰值 1.5×（无空头）"
_CAP = d.get("capital") or 0.0
# 「本轮要动作」**取信号文件里的 urgent**，不在这里按 action 重算一遍。
# 重算会漏掉 live_signals 的两层过滤：exec（这一单要不要真下）与 settled
# （这一段的工单是不是已经执行过）。2026-11-27 LINK 那条就是：信号已标 settled，
# 页面自己一算又说「需动作 1 个」—— 用户刚买完，下一封邮件还在催。
_URG = set(d.get("urgent") or [])
urgent = [o for o in PORT_ROWS if o["c"] in _URG]
holds = [o for o in PORT_ROWS if o["pos"]]
blocked = [o for o in PORT_ROWS if o["gate_blocked"]]
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
# 原来这里是「本次需要动作」的一次性列表：每一轮重新生成，上一轮没做的动作就此消失。
# 手动执行最怕的正是这个 —— 看到了、以为做了、实际没做，而系统下一轮就不提了。
# 换成 ticket.py 维护的跨轮工单：有编号、有年龄、有 TTL，直到显式结算才离开视野。
ticket_block = TK.render()

# 本轮新触发的动作单独摘一行 —— 工单区只说「还欠哪些」，不说「刚刚多了哪些」
_new_now = (TK.load().get("last") or {}).get("bar") == d.get("bar4h")
if urgent and _new_now:
    names = "、".join(f'{o["c"]} {o["action"]}' for o in urgent)
    action_block = (
        f'<div style="border-left:4px solid {UP};background:#fff5f5;border-radius:6px;'
        f'padding:10px 12px;margin:14px 0;font-size:13px;color:{TX}">'
        f'<b>本轮新触发 {len(urgent)} 个动作：</b>{names}'
        f'　（已进下方工单，执行完记得标记）</div>')
else:
    action_block = ""

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
            f'<td style="padding:8px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU};white-space:nowrap">{p.get("entry_bj") or "—"}</td>'
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
            + (f'<b style="color:{DN}">已翻转</b>' if p["flipped"] else
               ('<span style="color:#6a1b9a" title="策略书在场，账户没建过这笔">未建仓</span>'
                if o.get("paper_why") == "no_pos" else
                ('<span style="color:#6a1b9a">纸面</span>'
                 if not o.get("exec", True) else "持有")))
            + '</td></tr>')
    _paper_n = sum(1 for o in holds if not o.get("exec", True))
    _unop_n = sum(1 for o in holds if o.get("paper_why") == "no_pos")
    if _unop_n and _unop_n == len(holds):
        _ptxt = " · 全部未建仓（策略书在场，账户没建过）"
    elif _unop_n:
        _ptxt = f" · {_unop_n} 个未建仓"
    elif _paper_n:
        _ptxt = f" · {_paper_n} 个纸面，本账户不下单"
    else:
        _ptxt = ""
    _htitle = f'当前持仓（{len(holds)} 个{_ptxt}）'
    hold_block = (
        f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">{_htitle}</h2>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<tr style="background:#f5f5f5">'
        + "".join(f'<th style="padding:7px 9px;text-align:right;font-size:12px;color:{MU};font-weight:500">{h}</th>'
                  for h in ["品种", "方向", "入场价", "入场时间", "现价", "浮盈", "止损", "距止损", "已持(4H)", "剩兜底", "状态"])
        + f'</tr>{hrows}</table>'
        f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
        f'「入场时间」为北京时间（UTC+8），即那根 4H 的收盘时刻——不是下单时刻。'
        f'「剩余兜底」= 距满 {120} 根 4H 强制平仓还剩多少根。到 0 就按收盘价平。</div>'
        + (f'<div style="font-size:12px;color:#6a1b9a;margin-top:4px;line-height:1.8">'
           f'<b>「未建仓」= 策略书里在场、账户从没建过这笔</b>：这几个空头在系统上线前'
           f'（11-09）就入场了，当时的价距现在已远，止损位置失去意义，没有追。'
           f'影响仅限「它们的平仓单不用执行」；<b>新出现的做空信号照常发单</b>，'
           f'该怎么空就怎么空。</div>' if _unop_n else '')
        + (f'<div style="font-size:12px;color:#6a1b9a;margin-top:4px;line-height:1.8">'
           f'<b>「纸面」= 策略算得出、但本账户不执行的仓位。</b></div>'
           if _paper_n and not _unop_n else ''))
else:
    hold_block = (f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">当前持仓（4H 永续层）</h2>'
                  f'<div style="font-size:13px;color:{MU};padding:10px 0">4H 层全部空仓。</div>')

# ---------- 现货底仓块 ----------
# 上半部分（当前持仓）只讲 4H 永续仓。纯现货账户里那一栏永远是空的，
# 而真实的钱全在现货底仓上 —— 不把它单独列出来，"持仓 0/12" 就是在说谎。
_SPOT = d.get("spot_held") or {}
# 金额是「按当前目标收窄推出来的」而不是成交记录：这几行必须标「推定」。
# 不标的话，一个看起来精确的假数字比一个明确标注的估计值危险得多。
_EST = set(d.get("spot_est") or [])
# ---- 永续层：台账里 done 的开仓 − 平仓 ----
# 这个块讲的是「现货底仓」，但账户里一旦有永续仓，只报现货就是在少报一半敞口。
# 2026-11-27 开出第一笔 LINK 永续（40 U）之后，「实盘现货 +0.50×」这句话
# 就再也不等于账户状态了 —— 而看的人只会读成「我的敞口是 0.50×」。
# **文本在算完 _ssum 之后才拼**（它要用到现货合计），别在这里提前拼。
_PN = TK.perp_net()
if _SPOT:
    _srows = ""
    _ssum = 0.0
    for o in COINS:
        v = _SPOT.get(o["c"])
        if not v:
            continue
        _ssum += v
        tgt = o.get("target") or 0
        diff = tgt - v
        dcol = (MU if abs(diff) < max(20, 0.1 * v) else (UP if diff > 0 else DN))
        dtxt = ("—" if abs(diff) < max(20, 0.1 * v)
                else (f"{diff:+,.0f} 待{'买' if diff > 0 else '卖'}"))
        _srows += (
            f'<tr><td style="padding:7px 9px;border-bottom:1px solid {BD}"><b>{o["c"]}</b>'
            + (f'<span style="font-size:10px;color:{MU};border:1px solid {BD};'
               f'border-radius:3px;padding:0 3px;margin-left:4px" '
               f'title="台账里没有该单的实际成交额，此数按「建单建议额收窄到当前目标」推定">'
               f'推定</span>' if o["c"] in _EST else "")
            + f'</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};color:'
            f'{state_color(o["state"])}">{o["state"]}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};text-align:right">{v:,.0f} U</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};text-align:right">{tgt:,.0f} U</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};text-align:right;color:{dcol}">{dtxt}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:12px;color:{MU}">'
            f'{_esc(o.get("target_note") or "")}</td></tr>')
    # 日线 DOWN 而没有底仓的币：这是**符合规则**的状态，不是遗漏。
    # 不写这句，看到「11 个币只有 7 个有货」会以为漏买了。
    # （币数会随组合调整而变 —— 2026-11-27 去 DOGE 后是 11，别再写死这个数。）
    _flat = [o["c"] for o in PORT_ROWS if not _SPOT.get(o["c"]) and o["state"] == "DOWN"]
    _PN_TXT = (
        f'<br><b>永续层</b>（台账 done 的开仓 − 平仓）：净 <b>{_PN["net"]:+,.0f} U</b>'
        f'（{_PN["n"]} 笔，毛 {_PN["gross"]:,.0f} U）'
        f'　⇒　<b>实盘合计敞口 {_ssum + _PN["net"]:,.0f} U'
        f'（净 {(_ssum + _PN["net"]) / (_CAP or 1):+.2f}×）</b>'
        if _PN["n"] else "")
    _PN_WARN = (
        f'<br><span style="color:{MU}">上面是<b>名义</b>，不是权益：合约账户里的余额'
        f'（保证金 + 闲置）台账里没有记录，权益只能看交易所。'
        f'现货和永续用的是同一个本金池，相加不是「多出来的钱」。</span>'
        if _PN["n"] else "")
    spot_block = (
        f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">'
        f'现货底仓（{len(_SPOT)} 个 · {_ssum:,.0f} U）</h2>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<tr style="background:#f5f5f5">'
        + "".join(f'<th style="padding:7px 9px;text-align:{al};font-size:12px;color:{MU};font-weight:500">{h}</th>'
                  for h, al in [("品种", "left"), ("日线", "left"), ("持有", "right"),
                                ("该持有", "right"), ("差额", "right"), ("说明", "left")])
        + f'</tr>{_srows}</table>'
        f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
        f'「持有」由工单台账推（已结算的建底仓 − 减底仓 − 清底仓）。'
        f'现货底仓没有价格止损，退出只有两条：「日线翻 DOWN → 清底仓」'
        f'与「系数下调 → 减底仓」。'
        f'<br>带 <b>推定</b> 标记的行：台账里没有该单的实际成交额，'
        f'数字是把建单建议额收窄到当前目标推出来的 —— 它是估计，不是成交记录。'
        f'下单后执行 <code>ticket.py fill &lt;编号&gt; &lt;成交额U&gt;</code> 补录，标记即消失。'
        f'<br><b>「差额」为负 = 超配</b>：2026-11-27 起会生成<b>减底仓</b>工单'
        f'（卖出多出来的那部分，现货无止损）—— 最常见的是 UP→RANGE 且来路 DOWN，'
        f'底仓系数从 1.0 掉到 0.5。减仓单只在「目标仍低于全额基准」时有效，'
        f'状态回到全额档会自动作废。'
        + (_PN_TXT) + (_PN_WARN)
        + (f'<br><b>两个净敞口不要混用</b>：策略书 '
           f'{sum(o.get("target") or 0 for o in PORT_ROWS) / (_CAP or 1):+.2f}×'
           f'（含未建仓空头）｜实盘现货 {_ssum / (_CAP or 1):+.2f}×。'
           + ('风控看上面的<b>合计</b>（现货 + 永续），判断策略状态看前者。'
              if _PN["n"] else '风控看后者，判断策略状态看前者。')
           if (UNOPENED or not EXEC_SHORT) else '')
        + (f'<br>{"/".join(_flat)} 日线为 DOWN 且无现货底仓 —— 规则要求的持仓状态'
           f'（空头是永续层的事，与现货底仓无关）。'
           if _flat else '')
        + '</div>')
else:
    spot_block = ""

# ---------- 全表（只列组合；观察名单单独一块，见下）----------
rows = ""
for o in PORT_ROWS:
    st = o["state"]
    g = o["gate"]
    if o["pos"]:
        # 空头仓在策略书里在场、账户没建过时标「未建仓」；账户整体不做空时标「纸面」。
        # 不标出来，这个「持有」看着就像账户里真有一笔钱在动。
        if not o.get("exec", True):
            act = ("未建仓" if o.get("paper_why") == "no_pos" else "纸面")
        else:
            act = "持有"
        ac = MU
        trig, tcol = "—", MU
    else:
        act = o["action"]
        ac = {"开仓": UP, "闸门拦截": "#ef6c00", "空仓": MU,
              "建底仓": "#1565c0", "减底仓": "#e65100",
              "清底仓": "#d84315"}.get(act, MU)
        if not o.get("exec", True):
            act = f"{act}·未建仓" if o.get("paper_why") == "no_pos" else f"{act}·纸面"
        # 这一段动作的工单已经执行过了。信号本身还是「开仓」（策略书照算），
        # 但页面必须说「已执行」—— 否则用户刚下完单，这封邮件还在催同一次突破。
        # 「看到了、以为做了」的反面：「做完了、系统还说要做」。两者都消耗可信度。
        if o.get("settled"):
            act, ac = "已执行", "#2e7d32"
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
    # 波动目标化系数：>1 = 低波动加仓，<1 = 高波动减仓。1.0 表示未缩放。
    _vt = o.get("vt") or {}
    _sc = _vt.get("scale", 1.0)
    vtxt = f"{_sc:.2f}" if _vt.get("on") else "—"
    vcol = ("#c62828" if _sc > 1.05 else "#2e7d32" if _sc < 0.95 else MU)
    # 血统标注：只标「不可纠错」的那种，其余留白，避免表格噪声。
    # init 血统 = 状态机刚预热完的初始猜测，没有均线/背离纠错通道，
    # 出口只剩「RSI 破 os 再升破」；实盘在这段不据此开新仓。
    _lock = o.get("blood_locked")
    st_tail = ('<div style="font-size:10px;color:#d32f2f;margin-top:2px;line-height:1.2">'
               '⚠ 初始化<br>不可纠错</div>' if _lock else "")
    # RANGE 段的「来路」：2026-11-20 盘后起 UP→1.0 / DOWN→0.5，
    # 即来自 DOWN 的震荡期只留半底仓，这里会标出比例。
    # 保留 _rr<=0 分支作防御：将来若把 DOWN 调回 0，面板会自动恢复红标。
    _rr = o.get("range_ratio", 1.0)
    _src = o.get("range_src")
    if st == "RANGE" and _src:
        if _rr <= 0:
            st_tail += ('<div style="font-size:10px;color:#c62828;margin-top:2px;'
                        'line-height:1.2">来路 %s<br>底仓归零</div>' % _src)
        else:
            st_tail += ('<div style="font-size:10px;color:#757575;margin-top:2px;'
                        'line-height:1.2">来路 %s</div>' % _src)
    # 底仓闸门：1.00 = 全额，0.25 = 价格已跌破 MA50 只留 25%。
    # 只影响底仓，加仓层由 4H 突破决定，与它无关。关闭时整列不渲染。
    _bg = o.get("base_gate", 1.0)
    bgtxt = ("—" if st == "DOWN"
             else ("100%" if _bg >= 1.0 else f"{_bg * 100:.0f}%"))
    bgcol = "#2e7d32" if _bg < 1.0 else MU
    bgcell = (f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
              f'color:{bgcol};font-weight:{"600" if _bg < 1.0 else "400"}">{bgtxt}</td>'
              if BG_ON else "")
    rows += (
        f'<tr><td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px"><b>{o["c"]}</b>'
        f'<span style="color:{MU};font-size:11px"> {o["sector"]}</span></td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;color:{state_color(st)};'
        f'font-weight:600;text-align:center;white-space:nowrap">{st}{st_tail}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(o["px"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">'
        f'{pct(o["atrp"]*100) if o["atrp"] else "—"}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{gcol}">{gtxt}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
        f'color:{vcol};font-weight:{"600" if abs(_sc-1) > 0.05 else "400"}">{vtxt}</td>'
        + bgcell +
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">{px(o["hi20"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">{px(o["lo20"])}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
        f'color:{tcol};font-weight:600">{trig}</td>'
        f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:center;'
        f'color:{ac};font-weight:{"600" if act in ("开仓", "闸门拦截") else "400"}">{act}</td></tr>')

all_block = (
    f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">组合 {len(PORT_ROWS)} 个品种</h2>'
    f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
    f'<tr style="background:#f5f5f5">'
    + "".join(f'<th style="padding:7px 9px;text-align:right;font-size:12px;color:{MU};font-weight:500">{h}</th>'
              for h in (["品种", "日线状态", "现价", "ATR%", "波动分位", "仓位系数"]
                        + (["底仓"] if BG_ON else [])
                        + ["20根上轨", "20根下轨", "距触发", "动作"]))
    + f'</tr>{rows}</table>'
    f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
    f'「距触发」：空仓时还要走多少才突破 20 根极值（正数=需要朝该方向再走这么多）。'
    f'「波动分位」> 85% 会被闸门拦截，不开新仓。<br>'
    f'<b>「仓位系数」</b>：波动目标化（按 1/σ 分配风险）。'
    f'<span style="color:#c62828">红 = 加仓</span>（该币波动低，同样的钱承担的风险更小）、'
    f'<span style="color:#2e7d32">绿 = 减仓</span>（波动高）。'
    f'系数已含 κ，平均总敞口与不加时相同；只改金额、不改方向'
    f'{"，空头同样被缩" if SHORT_ON else ""}。<br>'
    + ((f'<b>「底仓」</b>：底仓闸门。价格在 MA{BG_MA} 上方 = 100%，跌破 = 25%，'
        f'中间滞回带防抖动。只作用于底仓，加仓层由 4H 突破决定、不受影响。'
        f'DOWN 时底仓本来就是 0，显示「—」。<br>')
       if BG_ON else
       ('<b>「底仓」</b>：底仓闸门已关闭（默认）。它是一条连续跟随规则，'
        '晚一根 4H 执行就吃掉全部收益，所以不上线 —— 详见 base_gate.py 文件头。<br>'))
    + f'<b>「⚠ 初始化」</b>：这段状态来自状态机刚预热完的初始均线判断，'
    f'既不受 MA60 纠错、也不受「背离失败」约束，只有「RSI 破 30 再升破」一个出口——'
    f'上涨行情中该出口永不触发，会长时间锁死。此段不开新仓，已有持仓照常按翻转平。</div>')

# ---------- 观察名单块（2026-11-27）----------
# 只跟状态，不持仓。**为什么不并进上面那张表**：那张表每列（底仓 / 权重 / 峰值）
# 对它们都是 0，而 0 在这里的含义是「不参与组合」，不是「规则要求持有 0」。
# 同一个数字两种含义塞进一张表，必然被读错 —— 而读错的后果是「为什么系统让我买 0 U」。
watch_block = ""
if WATCH_ROWS:
    _wrows = ""
    for o in WATCH_ROWS:
        st = o["state"]
        if o.get("need") is not None:
            # 与组合表同口径：need 是「还要走多少百分比」，乘方向才看得出涨还是跌
            _v = o["need"] * o.get("want", 1)
            trig, tcol = f"{_v:+.2f}%", (UP if _v > 0 else DN)
        else:
            trig, tcol = "—", MU
        _g = o["gate"]
        gtxt = f"{_g * 100:.0f}%" if _g is not None else "—"
        gcol = ("#ef6c00" if o["gate_blocked"]
                else (TX if _g is not None and _g > 0.7 else MU))
        _bl = o.get("blood_label") or "—"
        blcol = "#d32f2f" if o.get("blood_locked") else MU
        # 有模拟仓的（策略书里在场、账户没有）必须写出来：不然「动作=观察」会被
        # 读成「没有信号」。它确实有信号，只是我们不动 —— 这两件事不一样。
        _p = o.get("pos")
        _note = o.get("note") or ""
        if _p:
            _note = (f"策略书 {'空' if _p.get("side", 1) < 0 else '多'}头在场 "
                     f"{_p.get('held', 0)} 根 4H，浮盈 {_p.get('upnl_r', 0):+.2f}R"
                     f"（模拟，不执行）")
        _wrows += (
            f'<tr><td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px">'
            f'<b>{o["c"]}</b><span style="color:{MU};font-size:11px"> {o["sector"]}</span></td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;'
            f'color:{state_color(st)};font-weight:600;text-align:center">{st}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:12px;color:{blcol}">{_bl}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right">{px(o["px"])}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;color:{MU}">'
            f'{pct(o["atrp"] * 100) if o["atrp"] else "—"}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
            f'color:{gcol}">{gtxt}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:13px;text-align:right;'
            f'color:{tcol};font-weight:600">{trig}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid {BD};font-size:12px;color:{MU}">'
            f'{_esc(_note)}</td></tr>')
    watch_block = (
        f'<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">'
        f'观察名单（{len(WATCH_ROWS)} 个 · 只跟信号，不持仓）</h2>'
        f'<table style="width:100%;border-collapse:collapse;font-size:13px">'
        f'<tr style="background:#f5f5f5">'
        + "".join(f'<th style="padding:7px 9px;text-align:right;font-size:12px;color:{MU};'
                  f'font-weight:500">{h}</th>'
                  for h in ("品种", "日线状态", "状态血统", "现价", "ATR%",
                            "波动分位", "距触发", "说明"))
        + f'</tr>{_wrows}</table>'
        f'<div style="font-size:12px;color:{MU};margin-top:8px;line-height:1.8">'
        f'<b>这 {len(WATCH_ROWS)} 个币不占本金、不产生工单</b>：状态与触发方向照算照发，'
        f'但权重为 0（`portfolio_config.WATCH_ONLY`，不在 `WEIGHTS_SMALL` 里），'
        f'所以没有底仓、没有加仓层、也不会出现在任何一封「执行」邮件里。'
        f'想升级成持仓：从 `WATCH_ONLY` 删掉 → 加进 `WEIGHTS_SMALL`（Σw 会变）→ '
        f'三个口径重算，不能只加权重不复算。<br>'
        f'数据：4H 走 Binance 现货归档（与组合同源）。RENDER 2024-07 由 RNDR 改名，'
        f'历史已按 1:1 置换拼回前身 RNDR（2021-11 起）；不拼会从改名那天才开始，'
        f'等于丢掉 2022 整段 —— 而那种「少了一段」不会报错，只会让状态看起来更新。</div>')


# ---------- 顶部 KPI ----------
nearest = min((o for o in PORT_ROWS if not o["pos"] and o.get("need") is not None),
              key=lambda o: abs(o["need"]), default=None)
# KPI 里的「待执行」用跨轮工单数，不用本轮的 urgent ——
# 本轮的 urgent 每 4 小时清零，读它会让人以为「没有欠账」。
_open_n = sum(1 for i in TK.load()["items"] if i["status"] == "open")
# 本金放在第一张卡：工单金额都是它的派生量，不摆在最前面，
# 「这页按多少钱算的」就只能靠金额反推（5000→2000 时就吃过这个亏）。
_cap = d.get("capital") or 0
kpis = [("本金", f"{_cap:,.0f} U", TX),
        # 模式放在第二张：工单、峰值、净敞口全都是它的派生量。
        # 写「含做空·纸面」而不是「纯现货」——策略仍算空头，只是不发单，
        # 这两件事对「该期待什么收益」的影响完全不同。
        ("模式", MODE_SHORT, "#1565c0"),
        ("4H 仓", f"{len(holds)} / {len(PORT_ROWS)}", TX),
        ("待执行工单", f"{_open_n} 张", UP if _open_n else TX),
        ("闸门拦截", f"{len(blocked)}", "#ef6c00" if blocked else TX),
        ("最接近触发", f'{nearest["c"]} {abs(nearest["need"]):.2f}%' if nearest else "—", TX)]
_kw = 100 // len(kpis)
kpi_html = "".join(
    f'<td style="padding:10px 12px;border:1px solid {BD};border-radius:6px;width:{_kw}%">'
    f'<div style="font-size:19px;font-weight:600;color:{c}">{v}</div>'
    f'<div style="font-size:11px;color:{MU};margin-top:2px">{k}</div></td>' for k, v, c in kpis)

err_block = ""
if errs:
    err_block = (f'<div style="border:1px solid #ffcc80;background:#fff8e1;border-radius:8px;'
                 f'padding:10px 12px;margin:14px 0;font-size:12.5px;color:#e65100">'
                 f'<b>取数失败：</b>{"; ".join(e["c"] + " " + e["err"] for e in errs)}'
                 f'<br>这些品种本轮没有信号，不代表它没有信号。</div>')

# 各币最新已收盘 4H 不一定同步：Binance 走按日归档，当天文件要到次日才发布，
# 于是它比 Hyperliquid 的实时接口慢 —— 慢多少取决于跑的时刻：
#   UTC 00~02 点跑（北京 08~10 点），前一天的日归档往往还没发布 → 直接落后一整天然（6 根）
#   其余时段 → 落后 1 根
# 这个差值必须显式说出来，否则「动作」列会被读成最新的判断，而它其实是昨天的。
_bars = sorted({o["bar"] for o in COINS if o.get("bar")})
_coinline = " / ".join(o["c"] for o in COINS)      # 跟着 COINS 自动走，别再手写死品种表
_lag = ""
stale_block = ""
if len(_bars) > 1:
    _f = "%Y-%m-%d %H:%M"
    _gap = int(((_dt.datetime.strptime(_bars[-1], _f)
                 - _dt.datetime.strptime(_bars[0], _f)).total_seconds()) // (4 * 3600))
    _old = [o["c"] for o in COINS if o.get("bar") == _bars[0]]
    _lag = (f'；其中 {len(_old)} 个品种停在 <b>{_bars[0]}</b>'
            f'（落后 {_gap} 根 4H，因 Binance 按日归档要到次日才发布）')
    if _gap >= 2:
        # 落后两根以上就不是「稍慢」，是「用昨天的数据在做今天的判断」，必须阻断阅读
        stale_block = (
            f'<div style="border:1px solid #ff8a65;background:#fbe9e7;border-radius:8px;'
            f'padding:10px 12px;margin:14px 0;font-size:12.5px;color:#bf360c;line-height:1.7">'
            f'<b>数据不同步：{len(_old)} 个品种落后 {_gap} 根 4H（{_gap * 4} 小时）</b><br>'
            f'{" ".join(_old)} 这一轮用的是 <b>{_bars[0]}</b> 的收盘数据，'
            f'其余品种已到 {_bars[-1]}。<b>这几个品种的「动作」列不代表最新状态</b> —— '
            f'请等下一班（Binance 日归档发布后）再看。</div>')

html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>加密骨架信号 {d['asof_bj']}</title></head>
<body style="margin:0;padding:0;background:#f0f2f5">
<div style="max-width:940px;margin:0 auto;padding:20px 16px 40px;background:#fff;
font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:{TX}">

<div style="font-size:12px;color:{MU};letter-spacing:.5px">日线 G 状态机 + 4H 突破 · 只出信号，不下单</div>
<h1 style="font-size:20px;margin:6px 0 4px;font-weight:600">加密骨架信号 · {d['asof_bj']}（北京）</h1>
<div style="font-size:12.5px;color:{MU};line-height:1.8;margin-bottom:16px">
数据截止：4H 收盘 <b>{d['bar4h']} UTC</b>（未收盘的 K 线已剔除{_lag}）　生成于 {d['asof']}<br>
品种：{_coinline}
</div>

<table style="width:100%;border-collapse:separate;border-spacing:8px 0;margin:0 -8px 4px">{kpi_html}</table>
{err_block}
{stale_block}
{action_block}
{ticket_block}
{hold_block}
{spot_block}
{all_block}
{watch_block}

<h2 style="font-size:15px;margin:26px 0 10px;color:{TX}">规则速查</h2>
<div style="font-size:12.5px;color:#444;line-height:1.9;background:#fafafa;border:1px solid {BD};
border-radius:8px;padding:12px 14px">
<b>开仓</b>：日线 UP 且 4H 收盘 &gt; 前 20 根最高 → 做多。{SHORT_RULE}<b>波动分位 &gt; 85% 时一律不开新仓。</b><br>
<b>平仓</b>：① 日线状态翻转　② 价格触及 1.5×ATR(14) 止损　③ 持有满 120 根 4H 强制平仓。<br>
<b>仓位</b>：底仓 1.0 走现货，突破加仓 +0.5 走永续，总名义 {PEAK_TXT}。本页只给方向，不含下单。<br>
<b>RANGE 来路</b>：RANGE 段的底仓按「进入前那段是什么」打折 —— 上行转来保持 100%（段内平均 +8.78%），
下行转来归零（段内平均 +0.50%，零期望的纯波动）。状态格会标「来路 UP/DOWN」。<br>
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
