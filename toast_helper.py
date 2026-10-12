# -*- coding: utf-8 -*-
"""米游社签到助手 · Windows 通知助手（作为 MSIX 包的应用入口运行）

由主程序通过"应用执行别名"调用，例如：
    miyoushe-toast.exe --title "米游社签到失败：原神 需要重新登录" --message "..."

它被 Windows 以"已安装应用"的身份启动（进程带程序包标识），因此可以使用
WinRT 的 ToastNotificationManager 发真正的系统通知（右下角 + 通知中心）。
纯源码运行的 Python 进程没有这个身份，会报 0x80073D54，所以才需要这个助手。

它同时兼任"系统操作代理"：某些安全软件（360 等）会按进程树拦截 python——
实测 python 连"写注册表启动项 / 写启动文件夹 / 调 schtasks"都会被拒（同一个
任务 XML 由 PowerShell 执行却成功）。而这个助手是打包应用、镜像也不是
python.exe，不受该规则限制，所以主程序把"注册开机自启"这类系统操作委托给它：
    miyoushe-toast.exe --autostart-create 00:05 --pythonw <路径> --script <路径>
    miyoushe-toast.exe --autostart-delete
    miyoushe-toast.exe --probe            # 只诊断各条通道是否可用

--check 只打印自身的程序包标识，用于验证身份是否生效。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

APP_ID = "MiyousheSignInAssistant"
TASK_NAME = "MYS-DailySignIn"
ALT_TASK_NAMES = ("MiyousheSignInAssistant",)
LEGACY_TASK_NAMES = ("MYS-DailySignInTask",)
RUN_VALUE = "MiyousheSignInAssistant"
STARTUP_NAME = "米游社签到助手（开机签到）.cmd"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>%(start)s</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>%(command)s</Command><Arguments>%(arguments)s</Arguments></Exec>
  </Actions>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
</Task>
"""


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


# --------------------------------------------------------------- 系统操作代理
def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="gbk",
                          errors="replace", **kw)


def _task_exists(name: str) -> bool:
    return _run(["schtasks", "/Query", "/TN", name]).returncode == 0


def _startup_dir() -> Path:
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" /
            "Start Menu" / "Programs" / "Startup")


def _run_key_write(pythonw: str, script: str) -> bool:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ,
                          '"%s" "%s" sign' % (pythonw, script))
    return True


def _startup_write(pythonw: str, script: str) -> bool:
    d = _startup_dir()
    d.mkdir(parents=True, exist_ok=True)
    body = ("@echo off\r\n"
            "rem 由「米游社签到助手」写入：登录 Windows 后自动签到一次\r\n"
            'start "" "%s" "%s" sign\r\n' % (pythonw, script))
    (d / STARTUP_NAME).write_text(body, encoding="gbk", errors="replace")
    return (d / STARTUP_NAME).exists()


def autostart_create(time_str: str, pythonw: str, script: str) -> str:
    """依次尝试 计划任务 → 注册表启动项 → 启动文件夹，返回成功的模式名。"""
    notes = []
    # 1) 计划任务
    from datetime import datetime
    xml = TASK_XML % {
        "start": datetime.now().strftime("%Y-%m-%d") + "T" + time_str + ":00",
        "command": _xml_escape(pythonw),
        "arguments": _xml_escape('"%s" sign' % script),
        "user": "%s\\%s" % (os.environ.get("USERDOMAIN", ""),
                            os.environ.get("USERNAME", "")),
    }
    xml_path = Path(os.environ.get("TEMP") or ".") / "mys-task-helper.xml"
    try:
        xml_path.write_text(xml, encoding="utf-16")
    except Exception:
        xml_path = Path(__file__).resolve().parent / "mys-task-helper.xml"
        xml_path.write_text(xml, encoding="utf-16")
    for name in (TASK_NAME,) + ALT_TASK_NAMES:
        r = _run(["schtasks", "/Create", "/TN", name, "/XML", str(xml_path), "/F"])
        if r.returncode == 0 and _task_exists(name):
            notes.append("task:%s" % name)
            break
        notes.append("task:%s=%s" % (name, ((r.stdout or "") + (r.stderr or "")).strip()[:60]))
    try:
        xml_path.unlink()
    except OSError:
        pass
    if notes and notes[-1].startswith("task:") and "=" not in notes[-1]:
        return notes[-1]

    # 2) 注册表启动项
    try:
        _run_key_write(pythonw, script)
        notes.append("runkey:OK")
    except Exception as e:
        notes.append("runkey=FAIL(%s)" % e)

    # 3) 启动文件夹
    try:
        _startup_write(pythonw, script)
        notes.append("startup:OK")
    except Exception as e:
        notes.append("startup=FAIL(%s)" % e)

    for line in notes:
        print(line)
    for mode, key in (("task", "task:"), ("runkey", "runkey:OK"), ("startup", "startup:OK")):
        if any(n.startswith(key) and "=" not in n for n in notes):
            return mode
    return ""


def autostart_delete() -> int:
    import winreg
    for name in (TASK_NAME,) + ALT_TASK_NAMES + LEGACY_TASK_NAMES:
        _run(["schtasks", "/Delete", "/TN", name, "/F"])
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
    except Exception:
        pass
    for p in (_startup_dir() / STARTUP_NAME,
              _startup_dir() / "米游社签到助手（开机签到）.lnk"):
        try:
            p.unlink()
        except OSError:
            pass
    print("autostart-deleted")
    return 0


def probe() -> int:
    """诊断：本进程（打包应用）能否做各类系统写入。"""
    import tempfile
    print("package=%s" % (package_full_name() or "(无身份)"))
    try:
        d = tempfile.mkdtemp(prefix="mys_probe_")
        print("  %%TEMP%% 可写: OK (%s)" % d)
        os.rmdir(d)
    except Exception as e:
        print("  %%TEMP%% 可写: FAIL %s" % e)
    try:
        _run_key_write(sys.executable, "probe")
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        print("  注册表启动项: OK")
    except Exception as e:
        print("  注册表启动项: FAIL %s" % e)
    try:
        _startup_write(sys.executable, "probe")
        (_startup_dir() / STARTUP_NAME).unlink()
        print("  启动文件夹: OK")
    except Exception as e:
        print("  启动文件夹: FAIL %s" % e)
    r = _run(["schtasks", "/Query", "/TN", TASK_NAME])
    print("  schtasks 可调用: %s" % ("OK" if r.returncode in (0, 1) and
                                     "找不到" not in (r.stderr or "") else
                                     "FAIL %s" % (r.stderr or "")[:80]))
    return 0


def package_full_name() -> str:
    """读取当前进程的程序包标识（没有则返回空串）。"""
    try:
        import ctypes
        from ctypes import wintypes
        length = wintypes.UINT(0)
        kernel32 = ctypes.windll.kernel32
        # GetCurrentPackageFullName(NULL, &len) 返回 APPMODEL_ERROR_NO_PACKAGE(15700) 时无身份
        rc = kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        if rc == 0 or length.value == 0:
            return ""
        buf = ctypes.create_unicode_buffer(length.value)
        if kernel32.GetCurrentPackageFullName(ctypes.byref(length), buf) == 0:
            return buf.value
        return ""
    except Exception:
        return ""


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def show_toast(title: str, message: str) -> bool:
    import winrt.windows.data.xml.dom as wx
    import winrt.windows.ui.notifications as wn

    xml = ('<toast activationType="foreground">'
           '<visual><binding template="ToastGeneric">'
           '<text>%s</text><text>%s</text>'
           '</binding></visual>'
           '<audio silent="true" />'
           '</toast>' % (_xml_escape(title), _xml_escape(message)))
    doc = wx.XmlDocument()
    doc.load_xml(xml)
    notifier = wn.ToastNotificationManager.create_toast_notifier()
    notifier.show(wn.ToastNotification(doc))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="米游社签到助手通知/系统操作助手")
    ap.add_argument("--title", default="米游社签到助手")
    ap.add_argument("--message", default="")
    ap.add_argument("--check", action="store_true", help="只打印程序包标识后退出")
    ap.add_argument("--probe", action="store_true", help="诊断各条系统通道是否可用")
    ap.add_argument("--autostart-create", metavar="HH:MM",
                    help="注册开机自动签到（计划任务/注册表/启动文件夹依次尝试）")
    ap.add_argument("--autostart-delete", action="store_true", help="移除开机自动签到")
    ap.add_argument("--pythonw", default="", help="开机要启动的 pythonw.exe 路径")
    ap.add_argument("--script", default="", help="开机要运行的 cli.py 路径")
    args = ap.parse_args()

    if args.check:
        name = package_full_name()
        print("package=%s" % (name or "(无身份)"))
        return 0 if name else 1
    if args.probe:
        return probe()
    if args.autostart_delete:
        return autostart_delete()
    if args.autostart_create:
        mode = autostart_create(args.autostart_create, args.pythonw, args.script)
        print("MYS-AUTOSTART:%s" % mode if mode else "MYS-AUTOSTART:failed")
        return 0 if mode else 1

    try:
        show_toast(args.title, args.message)
        return 0
    except Exception as e:
        # 出错时把原因写到 stderr，方便主程序/日志排查
        print("toast-failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
