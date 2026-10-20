# utils/logger.py
"""Reporter 工厂:按 config 组合 FileReporter(落 data/logs/)+ 上层 reporter。"""
import datetime
import os

from utils import paths
from utils.config import load_config
from utils.reporter import FileReporter, PrintReporter, Reporter

# 日志只保留最近 N 天 (启动时清理超龄文件, 防止 logs/ 无限累积)
LOG_RETENTION_DAYS = 7
# logs/ 下文件可能上千 (分钟粒度命名), 每次清理逐个 stat 成本高;
# build_reporter 每次请求都会路过这里, 用进程级节流避免把磁盘 IO 摊到热路径。
_CLEANUP_INTERVAL = 600.0
_last_cleanup_ts = 0.0


def _cleanup_old_logs(logs_dir):
    """删除 logs/ 下 mtime 超过 LOG_RETENTION_DAYS 天的 .txt 日志文件 (节流: 最多 10 分钟清理一次)。"""
    global _last_cleanup_ts
    now = datetime.datetime.now().timestamp()
    if now - _last_cleanup_ts < _CLEANUP_INTERVAL:
        return
    _last_cleanup_ts = now
    try:
        keep_after = now - LOG_RETENTION_DAYS * 86400
        for name in os.listdir(logs_dir):
            if not name.endswith(".txt"):
                continue
            p = os.path.join(logs_dir, name)
            try:
                if os.path.getmtime(p) < keep_after:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def build_reporter(inner: "Reporter | None" = None, config: "dict | None" = None) -> Reporter:
    """
    构造默认 reporter 链:
    - config['logging']['write_to_file']=True 时包一层 FileReporter 落盘,转发给 inner
    - 否则直接用 inner(PrintReporter/QueueReporter)
    """
    if config is None:
        config = load_config()
    lcfg = config.get("logging", {})

    if lcfg.get("write_to_file", True):
        pattern = lcfg.get("log_filename_pattern", "日志_{Y%m%d_%H%M}.txt")
        now = datetime.datetime.now()
        name = (
            pattern.replace("{Y%m%d_%H%M}", now.strftime("%Y%m%d_%H%M"))
                   .replace("{Y%m%d}", now.strftime("%Y%m%d"))
        )
        log_file = paths.user_data(f"logs/{name}")
        _cleanup_old_logs(os.path.dirname(log_file))
        return FileReporter(log_file, inner)
    return inner or PrintReporter()
