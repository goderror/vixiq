# main.py
"""
Vixiq · Web 版启动器 (替代 Tkinter 主窗口)。

启动流程:
  1. 用 Flask 起本地服务 (http://127.0.0.1:<port>)
  2. **默认只驻右下角托盘** (launcher.tray_enabled): 悬停看当前壁纸溯源, 右键出菜单
     (打开图库 / 打开所在文件夹 / 图像溯源 / 设置 / 退出 Vixiq)
  3. 是否启动时自动弹浏览器由 launcher.auto_open_browser 决定 (默认关)
  4. 退出唯一口径 = 托盘右键「退出 Vixiq」(Ctrl+C 仍可用);
     心跳守护「关页面即退出」由 launcher.exit_on_browser_close 决定 (默认关)

  ⚠ 2026-09-24 改动: 以前双击 exe 强制弹浏览器、页面全关就自杀 —— 与「托盘常驻」冲突。
    壁纸是纯本地场景, 不需要一直挂着浏览器, 所以生命周期从「围着浏览器转」改成「围着托盘转」。

Web 界面功能 (全部复用 core/db, 取代 gui/):
  - 🔍 查询 作品ID / 作者URL / 关键字 → 结果卡片 + 加入清单
  - 🛒 购物车下载 (SSE 实时进度)
  - 🔎 反搜来源 (按ID直查 / SauceNAO 以图搜源)
  - 📚 图库管理 (统计 / 搜索 / 备份)
  - ⚙ 登录管理 + 实时日志

保留 main.py 名字的原因:
  - PyInstaller build.spec L39 Analysis(['main.py']) —— 打包 exe 的唯一脚本入口
  - PyCharm / VS Code 右上角绿色 ▶️ Run 默认跑这个文件
"""
import sys
import os
import time
import socket
import signal
import atexit
import json
import threading 
import subprocess
import urllib.request
import webbrowser
from pathlib import Path

# 与 web/server.py 顶部路径注入对称, 保证从任意 CWD 执行都能 import
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from utils.config import load_config

DEFAULT_PORT = 7790
# 全局退出标志: 后台线程检测到后应尽快退出
_shutting_down = threading.Event()
# 单实例互斥锁句柄 (Windows 命名互斥锁, 防竞态双开)
_instance_mutex = None
# 当前活着的托盘 (utils/tray.py 的 Tray); 退出时靠它摘图标, 不留残影
_tray = None
# 桌面壁纸小控件 (utils/widget_window.py 的 WallWidget; launcher.wall_widget 开着时才有)
_wall_widget = None
# 服务实际绑定的端口 (端口顺延后才是最终值): 「启动与托盘」开关热应用时按它起组件
_active_port = DEFAULT_PORT
# 心跳守护的**代次**: 每关/每开一次都 +1, 在跑的守护线程发现自己的代次过期就自己退出。
# 用代次而不是「停止事件 + 复用线程」—— 关了又马上开时绝不会出现两条守护线程同时判退出。
_watchdog_gen = 0


def _acquire_single_instance(name: str = "vixiq_web") -> "object | None":
    """Windows 命名互斥锁: 全系统同时只能有一个实例。
    重复启动时已有实例持有同名互斥 → 本进程拿不到创建权 (ERROR_ALREADY_EXISTS),
    返回 None, 调用方交给端口检测/直接打开已运行实例。非 Windows 直接放行。"""
    global _instance_mutex
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes  # type: ignore
        _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        leaked = bool(_instance_mutex and ctypes.windll.kernel32.GetLastError() == 183)
        if leaked:
            ctypes.windll.kernel32.CloseHandle(_instance_mutex)
            _instance_mutex = None
            return None
        return _instance_mutex if _instance_mutex else True
    except Exception:
        return True  # 非 Windows / 拿不到也放行, 端口检测兜底


def _release_instance_mutex():
    global _instance_mutex
    if _instance_mutex:
        try:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(_instance_mutex)
        except Exception:
            pass
        _instance_mutex = None


def _safe_console():
    """让 stdout/stderr 在任何环境都不因字符编码崩溃。

    GBK 控制台 / 输出重定向到文件时, 个别符号(如 emoji)编码失败会直接抛
    UnicodeEncodeError 把进程打死(服务还没起浏览器就打不开)。这里把错误策略
    改成 replace: 显示不了的符号用 '?' 代替, 绝不崩溃。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def _port_alive(host: str, port: int) -> bool:
    """该端口上是否有一个能响应 HTTP 的活实例?
    (端口被占但连不通 = 进程已死的僵尸 socket, 应换端口启动而不是放弃)"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=1.5) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _kill_zombie_port(port: int):
    """端口被占但无 HTTP 响应 (僵尸进程残留 socket) → 找到并杀掉占用端口的进程。
    Windows: netstat 查 PID → taskkill /F。"""
    try:
        out = subprocess.run(
            f'netstat -ano | findstr ":{port}" | findstr LISTENING',
            shell=True, capture_output=True, text=True, timeout=5)
        for line in out.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 5:
                pid = parts[-1]
                if pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(f'taskkill /F /PID {pid}',
                                   shell=True, capture_output=True, timeout=5)
                    print(f"🧹 已清理占用端口 {port} 的残留进程 (PID {pid})")
    except Exception:
        pass


def _kill_orphan_drivers():
    """清理本项目遗留的孤儿 msedgedriver.exe。

    僵尸端口的真凶往往是它: exe 被强杀/崩溃后, 其子进程 msedgedriver 仍存活,
    会锁住 driver 文件 (导致 PyInstaller 重建失败) 并让已死父进程的监听 socket
    变成『幻影占用』(netstat 显示死 PID, taskkill 找不到, 端口 7810 反复被占)。
    只匹配命令行含本项目路径(pixiv-toolkit 开发目录 / vixiq 打包目录)的 driver,
    不误杀用户其他程序。"""
    try:
        out = subprocess.run(
            "wmic process where \"name='msedgedriver.exe'\" get ProcessId,CommandLine /format:csv",
            shell=True, capture_output=True, text=True, timeout=8)
        for line in out.stdout.strip().splitlines()[1:]:
            parts = line.split(',', 2)
            if len(parts) < 3:
                continue
            pid = parts[1].strip()
            cl = parts[2].lower()
            if pid.isdigit() and int(pid) != os.getpid() \
                    and 'msedgedriver.exe' in cl \
                    and ('pixiv-toolkit' in cl or 'vixiq' in cl):
                # /T 连 Edge 子树一起杀: 只杀 driver 的话, Edge 子进程仍持有
                # 继承来的监听 socket 句柄, 幻影端口不会消失
                subprocess.run(f'taskkill /F /T /PID {pid}',
                               shell=True, capture_output=True, timeout=5)
                print(f"🧹 已清理孤儿 msedgedriver 进程树 (PID {pid} 及 Edge 子进程)")
    except Exception:
        pass


def _shutdown_browser():
    """显式退出 Selenium 共享浏览器 (driver.quit 删临时 profile + 释放 driver 端口)。"""
    try:
        from web.server import _reset_search_client
        _reset_search_client()
    except Exception:
        pass


def _tray_stop():
    """摘掉托盘图标 (NIM_DELETE + 退消息循环), 保证退出后右下角不留残影。

    Shell_NotifyIcon 的图标是**挂在窗口上的**, 进程被强杀时图标会留成「幽灵图标」——
    鼠标划过去才消失。所以正常退出路径必须显式删。"""
    global _tray
    t, _tray = _tray, None
    if t is None:
        return
    try:
        t.stop()
    except Exception:
        pass


def _wall_widget_stop():
    """收起桌面小控件窗口 (只是关掉那个控件; 托盘与服务端不受影响)。

    用在应用退出路径 (``_cleanup``) 上: 进程要走了, 不摘窗口会留一个贴边的残影。"""
    global _wall_widget
    w, _wall_widget = _wall_widget, None
    if w is None:
        return
    try:
        w.stop()
    except Exception:
        pass


def _notify_pages_quit() -> None:
    """退出前告诉所有已打开的标签页「应用要退了」: 前端收到 app_quit 会弹遮罩并尝试
    ``window.close``, 一次退出把所有窗口一起收掉。

    ——
    网页版「退出程序」按钮 (``/api/app/quit``) 一直有这条广播, 但**托盘退出**走的是
    本文件的 ``_cleanup`` → ``os._exit(0)``, 完全没广播, 页面就留在那儿变成一个连不上的
    死标签。这里补上同一个事件。

    必须**在 os._exit 之前**发完: ``os._exit`` 不给任何缓冲/flush 机会, 所以 publish
    之后要留一小段等待, 让 SSE 生成器把消息写到 socket 上 (生成器阻塞在
    ``q.get(timeout=15)`` 上, publish 会立刻把它唤醒)。没有任何页面连着就跳过, 不白等。
    """
    try:
        from web.server import _hub
        if _hub.client_count() <= 0:
            return
        _hub.publish({"type": "app_quit"})
    except Exception:
        return  # 服务端没起来/已拆: 没有页面可通知, 静默
    threading.Event().wait(0.6)


def _cleanup():
    """atexit / signal handler 统一收尾: 设标志让后台线程退出,
    显式关浏览器与托盘, 释放单实例锁。"""
    _shutting_down.set()
    _release_instance_mutex()  # 释放单实例互斥锁, 允许下次启动
    print("🧹 正在清理...")  # 正常退出也打印提示 (项目约定: 进程退出时必须有清理提示)
    _notify_pages_quit()  # 先让页面知道, 再拆台 —— 拆完就没 SSE 连接可发了
    _tray_stop()
    _wall_widget_stop()
    # 触发 server.py 的 _cancel_event 让 _kick_metadata_fill 等后台循环退出
    try:
        from web.server import _cancel_event
        _cancel_event.set()
    except Exception:
        pass
    # 显式 driver.quit(): 正常退出走优雅路径 (删 Edge 临时 profile)。
    # 最多等 3s, 等不完也没关系 —— driver 已挂入 KILL_ON_JOB_CLOSE 作业对象,
    # 进程一死 OS 自动杀光整棵树, 不会留孤儿 driver / 幻影端口。
    t = threading.Thread(target=_shutdown_browser, daemon=True)
    t.start()
    t.join(3.0)
    # 给 daemon 线程一瞬时间自行退出 (atexit 后进程即将终止)
    threading.Event().wait(0.3)


def _on_signal(signum, frame):
    """Ctrl+C / taskkill 时: 先收尾再退出, 避免 socket 残留。"""
    print(f"\n🧹 收到退出信号, 正在清理...")
    _cleanup()
    sys.exit(0)


# ── Windows 控制台事件 ──
CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT = 0, 1, 2


def _console_event_action(event) -> str:
    """控制台事件 → 动作名 (纯函数, 便于冒烟)。

    ``quit`` = 自己走清理; ``pass`` = 交回给 Python 的信号处理。
    Ctrl+C / Ctrl+Break 必须让路: ``signal.signal`` 已经把它们翻成 SIGINT/SIGBREAK,
    本处理器抢着处理会把那句「收到退出信号」和 sys.exit 的老路截掉。
    """
    try:
        e = int(event)
    except (TypeError, ValueError):
        return "pass"
    return "quit" if e == CTRL_CLOSE_EVENT else "pass"


# SetConsoleCtrlHandler 的回调是**裸函数指针**: 局部变量被 GC 掉之后 Windows 再调就是
# 跳空地址 → 进程直接崩。必须留模块级引用 (与 tray.py 的 _WNDPROC_REF 同理)。
_console_handler_ref = None


def _install_console_handler() -> None:
    """把「关闭控制台窗口」接到:func:`_cleanup` 上 —— 治托盘幽灵图标。

    退出路径原先只覆盖三条: 托盘菜单退出 / Ctrl+C(``signal``) / ``--quit``。**点终端窗口
    右上角 ✕** 走的是 CTRL_CLOSE_EVENT, 既不跑 atexit 也不触发信号处理器, 进程被直接终结
    → ``_tray_stop`` 没执行 → 右下角留一个空壳 (鼠标划过去才消失)。源码模式与带控制台的
    运行方式反复开关终端, 这些空壳会越积越多, 看上去就是「托盘里有好几个图标」。

    返回值语义: 返回 True = 我接管了, Windows 不再往下调; 所以非 ``quit`` 的事件一律
    返回 False, 让 Python 自己的处理器照常跑。
    """
    global _console_handler_ref
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")   # 私有实例: 免得与托盘/小控件的函数原型串台
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        # 回调是函数指针: 不钉 argtypes 的话 ctypes 未必知道怎么把 WINFUNCTYPE 实例递过去
        k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
        k32.SetConsoleCtrlHandler.argtypes = [HANDLER, wintypes.BOOL]

        def _on_event(event):
            if _console_event_action(event) != "quit":
                return False              # 交回 Python (SIGINT/SIGBREAK 那条路)
            try:
                _cleanup()
            finally:
                os._exit(0)               # 控制台马上要没了, 不留给解释器慢慢收尾的机会
            return True

        _console_handler_ref = HANDLER(_on_event)
        k32.SetConsoleCtrlHandler(_console_handler_ref, True)
    except Exception:
        pass                              # 装不上也不影响启动 (最坏就是老样子留空壳)


def _close_native_splash():
    """关闭 PyInstaller 原生 splash 窗口 (仅打包版存在 pyi_splash 模块;
    源码/开发运行 import 失败 → 静默跳过, 不影响启动)。"""
    try:
        import pyi_splash  # type: ignore
        pyi_splash.close()
    except Exception:
        pass


def _close_splash(stop):
    """收尾启动画面: 停文字动画 + 关原生 splash (服务就绪/异常/无需建服务时调用)。"""
    if stop:
        try:
            stop.set()
        except Exception:
            pass
    _close_native_splash()


# 启动画面状态文字动画帧 (原生 splash 的文字区, 靠 pyi_splash IPC 更新)
_SPLASH_FRAMES = ("正在启动", "正在启动 ·", "正在启动 ··", "正在启动 ···")


def _splash_text_anim():
    """后台线程驱动原生启动画面的状态文字动画; 返回停止事件 (无 splash 时返回 None)。

    关键: 只走 pyi_splash 的 IPC, **完全不用 Python 的 tkinter**。
    早期版本用 Tkinter 在非主线程画「转圈」窗口, 销毁/GC 时 Tcl 会 abort
    (tcl86t.dll, 异常码 0x80000003), 进程**无声消失** → 用户表现是「用一会儿
    各处就 failed to fetch」(2026-09-13 由 Windows 事件日志 Application Error 确认)。"""
    try:
        import pyi_splash  # type: ignore
        if not pyi_splash.is_alive():
            return None
    except Exception:
        return None
    stop = threading.Event()

    def _run():
        i = 0
        while not stop.wait(0.4):
            try:
                pyi_splash.update_text(_SPLASH_FRAMES[i % len(_SPLASH_FRAMES)])
            except Exception:
                return
            i += 1

    threading.Thread(target=_run, daemon=True).start()
    return stop


def _app_log(msg: str) -> None:
    """把启动/退出等生命周期消息落 data/logs/。
    exe 是 windowed (console=False), print 无处可看 —— 落盘后「服务什么时候退的、
    为什么退」才有据可查。日志失败绝不影响主流程。"""
    try:
        import datetime
        from utils import paths
        p = paths.user_data("logs/app_lifecycle.txt")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def _start_tray(port: int) -> None:
    """起托盘线程 (失败静默降级: 没托盘也得让服务跑起来, 否则用户什么都看不到)。"""
    global _tray
    try:
        from utils.tray import Tray
        t = Tray(port=port, log=_app_log, on_quit=_quit_from_tray)
        if t.start():
            _tray = t
        else:
            _app_log("托盘窗口没建起来, 降级为纯服务模式")
    except Exception as e:
        _app_log(f"托盘启动失败: {e}")


def _quit_from_tray() -> None:
    """托盘右键「退出 Vixiq」→ 唯一退出口径 (走和 Ctrl+C 同一条清理路径)。"""
    _app_log("托盘请求退出")
    try:
        _cleanup()
    finally:
        os._exit(0)


def _start_wall_widget(port: int) -> None:
    """(可选) 起桌面壁纸小控件 —— 贴边框的三态分层窗口 (边缘线 → 悬停便签 → 左键窗口)。

    自包含: 数据走本地 HTTP (``/api/wallpaper/current``), 不碰业务层也不建 SSE
    (建了会污染「浏览器是否全关」的判定)。失败静默降级, 不影响托盘/服务。
    """
    global _wall_widget
    try:
        from utils.widget_window import WallWidget
        w = WallWidget(port, log=_app_log)
        if w.start():
            _wall_widget = w
            _app_log("桌面壁纸小控件已就绪 (边缘线 → 悬停便签 → 左键窗口)")
        else:
            _app_log("桌面小控件窗口没建起来")
    except Exception as e:
        _app_log(f"桌面小控件启动失败: {e}")


# ── 「🚀 启动与托盘」开关的热应用 (2026-10-20: 改为无需重启, 存盘即生效) ──
# 这组开关里只有「启动自动开浏览器」是**启动动作** (进程起浏览器那一刻才有意义), 其余四项都能
# 就地生效 —— 于是做成存盘后立刻回调这里 reconcile, 界面不再笼统写「重启后生效」。
_LAUNCHER_STARTUP_ONLY = ("auto_open_browser",)


def _apply_launcher(upd: dict) -> dict:
    """让启动与托盘开关**立即生效** (不重启), 返回逐项结果供接口回传。

    - ``tray_enabled`` / ``wall_widget``   → 就地起/停托盘 / 桌面小控件
    - ``exit_on_browser_close``            → 就地开/关心跳守护 (见 _set_watch_browser)
    - ``notify_on_wall_change``            → 托盘每次判气泡时现读配置, 本来就热, 无需动作
    - ``auto_open_browser``                → 是启动动作, 此刻无浏览器可开, 如实回报 startup_only
    """
    result = {}
    for k, v in (upd or {}).items():
        try:
            if k == "tray_enabled":
                if v and _tray is None:
                    _start_tray(_active_port)
                    result[k] = "started" if _tray is not None else "failed"
                elif (not v) and _tray is not None:
                    _tray_stop()
                    result[k] = "stopped"
                else:
                    result[k] = "unchanged"
            elif k == "wall_widget":
                if v and _wall_widget is None:
                    _start_wall_widget(_active_port)
                    result[k] = "started" if _wall_widget is not None else "failed"
                elif (not v) and _wall_widget is not None:
                    _wall_widget_stop()
                    result[k] = "stopped"
                else:
                    result[k] = "unchanged"
            elif k == "exit_on_browser_close":
                _set_watch_browser(bool(v))
                result[k] = "applied"
            elif k == "notify_on_wall_change":
                result[k] = "applied"
            elif k in _LAUNCHER_STARTUP_ONLY:
                result[k] = "startup_only"
        except Exception as e:
            _app_log(f"热应用 {k} 失败: {e}")
            result[k] = "failed"
    return result


def _register_launcher_bridge() -> None:
    """把:func:`_apply_launcher` 交给 web/server.py —— /api/launcher 存盘后立刻回调它。"""
    try:
        from web.server import set_launcher_apply
        set_launcher_apply(_apply_launcher)
    except Exception as e:
        _app_log(f"启动器热应用钩子未接上 (开关仍需重启生效): {e}")


# 浏览器存活判定阈值 (模块级: 便于冒烟测试直接引用)
_HB_STALE = 300.0     # 心跳兜底: 5min 无心跳才认为浏览器可能真崩了/休眠了。
                      # 取 5min 而非 90s: 浏览器会节流隐藏标签定时器(约 1 次/分钟),
                      # 90s 阈值余量太小, 一旦 SSE 也恰好断开就会误杀服务。
_BYE_GRACE = 12.0     # bye 后宽限 (覆盖刷新: 新页面 1~3s 内会重连并发心跳清掉 bye)
_SSE_GRACE = 30.0     # SSE 全断后宽限 (覆盖刷新/浏览器自动重连, EventSource retry=1s)


def _browser_gone(*, now: float, clients: int, seen_sse: bool, last_client_at: float,
                  ever_hb: bool, last_hb: float, bye_at: float) -> "tuple[bool, str]":
    """判断浏览器是否真的都关了 (纯函数, 便于冒烟测试)。返回 (是否退出, 原因)。

    必须**两个信号都超期**才认定页面全关, 任一显示「页面还在」就不退出:
      ① SSE 连接全部断开且超过宽限 —— 服务端真实连接 (EventSource),
         不受隐藏标签定时器节流影响, 标签关闭会立刻断开;
      ② 心跳与 bye 都已超期 —— 兜底浏览器崩溃/休眠时 SSE 未优雅关闭。
    背景: 旧实现只看心跳 90s, 而隐藏标签定时器被节流(约 1 次/分钟)、省电模式
    还会冻结标签(定时器全停) → 用户切走一会儿服务就自杀, 切回来处处 failed to fetch。"""
    if not (seen_sse or ever_hb):
        return False, "从未有过页面 (纯 API/命令行使用)"
    if clients > 0:
        return False, f"仍有 {clients} 个页面连接"
    if (now - last_client_at) <= _SSE_GRACE:
        return False, "SSE 刚断开, 等待重连宽限"
    if bye_at and (now - bye_at) >= _BYE_GRACE:
        return True, f"页面已关闭 (bye 后 {int(now - bye_at)}s, SSE 已断)"
    if ever_hb and (now - last_hb) > _HB_STALE:
        return True, f"页面已关闭 (心跳停 {int(now - last_hb)}s, SSE 已断)"
    return False, "心跳仍新鲜 (页面可能只是被节流)"


def _set_watch_browser(on: bool) -> None:
    """开/关「关页面即退出」心跳守护 —— 现在改完立即生效, 不需要重启。

    关 = 把代次 +1: 正在跑的守护线程发现自己的代次过期就自行退出 (最多等它一个睡眠周期
    约 3 秒)。开 = 再 +1 起一条新的。代次单调递增, 任何时刻至多一条线程在判退出。
    """
    global _watchdog_gen
    _watchdog_gen += 1
    if not on:
        _app_log("心跳守护已关闭 (launcher.exit_on_browser_close=false): 关浏览器不会退出服务")
        return
    gen = _watchdog_gen
    threading.Thread(target=_heartbeat_watchdog, args=(gen,), daemon=True,
                     name="vixiq-hbwatch").start()
    _app_log("心跳守护已开启 (launcher.exit_on_browser_close=true): 页面全关即退出服务")


def _heartbeat_watchdog(gen: int):
    """浏览器页面心跳守护: 页面全部关闭后自动退出服务 (判定逻辑见 _browser_gone)。

    ``gen`` 是启动它时的代次; 一旦 ``_watchdog_gen`` 变了 (用户在设置页关掉了这个开关) 就
    立刻收工, 绝不再判退出 —— 否则「关了还照样自杀」.
    **默认不启用** —— 托盘常驻后进程不再围着浏览器转, 关掉浏览器不该把它也带走。"""
    from web.server import _heartbeat, _hub
    seen_sse = False
    last_client_at = 0.0
    # 启动宽限: 等首个 SSE 连接或首个心跳 (浏览器约 1~3s 后自动打开)
    for _ in range(30):
        if _watchdog_gen != gen:
            return
        time.sleep(2)
        try:
            if _hub.client_count() > 0 or _heartbeat["ever"]:
                break
        except Exception:
            pass
    while _watchdog_gen == gen:
        time.sleep(3)
        if _watchdog_gen != gen:
            return          # 睡的这一觉里用户把开关关了 → 别再判退出
        now = time.time()
        try:
            clients = _hub.client_count()
        except Exception:
            clients = 0
        if clients > 0:
            seen_sse = True
            last_client_at = now
        gone, why = _browser_gone(
            now=now, clients=clients, seen_sse=seen_sse, last_client_at=last_client_at,
            ever_hb=bool(_heartbeat["ever"]), last_hb=_heartbeat["last"],
            bye_at=_heartbeat["bye_at"])
        if gone:
            _app_log(f"自动退出服务: {why}")
            print(f"\n🧹 检测到浏览器{why}, 自动退出服务", flush=True)
            _cleanup()
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


def main():
    _safe_console()
    # --quit: 保证退得掉的兜底口子。Windows 会把新图标默认收进任务栏的「隐藏图标」
    # 溢出区, 用户点不到它时就完全没有退出入口 (只能去任务管理器)。再跑一次
    # `vixiq.exe --quit` 即可让运行中的实例走正常清理路径退出。
    # 必须放在单实例锁**之前** —— 否则会被当成「重复启动」走进二实例分支。
    if "--quit" in sys.argv[1:]:
        _close_native_splash()
        try:
            import ctypes
            from utils.tray import WM_CLOSE, find_tray_window
            hwnd = find_tray_window()
        except Exception:
            hwnd = 0
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            print("已请求运行中的 Vixiq 退出")
        else:
            print("没找到运行中的 Vixiq (托盘未启动或已退出)")
        return
    # 单实例锁 (Windows 命名互斥): 已有实例在运行 → 本进程直接退出, 不重复启动服务、
    # 也不再 webbrowser.open 新窗口 (否则每次双击 exe 都多开一个同端口标签页)。
    # 拿到 None = 互斥名已被其他实例持有 (ERROR_ALREADY_EXISTS)。
    if _acquire_single_instance() is None:
        # 已有实例在运行: 不重复启动服务。
        # 托盘常驻时 → 给运行实例的托盘投一条消息, 弹「已在托盘中运行」(有反应且不重开窗口);
        # 托盘关掉时 → 回退旧行为: POST /api/focus 激活已有浏览器窗口。
        _close_native_splash()   # 引导器已弹出原生 splash, 本分支不建服务需立即撤掉
        try:
            cfg = load_config()
            port = int(cfg.get("web", {}).get("port", DEFAULT_PORT))
        except Exception:
            cfg, port = {}, DEFAULT_PORT
        url = f"http://127.0.0.1:{port}/"
        try:
            from utils.tray import (WM_ALREADY_RUNNING, post_to_tray,
                                    second_instance_action)
            _act = second_instance_action(cfg)
        except Exception:
            _act, post_to_tray, WM_ALREADY_RUNNING = "focus", None, 0
        if _act == "bubble" and post_to_tray and post_to_tray(WM_ALREADY_RUNNING):
            print(f"Vixiq 已在托盘中运行 ({url})，已提示现有实例")
            return
        try:
            # 用 http.client 直接连 127.0.0.1 (不走系统代理; urllib 在部分机器上
            # 会因代理配置连 localhost 超时)。POST /api/focus → SSE 广播激活窗口。
            import http.client
            _conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            _conn.request("POST", "/api/focus")
            _resp = _conn.getresponse()
            _body = _resp.read().decode("utf-8", "replace")
            _conn.close()
            # clients = 当前活跃浏览器标签页数: >0 只激活已有窗口(不重复开标签);
            # ==0 说明浏览器窗口已全部关闭(服务还在 12s 宽限内) → 重新打开浏览器
            _clients = 0
            try:
                _clients = int(json.loads(_body).get("clients") or 0)
            except Exception:
                pass
            if _clients > 0:
                print(f"Vixiq 已在运行 ({url}), 已激活已打开的窗口")
            else:
                webbrowser.open(url)
                print(f"Vixiq 已在运行, 未发现打开的窗口, 已重新打开浏览器 {url}")
        except Exception:
            # 服务无响应(异常残留): 兜底直接打开浏览器
            webbrowser.open(url)
            print(f"Vixiq 已在运行, 已打开界面 {url}")
        return
    config = load_config()
    port = int(config.get("web", {}).get("port", DEFAULT_PORT))

    # 启动画面文字动画: 在导入重模块 (Flask/cv2/selenium) 之前起, 覆盖这段启动延迟。
    # 只驱动 PyInstaller 原生 splash 的文字 (pyi_splash IPC), 不用 Python tkinter
    # —— 见 _splash_text_anim 注释里那次 tcl86t.dll 崩溃的教训。
    splash_stop = _splash_text_anim()

    # 启动前先清孤儿 msedgedriver: 强杀/崩溃后它常让 7810 变幻影占用(锁文件+僵尸 socket)
    _kill_orphan_drivers()

    try:
        from web.server import create_app
        app = create_app()
    except Exception:
        _close_splash(splash_stop)   # 服务起不来也别让启动画面卡住用户
        raise
    # 设置页「启动与托盘」开关的热应用钩子: 存盘后立刻 reconcile, 不用重启
    _register_launcher_bridge()

    # 端口决策:
    #  - 端口上有活实例(能响应 HTTP) → 不重复启动, 直接打开它
    #  - 端口被占但无响应(僵尸 socket, 强杀/安全软件挂钩后偶发) → 顺延找下一个空闲端口
    if _port_alive("127.0.0.1", port):
        url = f"http://127.0.0.1:{port}/"
        print(f"检测到已有实例在运行: {url}，直接打开界面")
        _close_splash(splash_stop)   # 本进程不建服务, 撤掉启动画面再打开已有界面
        webbrowser.open(url)
        return
    if not _port_free(port):
        old = port
        # 僵尸端口 (被占但无 HTTP 响应): 主动杀残留进程, 杀完重试绑定
        if not _port_alive("127.0.0.1", port):
            _kill_zombie_port(port)
            # OS 回收 socket 需要时间, 重试几次, 每次 1s
            for _ in range(3):
                threading.Event().wait(1.0)
                if _port_free(port):
                    break
            if _port_free(port):
                print(f"🧹 端口 {old} 的残留已清理, 继续使用该端口")
            else:
                for cand in range(port + 1, port + 20):
                    if _port_free(cand):
                        port = cand
                        break
                print(f"⚠ 端口 {old} 仍被占用, 已自动改用端口 {port}")
        else:
            for cand in range(port + 1, port + 20):
                if _port_free(cand):
                    port = cand
                    break
            print(f"⚠ 端口 {old} 被占用但无响应(疑似僵尸残留), 已自动改用端口 {port}")

    url = f"http://127.0.0.1:{port}/"
    # 把最终端口亮给热应用 (端口顺延后, 起托盘/小控件得用真正在跑的那个)
    global _active_port
    _active_port = port
    ln = config.get("launcher") or {}

    # 端口就绪后: 关掉启动画面 → 起托盘 → (可选) 开浏览器 / 桌面小控件。
    # 轮询就绪比固定 1.2s 更稳: 慢机多等、快机秒开, 启动画面正好覆盖这段等待。
    def _start_ui_when_ready():
        for _ in range(120):   # 最多等 60s
            if _port_alive("127.0.0.1", port):
                break
            threading.Event().wait(0.5)
        _close_splash(splash_stop)   # 服务就绪 → 收掉启动画面
        if ln.get("tray_enabled", True):
            _start_tray(port)
        if ln.get("auto_open_browser"):
            webbrowser.open(url)
        if ln.get("wall_widget"):
            _start_wall_widget(port)
        if _tray is not None:
            print("🖥 已在右下角托盘运行（悬停看当前壁纸 · 右键打开图库/设置/退出）")
        else:
            print(f"Vixiq Web 版已启动 -> {url}  (Ctrl+C 退出)")

    threading.Thread(target=_start_ui_when_ready, daemon=True).start()
    _app_log(f"服务启动 (端口 {port}, PID {os.getpid()}, "
             f"托盘={bool(ln.get('tray_enabled', True))})")
    # 启动后若库里有缺元数据的新图 (如批量导入后重启), 后台自动补齐
    # (HTTP 直连 ajax, 不启浏览器; 内部有运行标志防重入/未登录自动跳过)
    try:
        from web.server import (_kick_metadata_fill, warm_thumbs_background,
                                _backfill_page_counts, _normalize_timestamps_at_startup)
        # 时间戳格式统一  几处老代码写的 ISO 串(带 'T')会让「按修改时间」
        # 排序永远把它们顶在最前 —— 启动时先纠正, 不用等用户去跑体检
        _normalize_timestamps_at_startup()
        _kick_metadata_fill()
        # 作品视图「原 N 页」历史数据补齐: 后台抓缺失的总页数 (未登录自动跳过)
        _backfill_page_counts()
        # 后台预生成全库缩略图 (只跑一次): 浏览任意页面全部命中缓存秒开
        warm_thumbs_background()
    except Exception as e:
        print(f"⚠ 后台元数据补全未启动: {str(e).splitlines()[0]}")
    # 注册收尾: Ctrl+C / 关闭终端 / taskkill 时干净退出, 不留僵尸 socket
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)
    # 关闭终端窗口 (CTRL_CLOSE_EVENT) 不触发 atexit / 信号, 必须单独接上, 否则托盘留幽灵图标
    _install_console_handler()
    atexit.register(_cleanup)
    # 心跳守护: 浏览器页面全部关闭后自动退出服务 (daemon, 不阻塞进程退出)。
    # ⚠ 2026-09-24 起**默认不启动** —— 托盘常驻后进程不再围着浏览器转, 关掉浏览器不该把
    #   服务也带走 (以前就是这样, 用户切走一会儿回来处处 failed to fetch)。
    #   想恢复旧行为: 设置页「🚀 启动与托盘」里打开「关页面即退出」, 存盘后立即生效。
    try:
        from utils.tray import should_watch_browser
        _set_watch_browser(should_watch_browser(config))
    except Exception as e:
        _app_log(f"心跳守护未启用: {e}")
    try:
        app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
    except Exception:
        _close_splash(splash_stop)   # 服务异常退出也别让启动画面卡住
        raise


if __name__ == "__main__":
    main()
