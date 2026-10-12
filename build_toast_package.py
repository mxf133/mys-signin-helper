# -*- coding: utf-8 -*-
"""构建并注册"通知助手"（真正的 Windows 通知所需的 MSIX 包）

为什么需要它
------------
WinRT 的 ToastNotificationManager 只允许"有程序包标识（已安装应用）"的进程
调用；纯源码运行的 Python 进程会报 0x80073D54「该进程没有程序包标识符」。
于是这里把极小的 toast_helper.py 用 PyInstaller 打成 exe，放进一个**松散布局的
MSIX 包**里注册（开发者模式下免签名），并声明"应用执行别名"。主程序只要调用
这个别名，助手就等于以已安装应用的身份运行，于是能发真正的 Windows 通知
（右下角弹出 + 进通知中心，带本程序的名字和图标）。

用法
----
    .venv\\Scripts\\python.exe build_toast_package.py           # 构建 + 注册
    .venv\\Scripts\\python.exe build_toast_package.py --check    # 只看状态
    .venv\\Scripts\\python.exe build_toast_package.py --uninstall

卸载：上面的 --uninstall，或「设置 → 应用」里卸载"米游社签到助手"。
说明：注册需要 Windows 开发者模式；WinRT 构建依赖缺失时脚本会自动补齐。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
PKG_DIR = BASE / ".toast_pkg"
BUILD_DIR = BASE / ".toast_build"
# 用 onedir（而不是 onefile）：onefile 启动时要先解压到系统临时目录，而被安全
# 软件管制的机器上临时目录常常不可写，会直接弹 "Could not create temporary
# directory!"。onedir 免解压，启动更快也更稳。
HELPER_SUBDIR = "toast_helper"
HELPER_REL = HELPER_SUBDIR + "\\toast_helper.exe"
HELPER_EXE = PKG_DIR / HELPER_SUBDIR / "toast_helper.exe"
ALIAS = "miyoushe-toast.exe"
PKG_NAME = "MiyousheSignInAssistant"
PUBLISHER = "CN=MiyousheSignInAssistant"

WINRT_WHEELS = ["winrt-Windows.UI.Notifications", "winrt-Windows.Data.Xml.Dom"]

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<Package
  xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10"
  xmlns:uap="http://schemas.microsoft.com/appx/manifest/uap/windows10"
  xmlns:uap5="http://schemas.microsoft.com/appx/manifest/uap/windows10/5"
  xmlns:rescap="http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities"
  IgnorableNamespaces="uap uap5 rescap">
  <Identity Name="{name}" Publisher="{publisher}" Version="{version}" ProcessorArchitecture="x64" />
  <Properties>
    <DisplayName>米游社签到助手</DisplayName>
    <PublisherDisplayName>Miyoushe Sign-in Assistant</PublisherDisplayName>
    <Logo>StoreLogo.png</Logo>
  </Properties>
  <Resources><Resource Language="zh-cn" /></Resources>
  <Dependencies>
    <TargetDeviceFamily Name="Windows.Desktop" MinVersion="10.0.17763.0" MaxVersionTested="10.0.26100.0" />
  </Dependencies>
  <Applications>
    <Application Id="App" Executable="{helper}" EntryPoint="Windows.FullTrustApplication">
      <uap:VisualElements DisplayName="米游社签到助手" Description="米游社签到失败提醒"
        BackgroundColor="transparent" Square150x150Logo="Square150x150Logo.png"
        Square44x44Logo="Square44x44Logo.png" />
      <Extensions>
        <uap5:Extension Category="windows.appExecutionAlias" Executable="{helper}"
          EntryPoint="Windows.FullTrustApplication">
          <uap5:AppExecutionAlias><uap5:ExecutionAlias Alias="{alias}" /></uap5:AppExecutionAlias>
        </uap5:Extension>
      </Extensions>
    </Application>
  </Applications>
  <Capabilities><rescap:Capability Name="runFullTrust" /></Capabilities>
</Package>
"""


def alias_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / ALIAS


def _powershell(script: str, encoding: str = "gbk") -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", script],
                       capture_output=True, text=True, encoding=encoding,
                       errors="replace")
    return ((r.stdout or "") + (r.stderr or "")).strip()


def installed() -> str:
    """返回已安装的包全名；查不到（含被安全软件拦掉 PowerShell）时返回空串。"""
    out = _powershell("(Get-AppxPackage -Name %s).PackageFullName" % PKG_NAME)
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(PKG_NAME + "_"):
            return line
    return ""


# --------------------------------------------------------------- 构建依赖自愈
def ensure_winrt() -> bool:
    """确保 WinRT 绑定可用（只在构建助手时需要）。

    不走 pip：这台机器上安全软件会让 pip 的临时目录解包失败，所以直接下 wheel
    解压进 site-packages。
    """
    try:
        import winrt.windows.data.xml.dom  # noqa: F401
        import winrt.windows.ui.notifications  # noqa: F401
        return True
    except Exception:
        pass

    import site
    targets = site.getsitepackages()
    site_dir = next((Path(p) for p in targets if "site-packages" in p),
                    Path(sys.prefix) / "Lib" / "site-packages")
    tag = "cp%d%d-cp%d%d-win_amd64" % (sys.version_info.major, sys.version_info.minor,
                                       sys.version_info.major, sys.version_info.minor)
    cache = BUILD_DIR / "wheels"
    cache.mkdir(parents=True, exist_ok=True)
    print("   自动补齐 WinRT 构建依赖（%s）" % tag)

    seen, queue, plan = set(), list(WINRT_WHEELS), []
    while queue:
        name = queue.pop(0)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        with urllib.request.urlopen("https://pypi.org/pypi/%s/json" % name, timeout=60) as r:
            data = json.load(r)
        wheel = next((f for f in data["urls"]
                      if f["filename"].endswith(".whl") and tag in f["filename"]), None)
        if not wheel:
            print("   !! %s 没有 %s 的 wheel" % (name, tag))
            return False
        plan.append(wheel)
        for dep in data["info"].get("requires_dist") or []:
            m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", dep)
            if m and m.group(1).lower().startswith("winrt-"):
                queue.append(m.group(1))

    for wheel in plan:
        dst = cache / wheel["filename"]
        if not dst.exists():
            with urllib.request.urlopen(wheel["url"], timeout=180) as r, \
                    open(dst, "wb") as f:
                f.write(r.read())
        with zipfile.ZipFile(dst) as z:
            z.extractall(site_dir)
    print("   已安装 %d 个包到 %s" % (len(plan), site_dir))
    try:
        import winrt.windows.ui.notifications  # noqa: F401
        return True
    except Exception as e:
        print("   装完仍无法导入：%s" % e)
        return False


# --------------------------------------------------------------------- 三步走
def build_helper() -> bool:
    print("== 1/3 用 PyInstaller 打通知助手（onedir）==")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir", "--noconsole", "--name", "toast_helper",
           "--distpath", str(PKG_DIR), "--workpath", str(BUILD_DIR),
           "--specpath", str(BUILD_DIR), str(BASE / "toast_helper.py")]
    if subprocess.run(cmd, cwd=str(BASE)).returncode != 0 or not HELPER_EXE.exists():
        print("   打包失败")
        return False
    size = sum(f.stat().st_size for f in (PKG_DIR / HELPER_SUBDIR).rglob("*")
               if f.is_file())
    print("   生成 %s（%.1f MB）" % (HELPER_REL, size / 1048576))
    return True


def write_layout(version: str) -> bool:
    print("== 2/3 写清单与图标 ==")
    PKG_DIR.mkdir(parents=True, exist_ok=True)
    (PKG_DIR / "AppxManifest.xml").write_text(
        MANIFEST.format(name=PKG_NAME, publisher=PUBLISHER, version=version,
                        alias=ALIAS, helper=HELPER_REL), encoding="utf-8")
    icon = BASE / "icon.png"
    for logo in ("StoreLogo.png", "Square150x150Logo.png", "Square44x44Logo.png"):
        if icon.exists():
            shutil.copyfile(icon, PKG_DIR / logo)
    print("   清单版本 %s（图标取自 icon.png）" % version)
    return True


def register() -> bool:
    """注册/更新松散布局包。

    注意：不能从 Python 直接捕获 Add-AppxPackage 的输出——它往被重定向的
    控制台写日志时会报 "Win32 internal error Access is denied"（读控制台
    缓冲区失败），所以这里把动作写成 .ps1 交给 powershell -File 执行，
    再通过查询 Get-AppxPackage 判断结果。
    """
    print("== 3/3 注册松散布局包（需要开发者模式）==")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    ps1 = BUILD_DIR / "register.ps1"
    ps1.write_text(
        "$ErrorActionPreference = 'Continue'\n"
        "Get-AppxPackage -Name %s | Remove-AppxPackage -ErrorAction SilentlyContinue\n"
        "Add-AppxPackage -Register '%s'\n"
        "if (Get-AppxPackage -Name %s) { 'MYS-OK' } else { 'MYS-FAIL' } | "
        "Out-File -Encoding utf8 '%s'\n"
        % (PKG_NAME, (PKG_DIR / "AppxManifest.xml"), PKG_NAME,
           BUILD_DIR / "register.result"),
        encoding="utf-8-sig")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(ps1)], capture_output=True, text=True,
                   encoding="gbk", errors="replace")
    name = installed()
    if name:
        print("   已注册：%s" % name)
    else:
        # 安全软件会拦掉"python 拉起的 PowerShell"，导致查询拿不到结果，
        # 所以这里不据此判失败——真正的判据是下面通过别名跑一次身份校验。
        print("   查询被拦或未生效，改用别名身份校验判断")
    return True


def uninstall() -> int:
    out = _powershell("Get-AppxPackage -Name %s | Remove-AppxPackage; 'done'" % PKG_NAME)
    if alias_path().exists():
        print("卸载命令输出：%s" % out[:200].replace("\n", " "))
        print("若仍存在，可在「设置 → 应用 → 已安装的应用」里卸载“米游社签到助手”")
        return 1
    print("已卸载（主程序会自动退回到自绘卡片提醒）")
    return 0


def verify() -> bool:
    print("== 验证：通过别名运行助手，确认它带程序包身份 ==")
    exe = alias_path()
    if not exe.exists():
        print("   别名不存在：%s" % exe)
        return False
    try:
        r = subprocess.run([str(exe), "--check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except Exception as e:
        print("   运行失败：%s" % e)
        return False
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print("   " + out)
    return "package=" in out and "(无身份)" not in out


def main() -> int:
    ap = argparse.ArgumentParser(description="构建/注册 Windows 通知助手")
    ap.add_argument("--check", action="store_true", help="只检查状态")
    ap.add_argument("--uninstall", action="store_true", help="卸载通知助手")
    ap.add_argument("--version", default="1.0.0.0", help="包版本（升级时调大）")
    ap.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller")
    ap.add_argument("--no-deps", action="store_true", help="不自动补 WinRT 构建依赖")
    args = ap.parse_args()

    if args.check:
        print("通知助手：%s" % (installed() or "（查询被安全软件拦，见下方身份校验）"))
        print("执行别名：%s（%s）" % (alias_path(),
                                     "存在" if alias_path().exists() else "不存在"))
        # 身份校验才是权威判据：能跑通就说明通知可用
        return 0 if verify() else 1
    if args.uninstall:
        return uninstall()

    if not args.skip_build:
        if not args.no_deps and not ensure_winrt():
            return 1
        if not build_helper():
            return 1
    if not write_layout(args.version) or not register():
        return 1
    ok = verify()
    if ok:
        print("\n结果：通知助手可用，程序会自动优先使用真正的系统通知")
    else:
        print("\n结果：注册没成功或身份校验失败。常见原因：")
        print("  · 未开启开发者模式（需要管理员开启一次）")
        print("  · 包版本与已安装的相同 —— 加 --version 1.0.0.1 调大版本号再试")
        print("  · 安全软件拦了本程序调用 PowerShell —— 加入信任区后重试")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
