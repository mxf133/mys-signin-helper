# -*- coding: utf-8 -*-
"""米游社签到助手 · Windows 通知助手（作为 MSIX 包的应用入口运行）

由主程序通过"应用执行别名"调用，例如：
    miyoushe-toast.exe --title "米游社签到失败：原神 需要重新登录" --message "..."

它被 Windows 以"已安装应用"的身份启动（进程带程序包标识），因此可以使用
WinRT 的 ToastNotificationManager 发真正的系统通知（右下角 + 通知中心）。
纯源码运行的 Python 进程没有这个身份，会报 0x80073D54，所以才需要这个助手。

这个 exe 同时负责**把自己注册成已安装应用**（便携版就靠它拿到系统通知能力）：
    miyoushe-toast.exe --register-self      # 需要管理员权限（会开开发者模式）
    miyoushe-toast.exe --unregister-self
    miyoushe-toast.exe --probe              # 诊断各条通道是否可用
注册用的是 WinRT 的 PackageManager API，**不调用 PowerShell**。

它还兼任"系统操作代理"：某些安全软件（360 等）会按"未知/未签名程序"拦截本程序，
实测 python 与打包 exe 都写不了注册表启动项 / 启动文件夹 / 计划任务，所以主程序把
这类系统操作委托给它：
    miyoushe-toast.exe --autostart-create 00:05 --pythonw <路径> --script <路径>
    miyoushe-toast.exe --autostart-delete

--check 只打印自身的程序包标识，用于验证身份是否生效。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_ID = "MiyousheSignInAssistant"
TASK_NAME = "MYS-DailySignIn"
ALT_TASK_NAMES = ("MiyousheSignInAssistant",)
LEGACY_TASK_NAMES = ("MYS-DailySignInTask",)
RUN_VALUE = "MiyousheSignInAssistant"
STARTUP_NAME = "米游社签到助手（开机签到）.cmd"
PKG_NAME = "MiyousheSignInAssistant"
PUBLISHER = "CN=MiyousheSignInAssistant"
# 助手的稳定安装位置（便携版文件夹可以随意搬动/删除，注册指向这里）
INSTALL_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "MiyousheSignInAssistant"
RESULT_FILE = INSTALL_DIR / "register.result"

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
    xml_written = False
    try:
        xml_path.write_text(xml, encoding="utf-16")
        xml_written = True
    except Exception:
        fallback = Path(__file__).resolve().parent / "mys-task-helper.xml"
        try:
            fallback.write_text(xml, encoding="utf-16")
            xml_path = fallback
            xml_written = True
        except Exception:
            pass
    if xml_written:
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
    else:
        notes.append("task=FAIL(无法写入临时XML文件)")
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


def _write_result(text: str):
    """把结论写给调用方（提权进程的 stdout 拿不到）。"""
    try:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        RESULT_FILE.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _dev_mode_on() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock",
                            0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, "AllowDevelopmentWithoutDevLicense")[0] == 1
    except OSError:
        return False


def _enable_dev_mode() -> bool:
    """打开 Windows「开发者模式」——免签名松散注册的前提（需要管理员）。"""
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock",
                            0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "AllowDevelopmentWithoutDevLicense", 0, winreg.REG_DWORD, 1)
    return _dev_mode_on()


def _register_layout(manifest: Path) -> str:
    """用 WinRT 的 PackageManager 注册松散布局（等价 Add-AppxPackage -Register）。

    返回空串表示成功，否则返回错误说明。
    """
    try:
        from winrt.windows.foundation import Uri
        from winrt.windows.management.deployment import (DeploymentOptions,
                                                         PackageManager)
    except Exception as e:
        return "缺少部署 API 绑定：%s" % e
    try:
        pm = PackageManager()
        # 注意：WinRT 的 Uri 不接受裸 Windows 路径（会被当成 scheme 解析），
        # 必须传 file:/// 形式；依赖参数是 IIterable<Uri>，传 None 会 TypeError。
        # 关键：免签名的松散布局必须带 DevelopmentMode 选项，否则部署会以
        # "清单没有位于程序包根目录中"(0x80070057) 拒绝（NONE 只适用于已签名的包）。
        uri = manifest.as_uri() if hasattr(manifest, "as_uri") else str(manifest)
        op = pm.register_package_async(Uri(uri), [],
                                       DeploymentOptions.DEVELOPMENT_MODE)
        waited = 0.0
        while op.status == 0 and waited < 120:          # 0 = Started
            time.sleep(0.2)
            waited += 0.2
        res = op.get_results()
        # 注意：extended_error_code 是 pywinrt 的 HResult 对象，不能直接 & int
        code = getattr(res, "extended_error_code", 0) or 0
        try:
            code_int = int(code)
        except Exception:
            code_int = int(getattr(code, "value", 0) or 0)
        if code_int:
            err = (getattr(res, "error_text", "") or "").strip()
            return "HRESULT 0x%08X%s" % (code_int & 0xFFFFFFFF,
                                         ("（%s）" % err) if err else "")
        return (getattr(res, "error_text", "") or "").strip()
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)


def _find_self(pm):
    """找出本程序的已安装实例。

    注意：pywinrt 对"空集合"返回 None（不是空列表），所以这里要判 None——
    否则第一次注册（本来就没装过）会直接 TypeError。
    非提权时列表接口可能被系统拒绝，返回空列表即可。
    """
    try:
        found = pm.find_packages_by_name_publisher(PKG_NAME, PUBLISHER)
        return list(found) if found else []
    except Exception:
        try:
            allp = pm.find_packages()
            return [p for p in (allp or []) if p.id.name == PKG_NAME]
        except Exception:
            return []


def _remove_installed(pm) -> None:
    for p in _find_self(pm):
        op = pm.remove_package_async(p.id.full_name)
        waited = 0.0
        while op.status == 0 and waited < 120:
            time.sleep(0.2)
            waited += 0.2
        op.get_results()


def register_self() -> int:
    """把自己注册成"已安装应用"（便携版靠这一步拿到系统通知能力）。

    1) 把整套文件复制到 %LOCALAPPDATA%\\MiyousheSignInAssistant\\pkg\\
       —— 稳定位置，便携版文件夹之后可以随便搬走或删掉；
    2) 需要时打开开发者模式（免签名松散注册的前提，需管理员）；
    3) 用 WinRT PackageManager 注册清单；结果写进 register.result 并打印。
    """
    src = Path(sys.executable).resolve().parent
    manifest_src = src / "AppxManifest.xml"
    if not manifest_src.exists():
        msg = "MYS-REGISTER:failed:没找到 AppxManifest.xml（%s）" % src
        print(msg)
        _write_result(msg)
        return 1

    pkg = INSTALL_DIR / "pkg"
    try:
        pkg.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = pkg / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    except Exception as e:
        msg = "MYS-REGISTER:failed:复制文件失败 %s: %s" % (type(e).__name__, e)
        print(msg)
        _write_result(msg)
        return 1

    if not _dev_mode_on():
        if not _is_admin():
            msg = ("MYS-REGISTER:need-admin:需要管理员权限来开启 Windows 开发者模式"
                   "（免签名安装的前提）")
            print(msg)
            _write_result(msg)
            return 1
        try:
            _enable_dev_mode()
            print("已开启 Windows 开发者模式")
        except Exception as e:
            msg = "MYS-REGISTER:failed:开启开发者模式失败 %s: %s" % (type(e).__name__, e)
            print(msg)
            _write_result(msg)
            return 1

    # 同名包已存在时先卸载（同版本不允许覆盖注册，重复运行要幂等）
    try:
        from winrt.windows.management.deployment import PackageManager
        _remove_installed(PackageManager())
    except Exception as e:
        print("清理旧版本时出错（继续尝试注册）：%s" % e)

    err = _register_layout(pkg / "AppxManifest.xml")
    if err:
        msg = "MYS-REGISTER:failed:%s" % err
        print(msg)
        _write_result(msg)
        return 1
    msg = "MYS-REGISTER:ok:%s" % pkg
    print(msg)
    _write_result(msg)
    return 0


def unregister_self() -> int:
    try:
        from winrt.windows.management.deployment import PackageManager
        pm = PackageManager()
        found = _find_self(pm)
        if not found:
            print("未安装")
            return 0
        _remove_installed(pm)
        print("已卸载通知助手")
        return 0
    except Exception as e:
        print("卸载失败：%s: %s" % (type(e).__name__, e))
        return 1


def selftest() -> int:
    """构建后自检：需要的 WinRT 命名空间都能导入吗？身份在吗？通知发得出吗？

    用于给助手做"瘦身"（删掉用不到的 winrt 扩展模块）后的回归验证。
    """
    ok = True
    for mod, label in (("winrt.windows.ui.notifications", "通知"),
                       ("winrt.windows.data.xml.dom", "通知 XML"),
                       ("winrt.windows.management.deployment", "应用部署/自注册")):
        try:
            __import__(mod)
            print("  %-16s OK" % label)
        except Exception as e:
            ok = False
            print("  %-16s 失败：%s" % (label, e))
    name = package_full_name()
    print("  %-16s %s" % ("程序包标识", name or "（无，说明尚未注册）"))
    # 注册流程用到的类型（惰性加载，不实际用一次就发现不了缺模块）
    try:
        from winrt.windows.foundation import Uri
        from winrt.windows.management.deployment import (DeploymentOptions,
                                                         PackageManager)
        Uri("file:///C:/windows/win.ini")
        pm = PackageManager()
        try:
            pm.find_packages_by_name_publisher(PKG_NAME, PUBLISHER)   # 触发集合类型
        except PermissionError:
            pass                    # 非提权时列表会被拒，不算缺模块
        # 用假清单真调一次注册：路径不合法必然失败，但只要是"部署错误"而不是
        # "缺模块 / 参数类型错"，就说明注册这条路的依赖和调用姿势都是对的
        probe = _register_layout(Path("C:/__mys_not_exist__/AppxManifest.xml"))
        if "ModuleNotFound" in probe or "TypeError" in probe:
            raise RuntimeError(probe)
        print("  %-16s OK" % "注册 API")
    except Exception as e:
        ok = False
        print("  %-16s 失败：%s" % ("注册 API", e))
    if ok and name:
        try:
            show_toast("米游社签到助手", "助手自检：这条通知能发出来就说明一切正常。")
            print("  %-16s OK" % "发送通知")
        except Exception as e:
            ok = False
            print("  %-16s 失败：%s" % ("发送通知", e))
    return 0 if ok else 1


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
    ap.add_argument("--selftest", action="store_true", help="构建后自检（导入/身份/通知）")
    ap.add_argument("--register-self", action="store_true",
                    help="把自己注册成已安装应用（需要管理员权限）")
    ap.add_argument("--unregister-self", action="store_true", help="卸载自己")
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
    if args.selftest:
        return selftest()
    if args.register_self:
        return register_self()
    if args.unregister_self:
        return unregister_self()
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
