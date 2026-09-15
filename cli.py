# -*- coding: utf-8 -*-
"""米游社签到助手 —— 命令行入口

用法:
    python cli.py login              打开 Edge 登录米游社，凭据加密保存（只需一次）
    python cli.py sign               签到全部游戏（Windows 计划任务调用这个）
    python cli.py sign --game 原神    只签某一个游戏
    python cli.py status             查询签到状态，不重复签到
    python cli.py task               注册计划任务（每天 09:05 自动签到）
    python cli.py untask             删除计划任务

--game 可用值：all（默认）、genshin（原神）、starrail（崩坏：星穹铁道）。
图形界面请直接运行 app.pyw。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def _in_project_venv() -> bool:
    """当前解释器是否已经在项目的 .venv 里。

    不能比较可执行文件名：`.venv\\Scripts` 下 python.exe 与 pythonw.exe 并存，
    计划任务用 pythonw.exe 调本脚本（core.task_command），手动调试用
    python.exe，一比文件名就会永远判定"环境不对"，于是每次定时签到都多起
    一个进程在外面干等，任务也永远不会真正结束。
    """
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


def main():
    core.ensure_std_streams()
    ap = argparse.ArgumentParser(description="米游社每日签到（原神 / 崩坏：星穹铁道）")
    ap.add_argument("action", choices=["login", "sign", "status", "task", "untask"],
                    help="login=登录一次  sign=签到  status=查看状态  "
                         "task=注册定时任务  untask=删除定时任务")
    ap.add_argument("--game", default="all",
                    choices=["all"] + list(core.GAME_KEYS),
                    help="指定游戏，默认 all（全部）")
    args = ap.parse_args()

    keys = None if args.game == "all" else [args.game]

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


if __name__ == "__main__":
    main()
