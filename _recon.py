#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_recon.py —— 只读对账 + 资金日报（执行自动化 P0，**不下任何单**）

为什么先做这个，而不是直接上"自动下单机器人"：
  1. `_fills.jsonl` 现在是空的，所以「滑点 5bp」「容量 3.6 万 U」全是假想值。
     每天对一次账，两周后就有真实成交数据，那时容量不用估、滑点不用假设。
  2. 只读权限（Enable Reading）的 API key **即使泄露也搬不走钱**，
     所以这一期的风险是零。用它先把链路、时区、对账口径全部跑通。
  3. 对账是下单机器人的地基：下单必须基于「目标 − 实际」的差。
     这一步没做，自动下单就是在盲打。

用到的接口（币安 USDT-M 合约，全部只读，HMAC 签名）：
  GET /fapi/v2/account        钱包余额、可用、未实现盈亏
  GET /fapi/v2/positionRisk   逐币持仓、开仓价、标记价、强平价、杠杆

⚠ 只勾 Enable Reading。**不要勾合约交易、不要勾提现、不要勾划转。**
   （子账号的资金隔离全靠权限，不靠子账号本身 —— 同主账户下可以互转。）

配置 `_cex.json`（与 _mail.json 同目录，记得进 .gitignore）：
    {"exchange": "binance",
     "key": "子账号的 API Key", "secret": "子账号的 Secret",
     "equity0": 2000.0,
     "note": "regime2000 子账号"}
equity0 = 起始权益，用于算累计收益。策略本体用 portfolio_config.CAPITAL，
两者应该大致相等 —— 偏离超过 10% 时报告会提醒你同步（否则目标仓位算错）。

服务器要么在境外，要么给 urllib 配代理（环境变量 HTTPS_PROXY）。
国内直连 fapi.binance.com 会 502，这是已知的。

用法：
    python _recon.py --dry       # 不联网，只检查配置与将要请求的 URL
    python _recon.py --no-mail   # 拉一次、写 _recon.json，不发信
    python _recon.py             # 拉一次、写 json、发日报邮件
"""
import datetime as dt
import html
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import portfolio_config as PC

CFG = "_cex.json"
SIG = "_signal.json"
OUT = "_recon.json"
BJ = dt.timezone(dt.timedelta(hours=8))

# 对账容差：绝对 50U 或目标的 25%，取大者。
# 为什么这么松：底仓/加仓分两次成交、VT 系数每周重算、闸门打折都会造成
# 目标与实际天然不一致。太紧会天天报警，报警一多就没人看了 —— 那比不报更糟。
TOL_ABS = 50.0
TOL_REL = 0.25
# 权益与 CAPITAL 偏离超过这个比例就提醒同步（否则 target 用的是错的本金）
CAP_DRIFT_WARN = 0.10


# ---------------- 配置 ----------------
def load_cfg():
    """凭据来源，优先级：DPAPI 密文 > 明文 `_cex.json`（旧路径，保留兼容）。

    2026-11-27 起以密文为主：`binance_account.py set` 把 key 加密存到
    `%LOCALAPPDATA%\\regime_dashboard\\cex.enc`，**在仓库之外**。
    明文 `_cex.json` 仍认，但它会躺在一个 public 仓库的工作区里 —— 别再新增这种文件。
    """
    try:
        import binance_account as BA
        c = BA.load_creds()
        if c:
            c.setdefault("exchange", "binance")
            c.setdefault("equity0", PC.CAPITAL)
            c["_src"] = "DPAPI 密文"
            return c
    except Exception as e:                                   # noqa: BLE001
        print(f"[配置] 读取密文失败：{type(e).__name__}: {e}")
    p = os.path.join(HERE, CFG)
    if not os.path.exists(p):
        return None
    try:
        c = json.load(open(p, encoding="utf-8"))
    except Exception as e:                                  # noqa: BLE001
        print(f"[配置] {CFG} 解析失败：{e}")
        return None
    miss = [k for k in ("key", "secret") if not c.get(k)]
    if miss:
        print(f"[配置] {CFG} 缺少字段：{', '.join(miss)}")
        return None
    c.setdefault("exchange", "binance")
    c.setdefault("equity0", PC.CAPITAL)
    c["_src"] = f"明文 {CFG}"
    return c


# ---------------- 本地目标 ----------------
def local_view():
    """本地信号层的「应有仓位」与工单状态。"""
    try:
        sig = json.load(open(SIG, encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None
    tgt = {o["c"]: float(o.get("target") or 0) for o in sig.get("coins", [])}
    try:
        tk = json.load(open("_tickets.json", encoding="utf-8"))
        open_n = sum(1 for i in tk["items"] if i["status"] == "open")
    except Exception:                                        # noqa: BLE001
        open_n = 0
    return {"asof_bj": sig.get("asof_bj"), "bar4h": sig.get("bar4h"),
            "capital": sig.get("capital"), "target": tgt, "open_tickets": open_n}


# ---------------- 交易所快照 ----------------
def cex_view(cfg):
    """现货 + USDT-M 合约。**签名与解析全部复用 binance_account** ——

    原来这里另写了一份 `signed_get`（只覆盖合约），于是同一个"账户实际持仓"存在两个
    实现。这正是本项目反复踩的「同一件事两处各算一遍」，第 N+1 次。现在只留一份。

    合约端点允许失败：只读现货的 key（比如币安的「税务报告 API」）拿不到 futures 权限，
    `/fapi/v2/*` 会 403。这时仍要能出对账报告 —— 只是永续那一层留空并标注，
    而不是整个脚本崩掉、什么也没告诉你。
    """
    import binance_account as BA
    spot, _acc = BA.spot_balances(cfg)
    perp_ok, perp_err = True, None
    try:
        cx = BA.futures_state(cfg)
    except Exception as e:                                   # noqa: BLE001
        perp_ok, perp_err = False, str(e)
        cx = {"wallet": 0.0, "upnl": 0.0, "avail": 0.0, "maint": 0.0, "pos": {}}
    cx["spot"] = {k: v for k, v in spot.items() if k != "USDT"}
    cx["cash_usdt"] = float(spot.get("USDT", {}).get("qty", 0.0))
    cx["spot_value"] = float(sum(v["usd"] for v in spot.values()))
    cx["perp_ok"] = perp_ok
    cx["perp_err"] = perp_err
    return cx


# ---------------- 对账 ----------------
def reconcile(lv, cx, cfg):
    """**「实际」必须是现货 + 永续两层之和。**

    这里原来只算合约持仓。而底仓（1.0× 那一层）是在**现货**的：XRP 目标 423 U，
    现货 282 + 永续 141；只数永续会把每个 UP 币都判成「少 282 U」，
    整张表漂红却全都不是真问题 —— 警告一多就没人看了，那比不报更糟。

    target 本身是「峰值 = 底仓 + 加仓层」，所以判据是 `target ≈ 现货 + 永续`，
    不需要再拆腿；拆腿只在显示时做（看这一笔该在现货还是永续）。
    """
    rows, bad = [], []
    sp = cx.get("spot") or {}
    coins = sorted(set(lv["target"]) | set(cx["pos"]) | set(sp))
    for c in coins:
        t = lv["target"].get(c, 0.0)
        p = cx["pos"].get(c)
        s_usd = float(sp.get(c, {}).get("usd", 0.0))
        p_usd = p["notional"] if p else 0.0
        a = s_usd + p_usd
        d = a - t
        tol = max(TOL_ABS, abs(t) * TOL_REL)
        ok = abs(d) <= tol
        rows.append({"c": c, "target": t, "actual": a, "diff": d, "tol": tol,
                     "ok": ok, "pos": p, "spot": s_usd, "perp": p_usd})
        if not ok:
            bad.append(c)
    eq = cx["spot_value"] + cx["wallet"] + cx["upnl"]
    e0 = float(cfg["equity0"])
    # 用**信号自己记录的本金**做基准，不是 PC.CAPITAL ——
    # 目标仓位是 _signal.json 用当时的 capital 算出来的。若你改了 CAPITAL 但还没重跑
    # run_signal.py，两者的差就是「目标整体偏了几倍」，这是最危险的一种不一致。
    sig_cap = float(lv.get("capital") or PC.CAPITAL)
    drift = eq / sig_cap - 1 if sig_cap else 0
    return {"rows": rows, "bad": bad, "equity": eq, "equity0": e0,
            "ret": eq / e0 - 1 if e0 else 0,
            "cap_drift": drift, "sig_cap": sig_cap,
            "cap_stale": abs(sig_cap - PC.CAPITAL) > 0.5,
            "gross": sum(abs(r["actual"]) for r in rows),
            "net": sum(r["actual"] for r in rows),
            "spot_value": cx["spot_value"], "cash": cx["cash_usdt"],
            "wallet": cx["wallet"], "upnl": cx["upnl"], "avail": cx["avail"]}



# ---------------- 报告 ----------------
def num(v, n=0):
    return f"{v:,.{n}f}"


def html_report(lv, cx, rc, cfg):
    ts = dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    kpi = [("账户权益", f"{num(rc['equity'])} U", "#1565c0"),
           ("现货估值", f"{num(rc['spot_value'])} U", "#00796b"),
           ("合约钱包", f"{num(rc['wallet'])} U", "#5e35b1"),
           ("累计", f"{rc['ret'] * 100:+.1f}%", "#c62828" if rc["ret"] >= 0 else "#2e7d32"),
           ("浮动盈亏", f"{cx['upnl']:+,.0f} U", "#c62828" if cx["upnl"] >= 0 else "#2e7d32"),
           ("总敞口", f"{num(rc['gross'])} U（{rc['gross'] / max(rc['equity'], 1):.2f}×）", "#6a1b9a")]
    cards = "".join(
        f'<td style="padding:10px 12px;border:1px solid #e3e7ec;border-radius:6px">'
        f'<div style="font-size:11px;color:#77808c">{k}</div>'
        f'<div style="font-size:18px;font-weight:600;color:{c};margin-top:2px">{v}</div></td>'
        for k, v, c in kpi)

    rows = ""
    for r in sorted(rc["rows"], key=lambda x: -abs(x["actual"])):
        p = r["pos"]
        if not p and abs(r["target"]) < 1:
            continue                       # 双方都空，不占版面
        if p and p["liq"] > 0:
            liq = f"{p['liq']:,.2f}（距 {(p['liq'] / p['mark'] - 1) * 100:+.1f}%）"
        else:
            liq = "—"
        mark_s = num(p["mark"], 4) if p else "—"
        upnl_s = f"{p['upnl']:+,.0f}" if p else "—"
        bg = "#fff3e0" if not r["ok"] else "#fff"
        rows += (
            f'<tr style="background:{bg}">'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee"><b>{r["c"]}</b></td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right">{num(r["target"])}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right;color:#00796b">{num(r["spot"])}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right;color:#5e35b1">{num(r["perp"])}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right">{num(r["actual"])}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right;'
            f'color:{"#e65100" if not r["ok"] else "#90a4ae"}">{r["diff"]:+,.0f}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right;color:#90a4ae">{mark_s}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right">{upnl_s}</td>'
            f'<td style="padding:7px 9px;border-bottom:1px solid #eee;text-align:right;'
            f'font-size:12px;color:#5d4037">{liq}</td></tr>')

    warn = ""
    if rc.get("cap_stale"):
        warn += (f'<div style="border-left:4px solid #6a1b9a;background:#f3e5f5;padding:9px 12px;'
                 f'margin:12px 0;font-size:13px;color:#4a148c">'
                 f'⚠ <b>信号是用旧本金算的</b>：<code>_signal.json</code> 里记的是 '
                 f'{num(rc["sig_cap"])} U，而 <code>portfolio_config.CAPITAL</code> 已是 '
                 f'{num(PC.CAPITAL)} U。<b>目标和名义金额全部按旧本金</b> —— '
                 f'先重跑一次 <code>run_signal.py</code> 再照这张表下单。</div>')
    if abs(rc["cap_drift"]) > CAP_DRIFT_WARN:
        warn += (f'<div style="border-left:4px solid #ef6c00;background:#fff8e1;padding:9px 12px;'
                 f'margin:12px 0;font-size:13px;color:#e65100">'
                 f'⚠ 账户权益 {num(rc["equity"])} U 与信号本金 {num(rc["sig_cap"])} U '
                 f'偏离 {rc["cap_drift"] * 100:+.1f}%，目标仓位是按后者算的。'
                 f'<b>请把 CAPITAL 同步成当前权益并重跑信号</b>，否则每次下单量都会偏。</div>')
    if cx.get("perp_ok") is False:
        warn += ('<div style="border-left:4px solid #6a1b9a;background:#f3e5f5;padding:9px 12px;'
                 'margin:12px 0;font-size:13px;color:#4a148c">'
                 '⚠ <b>合约端点读不到，本报告的「实际」只含现货。</b>'
                 f'原因：{str(cx.get("perp_err"))[:120]}<br>'
                 '只读现货的 key（例如币安「税务报告 API」）没有 futures 权限，'
                 '这是正常的。永续加仓层这一半仍是推定值 —— 别把「目标 vs 实际」的差额'
                 '当成要下的单。</div>')
    if rc["bad"]:
        warn += (f'<div style="border-left:4px solid #c62828;background:#ffebee;padding:9px 12px;'
                 f'margin:12px 0;font-size:13px;color:#b71c1c">'
                 f'⚠ 有 {len(rc["bad"])} 个币的实际持仓偏离目标超过容差：'
                 f'{", ".join(rc["bad"])}。<b>先别下单</b>，看是漏成交、还是手动改过仓。</div>')
    else:
        warn += ('<div style="border-left:4px solid #2e7d32;background:#e8f5e9;padding:9px 12px;'
                 'margin:12px 0;font-size:13px;color:#1b5e20">✓ 全部持仓与信号目标一致。</div>')

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#f4f6f8;font-family:'Segoe UI','PingFang SC',sans-serif;font-size:13px;color:#1c2026">
<div style="max-width:900px;margin:0 auto;padding:20px 16px 40px;background:#fff">
<div style="font-size:12px;color:#77808c;letter-spacing:.5px">资金日报 · 只读对账（P0，不下单）</div>
<h1 style="font-size:19px;margin:6px 0 4px">加密趋势策略 · {ts}（北京）</h1>
<div style="font-size:12px;color:#77808c;margin-bottom:14px">
交易所 {cfg.get('exchange')}　信号 bar {lv['bar4h']}　未结算工单 {lv['open_tickets']} 张
</div>
<table style="width:100%;border-collapse:separate;border-spacing:6px 0"><tr>{cards}</tr></table>
{warn}
<h2 style="font-size:14px;margin:22px 0 8px">持仓对账（目标来自本地信号，实际来自交易所）</h2>
<table style="width:100%;border-collapse:collapse">
<tr style="background:#f5f5f5"><th style="padding:7px 9px;text-align:left;font-size:12px">币</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">目标 U</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">现货</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">永续</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">实际 U</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">差额</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">标记价</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">浮盈 U</th>
<th style="padding:7px 9px;text-align:right;font-size:12px">强平价</th></tr>
{rows}</table>
<div style="font-size:12px;color:#77808c;margin-top:10px;line-height:1.8">
容差 = max({TOL_ABS:.0f}U, 目标的 {TOL_REL * 100:.0f}%)。橙底 = 超容差。<br>
「目标」是策略要求的该币名义金额（负数=空头），**等于现货 + 永续两层之和** ——
底仓在现货、加仓层在永续，只数永续会把每个多头币都误判成欠配。<br>
「差额」才是要下的单。数据源：{cfg.get('_src', '未知')}。<br>
本报告只读，不含任何下单动作。止损单请自行在交易所挂，进程挂了它不会替你止损。
</div></div></body></html>"""


def send_mail(html_body, subject):
    """复用 notify_mail 的配置加载规则（_mail.json / mail_config.json 都认）。"""
    try:
        import notify_mail as NM
    except Exception as e:                                   # noqa: BLE001
        print(f"[邮件] 导入 notify_mail 失败：{e}")
        return False
    cfg = NM.load_cfg()
    if not cfg:
        print("[邮件] 找不到 _mail.json，跳过发信")
        return False
    import smtplib
    import ssl
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr, formatdate
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("资金日报", cfg["user"]))
    msg["To"] = ", ".join(cfg["to"])
    msg["Date"] = formatdate(localtime=True)
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port", 465)), context=ctx, timeout=30) as s:
        s.login(cfg["user"], cfg["pass"])
        s.sendmail(cfg["user"], cfg["to"], msg.as_string())
    print(f"[邮件] 已发送 → {', '.join(cfg['to'])}")
    return True


def main():
    dry = "--dry" in sys.argv
    no_mail = "--no-mail" in sys.argv

    lv = local_view()
    if lv is None:
        print(f"[本地] 读不到 {SIG} —— 先跑一次 run_signal.py")
        return 1

    cfg = load_cfg()
    if cfg is None:
        print("[配置] 没有可用凭据。二选一：")
        print("  推荐  python binance_account.py set    "
              "（DPAPI 加密，存在仓库之外的 %LOCALAPPDATA%）")
        print(f"  旧路 建 {CFG}（明文，会躺在 public 仓库的工作区里，不建议）")
        return 1

    if dry:
        print(f"[dry] 交易所 {cfg['exchange']}　凭据来源 {cfg.get('_src', '?')}　"
              f"起始权益 {cfg['equity0']:,.0f} U")
        print(f"[dry] 本地信号 {lv['asof_bj']}　bar {lv['bar4h']}　"
              f"CAPITAL {lv['capital']:,.0f} U　未结算工单 {lv['open_tickets']}")
        print("[dry] 将请求 /api/v3/account + /api/v3/ticker/price（现货）"
              " 与 /fapi/v2/account + /fapi/v2/positionRisk（合约），全部签名只读")
        print(f"[dry] 非零目标 {sum(1 for v in lv['target'].values() if abs(v) > 0.5)} 个币")
        return 0

    try:
        cx = cex_view(cfg)
    except Exception as e:                                   # noqa: BLE001
        print(f"[交易所] 请求失败：{type(e).__name__}: {e}")
        print("  国内直连币安会 502，确认代理在跑；代理端口会漂移，"
              "需要指定就设 BINANCE_PROXY")
        return 1

    rc = reconcile(lv, cx, cfg)
    snap = {"ts_bj": dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
            "bar4h": lv["bar4h"], "equity": rc["equity"], "ret": rc["ret"],
            "spot_value": rc["spot_value"], "wallet": rc["wallet"],
            "upnl": rc["upnl"], "avail": rc["avail"],
            "gross": rc["gross"], "net": rc["net"], "bad": rc["bad"],
            "rows": [{k: r[k] for k in ("c", "target", "spot", "perp",
                                        "actual", "diff", "ok")}
                     for r in rc["rows"]]}
    json.dump(snap, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"权益 {rc['equity']:,.0f} U（{rc['ret'] * 100:+.1f}%）"
          f"　= 现货 {rc['spot_value']:,.0f} + 合约钱包 {rc['wallet']:,.0f}"
          f" + 浮盈 {rc['upnl']:+,.0f}")
    print(f"总敞口 {rc['gross']:,.0f} U（{rc['gross'] / max(rc['equity'], 1):.2f}×）　"
          f"可用保证金 {rc['avail']:,.0f} U")
    print(f"偏离超容差：{rc['bad'] or '无'}")
    print(f"已写 {OUT}")

    if no_mail:
        return 0
    subj = (f"【资金日报】{num(rc['equity'])}U {rc['ret'] * 100:+.1f}%"
            + (f" ⚠{len(rc['bad'])}币偏离" if rc["bad"] else "")
            + f" | {dt.datetime.now(BJ).strftime('%m-%d %H:%M')}")
    send_mail(html_report(lv, cx, rc, cfg), subj)
    return 0


if __name__ == "__main__":
    sys.exit(main())
