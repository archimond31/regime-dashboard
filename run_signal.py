#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令跑完整个信号链路：出信号 → 渲染 HTML → 发邮件。

为什么单独写一个入口：
  Windows 任务计划程序和 GitHub Actions 都只会执行「一条命令」，
  把三段 python 串在计划任务里容易踩引号/工作目录的坑，收进一个脚本最省事。

用法：
    python run_signal.py                 # 有动作才发邮件（默认）
    python run_signal.py --always        # 不管有没有动作都发
    python run_signal.py --digest        # 发每日日报（每天北京最多一封，无动作也发）
    python run_signal.py --dry           # 全链路跑，但不真发信
    python run_signal.py --force         # 忽略去重，强制重发同一根 4H 的信号
    python run_signal.py --no-mail       # 只出 signal_now.html，不碰邮件

退出码：0 正常（含「无动作跳过」），1 有品种取数失败或出信号失败。
可取数失败不致命 —— 少一个币照发，只是表里会标红。
"""
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"
STATE = os.path.join(HERE, ".notify_state.json")


def current_bar(now=None):
    """当前「已收盘」的最后一根 4H 的标识（UTC）。

    4H 边界是 00/04/08/12/16/20。落在 19:39 时最后一根已收盘的是 16:00，
    与 live_signals 里剔除未收盘 bar 的口径一致。
    """
    t = now or dt.datetime.now(dt.timezone.utc)
    return t.replace(hour=t.hour // 4 * 4, minute=0, second=0,
                     microsecond=0).strftime("%Y-%m-%d %H:%M")


def processed_bar():
    if not os.path.exists(STATE):
        return None
    try:
        return json.load(open(STATE, encoding="utf-8")).get("bar4h")
    except Exception:
        return None


def sent_today():
    """今天（北京）是否已经真的发出过一封邮件。"""
    if not os.path.exists(STATE):
        return False
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return False
    bj = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    return st.get("last_sent_bj") == bj


def step(name, args, fatal=True):
    print(f"\n{'=' * 62}\n▶ {name}\n{'=' * 62}", flush=True)
    r = subprocess.run([PY] + args, cwd=HERE)
    if r.returncode != 0:
        head = "中断" if fatal else "继续（非致命）"
        print(f"✗ {name} 退出码 {r.returncode}，{head}", flush=True)
        return False
    return True


def main():
    argv = sys.argv[1:]
    no_mail = "--no-mail" in argv
    passthru = [a for a in argv if a in ("--always", "--dry", "--force", "--digest")]

    # 快速跳过：这根本 4H 已经完整算过一遍、**且今天已经真的发出过一封**时，
    # 没必要再拉一遍全量 K 线。正常一轮约 40 秒，跳过只要 0.3 秒。
    # 「今天还没有信」必须往下走 —— 否则当天第一封（保底日报）永远补不出来：
    # 上一轮算完、无动作、跳过发信，台账里只有 bar4h 没有 last_sent_bj，
    # 这时若还按 bar4h 相同就跳，收件箱就会一直空着。
    bypass = any(a in argv for a in ("--digest", "--force", "--always"))
    if not no_mail and not bypass:
        cur, done = current_bar(), processed_bar()
        if done == cur and sent_today():
            print(f"[跳过] 这根本 4H（{cur}）已处理过、且今天已发过信，无新内容。", flush=True)
            print("       要强制执行请加 --always 或 --force", flush=True)
            return 0

    if not step("① 拉数据 + 算信号", ["live_signals.py"]):
        return 1
    if not step("② 渲染 HTML", ["gen_signal_html.py"]):
        return 1
    if not no_mail:
        step("③ 发邮件", ["notify_mail.py"] + passthru, fatal=False)

    out = os.path.join(HERE, "signal_now.html")
    if os.path.exists(out):
        print(f"\n产物：{out}（{os.path.getsize(out) / 1024:.0f} KB）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
