#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vol_target.py —— 波动目标化（VT60 档）

== 它做什么 ==
每个币的仓位乘一个系数：

    s_c = clip(TARGET / σ_c, LO, HI) × KAPPA

σ_c 是该币**日收盘收益**的滚动年化波动（回看 60 天，严格只用当日之前的数据）。
σ 大的币自动减仓、σ 小的币自动加仓 —— **总敞口不变，只是重新分配**。
KAPPA 的作用就是把「重新分配后缩掉的那部分敞口」补回来，让平均总敞口
维持在 0.686×（与等资金配置相同），否则收益差里会混进「仓位更小」这个假因素。

== 为什么是 60 天（这是被验证过的，别随手改）==
`_exp_vt_long.py` 的完整对照：
  全样本 60→20.1× / 120→19.9× / 180→21.6× / 240→22.6× / 365→17.4×（非单调）
  剔除 2021：60→6.39× 最优，180→6.04×，365→5.28×（排序反转）
  → 180/240 的全样本优势是 2021 少砍 SOL 换来的，不是稳健改善。
  60 天在「剔除 2021」后是最强的一档，且是唯一有独立样本外支持（2022-2026）的窗口。

== 它治什么、不治什么 ==
治「币间风险预算不均」：CRV 年化波动 110%、BTC 58%，同样 10% 的钱风险差 2 倍。
**不治系统性风险**：2022 年 10 币同跌，VT 和等资金都亏 50% 上下，只差 5pp。
相关性均 0.67、10 币同日全跌的概率 19.6% —— 这类风险得靠总仓位解决，不是这个模块。

== 代价（写在这里，免得以后忘了）==
2021 那种「暴涨伴随波动飙升」的行情里，本模块会砍掉赢家的后半段：
VT60 当年 +151%，等资金 +363%，少赚 212pp。这是设计上的取舍，不是 bug。

== 开关 ==
默认开启。想关：环境变量 SKEL_VT=0（_signal.json 里会写明 vt.enabled=false）。
其余参数也可用环境变量覆盖，见下方常量。
"""
import datetime as dt
import math
import os

# ---------- 参数（默认值 = 已验证的 VT60 档）----------
ENABLED = os.environ.get("SKEL_VT", "1") != "0"
LB = int(os.environ.get("SKEL_VT_LB", "60"))              # 波动回看天数
TARGET = float(os.environ.get("SKEL_VT_TARGET", "0.60"))  # 目标年化波动
LO = float(os.environ.get("SKEL_VT_LO", "0.30"))          # 系数下限（最高波动时）
HI = float(os.environ.get("SKEL_VT_HI", "2.00"))          # 系数上限（最低波动时）
REBAL_DAYS = int(os.environ.get("SKEL_VT_REBAL", "7"))    # 系数刷新周期（天）

# κ：把平均总敞口拉回 0.686×（等资金口径）的归一化常数。
# 来源：10 币 2020-09~2026-11 全样本，VT60 原始平均敞口 0.562 → 0.686/0.562。
# 它是一个**全局常数**，不是逐币拟合出来的，所以不构成过拟合风险；
# 但也别指望它长期精确 —— 每季度看一眼实际平均敞口有没有漂太多即可。
KAPPA = float(os.environ.get("SKEL_VT_KAPPA", "1.22"))

# 周历锚点（周一）。用「距锚点第几个 7 天」决定何时刷新系数，
# 这样同一个 4H 时刻无论跑多少次、在哪台机器跑，算出来的系数都一样。
# 若改用「距上次刷新满 7 天」，就变成有状态的了 —— 进程重启会漂。
ANCHOR = dt.date(2020, 1, 6)


def _daily_closes(b4):
    """4H bar → 日收盘序列 [(YYYY-MM-DD, close)]，取每日最后一根。

    日期统一截成 10 位：Binance 的 bar 带 "HH:MM"，Hyperliquid 只有日期，
    不截会在查表时匹配不上。
    """
    dc = []
    for b in b4:
        d = b["date"][:10]
        if not dc or dc[-1][0] != d:
            dc.append([d, b["close"]])
        else:
            dc[-1][1] = b["close"]
    return dc


def sigma_series(b4, lb=None):
    """每个交易日的年化波动率 {YYYY-MM-DD: σ}。

    严格只用**当日之前**的 lb 个日收益（rets[max(0,i-lb):i]），无未来函数。
    历史不足 max(8, lb/3) 个样本时不给值 —— 新上线币要等约 20 天才开始缩放。
    """
    lb = LB if lb is None else lb
    dc = _daily_closes(b4)
    rets = [None] + [math.log(dc[i][1] / dc[i - 1][1]) for i in range(1, len(dc))]
    out = {}
    need = max(8, lb // 3)
    for i in range(len(dc)):
        seg = [r for r in rets[max(0, i - lb):i] if r is not None]
        if len(seg) >= need:
            m = sum(seg) / len(seg)
            sd = math.sqrt(sum((x - m) ** 2 for x in seg) / len(seg))
            out[dc[i][0]] = sd * math.sqrt(365)
    return out


def _bucket(d):
    """该日期属于第几个 7 天周期。无状态、可复现。"""
    y, m, dd = (int(x) for x in d[:10].split("-"))
    return (dt.date(y, m, dd) - ANCHOR).days // REBAL_DAYS


def coef_series(b4, lb=None):
    """逐 bar 的缩放系数（与 b4 等长）。回测/复核用，生产只用最后一位。"""
    if not ENABLED:
        return [1.0] * len(b4)
    sm = sigma_series(b4, lb)
    out, cur, bk = [], 1.0, None
    for b in b4:
        d = b["date"][:10]
        nb = _bucket(d)
        if nb != bk:
            v = sm.get(d)
            if v and v > 1e-9:
                cur = min(HI, max(LO, TARGET / v))
            bk = nb
        out.append(cur)
    return out


def coef_now(b4):
    """最新一根已收盘 bar 的缩放系数，以及对应的 σ（给界面显示用）。"""
    if not ENABLED:
        return 1.0, None
    cs = coef_series(b4)
    sm = sigma_series(b4)
    return cs[-1], sm.get(b4[-1]["date"][:10])


def amount(nominal, coef):
    """把名义金额按系数缩放。正负号原样保留（空头照样缩）。"""
    return nominal * coef * KAPPA if ENABLED else nominal


def describe(coef, sigma):
    """一句人话，用于信号表备注。"""
    if not ENABLED:
        return "波动目标化已关闭"
    if sigma is None:
        return "历史不足，暂不缩放"
    return f"σ{sigma * 100:.0f}% → 系数 {coef:.2f}×"


if __name__ == "__main__":
    print(f"VT{'开' if ENABLED else '关'}｜lb={LB} 天 TARGET={TARGET:.2f} "
          f"LO={LO:.2f} HI={HI:.2f} κ={KAPPA:.2f} 刷新={REBAL_DAYS} 天")
    print("系数表：" + "  ".join(
        f"σ={s:.0%}→{min(HI, max(LO, TARGET / s)):.2f}"
        for s in (0.4, 0.6, 0.8, 1.0, 1.2)))
