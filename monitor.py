#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻监控：跑起来就一直等，每根 4H 收盘后 5 分钟自动算一遍信号。

用法：
    python monitor.py          # 前台常驻，Ctrl+C 退出
    python monitor.py --once   # 只跑一次就退出（调试用）

它做的事：
    1. 启动立刻跑一次（这根本 4H 若已算过，run_signal 内部 0.3 秒就跳过）
    2. 算出下一根 4H 的收盘时刻，睡到那时候
    3. 重复，永不退出（除非你 Ctrl+C）

为什么不「睡 4 小时再睡 4 小时」：
    每一轮都重新计算目标时刻，而不是累计 sleep。系统休眠、时钟漂移之后会自动
    重新对齐，不会一天天偏下去 —— 累计 sleep 跑到第三天就已经偏出好几分钟了。

和 Windows 计划任务的关系（二选一，不能同开）：
    计划任务 RegimeSignal4H 目前是 Disabled —— 本脚本是唯一入口。
    两者都会在 4H 收盘后 5 分钟跑，同开会让同一根 4H 算出两封邮件
    （台账只按 bar 去重，同一秒双启动两边都还没写台账，去重挡不住）。
    想换回计划任务（不怕关窗口、不怕重启）：
        deploy\\task_ctl.cmd on
    想再切回来：
        deploy\\task_ctl.cmd off
"""
import datetime as dt
import logging
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "logs")
LOG = os.path.join(LOGDIR, "monitor.log")
LOCK_PORT = 47653          # 单实例锁：端口被占 = 已经有一个在跑了
UTC = dt.timezone.utc
BJ = dt.timezone(dt.timedelta(hours=8))
DELAY_MIN = 5              # 4H 收盘后再等 5 分钟（等交易所把最后一根 K 线落盘）


def now():
    return dt.datetime.now(UTC)


def floor4(t):
    """把时刻向下取整到 4H 边界（UTC 00/04/08/12/16/20）。"""
    return t.replace(hour=t.hour // 4 * 4, minute=0, second=0, microsecond=0)


def next_run(t=None):
    """下一次该跑的时刻（UTC）。

    边界 00/04/08/... 各对应一个跑点 00:05/04:05/08:05。
    若当前刚好落在 [边界, 边界+5min) 内，下一班就是边界+5min（马上）；
    否则跳到下一个边界的 +5min。
    """
    t = t or now()
    base = floor4(t)
    cand = base + dt.timedelta(minutes=DELAY_MIN)
    if cand <= t:
        cand = base + dt.timedelta(hours=4, minutes=DELAY_MIN)
    return cand


def run_once(logger):
    """跑一遍 run_signal.py，把它的输出原样收进日志。"""
    t0 = time.time()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-u", "run_signal.py"],
                       cwd=HERE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for line in ((r.stdout or "") + (r.stderr or "")).splitlines():
        if line.strip():
            logger.info("  " + line)
    dur = time.time() - t0

    if r.returncode == 0:
        logger.info(f"本轮结束，耗时 {dur:.1f}s")
        return 0

    logger.warning(f"本轮失败，退出码 {r.returncode}，耗时 {dur:.1f}s —— 发故障告警")
    try:
        subprocess.run([sys.executable, "alert_fail.py", str(r.returncode)],
                       cwd=HERE, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    except Exception as e:                  # noqa: BLE001
        logger.warning(f"告警脚本也没跑成：{type(e).__name__}: {e}")
    return r.returncode


def setup_log(logger):
    os.makedirs(LOGDIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.setLevel(logging.INFO)


def main():
    logger = logging.getLogger("monitor")
    setup_log(logger)

    # 单实例：绑一个本地端口当锁。进程退出时 socket 自动释放，不会留下死锁文件。
    guard = socket.socket()
    try:
        guard.bind(("127.0.0.1", LOCK_PORT))
        guard.listen(1)
    except OSError:
        logger.error("已经有一个 monitor 在跑了（端口 %d 被占）。要重开请先关掉那个。",
                     LOCK_PORT)
        return 1

    logger.info("=" * 62)
    logger.info("监控启动　解释器 %s", sys.executable)
    logger.info("日志：%s", LOG)
    logger.info("计划任务 RegimeSignal4H 已停用，本进程是唯一入口（Ctrl+C 即停）")

    def safe_run():
        """一轮跑挂了也不能把常驻进程带走 —— 记下来，下一班照跑。"""
        try:
            run_once(logger)
        except Exception:                  # noqa: BLE001
            logger.exception("本轮抛出未捕获异常，已吞掉；下一班继续")

    safe_run()
    if "--once" in sys.argv:
        logger.info("--once 已跑完，退出")
        return 0

    try:
        while True:
            target = next_run()
            left = (target - now()).total_seconds()
            logger.info("下一班 %s（北京），还有 %.1f 小时",
                        target.astimezone(BJ).strftime("%m-%d %H:%M"), left / 3600)
            while True:
                left = (target - now()).total_seconds()
                if left <= 0:
                    break
                # 分段睡：既能让 Ctrl+C 及时响应，也能在系统休眠后重新对齐
                time.sleep(min(left, 60))
            safe_run()
    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，监控退出")
        return 0
    finally:
        guard.close()


if __name__ == "__main__":
    sys.exit(main())
