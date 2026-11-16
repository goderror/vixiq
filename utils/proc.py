# utils/proc.py
"""子进程调用统一入口 (Windows 下隐藏控制台窗口)。

打包版是 runw.exe —— **没有控制台**。从这里起任何控制台程序
(wmic / taskkill / netstat / icacls / cmd …) 都会先弹出一个黑框再消失,
看起来就是「偶尔闪一下命令窗口」。以前靠每个调用点各自记得加 creationflags,
漏一个就闪; 现在统一收在这里, 新代码直接用它就不会再犯。

另外尽量不用 ``shell=True``: 它会多起一个 cmd.exe (闪窗的主要来源),
参数列表直传既不会闪, 也免去引号转义。
"""
from __future__ import annotations

import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _startupinfo():
    """STARTF_USESHOWWINDOW + SW_HIDE: 双保险 (部分旧接口只认 startupinfo)。"""
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def _hide(kw: dict) -> dict:
    if sys.platform == "win32":
        kw.setdefault("creationflags", NO_WINDOW)
        kw.setdefault("startupinfo", _startupinfo())
    return kw


def run(cmd, **kw):
    """同 :func:`subprocess.run`, 但控制台程序不会闪窗。"""
    return subprocess.run(cmd, **_hide(kw))


def popen(cmd, hide=True, **kw):
    """同 :func:`subprocess.Popen`, 但默认隐藏控制台窗口。

    ``hide=False`` 给资源管理器这类 GUI 程序用: 传 SW_HIDE 会把它们自己的窗口
    也藏起来, 表现就是接口返回成功、桌面上却什么都没弹出来。
    """
    if not hide:
        return subprocess.Popen(cmd, **kw)
    return subprocess.Popen(cmd, **_hide(kw))
