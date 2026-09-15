# -*- coding: utf-8 -*-
"""米游社每日签到助手 —— 核心逻辑层

支持原神、崩坏：星穹铁道（国服 · 米游社账号）。
本模块不含任何界面代码，供 app.pyw（图形界面）与 cli.py（命令行）共用。

凭据使用 Windows DPAPI 加密保存，只有当前 Windows 用户能够解密；
签到接口由 genshin.py 提供，原神对应 x-rpc-signgame: hk4e，
崩铁对应 x-rpc-signgame: hkrpg。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

BASE = Path(__file__).resolve().parent
CRED_FILE = BASE / "cookie.enc"
PROFILE_DIR = BASE / ".edge_profile"
LOG_FILE = BASE / "signin.log"
STATE_FILE = BASE / "state.json"
SETTINGS_FILE = BASE / "settings.json"

LOGIN_URL = "https://www.miyoushe.com/sr/"
TASK_NAME = "MYS-DailySignIn"
LEGACY_TASK_NAMES = ("HSR-DailySignIn",)

DEFAULT_SETTINGS = {"task_time": "09:05"}
MAX_LOG_LINES = 400

# 支持的游戏：key 内部标识，enum 对应 genshin.Game 成员名，
# signgame 即米哈游接口的 x-rpc-signgame 头（展示/排错用）
GAMES = (
    {"key": "genshin", "name": "原神", "enum": "GENSHIN", "signgame": "hk4e"},
    {"key": "starrail", "name": "崩坏：星穹铁道", "enum": "STARRAIL", "signgame": "hkrpg"},
)
GAME_KEYS = tuple(g["key"] for g in GAMES)
GAME_BY_KEY = {g["key"]: g for g in GAMES}


def game_name(key: str) -> str:
    return GAME_BY_KEY.get(key, {}).get("name", key)


# ---------------------------------------------------------------- 通用工具

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_std_streams():
    """计划任务用 pythonw 启动时没有控制台，把 stdout/stderr 指到空设备。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


def write_log(msg: str):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (now_str(), msg))
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def read_log_tail(n: int = MAX_LOG_LINES) -> str:
    # n <= 0 时必须显式返回空：lines[-0:] 在 Python 里等价于 lines[0:]，
    # 会静默地把整个日志吐出来。
    if n <= 0 or not LOG_FILE.exists():
        return ""
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])


def _emit_factory(on_log):
    def emit(msg: str):
        write_log(msg)
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass
    return emit


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _write_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 配置 / 状态

def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(_read_json(SETTINGS_FILE, {}) or {})
    return s


def save_settings(**kw) -> dict:
    s = load_settings()
    s.update(kw)
    _write_json(SETTINGS_FILE, s)
    return s


def load_state() -> dict:
    st = _read_json(STATE_FILE, {}) or {}
    if not isinstance(st, dict):
        st = {}
    # state.json 可能被手工编辑坏（games 写成 null / 列表）。这里必须用
    # isinstance 而不是 setdefault——setdefault 只判断"键在不在"，
    # 对"键在、值是 null"完全无效，随后 save_game_state 就会抛
    # AttributeError，整个程序打不开。
    if not isinstance(st.get("games"), dict):
        st["games"] = {}
    if not isinstance(st.get("accounts"), dict):
        st["accounts"] = {}
    # 兼容早期只有崩铁的扁平结构
    if not st["games"] and any(k in st for k in ("days", "signed_today", "last_run")):
        st["games"]["starrail"] = {
            "last_run": st.get("last_run"),
            "status": st.get("last_result"),
            "message": st.get("last_message"),
            "days": st.get("days"),
            "signed_today": st.get("signed_today"),
        }
    return st


STATE_LEGACY_KEYS = ("last_run", "last_result", "last_message", "days",
                     "signed_today", "uid", "nickname")


def save_state(**kw) -> dict:
    st = load_state()
    st.update(kw)
    for k in STATE_LEGACY_KEYS:
        st.pop(k, None)
    _write_json(STATE_FILE, st)
    return st


def save_game_state(key: str, **kw) -> dict:
    st = load_state()
    games = st.setdefault("games", {})
    g = games.get(key)
    if not isinstance(g, dict):
        g = {}
        games[key] = g
    g.update(kw)
    for k in STATE_LEGACY_KEYS:
        st.pop(k, None)
    _write_json(STATE_FILE, st)
    return st


# ---------------------------------------------------------------- DPAPI 加解密

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    in_blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DATA_BLOB()
    if protect:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, None, None, None, 0x01, ctypes.byref(out))
    else:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise RuntimeError("DPAPI 调用失败（错误码 %d）"
                           % ctypes.windll.kernel32.GetLastError())
    res = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return res


def save_cookies(cookies: dict):
    raw = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    CRED_FILE.write_bytes(_dpapi(raw, True))
    save_state(login_time=now_str(), cred_count=len(cookies), cred_ok=True)
    write_log("凭据已加密保存到 %s" % CRED_FILE.name)


def load_cookies() -> dict:
    if not CRED_FILE.exists():
        raise RuntimeError("NOT_LOGGED_IN")
    try:
        return json.loads(_dpapi(CRED_FILE.read_bytes(), False).decode("utf-8"))
    except Exception as e:
        raise RuntimeError("凭据解密失败（是否换了 Windows 用户？）：%s" % e)


def clear_credentials():
    try:
        CRED_FILE.unlink()
    except Exception:
        pass
    save_state(cred_ok=False, accounts={})


def _has_login(cookies: dict) -> bool:
    return bool({"cookie_token_v2", "cookie_token", "ltoken_v2", "ltoken"} & set(cookies))


# ---------------------------------------------------------------- 客户端

def _make_client(cookies):
    import genshin
    return genshin.Client(cookies, region=genshin.Region.CHINESE, lang="zh-cn")


async def _close_client(client):
    closer = getattr(client, "close", None)
    if closer is not None:
        try:
            await closer()
        except Exception:
            pass


def _game_enum(genshin, key: str):
    return getattr(genshin.Game, GAME_BY_KEY[key]["enum"])


# ---------------------------------------------------------------- 登录（Playwright 驱动本机 Edge）

def login(on_log=None, timeout_sec: int = 600) -> dict:
    """打开 Edge 让用户登录米游社，从浏览器引擎层读取 cookie 并加密保存。"""
    emit = _emit_factory(on_log)
    res = {"ok": False, "status": "error", "message": "", "ts": now_str()}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        res["message"] = "缺少 playwright，请先运行 安装依赖.bat"
        emit(res["message"])
        return res

    emit("正在打开 Edge 窗口，请在窗口里登录米游社（扫码或账号密码均可）")
    emit("登录成功后窗口会自动关闭，最多等待 %d 分钟" % (timeout_sec // 60))

    found = None
    closed = False
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass

                waited = 0
                while waited < timeout_sec:
                    # 先读 cookie：用户手动关掉窗口时，第一个抛异常的就是
                    # ctx.cookies()。放在循环体最前面并就地捕获，才能把
                    # "窗口被关闭"和"启动浏览器失败"区分开——否则异常会一路
                    # 冒泡到外层，报出完全无关的"启动浏览器失败"。
                    try:
                        raw = ctx.cookies()
                    except Exception:
                        closed = True
                        break
                    cookies = {
                        c["name"]: c["value"]
                        for c in raw
                        if any(k in c.get("domain", "")
                               for k in ("mihoyo", "miyoushe", "hoyolab"))
                    }
                    if _has_login(cookies):
                        found = cookies
                        break
                    try:
                        if not ctx.pages:
                            closed = True
                            break
                    except Exception:
                        closed = True
                        break
                    import time as _t
                    _t.sleep(1)
                    waited += 1
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        res["message"] = "启动浏览器失败：%s: %s" % (type(e).__name__, e)
        emit(res["message"])
        return res

    if not found:
        if closed:
            res["status"] = "closed"
            res["message"] = "登录窗口在完成登录前被关闭了，未保存凭据，请重试"
        else:
            res["status"] = "timeout"
            res["message"] = "等待 %d 分钟仍未检测到登录状态，请重试" % (timeout_sec // 60)
        emit(res["message"])
        return res

    try:
        save_cookies(found)
    except Exception as e:
        res["message"] = "保存凭据失败：%s" % e
        emit(res["message"])
        return res

    emit("登录完成，已保存 %d 项 cookie" % len(found))
    res["ok"] = True
    res["status"] = "ok"
    res["message"] = "登录成功，已保存 %d 项 cookie" % len(found)

    accounts = fetch_accounts(on_log=on_log)
    if accounts:
        names = "、".join("%s %s" % (game_name(k), v.get("nickname") or v.get("uid"))
                         for k, v in accounts.items())
        res["message"] += "；识别到角色：%s" % names
    return res


# ---------------------------------------------------------------- 账号角色信息

async def _accounts_async(on_log=None) -> dict:
    emit = _emit_factory(on_log)
    try:
        cookies = load_cookies()
    except RuntimeError:
        return {}

    client = _make_client(cookies)
    try:
        raw = await client.get_game_accounts()
    finally:
        await _close_client(client)

    out = {}
    for a in raw or []:
        gname = str(getattr(a, "game", ""))
        for key, cfg in GAME_BY_KEY.items():
            if gname.endswith(cfg["enum"]):
                old = out.get(key)
                # 同游戏多角色时保留等级最高的那个
                if old is None or (getattr(a, "level", 0) or 0) > (old.get("level") or 0):
                    out[key] = {
                        "uid": str(getattr(a, "uid", "")),
                        "nickname": getattr(a, "nickname", "") or "",
                        "level": getattr(a, "level", None),
                        "server": str(getattr(a, "server", "") or ""),
                    }
    if out:
        st = load_state()
        accs = st.setdefault("accounts", {})
        accs.update(out)
        _write_json(STATE_FILE, st)
        emit("识别到角色：" + "、".join(
            "%s %s(%s)" % (game_name(k), v.get("nickname"), v.get("uid"))
            for k, v in out.items()))
    return out


def fetch_accounts(on_log=None) -> dict:
    """尽力获取各游戏下的角色信息（uid / 昵称 / 等级），失败返回空字典。"""
    try:
        return asyncio.run(_accounts_async(on_log))
    except Exception:
        return {}


# ---------------------------------------------------------------- 签到

async def _sign_many_async(game_keys, force_info: bool, on_log) -> dict:
    import genshin

    emit = _emit_factory(on_log)
    InvalidCookies = getattr(genshin, "InvalidCookies", ())
    AlreadyClaimed = getattr(genshin, "AlreadyClaimed", ())

    out = {}
    for k in game_keys:
        out[k] = {"ok": False, "status": "error", "message": "", "reward": None,
                  "days": None, "signed_today": None, "ts": now_str(),
                  "name": game_name(k)}

    try:
        cookies = load_cookies()
    except RuntimeError as e:
        msg = "尚未登录，请先点「登录米游社」" if str(e) == "NOT_LOGGED_IN" else str(e)
        for k in game_keys:
            out[k].update(status="not_logged_in", message=msg)
            save_game_state(k, last_run=out[k]["ts"], status="not_logged_in",
                            message=msg, days=None, signed_today=None)
        emit(msg)
        return out

    client = _make_client(cookies)
    try:
        for k in game_keys:
            res = out[k]
            game = _game_enum(genshin, k)
            try:
                if not force_info:
                    try:
                        reward = await client.claim_daily_reward(game=game)
                        res["status"] = "claimed"
                        res["ok"] = True
                        res["reward"] = "%s ×%s" % (getattr(reward, "name", "?"),
                                                    getattr(reward, "amount", "?"))
                        res["message"] = "签到成功，获得 " + res["reward"]
                        emit("%s：%s" % (res["name"], res["message"]))
                    except AlreadyClaimed:
                        res["status"] = "already"
                        res["ok"] = True
                        res["message"] = "今天已经签到过了"
                        emit("%s：%s" % (res["name"], res["message"]))
                    except InvalidCookies:
                        raise
                    except Exception as e:
                        res["status"] = "failed"
                        res["message"] = "签到失败：%s: %s" % (type(e).__name__, e)
                        emit("%s：%s" % (res["name"], res["message"]))

                info = await client.get_reward_info(game=game)
                res["days"] = getattr(info, "claimed_rewards", None)
                res["signed_today"] = bool(getattr(info, "signed_in", False))
                if force_info:
                    res["ok"] = True
                    res["status"] = "info"
                    res["message"] = "本月累计签到 %s 天，今日%s" % (
                        res["days"], "已签到" if res["signed_today"] else "未签到")
                emit("%s：本月累计签到 %s 天，今日状态：%s"
                     % (res["name"], res["days"],
                        "已签到" if res["signed_today"] else "未签到"))
            except InvalidCookies:
                res["status"] = "invalid_cookie"
                res["ok"] = False
                res["message"] = "cookie 已失效，请点「登录米游社」重新登录"
                emit("%s：%s" % (res["name"], res["message"]))
                save_state(cred_ok=False)
            except Exception as e:
                if res["status"] == "error":
                    res["message"] = "请求异常：%s: %s" % (type(e).__name__, e)
                emit("%s：请求异常 %s: %s" % (res["name"], type(e).__name__, e))
    finally:
        await _close_client(client)

    for k in game_keys:
        r = out[k]
        save_game_state(k, last_run=r["ts"], status=r["status"],
                        message=r["message"], days=r["days"],
                        signed_today=r["signed_today"], reward=r.get("reward"))
    return out


def sign_once(game_keys=None, force_info: bool = False, on_log=None) -> dict:
    """执行签到（force_info=True 时只查询状态，不重复签）。

    game_keys 为空表示全部游戏。返回 {"ok": bool, "games": {key: 结果}}。
    """
    keys = tuple(game_keys) if game_keys else GAME_KEYS
    keys = tuple(k for k in keys if k in GAME_BY_KEY)
    try:
        games = asyncio.run(_sign_many_async(keys, force_info, on_log))
    except Exception as e:
        msg = "运行异常：%s: %s" % (type(e).__name__, e)
        write_log(msg)
        if on_log:
            on_log(msg)
        games = {k: {"ok": False, "status": "error", "message": msg, "reward": None,
                     "days": None, "signed_today": None, "ts": now_str(),
                     "name": game_name(k)} for k in keys}
        for k in keys:
            save_game_state(k, last_run=now_str(), status="error", message=msg)

    return {"ok": any(g.get("ok") for g in games.values()), "games": games}


def summarize(result: dict) -> str:
    """把多游戏结果压成一句话，供状态栏使用。"""
    games = (result or {}).get("games") or {}
    parts = []
    for k in GAME_KEYS:
        g = games.get(k)
        if not g:
            continue
        st = g.get("status")
        if st == "claimed":
            parts.append("%s 已签到" % g["name"])
        elif st == "already":
            parts.append("%s 今日已签" % g["name"])
        elif st == "info":
            parts.append("%s %s天" % (g["name"], g.get("days")))
        elif st in ("invalid_cookie", "not_logged_in"):
            parts.append("%s 需重新登录" % g["name"])
        else:
            parts.append("%s 失败" % g["name"])
    return " · ".join(parts)


# ---------------------------------------------------------------- Windows 计划任务

def _python_for_task() -> str:
    pw = BASE / ".venv" / "Scripts" / "pythonw.exe"
    if pw.exists():
        return str(pw)
    return sys.executable


def task_command() -> str:
    return '"%s" "%s" sign' % (_python_for_task(), BASE / "cli.py")


def _query(name: str) -> bool:
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name],
                           capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        return r.returncode == 0
    except Exception:
        return False


def task_exists() -> bool:
    return any(_query(n) for n in (TASK_NAME,) + LEGACY_TASK_NAMES)


def task_detail() -> dict:
    """返回 {exists, time, next_run}"""
    out = {"exists": False, "time": None, "next_run": None}
    target = None
    for n in (TASK_NAME,) + LEGACY_TASK_NAMES:
        if _query(n):
            target = n
            break
    if not target:
        return out
    out["exists"] = True
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", target, "/FO", "LIST", "/V"],
                           capture_output=True, text=True,
                           encoding="gbk", errors="replace")
        text = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"(?:开始时间|Start Time):\s*(\d{2}:\d{2})", text)
        if m:
            out["time"] = m.group(1)
        m2 = re.search(r"(?:下次运行时间|Next Run Time):\s*([^\r\n]+)", text)
        if m2:
            out["next_run"] = m2.group(1).strip()
    except Exception:
        pass
    return out


def create_task(time_str: str = "09:05", on_log=None) -> dict:
    emit = _emit_factory(on_log)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str or ""):
        msg = "时间格式不对，应为 HH:MM（例如 09:05）"
        emit(msg)
        return {"ok": False, "message": msg}

    # 清理历史遗留的任务名，避免重复签到
    for n in LEGACY_TASK_NAMES:
        if _query(n):
            try:
                subprocess.run(["schtasks", "/Delete", "/TN", n, "/F"],
                               capture_output=True, text=True,
                               encoding="gbk", errors="replace")
                emit("已移除旧任务 %s" % n)
            except Exception:
                pass

    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", task_command(),
             "/SC", "DAILY", "/ST", time_str, "/F", "/RL", "LIMITED"],
            capture_output=True, text=True, encoding="gbk", errors="replace")
    except Exception as e:
        msg = ("无法调用系统计划任务命令 schtasks（%s: %s）。"
               "可能是安全软件或系统策略限制了该命令，"
               "可以改用「任务计划程序」手动创建，指向 %s"
               % (type(e).__name__, e, task_command()))
        emit(msg)
        return {"ok": False, "message": msg}

    if r.returncode == 0:
        save_settings(task_time=time_str)
        msg = "已开启每日自动签到：每天 %s 后台签到 %s" % (
            time_str, "、".join(g["name"] for g in GAMES))
        emit(msg)
        return {"ok": True, "message": msg}
    detail = ((r.stdout or "") + (r.stderr or "")).strip()
    msg = "注册计划任务失败：%s" % (detail or "权限不足，可尝试以管理员身份运行")
    emit(msg)
    return {"ok": False, "message": msg}


def delete_task(on_log=None) -> dict:
    emit = _emit_factory(on_log)
    ok, extra = False, []
    for n in (TASK_NAME,) + LEGACY_TASK_NAMES:
        if not _query(n):
            continue
        try:
            r = subprocess.run(["schtasks", "/Delete", "/TN", n, "/F"],
                               capture_output=True, text=True,
                               encoding="gbk", errors="replace")
        except Exception as e:
            extra.append("无法调用 schtasks（%s: %s）" % (type(e).__name__, e))
            break
        if r.returncode == 0:
            ok = True
        else:
            extra.append("删除 %s 失败：%s"
                         % (n, ((r.stdout or "") + (r.stderr or "")).strip()))
    msg = "已关闭每日自动签到" if ok else "当前没有已注册的自动签到任务"
    emit(msg)
    for m in extra:
        emit(m)
    return {"ok": len(extra) == 0, "message": msg}


# ---------------------------------------------------------------- 汇总状态（界面首屏用）

def local_state() -> dict:
    st = load_state()
    settings = load_settings()
    games = st.get("games") or {}
    accounts = st.get("accounts") or {}
    detail = []
    for cfg in GAMES:
        k = cfg["key"]
        g = dict(games.get(k) or {})
        g["key"] = k
        g["name"] = cfg["name"]
        g["signgame"] = cfg["signgame"]
        g["account"] = accounts.get(k) or {}
        detail.append(g)

    # cred_ok 必须同时看两处：文件在不在 + state.json 里的标记。
    # 只看文件的话，cookie 失效时 _sign_many_async 写入的 cred_ok=False 会被
    # 忽略，右上角一直绿着显示"已登录"，跟卡片上"需重新登录"自相矛盾。
    cred_flag = st.get("cred_ok")
    if cred_flag is None:
        cred_flag = CRED_FILE.exists()   # 兼容早期没有该字段的 state.json

    return {
        "cred_ok": bool(cred_flag) and CRED_FILE.exists(),
        "login_time": st.get("login_time"),
        "task_time": settings.get("task_time", "09:05"),
        "game_list": detail,
    }
