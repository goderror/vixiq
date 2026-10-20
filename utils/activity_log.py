# utils/activity_log.py
"""实时活动日志 : 把「程序在做什么 + 用户点了什么」逐条记下来,
既**打屏**(终端实时可见)又**落盘**(便于事后自检)。

为什么需要: "平时让你改代码的时候也经常参考一下 log 记录" —— 出问题时要有
一手现场, 而不是靠猜。所以格式必须**单行、带时间戳、带类别、可 grep**。

与另外两个日志的分工:
  - utils/logger    = 后端状态广播 (下载进度/浏览器启动), 面向排障, 看过即散;
  - utils/audit_log = 业务审计 (谁在什么时候改了什么作品), 结构化、可筛选;
  - 本模块          = **全量行为流水**: 每个 HTTP 请求 + 每次点击/控件变化/切页,
                      量大但可滚动、可定期清理, 专门用来"回放现场"。

落盘: <data_root>/logs/activity/activity-YYYY-MM-DD.log (按天分文件)
滚动: 单文件超过 max_mb 就接着写 activity-YYYY-MM-DD.2.log / .3.log ...
保留: 超过 keep_days 天的文件在写入时顺手清掉
性能: 内存缓冲 + 后台定时 flush (默认 0.4s), 绝不因为写日志把请求拖慢 (需求不卡顿)
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from utils import paths

_LOCK = threading.Lock()
_BUF: "list[str]" = []
_STATE = {"started": False, "last_retention": 0.0, "dropped": 0}

# 默认值 (可被 config 的 activity_log 节覆盖; 见 utils/config._canonicalize)
DEFAULTS = {"enabled": True, "console": True, "max_mb": 8, "keep_days": 7}
_FLUSH_INTERVAL = 0.4        # 秒: 缓冲这么久就落盘一次 (批量写, 不逐条 IO)
_RETENTION_EVERY = 600.0     # 秒: 每 10 分钟检查一次过期文件, 不必每次写都扫目录
_BUF_MAX = 4000              # 缓冲上限: 超过就直接丢弃并计数 (宁可丢日志, 不能吃内存)


def _cfg() -> dict:
    """读配置 (失败就用默认值; 日志模块绝不能因为配置问题把主流程搞挂)。"""
    try:
        from utils.config import load_config
        c = (load_config() or {}).get("activity_log") or {}
    except Exception:
        c = {}
    out = dict(DEFAULTS)
    for k, v in c.items():
        if k in out and v is not None:
            out[k] = v
    return out


def _dir() -> Path:
    return paths.user_data("logs/activity")


def _day_base(ts: datetime) -> str:
    return f"activity-{ts.strftime('%Y-%m-%d')}"


def _pick_path(ts: datetime, max_bytes: int) -> Path:
    """当天该写哪个文件: 主文件没超限就用它, 超了顺延 .2/.3… (按大小滚动)。"""
    d = _dir()
    base = _day_base(ts)
    p = d / f"{base}.log"
    if not p.exists() or p.stat().st_size < max_bytes:
        return p
    for k in range(2, 1000):
        q = d / f"{base}.{k}.log"
        if not q.exists() or q.stat().st_size < max_bytes:
            return q
    return p


def _cleanup_old(ts: datetime, keep_days: int) -> None:
    """删除超过保留天数的日志文件 (按文件名里的日期判断, 比 mtime 可靠)。"""
    if keep_days <= 0:
        return
    cutoff = (ts - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for p in _dir().glob("activity-*.log"):
        name = p.name[len("activity-"):]
        day = name[:10]
        if len(day) == 10 and day < cutoff:
            try:
                p.unlink()
            except OSError:
                pass


def _flush_locked() -> None:
    """把缓冲落盘 (调用方必须已持锁)。"""
    if not _BUF:
        return
    cfg = _cfg()
    if not cfg.get("enabled", True):
        _BUF.clear()
        return
    # 支持小数 MB (测试用 0.001 触发滚动): 先乘再取整, 不能先 int() 否则小数会被截成 0
    try:
        max_bytes = max(1024, int(float(cfg.get("max_mb", 8)) * 1024 * 1024))
    except (TypeError, ValueError):
        max_bytes = 8 * 1024 * 1024
    try:
        _dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        _BUF.clear()
        return
    now = datetime.now()
    try:
        p = _pick_path(now, max_bytes)
        with p.open("a", encoding="utf-8") as f:
            f.write("\n".join(_BUF) + "\n")
    except OSError:
        pass          # 磁盘满/被占用: 静默丢弃这一批, 绝不抛给主流程
    _BUF.clear()
    # 保留清理: 每 _RETENTION_EVERY 秒最多扫一次目录
    t = time.time()
    if t - _STATE["last_retention"] > _RETENTION_EVERY:
        _STATE["last_retention"] = t
        try:
            _cleanup_old(now, int(cfg.get("keep_days", 7)))
        except Exception:
            pass


def _flusher() -> None:
    while True:
        time.sleep(_FLUSH_INTERVAL)
        with _LOCK:
            _flush_locked()


def start() -> None:
    """启动后台落盘线程 (幂等; 由 server 启动时调一次)。"""
    with _LOCK:
        if _STATE["started"]:
            return
        _STATE["started"] = True
    threading.Thread(target=_flusher, name="activity-log", daemon=True).start()


def log(kind: str, message: str, **fields) -> None:
    """记一条: 打屏 + 入缓冲。kind 形如 HTTP/CLICK/CHANGE/NAV/UI/DB/SYS/ERR。

    fields 会以 k=v 追加在行尾 (值里的换行/竖线会被压平, 保证"一行一条"好 grep)。
    """
    def _flat(v) -> str:
        s = "" if v is None else str(v)
        return s.replace("\n", " ").replace("\r", " ").replace("|", "/").strip()

    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = [f"{_flat(k)}={_flat(v)}" for k, v in fields.items() if v not in (None, "")]
        line = f"{ts} | {_flat(kind):<6} | {_flat(message)}"
        if parts:
            line += "  |  " + "  ".join(parts)
        cfg = _cfg()
        if cfg.get("console", True):
            try:
                print(line, flush=True)
            except Exception:
                pass
        if not cfg.get("enabled", True):
            return
        with _LOCK:
            if len(_BUF) >= _BUF_MAX:
                _STATE["dropped"] += 1
                return
            if _STATE["dropped"]:
                _BUF.append(f"{ts} | SYS    | 日志缓冲溢出, 丢弃 {_STATE['dropped']} 条")
                _STATE["dropped"] = 0
            _BUF.append(line)
    except Exception:
        pass          # 日志模块自身绝不抛异常


def flush() -> None:
    """强制落盘 (关闭/测试用)。"""
    with _LOCK:
        _flush_locked()


def tail(n: int = 200, day: "str | None" = None) -> dict:
    """读最近 n 行 (跨当天所有滚动文件, 新→旧拼好再取尾部)。供 /api/activity/tail。"""
    try:
        n = max(1, min(int(n), 5000))
    except (TypeError, ValueError):
        n = 200
    flush()
    d = _dir()
    if not d.is_dir():
        return {"day": day or datetime.now().strftime("%Y-%m-%d"), "lines": [], "files": []}
    if day:
        files = sorted(d.glob(f"activity-{day}*.log"))
    else:
        allf = sorted(d.glob("activity-*.log"))
        days = sorted({p.name[len("activity-"):][:10] for p in allf})
        day = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
        files = [p for p in allf if p.name.startswith(f"activity-{day}")]
    out: "list[str]" = []
    for p in files:
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                out.extend(f.read().splitlines())
        except OSError:
            continue
    return {"day": day, "lines": out[-n:], "files": [p.name for p in files]}
