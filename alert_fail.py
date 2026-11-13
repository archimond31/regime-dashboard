#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计划任务失败时发一封告警邮件。

注意它报的不是「策略故障」，而是「这台机器上的定时任务没跑成」：
python 路径失效、网络断、代码报错、磁盘满，都会走到这里。

为什么要单独做这件事：
    在补上它之前，用户眼里只有两种状态 ——「收到邮件」和「没收到」。
    而「没收到」有两种含义：真的没动作（正常），或者跑挂了（故障）。
    静默故障是最贵的：11-13 早上那次数据滞后，就是因为分不清这两者才没被发现。

刻意设计成「自己也失败也不影响主流程」：
    调用它的 cmd 早就把真实退出码存进 RC 了，这里再报不出去也只能算了，
    绝不允许把退出码改掉（否则会掩盖真正的错误）。
"""
import datetime as dt
import os
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

HERE = os.path.dirname(os.path.abspath(__file__))   # 本文件就在项目根
LOG = os.path.join(HERE, "logs", "signal.log")
TAIL = 60

# Windows 上常见的退出码，直接翻译成一句人话，省得还要去查
RC_HINT = {
    "1": "脚本自身报错（Python 异常），看下面日志里的 traceback",
    "2": "找不到文件或路径（PyCharm 里则是解释器路径失效）",
    "9009": "python 没找到 —— run_signal.cmd 里的 PY 路径失效了",
    "267009": "任务还在运行（0x41301），通常是上一班卡住了",
}


def tail_lines(path, n=TAIL):
    try:
        with open(path, "rb") as f:
            txt = f.read().decode("utf-8", "replace")
        return "\n".join(txt.splitlines()[-n:])
    except Exception as e:                      # noqa: BLE001
        return f"(读不到日志：{e})"


def main():
    rc = sys.argv[1] if len(sys.argv) > 1 else "?"

    try:
        sys.path.insert(0, HERE)
        import notify_mail as N                 # 复用它的配置解析与字段别名
        cfg = N.load_cfg()
    except Exception as e:                      # noqa: BLE001
        print(f"[告警] 加载邮件配置失败：{e}")
        return 0

    if not cfg:
        print("[告警] 找不到可用的 _mail.json，告警发不出去")
        return 0

    bj = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    subj = f"【加密信号·故障】{bj:%m-%d %H:%M} 计划任务失败（exit={rc}）"
    hint = RC_HINT.get(str(rc), "看下面日志末尾的报错")
    body = (
        '<div style="font-family:-apple-system,\'Segoe UI\',\'PingFang SC\',sans-serif;'
        'color:#1a1a1a;font-size:13px;line-height:1.8;max-width:820px">'
        '<div style="border-left:4px solid #c62828;background:#fbe9e7;padding:10px 14px;'
        'margin-bottom:16px">'
        '<b style="font-size:14px">计划任务 RegimeSignal4H 本次运行失败</b><br>'
        f'退出码 <b>{rc}</b>　时间 {bj:%Y-%m-%d %H:%M}（北京）<br>'
        '这不是策略信号，是这台机器上的监控没跑成。</div>'
        f'<b>可能原因</b><br>{hint}<br><br>'
        f'<b>日志最后 {TAIL} 行</b>'
        '<pre style="background:#f5f5f5;border:1px solid #e0e0e0;border-radius:6px;'
        'padding:10px;font-size:11.5px;line-height:1.5;white-space:pre-wrap;'
        f'overflow-x:auto">{tail_lines(LOG)}</pre>'
        '<b>怎么修</b><br>'
        '· python 路径失效 → 改 deploy\\run_signal.cmd 里的 PY<br>'
        '· 网络不通 → 下一班通常会自愈，连续两班失败再查<br>'
        '· 代码报错 → 按 traceback 定位，改完 <code>task_ctl.cmd run</code> 验证<br>'
        '· 磁盘满 → 清 .cache_binance 里的旧 zip'
        '</div>')

    if "--dry" in sys.argv:
        # 调试用：只打印不真发，免得每次验证都往收件箱里塞一封假故障
        print(f"[dry] 会发送：{subj}")
        print(f"[dry] 日志尾部读到 {len(tail_lines(LOG).splitlines())} 行")
        return 0

    try:
        msg = MIMEText(body, "html", "utf-8")
        msg["Subject"] = Header(subj, "utf-8")
        msg["From"] = formataddr(("加密信号", cfg["user"]))
        msg["To"] = ", ".join(cfg["to"])
        msg["Date"] = formatdate(localtime=True)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)),
                              context=ctx, timeout=30) as s:
            s.login(cfg["user"], cfg["pass"])
            s.sendmail(cfg["user"], cfg["to"], msg.as_string())
        print(f"[告警已发送] {subj}")
    except Exception as e:                      # noqa: BLE001
        # 告警发不出去就算了 —— 绝不能因为报错失败而改掉真实退出码
        print(f"[告警发送失败] {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
