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

CFG = "_mail.json"
CFG_ALT = "mail_config.json"   # 仓库里早先留的 example 用的是另一套字段名，一并兼容
HTML = "signal_now.html"
SIG = "_signal.json"

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


def _sig_key(sig):
    """一根 4H 收盘对应唯一一个信号，用它当去重指纹。

    为什么必须去重：定时任务会被延迟、被手动重跑、被 CI 重试，
    同一个信号发两遍会让人怀疑「是不是有新信号」—— 邮件的可信度就是这样耗光的。
    """
    return f"{sig.get('bar4h')}|{','.join(sorted(sig['urgent']))}"


def _read_state():
    if not os.path.exists(STATE):
        return {}
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def _write_state(st):
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def main():
    always = "--always" in sys.argv
    dry = "--dry" in sys.argv
    force = "--force" in sys.argv
    digest_req = "--digest" in sys.argv
    sig = json.load(open(SIG, encoding="utf-8"))
    n_urgent = len(sig["urgent"])
    holds = [o["c"] for o in sig["coins"] if o["pos"]]
    bj_hm = sig["asof_bj"][5:16]

    st = _read_state()
    key = _sig_key(sig)
    dup = st.get("sig") == key
    # 「今天已经有过一封信」—— 不是「今天发过日报」。任何一封都算，
    # 因为它们正文都是同一份持仓表，多收一封并不增加信息量。
    sent_today = st.get("last_sent_bj") == _today_bj()

    # 日报的判据是「今天北京日期还没有任何一封」，不是「现在是不是 UTC 01 点」。
    #
    # 原来 workflow 里写的是 `[ "$HOUR" = "01" ] && --digest`，把日报挂在了
    # UTC 01:05 那一班 cron 上。可 GitHub 的 schedule 是 best-effort：
    # 实测延迟 30~60 分钟、还会整班漏跑。翻运行记录，从来没有一次落在 01 点，
    # 于是「该发日报的那一班」被吃掉，「无动作」的那几班又静默跳过 ——
    # 结果是日报一次都没发出去，用户只能看到空收件箱。
    # 判据落回台账后，跑在几点都不影响结论，只影响你几点收到。
    digest = digest_req or (n_urgent == 0 and not always and not sent_today)

    if digest:
        # 日报不传递新信息，它的存在意义是：让「今天一封邮件都没有」
        # 只可能表示故障，而不是表示无事发生。
        subj = (f"【加密日报】{bj_hm} 持仓 {len(holds)}/{len(sig['coins'])}"
                + (f" · 待处理 {n_urgent}（{' '.join(sig['urgent'])}）"
                   if n_urgent else " · 今日无新动作"))
    else:
        subj = (f"【加密信号】{bj_hm} 需动作 {n_urgent} | "
                f"持仓 {len(holds)}/{len(sig['coins'])}"
                + (f"（{' '.join(sig['urgent'])}）" if n_urgent else ""))

    if digest:
        if digest_req and sent_today and not force:
            print(f"[跳过] 今天（北京 {_today_bj()}）已经有信了，日报每天最多一封。要重发加 --force")
            return
    else:
        if n_urgent == 0 and not always:
            # 走到这里说明今天已经发过信了、本轮又没有动作 —— 没什么可说的。
            # 但仍要记下「这根本 4H 已经算过了」，run_signal 靠它做 0.3 秒快速跳过。
            if not dry:
                st.update({"sig": key, "ts": sig.get("asof"), "bar4h": sig.get("bar4h")})
                _write_state(st)
            print(f"[跳过] 今天已有信、本轮无动作（已记录这根本 4H）。主题本应是：{subj}")
            return
        if dup and not force:
            print(f"[跳过] 这根 4H（{sig.get('bar4h')}）的信号已经发过了，不重复发。"
                  f"要重发加 --force")
            return

    cfg = load_cfg()
    if not cfg:
        print(f"[未配置] 找不到可用的 {CFG}（或 {CFG_ALT}）。在脚本同目录建一个：\n"
              '{"host":"smtp.qq.com","port":465,"user":"1084328481@qq.com",\n'
              ' "pass":"QQ邮箱授权码（不是登录密码）","to":["1084328481@qq.com"]}\n'
              "授权码获取：QQ邮箱 设置 → 账户 → POP3/SMTP服务 → 开启 → 生成授权码")
        return
    if dry:
        # 演练不走去重分支：要看的是「这一轮到底会不会发」，而不是被上次的记录挡住
        if digest and digest_req:
            verdict = "显式 --digest，今天未发过 → 实跑会发送日报"
        elif digest:
            verdict = "今天还没有信 + 本轮无动作 → 实跑会自动补一封日报"
        elif force:
            verdict = "已加 --force → 实跑会发送（绕过去重）"
        elif dup:
            verdict = "与上次相同 → 实跑会被跳过（要发加 --force）"
        else:
            verdict = "与上次不同 → 实跑会发送"
        print(f"[dry] 配置来自 {cfg['_src']}，收件人 {', '.join(cfg['to'])}")
        print(f"[dry] 去重指纹 {key}")
        print(f"[dry] 今天（北京 {_today_bj()}）已有信：{'是' if sent_today else '否'}")
        print(f"[dry] {verdict}")
        print(f"[dry] {subj}")
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
    # last_sent_bj 记的是「今天已经有信了」（不分信号信还是日报）：
    # 它既是日报的每日上限，也是「今天到底有没有人被通知到」的唯一凭据。
    st.update({"sig": key, "ts": sig.get("asof"), "bar4h": sig.get("bar4h"),
               "last_sent_bj": _today_bj()})
    _write_state(st)
    print(f"[已发送] {subj} → {', '.join(cfg['to'])}")


if __name__ == "__main__":
    main()
