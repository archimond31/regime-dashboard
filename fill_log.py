#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成交滑点台账 —— 把「理论容量」换成「你自己的实测曲线」。

为什么需要它：容量上限算出来只是量级（合约口径 7.7 万 U），真实的滑点取决于
你的下单方式、时段、交易所深度。唯一可靠的办法是逐笔记账，跑三个月后拟合出
「参与率 → 滑点」的关系，那时容量就不用估了。

用法：
  python fill_log.py add BTC -1 525 80261.0            # 币 方向 名义U 成交均价（信号价自动取）
  python fill_log.py add BTC -1 525 80250.5 80261.0    # 显式给信号价与成交价
  python fill_log.py show                              # 看台账
  python fill_log.py backfill                          # 补「成交那根 4H 收盘后才有的成交额」
  python fill_log.py report                            # 拟合 + 反推容量

方向：+1 买入/做多，-1 卖出/做空。
滑点一律记「不利方向为正」：买贵了、卖便宜了都记正数（bp）。
参与率 = 本笔名义 / 成交所在那根 4H 的合约成交额。

流程要点：成交时那根 4H 往往还没收盘，成交额不完整，所以 add 只记 bar_ms、
参与率留空；等该根收盘后跑一次 backfill 补齐。别拿「最近一根已收盘」凑数，
那会把参与率算到隔壁 4 小时去。
"""
import datetime as dt
import json
import math
import os
import socket
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portfolio_config as PC

LEDGER = os.environ.get("FILL_LEDGER", "_fills.jsonl")   # 设 FILL_LEDGER 可指到测试台账
FAPI = "https://fapi.binance.com/fapi/v1/klines"
PROXIES = ["http://127.0.0.1:7890", "http://127.0.0.1:10809",
           "http://127.0.0.1:7897", "http://127.0.0.1:10808", None]
# 一律用小数（不是百分数），避免「4.2% 又乘一次 100」这类量纲事故。
STOP_PCT = 0.042        # 1.5×ATR 止损的价格距离（ATR 中位 2.77% → 约 4.2%）
SLIP_BUDGET = 0.10      # 滑点预算：吃掉止损空间的 10%
TARGET_BP = SLIP_BUDGET * STOP_PCT * 1e4      # = 42.0 bp


def _now():
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def _iso(ms):
    return (dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)
            .strftime("%Y-%m-%d %H:%M") if ms else None)


def _get(url, proxy, timeout=15):
    if proxy:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        op.addheaders = [("User-Agent", "Mozilla/5.0")]
        return op.open(url, timeout=timeout).read().decode()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode()


def _bar_open_ms(ms, hours=4):
    """把任意时刻向下取整到它所属 4H 的开盘时刻（UTC 整 4 小时边界）。"""
    step = hours * 3600 * 1000
    return int(ms) // step * step


def _bar_closed(open_ms, hours=4):
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    return open_ms + hours * 3600 * 1000 <= now_ms


def fetch_bar_qv(coin, open_ms):
    """取指定 4H（按开盘时间定位）的合约成交额。

    关键点：必须定位到「成交所在的那根」，不能拿「最近一根已收盘」凑数——否则
    在 12:05 成交会误用 08:00 那根的成交额，参与率直接算错。
    该根尚未收盘时返回 None，等 backfill 补。
    """
    if not _bar_closed(open_ms):
        return None
    socket.setdefaulttimeout(15)
    url = (FAPI + "?symbol=" + coin + "USDT&interval=4h&limit=1&startTime=" + str(int(open_ms)))
    for px in PROXIES:
        try:
            d = json.loads(_get(url, px))
            if not d:
                continue
            k = d[0]
            if int(k[0]) != int(open_ms):
                continue
            return float(k[7])
        except Exception:
            continue
    return None


def sig_px_of(coin):
    """从 _signal.json 取该币当前信号价。"""
    try:
        d = json.load(open("_signal.json", encoding="utf-8"))
        for o in d["coins"]:
            if o["c"] == coin:
                return float(o["px"])
    except Exception:
        pass
    return None


def load():
    rows = []
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


def save(rows):
    with open(LEDGER, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_add(argv):
    if len(argv) < 3:
        print("用法：add <币> <方向 +/-1> <名义U> [信号价] <成交价>")
        return 1
    coin = argv[0].upper()
    side = int(argv[1])
    notional = float(argv[2])
    if side not in (1, -1):
        print("方向只能是 +1 或 -1")
        return 1
    if len(argv) >= 5:
        spx, fpx = float(argv[3]), float(argv[4])
    elif len(argv) == 4:
        spx, fpx = sig_px_of(coin), float(argv[3])
        if spx is None:
            print(f"取不到 {coin} 的信号价，请显式给出：add {coin} {side} {notional} <信号价> <成交价>")
            return 1
    else:
        spx = sig_px_of(coin)
        if spx is None:
            print(f"取不到 {coin} 的信号价，请显式给出")
            return 1
        fpx = float(input(f"{coin} 信号价 {spx:g}，请输入成交均价：").strip())

    if spx <= 0 or fpx <= 0:
        print("价格必须为正")
        return 1
    # 不利方向为正：做多买贵 / 做空卖便宜
    slip = side * (fpx - spx) / spx
    bar_ms = _bar_open_ms(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    qv = fetch_bar_qv(coin, bar_ms)
    part = (notional / qv) if qv else None

    rec = {"ts": _now(), "coin": coin, "side": side, "notional": notional,
           "sig_px": spx, "fill_px": fpx, "slip_bp": round(slip * 1e4, 2),
           "bar_ms": bar_ms, "bar_qv": qv, "participation": part,
           "bar_ts": _iso(bar_ms)}
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    cost = abs(slip) * notional
    print(f"已记：{coin} {'买' if side > 0 else '卖'} {notional:,.0f} U  "
          f"信号 {spx:g} → 成交 {fpx:g}")
    print(f"      滑点 {rec['slip_bp']:+.2f} bp = {cost:,.2f} U")
    if part:
        print(f"      所在 4H {rec['bar_ts']} UTC 成交额 {qv:,.0f} U → 参与率 {part * 1e6:.3f} ppm")
    else:
        print(f"      所在 4H {rec['bar_ts']} UTC 还没收盘，参与率先留空 —— 收盘后跑 backfill 补上")
    return 0


def cmd_backfill(argv):
    """把 bar_qv 空着的记录补上（等那根 4H 收盘后再跑）。"""
    rows = load()
    todo = [r for r in rows if not r.get("bar_qv") and r.get("bar_ms")]
    if not todo:
        print("没有待补的记录。")
        return 0
    print(f"待补 {len(todo)} 笔：")
    done = 0
    for r in todo:
        qv = fetch_bar_qv(r["coin"], r["bar_ms"])
        if not qv:
            print(f"  {r['ts']}  {r['coin']:<5} {_iso(r['bar_ms'])} UTC 还没收盘，跳过")
            continue
        r["bar_qv"] = qv
        r["participation"] = r["notional"] / qv
        done += 1
        print(f"  {r['ts']}  {r['coin']:<5} {_iso(r['bar_ms'])} UTC → "
              f"成交额 {qv:,.0f} U，参与率 {r['participation'] * 1e6:.3f} ppm")
    if done:
        save(rows)
    print(f"\n补上 {done} 笔，还剩 {len(todo) - done} 笔没收盘。")
    return 0


def cmd_show(argv):
    rows = sorted(load(), key=lambda r: r.get("ts", ""))
    if not rows:
        print("台账还是空的。用 add 记第一笔。")
        return 0
    print(f"{'时间':<18}{'币':<6}{'方向':>4}{'名义U':>10}{'信号价':>12}"
          f"{'成交价':>12}{'滑点bp':>9}{'参与ppm':>10}")
    print("-" * 84)
    for r in rows[-30:]:
        p = f"{r['participation'] * 1e6:.3f}" if r.get("participation") else "—"
        print(f"{r['ts']:<18}{r['coin']:<6}{'买' if r['side'] > 0 else '卖':>4}"
              f"{r['notional']:>10,.0f}{r['sig_px']:>12g}{r['fill_px']:>12g}"
              f"{r['slip_bp']:>+9.2f}{p:>10}")
    if len(rows) > 30:
        print(f"... 共 {len(rows)} 笔，上面只显示最近 30 笔")
    n = len(rows)
    s = sorted(abs(r["slip_bp"]) for r in rows)
    print(f"\n共 {n} 笔 · 滑点中位 {s[n // 2]:.2f} bp · 均值 "
          f"{sum(s) / n:.2f} bp · 最差 {s[-1]:.2f} bp")
    return 0


def _fit(pts):
    """log-log 最小二乘：log(slip) = log(a) + b·log(part)。返回 (a, b) 或 None。"""
    xs = [(math.log(p), math.log(s)) for p, s in pts if p > 0 and s > 0]
    n = len(xs)
    if n < 3:
        return None
    mx = sum(x for x, _ in xs) / n
    my = sum(y for _, y in xs) / n
    den = sum((x - mx) ** 2 for x, _ in xs)
    if den == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in xs) / den
    a = math.exp(my - b * mx)
    return a, b


def cmd_report(argv):
    rows = load()
    pts = [(r["participation"], abs(r["slip_bp"])) for r in rows
           if r.get("participation") and abs(r["slip_bp"]) > 0]
    print(f"台账 {len(rows)} 笔，其中 {len(pts)} 笔可用于拟合（有参与率且滑点非零）")
    print(f"目标滑点预算：{SLIP_BUDGET * 100:.0f}% × 止损 {STOP_PCT * 100:.1f}% = "
          f"{TARGET_BP / 1e4 * 100:.2f}% = {TARGET_BP:.0f} bp\n")

    # 分桶一律用 ppm，和 show 的输出同口径（此前用小数区间比 ppm，永远落不进桶）
    if pts:
        buckets = [(0, 10), (10, 50), (50, 200), (200, 1000), (1000, 10000)]
        print(f"{'参与率(ppm)':<16}{'笔数':>6}{'滑点中位bp':>12}{'均值bp':>10}")
        print("-" * 46)
        for lo, hi in buckets:
            sel = sorted(s for p, s in pts if lo <= p * 1e6 < hi)
            if sel:
                print(f"{f'{lo:g} ~ {hi:g}':<16}{len(sel):>6}{sel[len(sel) // 2]:>12.2f}"
                      f"{sum(sel) / len(sel):>10.2f}")
        print()

    fit = _fit(pts)
    if not fit:
        print("样本不足（需要 ≥3 笔带参与率的成交），先攒数据。")
        print("粗略判据：滑点中位 < 15 bp 说明容量还远；> 40 bp 说明已经在吃止损空间。")
        return 0

    a, b = fit
    pmax = max(p for p, _ in pts)
    print(f"拟合：滑点(bp) = {a:.3f} × 参与率^{b:.2f}   （业界平方根模型 b≈0.5）")
    print(f"实测参与率范围：{min(p for p, _ in pts) * 1e6:.0f} ~ {pmax * 1e6:.0f} ppm")
    if not (0.25 <= b <= 1.0):
        print(f"  ⚠ 指数 b={b:.2f} 偏离平方根模型太远，样本可能太窄，先别拿它定容量。")

    p_star = (TARGET_BP / a) ** (1 / b) if a > 0 and b > 0 else None
    if not p_star:
        print("拟合参数不合法（a 或 b ≤ 0），无法反推。")
        return 0

    extrap = p_star / pmax
    print(f"\n反推：滑点 = {TARGET_BP:.0f} bp 时 → 参与率 {p_star * 1e6:,.0f} ppm"
          f"  （实测上限 {pmax * 1e6:,.0f} ppm 的 {extrap:.1f} 倍）")
    if extrap > 5:
        print(f"  ⚠ 外推 {extrap:.0f} 倍，远超实测覆盖区间 —— 下面的容量当上限看，不能当结论。")
        print(f"    要把资金加到那个量级，必须先补该量级的实测成交。")

    print("\n" + "-" * 78)
    print(f"{'币':<6}{'权重':>6}{'合约4H中位':>15}{'单笔上限U':>12}{'该币峰值U':>12}{'反推组合U':>13}")
    print("-" * 78)
    caps = []
    try:
        cf = json.load(open("_cap_fut.json", encoding="utf-8"))
        qv_map = {r["c"]: r["fut_med"] for r in cf["rows"]}
    except Exception:
        qv_map = {}
    if not qv_map:
        print("（缺 _cap_fut.json，先跑 _cap_futures.py 生成合约成交额）")
    tight = PC.BASE_RATIO + PC.ADD_RATIO
    for c, w in sorted(PC.WEIGHTS.items(), key=lambda kv: -kv[1]):
        qv = qv_map.get(c)
        if not qv:
            continue
        lots = p_star * qv
        peak = PC.CAPITAL * w * tight
        combo_cap = lots / (w * tight)
        caps.append((c, combo_cap))
        print(f"{c:<6}{w * 100:>5.0f}%{qv:>15,.0f}{lots:>12,.0f}{peak:>12,.0f}{combo_cap:>13,.0f}")
    if caps:
        c_min, v_min = min(caps, key=lambda kv: kv[1])
        print("-" * 78)
        print(f"组合上限（最紧的品种 {c_min}）：约 {v_min:,.0f} U")

    # 最实用的一块：当前配置本身离预算有多远
    print(f"\n当前配置（{PC.CAPITAL:,.0f} U）的预测滑点")
    print("-" * 78)
    print(f"{'币':<6}{'峰值名义U':>12}{'参与率ppm':>12}{'预测滑点bp':>13}{'占预算':>10}{'判定':>8}")
    print("-" * 78)
    worst = 0.0
    for c, w in sorted(PC.WEIGHTS.items(), key=lambda kv: -kv[1]):
        qv = qv_map.get(c)
        if not qv:
            continue
        peak = PC.CAPITAL * w * tight
        p = peak / qv
        bp = a * (p ** b)
        frac = bp / TARGET_BP
        worst = max(worst, frac)
        flag = "超标" if frac > 1 else ("接近" if frac > 0.5 else "宽松")
        print(f"{c:<6}{peak:>12,.0f}{p * 1e6:>12.1f}{bp:>13.2f}{frac * 100:>9.0f}%{flag:>8}")
    print("-" * 78)
    print(f"全组合最紧：{worst * 100:.0f}% 的滑点预算"
          + ("（已超标，必须拆单）" if worst > 1 else "（还在预算内）"))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "add":
        return cmd_add(rest)
    if cmd == "show":
        return cmd_show(rest)
    if cmd == "backfill":
        return cmd_backfill(rest)
    if cmd == "report":
        return cmd_report(rest)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
