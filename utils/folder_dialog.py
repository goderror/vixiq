# utils/folder_dialog.py
"""📁 原生「选择文件夹」对话框 —— 2026-09-26

为什么非有不可: 浏览器**拿不到**用户选中目录的绝对路径。`<input type="file" webkitdirectory>`
只给相对路径 (``webkitRelativePath``), 让用户手打路径又等于把「选文件夹」变成「填表单」——
早期实测出现过按钮点了没反应的问题。
所以「选目录」这件事只能交给**服务端进程**去弹系统对话框。

实现走 ``SHBrowseForFolderW`` (shell32) 而不是 ``IFileOpenDialog``: 后者要声明一整套 COM
接口 (vtable 顺序错一位就是崩溃), 前者一个结构体 + 一个回调就够, 稳定性优先。

两个必须注意的点:
  - ``ctypes.windll`` 是**带缓存的共享加载器**, 其 argtypes 是**进程级**的 —— 与托盘/小控件
    那些模块互不兼容的原型会互相覆盖 (2026-09-25 托盘图标被摘掉那次就是它)。所以这里
    一律用**私有实例** ``ctypes.WinDLL("shell32")``。
  - 对话框由**服务端进程**弹出, 而前台窗口是浏览器 —— 不给它 ``SetForegroundWindow`` 就会被
    浏览器挡在身后, 表现又成了点了没反应。所以用 ``BFFM_INITIALIZED`` 回调拿句柄并置前。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

BIF_RETURNONLYFSDIRS = 0x0001      # 只能选目录, 不许选文件
BIF_NEWDIALOGSTYLE = 0x0040        # 新版可缩放对话框 (老版是窄窄一棵树)
BIF_EDITBOX = 0x0010               # 顶部带路径输入框, 可以直接粘贴
BFFM_INITIALIZED = 1
BFFM_SETSELECTIONW = 0x0467      # WM_USER + 103: 打开时把光标定位到某个目录 (wParam=0 → lParam 是路径)

# CoInitializeEx: 公寓线程。shell 对话框内部要用 COM, 请求线程是裸 Python 线程 (未初始化)。
COINIT_APARTMENTTHREADED = 0x2
S_OK, S_FALSE = 0, 1
RPC_E_CHANGED_MODE = -2147417850   # 已经以别的模型初始化过 → 别去 Uninitialize

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32", use_last_error=True)


class BROWSEINFOW(ctypes.Structure):
    # pszDisplayName 声明成 ``c_void_p`` 而**不是** ``wintypes.LPWSTR``: LPWSTR 是 c_wchar_p,
    # 往它上面赋 ``create_unicode_buffer(N)``(c_wchar_Array_N) 会被 ctypes 判为
    # 「incompatible types」直接抛 TypeError —— 2026-09-26 真机实测就是这个 500:
    #   POST /api/wallpaper/dir/pick status=500 resp=error=TypeError: incompatible types,
    #   c_wchar_Array_1024 instance i…      (前端把错误当取消, 会清空输入框)
    # 结构体字段比函数参数严格得多, 这里只能自己 cast 成裸指针。
    _fields_ = [("hwndOwner", wintypes.HWND),
                ("pidlRoot", ctypes.c_void_p),
                ("pszDisplayName", ctypes.c_void_p),
                ("lpszTitle", wintypes.LPCWSTR),
                ("ulFlags", ctypes.c_uint),
                ("lpfn", ctypes.c_void_p),
                ("lParam", ctypes.c_ssize_t),
                ("iImage", ctypes.c_int)]


_BFFCALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_int, wintypes.HWND, ctypes.c_uint, wintypes.LPARAM, wintypes.LPARAM)

# 私有实例上逐个钉死签名 (x64 默认 restype=c_int 会把指针/句柄截成 32 位 = 静默失败)
_shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
_shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
_shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
_shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoInitializeEx.restype = ctypes.c_long
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_ole32.CoUninitialize.argtypes = []
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
# x64 上默认 restype=c_int 会把句柄截成 32 位 → owner 传个残值, 对话框照样弹不出来
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.GetForegroundWindow.argtypes = []
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                 ctypes.c_size_t, ctypes.c_ssize_t]


def pick_folder(title: str = "选择文件夹", initial: "str | None" = None) -> "str | None":
    """弹系统「选择文件夹」对话框; 返回绝对路径, 取消/失败 → None。

    **阻塞**当前线程直到用户选完 —— 调用方必须是请求线程或后台线程, 不能在 GUI 消息循环里调。
    ``initial`` 是打开时的默认定位目录 (传不存在的路径没关系, 对话框会忽略)。
    """
    if os.name != "nt":
        return None
    # 打开时就定位到 initial: BFFM_SETSELECTIONW 的 wParam=0 表示 lParam 是**路径字符串**而非
    # PIDL。对话框跑在**本线程**的消息循环里, 所以直接传自己缓冲区的地址是安全的。
    init_buf = ctypes.create_unicode_buffer(str(initial)) if initial else None

    # 置前: 本进程不是前台进程, 光 SetForegroundWindow 常常被系统拒绝, 先 BringWindowToTop
    @_BFFCALLBACK
    def _cb(hwnd, msg, _lparam, _lparam2):
        if msg == BFFM_INITIALIZED:
            try:
                if init_buf is not None:
                    _user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 0,
                                         ctypes.cast(init_buf, ctypes.c_void_p).value)
            except Exception:
                pass
            try:
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        return 0

    co_ok = False
    try:
        hr = _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        co_ok = hr in (S_OK, S_FALSE)
        if hr == RPC_E_CHANGED_MODE:
            co_ok = False                    # 别人已经初始化过 → 不碰 Uninitialize
    except Exception:
        co_ok = False
    try:
        buf = ctypes.create_unicode_buffer(1024)
        bi = BROWSEINFOW()
        # owner 取**当前前台窗口**(正常就是浏览器): 对话框会挂在它上面, 而不是被它挡在身后。
        # 取不到就退化成没有 owner, 靠回调里的 BringWindowToTop + SetForegroundWindow 置前。
        try:
            owner = int(_user32.GetForegroundWindow() or 0)
        except Exception:
            owner = 0
        bi.hwndOwner = owner or None
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(buf, ctypes.c_void_p)
        bi.lpszTitle = str(title or "选择文件夹")
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
        bi.lpfn = ctypes.cast(_cb, ctypes.c_void_p)
        bi.lParam = 0
        bi.iImage = 0
        pidl = _shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            return None                      # 用户点了取消
        try:
            path = ctypes.create_unicode_buffer(2048)
            if not _shell32.SHGetPathFromIDListW(pidl, ctypes.cast(path, ctypes.c_void_p)):
                return None
            out = str(path.value or "").strip()
        finally:
            try:
                _ole32.CoTaskMemFree(pidl)   # SHBrowseForFolder 返回的 PIDL 必须显式释放
            except Exception:
                pass
    finally:
        if co_ok:
            try:
                _ole32.CoUninitialize()
            except Exception:
                pass
    if not out or not os.path.isdir(out):
        return None
    return out