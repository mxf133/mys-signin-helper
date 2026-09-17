# -*- coding: utf-8 -*-
"""米游社签到助手 —— 命令行入口

用法:
    python cli.py login              打开 Edge 登录米游社，凭据加密保存（只需一次）
    python cli.py sign               签到（按界面里勾选的游戏；默认全部）
    python cli.py sign --game zzz     只签某一个游戏
    python cli.py sign --game genshin --game zzz   同时签指定的几个游戏
    python cli.py status             查询签到状态，不重复签到
    python cli.py task               注册计划任务（按 settings.json 里的时间自动签到）
    python cli.py untask             删除计划任务
    python cli.py doctor             环境自检（排查问题时跑这个）

--game 可用值：genshin（原神）、starrail（崩坏：星穹铁道）、zzz（绝区零）、
all（默认，表示按界面上勾选的游戏）。可重复传入以指定多个。
图形界面请直接运行 app.pyw。

便携版（打包成 exe）用法相同，把 `python cli.py` 换成 exe 本身即可，
例如 `米游社签到助手.exe sign`。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
FROZEN = bool(getattr(sys, "frozen", False))


def _in_project_venv() -> bool:
    """当前解释器是否已经在项目的 .venv 里。

    不能比较可执行文件名：`.venv\\Scripts` 下 python.exe 与 pythonw.exe 并存，
    计划任务用 pythonw.exe 调本脚本（core.task_command），手动调试用
    python.exe，一比文件名就会永远判定"环境不对"，于是每次定时签到都多起
    一个进程在外面干等，任务也永远不会真正结束。

    打包成 exe 后解释器和依赖都已内置，没有再切换环境的余地。
    """
    if FROZEN:
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
    if _in_project_venv():
        return
    exe = BASE / ".venv" / "Scripts" / "python.exe"
    if not exe.exists():
        return
    try:
        sys.exit(subprocess.call([str(exe), str(Path(__file__).resolve())] + sys.argv[1:]))
    except Exception:
        pass


_ensure_venv()
sys.path.insert(0, str(BASE))

import core  # noqa: E402


def _find_edge():
    """返回 msedge.exe 路径，找不到返回 None。"""
    try:
        import winreg
        for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
        ):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    p = winreg.QueryValueEx(k, None)[0]
                    if p and Path(p).exists():
                        return p
            except Exception:
                continue
    except Exception:
        pass
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).exists():
            return p
    return None


def _check_playwright():
    """返回 (是否可用, 说明)。会真的用无头 Edge 起一次，验证驱动能跑。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return False, "导入失败：%r" % (e,)

    try:
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        if not isinstance(drv, (list, tuple)):
            drv = (drv,)
        missing = [str(p) for p in drv if not Path(str(p)).exists()]
        if missing:
            return False, "驱动文件缺失：%s" % missing
    except Exception as e:
        return False, "驱动定位失败：%r" % (e,)

    import tempfile
    import shutil
    tmp = tempfile.mkdtemp(prefix="mys_pw_")
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=tmp, channel="msedge", headless=True,
                args=["--no-first-run", "--no-default-browser-check"])
            ctx.close()
        return True, "无头 Edge 启动正常"
    except Exception as e:
        return False, "启动无头 Edge 失败：%r" % (e,)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def doctor():
    """环境自检：出问题时最需要看的东西一次打全，并存成「自检报告.txt」。"""
    L = []
    L.append("米游社签到助手 · 环境自检")
    L.append("=" * 52)
    L.append("运行模式      : %s" % ("便携版 exe" if core.is_frozen() else "源码 / Python"))
    L.append("Python 版本   : %s" % sys.version.split()[0])
    L.append("程序目录      : %s" % core.BASE)

    try:
        probe = core.BASE / ".write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        L.append("目录可写      : OK")
    except Exception as e:
        L.append("目录可写      : 失败 %r" % (e,))
        L.append("                ↑ 凭据和日志会存不下来，别把程序放在")
        L.append("                  C:\\Program Files 这类受保护目录里")

    edge = _find_edge()
    L.append("Edge 浏览器   : %s" % (edge if edge else "未找到（登录功能不可用）"))

    try:
        import genshin
        L.append("genshin.py    : OK")
    except Exception as e:
        L.append("genshin.py    : 导入失败 %r" % (e,))

    ok, msg = _check_playwright()
    L.append("playwright    : %s" % msg)

    st = core.load_state()
    L.append("凭据 cookie.enc: %s" % ("已保存" if core.CRED_FILE.exists() else "不存在"))
    L.append("凭据有效标记  : %s" % st.get("cred_ok"))
    L.append("上次登录时间  : %s" % st.get("login_time"))

    try:
        d = core.task_detail()
    except Exception as e:
        d = {"exists": False, "err": repr(e)}
    if d.get("exists"):
        L.append("计划任务      : 已创建（%s，下次 %s）"
                 % (d.get("time"), d.get("next_run")))
    else:
        L.append("计划任务      : 未创建%s"
                 % ("（查询失败 %s）" % d["err"] if d.get("err") else ""))

    L.append("计划任务命令  : %s" % core.task_command())
    L.append("-" * 52)
    L.append("最近日志：")
    L.append(core.read_log_tail(8) or "（空）")

    text = "\n".join(L)

    report = core.BASE / "自检报告.txt"
    try:
        report.write_text(text, encoding="utf-8")
        tail_hint = "\n\n（本报告已保存到 %s）" % report
    except Exception:
        tail_hint = ""

    print(text)
    if not tail_hint:
        print("\n（报告文件写入失败，程序目录可能不可写）")
    else:
        print(tail_hint)
    return report


def open_report(report: Path):
    """用系统默认程序打开自检报告（便携版没有控制台，靠这个给用户看）。"""
    try:
        os.startfile(str(report))  # noqa: S606 - Windows 专用
    except Exception:
        pass


def main():
    core.ensure_std_streams()
    ap = argparse.ArgumentParser(
        description="米游社每日签到（原神 / 崩坏：星穹铁道 / 绝区零）")
    ap.add_argument("action",
                    choices=["login", "sign", "status", "task", "untask", "doctor"],
                    help="login=登录一次  sign=签到  status=查看状态  "
                         "task=注册定时任务  untask=删除定时任务  doctor=环境自检")
    ap.add_argument("--game", action="append", default=None,
                    choices=["all"] + list(core.GAME_KEYS),
                    help="指定游戏，可重复传入（如 --game genshin --game zzz）；"
                         "不传或传 all 表示按界面里勾选的游戏")
    ap.add_argument("--show", action="store_true",
                    help="仅用于 doctor：自检完自动打开报告文件")
    args = ap.parse_args()

    if not args.game or "all" in args.game:
        keys = None                      # None = 跟随界面勾选（默认全部）
    else:
        seen, keys = set(), []
        for k in args.game:              # 去重且保持传入顺序
            if k not in seen:
                seen.add(k)
                keys.append(k)

    if args.action == "login":
        res = core.login()
        sys.exit(0 if res.get("ok") else 1)
    elif args.action == "sign":
        res = core.sign_once(keys, False)
        core.write_log("结果：" + core.summarize(res))
        sys.exit(0 if res.get("ok") else 1)
    elif args.action == "status":
        res = core.sign_once(keys, True)
        sys.exit(0 if res.get("ok") else 1)
    elif args.action == "task":
        res = core.create_task(core.load_settings().get("task_time", "09:05"))
        sys.exit(0 if res.get("ok") else 1)
    elif args.action == "untask":
        res = core.delete_task()
        sys.exit(0 if res.get("ok") else 1)
    elif args.action == "doctor":
        report = doctor()
        if args.show:
            open_report(report)
        sys.exit(0)


if __name__ == "__main__":
    main()
