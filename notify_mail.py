#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 signal_now.html 当邮件正文发出去。

为什么用邮件而不是别的：
  · 不需要公网 IP、不需要机器人 token、不需要手机装东西，QQ 邮箱 SMTP 直发就能到
  · 手机上收信 = 推送，和 IM 的体验差距不大
  · 存档天然完整，事后能翻「那天信号长什么样」

配置：在脚本同目录建 _mail.json（已在 .gitignore 里；也认旧名 mail_config.json），格式：
    {"host":"smtp.qq.com","port":465,
     "user":"1084328481@qq.com","pass":"QQ邮箱授权码（不是登录密码）",
     "to":["1084328481@qq.com"]}
QQ 邮箱授权码：设置 → 账户 → POP3/SMTP服务 → 开启 → 生成授权码。

用法：
    python notify_mail.py                 # 自动：有动作发信号信，无动作则当天第一封补日报
    python notify_mail.py --always        # 无论有没有动作都发（仍受同 bar 去重）
    python notify_mail.py --digest        # 强制按日报发（每天北京最多一封，无动作也发）
    python notify_mail.py --dry           # 只打印主题与判定，不真发
    python notify_mail.py --force         # 忽略去重台账，强制重发

「发不发」的判据是台账里的 `last_sent_bj`（今天北京日期是否已有信），不是当前几点。
这么改的原因见下面的注释 —— 用「UTC 01 点那一班」当日报触发器，在 GitHub 的
schedule 面前等于永不触发。
"""
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ticket as TK

CFG = "_mail.json"
CFG_ALT = "mail_config.json"   # 仓库里早先留的 example 用的是另一套字段名，一并兼容
HTML = "signal_now.html"
SIG = "_signal.json"

# 催办的最小间隔（小时）。过期工单每 4 小时就提醒一次等于关掉了提醒功能，
# 8 小时 = 每两根 4H 提一次，既够密又不至于让人屏蔽发件人。
REMIND_GAP_H = 8

# 字段别名：两种历史约定都认
ALIAS = {
    "host": ("host", "smtp_host"),
    "port": ("port", "smtp_port"),
    "user": ("user", "sender", "username"),
    "pass": ("pass", "password", "auth_code"),
    "to": ("to", "receivers", "rcpt"),
}


def load_cfg():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in (CFG, CFG_ALT):
        p = os.path.join(here, name)
        if not os.path.exists(p):
            continue
        raw = json.load(open(p, encoding="utf-8"))
        cfg = {}
        for k, keys in ALIAS.items():
            for kk in keys:
                if raw.get(kk):
                    cfg[k] = raw[kk]
                    break
        if isinstance(cfg.get("to"), str):
            cfg["to"] = [cfg["to"]]
        missing = [k for k in ("host", "user", "pass", "to") if not cfg.get(k)]
        if missing:
            print(f"[配置不全] {name} 缺少字段：{', '.join(missing)}")
            return None
        cfg.setdefault("port", 465)
        cfg["_src"] = name
        return cfg
    return None


STATE = ".notify_state.json"   # 台账：上次发的是哪根 4H 的信号 + 上次发日报的北京日期
BJ = dt.timezone(dt.timedelta(hours=8))


def _today_bj():
    return dt.datetime.now(dt.timezone.utc).astimezone(BJ).strftime("%Y-%m-%d")


def _sig_key(sig, new_ids=()):
    """一根 4H 收盘 + 这一轮新开的工单编号 = 唯一指纹。

    为什么必须去重：定时任务会被延迟、被手动重跑、被 CI 重试，
    同一个信号发两遍会让人怀疑「是不是有新信号」—— 邮件的可信度就是这样耗光的。

    为什么不用 sig["urgent"]：那是 live_signals 的「本轮动作」，建底仓是按状态推出来的
    （不进 urgent），用它当指纹会把「AAVE 该建底仓」这种新单误判成重复。工单编号才是
    「这批活通知过没有」的真凭据。
    """
    ids = ",".join(str(i) for i in sorted(new_ids))
    return f'{sig.get("bar4h")}|{ids}|{",".join(sorted(sig["urgent"]))}'


def _subj_action(new_items, bj_hm):
    """有新动作时的主题：锁屏上就要看得出「买还是卖、多少钱」。

    只列前 3 个，多了收尾 —— 主题被截断比信息少更糟，截断后「买 825U」可能刚好没了。
    """
    parts = []
    for i in new_items[:3]:
        sgn = "买" if i["side"] > 0 else "卖"
        parts.append(f'{i["coin"]} {sgn}{i["notional"]:,.0f}U')
    tail = f' 等{len(new_items)}个' if len(new_items) > 3 else ""
    hard = any(i["kind"] == "开仓" for i in new_items)
    return f'【执行{"·急" if hard else ""}】' + " · ".join(parts) + tail + f' | {bj_hm}'


def _subj_remind(exp, bj_hm):
    top = max(exp, key=lambda i: i["age_bars"])
    return (f'【催办】{len(exp)} 单已过期未结算'
            f'（{top["coin"]} {top["kind"]} 挂 {top["age_bars"] // 6} 天）| {bj_hm}')


def _subj_patrol(sig, holds, bj_hm, op_n):
    near = min((o for o in sig["coins"] if not o["pos"] and o.get("need") is not None),
               key=lambda o: abs(o["need"]), default=None)
    # 「持仓」只数 4H 永续仓，现货账户里永远是 0/12 —— 看着像空仓，
    # 实际有 7 个币的现货底仓。两个数都写出来，否则每封信都在自相矛盾。
    # 「纸面」= 策略算到但本账户不执行的空头动作数，不写就不知道策略在做什么。
    spot = sig.get("spot_held") or {}
    md = ("含做空·执行" if (sig.get("allow_short") and sig.get("execute_short"))
          else ("含做空·纸面" if sig.get("allow_short") is not False else "不做空"))
    s = (f'【巡检】{md} · 4H 仓 {len(holds)}/{len(sig["coins"])}'
         f' · 现货 {len(spot)}')
    paper = sig.get("paper") or []
    if paper:
        s += f' · 纸面 {len(paper)}'
    s += f' · 待办 {op_n}'
    if near:
        s += f' · 距{near["c"]}触发{abs(near["need"]):.1f}%'
    return s + f' | {bj_hm}'


def _subj_fault(errs, bj_hm, n_patrol):
    names = " ".join(e["c"] for e in errs[:4])
    return (f'【故障】{len(errs)} 个品种取数失败（{names}）· 其余 {n_patrol} 个照常 | {bj_hm}')


def _read_state():
    if not os.path.exists(STATE):
        return {}
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def _write_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def _hours_since(iso):
    """距上次某类事件过了几小时。没有记录返回一个很大的数（当作早就该做了）。"""
    if not iso:
        return 1e9
    try:
        t = dt.datetime.strptime(iso[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 1e9


def main():
    always = "--always" in sys.argv
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv
    digest_req = "--digest" in sys.argv
    sig = json.load(open(SIG, encoding="utf-8"))
    holds = [o for o in sig["coins"] if o["pos"]]
    errs = sig.get("errors") or []
    bj_hm = sig["asof_bj"][5:16]

    tk = TK.load()
    op = [i for i in tk["items"] if i["status"] == "open"]
    # 「新动作」以单子上的 notified 标记为准，不看 sync 覆盖的 last.opened ——
    # 否则某次没发出去（额度已用 / SMTP 失败 / 进程崩）这条提醒就永久丢了。
    new_items = TK.unnotified(tk)
    exp = [i for i in op if TK.expired(i)]

    st = _read_state()
    key = _sig_key(sig, [i["id"] for i in new_items])
    dup = st.get("sig") == key
    # 「今天已经有过一封信」—— 不是「今天发过巡检信」。任何一封都算，
    # 因为它们正文都是同一份持仓表，多收一封并不增加信息量。
    sent_today = st.get("last_sent_bj") == _today_bj()
    ready_remind = _hours_since(st.get("last_remind_at")) >= REMIND_GAP_H

    # ---- 发不发、发哪种：按「要不要叫醒你」排优先级 ----
    #   故障 > 催办（有工单过期）> 新动作 > 巡检（每天保底一封）
    #
    # 巡检那一封不是给人看的，是给「静默失败」看的：收件箱里连续两天没有巡检信，
    # 只可能是链路断了，而不是策略安静。没有它，故障和「今天没活儿」长得一模一样。
    #
    # 判据落回台账（今天有没有信）而不是「现在几点」：GitHub 的 schedule 是
    # best-effort，实测延迟 30~60 分钟还会整班漏跑，挂钟判据在 CI 上等于永不触发。
    mode = None
    if digest_req:
        mode = "patrol"
    elif len(errs) >= 2 and not sent_today:
        mode = "fault"
    elif exp and ready_remind:
        mode = "remind"
    elif new_items:
        mode = "action"
    elif not sent_today or always:
        mode = "patrol"

    if mode is None:
        # 本轮无新动作、今天已发过、也没有该催的 —— 没什么可说的。
        # 但仍要记下「这根本 4H 已经算过了」，run_signal 靠它做 0.3 秒快速跳过。
        if not dry:
            st.update({"sig": key, "ts": sig.get("asof"), "bar4h": sig.get("bar4h")})
            _write_state(st)
        print("[跳过] 无新动作、今天已有信、无过期工单（已记录这根本 4H）。"
              "想看巡检信的样式：python notify_mail.py --digest --dry")
        return

    # 同一根 4H 的同一批新动作，重跑时不重复发（催办不受此限，它靠冷却时间控制）
    if mode == "action" and dup and not force:
        print(f"[跳过] 这根 4H（{sig.get('bar4h')}）的新动作已经发过了，不重复发。要重发加 --force")
        return
    if mode == "patrol" and digest_req and sent_today and not force:
        print(f"[跳过] 今天（北京 {_today_bj()}）已经有信了，巡检信每天最多一封。要重发加 --force")
        return

    subj = {"patrol": lambda: _subj_patrol(sig, holds, bj_hm, len(op)),
            "fault": lambda: _subj_fault(errs, bj_hm, len(sig["coins"]) - len(errs)),
            "remind": lambda: _subj_remind(exp, bj_hm),
            "action": lambda: _subj_action(new_items, bj_hm)}[mode]()

    cfg = load_cfg()
    if not cfg:
        print(f"[未配置] 找不到可用的 {CFG}（或 {CFG_ALT}）。在脚本同目录建一个：\n"
              '{"host":"smtp.qq.com","port":465,"user":"1084328481@qq.com",\n'
              ' "pass":"QQ邮箱授权码（不是登录密码）","to":["1084328481@qq.com"]}\n'
              "授权码获取：QQ邮箱 设置 → 账户 → POP3/SMTP服务 → 开启 → 生成授权码")
        return
    if dry:
        VERD = {"patrol": "巡检信：证明链路活着，无新动作时每天保底一封",
                "fault": f"故障信：{len(errs)} 个品种取数失败",
                "remind": f"催办信：{len(exp)} 单过期未结算（冷却 {REMIND_GAP_H}h 已过）",
                "action": f"执行信：{len(new_items)} 张工单还没通知过"}
        print(f"[dry] 配置来自 {cfg['_src']}，收件人 {', '.join(cfg['to'])}")
        print(f"[dry] 今天（北京 {_today_bj()}）已有信：{'是' if sent_today else '否'}")
        print(f"[dry] 未结算工单 {len(op)} 张，其中过期 {len(exp)} 张")
        print(f"[dry] 判定：{VERD[mode]} → 实跑会发送")
        print(f"[dry] 主题：{subj}")
        return

    body = open(HTML, encoding="utf-8").read()
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(subj, "utf-8")
    msg["From"] = formataddr(("加密信号", cfg["user"]))
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), context=ctx, timeout=30) as s:
        s.login(cfg["user"], cfg["pass"])
        s.sendmail(cfg["user"], cfg["to"], msg.as_string())
    # 只有真发成功才记账，发失败不写，下次还会重试
    # last_sent_bj 记的是「今天已经有信了」（不分类型）：
    # 它既是巡检信的每日上限，也是「今天到底有没有人被通知到」的唯一凭据。
    st.update({"sig": key, "ts": sig.get("asof"), "bar4h": sig.get("bar4h"),
               "last_sent_bj": _today_bj(), "last_mode": mode})
    if mode == "remind":
        st["last_remind_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    _write_state(st)
    # 只有真发成功才标记「已通知过」。发失败不标，下一班还会再提。
    if mode == "action":
        TK.mark_notified([i["id"] for i in new_items])
    print(f"[已发送·{mode}] {subj} → {', '.join(cfg['to'])}")


if __name__ == "__main__":
    main()
