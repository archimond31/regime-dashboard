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
    python ticket.py fill 7 136      # 补录实际成交额（不改状态，只补事实）
    python ticket.py adopt BTC 199 空  # 账户里已有这笔仓、台账里没有 → 补进台账
    python ticket.py stats           # 实测「建单到结算」的延迟分布 + 折算 R 损失
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
# 当前模式，印在邮件资金条上 —— 「这封邮件按什么口径算的」是会变的，
# 不写出来只能靠工单里有没有「做空」去猜。
# 三种状态要分开：策略能做空 / 策略能做空但账户不执行 / 策略本身不做空。
MODE = ("合约（含做空·执行）" if (PC.ALLOW_SHORT and PC.EXECUTE_SHORT) else
        ("策略含做空 · 执行纯现货" if PC.ALLOW_SHORT else "纯现货（策略不做空）"))


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
def _opened_amount(coin):
    """该币最近一张**已执行**「开仓」单的金额。平仓按它回推，不用今天的权重重算。"""
    try:
        d = load()
    except Exception:
        return None
    for i in reversed(d.get("items", [])):
        if i.get("coin") == coin and i.get("kind") == "开仓" and i.get("status") == "done":
            amt = i.get("fill_notional")
            amt = abs(float(amt)) if amt is not None else abs(float(i.get("notional") or 0))
            if amt > 0:
                return amt
    return None


def _notional(o):
    """这张工单要动的名义金额（U）。**这是「这一单要交易多少」，不是「这个币该有多少」。**

    2026-11-27 订正（用户指出有合约账户后才发现这条必须改）：
      底仓 1.0× 走**现货**，加仓层 0.5× 走**永续**（portfolio_config: ADD_RATIO 注释
      「突破后追加（多头叠加 / 空头唯一来源）」）。所以永续这条腿在多头里**只承担
      加仓层**，而 `target` 是「峰值 = 底仓 + 加仓层」。
      旧写法直接拿 target 当开仓金额 → 把底仓在永续上又买了一遍：
        LINK 82 U 已在现货，开仓单还写 122 U → 执行后 204 U = 本金 10.2%（2.5× 权重），
        而策略峰值是 1.5× 权重 = 123 U。**多买 66%。**
      空头没有这个问题：DOWN 段底仓本来就是 0，`target` 就等于加仓层。

    不能一律用 target：日线翻转时 target 被强制归零，反而拿不到「要平多少」。
    """
    w = PC.WEIGHTS.get(o["c"], 0.0)
    a = o["action"]
    if a == "建底仓":
        # 买多少 = 这一单的差额（order_amount，live_signals 用「目标 − 已持有」算好）；
        # 没有 order_amount 时（手上本来是空的）才回落到 target 全额。
        # 旧写法 CAPITAL*w*BASE_RATIO 三样都不含：闸门把底仓砍到 25% 时会虚报 4 倍。
        # 显式判 >0 而不是用 `or`：RANGE 来路 DOWN 时 target 恰好是 0，
        # `or` 会把它吞掉、回落到 base_held（全额），变成反向的错误信号。
        oa = o.get("order_amount")
        if oa is not None and oa > 0:
            return round(oa)
        t = o.get("target")
        if t is not None and t > 0:
            return round(t)
        return round(o.get("base_held") or PC.CAPITAL * w * PC.BASE_RATIO)
    if a == "清底仓":
        # 卖多少 = **手上真实持有**的底仓。信号里的 base_held 已按上一轮的闸门打过折，
        # 不能拿全额去平 —— 闸门在仓时那是超卖。
        return round(o.get("base_held") or PC.CAPITAL * w * PC.BASE_RATIO)
    if a == "开仓":
        t = o.get("target") or 0
        if t > 0:
            # 多头：峰值 − 底仓 = 加仓层。底仓在现货，另有建底仓单，不能重复买。
            base = float(o.get("base_held") or 0)
            return round(max(0.0, abs(t) - base))
        return round(abs(t))          # 空头：整个仓就是加仓层，不减
    if a in ("平仓", "止损临近", "即将到期"):
        # 平的是**永续那条腿**，所以口径是加仓层，不是 1.5×。
        # 旧写法对多头用 (BASE+ADD)=1.5× → LINK 算出 120 U，而实际只开了 41 U，
        # 照它平会在永续上卖成 79 U 的净空头。
        # 优先用「当时实际开了多少」，那是唯一不会漂的口径（VT 系数每天都在变）；
        # 没执行记录时才回落到今天的权重重算。
        n = _opened_amount(o["c"])
        if n:
            return round(n)
        sc = (o.get("vt") or {}).get("scale") or 1.0
        return round(PC.CAPITAL * w * PC.ADD_RATIO * sc)
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
    """这一单要挂的价格止损；返回 None 表示**没有价格止损**。

    三类的口径不一样，不要混：
      · 开仓      → 新算 1.5×ATR(14)（= 入场 ∓ k×ATR，与 bt_skeleton.ATR_K 同源）
      · 平仓类    → 沿用在场那笔的止损（本来就在场，不重新给）
      · 底仓类    → **None**。建底仓是现货买入，策略里底仓没有价格止损，
                   唯一退出通道是「日线状态翻 DOWN → 清底仓」（见 portfolio_config.target）。
                   这不是漏算 —— 但 None 在页面上和「缺 ATR 算不出来」长得一样，
                   所以 render() 里给「建底仓」单独渲染成「无」，不用「—」。
    """
    if o["action"] == "开仓":
        atr = o.get("atr")
        px = o.get("px")
        if not atr or not px:
            return None
        return round(px - side * ATR_K * atr, 6)
    # 平仓 / 止损临近 / 即将到期 → 沿用在场仓位的止损
    # 建底仓 / 清底仓 → pos 为 None，自然返回 None（现货无止损）
    p = o.get("pos") or {}
    return p.get("stop")


def new_item(o, bar):
    side = _side(o)
    return {
        "id": None,
        "key": f'{o["c"]}|{o["action"]}|{o.get("regime_start") or bar}',
        "coin": o["c"],
        "kind": o["action"],
        "side": side,
        "notional": _notional(o),
        # 建单时的本金。notional 是快照、永不更新，改本金后就无法从金额反推
        # 「这单是按多少钱生成的」—— 存下原值，邮件里才能说清差在哪。
        "cap": PC.CAPITAL,
        "ref_px": o.get("px"),
        "stop": _stop(o, side),
        "state": o.get("state"),
        # 在场仓位的方向（+1 多 / -1 空），只用来判断「平仓类单是不是在平空」。
        # 单看 side 不行：平多也是卖。
        "pos_side": (o.get("pos") or {}).get("side", 0),
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
        # 实际成交金额（U）。**与 notional 不是一回事**：notional 是建单那一刻的
        # 建议金额，会随本金口径（5000→2000）和波动率目标化漂移；人真正下单的
        # 金额可能不等于它（常见情形：按新本金口径折算后买入）。
        # 没有这个字段时，系统只能靠 min(notional, 当前目标) 去**猜**真实持仓，
        # 猜对了页面就对、猜错了就系统性偏差 —— 2026-11-26 AAVE 那次两种解释
        # （167 还是 71）算出来的敞口差 36 个百分点。金额必须记事实，不能靠推断。
        "fill_notional": None,
        "void_note": None,
        # 「这单通知过没有」必须记在单子上，不能记在全局的 last.opened 里 ——
        # 那个字段每次 sync 都被覆盖，一旦某次没发出去（当天额度已用、SMTP 失败、
        # 进程崩溃），这条提醒就永久丢了，而单子还挂在列表里没人知道。
        "notified": False,
    }


def _held_from_ledger(items):
    """手上真实持有的现货金额（U），**不做任何收敛**。

    金额优先取 `fill_notional`（实际成交额），没有才退回 `notional`（建单建议额）。
    这里不拿「当前目标」去收窄 —— 收窄属于显示层的推定，不属于决策层的账本；
    决策层一旦收窄，就再也分不清「我有这么多」和「我该有这么多」。

    2026-11-26 的教训（第一版结论是错的，这里是修正后的版本）：
    AAVE 建单建议 167 U（5000 本金口径），人实际按 2000 口径买了 71 U。
    我最初把 `done` 当成「按面值成交」，据此算出全表超配 727 U、真实敞口 87% —— 
    **那是错的**。账面 1,004 U 才是对的。
    正确的修法不是「去掉收窄让数字变大」，而是**把成交额记下来**（fill_notional）。
    没有事实字段时用推定、并且明确标记，是可接受的；用推定冒充事实不行。
    """
    held = {}
    for i in items:
        if i.get("kind") not in ("建底仓", "清底仓") or i.get("status") != "done":
            continue
        # 优先用实际成交额；没有则退回建单时的建议额。
        amt = i.get("fill_notional")
        amt = abs(float(amt)) if amt is not None else abs(i.get("notional") or 0)
        sgn = 1 if i["kind"] == "建底仓" else -1
        held[i["coin"]] = held.get(i["coin"], 0.0) + sgn * amt
    return {k: v for k, v in held.items() if v > 0}


def sync(sig=None):
    """按 _signal.json 同步一次：开新单、作废失效单、刷新年龄。"""
    if sig is None:
        sig = json.load(open(SIG, encoding="utf-8"))
    bar = sig.get("bar4h")
    d = load()
    items = d["items"]

    state_of = {o["c"]: o.get("state") for o in sig["coins"]}
    # 目标名义（含空头为负）。作废判断现在按 target 走而不是按状态 ——
    # 因为 2026-11-19 起 RANGE 段也可能要求「底仓归零」（来路 DOWN），
    # 只看 state != DOWN 会把这类清仓单当天误作废掉。
    target_of = {o["c"]: (o.get("target") or 0) for o in sig["coins"]}
    live = {}
    for o in sig["coins"]:
        if o.get("action") not in TTL:
            continue
        # 策略算到 ≠ 账户要执行。空头在现货账户里只进策略书，不开工单 ——
        # 判据用信号自带的 exec 字段（由 PC.executable 生成），不在这里另判一遍。
        if not o.get("exec", True):
            continue
        k = f'{o["c"]}|{o["action"]}|{o.get("regime_start") or bar}'
        live[k] = o

    # 建底仓不能只靠「动作出现」，要按**状态**推：日线在 UP/RANGE 就该有现货底仓。
    # 只按事件的话会漏 —— _base_clear.json 提示过一次就不再报，等你哪天打开页面
    # 才发现「日线 UP 两个月了，底仓一直是空的」。清底仓反过来只能按事件，
    # 因为 DOWN 状态下底仓本来就该是 0，从状态里推不出「还没清」。
    #
    # **但这条兜底必须查持有量。** 它原来的判据是「台账里有没有这个 key」，
    # 而 key 带 regime_start —— 状态段一变就是新 key，哪怕手上早就买满了。
    # 2026-11-26 AAVE：11-22 按 5000 本金买过 167 U（id=8 done），11-26 翻 UP 后
    # 目标是 71 U，live_signals 算出差额 −96、**主动放弃发单**（action=空仓），
    # 这里却按新 key 又开了一张 71 U 的全额单（id=19）。两层各判一遍就是这个结果：
    # live_signals 说「别买」，ticket 说「买」，而工单备注里连金额都不写，
    # 人根本看不出它和自己手里的持仓是矛盾的。执行后 238 U，是峰值的 2.2 倍。
    held_raw = _held_from_ledger(items)
    for o in sig["coins"]:
        if o.get("pos") is not None or o.get("triggered"):
            continue                       # 有 4H 仓 / 已触发 → 走开仓单，不单列底仓
        if o.get("state") not in ("UP", "RANGE"):
            continue
        tgt = float(o.get("target") or 0)
        if tgt <= 0:
            continue                       # 这一段不要底仓（来路 DOWN 且系数 0）
        real = held_raw.get(o["c"], 0.0)
        if real >= tgt - PC.min_order_value(o["c"]):
            continue                       # 已持有达到目标或超配 → 不该再买
        k = f'{o["c"]}|建底仓|{o.get("regime_start") or bar}'
        if k not in live:
            delta = round(tgt - real)
            tail = (f'已持有 {round(real)} U，本单补 {delta} U' if real > 0
                    else f'底仓 {round(tgt)} U 应在场')
            live[k] = dict(o, action="建底仓", order_amount=delta,
                           note=f'日线 {o.get("regime_start") or bar} 起为 {o["state"]}，'
                                f'{tail}（4H 突破后再加到峰值）；'
                                f'退出：日线翻 DOWN 即清底仓，现货无价格止损')

    opened = 0
    opened_ids = []
    for k, o in live.items():
        prev = [i for i in items if i["key"] == k]
        if prev:
            last = prev[-1]
            # ---- 去重口径必须按「层」分开：现货一段一次，4H 层一次事件一次 ----
            # 原写法是 `if any(key) → continue`（无论 done/drop/void），把两类动作
            # 当成一件事。实际上 regime_start 只在**日线翻状态**时才变，于是一整段
            # UP 里「每类动作一辈子只能发一次」——2026-11-24/26/27 三次实测漏发：
            #   · 11-24 08:05 LINK 止损临近（距止损 1.06%）122 U → 0 新单
            #   · 11-26 00:05 NEAR 开仓·空 54 U → 0 新单
            #   · 11-27 12:05 LINK 开仓 122 U → 0 新单
            # 三次的共同点：第一张单被 void（行情变化自动作废），后续同段的新突破
            # / 新止损提醒被永久挡掉，而页面和日志照样写「需要动作 1 个」。
            #
            # 现货底仓（建/清）维持「一段一次」：同一段不该重复买同一份底仓，
            # 金额也已经由 live_signals 的「目标 − 已持有」和 _held_from_ledger 核对过。
            if o["action"] in ("建底仓", "清底仓"):
                continue
            # 4H 层（开仓/平仓/止损临近/即将到期）：只挡**已执行**和**还挂着**的。
            # void/drop = 那一次没执行成（行情变了 / 人放弃），同段里再出现新的突破
            # 就是新的一次机会，应当重新发单。
            if last["status"] in ("done", "open"):
                continue
            # 同一根 4H 不重复发：一轮里 sync 可能被调多次，避免刷屏。
            # 换了一根 bar 才允许 —— 那时是「又一次突破」，不是「同一次被重放」。
            if last.get("bar") == bar:
                continue
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
        # 现货账户里，做空性质的单直接作废（策略仍算空头，但这一单不下到账户）。
        # 不能只看 side：平多也是卖（side=-1），误杀会把「止损临近·多」清掉。
        # 所以判据用「开仓且 side<0」+ 新单里记下的 pos_side<0（平空/空头止损）。
        if not PC.EXECUTE_SHORT and i["kind"] != "清底仓" and (
                (i["kind"] == "开仓" and i.get("side", 1) < 0)
                or i.get("pos_side", 0) < 0):
            i["status"] = "void"
            i["void_note"] = "现货账户不执行空头（EXECUTE_SHORT=False），只存策略书"
            i["settled_at"] = bar
            voided += 1
            continue
        note = None
        if i["kind"] in ("清底仓", "建底仓"):
            t = target_of.get(i["coin"])
            if t is None:
                continue                  # 这一轮没取到该币，先不动
            if i["kind"] == "清底仓" and t > 0:
                note = f"当前目标 {t:,.0f} U > 0，不必再清底仓"
            elif i["kind"] == "建底仓" and t <= 0:
                note = "当前目标为 0，不必建底仓"
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


def mark(ids, status, fill_px=None, fill_notional=None):
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
        if fill_notional is not None:
            i["fill_notional"] = abs(float(fill_notional))
        hit += 1
        print(f"  #{i['id']} {i['coin']:<5} {LAB.get(i['kind'], i['kind']):<12} → {status}")
    if not hit:
        print("  没有匹配的未结算工单。")
    save(d)
    return hit


def expired(i):
    t = TTL.get(i["kind"])
    return bool(t and i["age_bars"] > t)


def unnotified(d=None):
    """还没通知过你的工单。老台账里没有这个字段 → 视为未通知，宁可多发一次。"""
    d = d or load()
    return [i for i in d["items"] if i["status"] == "open" and not i.get("notified")]


def mark_notified(ids):
    """发信成功后调用。只标记真正发出去的那几张。"""
    d = load()
    want = set(ids)
    hit = 0
    for i in d["items"]:
        if i["id"] in want and not i.get("notified"):
            i["notified"] = True
            hit += 1
    if hit:
        save(d)
    return hit


def _expect(i, CUR):
    """「这张单若按**此刻**的信号重算，金额该是多少」。

    == 为什么这个函数必须存在，而且必须复用 _notional ==
    它的用途是**发现工单金额过时**（改本金、波动目标化系数变了）。判据是
    「拿台账里的旧 notional 跟现在的口径比」，所以「现在的口径」必须与
    **建单时的口径**是同一个函数 —— 否则它比的不是「时间差」，是「两套公式」。

    2026-11-27 的实例：`_notional` 改成「永续只承担加仓层」之后，这里还留着
    自己的一套 `capital*w*(BASE+ADD)`：
      · #20 LINK 开仓真实金额 40 U → 这里算成 122 U → 差值 82 > 阈值
        → 邮件把主数字印成「买入 122 U」，40 U 缩成小字「⚠ 原单已换算」
        → **刚在 _notional 里修掉的 66% 多买，被显示层原样搬了回来**。
      · 平仓类同向错 3 倍（LINK 应平 40 U，这里算出 120 U）。
    「同一件事两处各算一遍」必然分叉 —— 这是本项目第四次踩同一条
    （前三次：SPOT 与策略书净敞口、live_signals 与 ticket 各判一遍建底仓、
    market_close_hour 收盘时钟）。分叉的代价都是**静默**的：数字不会报错。

    口径细节：**四类动作都原样交给 _notional，不做任何参数修饰**。
    特别是建底仓的 order_amount（「这一轮还差多少」）必须保留 —— 它才是
    同口径的比较对象。拿 target（「这一段该持有多少」）去比一张补仓单，
    28 U 的差额单会被显示成 282 U 的全额买入，人照着买就是超配 10 倍。
    这类错的方向性很重要：**高估建仓金额比低估危险得多**。
    """
    c = CUR.get(i["coin"])
    if not c:
        return None                     # 该币已不在信号里（比如刚被移出组合）
    return _notional(dict(c, action=i["kind"]))


def _disp_amount(i, CUR):
    """表格里显示的那个金额：返回 (金额, 换算法由 or None)。

    **「显示什么」和「复制走什么」必须是同一个数**。邮件末尾那几行
    `python fill_log.py add ...` 是直接抄去下单的 —— 表格印 40、命令带 122，
    比两个都错还糟（人会以为自己抄对了）。
    """
    ref = _expect(i, CUR)
    why = "系数/闸门已变" if i["kind"] in ("开仓", "建底仓") else "本金已变"
    # 老单没存 cap 字段；存了的就能确指「这是按 5000 生成的」
    if i.get("cap") and abs(i["cap"] - PC.CAPITAL) > 1.0:
        why = f"按 {i['cap']:,.0f} U 本金生成"
    if (ref is not None and i["notional"]
            and abs(ref - i["notional"]) > max(20.0, 0.05 * abs(i["notional"]))):
        return ref, why
    return i["notional"], None


def _cur_sig():
    """当前 _signal.json 的逐币快照 —— 用来发现「工单金额已过时」。

    两种过时：
      · 波动目标化（vol_target）每 7 天重算系数 → 建仓类金额变了；
      · **改本金**（5000→2000）→ 平仓类的金额会差整整 2.5 倍。
    不提示的话，人会照着旧数字下单。所以两个都要原文取回来。
    """
    try:
        with open(SIG, encoding="utf-8") as f:
            d = json.load(f)
        return ({o["c"]: o for o in d.get("coins", [])},
                d.get("capital") or PC.CAPITAL,
                d.get("spot_held") or {})
    except Exception:
        return {}, PC.CAPITAL, {}


def render():
    """给邮件正文用的工单区 HTML（内联样式 + table，和 gen_signal_html 同规矩）。"""
    d = load()
    op = sorted(_open_items(d), key=lambda i: (-expired(i), i["id"]))
    if not op:
        return ('<div style="border:1px solid #c8e6c9;background:#f1f8f4;border-radius:8px;'
                'padding:12px 14px;margin:18px 0;font-size:13px;color:#1b5e20">'
                '<b>没有待执行工单。</b>上一批动作都已结算。</div>')
    CUR, SIG_CAP, SPOT = _cur_sig()

    rows = ""
    buy_sum = sell_sum = 0.0
    stale_n = 0        # 有多少张单的原始金额已过时（需要换算）
    for i in op:
        c = COLOR.get(i["kind"], "#1a1a1a")
        ex = expired(i)
        bg = "#fff3e0" if ex else "#fff"
        age = ("刚触发" if i["age_bars"] == 0 else
               (f"挂 {i['age_bars'] // 6} 天 {i['age_bars'] % 6 * 4}h" if i["age_bars"] >= 6
                else f"挂 {i['age_bars'] * 4}h"))
        age_col = "#d32f2f" if ex else "#757575"
        # 过期只说明「超了」，不说明「还剩多少」。剩多久更有用 ——
        # 开仓 TTL 只有 24h，看到「剩 8h」才知道下班前得处理掉。
        ttl = TTL.get(i["kind"])
        left_h = (ttl - i["age_bars"]) * 4 if ttl else None
        if ex:
            age_sub = f'<div style="font-size:11px;color:#d32f2f;margin-top:2px">已超时 {-left_h}h</div>'
        elif left_h is not None and left_h <= 24:
            age_sub = f'<div style="font-size:11px;color:#ef6c00;margin-top:2px">剩 {left_h}h</div>'
        else:
            age_sub = ""
        sgn = "买入" if i["side"] > 0 else "卖出"
        # 「现在的口径」只能有一个来源 —— 直接问 _notional（见 _expect 的注释）。
        # 曾经这里对建仓类拿 target、对平仓类自己算 1.5×，两套公式都和 _notional 分叉过。
        disp, why = _disp_amount(i, CUR)
        drift = ""
        # 主数字用**现口径**：这封邮件是拿来下单的。主数字写 477、小字写「现口径 200」，
        # 等于让最容易被误读的那个数占主导 —— 5000→2000 那次就是这么下错的风险。
        # 台账里的 notional 仍保留建单时的原值（它是历史记录，不该被改写）。
        if why:
            drift = (f'<div style="font-size:11px;color:#ef6c00;margin-top:2px">'
                     f'⚠ 原单 {i["notional"]:,.0f} U（{why}，已换算）</div>')
            stale_n += 1
        # 汇总按**现口径**走：把 5000 时代的旧数字加起来会虚报总需求，
        # 那正好是这张表最容易被误读的地方（560U 的卖单会让人以为手上有 560U 的货）。
        eff = abs(disp)
        if i["side"] > 0:
            buy_sum += eff
        else:
            sell_sum += eff
        px = i["ref_px"]
        pxs = (f"{px:,.4f}" if px is not None and abs(px) < 10
               else (f"{px:,.2f}" if px is not None else "—"))
        stp = i["stop"]
        if stp is not None:
            stps = f"{stp:,.4f}" if abs(stp) < 10 else f"{stp:,.2f}"
        elif i["kind"] == "建底仓":
            # 现货新建仓没有价格止损（策略如此，不是漏算）。
            # 用「无」而不是「—」：两者含义不同，且单个汉字几乎不占宽 ——
            # 写成「无（现货）」会把「挂起」列挤出边框（这列是 nowrap 的）。
            stps = '<span style="color:#8d6e63">无</span>'
        else:
            stps = "—"
        # note 里可能有 "> 85%" 这种字符，裸放进 HTML 会把标签截断
        # note 是建单时拼好的，里面**嵌着旧金额**（"底仓 293 U 应在场"）。
        # 主数字已经换算成 136，note 还写 293 就是同一行里自相矛盾。
        note_txt = i.get("note") or ""
        if note_txt and disp != i["notional"]:
            note_txt = note_txt.replace(f'{i["notional"]:,.0f} U', f'{disp:,.0f} U')
        note = (f'<div style="font-size:11px;color:#757575;margin-top:3px;line-height:1.5">'
                f'{html.escape(note_txt)}</div>' if note_txt else "")
        rows += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;color:#9e9e9e">#{i["id"]}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:14px"><b>{i["coin"]}</b></td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;color:{c};font-weight:600">'
            f'{sgn} {disp:,.0f} U{drift}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px">'
            f'<span style="color:{c}">{html.escape(LAB.get(i["kind"], i["kind"]))}</span>{note}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;text-align:right;'
            f'white-space:nowrap">{pxs}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:13px;text-align:right;'
            f'color:#757575;white-space:nowrap">{stps}</td>'
            f'<td style="padding:9px 8px;border-bottom:1px solid #e0e0e0;font-size:12px;text-align:right;'
            f'color:{age_col};white-space:nowrap">{age}{age_sub}</td></tr>')

    cmd = ""
    for i in op[:4]:
        # 用 _disp_amount 而不是 i["notional"]：这行是**照抄去下单**的，
        # 必须与表格里的主数字一致（表格已换算、命令抄旧值 = 照过期数字成交）。
        _amt = _disp_amount(i, CUR)[0]
        cmd += (f"python fill_log.py add {i['coin']} {i['side']:+d} {_amt:,.0f} "
                f"{i['ref_px']} &lt;成交均价&gt;\n")
    ex_n = sum(1 for i in op if expired(i))
    # 本金条：这行以前没有，导致「邮件里到底按多少钱算的」只能从金额反推。
    # 改成 2000 之后必须显式印出来 —— 否则 5000 口径的旧工单混在里面看不出来。
    #
    # 敞口也印两个口径：**策略书的净敞口 ≠ 实盘账户的净敞口**。
    # 策略含做空时，账本净 +424 U(0.21×)，而现货账户实际 +1,004 U(0.50×)，差 2.4 倍。
    # 只印一个，看的人会拿错的那个去做风控判断。
    #
    # 2026-11-27 更正：用户**有**合约账户，永续层是能执行的。所以这里的「实盘」现在
    # 指的是**现货底仓**，不是账户总敞口 —— 一旦有「开仓」工单被 done，账户里会多出
    # 永续仓，而下面这个数不含它。要算账户总敞口，得再加上 done 的开仓·多 减去平仓。
    # （此前这里把「没有合约账户」当成既定事实，是记错了，别在这条上再推一遍。）
    gross = sum(abs(o.get("target") or 0) for o in CUR.values())
    lev = (gross / PC.CAPITAL) if PC.CAPITAL else 0.0
    net_s = sum(o.get("target") or 0 for o in CUR.values()) / PC.CAPITAL if PC.CAPITAL else 0.0
    net_r = (sum(SPOT.values()) / PC.CAPITAL) if PC.CAPITAL else 0.0
    stale_cap = (SIG_CAP and abs(SIG_CAP - PC.CAPITAL) > 1.0)
    bar = (f'<div style="padding:7px 12px;background:#fffaf5;border-bottom:1px solid #f0d9d9;'
           f'font-size:12px;color:#5d4037">'
           f'本金 <b style="font-size:13px">{PC.CAPITAL:,.0f} U</b>'
           f'　模式 <b>{html.escape(MODE)}</b><br>'
           f'策略书 净敞口 <b>{net_s:+.2f}×</b>（毛 {gross:,.0f} U / {lev:.2f}×）'
           + (f'　｜　实盘 现货 <b>{sum(SPOT.values()):,.0f} U</b>'
              f'（净 {net_r:+.2f}×）' if SPOT else '')
           + f'<br>待买 <b>{buy_sum:,.0f} U</b> / 待卖 <b>{sell_sum:,.0f} U</b>'
           + (f'　<span style="color:#ef6c00">⚠ {stale_n} 张单的原金额已过时，表中已按现口径换算</span>'
              if stale_n else '')
           + (f'　<span style="color:#7b1fa2">⚠ 信号文件本金 {SIG_CAP:,.0f} U 与本配置不一致</span>'
              if stale_cap else '')
           + '</div>')
    heads = ["#", "币", "动作 / 名义", "类型", "参考价", "止损", "挂起"]
    hali = ["left", "left", "left", "left", "right", "right", "right"]
    head = (f'<div style="border:2px solid #c62828;border-radius:8px;margin:18px 0;background:#fff5f5">'
            f'<div style="padding:10px 12px;background:#c62828;color:#fff;font-size:13px;'
            f'font-weight:600;border-radius:6px 6px 0 0">'
            f'待执行工单 {len(op)} 张'
            + (f'（其中 {ex_n} 张已过期）' if ex_n else '') + '</div>'
            + bar +
            f'<table style="width:100%;border-collapse:collapse">'
            f'<tr style="background:#fafafa">'
            + "".join(f'<th style="padding:6px 8px;text-align:{al};font-size:11px;'
                      f'color:#757575;font-weight:500;white-space:nowrap">{h}</th>'
                      for h, al in zip(heads, hali))
            + f'</tr>{rows}</table>'
            f'<div style="padding:10px 12px;font-size:12px;color:#5d4037;line-height:1.9;'
            f'border-top:1px solid #f0d9d9">'
            f'执行完标记：<code style="background:#fff;padding:1px 5px;border:1px solid #e0e0e0">'
            f'python ticket.py done &lt;编号&gt; &lt;成交均价&gt;</code>　'
            f'放弃：<code style="background:#fff;padding:1px 5px;border:1px solid #e0e0e0">'
            f'drop &lt;编号&gt;</code><br>'
            f'「参考价」是信号那根 4H 的收盘价，不是限价 —— 隔久了照这个价挂不上。<br>'
            f'<b>「止损」有三种取值，含义不同：</b>'
            f'<b>数字</b>＝1.5×ATR(14) 硬止损（只有「开仓」新给，平仓类沿用在场那笔）；'
            f'<b>无</b>＝建底仓是现货买入，策略里底仓没有价格止损，'
            f'退出只走「日线状态翻 DOWN → 清底仓」这一条；'
            f'<b>—</b>＝卖出单本来就不需要止损，或该单缺 ATR 算不出。</div></div>')
    return head


# 开仓信号「延迟 d 根 4H」的总 R 衰减（_diag_delay.py 实测，1449 笔，无偏口径）
# 注意：这张表只对**开仓**成立。平仓/底仓没做过延迟实验，不套用。
DELAY_CURVE = [(0, 0.0), (1, -7.8), (2, -12.1), (3, -21.7),
               (6, -33.8), (12, -51.3), (18, -59.3), (24, -58.3)]
KIND_GROUP = {"开仓": "开仓", "平仓": "平仓类", "止损临近": "平仓类", "即将到期": "平仓类",
              "清底仓": "底仓类", "建底仓": "底仓类"}


def _delay_cost(bars):
    """延迟该根数对应的总 R 衰减（%）。线性插值到实测曲线上。"""
    if bars <= 0:
        return 0.0
    if bars >= DELAY_CURVE[-1][0]:
        return DELAY_CURVE[-1][1]
    for (b0, c0), (b1, c1) in zip(DELAY_CURVE, DELAY_CURVE[1:]):
        if b0 <= bars <= b1:
            t = (bars - b0) / (b1 - b0)
            return c0 + t * (c1 - c0)
    return 0.0


def _parse_utc(s):
    if not s:
        return None
    try:
        return dt.datetime.strptime(s.replace("UTC", "").strip()[:16],
                                     BAR_FMT).replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _pct(xs, q):
    """线性插值分位数（样本少时比 numpy 那套更稳）。"""
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def cmd_stats():
    """实测「建单 → 结算」的延迟，并用它反推真实的调仓节奏。

    这是把「每天看三次邮件够不够」从假设变成实测的唯一办法：
    第一版设计里 −7.8%/4h 的衰减是回测算的，你实际多少小时才动手只有台账知道。
    """
    d = load()
    items = d["items"]
    if not items:
        print("台账还空着。跑两三周、攒到十几张已结算工单再看。")
        return 0

    n = {"done": 0, "drop": 0, "void": 0, "open": 0}
    for i in items:
        n[i["status"]] = n.get(i["status"], 0) + 1
    tot = len(items)
    print(f"台账共 {tot} 张：已执行 {n['done']} · 放弃 {n['drop']} · "
          f"自动作废 {n['void']} · 仍挂起 {n['open']}")
    if tot:
        print(f"  作废率 {n['void'] / tot * 100:.0f}%（行情自己走掉了，本来就不用做）")
        print(f"  放弃率 {n['drop'] / tot * 100:.0f}%（看到了但决定不做）")

    # ---- 延迟分布：只看真的执行了的 ----
    lag_h, lag_by = [], {}
    for i in items:
        if i["status"] != "done":
            continue
        t0, t1 = _parse_utc(i["bar"]), _parse_utc(i["settled_at"])
        if not t0 or not t1:
            continue
        h = max(0.0, (t1 - t0).total_seconds() / 3600)
        lag_h.append(h)
        lag_by.setdefault(KIND_GROUP.get(i["kind"], "其他"), []).append(h)

    print("\n" + "-" * 74)
    if len(lag_h) < 5:
        print(f"已执行工单只有 {len(lag_h)} 张，延迟分布还看不出形状。")
        print("先按「早 8:30 / 午 13:00 / 晚 21:00」看三次跑两三周，再回来看这张表。")
        print("-" * 74)
        _void_reasons(items)
        return 0

    print(f"建单 → 结算的实测延迟（{len(lag_h)} 张已执行，起点=信号那根 4H 收盘）")
    print("-" * 74)
    print(f"{'口径':<10}{'小时':>9}{'折成 4H 根数':>14}")
    print("-" * 74)
    for lab, q in [("中位 P50", 0.5), ("均值", None), ("P25", 0.25),
                   ("P75", 0.75), ("P90", 0.90), ("最长", 1.0)]:
        v = (sum(lag_h) / len(lag_h)) if q is None else _pct(lag_h, q)
        print(f"{lab:<10}{v:>9.1f}{v / 4:>14.1f}")

    print("\n按动作类型分开看（开仓时效最硬，底仓最松）")
    print("-" * 74)
    print(f"{'类型':<10}{'笔数':>6}{'中位h':>9}{'P75h':>9}{'最长h':>9}")
    print("-" * 74)
    for g in ("开仓", "平仓类", "底仓类", "其他"):
        xs = lag_by.get(g)
        if not xs:
            continue
        print(f"{g:<10}{len(xs):>6}{_pct(xs, 0.5):>9.1f}"
              f"{_pct(xs, 0.75):>9.1f}{max(xs):>9.1f}")

    # ---- 折算成钱 ----
    opens = lag_by.get("开仓") or []
    print("\n折算成收益（只对开仓有意义，曲线来自 _diag_delay.py 的 1449 笔实测）")
    print("-" * 74)
    if opens:
        med_bars = _pct(opens, 0.5) / 4
        p75_bars = _pct(opens, 0.75) / 4
        print(f"  开仓中位延迟 {med_bars:.1f} 根 4H → 总 R {_delay_cost(med_bars):+.1f}%")
        print(f"  开仓 P75 延迟 {p75_bars:.1f} 根 4H → 总 R {_delay_cost(p75_bars):+.1f}%")
        bad = [h for h in opens if h > 24]
        if bad:
            print(f"  {len(bad)} 笔开仓超过 24h（{len(bad) / len(opens) * 100:.0f}%），"
                  f"这部分已越过 TTL，市场多半走掉了")
    else:
        print("  还没有已执行的开仓单，无法折算。")
    # 参照值直接从同一条曲线算，避免手写常量与实测曲线漂移
    print(f"  参照：每天看 3 次 ≈ 中位 6h（{_delay_cost(1.5):+.0f}%）；"
          f"只看 1 次 ≈ 中位 12h（{_delay_cost(3):+.0f}%）；"
          f"隔天才看 ≈ 24h（{_delay_cost(6):+.0f}%）")

    _void_reasons(items)
    return 0


def _void_reasons(items):
    vs = [i for i in items if i["status"] == "void" and i.get("void_note")]
    if not vs:
        return
    print("\n自动作废的原因分布（= 系统替你省掉的无效动作）")
    print("-" * 74)
    cnt = {}
    for i in vs:
        k = ("日线翻回来了" if "日线" in i["void_note"] else
             ("行情走掉了" if "行情已变化" in i["void_note"] else i["void_note"]))
        cnt[k] = cnt.get(k, 0) + 1
    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<20}{v:>4} 张（{v / len(vs) * 100:.0f}%）")


def set_fill(ids, amount, px=None):
    """给**已结算**的工单补录实际成交额（U）。

    为什么不复用 mark：mark 只动 open 的，而这里要修的恰恰是历史上已经标了
    done、但金额是「建单建议额」而非「真实成交额」的那批。典型来源是本金口径
    变更（5000→2000）：工单面值 167，人实际只买了 71，标 done 时却没记金额。
    """
    d = load()
    hit = 0
    for i in d["items"]:
        if i["id"] not in ids:
            continue
        i["fill_notional"] = abs(float(amount))
        if px is not None:
            i["fill_px"] = float(px)
        hit += 1
        print(f"  #{i['id']} {i['coin']:<5} {LAB.get(i['kind'], i['kind']):<12} "
              f"成交额 {i['notional']} → {i['fill_notional']:,.0f} U")
    if not hit:
        print("  没有匹配的工单。")
    save(d)
    return hit


def adopt(coin, amount, side=None, px=None, force=False):
    """把「账户里**已经存在**的仓位」补进台账。只在真的持有那笔仓时用。

    == 为什么必须有这个入口（一个静默失败模式）==
    `has_pos` 只能从台账里 status=done 的「开仓」记录推出来
    （live_signals.short_opened → PC.executable）。也就是说：
    **只要那笔仓不是靠本系统的工单开的，系统就永远不知道它在。**
    后果不是报错，而是**从此不再发任何平仓 / 止损单** —— 一整笔敞口无人看管，
    页面上还标着「未建仓」，看起来像「本来就没有这笔仓」，一切正常。这正是
    这个项目反复出现的同一类病：错的不是数字，是「没算」。

    两类真实场景：
      · BTC / ETH / BNB 11-09 那三笔空头 —— 系统 11-24 才上线，从未下过单；
      · 你手动开的仓 —— 合约账户在手，看到信号直接下了，没走工单。

    补一条 status=done 的「开仓」即可，两个下游同时打通：
      · short_opened() → has_pos=True → 平仓 / 止损开始发单；
      · _opened_amount() → 平仓金额用**你填的实际金额**，不用今日 scale 重算。
        重算必漂：VT 系数每 7 天重算一次，拿今天的 scale 去平一个月前的仓，
        平出来就不是原仓位了（多平 = 反向开仓）。

    != 纸面 !=
    这条记录写的是**事实**，不是意图。金额没填对，平仓就会对着一个不存在的
    仓位下单 —— 例如把不存在的 199 U 空头「平掉」，实际是开出 199 U 多单。
    所以宁可拒收，也不给默认值。

    side: +1 多 / -1 空。不给就按当前信号推：target<0 → 空，否则多。
    """
    try:
        amount = abs(float(amount))
    except Exception:
        print("金额要是数字（U）。")
        return 1
    if amount <= 0:
        print("金额要大于 0。")
        return 1
    try:
        raw = json.load(open(SIG, encoding="utf-8"))
    except Exception:
        raw = {}
    c = {o["c"]: o for o in raw.get("coins", [])}.get(coin)
    bar = raw.get("bar4h") or ""
    if c is None:
        print(f"  ⚠ {coin} 不在当前信号里 —— 补进去也不会再有它的平仓单。")
    if side is None:
        side = -1 if (c and (c.get("target") or 0) < 0) else 1
    side = -1 if side < 0 else 1

    d = load()
    if not force:
        prev = [i for i in d["items"] if i["coin"] == coin
                and i["kind"] == "开仓" and i["status"] == "done"]
        if prev:
            last = prev[-1]
            got = last.get("fill_notional")
            got = got if got is not None else last.get("notional")
            print(f"  {coin} 已有 {len(prev)} 张已执行「开仓」（最近 #{last['id']} "
                  f"{float(got or 0):,.0f} U）。")
            print("  平仓金额依据的是**最近**这一张，再补一条会把它换掉。"
                  "确认要补就加 --force。")
            return 1

    it = {
        "id": d["next_id"],
        "key": f'{coin}|开仓|{(c or {}).get("regime_start") or bar}',
        "coin": coin, "kind": "开仓", "side": side,
        "notional": round(amount), "cap": PC.CAPITAL,
        "ref_px": px if px is not None else (c or {}).get("px"),
        "stop": None,
        "state": (c or {}).get("state"),
        "pos_side": 0,
        "regime_start": (c or {}).get("regime_start"),
        "bar": bar, "created": bar, "age_bars": 0,
        "slices": 1, "cap_flag": "",
        "note": ("手工补录：这笔仓在账户里已存在（不是本系统工单开的），"
                 "平仓金额以它为准"),
        "status": "done",
        "settled_at": dt.datetime.now(dt.timezone.utc).strftime(BAR_FMT) + " UTC",
        "fill_px": px,
        "fill_notional": round(amount),
        "void_note": None,
        "notified": True,          # 补录的历史仓不需要再提醒一次
    }
    d["next_id"] += 1
    d["items"].append(it)
    save(d)
    print(f"  #{it['id']} {coin} 已补录为 {('多' if side > 0 else '空')}头 {round(amount):,.0f} U"
          f"（status=done）")
    if side < 0:
        print("  → 该币 has_pos 变 True：平仓 / 止损单从此会发，金额按这 "
              f"{round(amount):,.0f} U 回推。")
    else:
        print("  → 该币最近一张已执行「开仓」换成这一条，平仓金额按它回推。")
    return 0


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
    if cmd == "stats":
        return cmd_stats()
    if cmd == "fill":
        # 补录实际成交额：fill <编号> <成交额U> [成交价]
        if len(rest) < 2:
            print("用法：fill <编号> <成交额U> [成交价]")
            return 1
        ids = {int(x) for x in rest[0].split(",")}
        px = float(rest[2]) if len(rest) > 2 else None
        set_fill(ids, float(rest[1]), px)
        return 0
    if cmd == "adopt":
        # 账户里已有的仓补进台账：adopt <币> <金额U> [多|空] [入场价]
        if len(rest) < 2:
            print("用法：adopt <币> <金额U> [多|空] [入场价] [--force]")
            return 1
        sd = None
        if len(rest) > 2 and rest[2] in ("多", "空", "long", "short"):
            sd = 1 if rest[2] in ("多", "long") else -1
        px = None
        for x in rest[2:]:
            try:
                px = float(x)
                break
            except Exception:
                continue
        return adopt(rest[0].upper(), rest[1], sd, px, "--force" in argv)
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
            print(f"用法：{cmd} <编号|all> [成交价] [成交额U]")
            return 1
        if rest[0] == "all":
            ids = "all"
            fp = float(rest[1]) if len(rest) > 1 else None
            fn = float(rest[2]) if len(rest) > 2 else None
        else:
            ids = {int(x) for x in rest[0].split(",")}
            fp = float(rest[1]) if len(rest) > 1 else None
            fn = float(rest[2]) if len(rest) > 2 else None
        mark(ids, "done" if cmd == "done" else "drop", fp, fn)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
