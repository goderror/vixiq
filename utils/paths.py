# utils/paths.py
"""
路径中枢:区分"可写区"与"只读资源区",杜绝硬编码本机路径。

- 可写区(writable_root):exe 同级目录。data/、config/user.json 都建在这。
  开发期回溯到项目根。
- 只读资源区(resource_root):打包进 exe 内部的资源(default.json/driver/icon)。
  开发期=项目根;frozen 后为 sys._MEIPASS(onedir 的 _internal/ 或 onefile 临时解压区)。
- 数据区(data_root):默认 writable_root/data,支持 paths.data_root_override 重定向到 F 盘等非系统盘
- 临时区(temp_root):默认 data_root/temp,支持 paths.temp_root_override 重定向到 F 盘
  所有 Selenium Edge 临时 profile / Python tempfile / 上传缓冲 / 浏览器磁盘缓存 统一走这,
  带容量上限+过期清理, 不往系统盘 %TEMP% 堆 GB 级垃圾。

关键原则:data/ 必须落用户能看见、能备份、能手动清理的位置;
temp/ 必须是项目独占、可随时整删、不混进系统盘通用 Temp 导致清理工具误杀 site-packages。
"""
import atexit
import os
import sys
import tempfile
import time
from pathlib import Path


def is_frozen() -> bool:
    """是否被 PyInstaller 打包成 exe"""
    return getattr(sys, 'frozen', False)


def writable_root() -> Path:
    """可写根目录:用户解压后双击 exe 的位置,config/ 建在这。"""
    if is_frozen():
        return Path(sys.executable).parent
    # 开发期:本文件在 <root>/utils/paths.py,回溯两级到项目根
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源根:打包进 exe 内部的资源所在。"""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


# ---- 配置感知的 data/temp 根 ----
# 设计: data_root / temp_root 依赖 _paths_cfg() → 依赖 load_config。
# 为避免「config.py 里 import paths.user_data → paths 里 import config 循环」,
# 首次调用 _paths_cfg() 用 importlib 懒加载 config。

_CFG_CACHE: "dict | None" = None
_CFG_READY: bool = False


def _paths_cfg() -> dict:
    """读一次 config.paths,结果进程内缓存, 避免热路径重复 IO。"""
    global _CFG_CACHE, _CFG_READY
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    # 懒加载 config, 防止循环 import
    import importlib
    cfg_mod = importlib.import_module("utils.config")
    cfg = cfg_mod.load_config()
    _CFG_CACHE = cfg.get("paths", {}) or {}
    if not _CFG_READY:
        # 首次读配置 → 注册 atexit + 启动时的 temp 过期清理
        _register_temp_cleanup()
        _CFG_READY = True
    return _CFG_CACHE


def _abs(p: "str | None", fallback: Path) -> Path:
    """把配置里的覆盖路径解析成绝对 Path。空/非法 → fallback。"""
    if not p:
        return fallback
    try:
        ap = Path(str(p)).expanduser()
        if not ap.is_absolute():
            return fallback
        return ap
    except Exception:
        return fallback


def data_root() -> Path:
    """运行时数据根:db/img/logs/cookies 所在, 支持 data_root_override 重定向。
    默认 = writable_root/data,用户也可以写死 D:/vixiq-data 放非系统盘。"""
    fallback = writable_root() / "data"
    override = _paths_cfg().get("data_root_override") or ""
    d = _abs(override, fallback)
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_data_dir() -> Path:
    """data/ 目录(运行时数据:db/img/logs/cookies)。兼容旧命名,等价于 data_root()。"""
    return data_root()


def user_data(rel: "str | Path") -> Path:
    """data_root 下的子路径(自动创建父目录)。rel 形如 'img' / 'logs/x.txt' / 'pixiv.db'"""
    p = data_root() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def temp_root() -> Path:
    """项目独占临时根。所有 Selenium profile / Python tempfile / 上传缓冲统一走这。
    默认 = data_root/temp,也支持 temp_root_override 单独写死 D:/vixiq-temp。"""
    override = _paths_cfg().get("temp_root_override") or ""
    if override:
        fallback = data_root() / "temp"
        d = _abs(override, fallback)
    else:
        d = data_root() / "temp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def temp_path(rel: "str | Path | None" = None) -> Path:
    """temp_root 下的子路径(自动建父目录);不传 rel 只返回根并 mkdir。"""
    if rel is None:
        return temp_root()
    p = temp_root() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def resource(rel: "str | Path") -> Path:
    """resources/ 下的只读资源路径。rel 形如 'msedgedriver.exe' / 'default.json'"""
    return resource_root() / 'resources' / rel


def config_dir() -> Path:
    """config/ 目录(用户可写的 user.json 放这),位于可写区,自动创建。"""
    d = writable_root() / 'config'
    d.mkdir(parents=True, exist_ok=True)
    return d


def user_config_path() -> Path:
    """config/user.json 路径(用户覆盖配置,可写,.gitignore)"""
    return config_dir() / 'user.json'


# ============== temp 清理守护 ==============
# 策略:
#   1) 启动时: 清 temp_root 下所有 > temp_max_age_h 的旧目录/文件
#   2) 容量: 启动时 + 每次 atexit: 若 temp_root 总容量 > temp_max_size_mb,
#      按 mtime 从旧到新删直到低于阈值
#   3) atexit: 清本次启动产生的 "browser/本次pid前缀" 子目录 + 再次跑过期/容量收敛


def _dir_size_bytes(p: Path) -> int:
    s = 0
    for r, _, fs in os.walk(str(p)):
        for f in fs:
            try:
                s += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return s


def _cleanup_temp(max_age_h: "int | None" = None,
                  max_size_mb: "int | None" = None,
                  aggressive: bool = False) -> "dict":
    """执行 temp_root 的过期/容量清理。返回清理统计。"""
    try:
        cfg = _paths_cfg()
        tr = temp_root()
    except Exception:
        return {"removed": 0, "freed_mb": 0.0, "reason": "paths_unavailable"}
    if max_age_h is None:
        max_age_h = int(cfg.get("temp_max_age_h", 24) or 24)
    if max_size_mb is None:
        max_size_mb = int(cfg.get("temp_max_size_mb", 5120) or 5120)
    now = time.time()
    age_cut = now - max(1, max_age_h) * 3600
    removed = 0
    freed = 0

    # Step 1: 按过期删 (顶层条目: 文件或目录)
    try:
        entries = list(tr.iterdir())
    except OSError:
        return {"removed": 0, "freed_mb": 0.0, "reason": "iterdir_failed"}

    for entry in entries:
        # 保留: 非 aggressive 时, 本次进程写的活跃目录(依据 pid 或近期 mtime)留到 atexit
        try:
            mt = entry.stat().st_mtime
        except OSError:
            continue
        if mt >= age_cut and not aggressive:
            continue
        size = _dir_size_bytes(entry) if entry.is_dir() else (entry.stat().st_size if entry.is_file() else 0)
        try:
            if entry.is_dir():
                import shutil
                shutil.rmtree(str(entry), ignore_errors=False)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
            freed += size
        except Exception:
            # 被占用/权限 → 跳过,绝不硬删
            continue

    # Step 2: 容量收敛, 若仍超 max_size_mb,按 mtime 从旧到新删直到低于阈值
    try:
        total = _dir_size_bytes(tr)
    except Exception:
        total = 0
    max_bytes = max(256, max_size_mb) * 1048576
    if total > max_bytes:
        # 重新枚举, 按 mtime 升序(旧→新)
        try:
            entries = list(tr.iterdir())
            entries.sort(key=lambda e: e.stat().st_mtime)
        except Exception:
            entries = []
        for entry in entries:
            if total <= max_bytes:
                break
            size = _dir_size_bytes(entry) if entry.is_dir() else (entry.stat().st_size if entry.is_file() else 0)
            try:
                if entry.is_dir():
                    import shutil
                    shutil.rmtree(str(entry), ignore_errors=False)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
                freed += size
                total -= size
            except Exception:
                continue

    return {"removed": removed, "freed_mb": round(freed / 1048576, 2)}


_CLEANUP_REGISTERED = False


def _register_temp_cleanup() -> None:
    """首次读取 paths 时调用: ①把 Python tempfile.tempdir 绑到 temp_root ②启动清理一次 ③注册 atexit 再清一次。"""
    global _CLEANUP_REGISTERED
    if _CLEANUP_REGISTERED:
        return
    _CLEANUP_REGISTERED = True
    # 把整个进程后续所有 tempfile.* (TemporaryDirectory / mkdtemp / NamedTemporaryFile ...)
    # 一律重定向到项目专属 temp_root, 不再在系统盘 %TEMP% 产生垃圾
    try:
        tr = temp_root()
        tempfile.tempdir = str(tr)
    except Exception:
        pass
    # 启动时的"过期+容量"清理(温和,不删近期活跃条目)
    try:
        _cleanup_temp()
    except Exception:
        pass
    # atexit 时 aggressive 清理:过期阈值降到 0h + 容量收敛
    def _atexit_cleanup():
        try:
            _cleanup_temp(max_age_h=0, aggressive=True)
        except Exception:
            pass
    try:
        atexit.register(_atexit_cleanup)
    except Exception:
        pass
