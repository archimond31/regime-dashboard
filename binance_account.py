#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""binance_account.py —— 只读账户快照：现货 + USDT-M 合约（不下任何单）

## 这个模块解决什么

`live_signals.spot_held()` 现在是从**工单台账**推「手上真有多少现货」。这有三处必然失真：

  1. 没有 `fill_notional` 时只能「按建议额收窄到当前目标」猜 → 权重一变猜出来的数就漂
     （2026-11-27 实测 1,004 → 1,056）；
  2. 账户里有、台账里没有的仓，系统**永不发平仓/止损**（BTC/ETH/BNB 11-09 那三笔，
     页面还标「未建仓」，看着像本来就没这笔仓）；
  3. `PC.CAPITAL` 是写死的 2,000，账户权益涨了跌了它不知道 → 目标仓位整体偏。

接上只读 API 之后，这三条全部变成「读到的事实」而不是「推出来的数」。

## 安全模型（**先读这一段再动手**）

**仓库是 public。密钥绝不能进仓库，一行都不行。**

- 存储位置：`%LOCALAPPDATA%\\regime_dashboard\\cex.enc`，**在仓库之外**，
  就算哪天 `git add -A` 也不可能带进去。
- 加密方式：Windows DPAPI（`CryptProtectData`），密钥材料由 Windows 用**当前用户**的
  登录凭据派生。含义是：
    · 换一台机器拷过去 → 解不开；
    · 同一个机器换一个 Windows 用户 → 解不开；
    · 不需要记密码，也就没有「密码写在代码里」这个次生灾难。
  代价也在这里：**重装系统或换用户会丢**，那就重新 `set` 一次（重跑不花钱）。
- 明文只在内存里存在一次请求的时间，不落盘、不进日志、不进 json。
- 打印一律走 `mask()`（`AbCdEf…wXyZ`），任何地方都不出现完整 secret。

## 权限清单（照这个勾，勾错会被本模块拒绝保存）

  子账号 → API 管理 → 创建 API：
    ✅ Enable Reading                     ← 唯一需要的
    ✅ Restrict access to trusted IPs only ← 强烈建议（填你的出口 IP）
    ❌ Enable Spot & Margin Trading        ← 关掉
    ❌ Enable Futures                      ← 关掉
    ❌ Enable Withdrawals                  ← 关掉（这条最要命）
    ❌ Enable Internal Transfer            ← 关掉（子账号间可以互转，等于绕过提现限制）

`set` 会调 `GET /sapi/v1/account/apiRestrictions` 核对上面四项，任何一项开着就**拒绝保存**
（要硬存加 `--force`，但只要认读就该把权限改掉）。

## 接口

    GET /sapi/v1/account/apiRestrictions   权限自检（不需要签名之外的东西）
    GET /api/v3/account                    现货余额（签名）
    GET /api/v3/ticker/price               现货估值（公开）
    GET /fapi/v2/account                   合约钱包/可用/浮盈（签名）
    GET /fapi/v2/positionRisk              逐币持仓/开仓价/标记价/强平价（签名）

## 用法

    python binance_account.py set         # 交互式录入（getpass，不进 shell 历史）
    python binance_account.py set --from %TEMP%\\k.txt
                                          # 从文件读（两行或 {"key","secret"}），读完覆写+删除。
                                          # 给「终端会把输入回显」的情况兜底（Git Bash/mintty 常见）
    python binance_account.py test        # 权限自检 + 拉一次，只打印掩码
    python binance_account.py snap        # 拉一次并写 .cex_snapshot.json
    python binance_account.py show        # 只看本地存了什么（key 打码）
    python binance_account.py rm          # 删掉本地密文

⚠ **不要把 key 贴进聊天窗口 / 微信 / 任何 Agent 的对话框。**
   对话记录是另一份**没有加密**的持久化副本（本机 jsonl + 服务商侧），
   而且它不在你的 `%LOCALAPPDATA%` 里 —— 那样等于把最脏的一份放在最安全的那层外面。
   要在本机输入，或者 `--from` 传文件。

## 网络

币安域名国内直连会 502。代理端口**会漂移**（2026-11 实测 10809 → 7890），
所以这里不写死：先读 `HTTPS_PROXY`，没有就按 7890 → 10809 → 7897 → 10808 → 1080
顺序做 TCP 探测取第一个通的。都探不到才直连（然后大概率 502，会给你明确提示）。
"""
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

SNAP = os.path.join(HERE, ".cex_snapshot.json")
APP = "regime_dashboard"
ENTROPY = b"regime_dashboard/cex/v1"

SPOT_HOSTS = ["https://api.binance.com", "https://api1.binance.com",
              "https://api-gcp.binance.com", "https://api4.binance.com"]
FAPI_HOSTS = ["https://fapi.binance.com", "https://fapi1.binance.com"]

# 探测顺序 = 本机历史上真实出现过的端口顺序。加新端口就往后加，别删旧的。
PROXY_PORTS = (7890, 10809, 7897, 10808, 1080, 8080)

# 现货里这些不是「一个币」，不参与估值/对账
SKIP_ASSET = {"USDT", "BUSD", "USDC", "FDUSD", "TUSD", "DAI", "LDUSDT"}


# ============================================================
# 一、DPAPI 加解密
# ============================================================
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _mkblob(data):
    """返回 (BLOB, 持有内存的 buffer)。buffer 必须一直被引用，否则 GC 走后指针就悬了 ——
    这是 ctypes 里最经典的 use-after-free，报错形式是「解密出来是乱码」而不是崩溃。"""
    if not data:
        data = b"\0"
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data, protect):
    din, _keep_in = _mkblob(data)
    ent, _keep_ent = _mkblob(ENTROPY)
    dout = _BLOB()
    if protect:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(din), "regime_dashboard", ctypes.byref(ent),
            None, None, 0, ctypes.byref(dout))
    else:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(din), None, ctypes.byref(ent),
            None, None, 0, ctypes.byref(dout))
    if not ok:
        raise OSError("DPAPI 失败（错误码 %d）" % ctypes.GetLastError())
    try:
        return ctypes.string_at(dout.pbData, dout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dout.pbData)


# ============================================================
# 二、凭据存储
# ============================================================
def store_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP, "cex.enc")


def mask(s):
    if not s:
        return "—"
    if len(s) <= 12:
        return "*" * len(s)
    return f"{s[:6]}…{s[-4:]}"


def save_creds(key, secret, extra=None):
    """加密写盘。**先写临时文件再原子替换** —— 直接覆盖写到一半断电，密文就废了，
    而你可能以为只是「key 过期了」，去交易所重新建一个。"""
    p = store_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    rec = dict(key=key, secret=secret, saved_bj=now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    if extra:
        rec.update(extra)
    blob = _dpapi(json.dumps(rec, ensure_ascii=False).encode("utf-8"), True)
    tmp = p + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, p)
    # 尽力收紧 ACL（失败不致命：文件已在用户目录且是密文）
    try:
        os.system(f'icacls "{p}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
    except Exception:                                        # noqa: BLE001
        pass
    return p


def load_creds():
    p = store_path()
    if not os.path.exists(p):
        return None
    try:
        raw = _dpapi(open(p, "rb").read(), False)
        c = json.loads(raw.decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        print(f"[凭据] 解不开 {p}：{type(e).__name__}: {e}")
        print("       换了机器/Windows 用户，或者重装过系统 → 重新 `set` 一次即可")
        return None
    if not c.get("key") or not c.get("secret"):
        print("[凭据] 密文里缺少 key/secret，重新 `set`")
        return None
    return c


# ============================================================
# 三、网络
# ============================================================
def now_bj():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def _probe(port, timeout=0.5):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:                                        # noqa: BLE001
        return False
    finally:
        s.close()


_PROXY = "?"


def proxy_url():
    """`BINANCE_PROXY` > 环境变量（探通才采信）> 按 PROXY_PORTS 探测。结果缓存。

    两个坑，都是实测踩出来的：

    1. **本会话的 `HTTPS_PROXY=http://127.0.0.1:59826` 是 WorkBuddy 沙箱注入的**，
       在你自己的终端里不存在。无条件采信它 → 脚本在我这儿"能跑"、在你那儿超时。
       所以本地地址必须先探通。

    2. **Windows 的环境变量名不区分大小写。** `os.environ` 会把 `HTTPS_PROXY` 和
       `https_proxy` 合并成同一个键，后写的那个赢。代理工具写大写、shell profile 写小写
       是常见组合，合并结果就不可预测 —— 实测拿到的就是小写那个（59826）。
       想指定就用 `BINANCE_PROXY`，显式覆盖一切猜测。
    """
    global _PROXY
    if _PROXY != "?":
        return _PROXY
    v = (os.environ.get("BINANCE_PROXY") or "").strip()
    if v:
        _PROXY = v if "://" in v else "http://" + v
        return _PROXY
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if not v:
            continue
        m = urllib.parse.urlparse(v if "://" in v else "http://" + v)
        try:
            port = m.port or 8080
        except Exception:                                    # noqa: BLE001
            continue
        host = m.hostname or "127.0.0.1"
        if host in ("127.0.0.1", "localhost") and not _probe(port):
            continue                       # 幽灵代理：变量在、进程不在
        _PROXY = v
        return v
    for port in PROXY_PORTS:
        if _probe(port):
            _PROXY = f"http://127.0.0.1:{port}"
            return _PROXY
    _PROXY = ""
    return ""


def _open(req, timeout=20):
    px = proxy_url()
    if px:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": px, "http": px}))
        return op.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _err_hint(code, msg, kind):
    m = (msg or "").lower()
    hints = []
    if code == -2015 or "invalid api-key" in m or "permissions for action" in m:
        hints.append("key 无效 / IP 不在白名单 / 该 key 没开读数权限")
        hints.append("  → 去币安核对：Enable Reading 是否勾上；若勾了 IP 白名单，"
                     "把当前出口 IP 加进去（本机走代理，出口 IP 不是你家宽带）")
    if code == -1022 or "signature" in m:
        hints.append("签名不匹配 → secret 抄错（注意首尾空格、有没有漏字符）")
    if code == -1021 or "timestamp" in m:
        hints.append("时间戳超窗 → 本机时钟偏了，校一下 Windows 时间同步")
    if code == 0 or code is None:
        hints.append("连不上 → 国内直连会 502，确认代理在跑（本模块会自动探端口）")
    return (f"[{kind}] {code} {msg}\n  " + "\n  ".join(hints)) if hints else \
           f"[{kind}] {code} {msg}"


def request(kind, path, params=None, key=None, secret=None, timeout=20):
    """kind = 'spot' | 'fapi'。secret 有值就签名。返回解析后的 json。

    域名逐个试：币安有多个入口，某个 502/超时换下一个。**只在网络层失败时换**，
    签名错（-2015/-1022）换域名也没用，必须立刻抛出去 —— 否则会把「key 错了」
    误报成「网络问题」，让你去查代理。
    """
    hosts = SPOT_HOSTS if kind == "spot" else FAPI_HOSTS
    q = dict(params or {})
    last = None
    for i, h in enumerate(hosts):
        try:
            if secret:
                q["timestamp"] = int(time.time() * 1000)
                q.setdefault("recvWindow", 5000)
                qs = urllib.parse.urlencode(q)
                sig = hmac.new(secret.encode(), qs.encode(),
                               hashlib.sha256).hexdigest()
                url = f"{h}{path}?{qs}&signature={sig}"
            else:
                qs = urllib.parse.urlencode(q)
                url = f"{h}{path}" + (f"?{qs}" if qs else "")
            req = urllib.request.Request(url, headers={"X-MBX-APIKEY": key or ""})
            with _open(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            return h, json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(body)
            except Exception:                                # noqa: BLE001
                j = {}
            code, msg = j.get("code"), j.get("msg") or body[:120]
            # 业务错误：不换域名，直接抛
            if code is not None:
                raise RuntimeError(_err_hint(code, msg, kind))
            last = f"HTTP {e.code} {msg}"
        except Exception as e:                               # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i + 1 < len(hosts):
            print(f"  [{kind}] {h} 不通（{last}），换下一个入口 …")
    raise RuntimeError(_err_hint(None, last or "全部入口不可达", kind))


# ============================================================
# 四、业务读取
# ============================================================
RO_CHECKS = [
    ("enableReading", True, "读数"),
    ("enableSpotAndFuturesTrading", False, "现货/合约交易"),
    ("enableWithdrawals", False, "提现"),
    ("enableInternalTransfer", False, "内部划转"),
]


def api_restrictions(creds):
    """权限自检。返回 (ok, rows)。ok=False 表示这个 key 能搬钱或能下单。"""
    _h, j = request("spot", "/sapi/v1/account/apiRestrictions",
                    key=creds["key"], secret=creds["secret"])
    rows, ok = [], True
    for k, want, lab in RO_CHECKS:
        got = bool(j.get(k))
        bad = (got != want)
        if bad:
            ok = False
        rows.append({"field": k, "label": lab, "want": want, "got": got, "bad": bad})
    rows.append({"field": "ipRestrict", "label": "IP 白名单", "want": True,
                 "got": bool(j.get("ipRestrict")), "bad": not bool(j.get("ipRestrict"))})
    return ok, rows, j


def spot_balances(creds):
    """现货非零余额 + 估值。估值用公开行情，不需要额外权限。"""
    _h, acc = request("spot", "/api/v3/account",
                      key=creds["key"], secret=creds["secret"])
    bal = {}
    for b in acc.get("balances", []):
        a = b["asset"]
        qty = float(b["free"]) + float(b["locked"])
        if a in SKIP_ASSET or qty == 0:
            continue
        if a == "USDT":
            bal["USDT"] = {"qty": qty, "usd": qty, "px": 1.0}
            continue
        bal[a] = {"qty": qty, "usd": 0.0, "px": None}
    coins = [a for a in bal if a != "USDT"]
    if coins:
        px = _prices(coins)
        for a in coins:
            p = px.get(a)
            bal[a]["px"] = p
            bal[a]["usd"] = (bal[a]["qty"] * p) if p else 0.0
    return bal, acc


def _prices(coins):
    """一次请求拿全部 `{COIN}USDT` 最新价。

    不用 `/ticker/price` 全量（3,000+ 条，慢且没必要），用 `symbols=[...]`。
    注意 `symbols` 要传 JSON 数组字符串并且 **URL 编码**（花括号和引号都得转），
    否则币安返回 -1100 参数错误。
    """
    syms = [f"{c}USDT" for c in coins]
    q = {"symbols": json.dumps(syms, separators=(",", ":"))}
    _h, arr = request("spot", "/api/v3/ticker/price", q)
    out = {}
    for it in arr:
        s = it["symbol"]
        if s.endswith("USDT"):
            try:
                out[s[:-4]] = float(it["price"])
            except Exception:                                # noqa: BLE001
                pass
    return out


def futures_state(creds):
    """USDT-M：钱包余额、可用、浮盈、逐币持仓。"""
    _h1, acc = request("fapi", "/fapi/v2/account",
                       key=creds["key"], secret=creds["secret"])
    _h2, pr = request("fapi", "/fapi/v2/positionRisk",
                      key=creds["key"], secret=creds["secret"])
    pos = {}
    for p in pr:
        amt = float(p["positionAmt"])
        if abs(amt) < 1e-12:
            continue
        s = p["symbol"]
        # 只认 USDT 本位。positionRisk 会把 BTCUSDC / BTCUSD1 一起返回，
        # 用 [:-4] 硬切会把它们切成 "BTCUS" / "BTCUSD1" 这种不存在的币。
        if not s.endswith("USDT"):
            continue
        mark = float(p["markPrice"])
        pos[s[:-4]] = {
            "amt": amt, "notional": amt * mark,
            "entry": float(p["entryPrice"]), "mark": mark,
            "upnl": float(p["unRealizedProfit"]),
            "liq": float(p.get("liquidationPrice") or 0),
            "lev": int(float(p.get("leverage") or 0)),
            "margin_type": p.get("marginType"),
        }
    return {"wallet": float(acc["totalWalletBalance"]),
            "upnl": float(acc["totalUnrealizedProfit"]),
            "avail": float(acc["availableBalance"]),
            "maint": float(acc.get("totalMaintMargin") or 0),
            "pos": pos}


def snapshot(creds=None, write=True, quiet=False):
    """统一快照。**这是唯一对外的出口**，live_signals / ticket / 报表都读这个结构。

    返回：{
      ts, spot_value, spot:{ASSET:{qty,px,usd}}, cash_usdt,
      perp:{wallet,upnl,avail,maint,pos:{COIN:{...}}},
      equity, gross, net
    }
    equity = 现货估值 + 合约钱包 + 合约浮盈
      ← 不能用「现货 + 可用保证金」：合约的钱包余额已经包含被占用的保证金，
        再加一遍可用会把权益算大。
    """
    creds = creds or load_creds()
    if not creds:
        raise RuntimeError("没有凭据，先 `python binance_account.py set`")
    spot, _acc = spot_balances(creds)
    perp = futures_state(creds)
    spot_value = sum(v["usd"] for v in spot.values())
    cash = spot.get("USDT", {}).get("qty", 0.0)
    equity = spot_value + perp["wallet"] + perp["upnl"]
    gross = sum(v["usd"] for k, v in spot.items() if k != "USDT")
    gross += sum(abs(p["notional"]) for p in perp["pos"].values())
    net = sum(v["usd"] for k, v in spot.items() if k != "USDT")
    net += sum(p["notional"] for p in perp["pos"].values())
    snap = {"ts": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            "ts_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "spot": spot, "cash_usdt": cash, "spot_value": spot_value,
            "perp": perp, "equity": equity, "gross": gross, "net": net,
            "proxy": proxy_url() or "（直连）"}
    if write:
        # 原子替换：读的一方（live_signals）可能正在读，不能让它读到半个文件
        tmp = SNAP + ".tmp"
        json.dump(snap, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, SNAP)
    if not quiet:
        print(f"  权益 {equity:,.0f} U = 现货 {spot_value:,.0f} + 合约钱包 "
              f"{perp['wallet']:,.0f} + 浮盈 {perp['upnl']:+,.0f}")
        print(f"  现货 {len([k for k in snap['spot'] if k != 'USDT'])} 个币、"
              f"现金 {cash:,.0f} U；合约 {len(perp['pos'])} 个持仓、"
              f"可用保证金 {perp['avail']:,.0f} U")
    return snap


def read_snapshot(max_age_min=60):
    """给生产代码读的快照（不联网）。过期或不存在返回 None —— **绝不在这里发请求**：

    live_signals 每条邮件都会跑，如果它自己联网拉账户，那 (a) 币安挂了整条信号链就挂了，
    (b) 一次失败就变成「读不到 → 退回猜」，而人看不出这次是猜的。
    宁可拿一个 30 分钟前的真实数，也不要现场去赌一次网络。
    """
    if not os.path.exists(SNAP):
        return None
    try:
        s = json.load(open(SNAP, encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None
    try:
        t = dt.datetime.strptime(s["ts"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=dt.timezone(dt.timedelta(hours=8)))
    except Exception:                                        # noqa: BLE001
        return None
    age = (now_bj() - t).total_seconds() / 60
    s["age_min"] = round(age, 1)
    if max_age_min and age > max_age_min:
        return None
    return s


def enabled():
    """有没有凭据。**所有集成点的总开关** —— 没配就是纯台账模式，行为与今天完全一致。"""
    return os.path.exists(store_path())


def refresh(max_age_min=10, quiet=True):
    """按需刷新：够新就直接用，过期才联网。**任何失败都返回 None，不抛异常。**

    这是给 run_signal / monitor 用的入口。它必须永不抛 —— 一条读不到账户的邮件
    仍然是一封有用的邮件，而一个因为币安限频就中断的信号链不是。
    """
    if not enabled():
        return None
    s = read_snapshot(max_age_min=max_age_min)
    if s:
        return s
    try:
        return snapshot(load_creds(), quiet=quiet)
    except Exception as e:                                   # noqa: BLE001
        if not quiet:
            print(f"[账户] 刷新失败，沿用上一次快照：{type(e).__name__}: {e}")
        return read_snapshot(max_age_min=0)


# ============================================================
# 五、CLI
# ============================================================
def _read_secret_file(p):
    """从文件读 key/secret。认两种格式：`{"key":..,"secret":..}` 或两行纯文本。"""
    txt = open(p, encoding="utf-8-sig").read()
    try:
        j = json.loads(txt)
        if isinstance(j, dict):
            return (str(j.get("key") or "")).strip(), (str(j.get("secret") or "")).strip()
    except Exception:                                        # noqa: BLE001
        pass
    lines = [x.strip() for x in txt.replace("\r", "").split("\n") if x.strip()]
    if len(lines) >= 2:
        return lines[0], lines[1]
    return "", ""


def _shred(p):
    """先覆写随机字节再删。**这是"尽力"，不是保证** ——

    SSD 的写时复制、Windows 的卷影副本、杀软隔离区都可能留着旧内容。
    所以 `--from` 只在 getpass 不可用时用（有些终端不禁用回显），
    能本地敲就打本地敲。
    """
    try:
        n = max(os.path.getsize(p), 1)
        with open(p, "r+b") as f:
            f.write(os.urandom(n))
            f.flush()
            os.fsync(f.fileno())
    except Exception:                                        # noqa: BLE001
        pass
    try:
        os.remove(p)
    except Exception:                                        # noqa: BLE001
        pass


def _cmd_set(argv):
    force = "--force" in argv
    src = None
    if "--from" in argv:
        i = argv.index("--from")
        if i + 1 < len(argv):
            src = argv[i + 1]
    if src:
        if not os.path.exists(src):
            print(f"文件不存在：{src}")
            return 1
        key, secret = _read_secret_file(src)
        print(f"从 {src} 读取（读完会覆写并删除）")
    else:
        print("输入 key 与 secret（不回显、不进 shell 历史）。")
        print("若你的终端把输入回显出来了，Ctrl+C 中止，改用 --from 传文件。")
        print("先确认权限已按文件头的清单勾好：只开 Enable Reading。")
        key = getpass.getpass("API Key   : ").strip()
        secret = getpass.getpass("API Secret: ").strip()
    if src:
        _shred(src)
        print(f"  已覆写并删除 {src}")
    if not key or not secret:
        print("空输入，放弃")
        return 1
    print(f"  key = {mask(key)}  长度 {len(key)}")
    creds = {"key": key, "secret": secret}
    print("\n核对权限 …")
    try:
        ok, rows, raw = api_restrictions(creds)
    except Exception as e:                                   # noqa: BLE001
        print(f"  自检失败：{e}")
        print("  连权限都读不到，说明 key/IP/代理至少有一项不对，先别存。")
        return 1
    for r in rows:
        mark = "✓" if not r["bad"] else ("✗" if r["want"] else "!")
        print(f"  {mark} {r['label']:14} 期望 {'开' if r['want'] else '关'}，"
              f"实际 {'开' if r['got'] else '关'}")
    if not ok and not force:
        print("\n拒绝保存：这个 key 不是纯只读。")
        print("  去币安把交易/提现/划转权限关掉再重来；确定要硬存加 --force。")
        return 1
    p = save_creds(key, secret, {"readonly_ok": ok,
                                 "restrictions": {r["field"]: r["got"] for r in rows}})
    print(f"\n已加密写入 {p}")
    print(f"  加密方式 DPAPI（绑定当前 Windows 用户 {os.environ.get('USERNAME')}）")
    print("  下一步：python binance_account.py snap")
    return 0


def _cmd_test(argv):
    creds = load_creds()
    if not creds:
        print("没有凭据。先 `python binance_account.py set`")
        return 1
    print(f"凭据 {mask(creds['key'])}，存于 {store_path()}")
    print(f"代理 {proxy_url() or '（直连，国内大概率 502）'}")
    try:
        ok, rows, _raw = api_restrictions(creds)
    except Exception as e:                                   # noqa: BLE001
        print(f"权限自检失败：{e}")
        return 1
    for r in rows:
        mark = "✓" if not r["bad"] else ("✗" if r["want"] else "!")
        print(f"  {mark} {r['label']:14} 期望 {'开' if r['want'] else '关'}，"
              f"实际 {'开' if r['got'] else '关'}")
    if not ok:
        print("  ⚠ 这个 key 不是纯只读 —— 建议立刻去交易所收权限")
    print()
    try:
        snapshot(creds)
    except Exception as e:                                   # noqa: BLE001
        print(f"拉取失败：{e}")
        return 1
    print(f"  已写 {SNAP}")
    return 0


def _cmd_show(argv):
    creds = load_creds()
    if not creds:
        print("没有凭据")
        return 1
    print(f"存储 {store_path()}")
    print(f"  key    {mask(creds['key'])}")
    print(f"  secret {mask(creds['secret'])}")
    print(f"  存入   {creds.get('saved_bj')}")
    print(f"  只读   {creds.get('readonly_ok')}")
    r = creds.get("restrictions") or {}
    if r:
        print("  权限   " + "，".join(f"{k}={'开' if v else '关'}" for k, v in r.items()))
    s = read_snapshot(max_age_min=0)
    if s:
        print(f"  快照   {s['ts']}（{s['age_min']} 分钟前）权益 {s['equity']:,.0f} U")
    return 0


def _cmd_rm(argv):
    p = store_path()
    if os.path.exists(p):
        os.remove(p)
        print(f"已删除 {p}")
    else:
        print("本来就没有")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "show"
    if cmd == "set":
        return _cmd_set(argv)
    if cmd == "test":
        return _cmd_test(argv)
    if cmd == "snap":
        creds = load_creds()
        if not creds:
            print("没有凭据。先 `python binance_account.py set`")
            return 1
        try:
            snapshot(creds)
        except Exception as e:                               # noqa: BLE001
            print(f"拉取失败：{e}")
            return 1
        return 0
    if cmd == "show":
        return _cmd_show(argv)
    if cmd == "rm":
        return _cmd_rm(argv)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
