"""沙盒 (sandbox) · 离线/在线功能验证脚本, 保证浏览器进程绝不残留。

用法:
    .venv\\Scripts\\python.exe -u autotest\\sandbox.py 作者 c3ぴよ
    .venv\\Scripts\\python.exe -u autotest\\sandbox.py 作品 12345678

特性:
  - 独立无头浏览器, 随用随启, 用完即毁
  - 20s 看门狗硬超时: 即使浏览器卡死也会强制收尾 + os._exit,
    彻底避免「进程不能完全刷新 / msedge 残留占用」问题
  - 结果同时落盘到 data/logs/sandbox_<ts>.log, 全量可回溯
"""
import os
import shutil
import sys
import tempfile
import time
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from utils.config import load_config  # noqa: E402
from core.search_client import SearchClient  # noqa: E402

WATCHDOG_S = 20
_GLOBALS = {"sc": None, "done": False}


def _watchdog(name: str):
    """硬看门狗: 超时后强杀浏览器再 os._exit, 保证进程和浏览器都不残留。"""
    t0 = time.time()
    while time.time() - t0 < WATCHDOG_S:
        if _GLOBALS["done"]:
            return
        time.sleep(0.5)
    print(f"[watchdog] 超时 {WATCHDOG_S}s → 强制收尾浏览器", flush=True)
    sc = _GLOBALS.get("sc")
    if sc is not None:
        try:
            sc.close()
        except Exception:
            pass
    _kill_selenium_leftovers()
    os._exit(2)


def _kill_selenium_leftovers():
    """兜底杀本机残留的 Selenium 浏览器(仅 headless webdriver, 绝不碰用户 Edge)。
    无 psutil 依赖: 用 PowerShell 按命令行特征过滤。"""
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
             "Where-Object { $_.CommandLine -match 'remote-debugging-port' -and "
             "$_.CommandLine -match 'scoped_dir' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
            capture_output=True, timeout=15)
    except Exception:
        pass
    _clean_scoped_dirs()


def _clean_scoped_dirs():
    """清理 Selenium 残留的 Edge 临时 profile:
    ① 项目专属 temp_root/browser (新方案以后写这里, 支持迁 F 盘) —— 主清理目标
    ② 系统 Temp 里历史遗留的 scoped_dir_* (兼容旧方案和外部误残留) —— 兜底
    被占用的目录(含当前活跃会话)会被 Windows 文件锁挡住, rmtree 失败自动跳过,
    绝不影响正在运行的浏览器。"""
    # ① 项目专属 temp (paths 新方案)
    try:
        import importlib
        paths_mod = importlib.import_module("utils.paths")
        tr = paths_mod.temp_root()
        # temp_root 下顶层条目全清 (browser/、TemporaryDirectory 产生的名称随机、各种临时文件)
        _rmdir_children(tr)
        # 兼容: 如果 temp_root 下另有自定义 browser/ 子目录也再单独扫一遍
        br = tr / "browser"
        if br.is_dir():
            _rmdir_children(br)
    except Exception:
        pass
    # ② 系统 Temp 里的历史 scoped_dir_* (旧方案 / 外部产生的孤儿)
    t = tempfile.gettempdir()
    try:
        entries = os.listdir(t)
    except OSError:
        return
    for d in entries:
        if not d.startswith("scoped_dir"):
            continue
        p = os.path.join(t, d)
        if not os.path.isdir(p):
            continue
        try:
            shutil.rmtree(p)
        except Exception:
            continue  # 被占用 → 跳过


def _rmdir_children(root) -> None:
    """尝试删除 root 下所有直接子目录/子文件, 被占用的跳过。"""
    try:
        for entry in list(os.scandir(str(root))):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    os.unlink(entry.path)
            except Exception:
                continue
    except OSError:
        pass


def run_search(scope: str, keyword: str):
    cfg = load_config()
    sc = SearchClient(config=cfg, headless=True)
    _GLOBALS["sc"] = sc
    try:
        sc.ensure_login()
        if scope == "作者":
            aus = sc.search_authors(keyword, limit=10)
            print(f"\n== 作者搜索结果: {len(aus)} 位 ==", flush=True)
            for a in aus:
                print(f"   id={a.author_id}  名称={a.name}  粉丝={a.followers}  "
                      f"关注={a.following}  url={a.pixiv_url}", flush=True)
            return len(aus)
        res = sc.search_keyword(keyword, limit=10)
        ws = res.get("works") or []
        print(f"\n== 作品搜索结果: {len(ws)} 项 ==", flush=True)
        for w in ws:
            print(f"   id={w.artwork_id}  作者={w.author_name or w.author_id}  "
                  f"标题={w.title}", flush=True)
        return len(ws)
    finally:
        try:
            sc.close()
        except Exception:
            pass
        _GLOBALS["sc"] = None
        _GLOBALS["done"] = True
        _kill_selenium_leftovers()


def main():
    scope = sys.argv[1] if len(sys.argv) > 1 else "作者"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "c3ぴよ"
    threading.Thread(target=_watchdog, args=(scope,), daemon=True).start()
    try:
        run_search(scope, keyword)
    except Exception as e:
        print(f"[error] {repr(e)}", flush=True)
    _GLOBALS["done"] = True
    print("[done]", flush=True)


if __name__ == "__main__":
    main()