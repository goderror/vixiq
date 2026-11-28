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
import stat
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
    默认 = writable_root/data,也可写死绝对路径放非系统盘。"""
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
    默认 = data_root/temp,也支持 temp_root_override 单独写死绝对路径。"""
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


def browser_temp_root() -> Path:
    """Selenium profile / driver 日志的落点 —— 刻意不默认放进安装目录。

    旧默认是 data/temp/browser。一旦 driver/Edge 没被回收, 残留进程会锁住
    安装目录里的文件, 导致整个 vixiq 文件夹无法删除/改名 (2026-11 实测:
    8 个孤儿 msedge + msedgedriver 锁住 data/temp/browser)。

    现在: 用户显式配了 temp_root_override 就跟随它 (老行为不变); 否则放
    %LOCALAPPDATA%\\vixiq\\browser, 最坏情况也只锁系统盘的临时区。"""
    override = _paths_cfg().get("temp_root_override") or ""
    if override:
        d = Path(os.path.expandvars(override)).expanduser().resolve() / "browser"
    else:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        d = Path(base) / "vixiq" / "browser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource(rel: "str | Path") -> Path:
    """resources/ 下的只读资源路径。rel 形如 'msedgedriver.exe' / 'default.json'"""
    return resource_root() / 'resources' / rel


def ca_bundle() -> "Path | None":
    """随包附带的 CA 根证书包(certifi 的 cacert.pem)。取不到返回 None。"""
    try:
        import certifi
        p = Path(certifi.where())
    except Exception:
        return None
    return p if p.is_file() else None


def prefer_bundled_ca() -> "str | None":
    """把进程的 SSL_CERT_FILE 指向随包 certifi 证书包, 供 urllib / ssl 默认上下文使用。

    Windows 10 消费版 ESU 到期后不再通过 Windows Update 刷新根证书, 系统证书库会一直
    停在内置那几十个; certifi 跟着 pip / 重新打包更新, 根证书更及时。
    外部已显式设置 SSL_CERT_FILE / SSL_CERT_DIR 时不动, 尊重用户自定义。
    返回实际生效的证书包路径, 未启用时返回 None。
    """
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return os.environ.get("SSL_CERT_FILE") or None
    p = ca_bundle()
    if not p:
        return None
    os.environ["SSL_CERT_FILE"] = str(p)
    return str(p)


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


# ---------- 手动清理临时目录: 顶层条目枚举 + 安全删除 ----------
# 手动清理与启动/atexit 的 _cleanup_temp 分开: 前者要逐顶层项报告进度,
# 且必须保证链接/目录联接点只删链接本身, 不碰根外数据。

_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _lstat(p: Path):
    try:
        return os.lstat(str(p))
    except OSError:
        return None


def _lstat_probe(p: Path) -> "tuple[object | None, str | None]":
    """区分「确认不存在」与「读属性失败」。

    返回 (stat, error): 正常 → (st, None); 已不存在 → (None, None);
    权限/其它 OSError → (None, 原因)。删条目时不能把读失败当成已删除。
    """
    try:
        return os.lstat(str(p)), None
    except FileNotFoundError:
        return None, None
    except NotADirectoryError:
        return None, None
    except OSError as e:
        return None, str(e) or type(e).__name__


def _is_reparse_point(p: Path, st=None) -> bool:
    """符号链接 / 目录联接点。用 lstat 属性判断, 不跟随目标。"""
    st = st if st is not None else _lstat(p)
    if st is None:
        return False
    attrs = int(getattr(st, "st_file_attributes", 0) or 0)
    if attrs and (attrs & _FILE_ATTRIBUTE_REPARSE_POINT):
        return True
    try:
        if p.is_symlink():
            return True
    except OSError:
        pass
    try:
        is_j = getattr(p, "is_junction", None)
        if callable(is_j) and is_j():
            return True
    except OSError:
        pass
    return False


def _identity_from_stat(p: Path, st) -> tuple:
    reparse = _is_reparse_point(p, st)
    if reparse:
        kind = "link"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    else:
        kind = "file"
    return (int(getattr(st, "st_dev", 0) or 0),
            int(getattr(st, "st_ino", 0) or 0),
            kind,
            int(getattr(st, "st_reparse_tag", 0) or 0),
            int(getattr(st, "st_size", 0) or 0),
            int(getattr(st, "st_mtime_ns", 0) or 0))


def temp_entry_identity(p: Path) -> "tuple | None":
    """顶层条目身份。目录 mtime/size 随直接子项变化, 用于发现扫描后被替换/新增。"""
    st = _lstat(p)
    if st is None:
        return None
    return _identity_from_stat(p, st)


def list_temp_entries(root: "str | Path") -> "list[dict]":
    """只枚举临时根顶层条目; 根不可读时抛 OSError。

    能枚举到名字但读不到属性的条目也必须进入本轮分母, 标记 error/missing,
    由 remove_temp_entry 报"不可核验/已不存在", 不能静默当作空目录。
    """
    root_p = Path(root)
    out: "list[dict]" = []
    with os.scandir(str(root_p)) as it:
        for e in it:
            p = Path(e.path)
            st, err = _lstat_probe(p)
            if st is None and err is None:
                out.append({"name": e.name, "path": str(p), "kind": "missing",
                            "identity": None, "missing": True})
                continue
            if st is None:
                out.append({"name": e.name, "path": str(p), "kind": "unreadable",
                            "identity": None, "error": err})
                continue
            ident = _identity_from_stat(p, st)
            out.append({"name": e.name, "path": str(p),
                        "kind": ident[2], "identity": ident})
    out.sort(key=lambda x: str(x.get("name") or "").lower())
    return out


def _remove_link(p: Path) -> None:
    """只删链接本身。目录联接点 unlink 会失败, 回退 rmdir (不会删目标内容)。"""
    try:
        os.unlink(str(p))
        return
    except OSError:
        pass
    os.rmdir(str(p))


def _remove_tree(path: Path, errors: "list[str]") -> "tuple[int, bool]":
    """删除普通目录内容, 不跟随任何 reparse point。

    返回 (实际删除的文件字节, 是否真的删过东西)。后者用于区分「本任务删掉了空目录」
    与「目录在扫描后被外部移除」——后者应报已不存在, 不能计入清理数。
    """
    freed = 0
    touched = False
    stack = [(path, False)]
    while stack:
        cur, visited = stack.pop()
        if visited:
            try:
                os.rmdir(str(cur))
                touched = True
            except OSError as e:
                st2, err2 = _lstat_probe(cur)
                if st2 is None and err2 is None:
                    continue
                errors.append(f"{cur.name or cur}: {err2 or e}")
            continue
        st, err = _lstat_probe(cur)
        if err:
            errors.append(f"{cur.name or cur}: 无法读取属性：{err}")
            continue
        if st is None:
            continue
        if _is_reparse_point(cur, st):
            try:
                _remove_link(cur)
                touched = True
            except OSError as e:
                errors.append(f"{cur.name or cur}: {e}")
            continue
        try:
            with os.scandir(str(cur)) as it:
                children = list(it)
        except OSError as e:
            errors.append(f"{cur.name or cur}: {e}")
            continue
        stack.append((cur, True))
        for e in children:
            cp = Path(e.path)
            cst, cerr = _lstat_probe(cp)
            if cerr:
                errors.append(f"{cp.name}: 无法读取属性：{cerr}")
                continue
            if cst is None:
                continue
            if _is_reparse_point(cp, cst):
                try:
                    _remove_link(cp)
                    touched = True
                except OSError as ex:
                    errors.append(f"{cp.name}: {ex}")
                continue
            if stat.S_ISDIR(cst.st_mode):
                stack.append((cp, False))
                continue
            size = int(getattr(cst, "st_size", 0) or 0)
            try:
                os.unlink(str(cp))
                freed += size
                touched = True
            except OSError as ex:
                errors.append(f"{cp.name}: {ex}")
    return freed, touched


def _same_path(a: "str | Path", b: "str | Path") -> bool:
    return (os.path.normcase(os.path.abspath(str(a)))
            == os.path.normcase(os.path.abspath(str(b))))


def remove_temp_entry(entry: dict, root: "str | Path") -> dict:
    """删除一个已枚举的顶层条目。

    返回 ``{outcome: cleaned|skipped|failed, reason, freed}``。
    链接/联接点只删链接本身; 目录失败时 freed 只含实际删除的文件字节。
    读属性失败 → failed 且保留; 外部已移除 → skipped, 不计入清理数。
    """
    p = Path(str((entry or {}).get("path") or ""))
    if not p.name or not _same_path(p.parent, root):
        return {"outcome": "skipped", "reason": "目标已不在临时目录根内",
                "freed": 0}
    if entry.get("error"):
        # 初扫就读不到属性: 名字能枚举但身份不可核验, 不删、不猜
        st_now, err_now = _lstat_probe(p)
        if st_now is None and err_now is None:
            return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
        return {"outcome": "failed",
                "reason": f"无法核验条目身份（已保留）：{entry.get('error')}",
                "freed": 0}
    if entry.get("missing"):
        # 初扫时已不存在, 现在又出现同名项 → 无法确认是不是同一个, 保留
        st_now, err_now = _lstat_probe(p)
        if st_now is None and err_now is None:
            return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
        return {"outcome": "skipped",
                "reason": "扫描后出现同名项，无法确认身份，已保留", "freed": 0}
    st, err = _lstat_probe(p)
    if err:
        return {"outcome": "failed",
                "reason": f"无法读取属性（已保留）：{err}", "freed": 0}
    if st is None:
        return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
    ident = _identity_from_stat(p, st)
    if entry.get("identity") is not None and ident != entry.get("identity"):
        return {"outcome": "skipped", "reason": "处理前已被替换或修改",
                "freed": 0}
    if _is_reparse_point(p, st):
        try:
            _remove_link(p)
            return {"outcome": "cleaned", "reason": "已删除链接，目标数据保留",
                    "freed": 0}
        except FileNotFoundError:
            return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
        except OSError as e:
            return {"outcome": "failed", "reason": f"链接无法删除：{e}",
                    "freed": 0}
    if stat.S_ISDIR(st.st_mode):
        errors: "list[str]" = []
        freed, touched = _remove_tree(p, errors)
        st2, err2 = _lstat_probe(p)
        if st2 is None and err2 is None:
            if errors:
                return {"outcome": "failed",
                        "reason": f"部分文件未能删除：{errors[0]}",
                        "freed": freed}
            if not touched:
                return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
            return {"outcome": "cleaned", "reason": "", "freed": freed}
        if err2:
            return {"outcome": "failed",
                    "reason": f"无法确认目录状态（已保留）：{err2}",
                    "freed": freed}
        try:
            os.rmdir(str(p))
            return {"outcome": "cleaned", "reason": "", "freed": freed}
        except FileNotFoundError:
            if errors:
                return {"outcome": "failed",
                        "reason": f"部分文件未能删除：{errors[0]}",
                        "freed": freed}
            if not touched:
                return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
            return {"outcome": "cleaned", "reason": "", "freed": freed}
        except OSError as e:
            st3, err3 = _lstat_probe(p)
            if st3 is None and err3 is None:
                if errors:
                    return {"outcome": "failed",
                            "reason": f"部分文件未能删除：{errors[0]}",
                            "freed": freed}
                if not touched:
                    return {"outcome": "skipped", "reason": "已不存在",
                            "freed": 0}
                return {"outcome": "cleaned", "reason": "", "freed": freed}
            reason = (f"部分文件未能删除：{errors[0]}" if errors
                      else f"目录无法删除：{e}")
            return {"outcome": "failed", "reason": reason, "freed": freed}
    size = int(getattr(st, "st_size", 0) or 0)
    try:
        os.unlink(str(p))
        return {"outcome": "cleaned", "reason": "", "freed": size}
    except FileNotFoundError:
        return {"outcome": "skipped", "reason": "已不存在", "freed": 0}
    except OSError as e:
        return {"outcome": "failed",
                "reason": f"文件无法删除（可能被占用）：{e}", "freed": 0}


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
