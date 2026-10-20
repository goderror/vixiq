# utils/browser.py
"""
Selenium Edge 浏览器工厂,消除旧三个类(PixivDownloader/ReverseSearch/ReverseSource)
__init__ 里逐字重复的 EdgeOptions 配置。

- make_browser()        : 统一构造 driver + wait,支持捆绑 driver 与 Selenium Manager 回退
                          user-data-dir / cache 都放项目专属 temp_root (可配置迁 F 盘,
                          不落系统盘 %TEMP% 造成垃圾堆积); quit 后补删本次 profile。
- login_with_cookies() : 三个类的 load_cookies_and_login 公共逻辑
- absolute_xpath()      : 旧 utils.py 的排错工具(仅开发期用)
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.support.ui import WebDriverWait

from utils import paths
from utils.config import load_config
from utils.reporter import Reporter
from utils.sec import load_cookies_smart


# ========== Windows 作业对象 (Job Object): 父进程死 → OS 自动杀光 driver/Edge 树 ==========
# 幻影端口根因 (2026-09 实测证实): exe 被强杀/崩溃后, 孤儿 msedgedriver 与其 Edge 子树
# 仍持有继承来的 Flask 监听 socket 句柄 → netstat 显示死 PID 在 LISTENING, taskkill 找不到
# 进程, 端口永远绑不上。专业软件 (浏览器/IDE/数据库) 的标准根治手段: 把子进程挂进
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 作业对象 —— 父进程无论怎么死 (taskkill /F、os._exit、
# 崩溃), OS 关闭作业句柄时连带杀光作业内全部进程。Win8+ 支持作业嵌套, Edge 自己的子作业
# 也能挂进来, 整棵树一个不漏。
_kill_job_handle = None  # 作业句柄: 整个进程存活期持有, 绝不提前 CloseHandle


def _assign_to_kill_job(pid: int) -> bool:
    """把进程 pid 挂进「父死子随」作业对象。成功 True; 非 Windows/失败静默 False
    (close_fds=True 与启动时 taskkill /T 仍有两层兜底)。"""
    global _kill_job_handle
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        if _kill_job_handle is None:
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

            class _IO_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class _BASIC_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_void_p),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _EXTENDED_LIMIT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            h_job = kernel32.CreateJobObjectW(None, None)
            if not h_job:
                return False
            info = _EXTENDED_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            # JobObjectExtendedLimitInformation = 9
            ok = kernel32.SetInformationJobObject(
                h_job, 9, ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                kernel32.CloseHandle(h_job)
                return False
            _kill_job_handle = h_job  # 句柄泄漏到进程结束 —— 这是故意的: 句柄关=杀树

        # AssignProcessToJobObject 需要 PROCESS_SET_QUOTA | PROCESS_TERMINATE 权限
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h_proc = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not h_proc:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(_kill_job_handle, h_proc))
        finally:
            kernel32.CloseHandle(h_proc)
    except Exception:
        return False


def _assign_browser_tree_to_job(driver_pid: int, user_data_dir: "Path") -> None:
    """把 driver + 本次 profile 的整棵 Edge 进程树全部挂进「父死子随」作业对象。

    只挂 driver 有竞态: webdriver.Edge() 返回时 Edge 主进程可能已经被 driver 拉起
    (它不继承后挂的作业); 而 Edge 的渲染/GPU 子进程还会在接下来几秒陆续出生。
    因此这里按本次专属 --user-data-dir 路径做特征匹配, 全量扫 msedge/msedgedriver,
    首轮同步执行, 之后 daemon 线程再补扫 ~6s 兜住晚出生的子进程。"""
    tag = str(user_data_dir).lower()

    def _matched_pids() -> "set[int]":
        try:
            out = subprocess.run(
                "wmic process where \"name='msedge.exe' or name='msedgedriver.exe'\" "
                "get ProcessId,ParentProcessId,CommandLine /format:csv",
                shell=True, capture_output=True, text=True, timeout=8)
        except Exception:
            return set()
        pids: "set[int]" = set()
        for line in out.stdout.strip().splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            # csv 列序固定为 Node,CommandLine,ParentProcessId,ProcessId;
            # CommandLine 可能含逗号, 从右切 2 刀保证 pid/ppid 对齐
            fields = line.rsplit(',', 2)
            if len(fields) < 3 or not fields[2].strip().isdigit():
                continue
            pid = int(fields[2].strip())
            head_l = fields[0].lower()
            if pid == int(driver_pid) or ('msedge' in head_l and tag in head_l):
                pids.add(pid)
        return pids

    tried: "set[int]" = set()

    def _round():
        for pid in _matched_pids():
            if pid not in tried:
                tried.add(pid)
                _assign_to_kill_job(pid)

    _round()  # 同步首轮: driver + 已出生的 Edge 主进程

    def _tail():
        for _ in range(8):  # 再补扫 ~6.4s, 兜住陆续拉起的渲染/GPU 子进程
            time.sleep(0.8)
            try:
                _round()
            except Exception:
                pass

    threading.Thread(target=_tail, daemon=True).start()


def _file_version(exe_path: str) -> "str | None":
    """通过 Win32 GetFileVersionInfo 读 exe 的 FileVersion,不需要管理员权限。"""
    try:
        import ctypes
        from ctypes import wintypes
        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
        if size <= 0:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(exe_path, 0, size, buf):
            return None
        for cp in (0x040904B0, 0x040904E4, 0x080404B0):
            sub = f"\\StringFileInfo\\{cp:08X}\\FileVersion".encode("utf-16-le")
            p, l = ctypes.c_void_p(), wintypes.UINT()
            if ctypes.windll.version.VerQueryValueW(buf, sub, ctypes.byref(p), ctypes.byref(l)):
                if l.value and p.value:
                    s = ctypes.string_at(p.value, l.value * 2).decode("utf-16-le", errors="ignore")
                    s = s.rstrip("\0").strip()
                    if s and s[0].isdigit():
                        return s.split()[0]
    except Exception:
        return None
    return None


def _edge_info() -> "tuple[str | None, str | None]":
    """查找本机 Edge,返回 (exe路径, 版本号)。非管理员权限也可用。

    依次尝试:
    1) HKCU 注册表(App Paths + BLBeacon, 非管理员 100% 可读)
    2) HKLM 注册表(管理员可读,失败静默忽略)
    3) 已知安装目录(Program Files / Program Files(x86) / %LOCALAPPDATA%)
    """
    try:
        import winreg
        for hkey in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for sub in (
                r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                r"Software\Microsoft\Edge\BLBeacon",
                r"Software\WOW6432Node\Microsoft\Edge\BLBeacon",
            ):
                try:
                    with winreg.OpenKey(hkey, sub) as k:
                        try:
                            exe, _ = winreg.QueryValueEx(k, "")
                            if exe and os.path.isfile(exe):  # type: ignore[name-defined]
                                v = _file_version(exe)
                                return exe, v
                        except OSError:
                            pass
                        try:
                            v, _ = winreg.QueryValueEx(k, "version")
                            if v and v[0].isdigit():
                                try:
                                    ip, _ = winreg.QueryValueEx(k, "InstallLocation")
                                    exe = os.path.join(ip, "Application", "msedge.exe")  # type: ignore[arg-type]
                                    if os.path.isfile(exe):
                                        return exe, v
                                except OSError:
                                    pass
                        except OSError:
                            pass
                except OSError:
                    continue
    except Exception:
        pass
    roots = (r"C:\Program Files (x86)\Microsoft",
             r"C:\Program Files\Microsoft",
             os.path.expandvars(r"%LOCALAPPDATA%\Microsoft"))
    subs = ("Edge", "EdgeCore", "EdgeWebView")
    for r in roots:
        if not os.path.isdir(r):
            continue
        for s in subs:
            base = os.path.join(r, s)
            if not os.path.isdir(base):
                continue
            for cand in (os.path.join(base, "Application", "msedge.exe"),
                         os.path.join(base, "msedge.exe")):
                if os.path.isfile(cand):
                    return cand, _file_version(cand)
            appdir = os.path.join(base, "Application")
            if os.path.isdir(appdir):
                for entry in sorted(os.listdir(appdir), reverse=True):
                    if entry and entry[0].isdigit():
                        cand = os.path.join(appdir, entry, "msedge.exe")
                        if os.path.isfile(cand):
                            return cand, _file_version(cand) or entry
    return None, None


def _driver_version(exe: str) -> "str | None":
    """运行 msedgedriver.exe --version 取版本号,失败返回 None。"""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=8)
        combined = (out.stdout or "") + " " + (out.stderr or "")
        for tok in combined.split():
            if tok and tok[0].isdigit() and tok.count(".") >= 2:
                return tok
    except Exception:
        return None
    return None


def _major(ver: str) -> "int | None":
    try:
        return int(str(ver).split(".")[0])
    except Exception:
        return None


def make_browser(
    headless: "bool | None" = None,
    explicit_wait: "int | None" = None,
    reporter: "Reporter | None" = None,
    config: "dict | None" = None,
):
    """
    构造 Edge driver 与 WebDriverWait。

    headless/explicit_wait 为 None 时从 config 读;
    driver 优先用 resources/msedgedriver.exe,失败回退让 Selenium Manager 自动管理(需联网)。

    存储策略:
      - user-data-dir 固定放到 paths.temp_root()/browser/pid_ts_uuid,
        默认项目 data/temp 可通过 paths.temp_root_override 迁到 F 盘等非系统盘;
      - 磁盘缓存 100MB 上限 (104857600 bytes), 避免 component_crx_cache 无限膨胀;
      - 返回的 driver 包装 quit(): 先标准 quit, 再补删本次 user-data-dir,
        防止强杀/异常时 Temp 里堆 GB 级垃圾。
    返回 (driver, wait)。
    """
    if config is None:
        config = load_config()
    bcfg = config.get("browser", {})
    if headless is None:
        headless = bcfg.get("headless", False)
    if explicit_wait is None:
        explicit_wait = bcfg.get("explicit_wait_crawler", 10)

    # ========== 项目专属 temp profile (关键: 不在系统盘 %TEMP% 堆垃圾) ==========
    browser_root = paths.temp_path("browser")
    profile_name = f"p{os.getpid()}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    user_data_dir = browser_root / profile_name
    user_data_dir.mkdir(parents=True, exist_ok=True)
    disk_cache_dir = user_data_dir / "cache"
    disk_cache_dir.mkdir(parents=True, exist_ok=True)

    options = EdgeOptions()
    options.use_chromium = bcfg.get("use_chromium", True)
    # 强制把 user-data-dir / disk-cache-dir 绑定到项目 temp, 不落系统通用 Temp
    options.add_argument(f"--user-data-dir={str(user_data_dir)}")
    options.add_argument(f"--disk-cache-dir={str(disk_cache_dir)}")
    # 浏览器磁盘缓存硬上限 100MB, 防止 component_crx_cache/Safe Browsing 等目录无限长
    options.add_argument("--disk-cache-size=104857600")
    # 禁止首运行/组件更新弹窗, 减少额外写入与弹窗
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-component-update")
    # 禁 Crashpad: Chromium 会为崩溃报告在系统 %TEMP% 建 scoped_dir 临时目录(单个可达几十 MB),
    # 爬虫用不到崩溃报告, 直接禁掉
    options.add_argument("--disable-crashpad")
    options.add_argument("--disable-breakpad")
    if headless:
        options.add_argument("--headless=new")
    options.page_load_strategy = bcfg.get("page_load_strategy", "eager")

    # 查找 Edge 浏览器 exe + 版本;找到后显式绑定 binary_location
    # (双路径绑定 = driver 路径 + 浏览器路径, 避免驱动启动了却找不到浏览器)
    edge_binary, edge_ver = _edge_info()
    if edge_binary and os.path.isfile(edge_binary):
        options.binary_location = edge_binary

    driver_path = paths.resource("msedgedriver.exe")
    # driver 日志必须落在可写区,否则往 cwd 写会失败(只读区不允许写)
    log_path = paths.user_data("logs/msedgedriver.log")

    driver = None
    # 优先用捆绑 driver,但只在主版本与已装 Edge 匹配时才用,
    # 避免 Edge 自动升级后用旧 driver 启动注定失败(还可能留僵尸进程)
    if driver_path.exists():
        use_bundled = True
        drv_ver = _driver_version(str(driver_path))
        if edge_ver and drv_ver:
            emaj, dmaj = _major(edge_ver), _major(drv_ver)
            if emaj is not None and dmaj is not None and emaj != dmaj:
                if reporter:
                    reporter.warn(
                        f"捆绑 driver(v{drv_ver})与 Edge(v{edge_ver})主版本不匹配,"
                        f"跳过捆绑,改用 Selenium Manager 自动下载匹配版本")
                use_bundled = False
        if use_bundled:
            try:
                # close_fds=True: 禁止 driver 继承本进程的句柄 (关键是 Flask 监听 socket),
                # 从源头切断「父死子在、socket 句柄被子进程持有 → 幻影端口」链条。
                # Win 上 Python 3.7+ 支持 close_fds 与标准流重定向共存 (stdout 日志不受影响)。
                service = EdgeService(str(driver_path), log_output=str(log_path),
                                      popen_kw={"close_fds": True})
                driver = webdriver.Edge(service=service, options=options)
            except Exception as e:
                if reporter:
                    reporter.warn(f"捆绑 driver 启动失败,回退 Selenium Manager: {e}")
                driver = None
    # 回退:不指定 executable_path,让 Selenium Manager 兜底(需联网)
    if driver is None:
        try:
            service = EdgeService(log_output=str(log_path),
                                  popen_kw={"close_fds": True})
            driver = webdriver.Edge(service=service, options=options)
        except Exception:
            # 极端兜底:log_output 参数旧版不支持时完全不传
            try:
                service = EdgeService(popen_kw={"close_fds": True})
                driver = webdriver.Edge(service=service, options=options)
            except Exception as e2:
                # driver 没起来, 清理已经 mkdir 过的空 profile
                _safe_rmtree(user_data_dir)
                raise RuntimeError(
                    f"无法启动 Edge driver。捆绑 driver 存在:{driver_path.exists()};"
                    f" Edge 路径={edge_binary!r} 版本={edge_ver!r};"
                    f" Selenium Manager 回退也失败:{e2}. "
                    f"请到 https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/ "
                    f"下载 Edge v{edge_ver or '?'} 对应的 msedgedriver.exe,"
                    f"替换 resources/msedgedriver.exe 后重试"
                ) from e2

    # ========== 挂进「父死子随」作业对象 ==========
    # 本进程被 taskkill /F / os._exit / 崩溃带走时, OS 自动杀光 driver + Edge 整棵树,
    # 不留孤儿 driver 锁文件、不产生幻影端口。
    try:
        _drv_pid = int(driver.service.process.pid)
        _assign_browser_tree_to_job(_drv_pid, user_data_dir)
        if reporter:
            reporter.log(f"浏览器子进程树 (driver PID {_drv_pid}) 已挂入退出联动作业对象")
    except Exception:
        pass

    # ========== 包装 quit/close, 保证 driver 生命周期结束时删临时 profile ==========
    _patch_driver_cleanup(driver, user_data_dir)

    # P4a+: 页面加载硬超时 —— pixiv 网络不通/代理挂了时 driver.get 会挂几分钟,
    # 有了它 get 最多等 45s 就抛 TimeoutException, 搜索/登录会得到可读错误而不是永远转圈
    try:
        driver.set_page_load_timeout(int(bcfg.get("page_load_timeout", 45)))
    except Exception:
        pass
    driver.implicitly_wait(bcfg.get("implicit_wait", 3))
    # 无头模式下 maximize_window 部分版本会抛 WebDriverException, 不让它炸掉整个启动
    try:
        driver.maximize_window()
    except Exception:
        pass
    wait = WebDriverWait(driver, explicit_wait)
    return driver, wait


def _safe_rmtree(p: "Path | str") -> None:
    """尽量删目录, 被占用/权限失败静默跳过, 留到 atexit / 下次启动的 temp 清理收敛。"""
    try:
        pth = Path(p)
        if not pth.exists():
            return
        shutil.rmtree(str(pth), ignore_errors=False)
    except Exception:
        try:
            # 兜底: 部分 Windows 下 ignore_errors=True 能绕过被锁文件
            shutil.rmtree(str(p), ignore_errors=True)
        except Exception:
            pass


def _patch_driver_cleanup(driver, user_data_dir: "Path") -> None:
    """给 driver 包一层: quit/close/__del__ 时删除本次专属 user-data-dir。"""
    _profile_path = str(user_data_dir)
    _orig_quit = getattr(driver, "quit", None)
    _orig_close = getattr(driver, "close", None)

    def _quit_with_cleanup(*args, **kwargs):
        try:
            if _orig_quit is not None:
                _orig_quit(*args, **kwargs)
        except Exception:
            pass
        _safe_rmtree(_profile_path)

    def _close_maybe_cleanup(*args, **kwargs):
        try:
            if _orig_close is not None:
                _orig_close(*args, **kwargs)
        except Exception:
            pass
        # close 只是关当前窗口, 不删 profile; 只有 quit 时清(多 tab 场景复用 profile)

    try:
        driver.quit = _quit_with_cleanup  # type: ignore[method-assign]
        driver.close = _close_maybe_cleanup  # type: ignore[method-assign]
    except Exception:
        # 某些版本属性只读, 走 atexit 兜底即可
        pass

    # 终极兜底: 进程退出时再删一次 (atexit 由 paths._register_temp_cleanup 统一容量/过期收敛)


def login_with_cookies(
    driver,
    cookies_file: "str | Path",
    reporter: "Reporter | None" = None,
    stop_page: bool = True,
) -> bool:
    """
    用加密 cookies.bin 登录 pixiv.net。向后兼容:找不到 .bin 或绑定非本机时
    自动回退读取旧版明文 cookies.txt,读取成功后自动升级为加密 .bin
    (utils.sec.load_cookies_smart 负责)。

    返回是否成功。
    """
    cookies_file = Path(cookies_file)
    try:
        driver.get("https://www.pixiv.net/")
    except Exception as e:
        # P4a+: 网络不通/代理挂了 → 给出可读提示, 不让上层拿到干巴巴的 TimeoutException
        msg = str(e).splitlines()[0] if str(e) else repr(e)
        if reporter:
            reporter.error(
                f"打开 pixiv.net 失败(网络/代理?): {msg}\n"
                f"  · 请确认系统代理可用(Selenium 启动的 Edge 走系统代理);\n"
                f"  · 或在设置里调大 browser.page_load_timeout 后重试。")
        return False
    driver.delete_all_cookies()
    try:
        # smart load: 优先加密 .bin → 回退同目录 cookies.txt 明文 → 自动升级写回
        legacy_txt = cookies_file.with_suffix(".txt") if cookies_file.suffix != ".txt" else None
        cookies = load_cookies_smart(cookies_file, legacy_txt, upgrade=True)
        if not cookies:
            if reporter:
                reporter.error(
                    "未找到可读取的 cookies 文件。\n"
                    "  · 请先点【获取 Cookies】登录并保存;\n"
                    "  · 如从其他机器拷贝 cookies.bin,将无法解密(绑定本机与当前 Windows 用户)。"
                )
            driver.refresh()
            return False
        ok_count = 0
        for cookie in cookies:
            try:
                driver.add_cookie(cookie)
                ok_count += 1
            except Exception as e:
                # Pixiv cookie 过期时 domain 校验可能失败,跳过单条不影响整体
                if reporter:
                    reporter.warn(f"添加 cookie 失败: {str(e).splitlines()[0]}")
        time.sleep(1)
        if stop_page:
            try:
                driver.execute_script("window.stop();")  # 登录成功,停止加载无关图片
            except Exception:
                pass
        driver.refresh()
        if reporter:
            reporter.log(f"Cookies 登录成功({ok_count}/{len(cookies)} 条加载完成)")
        return True
    except Exception as e:
        if reporter:
            reporter.error(f"登录失败: {str(e).splitlines()[0]}")
        driver.refresh()
        return False


def fetch_pixiv_user(driver) -> "dict | None":
    """
    探测当前 driver 的 Pixiv 登录账号信息(未登录/网络异常返回 None)。

    通道 1 (旧): 在 pixiv.net 同源页面里 fetch `/ajax/user/extra?lang=zh`。
    注意: pixiv 新版 extra 接口 body 已不再返回 user_id/name/profileImg
    (只剩 following/followers 等计数), 所以真实环境常拿不到 → 走通道 2。
    通道 2 (导航兜底): 打开 mypage.php, 已登录会 302 到 /users/{uid},
    再读 `/ajax/user/{uid}` 的 name/image (官方 JSON, 稳定)。

    返回 {'user_id': str, 'name': str, 'avatar': str} 或 None。
    """
    def _probe_extra() -> "dict | None":
        try:
            data = driver.execute_script(
                "return fetch('/ajax/user/extra?lang=zh',{credentials:'include'})"
                ".then(function(r){return r.json()})")
            data = data or {}
            body = data.get('body') or {}
            if data.get('error') or not body.get('user_id'):
                return None
            return {
                'user_id': str(body.get('user_id') or ''),
                'name': body.get('name') or body.get('user_account') or '',
                'avatar': body.get('profileImg') or '',
            }
        except Exception:
            return None

    def _probe_via_navigation() -> "dict | None":
        """新版 extra 不带用户标识: 打开首页从 __NEXT_DATA__.gaUserData.userId
        取登录 uid (mypage.php 现在只 302 到首页, 不再跳 /users/uid),
        再读 `/ajax/user/{uid}` 的 name/image (官方 JSON, 稳定)。"""
        import json as _json
        import re as _re
        try:
            driver.get("https://www.pixiv.net/")
            time.sleep(1.0)
            uid = ""
            try:
                nd = driver.execute_script(
                    "var el=document.getElementById('__NEXT_DATA__');"
                    "return el?el.textContent:null;")
                if nd:
                    data = _json.loads(nd) if isinstance(nd, str) else nd
                    ga = (((data or {}).get('props') or {})
                          .get('pageProps') or {}).get('gaUserData') or {}
                    uid = str(ga.get('userId') or '')
            except Exception:
                uid = ""
            if not uid:
                # 退路: 页面 URL 里偶尔带 /users/uid
                m = _re.search(r'/users/(\d+)', driver.current_url or '')
                if not m:
                    return None  # 未登录
                uid = m.group(1)
            driver.get(f"https://www.pixiv.net/ajax/user/{uid}?lang=zh")
            data = _json.loads(driver.find_element("tag name", "body").text)
            body = (data or {}).get('body') or {}
            if data.get('error') or not body.get('name'):
                return None
            return {
                'user_id': str(body.get('userId') or uid),
                'name': body.get('name') or '',
                'avatar': body.get('image') or body.get('imageBig') or '',
            }
        except Exception:
            return None

    # 必须先落在 pixiv.net 域上, 相对路径 fetch 才有效
    try:
        if 'pixiv.net' not in (driver.current_url or ''):
            driver.get("https://www.pixiv.net/")
            time.sleep(0.8)
    except Exception:
        return None
    user = _probe_extra()
    if user is None:
        # 第一次可能页面还没就绪, 刷新后再试一次
        try:
            driver.refresh()
            time.sleep(1.0)
        except Exception:
            pass
        user = _probe_extra()
    if user is None:
        # 新版 extra 不再带用户标识 → mypage 重定向 + user 资料接口兜底
        user = _probe_via_navigation()
    return user


def absolute_xpath(el):
    """返回元素的绝对 XPath;非 WebElement(如文本节点)返回 None。仅开发排错用。"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.remote.webelement import WebElement

    if not isinstance(el, WebElement):
        return None
    path = ""
    while el.tag_name != "html":
        parent = el.find_element(By.XPATH, "..")
        siblings = parent.find_elements(By.XPATH, f'*[name()="{el.tag_name}"]')
        idx = siblings.index(el) + 1
        path = f"/{el.tag_name}[{idx}]{path}"
        el = parent
    return "/html" + path
