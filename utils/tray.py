# utils/tray.py
"""Windows 托盘常驻 + 壁纸切换事件 (2026-09-24)

exe 的生命周期原本是**围着浏览器转**的: 双击强制弹浏览器, 页面全关就自杀。壁纸却是
纯本地场景, 它像普通桌面小工具那样**缩在右下角任务栏**, 鼠标一悬停就能看到
「现在桌面这张是谁画的」。

本模块只做两件事, 都不碰业务/数据库:

1. **托盘图标** (``Shell_NotifyIconW``): 悬停 tooltip 显示当前壁纸溯源结论, 右键菜单给出
   打开图库 / 打开所在文件夹 / 设置 / 退出。(原来还有一项「图像溯源」, 2026-09-25 用户判定
   「就弹个通知」没意义, 已删除 —— 要跑 ORB 的那个入口在小控件卡片的「图像溯源」按钮上。)
2. **壁纸切换事件** (``RegNotifyChangeKeyValue``): 监听 ``HKCU\\Control Panel\\Desktop``,
   变了就通知托盘刷新 tooltip (事件驱动, 不轮询)。

**零新依赖**: 全部走 ctypes 直接调 Win32 (``pystray``/``PyQt`` 一律不引)。图标复用
``resources/icon.ico``。**tkinter 仍是禁区** —— main.py 记着那次 tcl86t.dll abort
让进程无声消失的事故。

可判定逻辑 (tooltip 文案 / 菜单结构 / 消息→动作映射 / 开关策略) 全部抽成**纯函数**,
ctypes 层只负责按名字执行, 于是这些规则能被冒烟测试离线覆盖 (照 main.py `_browser_gone`
的路子)。溯源本身走 `core.wallpaper_trace`, 与网页面板**同一份规则**。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from core.wallpaper_trace import trace_current_wallpaper, wallpaper_file
from utils import paths

# ── 菜单命令 id (TrackPopupMenu 直接返回它, 省掉 WM_COMMAND 分发) ──
CMD_OPEN_GALLERY = 1001
CMD_OPEN_FOLDER = 1002
CMD_SETTINGS = 1004
CMD_QUIT = 1005

_CMD_LABELS = {
    CMD_OPEN_GALLERY: "打开图库",
    CMD_OPEN_FOLDER: "打开所在文件夹",
    CMD_SETTINGS: "设置…",
    CMD_QUIT: "退出 Vixiq",
}

# ── Win32 常量 ──
WM_NULL = 0x0000
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_QUIT = 0x0012
WM_APP = 0x8000
# 托盘内部消息 (自定义窗口用 WM_APP 段)
WM_TRAY_CALLBACK = WM_APP + 1   # 图标鼠标回调 (lParam 是鼠标消息)
WM_WALL_CHANGED = WM_APP + 2    # 注册表事件线程投递: 壁纸换了
WM_ALREADY_RUNNING = WM_APP + 3  # 第二个实例投递: 提示「已在托盘中运行」
WM_SHOW_MENU = WM_APP + 4       # 自我投递: 把弹菜单挪回建窗线程 (见 tray_window_action)

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 1, 2, 4, 0x10
NIIF_INFO = 1

MF_STRING, MF_GRAYED, MF_DISABLED, MF_SEPARATOR = 0, 1, 2, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080

IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE = 0x0010, 0x0040
IDI_APPLICATION = 32512

TRAY_CLASS_NAME = "VixiqTrayWnd"   # 二实例靠 FindWindowW 找它
TRAY_UID = 1
TOOLTIP_MAX = 120                  # szTip 硬上限 128 wchar 含 NUL → 自限 120
RUNNING_TIP = "Vixiq · 壁纸未溯源"


# ══════════════════════════════════════════════════════════════════════
#  纯函数 (可离线冒烟)
# ══════════════════════════════════════════════════════════════════════
def _oneline(s: str) -> str:
    """压成单行 (tooltip 不接受换行, 多行会被截断或挤成方框)。"""
    return " ".join(str(s or "").split())


def _clip(s: str, n: int = TOOLTIP_MAX) -> str:
    return s if len(s) <= n else s[:max(1, n - 1)] + "…"


def tray_tooltip(info) -> str:
    """托盘悬停提示。格式 `Vixiq · #作品ID · 作者 · 标题`; 溯源不出来就退化成
    「Vixiq · 壁纸未溯源」。**≤120 字符且无换行** (szTip 硬上限 128 wchar 含 NUL)。"""
    if not isinstance(info, dict) or not info.get("active"):
        return RUNNING_TIP
    art = info.get("artwork") or {}
    sm = info.get("summary") or {}
    aid = str(art.get("artwork_id") or "")
    if not aid:
        name = str((info.get("file") or {}).get("name") or "")
        return _clip(_oneline(f"{RUNNING_TIP} · {name}" if name else RUNNING_TIP))
    parts = ["Vixiq", f"#{aid}"]
    for x in (sm.get("author") or art.get("author_name"), sm.get("title")):
        if x:
            parts.append(str(x))
    return _clip(_oneline(" · ".join(parts)))


def tray_menu_items(info) -> "list[tuple]":
    """右键菜单的**纯数据**描述, 由 ctypes 层逐条 AppendMenuW。

    每项 = ``(cmd_id, label, state)``:
      - ``state == 'sep'``    分隔线 (cmd_id/label 忽略)
      - ``state == 'gray'``   灰字不可点 (顶部只读信息行, 或缺文件时的禁用命令)
      - ``state == 'normal'`` 可点命令 (cmd_id 取 CMD_*)
    """
    art = (info or {}).get("artwork") or {}
    f = (info or {}).get("file") or {}
    aid = str(art.get("artwork_id") or "")
    has_file = bool(f.get("path"))
    if aid:
        line1 = f"#{aid} · {art.get('author_name') or '未知作者'}"
        line2 = str(art.get("title") or f.get("name") or "")
    else:
        line1 = "未识别当前壁纸"
        line2 = str(f.get("name") or "（没开幻灯片，或壁纸不在图库内）")
    return [
        (0, line1, "gray"),
        (0, line2, "gray"),
        (0, "", "sep"),
        (CMD_OPEN_GALLERY, _CMD_LABELS[CMD_OPEN_GALLERY], "normal"),
        (CMD_OPEN_FOLDER, _CMD_LABELS[CMD_OPEN_FOLDER], "normal" if has_file else "gray"),
        (0, "", "sep"),
        (CMD_SETTINGS, _CMD_LABELS[CMD_SETTINGS], "normal"),
        (CMD_QUIT, _CMD_LABELS[CMD_QUIT], "normal"),
    ]


def menu_cmd_label(cmd_id) -> str:
    """命令 id → 人话 (日志/气泡用)。"""
    try:
        return _CMD_LABELS.get(int(cmd_id), f"#{cmd_id}")
    except (TypeError, ValueError):
        return f"#{cmd_id}"


def wall_key(path=None) -> str:
    """壁纸「同一张」判定键 = ``路径|mtime_ns-字节数``。取不到 → 空串 (纯函数, 便于冒烟)。"""
    p = wallpaper_file() if path is None else path
    if not p:
        return ""
    try:
        st = Path(p).stat()
    except OSError:
        return ""
    return f"{p}|{st.st_mtime_ns}-{st.st_size}"


def tray_notify_action(prev_key, cur_key, cfg) -> str:
    """壁纸换了之后该做什么: ``none`` 没变 / ``refresh`` 只刷 tooltip / ``bubble`` 再弹气泡。"""
    if not cur_key or cur_key == prev_key:
        return "none"
    ln = ((cfg or {}).get("launcher") or {})
    return "bubble" if ln.get("notify_on_wall_change") else "refresh"


def should_watch_browser(cfg) -> bool:
    """是否启用「浏览器页面全关 → 自动退出」的心跳守护。

    默认 **False**: 托盘常驻后进程不再围着浏览器转, 关掉浏览器不该把它也带走。
    只有用户显式打开 ``launcher.exit_on_browser_close`` 才恢复旧行为。
    """
    ln = ((cfg or {}).get("launcher") or {})
    return bool(ln.get("exit_on_browser_close", False))


def second_instance_action(cfg) -> str:
    """第二次双击 exe 时干什么: ``bubble``=让运行实例的托盘提示一句 (托盘常驻时);
    ``focus``=回退旧行为 (请求激活浏览器窗口 / 重新打开)。"""
    ln = ((cfg or {}).get("launcher") or {})
    return "bubble" if ln.get("tray_enabled", True) else "focus"


def handle_tray_message(msg, wparam, lparam) -> "tuple[str, ...]":
    """把窗口消息翻成「动作名」列表 (纯函数, 便于冒烟)。ctypes 层只按名字执行。"""
    try:
        msg = int(msg)
    except (TypeError, ValueError):
        return ()
    if msg == WM_TRAY_CALLBACK:
        ev = int(lparam) & 0xFFFF
        if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
            return ("menu",)
        if ev == WM_LBUTTONUP:
            return ("refresh",)
        return ()
    if msg == WM_WALL_CHANGED:
        return ("wall_changed",)
    if msg == WM_ALREADY_RUNNING:
        return ("already_running",)
    if msg == WM_CLOSE:
        return ("quit",)
    return ()


def tray_window_action(msg, *, taskbar_created: int = 0) -> "str | None":
    """窗口消息 → 必须在**建窗那个线程**上同步做的动作 (纯函数, 便于冒烟)。

    这些动作**不能丢给子线程**: ``TrackPopupMenu`` 与 ``SetForegroundWindow`` 都要求
    目标窗口属于调用线程, 从别的线程调用时表现为「右键毫无反应 / 菜单一闪就没」——
    这正是 2026-09-24 用户实测「点托盘图标没反应」的根因。
    """
    try:
        m = int(msg)
    except (TypeError, ValueError):
        return None
    if m == WM_SHOW_MENU:
        return "show_menu"
    if taskbar_created and m == taskbar_created:
        return "readd_icon"
    return None


_TASKBAR_CREATED = None


def taskbar_created_msg() -> int:
    """``RegisterWindowMessageW("TaskbarCreated")`` 的值 (0 = 拿不到)。

    explorer 每次重启都会广播这条注册消息, 并**清空所有托盘图标**。不处理的话
    图标永久消失 —— 进程还活着、端口还占着, 但右下角找不到, 用户既以为没启动
    又退不出去。所以收到它必须重新 NIM_ADD。"""
    global _TASKBAR_CREATED
    if _TASKBAR_CREATED is None:
        try:
            u = ctypes.windll.user32
            u.RegisterWindowMessageW.restype = ctypes.c_uint
            u.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
            _TASKBAR_CREATED = int(u.RegisterWindowMessageW("TaskbarCreated") or 0)
        except Exception:
            _TASKBAR_CREATED = 0
    return _TASKBAR_CREATED


# ══════════════════════════════════════════════════════════════════════
#  找托盘窗口 / 投递消息 (二实例 + 注册表事件线程共用的入口)
# ══════════════════════════════════════════════════════════════════════
def find_tray_window() -> int:
    """找运行中实例的托盘消息窗 (找不到返回 0)。"""
    if os.name != "nt":
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(TRAY_CLASS_NAME, None) or 0)
    except Exception:
        return 0


def post_to_tray(msg: int, wparam: int = 0, lparam: int = 0) -> bool:
    """给运行中实例的托盘窗口投一条消息 (没有托盘在跑就返回 False)。"""
    hwnd = find_tray_window()
    if not hwnd:
        return False
    try:
        ctypes.windll.user32.PostMessageW(hwnd, msg, wparam, lparam)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
#  壁纸切换事件线程 (注册表)
# ══════════════════════════════════════════════════════════════════════
KEY_READ, KEY_NOTIFY = 0x20019, 0x0010
REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
HKEY_CURRENT_USER = 0x80000001
WAIT_TIMEOUT = 0x00000102


def watch_wallpaper(stop_event: threading.Event, on_change, *, log=None) -> None:
    """后台线程: ``HKCU\\Control Panel\\Desktop`` 一变就调 ``on_change()``。

    三个坑 (都踩过):
      - ``RegNotifyChangeKeyValue`` 是**一次性** API —— 每次醒来都必须重新注册, 否则只响一次。
      - 异步模式下首次 Wait 可能立刻假返回 (MSDN 明示) → 先丢弃一次。
      - 用 1s 超时轮询 stop 标志, 否则退出时线程卡死在 WaitForSingleObject。
    """
    if os.name != "nt":
        return
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        advapi32.RegOpenKeyExW.restype = ctypes.c_long
        advapi32.RegOpenKeyExW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p)]
        advapi32.RegNotifyChangeKeyValue.restype = ctypes.c_long
        advapi32.RegNotifyChangeKeyValue.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_int]
        # CreateEventW 返回句柄: 不声明 restype 会被截成 32 位 → Wait 永远等到一个假句柄
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_int, wintypes.LPCWSTR]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        hkey = ctypes.c_void_p()
        if advapi32.RegOpenKeyExW(ctypes.c_void_p(HKEY_CURRENT_USER),
                                  r"Control Panel\Desktop", 0,
                                  KEY_READ | KEY_NOTIFY, ctypes.byref(hkey)) != 0:
            return
        ev = kernel32.CreateEventW(None, False, False, None)
        if not ev:
            advapi32.RegCloseKey(hkey)
            return
    except Exception:
        return
    try:
        advapi32.RegNotifyChangeKeyValue(hkey, 1, REG_NOTIFY_CHANGE_LAST_SET, ev, 1)
        kernel32.WaitForSingleObject(ev, 1000)      # 丢弃异步模式的首次假返回
        while not stop_event.is_set():
            rc = kernel32.WaitForSingleObject(ev, 1000)
            if stop_event.is_set():
                break
            if rc == WAIT_TIMEOUT:
                continue
            time.sleep(0.4)                          # debounce: 幻灯片切换会连写几次注册表
            try:
                on_change()
            except Exception:
                pass
            advapi32.RegNotifyChangeKeyValue(hkey, 1, REG_NOTIFY_CHANGE_LAST_SET, ev, 1)
    finally:
        try:
            kernel32.CloseHandle(ev)
        except Exception:
            pass
        try:
            advapi32.RegCloseKey(hkey)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
#  ctypes 结构 (全 W 版, 不混 A 版; 不抄网上 V1/V2 的旧 size)
# ══════════════════════════════════════════════════════════════════════
if os.name == "nt":
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_byte * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD), ("pt", POINT),
                    ("lPrivate", wintypes.DWORD)]

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)


def _load_icon() -> int:
    """托盘图标: 优先 resources/icon.ico, 缺失/失败回退系统默认图标 (绝不因此起不来)。"""
    try:
        p = str(paths.resource("icon.ico"))
    except Exception:
        p = ""
    user32 = ctypes.windll.user32
    # ⚠ 返回的是句柄: 默认 restype 是 c_int, 在 x64 上会把 64 位句柄截断成 32 位 → 图标变空白
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.LoadIconW.restype = ctypes.c_void_p
    # 第二参是 MAKEINTRESOURCE 整数 → 声明成 c_void_p 才能直接塞 32512 (LPCWSTR 会当成字符串)
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    h = 0
    if p and os.path.isfile(p):
        try:
            h = int(user32.LoadImageW(None, p, IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE) or 0)
        except Exception:
            h = 0
    if not h:
        try:
            h = int(user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)) or 0)
        except Exception:
            h = 0
    return h


# 当前活着的托盘 (WNDPROC 是裸函数指针, 只能转发到这一个)
_ACTIVE_TRAY = None


def _tray_wnd_proc(hwnd, msg, wparam, lparam):
    t = _ACTIVE_TRAY
    if t is not None:
        try:
            return t.handle_message(hwnd, msg, wparam, lparam)
        except Exception:
            pass
    return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


if os.name == "nt":
    # ⚠ 必须保留模块级引用: 局部变量被 GC 掉函数指针后, Windows 回调会跳进空地址 → 进程崩
    _WNDPROC_REF = _WNDPROC(_tray_wnd_proc)
else:
    _WNDPROC_REF = None


class Tray:
    """托盘图标 + 隐藏消息窗。``start()`` 起一个 daemon 线程, 建窗与消息循环都在该线程内
    (HWND 线程亲和性天然满足; 主线程已被 ``app.run()`` 占用)。

    窗口是**隐藏的普通顶层窗** (``CreateWindowExW`` 且不带 WS_VISIBLE), **不是**
    ``HWND_MESSAGE`` —— message-only 窗口上 ``TrackPopupMenu``/``SetForegroundWindow``
    行为不可靠 (菜单会一闪就没)。
    """

    def __init__(self, *, port: int, log=None, on_quit=None):
        self.port = int(port)
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self.log = log
        self.on_quit = on_quit
        self.info = None
        self.hwnd = 0
        self.hicon = 0
        self._tid = 0
        self._wall_key = ""
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread = None
        self._wall_thread = None
        self._wall_stop = threading.Event()

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="vixiq-tray")
        self._thread.start()
        self._ready.wait(3.0)
        return bool(self.hwnd)

    def stop(self) -> None:
        """退出: 让托盘线程自己 NIM_DELETE + DestroyWindow + 退消息循环 (不留残图标)。"""
        self._wall_stop.set()
        if self.hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        elif self._tid:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        self._stopped.wait(1.5)

    # ---------- 消息循环 (托盘线程内) ----------
    def _run(self) -> None:
        global _ACTIVE_TRAY
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # ⚠ 所有**返回句柄/指针**的 API 都要显式声明 restype: 默认 c_int 在 x64 上把
        # 64 位句柄截成 32 位 → 建窗/菜单全失败 (而且是静默失败, 只表现为"没反应")
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.GetMessageW.restype = ctypes.c_int
        user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                       wintypes.UINT, wintypes.UINT]
        user32.DispatchMessageW.restype = ctypes.c_ssize_t
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        _ACTIVE_TRAY = self
        self._tid = int(kernel32.GetCurrentThreadId())
        hinst = kernel32.GetModuleHandleW(None)
        try:
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(_WNDPROC_REF, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = TRAY_CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))
            self.hwnd = int(user32.CreateWindowExW(
                0, TRAY_CLASS_NAME, "Vixiq", 0, 0, 0, 0, 0, 0, 0, hinst, None) or 0)
            if not self.hwnd:
                return
            self.hicon = _load_icon()
            # 防幽灵图标 :
            # 上一次进程被强杀时 explorer 会在通知区留一个空壳, 那壳认的就是这组身份
            # (窗口句柄 + uID 1); 句柄值会被系统回收再分配, 本进程的托盘窗有可能拿到同一个
            # 值 —— 先按同一身份删一下, 正好把旧壳顶掉。删一个不存在的图标是无副作用的
            # (返回 False 而已), 所以无脑先删; 真正防幽灵的是**下次启动**这一下, 本次进程
            # 自己不留壳靠的是退出路径 (见 main.py 的控制台处理器)。
            self._icon(NIM_DELETE)
            self._icon(NIM_ADD)
            self._ready.set()
            self.refresh_tooltip()
            # 壁纸切换事件线程 (与托盘分开, 各自一个线程最简单)
            self._wall_key = wall_key()
            self._wall_thread = threading.Thread(
                target=watch_wallpaper, args=(self._wall_stop, self._on_wall_changed),
                daemon=True, name="vixiq-wallwatch")
            self._wall_thread.start()
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                self._icon(NIM_DELETE)
            except Exception:
                pass
            try:
                user32.DestroyWindow(self.hwnd)
                user32.UnregisterClassW(TRAY_CLASS_NAME, hinst)
            except Exception:
                pass
            self.hwnd = 0
            self._stopped.set()

    # ---------- 窗口消息 ----------
    def handle_message(self, hwnd, msg, wparam, lparam):
        m = int(msg)
        # 必须在建窗线程上同步做的两件事 (理由见 tray_window_action 的 docstring)
        act = tray_window_action(m, taskbar_created=taskbar_created_msg())
        if act == "show_menu":
            # 直接在本线程弹: TrackPopupMenu 自带模态消息循环, 不会把托盘线程卡死
            self._show_menu()
            return 0
        if act == "readd_icon":
            # explorer 重启 → 图标被清空, 必须重新 NIM_ADD, 否则永久消失
            try:
                self._icon(NIM_ADD)
            except Exception:
                pass
            threading.Thread(target=self.refresh_tooltip, daemon=True).start()
            return 0
        for a in handle_tray_message(m, wparam, lparam):
            if a == "menu":
                # 关键: 不能在这里直接弹, 更不能开子线程弹 —— 自己给自己投一条消息,
                # 让消息循环把它派发回本线程再弹 (子线程弹菜单会毫无反应)。
                ctypes.windll.user32.PostMessageW(self.hwnd, WM_SHOW_MENU, 0, 0)
            elif a == "refresh":
                threading.Thread(target=self.refresh_tooltip, daemon=True).start()
            elif a == "wall_changed":
                self._on_wall_changed()
            elif a == "already_running":
                self._bubble("Vixiq", "已在托盘中运行（右键托盘图标可打开界面或退出）")
            elif a == "quit":
                self._request_quit()
        if m == WM_CLOSE:
            return 0
        return ctypes.windll.user32.DefWindowProcW(hwnd, m, wparam, lparam)

    # ---------- 图标 ----------
    def _nid(self, flags: int) -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd or 0          # 注意: 0 是合法 HWND, ctypes 直接赋 int 即可
        nid.uID = TRAY_UID
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = self.hicon or 0
        return nid

    def _icon(self, action: int, *, tip=None, info=None, title=None) -> None:
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.szTip = (tip or RUNNING_TIP)[:TOOLTIP_MAX]
        if info:
            nid.uFlags |= NIF_INFO
            nid.szInfoTitle = (title or "Vixiq")[:63]
            nid.szInfo = (info or "")[:255]
            nid.dwInfoFlags = NIIF_INFO
            nid.uTimeout = 10000
        ctypes.windll.shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    def refresh_tooltip(self) -> None:
        """读一次当前壁纸 (秒回三档, **永不 deep**) → 更新 tooltip。

        ORB 太贵 (整库特征首次构建几十秒 + 全程持锁), 悬停提示绝不能走它;
        deep 只在用户点「图像溯源」时跑一次。
        """
        try:
            self.info = trace_current_wallpaper()
        except Exception as e:
            self.info = None
            if self.log:
                self.log(f"托盘补溯源失败: {e}")
        try:
            self._icon(NIM_MODIFY, tip=tray_tooltip(self.info))
        except Exception:
            pass

    def _bubble(self, title: str, text: str) -> None:
        try:
            self._icon(NIM_MODIFY, tip=tray_tooltip(self.info), title=title, info=text)
        except Exception:
            pass

    # ---------- 菜单 ----------
    def _show_menu(self) -> None:
        user32 = ctypes.windll.user32
        # 句柄/命令 id 全部显式声明 (HMENU 与 TrackPopupMenu 的返回值都是 64 位)
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       ctypes.c_size_t, wintypes.LPCWSTR]
        user32.TrackPopupMenu.restype = wintypes.UINT
        user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                          wintypes.LPVOID]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [wintypes.HMENU]
        user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            for cmd, label, state in tray_menu_items(self.info):
                if state == "sep":
                    user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    flags = MF_STRING
                    if state == "gray":
                        flags |= MF_GRAYED | MF_DISABLED
                    user32.AppendMenuW(hmenu, flags, cmd or 0, label)
            pt = POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x, pt.y, 0, self.hwnd, None)
            # 不补这一下, 点菜单外面菜单不消失 (经典坑)
            user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(hmenu)
        if cmd:
            self._run_command(int(cmd))

    def _run_command(self, cmd: int) -> None:
        if self.log:
            self.log(f"托盘菜单: {menu_cmd_label(cmd)}")
        if cmd == CMD_OPEN_GALLERY:
            self._open_url(self.base_url + "#library")
        elif cmd == CMD_OPEN_FOLDER:
            self._open_folder()
        elif cmd == CMD_SETTINGS:
            self._open_url(self.base_url + "#settings")
        elif cmd == CMD_QUIT:
            self._request_quit()

    def _open_url(self, url: str) -> None:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    def _open_folder(self) -> None:
        p = str(((self.info or {}).get("file") or {}).get("path") or "")
        if not p or not os.path.isfile(p):
            return
        try:
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(p)}"])
        except Exception:
            pass

    # ---------- 壁纸事件 ----------
    def _on_wall_changed(self) -> None:
        from utils.config import load_config
        key = wall_key()
        try:
            act = tray_notify_action(self._wall_key, key, load_config())
        except Exception:
            act = tray_notify_action(self._wall_key, key, None)
        self._wall_key = key
        if act == "none":
            return
        self.refresh_tooltip()
        if act == "bubble":
            sm = (self.info or {}).get("summary") or {}
            head = " · ".join(x for x in (sm.get("author"), sm.get("title")) if x)
            self._bubble("壁纸已切换", (f"{head}　{sm.get('line') or ''}".strip()
                                       or "已刷新溯源信息"))

    def _request_quit(self) -> None:
        # 先自己把图标摘掉。退出往往就发生在**托盘线程**上 (菜单命令/--quit 投来的 WM_CLOSE
        # 都在这个线程处理), 此时再投 WM_CLOSE 给自己是没用的 —— 消息循环正被本函数占着,
        # 永远处理不到, _run() 的 finally 不执行 → 进程退出后右下角留一个幽灵图标。
        try:
            self._icon(NIM_DELETE)
        except Exception:
            pass
        try:
            ctypes.windll.user32.PostQuitMessage(0)
        except Exception:
            pass
        if self.on_quit:
            try:
                self.on_quit()
            except Exception:
                pass