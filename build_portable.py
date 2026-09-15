# -*- coding: utf-8 -*-
"""一键打包「免安装便携版」（PyInstaller）。

给不想装 Python 的人用：解压 zip 后双击 exe 即可，目标机器只需要有 Edge。

用法（在项目目录下）:
    .venv\\Scripts\\python.exe build_portable.py

产物:
    dist_portable\\mys-signin-helper-v<版本>-portable.zip
      └─ 解压后是「米游社签到助手-v<版本>-便携版\\」，exe 为中文名

    zip 本身用 ASCII 名，是因为 GitHub Release 会剥掉资产名里的非 ASCII
    字符（中文名上传后会变成 "…-v2.1.0-.zip" 这种残名）。

为什么先把源码复制到临时目录再打包：
  1. 避免 PyInstaller 顺着项目目录把 cookie.enc、.edge_profile、signin.log
     这类私有文件一起收进发行包；
  2. 避开中文路径在某些工具链里的编码问题。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXE_NAME = "米游社签到助手"
BUILD_NAME = "MYS-SignIn-Helper"          # PyInstaller 内部名，用 ASCII 更稳
DIST = HERE / "dist_portable"
PAYLOAD = ("core.py", "cli.py", "app.pyw", "icon.png", "icon.ico")

USAGE_TXT = """米游社签到助手 · 便携版
================================================

这是免安装版：不需要装 Python，双击 exe 就能用。

【第一次使用】

  1. 双击「米游社签到助手.exe」打开界面
  2. 点界面上的【登录】按钮
  3. 会自动弹出 Edge 打开米游社，用手机 App 扫码登录
  4. 登录成功前请不要关掉那个 Edge 窗口，登录完它会自己关
  5. 回到界面点【一键全部签到】试一下

【每天自动签到】

  在界面上点【开启 / 更新】按钮，就会注册一个 Windows 计划任务，
  每天 09:05 在后台自动签到（不需要开着这个程序）。

【关于登录有效期】

  登录凭据大约一个月会失效，之后签到会提示"需要重新登录"。
  这时再点一次【登录】重新扫码即可，其他不用管。

【出问题了怎么办】

  双击「自检.bat」，它会生成一份「自检报告.txt」并自动打开，
  里面能看到登录状态、浏览器检测、计划任务等关键信息。

【重要】

  · 请不要把程序放在 C:\\Program Files 这类受保护目录里，否则
    凭据和日志写不进去。放在桌面、文档或 D 盘都可以。
  · 程序目录里出现 cookie.enc / signin.log / state.json 是正常的，
    那是你的凭据和日志，不要删也不要发给别人。
  · 需要本机装有 Microsoft Edge（Windows 10/11 默认都有）。
"""

CHECK_BAT = """@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0{exe}" doctor --show
"""


def read_version() -> str:
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                  (HERE / "app.pyw").read_text(encoding="utf-8"), re.M)
    if not m:
        raise SystemExit("app.pyw 里没找到 VERSION")
    return m.group(1)


def main() -> int:
    version = read_version()
    print("[1/5] 版本 %s" % version)

    staging = Path(tempfile.mkdtemp(prefix="mys_portable_"))
    src = staging / "src"
    src.mkdir(parents=True)

    for name in PAYLOAD:
        s = HERE / name
        if not s.exists():
            raise SystemExit("缺少文件：%s" % s)
        shutil.copy2(s, src / name)

    # 铁律：绝不把私有文件带进发行包
    leaked = [p.name for p in src.iterdir()
              if p.name in ("cookie.enc", "signin.log", "state.json",
                            "settings.json", ".edge_profile")]
    if leaked:
        raise SystemExit("暂存目录出现私有文件，已中止：%s" % leaked)
    print("[2/5] 源码已复制到暂存目录（已确认无凭据文件）")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",
        "--onedir",
        "--name", BUILD_NAME,
        "--icon", str(src / "icon.ico"),
        "--add-data", "%s%s." % (src / "icon.png", os.pathsep),
        "--collect-all", "playwright",
        "--collect-all", "genshin",
        "--distpath", str(staging / "dist"),
        "--workpath", str(staging / "work"),
        "--specpath", str(staging),
        str(src / "app.pyw"),
    ]
    print("[3/5] PyInstaller 打包中（首次约 2~5 分钟）...")
    r = subprocess.run(cmd, cwd=str(staging))
    if r.returncode != 0:
        print("PyInstaller 失败，退出码 %d" % r.returncode)
        return r.returncode

    built = staging / "dist" / BUILD_NAME
    if not built.exists():
        print("没有找到产物目录 %s" % built)
        return 1

    print("[4/5] 整理发行目录")
    out = DIST / ("%s-v%s-便携版" % (EXE_NAME, version))
    if out.exists():
        shutil.rmtree(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(built), str(out))

    # PyInstaller 生成的 exe 是 ASCII 名，改成中文名更好认
    old_exe = out / (BUILD_NAME + ".exe")
    new_exe = out / (EXE_NAME + ".exe")
    if old_exe.exists():
        old_exe.rename(new_exe)

    (out / "使用说明.txt").write_text(USAGE_TXT, encoding="utf-8")
    (out / "自检.bat").write_bytes(
        CHECK_BAT.format(exe=EXE_NAME).replace("\n", "\r\n").encode("utf-8"))

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print("      目录 %s（%.1f MB）" % (out.name, total / 1e6))

    print("[5/5] 压缩")
    # zip 用 ASCII 名：GitHub Release 会剥掉资产名里的非 ASCII 字符，
    # 若用中文名上传会变成 "…-v2.1.0-.zip" 这种残名。解压出来的目录和
    # exe 仍然是中文名，不影响使用。
    zip_base = DIST / ("mys-signin-helper-v%s-portable" % version)
    if Path(str(zip_base) + ".zip").exists():
        Path(str(zip_base) + ".zip").unlink()
    archive = shutil.make_archive(str(zip_base), "zip", root_dir=str(DIST),
                                  base_dir=out.name)
    zsize = Path(archive).stat().st_size
    print("      完成：%s（%.1f MB）" % (archive, zsize / 1e6))

    shutil.rmtree(staging, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
