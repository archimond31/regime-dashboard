#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行工单台账 —— 把「邮件提醒」变成「可结算的工单」。

为什么邮件本身不够：
  live_signals 每根 4H 说一次「该做什么」，说完就忘。下一根 4H 的内容已经变了，
  于是「上一封里那条还没做的动作」在邮件里彻底消失。手动执行最大的风险不是漏看，
  是**看到了、以为做了、其实没做**，而系统每一轮都重启记忆，永远发现不了。

这个文件做的事：把每一轮的动作沉淀成一张带状态和年龄的工单，直到你显式结算。

  建单  —— sync 时发现新动作 → 开一张 open 工单（同一状态段同一动作只开一张）
  结算  —— done（已执行）/ drop（明确放弃）
  作废  —— 行情自己走掉了（价格回落、日线翻回来），系统置 void 并说明原因
  老化  —— 每过一根 4H 就 age_bars +1，超过 TTL 标「已过期」

TTL 的取值依据是 _diag_delay.py 的实测：开仓信号每延迟 4 小时，全样本总 R 掉 7.8%，
24 小时掉 33.8%。所以开仓只给 6 根（24h）的窗口，过期后必须重新判断；
清/建底仓是日线级别的动作，且「卖底仓后价格接近随机」（20/42/120 根 4H 后中位
-0.2%/-0.4%/-0.0%），没有硬时效，给 24 根只作为「是不是忘了」的提醒。

用法：
    python ticket.py sync            # 按 _signal.json 同步（run_signal.py 自动调）
    python ticket.py list            # 看当前未结算工单
    python ticket.py done 3 80255.5  # 标记 3 号已执行，记下成交价
    python ticket.py done all        # 全部标记已执行
    python ticket.py drop 3          # 明确放弃 3 号
"""
import datetime as dt
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_config as PC

TICKETS = os.environ.get("TICKET_LEDGER", "_tickets.json")   # 可指到测试台账
SIG = "_signal.json"

# 必须人工的动作 → TTL（根 4H）。不在表里的动作（持有/空仓/闸门拦截）不开单。
TTL = {
    "开仓": 6,        # 24h，超过就明显在削收益（-33.8% 总 R 在 24h 处）
    "平仓": 8,        # 32h，反手要等下一根 4H 确认，但拖久了浮盈回吐
    "止损临近": 8,
    "即将到期": 6,
    "清底仓": 24,     # 日线级，无硬时效，只防「忘了」
    "建底仓": 24,
}
LAB = {"开仓": "开仓", "平仓": "平仓", "止损临近": "止损临近",
       "即将到期": "即将到期", "清底仓": "清底仓（现货卖出）",
       "建底仓": "建底仓（现货买入）"}
COLOR = {"开仓": "#c62828", "平仓": "#2e7d32", "止损临近": "#ef6c00",
         "即将到期": "#6a1b9a", "清底仓": "#d84315", "建底仓": "#1565c0"}
BAR_FMT = "%Y-%m-%d %H:%M"
ATR_K = 1.5      # 与 bt_skeleton.ATR_K 一致：止损 = 入场 ∓ 1.5×ATR(14)


# ---------------- 台账 ----------------
def load():
    if not os.path.exists(TICKETS):
        return {"next_id": 1, "items": []}
    try:
        d = json.load(open(TICKETS, encoding="utf-8"))
        d.setdefault("next_id", 1)
        d.setdefault("items", [])
        return d
    except Exception:
        return {"next_id": 1, "items": []}


def save(d):
    with open(TICKETS, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def _bars_between(a, b):
    """两根 4H 之间隔了多少根（a 早、b 晚）。解析失败返回 0。"""
    try:
        t0 = dt.datetime.strptime(a[:16], BAR_FMT)
        t1 = dt.datetime.strptime(b[:16], BAR_FMT)
        return max(0, int((t1 - t0).total_seconds() // (4 * 3600)))
    except Exception:
        return 0


# ---------------- 建单 ----------------
def _notional(o):
    """这张工单要动的名义金额（U）。

    不能一律用 target：日线翻转时 target 被强制归零，反而拿不到「要平多少」。
    平仓类回到持仓口径推：UP 里的仓是 base+add=1.5×，DOWN 里的空头只有 add=0.5×。
    """
    w = PC.WEIGHTS.get(o["c"], 0.0)
    a = o["action"]
    if a in ("清底仓", "建底仓"):
        return round(PC.CAPITAL * w * PC.BASE_RATIO)
    if a in ("平仓", "止损临近", "即将到期"):
        p = o.get("pos") or {}
        k = (PC.BASE_RATIO + PC.ADD_RATIO) if p.get("regime") == "UP" else PC.ADD_RATIO
        return round(PC.CAPITAL * w * k)
    return abs(o.get("target") or 0)


def _side(o):
    """+1 买 / -1 卖。清底仓、平多 = 卖；建底仓、平空 = 买。"""
    a = o["action"]
    if a == "清底仓":
        return -1
    if a == "建底仓":
        return 1
    if a in ("平仓", "止损临近", "即将到期"):
        return -1 if (o.get("pos") or {}).get("side", 1) > 0 else 1
    return 1 if (o.get("target") or 0) > 0 else -1


def _stop(o, side):
    """开仓才给新止损；其余动作是把已有仓位处理掉，止损已经在场。"""
    if o["action"] != "开仓":
        p = o.get("pos") or {}
        return p.get("stop")
    atr = o.get("atr")
    px = o.get("px")
    if not atr or not px:
        return None
    return round(px - side * ATR_K * atr, 6)


def new_item(o, bar):
    side = _side(o)
    return {
        "id": None,
        "key": f'{o["c"]}|{o["action"]}|{o.get("regime_start") or bar}',
        "coin": o["c"],
        "kind": o["action"],
        "side": side,
        "notional": _notional(o),
        "ref_px": o.get("px"),
        "stop": _stop(o, side),
        "state": o.get("state"),
        "regime_start": o.get("regime_start"),
        "bar": bar,                      # 建单时的那根 4H（UTC）
        "created": o.get("bar"),
        "age_bars": 0,
        "slices": o.get("slices") or 1,
        "cap_flag": o.get("cap_flag") or "",
        "note": (o.get("note") or "")[:120],
        "status": "open",
        "settled_at": None,
        "fill_px": None,
        "void_note": None,
    }


def sync(sig=None):
    """按 _signal.json 同步一次：开新单、作废失效单、刷新年龄。"""
    if sig is None:
        sig = json.load(open(SIG, encoding="utf-8"))
    bar = sig.get("bar4h")
    d = load()
    items = d["items"]

    state_of = {o["c"]: o.get("state") for o in sig["coins"]}
    live = {}
    for o in sig["coins"]:
        if o.get("action") not in TTL:
            continue
        k = f'{o["c"]}|{o["action"]}|{o.get("regime_start") or bar}'
        live[k] = o

    # 建底仓不能只靠「动作出现」，要按**状态**推：日线在 UP/RANGE 就该有现货底仓。
    # 只按事件的话会漏 —— _base_clear.json 提示过一次就不再报，等你哪天打开页面
    # 才发现「日线 UP 两个月了，底仓一直是空的」。清底仓反过来只能按事件，
    # 因为 DOWN 状态下底仓本来就该是 0，从状态里推不出「还没清」。
    for o in sig["coins"]:
        if o.get("pos") is not None or o.get("triggered"):
            continue                       # 有 4H 仓 / 已触发 → 走开仓单，不单列底仓
        if o.get("state") not in ("UP", "RANGE"):
            continue
        k = f'{o["c"]}|建底仓|{o.get("regime_start") or bar}'
        if k not in live:
            live[k] = dict(o, action="建底仓",
                           note=f'日线 {o.get("regime_start") or bar} 起为 {o["state"]}，'
                                f'底仓应在场（4H 突破后再加到峰值）')

    opened = 0
    opened_ids = []
    for k, o in live.items():
        if any(i["key"] == k for i in items):
            continue                      # 开过就不再开（无论 done / drop / void）
        it = new_item(o, bar)
        it["id"] = d["next_id"]
        d["next_id"] += 1
        items.append(it)
        opened += 1
        opened_ids.append(it["id"])

    # ---- 作废：三种情形分开判，不能都靠「动作从 urgent 里消失」----
    # 清/建底仓被 _base_clear.json 去过重，提示一次后就不会再出现，
    # 若按「消失即作废」处理，会把你还没执行的清仓单当天作废掉。
    voided = 0
    for i in items:
        if i["status"] != "open":
            continue
        note = None
        if i["kind"] in ("清底仓", "建底仓"):
            st = state_of.get(i["coin"])
            if st is None:
                continue                  # 这一轮没取到该币，先不动
            if i["kind"] == "清底仓" and st != "DOWN":
                note = f"日线已回到 {st}，不必再清底仓"
            elif i["kind"] == "建底仓" and st == "DOWN":
                note = "日线又转 DOWN，不必建底仓"
        else:
            if i["key"] not in live:
                note = "行情已变化，这条动作不再需要执行"
        if note:
            i["status"] = "void"
            i["void_note"] = note
            i["settled_at"] = dt.datetime.now(dt.timezone.utc).strftime(BAR_FMT) + " UTC"
            voided += 1

    for i in items:
        if i["status"] == "open":
            i["age_bars"] = _bars_between(i["bar"], bar)

    # 留一份「这一轮发生了什么」，notify_mail 靠它决定主题该用「新动作」还是「催办」
    d["last"] = {"bar": bar, "opened": opened_ids, "voided": voided,
                 "open": sum(1 for i in items if i["status"] == "open")}
    save(d)
    return {"opened": opened, "voided": voided, "open": d["last"]["open"]}


# ---------------- 结算 ----------------
def _open_items(d):
    return [i for i in d["items"] if i["status"] == "open"]


def mark(ids, status, fill_px=None):
    d = load()
    hit = 0
    for i in d["items"]:
        if i["status"] != "open":
            continue
        if ids != "all" and i["id"] not in ids:
            continue
        i["status"] = status
        i["settled_at"] = dt.datetime.now(dt.timezone.utc).strftime(BAR_FMT) + " UTC"
        if fill_px is not None:
            i["fill_px"] = fill_px
        hit += 1
        print(f"  #{i['id']} {i['coin']:<5} {LAB.get(i['kind'], i['kind']):<12} → {status}")
    if not hit:
        print("  没有匹配的未结算工单。")
    save(d)
    return hit


def expired(i):
    t = TTL.get(i["kind"])
    return bool(t and i["age_bars"] > t)


def render():
    """给邮件正文用的工单区 HTML（内联样式 + table，和 gen_signal_html 同规矩）。"""
    d = load()
    op = sorted(_open_items(d), key=lambda i: (-expired(i), i["id"]))
    if not op:
        return ('<div style="border:1px solid #c8e6c9;background:#f1f8f4;border-radius:8px;'
                'padding:12px 14px;margin:18px 0;font-size:13px;color:#1b5e20">'
                '<b>没有待执行工单。</b>上一批动作都已结算。</div>')

    rows = ""
    for i in op:
        c = COLOR.get(i["kind"], "#1a1a1a")
        ex = expired(i)
        bg = "#fff3e0" if ex else "#fff"
        age = ("刚触发" if i["age_bars"] == 0 else
               (f"挂 {i['age_bars'] // 6} 天 {i['age_bars'] % 6 * 4}h" if i["age_bars"] >= 6
                else f"挂 {i['age_bars'] * 4}h"))
        age_col = "#d32f2f" if ex else "#757575"
        sgn = "买入" if i["side"] > 0 else "卖出"
        px = i["ref_px"]
        pxs = (f"{px:,.4f}" if px is not None and abs(px) < 10
               else (f"{px:,.2f}" if px is not None else "—"))
        stp = i["stop"]
        stps = "—" if stp is None else (
            f"{stp:,.4f}" if abs(stp) < 10 else f"{stp:,.2f}")
        # note 里可能有 "> 85%" 这种字符，裸放进 HTML 会把标签截断
        note = (f'<div style="font-size:11px;color:#757575;margin-top:3px;line-height:1.5">'
                f'{html.escape(i["note"])}</div>' if i.get("note") else "")
        rows += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;color:#9e9e9e">#{i["id"]}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:14px"><b>{i["coin"]}</b></td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;color:{c};font-weight:600">'
            f'{sgn} {i["notional"]:,.0f} U</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px">'
            f'<span style="color:{c}">{html.escape(LAB.get(i["kind"], i["kind"]))}</span>{note}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;text-align:right;'
            f'white-space:nowrap">{pxs}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;text-align:right;'
            f'color:#757575;white-space:nowrap">{stps}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:12px;text-align:right;'
            f'color:{age_col};white-space:nowrap">{age}{"<br>已过期" if ex else ""}</td></tr>')

    cmd = ""
    for i in op[:4]:
        cmd += (f"python fill_log.py add {i['coin']} {i['side']:+d} {i['notional']} "
                f"{i['ref_px']} &lt;成交均价&gt;\n")
    ex_n = sum(1 for i in op if expired(i))
    heads = ["#", "币", "动作 / 名义", "类型", "参考价", "止损", "挂起"]
    hali = ["left", "left", "left", "left", "right", "right", "right"]
    head = (f'<div style="border:2px solid #c62828;border-radius:8px;margin:18px 0;background:#fff5f5">'
            f'<div style="padding:10px 12px;background:#c62828;color:#fff;font-size:13px;'
            f'font-weight:600;border-radius:6px 6px 0 0">'
            f'待执行工单 {len(op)} 张'
            + (f'（其中 {ex_n} 张已过期）' if ex_n else '') + '</div>'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<tr style="background:#fafafa">'
            + "".join(f'<th style="padding:6px 8px;text-align:{al};font-size:11px;'
                      f'color:#757575;font-weight:500">{h}</th>'
                      for h, al in zip(heads, hali))
            + f'</tr>{rows}</table>'
            f'<div style="padding:10px 12px;font-size:12px;color:#5d4037;line-height:1.9;'
            f'border-top:1px solid #f0d9d9">'
            f'执行完标记：<code style="background:#fff;padding:1px 5px;border:1px solid #e0e0e0">'
            f'python ticket.py done &lt;编号&gt; &lt;成交均价&gt;</code>　'
            f'放弃：<code style="background:#fff;padding:1px 5px;border:1px solid #e0e0e0">'
            f'drop &lt;编号&gt;</code><br>'
            f'「参考价」是信号那根 4H 的收盘价，不是限价 —— 隔久了照这个价挂不上。</div></div>')
    return head


def main():
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "sync":
        r = sync()
        print(f"开新单 {r['opened']} 张、作废 {r['voided']} 张，当前未结算 {r['open']} 张")
        return 0
    if cmd == "list":
        d = load()
        op = _open_items(d)
        if not op:
            print("没有未结算工单。")
        for i in op:
            ex = " [已过期]" if expired(i) else ""
            print(f"  #{i['id']} {i['coin']:<5} {LAB.get(i['kind'], i['kind']):<14}"
                  f"{'买' if i['side'] > 0 else '卖'} {i['notional']:>7,.0f} U"
                  f"  参考 {i['ref_px']}  挂 {i['age_bars']} 根 4H{ex}")
        done = [i for i in d["items"] if i["status"] != "open"]
        if done:
            print(f"\n已结算 {len(done)} 张"
                  f"（done {sum(1 for i in done if i['status'] == 'done')} /"
                  f" drop {sum(1 for i in done if i['status'] == 'drop')} /"
                  f" void {sum(1 for i in done if i['status'] == 'void')}）")
        return 0
    if cmd in ("done", "drop"):
        if not rest:
            print(f"用法：{cmd} <编号|all> [成交价]")
            return 1
        if rest[0] == "all":
            ids = "all"
            fp = float(rest[1]) if len(rest) > 1 else None
        else:
            ids = {int(x) for x in rest[0].split(",")}
            fp = float(rest[1]) if len(rest) > 1 else None
        mark(ids, "done" if cmd == "done" else "drop", fp)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
