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
    python notify_mail.py                 # 有动作才发，同一根 4H 只发一次
    python notify_mail.py --always        # 无论有没有动作都发（仍受同 bar 去重）
    python notify_mail.py --digest        # 日报：每天（北京）最多一封，无动作也发
    python notify_mail.py --dry           # 只打印主题与判定，不真发
    python notify_mail.py --force         # 忽略去重台账，强制重发
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
    digest = "--digest" in sys.argv
    sig = json.load(open(SIG, encoding="utf-8"))
    n_urgent = len(sig["urgent"])
    holds = [o["c"] for o in sig["coins"] if o["pos"]]
    bj_hm = sig["asof_bj"][5:16]

    if digest:
        # 日报：每天（北京）最多一封，无动作也发。
        # 存在的意义不是传递信号，是让「今天没收到任何邮件」只可能表示故障。
        subj = (f"【加密日报】{bj_hm} 持仓 {len(holds)}/{len(sig['coins'])}"
                + (f" · 待处理 {n_urgent}（{' '.join(sig['urgent'])}）"
                   if n_urgent else " · 今日无新动作"))
    else:
        subj = (f"【加密信号】{bj_hm} 需动作 {n_urgent} | "
                f"持仓 {len(holds)}/{len(sig['coins'])}"
                + (f"（{' '.join(sig['urgent'])}）" if n_urgent else ""))

    st = _read_state()
    key = _sig_key(sig)
    dup = st.get("sig") == key
    sent_digest_today = st.get("last_sent_bj") == _today_bj()

    if digest:
        if sent_digest_today and not force:
            print(f"[跳过] 今天（北京 {_today_bj()}）的日报已经发过了，不重复发。要重发加 --force")
            return
    else:
        if n_urgent == 0 and not always:
            print(f"[跳过] 本轮无动作。主题本应是：{subj}\n       要强制发就加 --always")
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
        if digest:
            verdict = "日报模式，今天未发过 → 实跑会发送"
        elif force:
            verdict = "已加 --force → 实跑会发送（绕过去重）"
        elif dup:
            verdict = "与上次相同 → 实跑会被跳过（要发加 --force）"
        else:
            verdict = "与上次不同 → 实跑会发送"
        print(f"[dry] 配置来自 {cfg['_src']}，收件人 {', '.join(cfg['to'])}")
        print(f"[dry] 去重指纹 {key}")
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
    st.update({"sig": key, "ts": sig.get("asof"), "bar4h": sig.get("bar4h")})
    if digest:
        st["last_sent_bj"] = _today_bj()
    _write_state(st)
    print(f"[已发送] {subj} → {', '.join(cfg['to'])}")


if __name__ == "__main__":
    main()
