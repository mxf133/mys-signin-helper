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

def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def _app_dir() -> Path:
    """程序数据目录（凭据、日志、状态都存这里）。

    源码运行时就是项目目录；打包成 exe 后 `__file__` 指向 PyInstaller 的
    临时解包目录（_MEIPASS），每次启动都不一样，若沿用它，cookie.enc 会在
    退出时被清掉、signin.log 也会凭空消失。所以冻结模式下必须改用 exe
    所在的目录。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = _app_dir()
CRED_FILE = BASE / "cookie.enc"
PROFILE_DIR = BASE / ".edge_profile"
PW_TMP_DIR = BASE / ".pw_tmp"
LOG_FILE = BASE / "signin.log"
STATE_FILE = BASE / "state.json"
SETTINGS_FILE = BASE / "settings.json"

LOGIN_URL = "https://www.miyoushe.com/sr/"

# 版本号：界面标题栏、检查更新、打包脚本都从这里取，避免多处各写一份。
VERSION = "3.2.1"

# 检查更新要查的仓库（GitHub Releases）。
GITHUB_REPO = "mxf133/mys-signin-helper"

TASK_NAME = "MYS-DailySignIn"
# 备用任务名：安全软件可能把某个名字记进黑名单（同名怎么都建不上），
# 换个名字就能成功，所以注册失败时自动换个名字再试。
ALT_TASK_NAMES = ("MiyousheSignInAssistant",)
LEGACY_TASK_NAMES = ("HSR-DailySignIn",)

# 米游社账号会话校验接口（米游社 web 账号 SDK 自己用的那两个）。
# 必须分开看，因为米哈游把两套会话分得很清：
#   ltoken 会话       —— 社区、角色查询这类接口认它；
#   cookie_token 会话 —— 游戏签到（event/luna）这类接口认它。
# 历史上这里只认"有 ltoken 就算登录成功"，于是旧会话的 cookie_token
# 过期后，程序一直显示已登录、签到却始终返回 -100（请登录后重试）。
VERIFY_COOKIE_TOKEN_URL = ("https://passport-api.miyoushe.com"
                           "/account/ma-cn-session/web/verifyCookieToken")
VERIFY_LTOKEN_URL = ("https://passport-api.miyoushe.com"
                     "/account/ma-cn-session/web/verifyLtoken")

# 设备指纹 cookie：重新登录时保留，免得每次都被当成"新设备"而频繁触发短信验证。
DEVICE_COOKIE_NAMES = ("_MHYUUID", "DEVICEFP", "DEVICEFP_SEED_ID", "DEVICEFP_SEED_TIME")

# 登录态 cookie：重新登录前必须先清掉。否则浏览器会直接复用旧会话，
# 一两秒就"登录成功"——既拿不到新的 cookie_token（签到真正要用的凭据），
# 也换不了账号。这正是"点登录秒成功、无法退出"的原因。
SESSION_COOKIE_NAMES = (
    "cookie_token", "cookie_token_v2", "ltoken", "ltoken_v2",
    "stoken", "stoken_v2", "login_ticket", "login_uid",
    "account_id", "account_id_v2", "account_mid_v2",
    "ltuid", "ltuid_v2", "ltmid_v2",
)

DEFAULT_SETTINGS = {"task_time": "09:05", "selected_games": None,
                    "notify_on_success": True}
MAX_LOG_LINES = 400

# 支持的游戏：key 内部标识，enum 对应 genshin.Game 成员名，
# signgame 即米哈游接口的 x-rpc-signgame 头（展示/排错用）。
# 注意 signgame 的取值来自 genshin 库 daily.py 里的硬映射，不是 Game 的 value：
# GENSHIN->hk4e、STARRAIL->hkrpg、ZZZ->zzz。
GAMES = (
    {"key": "genshin", "name": "原神", "enum": "GENSHIN", "signgame": "hk4e"},
    {"key": "starrail", "name": "崩坏：星穹铁道", "enum": "STARRAIL", "signgame": "hkrpg"},
    {"key": "zzz", "name": "绝区零", "enum": "ZZZ", "signgame": "zzz"},
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


def selected_games() -> list:
    """用户勾选要签到的游戏 key 列表（存在 settings.json）。

    三重兜底，保证任何情况下都返回一个非空且合法的列表：
    非列表（None / 字符串）、含未知 key、空列表 —— 一律退回"全部游戏"。
    这样旧版 settings.json（压根没有这个键）升级上来也天然是全选。
    """
    raw = load_settings().get("selected_games")
    if not isinstance(raw, list):
        return list(GAME_KEYS)
    keys = [k for k in GAME_KEYS if k in raw]   # 按 GAMES 顺序，顺带滤掉未知 key
    return keys or list(GAME_KEYS)


def save_selected_games(keys) -> list:
    keys = [k for k in GAME_KEYS if k in (keys or [])]
    if not keys:
        keys = list(GAME_KEYS)      # 一个都不选等于全选，避免出现"什么都不签"
    save_settings(selected_games=keys)
    return keys


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


def save_cookies(cookies: dict, sign_ready=None):
    """加密保存 cookie。

    sign_ready：cookie_token 会话是否校验通过（True/False/None=没测出来）。
    存下来是为了界面上能区分"登录了"和"登录了但签到凭据不完整"。
    """
    raw = json.dumps(cookies, ensure_ascii=False).encode("utf-8")
    CRED_FILE.write_bytes(_dpapi(raw, True))
    save_state(login_time=now_str(), cred_count=len(cookies), cred_ok=True,
               cred_names=sorted(cookies), sign_ready=sign_ready)
    write_log("凭据已加密保存到 %s（%d 项 cookie，签到凭据校验=%s）"
              % (CRED_FILE.name, len(cookies), sign_ready))


def load_cookies() -> dict:
    if not CRED_FILE.exists():
        raise RuntimeError("NOT_LOGGED_IN")
    try:
        return json.loads(_dpapi(CRED_FILE.read_bytes(), False).decode("utf-8"))
    except Exception as e:
        raise RuntimeError("凭据解密失败（是否换了 Windows 用户？）：%s" % e)


def clear_credentials(on_log=None) -> dict:
    """清除本机凭据，并顺带退出浏览器里的米游社登录态。"""
    emit = _emit_factory(on_log)
    try:
        CRED_FILE.unlink()
    except Exception:
        pass
    save_state(cred_ok=False, sign_ready=None, accounts={},
               cred_count=0, cred_names=[])
    emit("已清除本机登录凭据")
    logged_out = _clear_browser_session(on_log=on_log)
    if logged_out:
        emit("已退出浏览器中的米游社登录状态")
    return {
        "ok": True,
        "message": "已退出登录：本机凭据已清除"
                   + ("，浏览器里的米游社登录态也已退出" if logged_out else ""),
    }


def _has_login(cookies: dict) -> bool:
    return bool({"cookie_token_v2", "cookie_token", "ltoken_v2", "ltoken"} & set(cookies))


def _filter_cookies(raw) -> dict:
    """只留米哈游系域下的 cookie，同名以最后一个为准。"""
    return {
        c["name"]: c["value"]
        for c in raw or []
        if any(k in (c.get("domain") or "") for k in ("mihoyo", "miyoushe", "hoyolab"))
    }


# ---------------------------------------------------------------- 会话校验

async def _verify_session_async(cookies: dict) -> dict:
    """分别校验 cookie_token 会话和 ltoken 会话。

    返回 {"cookie_token": bool|None, "ltoken": bool|None, "detail": str}，
    None 表示无法判定（网络异常、接口变更等），不能当成"失效"处理。
    """
    import aiohttp
    out = {"cookie_token": None, "ltoken": None, "detail": ""}
    details = []
    for key, url in (("cookie_token", VERIFY_COOKIE_TOKEN_URL),
                     ("ltoken", VERIFY_LTOKEN_URL)):
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, cookies=cookies,
                                  headers={"user-agent": "Mozilla/5.0"}) as r:
                    data = await r.json(content_type=None)
            rc = data.get("retcode")
            out[key] = (rc == 0)
            details.append("%s retcode=%s" % (key, rc))
        except Exception as e:
            details.append("%s 无法校验(%s)" % (key, type(e).__name__))
    out["detail"] = "，".join(details)
    return out


def verify_session(cookies=None) -> dict:
    """同步版会话校验；没凭据或异常时返回可判定的结构。"""
    if cookies is None:
        try:
            cookies = load_cookies()
        except RuntimeError:
            return {"cookie_token": None, "ltoken": None, "detail": "没有保存的凭据"}
    try:
        return asyncio.run(_verify_session_async(cookies))
    except Exception as e:
        return {"cookie_token": None, "ltoken": None,
                "detail": "%s: %s" % (type(e).__name__, e)}


# ---------------------------------------------------------------- Windows 系统通知

NOTIFY_APP_NAME = "米游社签到助手"


# 通知助手：一个已安装的 MSIX 包（松散布局注册）+ 应用执行别名
TOAST_ALIAS_NAME = "miyoushe-toast.exe"
_TOAST_ALIAS_WARNED = False


def toast_alias() -> Path:
    la = os.environ.get("LOCALAPPDATA") or ""
    return Path(la) / "Microsoft" / "WindowsApps" / TOAST_ALIAS_NAME


def _notify_toast(title: str, message: str) -> bool:
    """通过"通知助手"发真正的 Windows 系统通知。

    为什么要绕这一下：WinRT 的 ToastNotificationManager 只允许有"程序包标识"
    （已安装应用）的进程调用，纯源码跑的 Python 进程会报 0x80073D54。
    所以随程序安装了一个极小的 MSIX 包（底下是 toast_helper.exe，由
    build_toast_package.py 打包注册），并声明了应用执行别名；主程序调用这个
    别名，助手就等于"以已安装应用身份"运行，于是能发真通知（右下角+通知中心）。
    没装助手（或调用失败）时返回 False，由调用方退到托盘气泡/自绘卡片。

    注意：这里**故意不先做 alias.exists() 判断**——"应用执行别名"是重解析点，
    安全软件可能间歇性拒绝 stat，而 Path.exists() 会把异常吞成 False，导致明明
    装了助手却被跳过。直接尝试运行更可靠，真找不到文件会抛 FileNotFoundError。
    """
    global _TOAST_ALIAS_WARNED
    alias = str(toast_alias())
    last = ""
    for cmd in ([alias], [TOAST_ALIAS_NAME]):        # 绝对路径，再退化到 PATH 查找
        try:
            r = subprocess.run(cmd + ["--title", title, "--message", message],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                return True
            last = "返回 %s：%s" % (r.returncode, (r.stderr or "").strip()[:140])
            break                                     # 能跑起来但报错，就不再换路径试
        except FileNotFoundError:
            last = "找不到 %s" % cmd[0]
            continue
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
            break
    if not _TOAST_ALIAS_WARNED:
        _TOAST_ALIAS_WARNED = True
        write_log("通知助手不可用（%s），本次改用其他提醒方式" % last)
    return False


def notify(title: str, message: str, level: str = "warn") -> bool:
    """右下角弹一条提醒（能让用户"下次开电脑就发现签到失败"）。

    依次尝试三条路，哪条通用哪条：
      ① 通知助手 → 真正的 Windows 通知（右下角弹出、进通知中心）——
         需要先运行过 build_toast_package.py 安装助手；
      ② Shell_NotifyIcon 托盘气泡（同样是系统通知，但会被 360 之类拦住）；
      ③ 自绘右下角卡片（纯窗口绘制，几乎不会被拦，只作最后兜底）。
    刻意**不调用 PowerShell**：360 会把本程序拉起的 powershell.exe 判成
    Trojan.Generic 直接结束进程并弹"木马查杀"警报。

    三条都不行就只写日志——**绝不弹模态窗口**，不因为通知失败打断用户。
    """
    if sys.platform != "win32":
        return False
    title = (title or NOTIFY_APP_NAME)[:60]
    message = (message or "")[:230]
    if _notify_toast(title, message):
        return True
    if _notify_balloon(title, message, level):
        return True
    if _notify_card(title, message, level):
        write_log("系统通知被拦，已改用右下角通知卡片")
        return True
    write_log("右下角提醒没能弹出（系统通知与卡片都失败）")
    return False


def _ensure_dpi_aware():
    """声明 DPI 感知，否则高缩放（125%/150%）下窗口会被系统整体拉伸，文字发虚。

    桌面版主程序启动时已经声明过；但定时任务/命令行（cli.py）没声明，
    它弹出的右下角卡片就会糊。这里补上，进程级与线程级都试。
    """
    if sys.platform != "win32":
        return
    try:
        u32 = ctypes.windll.user32
        try:
            u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_V2
        except Exception:
            pass
        try:
            u32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            u32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        try:
            if not u32.GetDpiForSystem():
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        # tempfile 之外，很多库会缓存 DPI，这里不必额外处理
        pass


def _notify_card(title: str, message: str, level: str = "warn",
                 timeout_sec: int = 15) -> bool:
    """右下角自绘的"通知卡片"（外观照着 Windows 通知做的）。

    为什么需要它：真正的 WinRT Toast 要求进程有"程序包标识"（MSIX 打包应用），
    非打包的 Python 进程调用会一直报"该进程没有程序包标识符"；而
    Shell_NotifyIcon（托盘气泡）在装了 360 的机器上会被拒绝。
    于是这里用最基础的窗口/绘图 API 自己画一张：圆角、图标、标题+正文、
    从右侧滑入、十几秒后自动消失、点击立即关闭、不抢焦点。
    这些都是任何 GUI 程序都会做的事，不会被安全软件当成异常行为。
    """
    if sys.platform != "win32":
        return False
    _ensure_dpi_aware()
    try:
        import time
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        kernel32 = ctypes.windll.kernel32

        LRESULT = ctypes.c_ssize_t
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.BeginPaint.restype = wintypes.HDC
        user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.DrawTextW.restype = ctypes.c_int
        user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                     ctypes.c_void_p, wintypes.UINT]
        user32.FillRect.restype = ctypes.c_int
        user32.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HBRUSH]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.DrawIconEx.restype = wintypes.BOOL
        user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                      wintypes.HICON, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
        user32.SetWindowRgn.restype = ctypes.c_int
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        wintypes.UINT]
        user32.SystemParametersInfoW.restype = wintypes.BOOL
        user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                                 ctypes.c_void_p, wintypes.UINT]
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        gdi32.CreateFontW.restype = wintypes.HANDLE
        gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 4 \
            + [wintypes.DWORD] * 4 + [wintypes.LPCWSTR]
        gdi32.SelectObject.restype = wintypes.HANDLE
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        gdi32.SetTextColor.restype = wintypes.DWORD
        gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
        gdi32.SetBkMode.restype = ctypes.c_int
        gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
        # 其余会碰到指针/句柄的调用也必须声明，否则 64 位下会被截成 32 位而报
        # "OverflowError: int too long to convert"（表现为卡片画成空白）
        user32.EndPaint.restype = wintypes.BOOL
        user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UpdateWindow.restype = wintypes.BOOL
        user32.UpdateWindow.argtypes = [wintypes.HWND]
        user32.KillTimer.restype = wintypes.BOOL
        user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.SetTimer.restype = wintypes.UINT
        user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p]
        user32.LoadCursorW.restype = wintypes.HANDLE
        user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.PostQuitMessage.restype = None
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.TranslateMessage.argtypes = [ctypes.c_void_p]
        user32.DispatchMessageW.restype = LRESULT
        user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.CreateRoundRectRgn.restype = wintypes.HANDLE

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                        ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                        ("fIncUpdate", wintypes.BOOL),
                        ("rgbReserved", ctypes.c_byte * 32)]

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)
        # 颜色是 COLORREF(0x00BBGGRR)：错误红 #DC2626、警告橙 #D97706
        state = {"title": title, "text": message,
                 "accent": 0x002626DC if level == "error" else 0x000677D9,
                 "icon": None, "font_t": None, "font_b": None}

        def _draw(hwnd):
            ps = PAINTSTRUCT()
            hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
            try:
                rc = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rc))
                # 底色 + 1px 边框 + 左侧色条
                bg = gdi32.CreateSolidBrush(0x00FFFFFF)
                user32.FillRect(hdc, ctypes.byref(rc), bg)
                border = gdi32.CreateSolidBrush(0x00EAE5E2)     # #e2e5ea
                for r in (wintypes.RECT(0, 0, rc.right, 1),
                          wintypes.RECT(0, rc.bottom - 1, rc.right, rc.bottom),
                          wintypes.RECT(0, 0, 1, rc.bottom),
                          wintypes.RECT(rc.right - 1, 0, rc.right, rc.bottom)):
                    user32.FillRect(hdc, ctypes.byref(r), border)
                bar = wintypes.RECT(0, 0, 5, rc.bottom)
                user32.FillRect(hdc, ctypes.byref(bar),
                                gdi32.CreateSolidBrush(state["accent"]))
                gdi32.SetBkMode(hdc, 1)                         # TRANSPARENT
                if state["icon"]:
                    user32.DrawIconEx(hdc, 18, 20, state["icon"], 32, 32, 0, None, 3)
                gdi32.SetTextColor(hdc, 0x0029231f)
                old = gdi32.SelectObject(hdc, state["font_t"])
                # DT_SINGLELINE|DT_VCENTER|DT_END_ELLIPSIS|DT_NOPREFIX
                user32.DrawTextW(hdc, state["title"], -1,
                                 ctypes.byref(wintypes.RECT(62, 16, rc.right - 16, 42)),
                                 0x00000020 | 0x00000004 | 0x00008000 | 0x00000800)
                gdi32.SetTextColor(hdc, 0x00806B5f)
                gdi32.SelectObject(hdc, state["font_b"])
                # DT_WORDBREAK|DT_NOPREFIX：正文允许换行（多行时不能带 END_ELLIPSIS）
                user32.DrawTextW(hdc, state["text"], -1,
                                 ctypes.byref(wintypes.RECT(62, 44, rc.right - 16,
                                                            rc.bottom - 10)),
                                 0x00000010 | 0x00000800)
                gdi32.SelectObject(hdc, old)
                for h in (bg, border):
                    gdi32.DeleteObject(h)
            finally:
                user32.EndPaint(hwnd, ctypes.byref(ps))

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == 0x000F:                                   # WM_PAINT
                _draw(hwnd)
                return 0
            if msg == 0x0014:                                   # WM_ERASEBKGND
                return 1
            if msg == 0x0113:                                   # WM_TIMER
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0202:                                   # WM_LBUTTONUP
                user32.DestroyWindow(hwnd)
                return 0
            if msg == 0x0021:                                   # WM_MOUSEACTIVATE
                return 3                                        # MA_NOACTIVATE
            if msg == 0x0002:                                   # WM_DESTROY
                user32.KillTimer(hwnd, 1)
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(_wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        cls = "MYSCard_%d" % os.getpid()
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_wchar_p(32512))
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            return False

        dpi = 96
        try:
            dpi = int(user32.GetDpiForSystem()) or 96
        except Exception:
            pass
        s = dpi / 96.0
        w, h = int(400 * s), int(120 * s)
        wa = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)   # SPI_GETWORKAREA
        x = wa.right - w - int(16 * s)
        y = wa.bottom - h - int(16 * s)

        face = "Microsoft YaHei UI"
        state["font_t"] = gdi32.CreateFontW(-int(11 * s), 0, 0, 0, 700, 0, 0, 0, 1,
                                            0, 0, 5, 0, face)
        state["font_b"] = gdi32.CreateFontW(-int(9.5 * s), 0, 0, 0, 400, 0, 0, 0, 1,
                                            0, 0, 5, 0, face)
        icon_path = BASE / "icon.ico"
        if icon_path.exists():
            state["icon"] = user32.LoadImageW(None, str(icon_path), 1, 32, 32, 0x10)

        hwnd = user32.CreateWindowExW(
            0x00000008 | 0x00000080 | 0x08000000,      # TOPMOST | TOOLWINDOW | NOACTIVATE
            cls, cls, 0x80000000,                      # WS_POPUP
            wa.right, y, w, h, None, None, hinst, None)
        if not hwnd:
            return False
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, int(12 * s), int(12 * s))
        user32.SetWindowRgn(hwnd, rgn, True)
        user32.ShowWindow(hwnd, 4)                     # SW_SHOWNOACTIVATE
        user32.SetTimer(hwnd, 1, max(5, int(timeout_sec)) * 1000, None)
        # 从右侧滑入，做出"通知"的感觉
        steps = max(1, int(14 * s))
        for i in range(1, steps + 1):
            user32.SetWindowPos(hwnd, None, wa.right - int(w * i / steps) - int(16 * s),
                                y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
            time.sleep(0.012)
        user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        user32.UpdateWindow(hwnd)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        return True
    except Exception as e:
        write_log("右下角通知卡片失败：%s: %s" % (type(e).__name__, e))
        return False


def _notify_balloon(title: str, message: str, level: str = "warn") -> bool:
    """Shell_NotifyIcon 的任务栏气泡（Win10/11 显示成右下角系统通知）。

    纯 ctypes 实现：不调外部程序、不写注册表。实测在装了 360 的机器上
    会被拒绝（NIM_ADD 返回 ERROR_ACCESS_DENIED），所以它只是"首选"，
    失败后由 _notify_card 兜底。
    """
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        # 64 位下必须显式声明参数/返回类型，否则指针会被截成 32 位而报
        # "OverflowError: int too long to convert"，通知就弹不出来。
        LRESULT = ctypes.c_ssize_t
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.LoadIconW.restype = wintypes.HICON
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                        wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
        NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3
        WM_APP = 0x8000
        WM_TRAY = WM_APP + 21
        IDI_INFORMATION = 32516
        PM_REMOVE = 0x1

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                              ctypes.POINTER(NOTIFYICONDATAW)]

        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "MYSNotifyWnd_%d" % os.getpid()

        def _wndproc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        _proc_ref = WNDPROC(_wndproc)          # 必须留引用，否则回调会被回收
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = _proc_ref
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            raise RuntimeError("RegisterClassEx 失败（错误码 %d）" % kernel32.GetLastError())

        hwnd = user32.CreateWindowExW(0, cls_name, cls_name, 0, 0, 0, 0, 0,
                                      None, None, hinst, None)
        if not hwnd:
            raise RuntimeError("CreateWindowEx 失败（错误码 %d）" % kernel32.GetLastError())

        shown = False
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.c_wchar_p(IDI_INFORMATION))
        nid.szTip = title[:120]
        try:
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                nid.uFlags = NIF_INFO
                nid.szInfoTitle = title[:62]
                nid.szInfo = message[:250]
                nid.dwInfoFlags = {"info": NIIF_INFO, "warn": NIIF_WARNING,
                                   "error": NIIF_ERROR}.get(level, NIIF_WARNING)
                shown = bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
                # 气泡要显示几秒：期间得让它挂着并泵消息，用完再撤掉图标
                import time as _t
                msg = wintypes.MSG()
                end = _t.time() + 7
                while _t.time() < end:
                    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                        user32.TranslateMessage(ctypes.byref(msg))
                        user32.DispatchMessageW(ctypes.byref(msg))
                    _t.sleep(0.1)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        finally:
            user32.DestroyWindow(hwnd)

        if not shown:
            write_log("系统通知未能弹出（Shell_NotifyIcon 返回失败）")
        return shown
    except Exception as e:
        write_log("弹系统通知失败：%s: %s" % (type(e).__name__, e))
        return False


# 需要用户处理的状态（要重新登录 / 签到失败）
BAD_SIGN_STATUS = ("invalid_cookie", "not_logged_in", "failed", "error")
# 真的"签了一次"的状态（"info" 只是查状态、没有发起签到，不发通知）
SIGN_DONE_STATUS = ("claimed", "already") + BAD_SIGN_STATUS


def notify_on_success() -> bool:
    """用户是否希望在"签到成功"后也收到通知（默认开，可在界面里关掉）。

    关掉后只在失败 / 需要重新登录时提醒——适合不想被"每天签到成功"
    这类消息打扰的用户。签到失败的通知不受此设置影响，永远会发。
    """
    return bool(load_settings().get("notify_on_success", True))


def notify_sign_result(result: dict, source: str = "", force: bool = False) -> bool:
    """每次签到结束后都弹一条 Windows 通知——成功、已签过、失败都算。

    手动签到、开机自动签到、计划任务签到都会走到这里，用户随时知道
    "今天签上了 / 早就签过了 / 哪个游戏失败了"。纯状态查询（force_info，
    没有真的发起签到）不发通知，免得每次刷新状态都打扰用户。

    force=True 时无视 notify_on_success 设置（【通知样例】按钮的预览用：
    用户把成功通知关掉后，仍然可以主动看看它长什么样）。
    """
    games = (result or {}).get("games") or {}
    if not games:
        return False
    if not any((g or {}).get("status") in SIGN_DONE_STATUS for g in games.values()):
        return False                      # 全是 info：只查了状态，没签到

    ok_lines, bad = [], []
    for k in GAME_KEYS:                   # 按固定顺序输出，通知里游戏排序稳定
        g = games.get(k) or {}
        st = g.get("status")
        if st not in SIGN_DONE_STATUS:
            continue
        name = g.get("name") or game_name(k)
        days = g.get("days")
        tail = "（本月累计 %s 天）" % days if days not in (None, "") else ""
        if st == "claimed":
            ok_lines.append("【%s】%s%s"
                            % (name, g.get("message") or "签到成功", tail))
        elif st == "already":
            ok_lines.append("【%s】今天已经签到过了%s" % (name, tail))
        else:
            bad.append((name, (g.get("message") or "").strip()))

    need_login = any((games.get(k) or {}).get("status")
                     in ("invalid_cookie", "not_logged_in") for k in games)
    if bad:
        prefix = "米游社签到失败" if not ok_lines else "米游社签到部分完成"
        title = "%s：%s%s" % (prefix, "、".join(n for n, _ in bad),
                              " 需要重新登录" if need_login else "")
        if need_login:
            body = ("登录凭据已失效，有游戏没签成。\n"
                    "请打开「米游社签到助手」，点【登录米游社】重新登录一次。")
            if ok_lines:
                body += "\n" + "；".join(ok_lines)
        else:
            body = "；".join(ok_lines + [m for _, m in bad if m]) \
                or "请打开「米游社签到助手」查看运行日志。"
    else:
        if not force and not notify_on_success():
            return False                  # 用户选择了"成功后不打扰"
        title = "米游社签到完成"
        body = "；".join(ok_lines) or "今天已经没有需要签到的游戏了。"
    if source:
        body = "%s（%s）" % (body, source)
    return notify(title, body, level="error" if bad else "info")


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


# ---------------------------------------------------------------- Playwright 临时目录

def _playwright_env():
    """把 TMP/TEMP 指到程序目录下的 .pw_tmp，返回原始值以便还原。

    Playwright 的驱动是 Node，它按 TMP/TEMP 找临时目录。某些环境（受管控的
    沙箱、权限被改坏的系统 Temp）里 Node 在 %TEMP% 下建不了目录，Playwright
    会直接报：
        EPERM: operation not permitted, mkdtemp '...\\playwright-artifacts-XXXXXX'
    连浏览器窗口都起不来，用户看到的就是"点登录没反应/卡住"。
    指到程序自己可写的位置就能绕开，且不再依赖系统 Temp。
    """
    try:
        PW_TMP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    old = {}
    for name in ("TMP", "TEMP", "TMPDIR"):
        old[name] = os.environ.get(name)
        os.environ[name] = str(PW_TMP_DIR)
    return old


def _restore_env(old):
    if not old:
        return
    for name, val in old.items():
        if val is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = val


def _cleanup_pw_tmp():
    """清掉自己临时目录里的残留（清不掉也无所谓）。"""
    try:
        import shutil
        shutil.rmtree(PW_TMP_DIR, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 登录（Playwright 驱动本机 Edge）

def _drop_session_cookies(ctx) -> int:
    """清掉浏览器里米游社的登录态 cookie（保留设备指纹），返回清掉的项数。"""
    try:
        names = {c["name"] for c in (ctx.cookies() or [])
                 if any(k in (c.get("domain") or "")
                        for k in ("mihoyo", "miyoushe", "hoyolab"))}
    except Exception:
        return 0
    hit = [n for n in names if n in SESSION_COOKIE_NAMES]
    for name in hit:
        try:
            ctx.clear_cookies(name=name)
        except Exception:
            pass
    return len(hit)


def _clear_browser_session(on_log=None) -> bool:
    """用无头 Edge 退出持久化配置里的米游社登录态（不弹窗口）。"""
    emit = _emit_factory(on_log)
    if not PROFILE_DIR.exists():
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    env_backup = _playwright_env()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR), channel="msedge", headless=True,
                args=["--disable-blink-features=AutomationControlled"])
            try:
                _drop_session_cookies(ctx)
                return True
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        emit("退出浏览器登录状态失败（不影响本机凭据）：%s: %s" % (type(e).__name__, e))
        return False
    finally:
        _restore_env(env_backup)
        _cleanup_pw_tmp()


def login(on_log=None, timeout_sec: int = 600) -> dict:
    """打开 Edge 让用户登录米游社，从浏览器引擎层读取 cookie 并加密保存。

    每次登录前都会先清掉浏览器里的米游社登录态（保留设备指纹 cookie），因为：
      1) 不清就会直接复用旧会话，一两秒就"秒登录"，用户既换不了账号，
         也拿不到新的 cookie_token；
      2) 旧会话里的 cookie_token 失效，正是"显示已登录、签到却一直失败"的根因。
    拿到 cookie 后还会真的校验一次会话：只有 cookie_token 会话有效，才算
    "能签到的登录"，避免再来一次假成功。
    """
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
    env_backup = _playwright_env()
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel="msedge",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                # 先关掉会话恢复出来的其它标签页：否则它们可能把旧登录态又写回来
                try:
                    for extra in ctx.pages[1:]:
                        extra.close()
                except Exception:
                    pass
                dropped = _drop_session_cookies(ctx)
                if dropped:
                    emit("已退出浏览器里旧的米游社登录状态（%d 项 cookie），"
                         "请重新登录（这样可以换账号）" % dropped)

                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass

                import time as _t
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
                    cookies = _filter_cookies(raw)
                    if _has_login(cookies):
                        # 等 Set-Cookie 全部落地，免得只抓到一半
                        _t.sleep(2.5)
                        try:
                            cookies = _filter_cookies(ctx.cookies()) or cookies
                        except Exception:
                            pass
                        found = cookies
                        break
                    try:
                        if not ctx.pages:
                            closed = True
                            break
                    except Exception:
                        closed = True
                        break
                    _t.sleep(1)
                    waited += 1
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        msg = "启动浏览器失败：%s: %s" % (type(e).__name__, e)
        text = str(e)
        if "mkdtemp" in text or "EPERM" in text:
            # 最常见的两种：系统临时目录不可写 / 浏览器被安全软件拦住
            msg += ("\n（临时目录或权限问题：程序已改用自带的 %s 目录，"
                    "若仍失败请检查 %%TEMP%% 权限、或重启电脑后再试）"
                    % PW_TMP_DIR.name)
        res["message"] = msg
        emit(msg)
        return res
    finally:
        _restore_env(env_backup)
        _cleanup_pw_tmp()

    if not found:
        if closed:
            res["status"] = "closed"
            res["message"] = "登录窗口在完成登录前被关闭了，未保存凭据，请重试"
        else:
            res["status"] = "timeout"
            res["message"] = "等待 %d 分钟仍未检测到登录状态，请重试" % (timeout_sec // 60)
        emit(res["message"])
        return res

    # 关键一步：校验会话，确认这次拿到的是"能签到"的凭据
    check = verify_session(found)
    res["session"] = check
    emit("会话校验：%s" % check["detail"])

    if check.get("ltoken") is False:
        res["status"] = "invalid"
        res["message"] = ("登录没有真正完成（米游社会话校验未通过：%s）。"
                          "请重试，并在浏览器里改用「短信验证码 / 账号密码」方式登录。"
                          % check["detail"])
        emit(res["message"])
        return res

    sign_ready = check.get("cookie_token")
    try:
        save_cookies(found, sign_ready=sign_ready)
    except Exception as e:
        res["message"] = "保存凭据失败：%s" % e
        emit(res["message"])
        return res

    res["ok"] = sign_ready is not False
    if sign_ready is True:
        res["status"] = "ok"
        res["message"] = "登录成功，已保存 %d 项 cookie，签到凭据校验通过" % len(found)
    elif sign_ready is False:
        res["status"] = "partial"
        res["message"] = ("已保存 %d 项 cookie，但米哈游没有给出可用的 cookie_token"
                          "（%s），签到仍可能失败。建议再登录一次，"
                          "或改用短信验证码 / 账号密码方式登录。"
                          % (len(found), check["detail"]))
    else:
        res["status"] = "ok_unverified"
        res["message"] = ("登录成功，已保存 %d 项 cookie；"
                          "但本次无法校验签到凭据（%s）"
                          % (len(found), check["detail"]))
    emit(res["message"])

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
                res["message"] = ("登录凭据已失效或不完整（游戏签到需要有效的 "
                                  "cookie_token 会话），请点「登录米游社」重新登录")
                emit("%s：%s" % (res["name"], res["message"]))
                # 只标记"签到凭据不可用"，不要顺手把 cred_ok 也置 False：
                # 此时登录态可能仍然有效（能查到角色），右上角该显示
                # "已登录 · 签到凭据不完整"这种可操作的状态，而不是"未登录"。
                save_state(sign_ready=False)
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

    game_keys 为空表示按用户在界面上勾选的游戏（默认全部）。
    返回 {"ok": bool, "games": {key: 结果}}。
    """
    keys = tuple(game_keys) if game_keys else tuple(selected_games())
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

def task_command() -> str:
    """计划任务要执行的命令行。

    源码模式：用 .venv 的 pythonw.exe（无黑窗）跑 cli.py。
    打包模式：exe 自己就能带 sign 参数跑（app.pyw 会把参数转给 cli），
    既不需要额外的解释器，也没有 cli.py 这个文件可指。
    """
    if is_frozen():
        return '"%s" sign' % sys.executable
    pw = BASE / ".venv" / "Scripts" / "pythonw.exe"
    py = str(pw) if pw.exists() else sys.executable
    return '"%s" "%s" sign' % (py, BASE / "cli.py")


def task_action() -> tuple:
    """计划任务的 (程序, 参数)，供 XML 注册使用（路径含空格也安全）。"""
    if is_frozen():
        return sys.executable, "sign"
    pw = BASE / ".venv" / "Scripts" / "pythonw.exe"
    py = str(pw) if pw.exists() else sys.executable
    return py, '"%s" sign' % (BASE / "cli.py")


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 注册计划任务用的 XML 模板。不用 schtasks /TR 命令行方式，是因为那样建出的
# 任务带一组对"每日签到"很不友好的默认开关：
#   StartWhenAvailable=false   电脑关机/睡眠错过触发点后，开机也不补跑
#   DisallowStartIfOnBatteries=true / StopIfGoingOnBatteries=true  电池不跑
#   WakeToRun=false            睡眠不唤醒（保持默认，唤醒副作用多）
# XML 里显式把前三个配成对签到友好的值：错过后下次开机尽快补跑一次，电池也跑。
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%(start)s</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(command)s</Command>
      <Arguments>%(arguments)s</Arguments>
    </Exec>
  </Actions>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>%(run_level)s</RunLevel>
    </Principal>
  </Principals>
</Task>
"""


def _is_admin() -> bool:
    """当前进程是否以管理员权限运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_admin() -> bool:
    """对外暴露的权限判断（界面顶部徽标、自检报告用）。

    普通权限下签到本身照常能用，但注册 / 删除计划任务会被系统拒绝
    （于是程序要弹一次 UAC），所以界面上要让用户一眼看到当前是什么身份。
    """
    return _is_admin()


def admin_label() -> str:
    return "管理员权限" if is_admin() else "普通权限"


# ------------------------------------------------ 计划任务该用哪种权限级别
#
# 计划任务默认用「最少权限」(LeastPrivilege) 运行，正常情况下就够了。但有两种
# 机器上必须以管理员身份跑，否则任务每次触发都会立刻失败：
#
#   1. 程序 exe 被勾了「以管理员身份运行」（兼容性设置）——Windows 会把它记进
#      HKCU\...\AppCompatFlags\Layers，值是 "~ RUNASADMIN"。这种 exe 被普通权限
#      启动时直接返回 WinError 740（请求的操作需要提升），任务计划程序记录的
#      上次结果是 0x800702E4，进程连一行日志都写不出来。
#   2. 凭据/设置文件只有管理员能读（便携版如果曾用管理员权限跑过，写出来的
#      cookie.enc、settings.json 可能继承到"仅管理员可读"的 ACL）。这种时候
#      任务能启动，但读不到登录态，签到必然失败。
#
# 所以注册任务前先探测一遍，需要提权就把 XML 里的 RunLevel 写成
# HighestAvailable，这样任务运行时不再需要 UAC 弹窗，也不会 740。

_LAYERS_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"


def _norm_path(p: str) -> str:
    """规范化路径用于比较：去掉引号、统一大小写、转绝对路径。

    注册表里的值名有时带引号（"C:\\…\\x.exe"），直接 abspath 会被当成
    相对路径算出个乱七八糟的结果。
    """
    s = str(p or "").strip().strip('"')
    try:
        return os.path.normcase(os.path.abspath(s))
    except Exception:
        return os.path.normcase(s)


def _compat_runas_marked(path: str) -> bool:
    """目标程序是否被勾选了「以管理员身份运行」（兼容性 → 特权等级）。"""
    if not path:
        return False
    try:
        import winreg
    except Exception:
        return False

    target = _norm_path(path)
    # 兼容性设置里可能存短路径（8.3）或带引号的写法，所以逐个值名比对；
    # HKCU 存的是用户自己勾的，HKLM 存的是管理员为所有用户设的。
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _LAYERS_KEY) as k:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    if "RUNASADMIN" not in str(value).upper():
                        continue
                    if _norm_path(name) == target:
                        return True
        except OSError:
            continue
    return False


def _can_read(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False


def _task_needs_elevation() -> bool:
    """自动签到的子进程是否必须以管理员身份运行。"""
    if is_frozen():
        target = sys.executable
    else:
        target = _pythonw_exe() or sys.executable
    if _compat_runas_marked(target):
        return True

    # 凭据/设置只有管理员读得到 → 普通权限跑起来也是白跑。
    # 注意要在"当前不是管理员"的前提下判断：管理员进程读什么都成功，
    # 用它来判断会永远得出"没问题"的错误结论。
    if not _is_admin():
        for p in (CRED_FILE, SETTINGS_FILE):
            if p.exists() and not _can_read(p):
                return True
    return False


def _task_run_level() -> str:
    """计划任务该用的权限级别：需要用管理员跑时才写 HighestAvailable。"""
    return "HighestAvailable" if _task_needs_elevation() else "LeastPrivilege"


def _looks_missing(text: str, admin: bool = False) -> bool:
    """schtasks 的报错是不是"任务不存在"。

    坑一：中文系统下"任务不存在"的措辞不统一（找不到指定的任务/文件/路径）。
    坑二：同一个命令在提权进程里报中文、普通权限下报英文（实测）。
    坑三："找不到指定的路径"在普通权限下更可能是被安全软件拦了，不能当成"没有"。
    """
    t = (text or "").lower()
    if "找不到指定的任务" in text or "找不到指定的文件" in text:
        return True
    if "cannot find the file" in t or "cannot find the task" in t:
        return True
    if "找不到指定的路径" in text or "cannot find the path" in t:
        return bool(admin)
    return False


def _query_state(name: str) -> str:
    """查询计划任务：'yes' 有 / 'no' 确实没有 / 'unknown' 查不了（被拦）。"""
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", name],
                           capture_output=True, text=True,
                           encoding="gbk", errors="replace")
    except Exception:
        return "unknown"
    if r.returncode == 0:
        return "yes"
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    return "no" if _looks_missing(text, _is_admin()) else "unknown"


def _query(name: str) -> bool:
    """查询计划任务是否存在（区分不了"没有"和"查不了"时按没有处理）。"""
    return _query_state(name) == "yes"


# ---- 备用方案（不依赖计划任务、也绝不调用 PowerShell）--------------------
#
# 实测：360 安全卫士会把本程序拉起的 powershell.exe 判成 Trojan.Generic
# 直接结束进程（并弹"木马查杀"警报）。所以程序不再调用 PowerShell，
# 备用方案只用两种纯 Python 手段：
#   ① 注册表启动项 HKCU\...\CurrentVersion\Run（winreg）
#   ② 启动文件夹里放一个 .cmd（纯文件写入；登录后自动签到一次）
# 二者与计划任务等效：都是"开机/登录后自动签到一次"。

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "MiyousheSignInAssistant"
STARTUP_FILE_NAME = "米游社签到助手（开机签到）.cmd"
# 早期版本用 PowerShell 建过 .lnk，保留识别与清理，免得用户关不掉
STARTUP_LEGACY_NAMES = ("米游社签到助手（开机签到）.lnk",)


def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup")


def _startup_file() -> Path:
    return _startup_dir() / STARTUP_FILE_NAME


def _startup_files() -> list:
    return [_startup_dir() / n for n in (STARTUP_FILE_NAME,) + STARTUP_LEGACY_NAMES]


def _startup_enabled() -> bool:
    try:
        return any(f.exists() for f in _startup_files())
    except Exception:
        return False


def _run_key_command() -> str:
    exe, args = task_action()
    return '"%s" %s' % (exe, args)


def _run_key_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE_NAME)
        return True
    except Exception:
        return False


def _create_run_key() -> tuple:
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUN_VALUE_NAME, 0, winreg.REG_SZ, _run_key_command())
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _create_startup_file() -> tuple:
    """最后的后路：启动文件夹里放一个 .cmd（纯文件写入，不起任何外部程序）。"""
    try:
        _startup_dir().mkdir(parents=True, exist_ok=True)
        exe, args = task_action()
        _startup_file().write_text(
            '@echo off\r\nrem 米游社签到助手：登录 Windows 后自动签到一次\r\n'
            'start "" /min "%s" %s\r\n' % (exe, args), encoding="gbk")
        return _startup_file().exists(), ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _remove_extra_autostart(emit=None) -> bool:
    """清掉注册表启动项与启动文件夹里的备用项，返回是否清掉了东西。"""
    removed = False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE_NAME)
            removed = True
    except Exception:
        pass
    try:
        for f in _startup_files():
            if f.exists():
                f.unlink()
                removed = True
    except Exception:
        pass
    if removed and emit:
        emit("已移除开机启动项")
    return removed


# 查询计划任务要起进程，界面每次刷新都查会很卡，所以缓存一小会儿
_TASK_CACHE = {"at": 0.0, "data": None}


def task_exists() -> bool:
    return task_detail().get("exists", False)


TASK_RUNNING = 267009          # 0x41301：任务正在运行，不算失败


def _parse_task_result(raw: str):
    """把 schtasks 的「上次结果」转成整数；"从未运行"返回 None。"""
    s = (raw or "").strip()
    if not s or "从未" in s or "尚未" in s or s.lower().startswith("never"):
        return None
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except ValueError:
        return None


def task_failure_hint(td: dict, short: bool = False) -> str:
    """把「上次运行结果」翻译成一句人话；一切正常时返回空字符串。

    任务状态永远显示"就绪"，所以只看 State 会以为自动签到好好的，实际上它
    可能每天都因 740 失败、一行日志都写不出来。界面必须把上次结果摆出来。
    short=True 给界面用（状态栏那一行放不下长句），否则给自检报告用。
    """
    code = (td or {}).get("last_result")
    if code is None or code == 0 or code == TASK_RUNNING:
        return ""
    reasons = {
        0x800702E4: ("需要管理员权限（740），点一次「开启 / 更新」重建任务即可",
                     "需要管理员权限，点「开启 / 更新」修复"),
        0x80070005: ("权限不足（拒绝访问）", "权限不足，重开自动签到"),
        0x80070424: ("任务计划程序服务不可用", "任务计划服务不可用"),
        0x41303: ("任务从未运行过", "任务从未运行过"),
    }
    pair = reasons.get(code)
    if pair:
        return "上次自动签到失败：%s" % (pair[1] if short else pair[0])
    return "上次自动签到失败：错误码 0x%X" % code


def task_detail(use_cache: bool = True) -> dict:
    """返回 {exists, time, next_run, mode, last_result, last_result_text}

    mode: "task" = 系统计划任务；"runkey"/"startup" = 备用开机启动方式。
    """
    import time as _t
    if use_cache and _TASK_CACHE["data"] is not None \
            and _t.time() - _TASK_CACHE["at"] < 20:
        return dict(_TASK_CACHE["data"])

    out = {"exists": False, "time": None, "next_run": None, "mode": None}
    target = None
    target = ""
    unqueryable = False
    for n in (TASK_NAME,) + ALT_TASK_NAMES + LEGACY_TASK_NAMES:
        state = _query_state(n)
        if state == "yes":
            target = n
            break
        if state == "unknown":
            unqueryable = True

    if not target and unqueryable and load_settings().get("autostart") == "task":
        # 管理员建好的任务，普通权限的本程序常常"查不到"（安全软件连 schtasks
        # 查询一起拦）。此时以设置里记录的 autostart=task 为准，避免状态显示错、
        # 或者每次开机都重新注册一遍。
        target = TASK_NAME

    if target:
        out["exists"] = True
        out["mode"] = "task"
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
            # 「上次结果」是判断自动签到到底有没有跑成功的唯一可靠证据：
            # 任务状态永远显示"就绪"，哪怕它每次触发都失败。
            m3 = re.search(r"(?:上次结果|Last Result):\s*([^\r\n]+)", text)
            if m3:
                raw = m3.group(1).strip()
                out["last_result_text"] = raw
                out["last_result"] = _parse_task_result(raw)
        except Exception:
            pass
    elif _run_key_enabled():
        out.update(exists=True, mode="runkey",
                   time=load_settings().get("task_time") or "09:05")
    elif _startup_enabled():
        out.update(exists=True, mode="startup",
                   time=load_settings().get("task_time") or "09:05")

    _TASK_CACHE.update(at=_t.time(), data=dict(out))
    return out


def _create_task_via_schtasks(xml_path: str, task_name: str = TASK_NAME) -> tuple:
    """用系统自带的 schtasks 注册计划任务（不调用 PowerShell）。"""
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", task_name, "/XML", xml_path, "/F"],
            capture_output=True, text=True, encoding="gbk", errors="replace")
    except Exception as e:
        return False, "无法调用 schtasks：%s: %s" % (type(e).__name__, e)
    if r.returncode != 0:
        return False, ((r.stdout or "") + (r.stderr or "")).strip()
    # 复核：schtasks 返回 0 也要确认任务真的在（避免假成功）
    return _query(task_name), "任务已创建但查询不到"


def run_elevated(parameters: str, timeout_sec: int = 180) -> tuple:
    """用管理员权限运行本程序的 CLI（会弹一次 UAC），并等它结束。

    为什么需要：实测在装了 360 之类安全软件的机器上，**普通权限**运行的本程序
    （以及它拉起的任何子进程，包括打包好的 exe）做"注册表启动项 / 启动文件夹 /
    计划任务"这类系统写入时会被无声拒绝——同一个任务 XML 由 PowerShell 执行却
    立刻成功，说明这是按"未知/未签名程序"下发的策略。以管理员身份运行同样的
    命令则全部成功，所以这里用 ShellExecuteExW("runas") 请求一次提权，由用户
    在 UAC 窗口确认。
    返回 (是否成功, 说明)；用户点了"否"时说明里会带 ERROR_CANCELLED。
    """
    exe = _pythonw_exe() or sys.executable
    return run_elevated_exe(exe, '"%s" %s' % (BASE / "cli.py", parameters), timeout_sec)


def run_elevated_exe(exe: str, parameters: str, timeout_sec: int = 180) -> tuple:
    """用管理员权限运行指定程序（ShellExecuteExW "runas"，会弹一次 UAC）并等它结束。"""
    if sys.platform != "win32":
        return False, "仅支持 Windows"
    try:
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                        ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                        ("hProcess", wintypes.HANDLE)]

        shell32.ShellExecuteExW.restype = wintypes.BOOL
        shell32.ShellExecuteExW.argtypes = [ctypes.c_void_p]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        info = SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        info.fMask = 0x00000040                      # SEE_MASK_NOCLOSEPROCESS
        info.lpVerb = "runas"
        info.lpFile = str(exe)
        info.lpParameters = parameters
        info.lpDirectory = str(BASE)
        info.nShow = 1                               # SW_SHOWNORMAL
        if not shell32.ShellExecuteExW(ctypes.byref(info)):
            err = ctypes.get_last_error()
            return False, ("提权被取消（错误 %d）" % err if err == 1223
                           else "无法提权（错误 %d）" % err)
        kernel32.WaitForSingleObject(info.hProcess, max(10, timeout_sec) * 1000)
        kernel32.CloseHandle(info.hProcess)
        return True, "提权命令已执行完"
    except Exception as e:
        return False, "提权失败：%s: %s" % (type(e).__name__, e)


# ---- 通知助手：定位 / 状态 / 一键启用 -------------------------------------

NOTIFY_HELPER_DIRS = ("通知助手", ".toast_pkg/toast_helper")
HELPER_RESULT_FILE = (Path(os.environ.get("LOCALAPPDATA", ""))
                      / "MiyousheSignInAssistant" / "register.result")


def toast_helper_exe() -> Path:
    """找随程序分发的"通知助手"可执行文件（便携版 / 源码构建两种布局）。"""
    for rel in NOTIFY_HELPER_DIRS:
        p = BASE / rel / "toast_helper.exe"
        if p.exists():
            return p
    return BASE / NOTIFY_HELPER_DIRS[0] / "toast_helper.exe"


def toast_registered() -> bool:
    """通知助手是否已经注册成"已安装应用"（只有这样才发得出真通知）。"""
    return toast_alias().exists()


def _installed_helper_version() -> str:
    """读取"已注册副本"里清单的版本号（读不到返回空串）。"""
    manifest = HELPER_RESULT_FILE.parent / "pkg" / "AppxManifest.xml"
    try:
        m = re.search(r'<Identity[^>]*Version="([^"]+)"',
                      manifest.read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except OSError:
        return ""


def _bundled_helper_version() -> str:
    """读取"随程序分发"的助手清单版本号。"""
    helper = toast_helper_exe()
    try:
        m = re.search(r'<Identity[^>]*Version="([^"]+)"',
                      (helper.parent / "AppxManifest.xml").read_text(encoding="utf-8"))
        return m.group(1) if m else ""
    except OSError:
        return ""


def toast_helper_outdated() -> bool:
    """注册的助手是不是旧版本（分发的比已注册的新 → 需要一次升级注册）。"""
    have, want = _installed_helper_version(), _bundled_helper_version()
    return bool(have and want and have != want)


def setup_toast(on_log=None) -> dict:
    """一键启用真正的系统通知：把随程序分发的通知助手注册成已安装应用。

    需要一次管理员权限（UAC）：
      · 助手会把自己复制到 %LOCALAPPDATA%\\MiyousheSignInAssistant\\pkg（稳定位置，
        便携版文件夹之后可以随便搬动）
      · 需要时打开 Windows「开发者模式」（免签名松散注册的前提）
      · 用 WinRT 的 PackageManager 注册，成功后系统通知即可用
    不想用了：`python build_toast_package.py --uninstall`，或在「设置 → 应用」里
    卸载"米游社签到助手"。
    """
    emit = _emit_factory(on_log)
    if toast_registered() and not toast_helper_outdated():
        return {"ok": True, "already": True, "message": "系统通知已经可用"}
    if toast_registered():
        emit("已装的系统通知组件版本较旧，顺手升级一次。")
    helper = toast_helper_exe()
    if not helper.exists():
        msg = ("这个版本里没有带通知助手，无法启用系统通知"
               "（便携版请用 v3.0.1 及以后的包；源码版可运行 build_toast_package.py 构建）")
        emit(msg)
        return {"ok": False, "message": msg}

    emit("正在启用系统通知：会弹一次管理员权限（UAC），请点【是】。\n"
         "这一步只做两件事：① 在需要时打开 Windows「开发者模式」"
         "（免签名注册应用的前提，可随时关掉）；\n"
         "② 把通知助手注册成已安装应用，文件放在 %LOCALAPPDATA%\\MiyousheSignInAssistant"
         "，随时可卸载。")
    try:
        HELPER_RESULT_FILE.unlink()
    except OSError:
        pass
    ok, detail = run_elevated_exe(str(helper), "--register-self", timeout_sec=300)
    if not ok:
        emit(detail)
        return {"ok": False, "message": detail}

    result = ""
    try:
        result = HELPER_RESULT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not result and toast_registered():
        result = "MYS-REGISTER:ok"
    if result.startswith("MYS-REGISTER:ok"):
        msg = "系统通知已启用：以后签到失败会弹出真正的 Windows 通知（右下角 + 通知中心）"
        emit(msg)
        return {"ok": True, "message": msg}
    if "need-admin" in result:
        msg = "注册需要管理员权限，但这次没有获得（UAC 被取消或失败）"
    elif result.startswith("MYS-REGISTER:failed"):
        msg = "注册失败：%s" % result.split(":", 2)[-1][:160]
    else:
        msg = "注册没成功（助手没有返回结果；可看运行日志或重试一次）"
    emit(msg)
    return {"ok": False, "message": msg}


def create_task_elevated(time_str: str = "09:05", on_log=None,
                         run_level: str = "") -> dict:
    """用管理员权限注册计划任务（UAC 一次），成功后按子进程写的结论更新状态。

    run_level 传 "HighestAvailable" 时，提权子进程建出来的任务本身就是
    「使用最高权限运行」，之后每次触发都不需要 UAC，也不会因 740 失败。
    """
    emit = _emit_factory(on_log)
    emit("正在请求管理员权限——请在弹出的 UAC 窗口点「是」（只此一次）。")
    try:
        TASK_RESULT_FILE.unlink()
    except OSError:
        pass
    args = "task --time %s" % time_str
    if run_level in ("HighestAvailable", "LeastPrivilege"):
        args += " --run-level %s" % run_level
    ok, detail = run_elevated(args)
    _TASK_CACHE["at"] = 0.0
    if not ok:
        emit(detail)
        return {"ok": False, "message": detail}
    res = read_task_result()
    if res.get("ok"):
        msg = res.get("message") or "已用管理员权限开启每日自动签到"
        save_settings(task_time=time_str, autostart="task")
        emit(msg)
        return {"ok": True, "message": msg, "mode": res.get("mode") or "task"}
    emit(res.get("message") or "提权命令执行完了，但没有成功注册（详见运行日志）")
    return {"ok": False, "message": res.get("message") or "提权后仍未注册成功"}


def delete_task_elevated(on_log=None) -> dict:
    """用管理员权限删除计划任务（UAC 一次）。"""
    emit = _emit_factory(on_log)
    emit("需要管理员权限才能删除计划任务，请在弹出的 UAC 窗口点「是」（只此一次）。")
    try:
        TASK_RESULT_FILE.unlink()
    except OSError:
        pass
    ok, detail = run_elevated("untask")
    _TASK_CACHE["at"] = 0.0
    if not ok:
        emit(detail)
        return {"ok": False, "message": detail}
    res = read_task_result()
    if res.get("ok") or not task_detail(use_cache=False).get("exists"):
        msg = res.get("message") or "已关闭每日自动签到"
        emit(msg)
        return {"ok": True, "message": msg}
    emit(res.get("message") or "提权后仍未能关闭，详见运行日志")
    return {"ok": False, "message": res.get("message") or "提权后仍未能关闭"}


TASK_RESULT_FILE = BASE / ".task_result.json"


def write_task_result(res: dict):
    """把计划任务注册/删除的结果落盘。

    提权运行的 CLI 是被 ShellExecuteExW 拉起的独立进程，父进程拿不到它的返回值；
    而普通权限的父进程又常常"查不到"管理员创建的任务（安全软件连 schtasks 查询
    都拦），所以让子进程把结论写在这个文件里，父进程读完即知成败。
    """
    try:
        TASK_RESULT_FILE.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_task_result() -> dict:
    try:
        data = json.loads(TASK_RESULT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _create_task_via_helper(time_str: str) -> tuple:
    """让"打包助手"（已安装的 MSIX 应用）替我们注册开机自启。

    有些机器只拦 python 进程（普通权限下写启动项/计划任务被拒），却放行已安装的
    打包应用；助手就是干这个用的，成功时返回 (True, 模式名)。
    """
    pythonw = _pythonw_exe()
    if not pythonw:
        return False, "找不到 pythonw.exe"
    cli = BASE / "cli.py"
    args = ["--autostart-create", time_str, "--pythonw", pythonw, "--script", str(cli)]
    for cmd in ([str(toast_alias())], [TOAST_ALIAS_NAME]):
        try:
            r = subprocess.run(cmd + args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            continue
        except Exception as e:
            return False, "%s: %s" % (type(e).__name__, e)
        for line in (r.stdout or "").splitlines():
            if line.startswith("MYS-AUTOSTART:"):
                mode = line.split(":", 1)[1].strip()
                return (mode not in ("", "failed")), (mode or "failed")
        return False, ((r.stderr or "").strip()[:120] or "助手没有返回结果")
    return False, "找不到通知助手"


def _pythonw_exe() -> str:
    for name in ("pythonw.exe", "python.exe"):
        p = BASE / ".venv" / "Scripts" / name
        if p.exists():
            return str(p)
    return ""


def create_task(time_str: str = "09:05", on_log=None,
                run_level: str = "") -> dict:
    emit = _emit_factory(on_log)
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str or ""):
        msg = "时间格式不对，应为 HH:MM（例如 09:05）"
        emit(msg)
        return {"ok": False, "message": msg}

    # 决定任务的权限级别。提权子进程里判断"凭据是否只有管理员可读"是不可靠的
    # （管理员读什么都成功），所以由调用方把普通权限下探测到的结论显式传进来。
    level = run_level if run_level in ("HighestAvailable", "LeastPrivilege") \
        else _task_run_level()

    # 需要管理员权限、当前却不是：先别做注定失败的尝试，直接让上层请求 UAC。
    if level == "HighestAvailable" and not _is_admin():
        msg = ("这台机器上的签到程序需要管理员权限才能正常运行"
               "（程序被设为「以管理员身份运行」，或凭据文件仅管理员可读）。"
               "接下来会请求一次管理员权限来注册计划任务，请在 UAC 窗口点「是」。")
        emit(msg)
        return {"ok": False, "message": msg, "need_elevated": True,
                "run_level": "HighestAvailable",
                "status_text": "需要管理员权限，正在请求提权…"}

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

    cmd, args = task_action()
    created_name = None
    last_detail = ""

    # 任务名也要留后路：实测安全软件会把某个名字"记住"并永久拒绝创建
    # （同样的 XML 换个名字就能建成功），所以主名字失败就换备用名字再试。
    for task_name in (TASK_NAME,) + ALT_TASK_NAMES:
        xml_text = _TASK_XML % {
            "start": datetime.now().strftime("%Y-%m-%d") + "T" + time_str + ":00",
            "command": _xml_escape(cmd),
            "arguments": _xml_escape(args),
            "user": "%s\\%s" % (os.environ.get("USERDOMAIN", ""),
                                os.environ.get("USERNAME", "")),
            "run_level": level,
        }
        import tempfile
        fd, xml_path = tempfile.mkstemp(suffix=".xml", prefix="mys-task-")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as f:
                f.write(xml_text)
            ok, detail = _create_task_via_schtasks(xml_path, task_name)
            if ok:
                created_name = task_name
            else:
                last_detail = detail
                emit("注册计划任务「%s」没成功" % task_name)
        except Exception as e:
            last_detail = "%s: %s" % (type(e).__name__, e)
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass
        if created_name:
            break

    if created_name:
        _TASK_CACHE["at"] = 0.0
        save_settings(task_time=time_str, autostart="task")
        msg = "已开启每日自动签到：每天 %s 后台签到 %s" % (
            time_str, "、".join(game_name(k) for k in selected_games()))
        if level == "HighestAvailable":
            msg += "（已按本机情况设为管理员权限运行，运行时不会弹 UAC）"
        emit(msg)
        return {"ok": True, "message": msg, "mode": "task",
                "task_name": created_name, "run_level": level}

    # 第二条路：交给"打包助手"（已安装应用）试一次——有些机器只拦 python，
    # 却放行已安装的打包应用，这样就不用麻烦用户点 UAC。
    if not created_name:
        ok_h, mode_h = _create_task_via_helper(time_str)
        if ok_h:
            _TASK_CACHE["at"] = 0.0
            save_settings(task_time=time_str, autostart=mode_h or "task")
            msg = ("已开启每日自动签到：每天 %s 后台签到 %s（由通知助手注册）"
                   % (time_str, "、".join(game_name(k) for k in selected_games())))
            emit(msg)
            return {"ok": True, "message": msg, "mode": mode_h or "task"}
        last_detail = last_detail or ("助手方式：%s" % mode_h)

    # 第三条路：普通权限的纯 Python 手段（注册表启动项 → 启动文件夹）
    for label, maker, mode in (("注册表启动项", _create_run_key, "runkey"),
                               ("启动文件夹", _create_startup_file, "startup")):
        try:
            ok, detail = maker()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        if ok:
            _TASK_CACHE["at"] = 0.0
            save_settings(task_time=time_str, autostart=mode)
            msg = ("系统计划任务被安全软件拦住了，已改用【%s】方式："
                   "每次登录 Windows 后自动签到一次（当天没签的会自动补签）。"
                   % label)
            emit(msg)
            return {"ok": True, "message": msg, "mode": mode}
        last_detail = last_detail or detail

    msg = ("普通权限下无法注册开机自动签到：系统计划任务、注册表启动项、"
           "启动文件夹三条路都被拒绝了。\n"
           "这通常是安全软件对「未知/未签名程序」下发的策略"
           "——实测同一个任务 XML 交给 PowerShell 执行却能成功。\n"
           "程序接下来会请求一次管理员权限（UAC）再试：在弹窗里点「是」即可。")
    if last_detail:
        msg += "\n（技术细节：%s）" % last_detail.replace("\n", " ")[:150]
    emit(msg)
    return {"ok": False, "message": msg,
            # 状态栏只显示这一句，完整说明在运行日志里
            "status_text": "普通权限被拒，正在请求管理员权限…"}


def delete_task(on_log=None) -> dict:
    """关闭每日自动签到（普通权限）。删不动计划任务时返回 ok=False，
    让界面用管理员权限再试一次。"""
    emit = _emit_factory(on_log)
    removed, blocked, extra = False, False, []
    for n in (TASK_NAME,) + ALT_TASK_NAMES + LEGACY_TASK_NAMES:
        legacy = n in LEGACY_TASK_NAMES
        state = _query_state(n)
        if state == "no":
            continue                                  # 本来就没有
        if state == "unknown":
            # 查不了时：当前名字按"设置里记着是任务模式"处理；历史名字无法确认，
            # 不动它——删一个不存在的任务会报错，容易被误判成"被安全软件拦了"
            if legacy or load_settings().get("autostart") != "task":
                continue
        try:
            r = subprocess.run(["schtasks", "/Delete", "/TN", n, "/F"],
                               capture_output=True, text=True,
                               encoding="gbk", errors="replace")
        except Exception as e:
            if not legacy:
                extra.append("无法调用 schtasks（%s: %s）" % (type(e).__name__, e))
                blocked = True
            continue
        if r.returncode == 0:
            removed = True
            continue
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        if _looks_missing(text, _is_admin()):
            continue                                  # 任务不存在，不算失败
        if not legacy:
            extra.append("删除 %s 失败：%s" % (n, text[:120]))
            blocked = True
    # 备用方案（注册表启动项 / 启动文件夹）也要一起关掉
    if _remove_extra_autostart(emit):
        removed = True
    _TASK_CACHE["at"] = 0.0
    for m in extra:
        emit(m)
    if blocked:
        msg = "普通权限删不掉计划任务（被系统拒绝），接下来会请求管理员权限再试"
        emit(msg)
        return {"ok": False, "message": msg}
    if removed:
        save_settings(autostart="")
    msg = "已关闭每日自动签到" if removed else "当前没有已注册的自动签到任务"
    emit(msg)
    return {"ok": True, "message": msg}


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

    sel = selected_games()
    for g in detail:
        g["selected"] = g["key"] in sel

    return {
        "cred_ok": bool(cred_flag) and CRED_FILE.exists(),
        # sign_ready：凭据是否通过 cookie_token 会话校验（True/False/None=未知）。
        # 用它在界面上区分"登录了"和"登录了但签到凭据不完整"。
        "sign_ready": st.get("sign_ready"),
        "login_time": st.get("login_time"),
        "task_time": settings.get("task_time", "09:05"),
        "notify_on_success": notify_on_success(),
        "selected": sel,
        "game_list": detail,
    }


# ---------------------------------------------------------------- 检查更新
#
# 只查 GitHub 的 releases/latest，不下载、不自动替换——把"有新版本"和发布页
# 告诉用户就够了。国内直连 GitHub API 经常超时，所以：超时给 8 秒，失败只返回
# 一句话，绝不影响签到主流程；请求会走系统代理，开了代理也能查。

UPDATE_API = "https://api.github.com/repos/%s/releases/latest" % GITHUB_REPO
UPDATE_PAGE = "https://github.com/%s/releases/latest" % GITHUB_REPO
UPDATE_TIMEOUT = 8


def _version_tuple(text: str) -> tuple:
    """"3.2.0" -> (3, 2, 0)，方便比较大小；非数字部分忽略。"""
    nums = re.findall(r"\d+", str(text or ""))
    return tuple(int(n) for n in nums[:3]) or (0,)


def _system_proxy() -> str:
    """读 Windows 系统代理（"设置 → 网络和 Internet → 代理"里填的那个）。

    urllib 默认只看 http_proxy 环境变量，而绝大多数人是在系统设置里填的，
    所以要自己从注册表读，否则开着代理也查不到更新。
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
            server = str(winreg.QueryValueEx(k, "ProxyServer")[0] or "")
    except Exception:
        return ""
    if not enable or not server:
        return ""
    # ProxyServer 可能是 "127.0.0.1:7890"，也可能是
    # "http=127.0.0.1:7890;https=127.0.0.1:7890"
    for part in server.split(";"):
        part = part.strip()
        if "=" in part:
            scheme, _, addr = part.partition("=")
            if scheme.strip().lower() in ("https", "http") and addr.strip():
                return addr.strip()
        elif part:
            return part
    return ""


def _http_get_json(url: str, timeout: int = UPDATE_TIMEOUT) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "MYS-SignIn-Helper/%s (+https://github.com/%s)"
                      % (VERSION, GITHUB_REPO),
        "Accept": "application/vnd.github+json",
    })
    handlers = []
    proxy = _system_proxy()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _net_error_text(e: Exception) -> str:
    """把网络异常翻成用户看得懂的一句话。"""
    text = "%s: %s" % (type(e).__name__, e)
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return "检查更新超时（GitHub 在国内经常连不上，稍后再试或开代理）"
    if "getaddrinfo" in low or "name or service" in low or "11001" in text:
        return "检查更新失败：解析不了 GitHub 域名（网络不通或被拦截）"
    if "403" in text or "rate limit" in low:
        return "检查更新失败：GitHub 接口限流，过一会儿再试"
    return "检查更新失败：%s" % text[:120]


def _latest_tag_via_redirect(timeout: int = UPDATE_TIMEOUT):
    """用 github.com/<repo>/releases/latest 的 302 跳转取最新 tag。

    比 REST API 稳得多：API 对未认证请求限流（同一出口 IP 每小时 60 次，
    开代理时很容易被同 IP 的其他人耗光），而网页跳转不受这个限制。
    """
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    req = urllib.request.Request(
        UPDATE_PAGE, method="HEAD",
        headers={"User-Agent": "MYS-SignIn-Helper/%s" % VERSION,
                 # releases/latest 的跳转会经过 CDN，刚发版时可能还指向上一版，
                 # 带上 no-cache 尽量减少这种延迟（几分钟内仍可能滞后，可接受）
                 "Cache-Control": "no-cache", "Pragma": "no-cache"})
    handlers = [_NoRedirect()]
    proxy = _system_proxy()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            location = r.headers.get("Location") or r.url or ""
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location") or ""
    m = re.search(r"/tag/([^/?#]+)", location or "")
    return m.group(1).strip() if m else ""


def check_update(timeout: int = UPDATE_TIMEOUT) -> dict:
    """查询 GitHub 上的最新版本。

    返回 {ok, current, latest, has_update, url, notes, published, asset_url, message}。
    ok=False 表示"查不到"（断网 / 超时 / 被墙），不等于"没有新版本"。
    """
    errors = []

    # 主路径：网页跳转（不限流）
    latest = ""
    try:
        latest = _latest_tag_via_redirect(timeout).lstrip("vV")
    except Exception as e:
        errors.append(_net_error_text(e))

    # 补充信息（发布说明、下载直链）：API 失败不影响"有没有新版本"这个结论
    notes, url, published, asset_url = "", UPDATE_PAGE, "", ""
    data = None
    try:
        data = _http_get_json(UPDATE_API, timeout=timeout)
    except Exception as e:
        if not latest:
            errors.append(_net_error_text(e))

    if data:
        api_tag = str(data.get("tag_name") or "").strip().lstrip("vV")
        # 两个来源取版本更高的那个：跳转结果可能被 CDN 缓存住（刚发完版时
        # 还指着上一版），API 拿到的是实时数据，谁新听谁的。
        if api_tag and (not latest
                        or _version_tuple(api_tag) > _version_tuple(latest)):
            latest = api_tag
        notes = (data.get("body") or "").strip()
        url = data.get("html_url") or UPDATE_PAGE
        published = (data.get("published_at") or "")[:10]
        for a in (data.get("assets") or []):     # 优先给便携版 zip 的直链
            name = str((a or {}).get("name") or "").lower()
            if name.endswith(".zip") and "portable" in name:
                asset_url = a.get("browser_download_url") or ""
                break
        if not asset_url and data.get("assets"):
            asset_url = (data["assets"][0] or {}).get("browser_download_url") or ""

    if not latest:
        return {"ok": False, "current": VERSION,
                "message": errors[-1] if errors else "没能从 GitHub 取到版本号"}

    has_update = _version_tuple(latest) > _version_tuple(VERSION)
    return {
        "ok": True,
        "current": VERSION,
        "latest": latest,
        "has_update": has_update,
        "url": url,
        "notes": notes,
        "published": published,
        "asset_url": asset_url,
        "message": ("发现新版本 v%s（当前 v%s）" % (latest, VERSION) if has_update
                    else "已是最新版本（v%s）" % VERSION),
    }
