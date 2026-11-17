# utils/tunnel.py
"""手机访问 · Cloudflare 快速隧道 (cloudflared quick tunnel) 的生命周期。

为什么要有这一层
----------------
「手机随时看图库」原本要用户自己开终端跑 ``cloudflared tunnel --url ...``、再手抄
一长串临时域名 —— 关了终端就断, 地址也记不住。这里把它收进程序: 设置页/托盘一个开关,
开启后地址直接显示出来, 退出程序时隧道一起关掉。

三条硬约束 (都是 2026-11 实际踩过的坑)
--------------------------------------
1. **子进程 cwd 绝不能是安装目录**。msedgedriver 那次事故就是孤儿进程持有安装目录句柄,
   整个 `vixiq-...` 文件夹删不掉。这里 cwd 固定 data/logs, 日志也写在那儿。
2. **退出必须回收**。``stop()`` 走 terminate → 等 → kill; 启动时还会按 pid 文件清理上次
   异常退出留下的 cloudflared (只认自己的记录, 不碰用户别的隧道)。
3. **公网必须带密码**。access_pin 为空时 main.py 拒绝启动隧道 (安全红线)。

地址是 Cloudflare 的**临时域名**, 每次启动都会变; 想固定域名需要 Named Tunnel
(绑定自己的域名), 那是另一套配置, 不在本模块范围内。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from utils import paths, proc

# quick tunnel 的公网地址形如 https://xxx-yyy-zzz.trycloudflare.com
_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
_URL_TIMEOUT = 30.0       # 等地址的上限 (网络慢时 cloudflared 拉起要十几秒)
_STOP_WAIT = 4.0          # terminate 后等它自己退的时间, 超时再 kill


def parse_public_url(text: str) -> str:
    """从 cloudflared 日志里取出公网地址 (取不到返回空串)。纯函数, 便于冒烟。"""
    if not text:
        return ""
    m = _URL_RE.search(str(text))
    return m.group(0) if m else ""


def last_error_line(text: str) -> str:
    """取日志里最后一条失败原因 (给用户看的那一句), 没有就返回空串。纯函数。

    Cloudflare 对 quick tunnel 有频率限制, 触发时 cloudflared 会退出并留下
    ``failed to request quick Tunnel: ...`` —— 原样把这一句带到界面, 比
    「启动后立即退出」有用得多。
    """
    for line in reversed((text or "").splitlines()):
        s = line.strip()
        if not s:
            continue
        if "failed" in s.lower() or '"level":"error"' in s.lower():
            return s[:180]
    return ""


def find_cloudflared() -> "Path | None":
    """定位 cloudflared.exe: 只读资源区 → 源码 tools/ → PATH。找不到返回 None。"""
    cands = [paths.resource("cloudflared.exe"),
             paths.resource_root() / "tools" / "cloudflared.exe"]
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            pass
    w = shutil.which("cloudflared")
    return Path(w) if w else None


def log_path() -> Path:
    return paths.user_data("logs/cloudflared.log")


def err_path() -> Path:
    """cloudflared 的 stderr (致命错误只写这里, 不进 --logfile 的结构化日志)。"""
    return paths.user_data("logs/cloudflared.err")


def pid_path() -> Path:
    return paths.user_data("logs/cloudflared.pid")


# ── 进程识别 (只杀自己记录下来的那个 PID, 防 PID 复用误杀) ──
def _pid_is_cloudflared(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        # 句柄必须 64 位, 不声明 restype 会被截成 32 位 (静默失败)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = wintypes.DWORD(len(buf))
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return False
            return Path(buf.value).name.lower() == "cloudflared.exe"
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def kill_orphan() -> int:
    """清理上次异常退出遗留的 cloudflared (按 pid 文件, 且校验进程名)。

    返回杀掉的个数; 没有记录/进程已不在都返回 0。**不扫描全系统**, 免得误伤
    用户自己开着的其他 cloudflared 隧道。
    """
    p = pid_path()
    try:
        pid = int(p.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0
    n = 0
    if pid and _pid_is_cloudflared(pid):
        try:
            proc.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            n = 1
        except Exception:
            pass
    try:
        p.unlink()
    except OSError:
        pass
    return n


class Tunnel:
    """一条 quick tunnel。线程安全: 开关可能同时从托盘和设置页触发。"""

    def __init__(self, port: int, *, log=None):
        self.port = int(port)
        self.log = log
        self._proc = None
        self._url = ""
        self._error = ""
        self._starting = False
        self._lock = threading.RLock()   # start() 持锁时还会读 status(), 不能是普通 Lock
        self._watch = None
        self._err_fh = None

    def _say(self, msg: str) -> None:
        if self.log:
            try:
                self.log(msg)
            except Exception:
                pass

    # ---------- 查询 ----------
    def status(self) -> dict:
        with self._lock:
            alive = bool(self._proc is not None and self._proc.poll() is None)
            return {"running": alive, "starting": bool(alive and self._starting),
                    "url": self._url if alive else "", "error": "" if alive else self._error,
                    "pid": int(self._proc.pid) if alive else 0, "port": self.port}

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------- 开关 ----------
    def start(self) -> dict:
        """起隧道。立即返回状态; 地址由后台线程读日志补上 (starting=True)。"""
        with self._lock:
            if self._alive():
                return self.status()
            exe = find_cloudflared()
            if exe is None:
                self._error = "未找到 cloudflared.exe (安装包缺少该组件)"
                self._say("手机访问: " + self._error)
                return self.status()
            lp = log_path()
            try:
                lp.write_text("", encoding="utf-8")
            except OSError:
                pass
            self._url, self._error, self._starting = "", "", True
            args = [str(exe), "tunnel", "--url", f"http://127.0.0.1:{self.port}",
                    "--no-autoupdate", "--protocol", "http2", "--logfile", str(lp)]
            try:
                ep = err_path()
                ep.write_text("", encoding="utf-8")
                self._err_fh = open(ep, "w", encoding="utf-8", errors="replace")
                # cwd = data/logs: 子进程绝不继承安装目录 (见文件头约束 1)
                self._proc = proc.popen(args, cwd=str(lp.parent),
                                        stdout=subprocess.DEVNULL,
                                        stderr=self._err_fh)
            except Exception as e:
                self._error = f"启动 cloudflared 失败: {e}"
                self._starting = False
                self._say("手机访问: " + self._error)
                return self.status()
            try:
                pid_path().write_text(str(self._proc.pid), encoding="utf-8")
            except OSError:
                pass
            self._say(f"手机访问: cloudflared 已启动 (PID {self._proc.pid})")
        self._watch = threading.Thread(target=self._watch_url, args=(lp,),
                                       daemon=True, name="vixiq-tunnel")
        self._watch.start()
        return self.status()

    def stop(self) -> dict:
        """关隧道: terminate → 等 → kill, 并清掉 pid 文件。"""
        with self._lock:
            p, self._proc = self._proc, None
            fh, self._err_fh = self._err_fh, None
            self._starting = False
            self._url = ""
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=_STOP_WAIT)
                    except Exception:
                        p.kill()
            except Exception:
                pass
            self._say("手机访问: cloudflared 已关闭")
        try:
            pid_path().unlink()
        except OSError:
            pass
        return self.status()

    # ---------- 地址监听 ----------
    def _watch_url(self, lp: Path) -> None:
        """轮询 cloudflared 日志, 拿到地址就记下; 进程提前退出就记错误。"""
        deadline = time.time() + _URL_TIMEOUT
        while time.time() < deadline:
            if not self._alive():
                try:
                    detail = (last_error_line(lp.read_text(encoding="utf-8", errors="replace"))
                              or last_error_line(
                                  err_path().read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    detail = ""
                with self._lock:
                    self._error = ("cloudflared 启动后立即退出："
                                   + (detail or "检查网络/代理是否可用"))
                    self._starting = False
                return
            try:
                text = lp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            url = parse_public_url(text)
            if url:
                with self._lock:
                    self._url, self._starting = url, False
                self._say(f"手机访问: 地址 {url}")
                return
            time.sleep(0.4)
        with self._lock:
            self._starting = False
            if not self._url:
                self._error = "等待公网地址超时 (可能是网络/代理不通)"
        self._say("手机访问: " + self._error)
