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
import tkinter as tk
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP_TITLE = "米游社签到助手"
APP_SUB = "原神 · 崩坏：星穹铁道 · 米游社每日签到"
VERSION = "2.1.1"

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
    """单个游戏的签到状态卡片。"""

    def __init__(self, master, game_key, game_name, on_sign):
        super().__init__(master, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=px(1), bd=0)
        self.game_key = game_key

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

        tk.Frame(self, bg=BORDER, height=px(1)).pack(
            fill="x", padx=px(16), pady=(px(9), px(7)))

        self.v_today = self._row("今日签到")
        self.v_days = self._row("本月累计")

        foot = tk.Frame(self, bg=CARD)
        foot.pack(fill="x", padx=px(16), pady=(px(6), px(12)))
        self.time = tk.Label(foot, text="", bg=CARD, fg=SUBTEXT, font=F_SMALL)
        self.time.pack(side="left", pady=(px(4), 0))
        self.btn = Btn(foot, "单独签到", lambda: on_sign(game_key),
                       kind="secondary", padx=12, pady=4)
        self.btn.pack(side="right")

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
        r.rowconfigure(3, weight=1)

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
        cards = tk.Frame(r, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", padx=px(18), pady=(px(16), 0))
        for i in range(len(core.GAMES)):
            cards.columnconfigure(i, weight=1, uniform="game")
        for i, cfg in enumerate(core.GAMES):
            card = GameCard(cards, cfg["key"], cfg["name"], self.do_sign_one)
            card.grid(row=0, column=i, sticky="nsew",
                      padx=(0, px(8)) if i == 0 else (px(8), 0))
            self.cards[cfg["key"]] = card
            self.buttons.append(card.btn)

        # ---- 操作区
        ops = tk.Frame(r, bg=CARD, highlightbackground=BORDER,
                       highlightthickness=px(1))
        ops.grid(row=2, column=0, sticky="ew", padx=px(18), pady=(px(16), 0))
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
        row2.pack(fill="x", padx=px(18), pady=(px(10), px(16)))
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

        # ---- 日志区
        logf = tk.Frame(r, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=px(1))
        logf.grid(row=3, column=0, sticky="nsew", padx=px(18), pady=(px(16), 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(1, weight=1)

        lh = tk.Frame(logf, bg=CARD)
        lh.grid(row=0, column=0, sticky="ew", padx=px(18), pady=(px(11), px(6)))
        tk.Label(lh, text="运行日志", bg=CARD, fg=TEXT, font=F_SECTION,
                 anchor="w").pack(side="left")
        Btn(lh, "打开日志文件", self.open_log, kind="ghost").pack(side="right")
        Btn(lh, "清空", self.clear_log, kind="ghost").pack(side="right", padx=px(4))

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
        foot.grid(row=4, column=0, sticky="ew", padx=px(20), pady=(px(8), px(12)))
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
    def run_bg(self, fn, done_status=None):
        if self.busy:
            return
        self.set_busy(True, "执行中…")
        self._spawn(fn, done_status)

    def _spawn(self, fn, done_status):
        def worker():
            try:
                res = fn()
            except Exception as e:
                res = {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
            self.q.put(("done", res, done_status))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.append_log(item[1])
                elif item[0] == "done":
                    _, res, done_status = item
                    self.set_busy(False)
                    self._after_run(res, done_status)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _log_cb(self, msg):
        self.q.put(("log", "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)))

    # ------------------------------------------------------------ 状态刷新
    def _load_local(self):
        st = core.local_state()
        self.time_var.set(st.get("task_time") or "09:05")
        self._render(st)
        tail = core.read_log_tail(80)
        self.append_log(tail if tail else "（暂无日志）")

    def _render(self, st=None):
        st = st or core.local_state()
        for data in st.get("game_list") or []:
            card = self.cards.get(data["key"])
            if card:
                card.set(data)

        login_time = st.get("login_time")
        if st.get("cred_ok"):
            self.account.configure(
                text="已登录" + ("　上次登录 %s" % login_time if login_time else ""),
                fg=OK)
        else:
            self.account.configure(text="未登录", fg=BAD)

        td = core.task_detail()
        if td["exists"]:
            t = td.get("time") or st.get("task_time") or "09:05"
            if td.get("time"):
                self.time_var.set(td["time"])
            self.task_hint.configure(text="每日自动签到：已开启 · 每天 %s" % t, fg=OK)
        else:
            self.task_hint.configure(text="每日自动签到：未开启", fg=SUBTEXT)

    def refresh(self, silent=False):
        def job():
            res = core.sign_once(force_info=True, on_log=self._log_cb)
            # 首次运行顺手补一次角色信息，失败不影响主流程
            if not core.load_state().get("accounts"):
                core.fetch_accounts(self._log_cb)
            return res
        if silent:
            self._spawn(job, None)
        else:
            self.run_bg(job, "状态已刷新")

    def _after_run(self, res, done_status):
        self._render()
        res = res or {}
        games = res.get("games") or {}
        bad = any((g or {}).get("status") in ("invalid_cookie", "failed", "error",
                                              "not_logged_in")
                  for g in games.values())
        if res.get("ok") and not bad:
            self.set_status(done_status or res.get("message") or "完成", OK)
        elif res.get("ok"):
            # 部分成功（例如原神签上了、崩铁失败）：用黄字把两边的实际情况都摆出来
            self.set_status(core.summarize(res) or done_status or "部分完成", WARN)
        elif games:
            self.set_status(res.get("message") or core.summarize(res) or "操作失败", BAD)
        else:
            # 登录 / 计划任务这类没有 games 字段的操作：一律以 ok 判定，
            # 之前只认 games，导致登录失败也显示灰色，很容易被当成没事。
            self.set_status(res.get("message") or done_status or "操作失败", BAD)

    # ------------------------------------------------------------ 具体动作
    def do_sign_all(self):
        self.run_bg(lambda: core.sign_once(None, False, self._log_cb), "全部签到完成")

    def do_sign_one(self, key):
        name = core.game_name(key)
        self.run_bg(lambda: core.sign_once([key], False, self._log_cb),
                    "%s 签到完成" % name)

    def do_login(self):
        if core.local_state().get("cred_ok"):
            from tkinter import messagebox
            if not messagebox.askyesno(
                    "重新登录",
                    "已有保存的登录凭据，重新登录会覆盖它。\n\n继续吗？"):
                return
        self.run_bg(lambda: core.login(self._log_cb), "登录流程结束")

    def do_clear(self):
        from tkinter import messagebox
        if not messagebox.askyesno("清除凭据",
                                   "将删除本机保存的登录凭据，之后需要重新登录。\n\n继续吗？"):
            return
        core.clear_credentials()
        core.write_log("已清除本机登录凭据")
        self.append_log("[%s] 已清除本机登录凭据" % datetime.now().strftime("%H:%M:%S"))
        self._render()
        self.set_status("凭据已清除", WARN)

    def do_task_on(self):
        t = (self.time_var.get() or "").strip()
        self.run_bg(lambda: core.create_task(t, self._log_cb), "自动签到已开启")

    def do_task_off(self):
        self.run_bg(lambda: core.delete_task(self._log_cb), "自动签到已关闭")

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
CLI_ACTIONS = ("login", "sign", "status", "task", "untask", "doctor")


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
