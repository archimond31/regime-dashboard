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
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"


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
