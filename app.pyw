# -*- coding: utf-8 -*-
"""米游社签到助手 —— 图形界面

支持原神、崩坏：星穹铁道（国服 · 米游社账号）。
双击本文件或桌面快捷方式即可启动；若被系统 Python 直接打开，
会自动切换到项目自带的 .venv 环境重新运行。
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_TITLE = "米游社签到助手"
APP_SUB = "原神 · 崩坏：星穹铁道 · 绝区零 · 米游社每日签到"
VERSION = "3.1.3"

# 4K 屏上 Windows 缩放常为 150%~200%。进程若未声明 DPI 感知，系统会把整个
# 窗口当位图放大——字体边缘被插值糊掉，这是界面发虚的根因。
# 解决：① 声明 DPI 感知，让文字按真实 DPI 原生渲染；
#       ② 用 px() 把设计稿像素换算成实际像素，保持布局比例不变。
DPI = 96


def _enable_dpi_awareness():
    """必须在创建 Tk 窗口之前调用。"""
    global DPI
    if sys.platform != "win32":
        return
    u32 = ctypes.windll.user32

    already = False
    try:
        ctx = u32.GetThreadDpiAwarenessContext()
        already = u32.GetAwarenessFromDpiAwarenessContext(ctx) != 0
    except Exception:
        pass

    if not already:
        ok = False
        try:
            fn = u32.SetProcessDpiAwarenessContext
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = ctypes.c_bool
            ok = bool(fn(ctypes.c_void_p(-4)))       # PER_MONITOR_AWARE_V2
        except Exception:
            ok = False
        if not ok:
            try:
                ok = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
            except Exception:
                ok = False
        if not ok:
            try:
                ok = bool(u32.SetProcessDPIAware())
            except Exception:
                ok = False

    try:
        DPI = int(u32.GetDpiForSystem()) or 96
    except Exception:
        DPI = 96
    if DPI <= 0:
        DPI = 96


_enable_dpi_awareness()
SCALE = DPI / 96.0


def px(v):
    """设计稿像素 -> 当前 DPI 下的实际像素。"""
    return int(round(v * SCALE))


# ---------------------------------------------------------------- 环境自举
def _in_project_venv() -> bool:
    """当前解释器是否已经在项目的 .venv 里。

    不能比较可执行文件名：`.venv\\Scripts` 下 python.exe 和 pythonw.exe 并存，
    手动调试用 python.exe、计划任务用 pythonw.exe，一比文件名就永远判定
    "环境不对"，于是每次启动都白白多起一个进程在外面干等。

    打包成 exe 后解释器和依赖都已内置，没有再切换环境的余地。
    """
    if getattr(sys, "frozen", False):
        return True
    try:
        if os.path.normcase(str(Path(sys.prefix).resolve())) == \
           os.path.normcase(str((BASE / ".venv").resolve())):
            return True
    except Exception:
        pass
    try:
        return os.path.normcase(str(Path(sys.executable).resolve().parent)) == \
               os.path.normcase(str((BASE / ".venv" / "Scripts").resolve()))
    except Exception:
        return False


def _ensure_venv():
    """确保用项目自带的 venv 运行（用户直接双击 .pyw 也不会缺依赖）。"""
    if _in_project_venv():
        return
    exe = BASE / ".venv" / "Scripts" / "pythonw.exe"
    if not exe.exists():
        exe = BASE / ".venv" / "Scripts" / "python.exe"
    if not exe.exists():
        return
    try:
        subprocess.Popen([str(exe), str(Path(__file__).resolve())], cwd=str(BASE))
    except Exception:
        pass
    sys.exit(0)


_ensure_venv()
sys.path.insert(0, str(BASE))

import core  # noqa: E402

# ---------------------------------------------------------------- 配色 / 字体
BG = "#f3f4f7"
CARD = "#ffffff"
BORDER = "#e2e5ea"
TEXT = "#1f2329"
SUBTEXT = "#8b919b"
HINT = "#b6bac2"        # 比 SUBTEXT 更淡，用于"已取消勾选"这类弱化文字
ACCENT = "#6d5ae6"
ACCENT_D = "#5b48d9"
ACCENT_A = "#4b3ac8"
SOFT = "#eeecfb"
OK = "#12a150"
WARN = "#d97706"
BAD = "#dc2626"
LOG_BG = "#fbfbfd"

FONT = "Microsoft YaHei UI"
F_TITLE = (FONT, 17, "bold")
F_SUB = (FONT, 9)
F_GAME = (FONT, 11, "bold")
F_VAL = (FONT, 11, "bold")
F_BTN = (FONT, 10)
F_SMALL = (FONT, 9)
F_SECTION = (FONT, 10, "bold")
F_MONO = ("Consolas", 9)


def _pick_font(root):
    """微软雅黑缺失时退回系统其它字体。"""
    global FONT, F_TITLE, F_SUB, F_GAME, F_VAL, F_BTN, F_SMALL, F_SECTION
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families(root))
        if FONT not in families:
            for alt in ("Microsoft YaHei", "SimHei", "Segoe UI", "Arial"):
                if alt in families:
                    FONT = alt
                    break
            F_TITLE = (FONT, 17, "bold")
            F_SUB = (FONT, 9)
            F_GAME = (FONT, 11, "bold")
            F_VAL = (FONT, 11, "bold")
            F_BTN = (FONT, 10)
            F_SMALL = (FONT, 9)
            F_SECTION = (FONT, 10, "bold")
    except Exception:
        pass


# ---------------------------------------------------------------- 控件
class Btn(tk.Button):
    """扁平风格按钮，带 hover 效果。"""

    PRESETS = {
        "primary":   dict(bg=ACCENT,     fg="#ffffff", hover=ACCENT_D,  active=ACCENT_A),
        "secondary": dict(bg="#ffffff",  fg=TEXT,     hover=SOFT,      active="#e6e3fa"),
        "ghost":     dict(bg="#ffffff",  fg=SUBTEXT,  hover="#f0f1f3", active="#e8e9ec"),
        "danger":    dict(bg="#ffffff",  fg=BAD,      hover="#fdeaea", active="#fadada"),
    }

    def __init__(self, master, text, command=None, kind="secondary", **kw):
        p = self.PRESETS.get(kind, self.PRESETS["secondary"])
        self._p = p
        self._disabled_bg = "#c7bfef" if kind == "primary" else "#f0f1f3"
        padx = kw.pop("padx", 16)
        pady = kw.pop("pady", 7)
        super().__init__(
            master, text=text, command=command, bd=0, relief="flat",
            font=F_BTN, cursor="hand2", bg=p["bg"], fg=p["fg"],
            activebackground=p["active"], activeforeground=p["fg"],
            disabledforeground="#a8adb5", padx=px(padx), pady=px(pady),
            highlightthickness=px(1) if kind in ("secondary", "danger") else 0,
            highlightbackground=BORDER, highlightcolor=BORDER, **kw)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)

    def _enter(self, _=None):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._p["hover"])

    def _leave(self, _=None):
        if str(self["state"]) != "disabled":
            self.configure(bg=self._p["bg"])

    def set_enabled(self, flag: bool):
        if flag:
            self.configure(state="normal", bg=self._p["bg"], cursor="hand2")
        else:
            self.configure(state="disabled", bg=self._disabled_bg, cursor="")


class GameCard(tk.Frame):
    """单个游戏的签到状态卡片（带"是否参与签到"勾选框）。"""

    def __init__(self, master, game_key, game_name, on_sign, on_toggle):
        super().__init__(master, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=px(1), bd=0)
        self.game_key = game_key
        self.on_toggle = on_toggle

        head = tk.Frame(self, bg=CARD)
        head.pack(fill="x", padx=px(16), pady=(px(12), 0))
        self.dot = tk.Canvas(head, width=px(8), height=px(8), bg=CARD,
                             highlightthickness=0, bd=0)
        self.dot.pack(side="left", pady=(px(4), 0))
        d = px(8)
        self._dot_id = self.dot.create_oval(px(1), px(1), d - px(1), d - px(1),
                                            fill="#cfd3da", outline="")
        tk.Label(head, text=game_name, bg=CARD, fg=TEXT, font=F_GAME).pack(
            side="left", padx=(px(7), 0))
        self.uid = tk.Label(head, text="", bg=CARD, fg=SUBTEXT, font=F_SMALL)
        self.uid.pack(side="right")

        self.picked = tk.BooleanVar(value=True)
        self.chk = tk.Checkbutton(
            self, text="参与签到", variable=self.picked, command=self._toggled,
            bg=CARD, fg=SUBTEXT, font=F_SMALL, activebackground=CARD,
            activeforeground=TEXT, selectcolor=CARD, bd=0,
            highlightthickness=0, cursor="hand2", anchor="w")
        self.chk.pack(fill="x", padx=px(12), pady=(px(6), 0))

        tk.Frame(self, bg=BORDER, height=px(1)).pack(
            fill="x", padx=px(16), pady=(px(7), px(7)))

        self.v_today = self._row("今日签到")
        self.v_days = self._row("本月累计")

        foot = tk.Frame(self, bg=CARD)
        foot.pack(fill="x", padx=px(16), pady=(px(6), px(12)))
        self.time = tk.Label(foot, text="", bg=CARD, fg=SUBTEXT, font=F_SMALL)
        self.time.pack(side="left", pady=(px(4), 0))
        self.btn = Btn(foot, "单独签到", lambda: on_sign(game_key),
                       kind="secondary", padx=12, pady=4)
        self.btn.pack(side="right")

    def _toggled(self):
        if self.on_toggle:
            self.on_toggle(self.game_key, self.picked.get())

    def set_selected(self, flag):
        """由外部同步勾选状态，且不触发回调（避免回写时再存一次）。"""
        self.picked.set(bool(flag))
        self._apply_picked()

    def _apply_picked(self):
        """未参与签到的卡片整体压暗，一眼能看出哪些游戏不会被签。"""
        on = self.picked.get()
        self.chk.configure(fg=SUBTEXT if on else HINT)
        self.btn.configure(state="normal" if on else "disabled")

    def _row(self, label):
        r = tk.Frame(self, bg=CARD)
        r.pack(fill="x", padx=px(16), pady=(0, px(4)))
        tk.Label(r, text=label, bg=CARD, fg=SUBTEXT, font=F_SMALL).pack(side="left")
        v = tk.Label(r, text="—", bg=CARD, fg=TEXT, font=F_VAL)
        v.pack(side="right")
        return v

    def set(self, data):
        acc = data.get("account") or {}
        uid = acc.get("uid") or ""
        nick = acc.get("nickname") or ""
        self.uid.configure(text=("UID %s%s" % (uid, " · " + nick if nick else ""))
                           if uid else "")

        signed = data.get("signed_today")
        if signed is True:
            self.v_today.configure(text="已签到", fg=OK)
            dot = OK
        elif signed is False:
            self.v_today.configure(text="未签到", fg=WARN)
            dot = WARN
        else:
            self.v_today.configure(text="—", fg=SUBTEXT)
            dot = None

        st = data.get("status")
        if st in ("invalid_cookie", "not_logged_in"):
            self.v_today.configure(text="需重新登录", fg=BAD)
            dot = BAD
        elif st in ("failed", "error"):
            self.v_today.configure(text="签到失败", fg=BAD)
            dot = BAD

        days = data.get("days")
        self.v_days.configure(text=("%s 天" % days) if days is not None else "—",
                              fg=TEXT if days is not None else SUBTEXT)
        last = data.get("last_run")
        self.time.configure(text=("上次查询 %s" % last[11:16]) if last else "尚未查询")
        if dot:
            self.dot.itemconfigure(self._dot_id, fill=dot)


# ---------------------------------------------------------------- 主窗口
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.buttons = []
        self.cards = {}
        # 上次弹"签到失败"系统通知的时间（节流用，不重复打扰）
        self._notified_fail_at = 0.0
        # 用户接受"重新登录"后，登录成功就自动补签一次
        self._sign_after_login = False
        # 当前后台操作的超时时刻（看门狗用，防止界面卡在"执行中…"）
        self._op_deadline = None

        self._build()
        self._load_local()
        self._pump()
        self.root.after(220, lambda: self.refresh(silent=True))

    # ------------------------------------------------------------ 界面搭建
    def _build(self):
        _pick_font(self.root)
        r = self.root
        r.title(APP_TITLE)
        r.configure(bg=BG)
        r.geometry("%dx%d" % (px(900), px(760)))
        r.minsize(px(840), px(680))
        self._center(900, 760)
        self._set_icon()

        r.columnconfigure(0, weight=1)
        r.rowconfigure(4, weight=1)

        # ---- 顶部标题栏
        head = tk.Frame(r, bg=CARD)
        head.grid(row=0, column=0, sticky="ew")
        inner = tk.Frame(head, bg=CARD)
        inner.pack(fill="x", padx=px(22), pady=px(14))
        bar = tk.Frame(inner, bg=ACCENT, width=px(4), height=px(40))
        bar.pack(side="left", padx=(0, px(12)))
        bar.pack_propagate(False)
        txt = tk.Frame(inner, bg=CARD)
        txt.pack(side="left")
        tk.Label(txt, text=APP_TITLE, bg=CARD, fg=TEXT, font=F_TITLE,
                 anchor="w").pack(anchor="w")
        tk.Label(txt, text=APP_SUB, bg=CARD, fg=SUBTEXT, font=F_SUB,
                 anchor="w").pack(anchor="w")
        self.account = tk.Label(inner, text="", bg=CARD, fg=SUBTEXT,
                                font=F_SMALL, anchor="e", justify="right")
        self.account.pack(side="right")
        tk.Frame(r, bg=BORDER, height=px(1)).grid(row=0, column=0, sticky="sew")

        # ---- 各游戏状态卡片
        sel_bar = tk.Frame(r, bg=BG)
        sel_bar.grid(row=1, column=0, sticky="ew", padx=px(18), pady=(px(14), 0))
        tk.Label(sel_bar, text="勾选要签到的游戏", bg=BG, fg=SUBTEXT,
                 font=F_SMALL).pack(side="left")
        Btn(sel_bar, "全选", lambda: self.set_all_games(True),
            kind="secondary", padx=10, pady=2).pack(side="right")
        Btn(sel_bar, "全不选", lambda: self.set_all_games(False),
            kind="secondary", padx=10, pady=2).pack(side="right", padx=(0, px(6)))

        cards = tk.Frame(r, bg=BG)
        cards.grid(row=2, column=0, sticky="ew", padx=px(18), pady=(px(8), 0))
        for i in range(len(core.GAMES)):
            cards.columnconfigure(i, weight=1, uniform="game")
        for i, cfg in enumerate(core.GAMES):
            card = GameCard(cards, cfg["key"], cfg["name"], self.do_sign_one,
                            self.on_game_toggled)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0, px(8)) if i == 0 else (px(8), 0))
            self.cards[cfg["key"]] = card
            self.buttons.append(card.btn)

        # ---- 操作区
        ops = tk.Frame(r, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=px(1))
        ops.grid(row=3, column=0, sticky="ew", padx=px(18), pady=(px(16), 0))
        tk.Label(ops, text="操作", bg=CARD, fg=TEXT, font=F_SECTION,
                 anchor="w").pack(anchor="w", padx=px(18), pady=(px(12), px(8)))

        row1 = tk.Frame(ops, bg=CARD)
        row1.pack(fill="x", padx=px(18))
        for text, cmd, kind in (
                ("一键全部签到", self.do_sign_all, "primary"),
                ("刷新状态", self.refresh, "secondary"),
                ("登录米游社", self.do_login, "secondary"),
                ("清除登录凭据", self.do_clear, "danger")):
            b = Btn(row1, text, cmd, kind=kind)
            b.pack(side="left", padx=(0, px(8)))
            self.buttons.append(b)

        row2 = tk.Frame(ops, bg=CARD)
        row2.pack(fill="x", padx=px(18), pady=(px(10), 0))
        tk.Label(row2, text="每天自动签到时间", bg=CARD, fg=SUBTEXT,
                 font=F_SMALL).pack(side="left", padx=(0, px(8)))
        self.time_var = tk.StringVar(value="09:05")
        ent = tk.Entry(row2, textvariable=self.time_var, width=7, font=(FONT, 10),
                       bd=1, relief="solid", justify="center",
                       highlightthickness=0, bg="#ffffff", fg=TEXT)
        ent.configure(highlightbackground=BORDER)
        ent.pack(side="left", ipady=px(4))
        for text, cmd, kind, pad in (("开启 / 更新", self.do_task_on, "secondary", 10),
                                     ("关闭", self.do_task_off, "ghost", 6)):
            b = Btn(row2, text, cmd, kind=kind)
            b.pack(side="left", padx=(px(pad), 0))
            self.buttons.append(b)
        self.task_hint = tk.Label(row2, text="", bg=CARD, fg=SUBTEXT, font=F_SMALL)
        self.task_hint.pack(side="right")

        # ---- 签到成功后的通知开关（有人只想在失败时被提醒）
        row3 = tk.Frame(ops, bg=CARD)
        row3.pack(fill="x", padx=px(18), pady=(px(10), px(16)))
        self.notify_ok = tk.BooleanVar(value=True)
        tk.Checkbutton(
            row3, text="签到成功后也弹通知", variable=self.notify_ok,
            command=self.on_notify_toggle, bg=CARD, fg=SUBTEXT, font=F_SMALL,
            activebackground=CARD, activeforeground=TEXT, selectcolor=CARD,
            bd=0, highlightthickness=0, cursor="hand2", anchor="w").pack(side="left")
        tk.Label(row3, text="取消勾选后，只在签到失败 / 需要重新登录时提醒",
                 bg=CARD, fg=HINT, font=F_SMALL).pack(side="left",
                                                      padx=(px(10), 0))

        # ---- 日志区
        logf = tk.Frame(r, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=px(1))
        logf.grid(row=4, column=0, sticky="nsew", padx=px(18), pady=(px(16), 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(1, weight=1)

        lh = tk.Frame(logf, bg=CARD)
        lh.grid(row=0, column=0, sticky="ew", padx=px(18), pady=(px(11), px(6)))
        tk.Label(lh, text="运行日志", bg=CARD, fg=TEXT, font=F_SECTION,
                 anchor="w").pack(side="left")
        Btn(lh, "打开日志文件", self.open_log, kind="ghost").pack(side="right")
        Btn(lh, "清空", self.clear_log, kind="ghost").pack(side="right", padx=px(4))
        Btn(lh, "测试通知", self.do_notify_test, kind="ghost").pack(side="right", padx=px(4))
        Btn(lh, "失败通知样例", self.do_notify_sample_fail,
            kind="ghost").pack(side="right", padx=px(4))
        Btn(lh, "成功通知样例", self.do_notify_sample_ok,
            kind="ghost").pack(side="right", padx=px(4))

        wrap = tk.Frame(logf, bg=LOG_BG, highlightbackground=BORDER,
                        highlightthickness=px(1))
        wrap.grid(row=1, column=0, sticky="nsew", padx=px(14), pady=(0, px(14)))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log = tk.Text(wrap, bg=LOG_BG, fg="#3d4450", font=F_MONO, bd=0,
                           relief="flat", wrap="word", height=6,
                           padx=px(10), pady=px(8),
                           highlightthickness=0, insertbackground=LOG_BG,
                           state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = tk.Scrollbar(wrap, command=self.log.yview, width=px(11))
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

        # ---- 状态栏
        foot = tk.Frame(r, bg=BG)
        foot.grid(row=5, column=0, sticky="ew", padx=px(20), pady=(px(8), px(12)))
        self.status = tk.Label(foot, text="就绪", bg=BG, fg=SUBTEXT,
                               font=F_SMALL, anchor="w")
        self.status.pack(side="left")
        tk.Label(foot, text="%s v%s · 凭据经 Windows DPAPI 加密" % (APP_TITLE, VERSION),
                 bg=BG, fg="#b3b8c0", font=F_SMALL).pack(side="right")

    def _center(self, w, h):
        w, h = px(w), px(h)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, int((sh - h) * 0.38))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _set_icon(self):
        ico = BASE / "icon.png"
        if ico.exists():
            try:
                self._icon = tk.PhotoImage(file=str(ico))
                self.root.iconphoto(True, self._icon)
                return
            except Exception:
                pass

    # ------------------------------------------------------------ 日志与状态
    def append_log(self, line: str):
        line = (line or "").strip("\n")
        if not line:
            return
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        try:
            total = int(self.log.index("end-1c").split(".")[0])
            if total > core.MAX_LOG_LINES * 2:
                self.log.delete("1.0", "%d.0" % (total - core.MAX_LOG_LINES))
        except Exception:
            pass
        self.log.see("end-1c")
        self.log.configure(state="disabled")

    def set_status(self, text, color=SUBTEXT):
        self.status.configure(text=text, fg=color)

    def set_busy(self, flag: bool, text=""):
        self.busy = flag
        for b in self.buttons:
            b.set_enabled(not flag)
        if flag and text:
            self.set_status(text, ACCENT)

    # ------------------------------------------------------------ 线程与消息泵
    def run_bg(self, fn, done_status=None, is_login=False, notify_source=""):
        if self.busy:
            return
        # 看门狗：万一线程意外没了（没回结果），也不能让界面永久卡在"执行中…"
        timeout = 720 if is_login else 300
        self._op_deadline = time.monotonic() + timeout
        self.set_busy(True, "执行中…")
        self._spawn(fn, done_status, is_login=is_login,
                    notify_source=notify_source)

    def _spawn(self, fn, done_status, silent=False, is_login=False,
               notify_source=""):
        def worker():
            try:
                res = fn()
            except BaseException as e:  # noqa: BLE001 - 任何异常都必须变回结果，否则界面卡死
                res = {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
            try:
                self.q.put(("done", res, done_status, silent, is_login,
                            notify_source))
            except BaseException:  # noqa: BLE001
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _handle_item(self, item):
        if item[0] == "log":
            self.append_log(item[1])
            return
        if item[0] != "done":
            return
        res = item[1] if len(item) > 1 else None
        done_status = item[2] if len(item) > 2 else None
        silent = item[3] if len(item) > 3 else False
        is_login = item[4] if len(item) > 4 else False
        notify_source = item[5] if len(item) > 5 else ""
        self._op_deadline = None
        self.set_busy(False)
        self._after_run(res, done_status, silent=silent, is_login=is_login,
                        notify_source=notify_source)

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                try:
                    self._handle_item(item)
                except Exception as e:
                    # 处理单条消息出错绝不能把消息泵弄死：
                    # 泵一死，按钮就永远停用、状态栏永远"执行中…"，看起来就是卡死。
                    self._op_deadline = None
                    self.set_busy(False)
                    note = "界面处理结果时出错：%s: %s" % (type(e).__name__, e)
                    self.append_log("[界面] " + note)
                    core.write_log("[界面] " + note)
        except queue.Empty:
            pass
        except Exception as e:
            core.write_log("[界面] 消息循环异常：%s: %s" % (type(e).__name__, e))
        # 看门狗：操作超时（线程没回结果）就恢复界面，别一直"执行中…"
        try:
            if self.busy and self._op_deadline and time.monotonic() > self._op_deadline:
                self._op_deadline = None
                self.set_busy(False)
                msg = "操作超时（后台线程没有返回结果），已恢复按钮，可重试"
                self.append_log("[界面] " + msg)
                core.write_log("[界面] " + msg)
                self.set_status(msg, BAD)
        except Exception:
            pass
        try:
            self.root.after(120, self._pump)
        except Exception:
            pass

    def _log_cb(self, msg):
        self.q.put(("log", "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)))

    # ------------------------------------------------------------ 状态刷新
    def _load_local(self):
        st = core.local_state()
        self.time_var.set(st.get("task_time") or "09:05")
        self.notify_ok.set(bool(st.get("notify_on_success", True)))
        self._render(st)
        tail = core.read_log_tail(80)
        self.append_log(tail if tail else "（暂无日志）")

    def _render(self, st=None):
        st = st or core.local_state()
        for data in st.get("game_list") or []:
            card = self.cards.get(data["key"])
            if card:
                card.set(data)
                card.set_selected(data.get("selected", True))

        login_time = st.get("login_time")
        if st.get("cred_ok"):
            if st.get("sign_ready") is False:
                # 登录态在、但缺 cookie_token：签到会一直 -100，必须明说
                self.account.configure(
                    text="已登录 · 签到凭据不完整，请点「登录米游社」", fg=WARN)
            else:
                self.account.configure(
                    text="已登录" + ("　上次登录 %s" % login_time if login_time else ""),
                    fg=OK)
        else:
            self.account.configure(text="未登录", fg=BAD)

        td = core.task_detail()
        if td["exists"]:
            t = td.get("time") or st.get("task_time") or "09:05"
            if td.get("time") and td.get("mode") == "task":
                self.time_var.set(td["time"])
            if td.get("mode") == "startup":
                # 计划任务被安全软件拦了，走的是启动文件夹方案
                self.task_hint.configure(
                    text="每日自动签到：已开启（开机启动 · 约 %s）" % t, fg=OK)
            else:
                self.task_hint.configure(text="每日自动签到：已开启 · 每天 %s" % t, fg=OK)
        else:
            self.task_hint.configure(text="每日自动签到：未开启", fg=SUBTEXT)

    def refresh(self, silent=False):
        def job():
            # 开机/打开程序时，只要当天还有游戏没签就直接签掉；
            # 都签过了就只查状态，避免多余的接口调用。
            st = core.load_state()
            games = st.get("games") or {}
            pending = [k for k in core.selected_games()
                       if (games.get(k) or {}).get("signed_today") is not True]
            res = core.sign_once(force_info=not pending, on_log=self._log_cb)
            # 首次运行顺手补一次角色信息，失败不影响主流程
            if not core.load_state().get("accounts"):
                core.fetch_accounts(self._log_cb)
            return res
        if silent:
            # 开机时的静默刷新：当天没签的游戏会直接签掉，也算"自动签到"
            self._spawn(job, None, silent=True, notify_source="开机自动签到")
        else:
            self.run_bg(job, "状态已刷新", notify_source="手动刷新")

    def _notify_result(self, res, source=""):
        """签到结束后在右下角提醒（成功也提醒；先试系统通知，被拦就自绘浮层）。

        注意：提醒要走后台线程。右下角浮层会自己跑 20 秒消息循环，
        如果放在界面主线程里调用，整个窗口会卡住 20 秒。
        失败提醒 10 分钟内节流（静默刷新可能连续触发）；成功提醒不节流——
        每次手动 / 自动签到都应该有一条反馈。
        纯状态查询（没有真的签到）由 core.notify_sign_result 过滤掉。
        """
        games = (res or {}).get("games") or {}
        if not games:
            return
        bad = any((g or {}).get("status") in core.BAD_SIGN_STATUS
                  for g in games.values())
        if bad:
            now = time.monotonic()
            if now - self._notified_fail_at < 600:      # 10 分钟内不重复提醒
                return
            self._notified_fail_at = now
            # 用户看到提醒后自然会点【登录米游社】，登录成功就自动补签一次
            self._sign_after_login = True
        threading.Thread(
            target=lambda: core.notify_sign_result(res, source=source),
            daemon=True).start()

    def _after_run(self, res, done_status, silent=False, is_login=False,
                   notify_source=""):
        self._render()
        res = res or {}

        # 用户是为了修签到才重新登录的：登录成功就顺手补签一次，闭环
        if is_login:
            if res.get("ok") and self._sign_after_login:
                self._sign_after_login = False
                self.set_status("登录成功，正在自动补签一次…", ACCENT)
                self.do_sign_all("登录后自动补签")
                return
            self.set_status(self._brief(res, "登录流程结束"),
                            OK if res.get("ok") else BAD)
            return

        games = res.get("games") or {}
        bad = any((g or {}).get("status") in ("invalid_cookie", "failed", "error",
                                              "not_logged_in")
                  for g in games.values())
        if res.get("ok") and not bad:
            self.set_status(self._brief(res, done_status or "完成"), OK)
        elif res.get("ok"):
            # 部分成功（例如原神签上了、崩铁失败）：用黄字把两边的实际情况都摆出来
            self.set_status(core.summarize(res) or done_status or "部分完成", WARN)
        elif games:
            self.set_status(core.summarize(res) or self._brief(res, "操作失败"), BAD)
        else:
            # 登录 / 计划任务这类没有 games 字段的操作：一律以 ok 判定，
            # 之前只认 games，导致登录失败也显示灰色，很容易被当成没事。
            self.set_status(self._brief(res, done_status or "操作失败"), BAD)

        # 每次签到结束后都弹一条系统通知（成功也让用户知道签上了）。
        # 静默刷新（开机自动签到）也弹；纯状态查询由 core 里过滤，不会打扰。
        self._notify_result(res, notify_source)

    @staticmethod
    def _brief(res, fallback=""):
        """状态栏只放一句话。

        像"开启自动签到失败"这种提示是多行的、几百字，直接塞进状态栏会糊在
        窗口底部（截图里就是这样）。完整的说明已经写进运行日志了，状态栏
        只留第一行并截断即可。
        """
        text = (res or {}).get("status_text") or (res or {}).get("message") or ""
        # 注意：空串的 splitlines() 是空列表，直接取 [0] 会 IndexError
        # （"开启自动签到失败"这类多行提示一旦没有 message 就会踩到）
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        text = lines[0] if lines else ""
        if len(text) > 44:
            text = text[:43] + "…"
        return text or fallback

    # ------------------------------------------------------------ 具体动作
    def do_sign_all(self, source="手动签到"):
        picked = core.selected_games()
        if len(picked) == 1:
            label = "%s 签到完成" % core.game_name(picked[0])
        else:
            label = "%d 个游戏签到完成" % len(picked)
        self.run_bg(lambda: core.sign_once(None, False, self._log_cb), label,
                    notify_source=source)

    def on_notify_toggle(self):
        """签到成功后的通知开关：立刻落盘，下次签到（含计划任务）即生效。"""
        on = bool(self.notify_ok.get())
        core.save_settings(notify_on_success=on)
        self.set_status("签到成功后%s弹通知" % ("会" if on else "不再"), SUBTEXT)

    def on_game_toggled(self, key, flag):
        """勾选框变动：立刻落盘 + 刷新卡片样式（下次签到/定时任务都用这个选择）。"""
        card = self.cards.get(key)
        if not any(c.picked.get() for c in self.cards.values()):
            # 不允许一个都不选：把刚取消的这个勾回去，并提示
            if card:
                card.set_selected(True)
            self.set_status("至少要保留一个游戏，已还原", WARN)
            return
        if card:
            card._apply_picked()
        picked = [k for k in core.GAME_KEYS if self.cards[k].picked.get()]
        core.save_selected_games(picked)
        self.set_status("将签到：%s"
                        % "、".join(core.game_name(k) for k in core.selected_games()),
                        SUBTEXT)

    def set_all_games(self, flag):
        """全选 / 全不选。全不选没有意义，直接提示并忽略。"""
        if not flag:
            self.set_status("至少要保留一个游戏", WARN)
            return
        for card in self.cards.values():
            card.set_selected(True)
        core.save_selected_games(list(core.GAME_KEYS))
        self.set_status("将签到：%s"
                        % "、".join(core.game_name(k) for k in core.GAME_KEYS),
                        SUBTEXT)

    def do_sign_one(self, key):
        name = core.game_name(key)
        self.run_bg(lambda: core.sign_once([key], False, self._log_cb),
                    "%s 签到完成" % name, notify_source="手动签到")

    def do_login(self, skip_confirm=False):
        if core.local_state().get("cred_ok") and not skip_confirm:
            from tkinter import messagebox
            if not messagebox.askyesno(
                    "重新登录",
                    "已有保存的登录凭据。\n\n"
                    "登录时程序会先退出浏览器里旧的米游社登录状态"
                    "（保留设备信息），再请你重新登录。\n"
                    "这样才能换账号，也才能拿到可用于签到的新凭据。\n\n继续吗？"):
                return
        self.run_bg(lambda: core.login(self._log_cb), "登录流程结束", is_login=True)

    def do_clear(self):
        from tkinter import messagebox
        if not messagebox.askyesno(
                "清除凭据 / 退出登录",
                "将删除本机保存的登录凭据，并退出程序中浏览器里的米游社"
                "登录状态（之后需要重新登录，可换成别的账号）。\n\n继续吗？"):
            return
        self.run_bg(lambda: core.clear_credentials(self._log_cb), "已退出登录")

    def do_task_on(self):
        """开启 / 更新每日自动签到。

        先按普通权限试一次；被系统（安全软件按"未知程序"下发的策略）拒绝时，
        自动请求一次管理员权限再注册——实测提权后计划任务、注册表启动项、
        启动文件夹三条路都能成功，所以用户只需在 UAC 窗口点一次「是」。
        """
        t = (self.time_var.get() or "").strip()

        def job():
            res = core.create_task(t, self._log_cb)
            if res.get("ok"):
                return res
            return core.create_task_elevated(t, self._log_cb)

        self.run_bg(job, "自动签到已开启")

    def do_task_off(self):
        def job():
            res = core.delete_task(self._log_cb)
            if res.get("ok"):
                return res
            return core.delete_task_elevated(self._log_cb)

        self.run_bg(job, "自动签到已关闭")

    def _ensure_toast(self) -> bool:
        """确保系统通知可用；不可用时顺手启用一次（便携版常见：没跑过注册）。

        会弹一个 UAC，日志里会写清楚要做什么，用户点【是】即可。
        返回 False 表示这次只能靠托盘气泡 / 自绘卡片兜底。
        """
        if core.toast_registered():
            return True
        res = core.setup_toast(self._log_cb)
        if not res.get("ok"):
            self._log_cb("系统通知没启用成功，本次改用右下角提醒兜底")
            return False
        return True

    def do_notify_test(self):
        """发一条测试提醒，确认"签到提醒"能收到。"""
        def job():
            self._ensure_toast()
            ok = core.notify("米游社签到助手 · 通知测试",
                             "能在右下角看到这条，就说明签到提醒可用。")
            return {"ok": bool(ok),
                    "message": ("测试提醒已发送，请看屏幕右下角"
                                if ok else "提醒没能弹出，详见运行日志")}
        self.run_bg(job, "通知测试完成")

    @staticmethod
    def _sample_result(status: str) -> dict:
        """构造一份和各游戏真实签到结果同构的样例数据（只用于通知预览）。"""
        games = {}
        for k in (core.selected_games() or list(core.GAME_KEYS)):
            name = core.game_name(k)
            if status == "ok":
                games[k] = {"name": name, "status": "claimed",
                            "message": "签到成功，获得 签到奖励 ×1", "days": 7}
            else:
                games[k] = {"name": name, "status": "invalid_cookie",
                            "message": ("登录凭据已失效或不完整（游戏签到需要有效的 "
                                        "cookie_token 会话），请点「登录米游社」重新登录"),
                            "days": None}
        return {"ok": status == "ok", "games": games}

    def do_notify_sample(self, status: str):
        """弹一条签到结果的通知样例（成功 / 失败），看看签完提醒长什么样。

        用的是假数据，不会真的去签到；force=True 让预览不受
        「签到成功后也弹通知」开关的影响——关掉成功通知的用户
        仍然可以主动看看它长什么样。
        """
        def job():
            self._ensure_toast()
            ok = core.notify_sign_result(self._sample_result(status),
                                         source="通知样例", force=True)
            what = "签到成功" if status == "ok" else "签到失败"
            return {"ok": bool(ok),
                    "message": ("%s样例已发送，请看屏幕右下角" % what
                                if ok else "样例没能弹出，详见运行日志")}
        self.run_bg(job, "通知样例已发送")

    def do_notify_sample_ok(self):
        self.do_notify_sample("ok")

    def do_notify_sample_fail(self):
        self.do_notify_sample("fail")

    def open_log(self):
        try:
            os.startfile(str(core.LOG_FILE))  # noqa: S606
        except Exception:
            try:
                subprocess.Popen(["explorer", str(core.BASE)])
            except Exception:
                pass

    def clear_log(self):
        try:
            core.LOG_FILE.write_text("", encoding="utf-8")
        except Exception:
            pass
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.set_status("日志已清空", SUBTEXT)


# 便携版把图形界面与命令行合在同一个 exe 里：带这些参数时不建窗口，
# 直接走命令行逻辑。Windows 计划任务就是用 `exe sign` 把签到调起来的。
CLI_ACTIONS = ("login", "sign", "status", "task", "untask", "doctor", "notify-setup")


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in CLI_ACTIONS:
        import cli
        cli.main()
        return
    root = tk.Tk()
    # 让 Tk 按真实 DPI 换算字号（Tk 的 scaling 单位是"每点像素数"，即 DPI/72）。
    # 不设的话 Tk 会以为自己是 96 DPI，字全部偏小。
    try:
        root.tk.call("tk", "scaling", DPI / 72.0)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
