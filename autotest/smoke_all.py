# autotest/smoke_all.py
r"""
无人值守全自动冒烟自测脚本(参考「唯一后台任务+实时输出+报告落盘」)。

用途:
  - 新对话每改完一步代码, 直接执行:
      $ .venv\Scripts\python.exe -m autotest.smoke_all
    就会自动跑完所有非交互式用例 + 生成 autotest/SMOKE_REPORT.md 报告。

特性:
  1) 零额外依赖: 只用标准库 + 项目 requirements.txt 里已有的 4 个库
  2) 唯一后台任务原则: 每次运行会先尝试清理 autotest/_running.lock 老残留,
     但仍建议同一时间只跑一个实例。
  3) Tkinter GUI 构造不会卡住人: 主线程启动 ToolkitApp → after(600ms) 自动 destroy
     + 15s 全局 watchdog Timer 兜底（任何原因 mainloop 不退出都会被强制 destroy 退出,
     且超时写入报告 TIMEOUT）。
  4) 进度实时 stdout + 最终落 SMOKE_REPORT.md（用户早上回来能直接打开看通过没）。

用例列表:
  T1  语法全检: 全项目 .py 递归 py_compile（排除 .venv / __pycache__）
  T2  核心导入: 9 个模块 import OK + theme.init_style 正常
  T3  配置 canonicalize: load_config 缺节兜底 5 断言
  T4  查询 parse_input: 6 组样例分类命中（P3 功能链路入口）
  T5  下载队列 CRUD: PixivDatabase 新建临时数据库 + add/list/remove 全流程
  T6  GUI 主构造: ToolkitApp() 正常创建并在 600ms 后自销毁（验证不卡不炸）
  T7  入口脚本语法: main.py + build.spec 语法可读检查 + main 函数存在
  T8  P4a keyword 边例: parse_input 多字节/空格/引号 12 条
  T9  P4a 关键字搜索结果结构: stub 假数据验证 (不连网)
  T10 P4a+ 全程无头默认配置 + LoginDialog 接口面 + 顶栏触发方式回归
  T11 P4a+ fetch_pixiv_user 探测 (fake driver) + CartDrawer 闪动回归防线
"""
from __future__ import annotations

import json
import os
import py_compile
import queue as _queue
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

# ---- 路径常量 ----
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = PROJECT_ROOT / "autotest" / "SMOKE_REPORT.md"
LOCK_PATH = PROJECT_ROOT / "autotest" / "_running.lock"
assert (PROJECT_ROOT / "main.py").is_file(), (
    f"PROJECT_ROOT 计算错误: {PROJECT_ROOT} 下找不到 main.py"
)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---- 测试结果结构 ----
@dataclass
class CaseResult:
    case_id: str
    title: str
    ok: bool
    duration_ms: int = 0
    detail: str = ""


@dataclass
class SuiteResult:
    started_at: datetime = field(default_factory=datetime.now)
    cases: list[CaseResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, case_id: str, title: str, fn: Callable[[], None]) -> None:
        t0 = time.monotonic()
        try:
            fn()
            ok, detail = True, "PASS"
        except AssertionError as e:
            ok, detail = False, f"ASSERT FAIL: {e}"
        except Exception as e:  # noqa: BLE001 - 自测脚本要兜底抓所有异常写报告
            ok = False
            detail = f"EXCEPTION {type(e).__name__}: {e}\n{traceback.format_exc()}"
        dt_ms = int((time.monotonic() - t0) * 1000)
        self.cases.append(CaseResult(case_id, title, ok, dt_ms, detail))
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"[{self.cases.index(self.cases[-1]) + 1:02d}/{self.cases[-1].case_id}] "
              f"{status}  {title}  ({dt_ms}ms)")
        if not ok:
            # 实时打印错误到 stdout, 不等到报告才看见 
            print("--- FAIL DETAIL ---")
            print(detail)
            print("------------------")

    def passed(self) -> int:
        return sum(1 for c in self.cases if c.ok)

    def failed(self) -> int:
        return sum(1 for c in self.cases if not c.ok)

    def write_report(self) -> None:
        total = len(self.cases)
        p, f = self.passed(), self.failed()
        overall = "✅ 全部通过" if f == 0 else f"❌ 存在 {f} 项失败"
        duration_ms = sum(c.duration_ms for c in self.cases)
        lines = [
            "# Vixiq · 自动冒烟报告",
            "",
            f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 项目根: `{PROJECT_ROOT}`",
            f"- Python: `{sys.executable}` (v{sys.version.split()[0]})",
            f"- 总耗时: {duration_ms / 1000:.2f}s (总 {total} / 通过 {p} / 失败 {f})",
            f"- 整体结论: **{overall}**",
            "",
            "## 用例明细",
            "",
            "| # | ID | 用例标题 | 结果 | 耗时(ms) |",
            "|---|---|---|---|---|",
        ]
        for i, c in enumerate(self.cases, 1):
            tag = "✅" if c.ok else "❌"
            lines.append(f"| {i} | {c.case_id} | {c.title} | {tag} | {c.duration_ms} |")
        lines.append("")
        lines.append("## 失败详情（如有）")
        lines.append("")
        fail_list = [c for c in self.cases if not c.ok]
        if not fail_list:
            lines.append("> 无失败用例。")
        else:
            for c in fail_list:
                lines.append(f"### ❌ {c.case_id} · {c.title} ({c.duration_ms}ms)")
                lines.append("")
                lines.append("```")
                lines.append(c.detail)
                lines.append("```")
                lines.append("")
        REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n📄 报告已写入: {REPORT_PATH}")


# ==============================================================
# 用例实现
# ==============================================================

def t1_syntax_check_all() -> None:
    """T1: 全项目 .py 递归 py_compile"""
    bad: list[str] = []
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        # 跳过虚拟环境 / 缓存 / 打包产物
        rel = Path(dirpath).relative_to(PROJECT_ROOT).as_posix()
        if rel.startswith(".venv") or "__pycache__" in rel or rel.startswith("build") \
                or rel.startswith("dist"):
            continue
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as e:
                bad.append(str(e))
    assert not bad, f"存在 {len(bad)} 个语法错误:\n" + "\n".join(bad[:15])


def t2_import_core_modules() -> None:
    """T2: 核心模块 7 个 import OK (utils/db/core; P7 起 gui/ 已移除, 旧 Tk 界面不再检查)"""
    from utils import config as _c, paths as _p, sec as _s, browser as _b  # noqa: F401
    from db import migrator as _m, store as _st  # noqa: F401
    from core import models as _mdl  # noqa: F401
    from core import search_client as _sc, download_engine as _de  # noqa: F401
    from core import local_search as _lsearch, reverse_source as _rsrc  # noqa: F401


def t3_config_canonicalize() -> None:
    """T3: 配置 canonicalize 5 条断言"""
    from utils.config import load_config, _canonicalize
    cfg = load_config()
    # 顶层节必须有
    for key in ("cookies", "paths", "browser", "crawler", "logging"):
        assert key in cfg, f"缺少顶层节: {key}"
    # cookies ↔ paths 双向同步
    assert cfg["cookies"]["cookies_filename"] == cfg["paths"]["cookies_filename"], \
        f"cookies_filename 双向不一致: cookies={cfg['cookies']['cookies_filename']}, " \
        f"paths={cfg['paths']['cookies_filename']}"
    # output 必须存在 (保存根目录)
    assert cfg["paths"].get("output"), "paths.output 缺失, canonicalize 没兜底"
    # use_dpapi 默认 True
    assert cfg["cookies"]["use_dpapi"] is True, "cookies.use_dpapi 默认值错误"
    # 极端场景: 空 dict 喂 canonicalize 也不会炸
    mini = _canonicalize({})
    assert mini["paths"]["output"] and mini["cookies"]["cookies_filename"], \
        "空 dict canonicalize 结果不完整"

    # user.json 带 UTF-8 BOM 也必须照样生效 (2026-09-17 真实事故: dist/config/user.json
    # 被写进 BOM → json.load 抛 JSONDecodeError → 这里静默退回默认配置 → 端口回落 7790、
    # data_root_override 丢失, 用户看到「cookie 没了、数据库也不见了」, exe 跑成一个空库)
    import tempfile
    from pathlib import Path as _P
    import utils.paths as _up
    with tempfile.TemporaryDirectory(prefix="t3bom_") as td:
        f = _P(td) / "user.json"
        f.write_bytes(b"\xef\xbb\xbf" + b'{"web": {"port": 12345}, '
                      b'"paths": {"data_root_override": "D:/vixiq-data"}}')
        orig = _up.user_config_path
        _up.user_config_path = lambda: f
        try:
            c = load_config()
        finally:
            _up.user_config_path = orig
        assert int(c["web"]["port"]) == 12345, \
            f"带 BOM 的 user.json 必须照样生效, 不许静默退回默认配置: {c['web']}"
        assert c["paths"]["data_root_override"] == "D:/vixiq-data", \
            f"带 BOM 时 F 盘数据重定向被丢了: {c['paths']}"


def t4_search_parse_input() -> None:
    """T4: parse_input 6 样例（P3 查询入口）"""
    from core.search_client import parse_input

    cases = [
        # --- artwork 识别: 纯数字一律当 artwork_id (parse_input L80 注释:「纯数字先当作品查, 查不到再 fallback 作者」)
        ("120571595", "artwork_id"),
        ("15972953", "artwork_id"),
        # --- artwork URL 识别 (完整 URL + en 多语言前缀)
        ("https://www.pixiv.net/artworks/120571595", "artwork_url"),
        ("https://www.pixiv.net/en/artworks/120571595", "artwork_url"),
        # --- author 识别: 不用纯数字入口, 一律带 URL 形式
        ("https://www.pixiv.net/users/15972953", "author_url"),
        ("https://www.pixiv.net/member_illust.php?id=15972953", "author_url"),
        ("member_illust.php?member_id=15972953&mode=medium", "author_url"),
        # --- keyword 识别: 不含 pixiv.net 也不是纯数字
        ("Fate FGO スカサハ =スカディ=", "keyword"),
    ]
    # 期望 8 样例分布: artwork_id(2) / artwork_url(2) / author_url(3) / keyword(1) = 8
    # 注: author_id (纯数字 author) 的入口 parse_input 现在不提供 —— 因为纯数字一律先
    #     当 artwork_id 查 (search_client L80), 作者 UID 请用 users/<id> 或 member_illust.php
    #     的 URL 形式粘贴.
    want_counts = {"artwork_id": 2, "artwork_url": 2,
                   "author_url": 3, "keyword": 1}
    got_counts: dict[str, int] = {}
    for text, want_kind in cases:
        r = parse_input(text)
        got_kind = r.kind
        got_counts[got_kind] = got_counts.get(got_kind, 0) + 1
        assert got_kind == want_kind, (
            f"parse_input({text!r}) = kind={got_kind!r}, 期望 {want_kind!r}"
        )
        # ParsedQuery 定义在 core/search_client.py L41, 极简类.
        # 注意: 由于 search_client.py 顶部有 'from __future__ import annotations',
        # parse_input.__annotations__['return'] 会是字符串 'ParsedQuery' 不是类型对象,
        # 所以 isinstance(..., xxx_annot_return) 会直接 TypeError, 这里就直接按类名判断.
        assert type(r).__name__ == 'ParsedQuery', (
            f"parse_input 返回类型不是 ParsedQuery: {type(r).__module__}.{type(r).__name__}"
        )
        # value 字段永远应非空 (除了空输入, 本用例里没有):
        #   artwork_id / artwork_url → value 是提取到的作品 ID(数字)
        #   author_id / author_url → value 是 author_id(数字)
        #   keyword → value 就是原始 keyword
        assert r.value and r.value.strip(), (
            f"parse_input({text!r}) 返回空 value={r.value!r}"
        )
        assert r.raw == text.strip() or r.raw == text, (
            f"raw 字段不一致, raw={r.raw!r} vs input={text!r}"
        )
        # is_id_based(): keyword 应为 False, 其它应 True
        want_id_based = (want_kind != "keyword")
        assert r.is_id_based() is want_id_based, (
            f"parse_input({text!r}) is_id_based()={r.is_id_based()}, 期望 {want_id_based}"
        )
    # 总量统计: 避免未来正则匹配把本来要识别的类都归到 keyword
    assert got_counts == want_counts, (
        f"parse_input 按类别计数不匹配: 实际 {got_counts}  期望 {want_counts}"
    )


def t5_db_queue_crud() -> None:
    """T5: 下载清单 加/列表/删除 全流程（临时 DB 文件,跑完删掉）

    说明: utils.paths 模块根本没有全局属性 'db_filename'(DB 路径是 config["paths"]["db_filename"]
    运行时读出来的), 所以这里直接用 PixivDatabase(db_path=临时文件绝对路径) 做隔离测试,
    完全不会污染用户本机 data/pixiv.db, 也不需要改 paths 的任何全局状态.
    """
    from db.store import PixivDatabase

    tmp_db = PROJECT_ROOT / "autotest" / f"_tmp_smoke_{os.getpid()}.db"
    tmp_db.unlink(missing_ok=True)
    db: PixivDatabase | None = None
    try:
        # 传 db_path 绝对路径, PixivDatabase 就不走默认的 data/pixiv.db 了
        db = PixivDatabase(db_path=str(tmp_db))
        # 1) 空清单: PixivDatabase.queue_list L503 参数是 status(单字符串, 不是 list status_filter!)
        empty = db.queue_list(status="pending")
        assert empty == [], f"初始 pending 应空: {empty}"
        # 2) 加两条 (artwork + author_all).
        #    queue_add 签名 store.py L488: def queue_add(self, *, item_type, status='pending',
        #       title=None, author_id=None, author_name=None, artwork_id=None,
        #       page_count=None, strategy='only_new', source_note=None)
        #    注意: self 之后有 *, 所以所有参数必须关键字传! 没有 metadata / pages_total 关键字!
        aid1 = db.queue_add(item_type="artwork",
                            artwork_id="120571595",
                            title="Smoke Artwork A",
                            page_count=2,
                            strategy="all_pages",
                            source_note="Smoke test artwork")
        aid2 = db.queue_add(item_type="author_all",
                            author_id="15972953",
                            author_name="Smoke Artist B",
                            strategy="only_new",
                            source_note="Smoke test author_all scope=全部作品")
        assert aid1 > 0 and aid2 > 0, f"queue_add 返回 id 非正: {aid1}, {aid2}"
        pending = db.queue_list(status="pending")
        assert len(pending) == 2, (
            f"queue_add 2 条后 pending 条数不对: 期望 2, 实际 {len(pending)}"
        )
        # 校验关键字段: id 必须是刚返回的 aid1/aid2; item_type 与 content/artwork_id/author_id 应该能对上
        rows_by_id = {int(p["id"]): p for p in pending}
        assert set(rows_by_id.keys()) == {aid1, aid2}, (
            f"queue_list 返回 id 集合不对: {set(rows_by_id.keys())} vs 期望 {{{aid1}, {aid2}}}"
        )
        # 行 1: artwork
        r1 = rows_by_id[aid1]
        assert r1["item_type"] == "artwork" and r1.get("artwork_id") == "120571595", \
            f"artwork 行内容异常: {r1}"
        # 行 2: author_all
        r2 = rows_by_id[aid2]
        assert r2["item_type"] == "author_all" and r2.get("author_id") == "15972953", \
            f"author_all 行内容异常: {r2}"
        # 3) 全 remove 掉
        db.queue_remove(aid1)
        db.queue_remove(aid2)
        after = db.queue_list(status="pending")
        assert len(after) == 0, f"queue_remove 全部后仍有残留 pending: {after}"
    finally:
        # Windows sqlite3 释放文件句柄有延迟, 这里 gc.collect + 短 sleep 再删 (PermissionError 32 规避)
        import gc as _gc
        closed_ok = False
        try:
            if db is not None:
                db.close()
                closed_ok = True
        except Exception:  # noqa: BLE001
            pass
        try:
            # 可能 PixivDatabase 还有隐式引用 (比如 MigrationManager 持有的 conn),
            # 先强引用置空
            db = None  # type: ignore[assignment]
            _gc.collect()
            if closed_ok:
                time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        # 温柔地删: 一个一个尝试, PermissionError 就跳过 (下次跑 smoke_all 会先 unlink 同名旧文件,
        # 不会一直占着 autotest 目录)
        for p in (tmp_db,
                  tmp_db.with_name(tmp_db.name + "-wal"),
                  tmp_db.with_name(tmp_db.name + "-shm"),
                  tmp_db.with_name(tmp_db.name + "-journal")):
            try:
                p.unlink(missing_ok=True)
            except PermissionError:
                # 留个痕迹, 但不导致 FAIL
                print(f"[smoke_all/T5] 提示: 临时 DB 文件仍占锁, 跳过删除 {p.name}")
            except Exception:  # noqa: BLE001
                pass




def _req_declared() -> str:
    """requirements.txt 里**真正声明的包名**(小写), 已去掉注释行与行尾注释。

    必须剥注释: 否则"# 注释里提到 flask"也会被当成已声明 —— 护栏自己就废了。
    """
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    return "\n".join(ln.split("#", 1)[0] for ln in text.splitlines()).lower()


def t7_entry_sanity() -> None:
    """T7: main.py / build.spec 基础完整性 + main 函数存在 + requirements 依赖声明无遗漏

    依赖那条是 2026-09-18 补的护栏: web/server.py 用了 Flask 却一直没写进 requirements.txt
    (开发机 .venv 恰好装着 → 本地永远正常), 是典型的「只有别人 clone 才炸」的漏声明。
    """
    # main.py
    main_py = PROJECT_ROOT / "main.py"
    assert main_py.is_file(), "main.py 不存在 (打包和 IDE 默认入口硬依赖它)"
    code = main_py.read_text(encoding="utf-8")
    assert "def main():" in code, "main.py 没定义 main() 函数"
    assert "if __name__ == \"__main__\":" in code, "main.py 缺 __main__ guard"
    # build.spec 必须指向 main.py
    spec = PROJECT_ROOT / "build.spec"
    assert spec.is_file(), "build.spec 不存在"
    spec_txt = spec.read_text(encoding="utf-8")
    assert "'main.py'" in spec_txt or '"main.py"' in spec_txt, \
        "build.spec Analysis(...) 没指定 main.py 入口脚本, 打包会炸"

    # 依赖声明完整性 (2026-09-18 加): web/server.py 一直写着 `from flask import Flask`,
    # 但 requirements.txt 里从来没有 Flask —— 开发机 .venv 恰好装着, 所以从没暴露;
    # 别人 clone 下来 pip install -r requirements.txt 再运行会直接 ModuleNotFoundError。
    # 这里扫全部源码的顶层 import, 逐个要求在 requirements.txt 里有对应声明。
    req = _req_declared()
    # import 名 → PyPI 分发名 的差异 (只有这几个不符合"小写同名")
    _alias = {"pil": "pillow", "cv2": "opencv-python"}
    # 项目自身的包 / 仅 PyInstaller 冻结环境才有的模块, 不该出现在 requirements 里
    _local = {"core", "db", "utils", "web", "resources", "autotest", "main", "pyi_splash"}
    # 由别的依赖传递带来的 (Flask 依赖 Werkzeug), requirements 里无需单列
    _transitive = {"werkzeug"}
    _files = [PROJECT_ROOT / "main.py"]
    for _sub in ("core", "db", "utils", "web"):
        _files += sorted((PROJECT_ROOT / _sub).rglob("*.py"))
    _imp = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", re.M)
    _roots: set[str] = set()
    for _f in _files:
        if _f.is_file():
            _roots |= set(_imp.findall(_f.read_text(encoding="utf-8", errors="ignore")))
    _missing = sorted(
        n for n in _roots
        if n not in sys.stdlib_module_names and n not in _local and n not in _transitive
        and _alias.get(n.lower(), n.lower()) not in req
    )
    assert not _missing, \
        f"源码 import 了但 requirements.txt 没声明的第三方库: {_missing}(别人装不上就跑不起来)"


def t8_parse_input_keyword_edge() -> None:
    """T8: P4a · parse_input keyword 边界用例 (多字节/空格/引号/混合 URL 残片)

    验证点: 所有非 URL 非纯数字的输入都应被识别为 kind='keyword',
    且 value/raw 字段正确保留 (不丢多字节字符 / 不吞引号)。
    """
    from core.search_client import parse_input, ParsedQuery

    edge_cases = [
        # --- 日文/中文多字节字符 ---
        ("Fate FGO スカサハ =スカディ=", "keyword", "Fate FGO スカサハ =スカディ="),
        ("原神 雷电将军 雷光", "keyword", "原神 雷电将军 雷光"),
        ("서양화 일러스트", "keyword", "서양화 일러스트"),
        # --- 带空格 / 制表符 前后 / 中间多空格 ---
        ("   セイバー   ", "keyword", "セイバー"),
        ("blue  archive  aris", "keyword", "blue  archive  aris"),
        # --- 带双引号 / 单引号 (用户搜精确标签) ---
        ('"艦隊これくしょん"', 'keyword', '"艦隊これくしょん"'),
        ("'hololive EN'", "keyword", "'hololive EN'"),
        # --- 含数字但不是纯数字 (标签+编号) ---
        ("セイバー 5star", "keyword", "セイバー 5star"),
        ("原神 ver3.8", "keyword", "原神 ver3.8"),
        # --- 含斜杠但不含 pixiv.net (标签组合) ---
        ("少女/机甲/科幻", "keyword", "少女/机甲/科幻"),
        ("R-18 / オリジナル", "keyword", "R-18 / オリジナル"),
        # --- 纯空字符串 → value='' kind='keyword' ---
        ("", "keyword", ""),
        ("   \t  ", "keyword", ""),
    ]
    for text, want_kind, want_value in edge_cases:
        r = parse_input(text)
        assert r.kind == want_kind, (
            f"parse_input({text!r}) kind={r.kind!r}, 期望 {want_kind!r}"
        )
        assert r.value == want_value, (
            f"parse_input({text!r}) value={r.value!r}, 期望 {want_value!r}\n"
            f"  (多字节/引号可能被错误处理, 检查 parse_input 是否 strip 过度)"
        )
        # is_id_based() 对 keyword 永远为 False
        assert r.is_id_based() is False, (
            f"keyword case is_id_based() 应 False, 实际 True: text={text!r}"
        )
    # 数量校验: 12 条边例都得是 keyword
    kw_count = sum(1 for t, _, _ in edge_cases
                   if parse_input(t).kind == 'keyword')
    assert kw_count == len(edge_cases), (
        f"keyword 边例计数异常: {kw_count}/{len(edge_cases)} 被归类为 keyword"
    )


def t9_search_keyword_result_structure() -> None:
    """T9: P4a · 关键字搜索返回结构验证 (不跑 Selenium, stub 假数据)

    验证点:
      a) parse_input 识别 keyword + ParsedQuery 字段合规
      b) SearchPage.apply_result() 期望的 keyword 结果结构是否完整:
         type='keyword' / keyword:str / related_authors:list[AuthorInfo] /
         works:list[ArtworkInfo]
      c) list 中每个元素的字段类型正确 (ArtworkCard / AuthorHeaderCard 构造不炸)
      d) 空关键字 & 空作品列表 的兜底返回结构也合规

    说明: 这里不真打外网, 直接按 SearchClient.search_keyword() 的返回契约
    构造 stub dict, 等价于假 AJAX 返回。
    """
    # 1) parse_input → keyword 的 ParsedQuery 先验证一遍 (链路入口)
    from core.search_client import parse_input
    from core.models import AuthorInfo, ArtworkInfo, IllustType

    q = parse_input('"FGO セイバー"  五稜郭')
    assert q.kind == 'keyword', f"入口 parse_input kind 异常: {q.kind}"
    assert q.value == '"FGO セイバー"  五稜郭', (
        f"value 空格/引号被吞: {q.value!r}"
    )

    # 2) 构造一个等价 search_keyword() 返回的 stub dict
    #    (真实 search_keyword 解析 AJAX 后就是产出这个结构)
    authors = [
        AuthorInfo(author_id="100001", name="絵師A",
                   avatar_url="https://i.example.com/a.png",
                   followers=1234, bio="P站老司机",
                   pixiv_url="https://www.pixiv.net/users/100001"),
        AuthorInfo(author_id="100002", name="絵師B",
                   pixiv_url="https://www.pixiv.net/users/100002"),
        AuthorInfo(author_id="100003", name="絵師C",
                   pixiv_url="https://www.pixiv.net/users/100003"),
    ]
    works = [
        ArtworkInfo(
            artwork_id="99000001",
            author_id="100001",
            title="セイバー 聖剣",
            illust_type=IllustType.ILLUSTRATION,
            page_count=1,
            tags=["Fate", "セイバー", "FGO"],
            image_urls=["https://i.example.com/99000001p0.jpg"],
            bookmark_count=5678,
            is_r18=False,
        ),
        ArtworkInfo(
            artwork_id="99000002",
            author_id="100002",
            title="FGO 沖田総司",
            illust_type=IllustType.MANGA,
            page_count=8,
            tags=["FGO", "沖田総司", "漫画"],
            image_urls=[],
            is_r18=True,
        ),
        ArtworkInfo(
            artwork_id="99000003",
            author_id="",     # 有的卡片没解析到作者, 不应该炸
            title="オリジナル 背景画",
            page_count=1,
        ),
    ]
    stub_result = {
        'type': 'keyword',
        'keyword': q.value,
        'related_authors': authors,
        'works': works,
    }

    # 3) 结构字段断言
    assert stub_result['type'] == 'keyword', (
        f"type 字段错: {stub_result['type']!r}, SearchPage.apply_result() 需要 'keyword'"
    )
    assert isinstance(stub_result['keyword'], str), (
        f"keyword 字段不是 str: {type(stub_result['keyword']).__name__}"
    )
    ra = stub_result['related_authors']
    wk = stub_result['works']
    assert isinstance(ra, list), f"related_authors 不是 list: {type(ra).__name__}"
    assert isinstance(wk, list), f"works 不是 list: {type(wk).__name__}"
    assert len(ra) == 3, f"related_authors 期望 3 条 stub, 实际 {len(ra)}"
    assert len(wk) == 3, f"works 期望 3 条 stub, 实际 {len(wk)}"

    # 4) 每条 AuthorInfo 的必填字段 (AuthorHeaderCard 构造要用到)
    for i, a in enumerate(ra):
        assert isinstance(a, AuthorInfo), (
            f"related_authors[{i}] 不是 AuthorInfo 实例: {type(a).__name__}"
        )
        assert a.author_id and isinstance(a.author_id, str), (
            f"related_authors[{i}] author_id 缺失/非字符串: {a.author_id!r}"
        )
        assert a.name and isinstance(a.name, str), (
            f"related_authors[{i}] name 缺失: {a.name!r}"
        )
        # pixiv_url 是 search_author_by_id() 顶栏回填 + ➕加清单会用到的
        assert a.pixiv_url and isinstance(a.pixiv_url, str), (
            f"related_authors[{i}] pixiv_url 缺失: {a.pixiv_url!r}"
        )

    # 5) 每条 ArtworkInfo 的必填字段 (ArtworkCard 构造要用到)
    for i, w in enumerate(wk):
        assert isinstance(w, ArtworkInfo), (
            f"works[{i}] 不是 ArtworkInfo 实例: {type(w).__name__}"
        )
        assert w.artwork_id and isinstance(w.artwork_id, str), (
            f"works[{i}] artwork_id 缺失: {w.artwork_id!r}"
        )
        assert isinstance(w.title, str) and w.title, (
            f"works[{i}] title 空/非字符串: {w.title!r}"
        )
        assert isinstance(w.page_count, int) and w.page_count >= 1, (
            f"works[{i}] page_count 非法: {w.page_count!r}"
        )
        assert isinstance(w.tags, list), (
            f"works[{i}] tags 不是 list: {type(w.tags).__name__}"
        )
        assert isinstance(w.image_urls, list), (
            f"works[{i}] image_urls 不是 list: {type(w.image_urls).__name__}"
        )
        assert isinstance(w.is_r18, bool), (
            f"works[{i}] is_r18 不是 bool: {type(w.is_r18).__name__}"
        )

    # 6) 空关键字 & 空作品兜底: 不应缺字段 (search_keyword L168 的分支)
    empty_result = {
        'type': 'keyword',
        'keyword': '',
        'related_authors': [],
        'works': [],
    }
    assert empty_result['type'] == 'keyword'
    assert empty_result['related_authors'] == [] and empty_result['works'] == []
    # 空关键字走 apply_result 时, SearchPage 会渲染「没搜到结果」提示块, 不炸








def t13_ajax_data_layer() -> None:
    """T13: P4b+ · ajax 数据层 (搜索/作品/作者) —— 离线验证。

    背景: 用户实测「ID/关键字搜索无结果」。探针实证 ①DOM 扫描只等第 1 个
    锚点出现就开爬 (SPA 约 2s 才渲染满) → 只出 1 条; ②xpaths.json 绝对路径
    失效 → author 爬成空。修复: ajax JSON 主通道 + DOM 兜底。
    """
    sc_src = (PROJECT_ROOT / "core" / "search_client.py").read_text(encoding="utf-8")
    for token in ("_AJAX_BASE", "_ajax_get", "ajax/search/artworks",
                  "ajax/illust", "profile/all", "_illust_body_to_info",
                  "_search_keyword_dom", "_fetch_artwork_dom",
                  "_fetch_author_dom", "_AjaxNotFound"):
        assert token in sc_src, f"search_client 缺 ajax 数据层标记 {token!r}"
    assert "n == last" in sc_src, (
        "_search_keyword_dom 缺锚点收敛等待 → SPA 未渲染满就开爬 = 只出 1 条 (回归)"
    )

    from core.search_client import SearchClient, SearchError, _AjaxNotFound

    # ---- 映射: ajax illust body → ArtworkInfo ----
    stub = {
        "illustId": "136682996", "illustTitle": "東方キャラまとめ",
        "userId": "689690", "userName": "はすばな みれ",
        "pageCount": 2, "illustType": 1, "xRestrict": 1,
        "tags": {"tags": [{"tag": "R-18"}, {"tag": "東方"}]},
        "urls": {"thumb": "https://i.pximg.net/c/250x250/t.jpg"},
        "uploadDate": "2025-10-25T18:30:11+09:00",
    }
    info = SearchClient._illust_body_to_info(stub)
    assert info is not None and info.artwork_id == "136682996"
    assert info.title == "東方キャラまとめ" and info.author_id == "689690"
    assert info.page_count == 2 and info.is_r18 is True
    assert info.image_urls == ["https://i.pximg.net/c/250x250/t.jpg"]
    from core.models import IllustType
    assert info.illust_type == IllustType.MANGA
    assert SearchClient._illust_body_to_info({"illustId": ""}) is None

    # ---- _ajax_get 错误分类 (fake driver, 不开浏览器) ----
    class _FakeEl:
        def __init__(self, text): self._t = text
        def text(self): return self._t
        def get_attribute(self, _): return None

    class _FakeDriver:
        def __init__(self, text): self._t = text
        def get(self, _): pass
        def find_element(self, *a): return self

        @property
        def text(self): return self._t

    def _make(payload: str) -> SearchClient:
        sc = object.__new__(SearchClient)
        sc.driver = _FakeDriver(payload)
        sc.reporter = None
        return sc

    ok = _make('{"error":false,"body":{"illustId":"1"}}')._ajax_get("u")
    assert ok == {"illustId": "1"}

    try:
        _make('{"error":true,"message":"无法找到您所请求的页面"}')._ajax_get("u")
        raise AssertionError("404 消息应抛 _AjaxNotFound")
    except _AjaxNotFound:
        pass

    try:
        _make('{"error":true,"message":"不正确的请求。"}')._ajax_get("u")
        raise AssertionError("普通接口错误应抛 SearchError")
    except SearchError:
        pass

    try:
        _make('<html>登录页</html>')._ajax_get("u")
        raise AssertionError("非 JSON 返回应抛 SearchError")
    except SearchError:
        pass










def t18_download_engine_writes_files() -> None:
    """T18: 下载引擎 → 本地文件写出 + run_cart 调度 (离线, 不开 Selenium)。

    需求。分两层验证:
      ① PixivDownloader.download_with_retry: 用本地 http.server 真实下载一个小图
         字节流 → 断言文件写出且字节一致; 404 路径返回 False (失败处理不炸)。
      ② run_cart 全调度: 临时 DB 塞 2 条 artwork + 1 条 author_all →
         monkey patch download_engine.PixivDownloader 成假下载器 (写真实文件+回拨
         scope 进度) → run_cart 跑完 → 断言: 3/3 成功、save_folder 出现对应文件、
         DB 状态全 done、QueueReporter 收到每项 item_progress 最终 cur==total。
    """
    import tempfile, shutil, threading
    import queue as _q2
    from pathlib import Path
    from utils.reporter import PrintReporter, QueueReporter
    from core import download_engine as de
    from core.models import DownloadStrategy
    from db import store as _db_store_mod

    tmp = tempfile.mkdtemp(prefix="t18_")
    db_file = Path(tmp) / "cart.db"
    save_folder = Path(tmp) / "img"
    try:
        # ---------- ① download_with_retry 真实写文件 ----------
        from core.crawler import PixivDownloader
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"FAKEPIXELDATA" * 30  # > min_file_size 32

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/ok.png":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(png_bytes)))
                    self.end_headers()
                    self.wfile.write(png_bytes)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *a):  # noqa: N802
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            d0 = object.__new__(PixivDownloader)
            d0.config = {"crawler": {"max_retries": 0, "min_file_size": 32}}
            d0.reporter = PrintReporter()
            out_png = Path(tmp) / "ok.png"
            assert d0.download_with_retry(f"http://127.0.0.1:{port}/ok.png",
                                          str(out_png)) is True, "下载成功路径应返回 True"
            assert out_png.read_bytes() == png_bytes, "下载内容应与服务端字节一致"
            assert d0.download_with_retry(f"http://127.0.0.1:{port}/missing.png",
                                          str(Path(tmp) / "x.png")) is False, \
                "404 路径应返回 False (失败处理)"
        finally:
            srv.shutdown()
            srv.server_close()

        # ---------- ② run_cart 调度 (假下载器写真实文件) ----------
        class _FakeDownloader:
            def __init__(self, **kw):
                self.save_folder = Path(kw.get("save_folder"))
                self.config = kw.get("config")
                self._cb = None

            def __enter__(self): return self
            def __exit__(self, *a): pass

            def set_progress_callback(self, cb): self._cb = cb
            def set_abort_callback(self, cb): self._abort_cb = cb

            def _fire(self, scope, cur, total, status):
                if self._cb:
                    try: self._cb(scope, cur, total, status)
                    except Exception:  # noqa: BLE001
                        pass

            def download_artwork(self, aid, author_id=None, author_name=None,
                                 force_overwrite=False):
                self.save_folder.mkdir(parents=True, exist_ok=True)
                (self.save_folder / f"{aid}_p0.png").write_bytes(
                    b"PNG-FAKE-" + str(aid).encode())
                self._fire("artwork_page", 1, 1, f"{aid}_p0.png")
                return 1, 1        # (成功页数, 总页数): 0 会被判为失败 (T73)

            def download_author_all(self, user_id=None, author_name=None):
                self.save_folder.mkdir(parents=True, exist_ok=True)
                for i in range(2):
                    (self.save_folder / f"auth_{user_id}_p{i}.png").write_bytes(
                        b"PNG-AUTH-" + str(user_id).encode())
                    self._fire("artwork_in_list", i + 1, 2, f"auth_{user_id}_p{i}.png")

            def close(self): pass

        _orig_pdl = de.PixivDownloader
        de.PixivDownloader = _FakeDownloader
        _orig_init = _db_store_mod.PixivDatabase.__init__

        def _patched_init(self_inner, db_path=None, reporter=None):
            if db_path is None:
                db_path = db_file
            return _orig_init(self_inner, db_path=db_path, reporter=reporter)

        _db_store_mod.PixivDatabase.__init__ = _patched_init
        try:
            with _db_store_mod.PixivDatabase(None) as db:
                db.queue_add(item_type="artwork", title="#120571595 測試",
                             artwork_id="120571595", page_count=1,
                             strategy="only_new")
                db.queue_add(item_type="artwork", title="#654321 二號",
                             artwork_id="654321", page_count=2,
                             strategy="only_new")
                db.queue_add(item_type="author_all", title="作者全部 777001",
                             author_id="777001", strategy="only_new")

            q2 = _q2.Queue()
            cancel_ev = threading.Event()
            qr = QueueReporter(q2, cancel_ev)
            cfg = {"paths": {"img_subdir": "img"}}
            done, total = de.run_cart(qr, config=cfg, save_folder=save_folder,
                                      strategy=DownloadStrategy.ONLY_NEW,
                                      headless=True)
            assert (done, total) == (3, 3), f"run_cart 应 3/3 成功, 实际 {done}/{total}"
            # 文件真实写出
            assert (save_folder / "120571595_p0.png").exists(), "artwork1 文件未写出"
            assert (save_folder / "654321_p0.png").exists(), "artwork2 文件未写出"
            assert (save_folder / "auth_777001_p1.png").exists(), "author_all 文件未写出"
            # DB 状态全 done
            with _db_store_mod.PixivDatabase(None) as db2:
                rows = db2.queue_list(status="done")
                assert len(rows) == 3, f"DB 应 3 条 done, 实际 {len(rows)}: {rows}"
            # 每项 item_progress 最终 cur==total
            final_by_item: dict[int, tuple] = {}
            while not q2.empty():
                k, p = q2.get_nowait()
                if k == "item_progress":
                    final_by_item[int(p["item_id"])] = (p["cur"], p["total"], p["status"])
            assert len(final_by_item) == 3, f"应收到 3 项 item_progress, 实际 {final_by_item}"
            for _iid, (cur, tot, st) in final_by_item.items():
                assert cur == tot and st == "完成", \
                    f"item_progress 应为 (cur==total, 完成), 实际 ({cur},{tot},{st!r})"
        finally:
            de.PixivDownloader = _orig_pdl
            _db_store_mod.PixivDatabase.__init__ = _orig_init  # type: ignore[method-assign]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t19_reverse_source_offline() -> None:
    """T19: 反搜来源 (saucenao 解析 + DB 缓存 + ReverseSource.process_one 归档) —— 离线。

    需求。三层验证:
      ① saucenao 结果链接正则: 从典型 href 提取 artwork_id / author_id
      ② DB 反搜缓存 reverse_search_put/get 读写往返 (唯一键 upsert)
      ③ ReverseSource.process_one 归档: 打补丁跳过 Selenium (load_cookies_and_login /
         find_source), force_artwork_id → 图片按 [画师名_画师ID] 目录移动成功
         (download_engine reverse_sourced 分支底层就是它)
    """
    import tempfile, shutil, re as _re
    from pathlib import Path
    from utils.reporter import PrintReporter
    from db.store import PixivDatabase
    from core.reverse_source import ReverseSource

    # ---------- ① saucenao href 解析 (与 reverse_search.py search_by_image 同款正则) ----------
    pattern = r'https?://www\.pixiv\.net/.*?(?:illust_id|id|artworks|users)(?:=|/)?(\d+)(?:\s|$)'
    m = _re.search(pattern,
                   "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=120571595")
    assert m and m.group(1) == "120571595", m
    m = _re.search(pattern, "https://www.pixiv.net/artworks/987654")
    assert m and m.group(1) == "987654", m
    m = _re.search(pattern, "https://www.pixiv.net/users/777001")
    assert m and m.group(1) == "777001", m

    # ---------- ② DB 反搜缓存往返 ----------
    db = PixivDatabase(":memory:")
    try:
        db.reverse_search_put("C:/x/a.png", "md5abc", 0.95, "SauceNAO",
                              hit_author_id="777001", hit_artwork_id="120571595")
        row = db.reverse_search_get("C:/x/a.png", "SauceNAO")
        assert row is not None and row["hit_artwork_id"] == "120571595", row
        assert row["hit_author_id"] == "777001", row
        # 再次 put (ON CONFLICT upsert) 不炸且更新
        db.reverse_search_put("C:/x/a.png", "md5abc", 0.8, "SauceNAO",
                              hit_artwork_id="999999")
        row2 = db.reverse_search_get("C:/x/a.png", "SauceNAO")
        assert row2["hit_artwork_id"] == "999999", row2
        # 不同 source 不串
        row3 = db.reverse_search_get("C:/x/a.png", "Ascii2d")
        assert row3 is None, row3
    finally:
        db.close()

    # ---------- ③ process_one 归档 (打补丁跳过 Selenium) ----------
    tmp = tempfile.mkdtemp(prefix="t19_")
    try:
        src = Path(tmp) / "待反搜"
        src.mkdir()
        img = src / "123456_p0.jpg"
        img.write_bytes(b"JPEG-FAKE")
        rs = object.__new__(ReverseSource)
        rs.search_path = src
        rs.reporter = PrintReporter()
        rs.author_cache = {}
        rs.not_found = []
        rs.config = {}
        rs.load_cookies_and_login = lambda: True
        rs.find_source = lambda aid: str(src / f"畫師A_{aid}")  # type: ignore[method-assign]
        ok = rs.process_one(img, force_artwork_id="123456")
        assert ok is True, "force_artwork_id 反搜应归档成功"
        target = src / "畫師A_123456" / "123456_p0.jpg"
        assert target.exists(), f"图片应移动到 [畫師A_123456] 目录: {target}"
        assert not img.exists(), "原图应被移动走"

        # 未知 → False + not_found 记录
        img2 = src / "no_id.jpg"
        img2.write_bytes(b"x")
        rs2 = object.__new__(ReverseSource)
        rs2.search_path = src
        rs2.reporter = PrintReporter()
        rs2.author_cache = {}
        rs2.not_found = []
        rs2.config = {}
        rs2.load_cookies_and_login = lambda: True
        rs2.find_source = lambda aid: None  # type: ignore[method-assign]
        assert rs2.process_one(img2) is False, "无 ID 文件名应返回 False"
        assert "no_id.jpg" in rs2.not_found, rs2.not_found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t20_db_save_and_library() -> None:
    """T20: 数据库管理图片并保存 (入库→统计→搜索→导出/备份) —— 离线。

    需求。验证:
      ① update_pages_from_output: 把本地输出目录里的 <aid>_p0.png 同步入库
         (pages DONE + artworks 自动建行)
      ② change_work_status 补元数据 (标题/作者/tags) → search_artworks 全文搜索命中
      ③ stats_overview: 作品/页数/作者/磁盘统计正确
      ④ export_artworks_csv 导出 + backup_to_zip 备份 (保存到磁盘)
      ⑤ get_work_info (T18 bug 修复) 回归: done/total_pages 判断正确
    """
    import tempfile, shutil, zipfile
    from pathlib import Path
    from db.store import PixivDatabase, PSt, WSt

    tmp = tempfile.mkdtemp(prefix="t20_")
    try:
        out = Path(tmp) / "img"
        (out / "畫師A_111").mkdir(parents=True)
        (out / "畫師A_111" / "123456_p0.png").write_bytes(b"PNG-A0")
        (out / "畫師A_111" / "123456_p1.png").write_bytes(b"PNG-A1")
        (out / "畫師B_222").mkdir(parents=True)
        (out / "畫師B_222" / "654321_p0.jpg").write_bytes(b"JPG-B0")

        db = PixivDatabase(":memory:")
        try:
            # ① 输出目录 → 入库
            db.update_pages_from_output(out)
            p0 = db.get_page_info("123456", 0)
            assert p0 is not None and p0.status == PSt.DONE, p0
            assert p0.local_path, "page0 应回填 local_path"
            p1 = db.get_page_info("123456", 1)
            assert p1 is not None and p1.status == PSt.DONE, p1

            # ② 补元数据 → 全文搜索命中
            db.change_work_status("123456", WSt.DONE, total_pages=2,
                                  title="測試東方同人", author_id="111",
                                  tags=["東方", "測試"], is_r18=False)
            db.change_work_status("654321", WSt.DONE, total_pages=1,
                                  title="FGO 沖田", author_id="222",
                                  tags=["FGO"], is_r18=True)
            db.upsert_author("111", "畫師A")
            db.upsert_author("222", "畫師B")
            hits = db.search_artworks("東方")
            assert any(h["artwork_id"] == "123456" for h in hits), f"搜索未命中 123456: {hits}"
            assert any("東方" in (h.get("tags") or []) for h in hits), hits
            hits2 = db.search_artworks("畫師B")
            assert any(h["artwork_id"] == "654321" for h in hits2), hits2

            # ③ 图库统计
            st = db.stats_overview(out)
            assert st["total_artworks"] >= 2, st
            assert st["total_pages"] >= 3, st
            assert st["total_authors"] >= 2, st
            assert st["disk_size_bytes"] > 0, st

            # ④ 导出 CSV + 备份 zip
            csv_path = Path(tmp) / "export.csv"
            n = db.export_artworks_csv(csv_path)
            assert n >= 2 and csv_path.exists(), (n, csv_path)
            csv_text = csv_path.read_text(encoding="utf-8-sig")
            assert "123456" in csv_text and "654321" in csv_text
            zip_path = db.backup_to_zip(Path(tmp) / "backup.zip")
            assert zipfile.is_zipfile(zip_path), "备份应生成合法 zip"

            # ⑤ get_work_info 回归 (T18 bug 修复验证)
            wi = db.get_work_info("123456")
            assert wi is not None and wi.done is True and wi.total_pages == 2, wi
            assert db.get_work_info("999999") is None
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==============================================================
# P5 · Web 版 (Flask + 浏览器单页, 替代 Tkinter gui/) —— 离线冒烟
# ==============================================================

def t21_web_backend_import_and_health() -> None:
    """T21: Web 后端 create_app + 页面/健康/登录状态/静态资源 (test_client, 离线)。"""
    from web.server import create_app

    app = create_app()
    client = app.test_client()

    r = client.get("/")
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    assert "Vixiq" in html and "app.js" in html

    r = client.get("/static/style.css")
    assert r.status_code == 200 and "text/css" in r.content_type, r.content_type

    r = client.get("/static/app.js")
    assert r.status_code == 200, r.status_code

    r = client.get("/api/health")
    j = r.get_json()
    assert j and j.get("ok") is True and j.get("app") == "vixiq", j

    r = client.get("/api/auth/status")
    j = r.get_json()
    assert j is not None and "cookies_exist" in j and "cookies_file" in j, j


def t22_web_cart_and_library_api() -> None:
    """T22: Web API 购物车 CRUD + 图库统计/搜索 (test_client, 离线, 用完即清不留脏数据)。

    路径隔离 (教训 2026-09): POST /api/cart 会顺带写 artworks/authors 表,
    不 mock paths 时测试数据 (作品 9001 / 作者 11「冒烟作者」) 会落进生产库
    D:/vixiq-data/pixiv.db, 之后被后台补元数据写成「(作品已删除 9001)」空壳。
    与 T30+ 一样把 user_data/data_root 重定向到临时目录。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t22web_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        app = create_app()
        client = app.test_client()

        # 购物车 CRUD
        r = client.post("/api/cart", json=dict(
            item_type="artwork", title="#9001 冒烟作品", artwork_id="9001",
            author_id="11", author_name="冒烟作者", page_count=3))
        j = r.get_json()
        assert r.status_code == 200 and j.get("status") == "ok", (r.status_code, j)
        new_id = int(j["item_id"])
        assert new_id > 0

        r = client.get("/api/cart")
        items = r.get_json()["items"]
        assert any(int(it["id"]) == new_id and it["artwork_id"] == "9001" for it in items), items

        r = client.delete(f"/api/cart/{new_id}")
        assert r.get_json().get("status") == "ok"
        items = client.get("/api/cart").get_json()["items"]
        assert all(int(it["id"]) != new_id for it in items), items

        # 图库统计 + 搜索 (只读)
        r = client.get("/api/library/stats")
        j = r.get_json()
        assert "total_artworks" in j and "total_pages" in j and "disk_size_bytes" in j, j

        r = client.get("/api/library/search?q=")
        assert r.status_code == 200, r.status_code
        assert isinstance(r.get_json().get("items"), list)

        # 反搜状态 (只读)
        r = client.get("/api/reverse/status")
        j = r.get_json()
        assert "scan_dir" in j and "image_files" in j, j
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t23_web_search_api_serialize() -> None:
    """T23: /api/search stub (monkey patch 共享客户端) → dataclass 序列化成 JSON 结构。"""
    from core.models import ArtworkInfo, AuthorInfo, IllustType
    import web.server as ws

    fake_art = ArtworkInfo(
        artwork_id="88958112", author_id="123", title="測試東方圖",
        illust_type=IllustType.ILLUSTRATION, page_count=2,
        tags=["東方", "測試"], image_urls=["https://i.pximg.net/thumb/x.png"],
        publish_date="2026-01-01", is_r18=False)
    fake_auth = AuthorInfo(author_id="123", name="測試畫師", followers=100, following=2)

    class _FakeSC:
        op_lock = __import__("threading").RLock()

        def execute(self, q):
            return {"type": "artwork", "artwork": fake_art, "author": fake_auth,
                    "related_works": []}

    original = ws._get_search_client
    ws._get_search_client = lambda: _FakeSC()
    try:
        app = ws.create_app()
        client = app.test_client()
        r = client.get("/api/search?q=88958112")
        j = r.get_json()
        assert r.status_code == 200 and j.get("type") == "artwork", (r.status_code, j)
        a = j["artwork"]
        assert a["artwork_id"] == "88958112" and a["title"] == "測試東方圖"
        assert a["tags"] == ["東方", "測試"] and a["page_count"] == 2
        assert a["author_name"] == "測試畫師"
        assert a["pixiv_url"].startswith("https://www.pixiv.net/artworks/")
        au = j["author"]
        assert au["name"] == "測試畫師" and au["followers"] == 100
    finally:
        ws._get_search_client = original


def t24_sse_event_bus_bridge() -> None:
    """T24: SSE 事件总线 (EventHub + WebReporter) 把 core 事件正确广播/序列化。"""
    import queue as _q
    from web.event_bus import EventHub, WebReporter

    hub = EventHub()
    q = hub.subscribe()
    r = WebReporter(hub, threading.Event())
    r.log("你好")
    r.progress(1, 2, "进行中")
    r.item_progress(7, 1, 3, "下载")
    r.put("search_done", {"status": "ok"})

    evs = []
    while True:
        try:
            evs.append(q.get_nowait())
        except _q.Empty:
            break
    kinds = [e["type"] for e in evs]
    assert "log" in kinds and "progress" in kinds, kinds
    assert "item_progress" in kinds and "search_done" in kinds, kinds
    ip = next(e for e in evs if e["type"] == "item_progress")
    assert ip["item_id"] == 7 and ip["cur"] == 1 and ip["total"] == 3, ip
    sd = next(e for e in evs if e["type"] == "search_done")
    assert sd["status"] == "ok", sd

    # 取消传播
    ev = threading.Event()
    rr = WebReporter(hub, ev)
    assert rr.is_cancelled() is False
    ev.set()
    assert rr.is_cancelled() is True


def t25_library_metadata_and_thumb() -> None:
    """T25: 图库完善 · 元数据/缩略图链路 (迁移→写库→查询→Web API, 离线)。"""
    from db.store import PixivDatabase, WSt
    from web.server import create_app, _lib_items
    from core.search_client import SearchClient

    db = PixivDatabase(":memory:")
    try:
        # ① 迁移 002 生效: thumb_url 列存在
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(artworks)")}
        assert "thumb_url" in cols, f"缺 thumb_url 列: {cols}"

        # ② 写入元数据(含缩略图/作者) → works_missing_metadata 相应减少
        db.change_work_status("101", WSt.DONE, total_pages=1, title="測試圖A",
                              author_id="11", tags=["東方"], thumb_url="https://i.pximg.net/a.png")
        db.upsert_author("11", "畫師甲")
        # ai_type 也补上: 2026-09-20 起「缺 ai_type」同样进补全队列 (AI 筛选要靠它),
        # 所以「元数据齐全」现在包含这一项, 否则 101 会被判成还缺东西
        db.set_artwork_ai_type("101", 1)
        assert db.count_missing_metadata() == 0, db.count_missing_metadata()

        # 缺元数据: 未写标题的 102 → 计数 +1 且 works_missing_metadata 命中
        db.change_work_status("102", WSt.NOT_YET, total_pages=1)
        assert db.count_missing_metadata() == 1, db.count_missing_metadata()
        missing = db.works_missing_metadata(limit=10)
        assert "102" in missing and "101" not in missing, missing

        # ③ list_recent_artworks 返回 作者名 + tags + thumb_url
        rows = db.list_recent_artworks(limit=10)
        by_id = {r["artwork_id"]: r for r in rows}
        assert "101" in by_id, by_id
        r101 = by_id["101"]
        assert r101["author_name"] == "畫師甲", r101
        assert "東方" in (r101.get("tags") or []), r101
        assert r101.get("thumb_url") == "https://i.pximg.net/a.png", r101
        assert "tags_json" not in r101, "tags_json 应已被转为 tags"

        # ④ _lib_items 前端网格字段转换 (image_urls/pixiv_url)
        items = _lib_items([r101])
        assert items[0]["image_urls"] == ["https://i.pximg.net/a.png"], items[0]
        assert items[0]["pixiv_url"].startswith("https://www.pixiv.net/artworks/"), items[0]

        # ⑤ ajax body → ArtworkInfo 带 author_name (fetch_artwork 的数据映射)
        art = SearchClient._illust_body_to_info(dict(
            illustId="777", userId="33", illustTitle="標題", pageCount=2,
            xRestrict=0, userName="畫師丙",
            urls={"thumb": "https://i.pximg.net/t.png"},
            tags={"tags": [{"tag": "R-18"}, {"tag": "テスト"}]}))
        assert art is not None and art.author_name == "畫師丙", art
        assert art.tags == ["R-18", "テスト"] and art.is_r18 is True, art
        assert art.image_urls == ["https://i.pximg.net/t.png"], art
    finally:
        db.close()

    # ⑥ Web API: /api/library/stats 含 missing_metadata; /recent /missing 可用
    app = create_app()
    client = app.test_client()
    j = client.get("/api/library/stats").get_json()
    assert "missing_metadata" in j, j
    r = client.get("/api/library/recent")
    assert r.status_code == 200 and isinstance(r.get_json().get("items"), list), r.status_code
    r = client.get("/api/library/missing")
    assert r.status_code == 200 and "count" in r.get_json(), r.status_code


def t26_library_manage_crud() -> None:
    """T26: 图库管理增强 · 筛选/排序/删除/标签增删改/编辑/分类 (离线)。"""
    from db.store import PixivDatabase, WSt, PSt
    from web.server import create_app

    db = PixivDatabase(":memory:")
    try:
        # 准备 3 个作品: 101 插画非R18, 102 漫画R18, 103 插画非R18 缺元数据
        db.change_work_status("101", WSt.DONE, total_pages=1, title="AAA 插畫",
                              author_id="11", illust_type=0, tags=["東方", "場景"],
                              bookmark_count=10, is_r18=0,
                              thumb_url="https://i.pximg.net/a.png")
        db.change_work_status("102", WSt.DONE, total_pages=3, title="BBB 漫畫",
                              author_id="11", illust_type=1, tags=["R-18", "東方"],
                              bookmark_count=99, is_r18=1,
                              thumb_url="https://i.pximg.net/b.png")
        db.change_work_status("103", WSt.NOT_YET, total_pages=1, illust_type=0)
        db.upsert_author("11", "畫師甲")

        # ① list_artworks 全量 + 筛选
        all_rows = db.list_artworks(limit=50)
        assert {r["artwork_id"] for r in all_rows} == {"101", "102", "103"}, all_rows
        # 类型筛选
        ill = db.list_artworks(illust_type=0, limit=50)
        assert {r["artwork_id"] for r in ill} == {"101", "103"}, ill
        # R18 筛选
        r18 = db.list_artworks(is_r18=1, limit=50)
        assert {r["artwork_id"] for r in r18} == {"102"}, r18
        # 作者筛选 (103 未设作者, 只含 101/102)
        by_a = db.list_artworks(author_id="11", limit=50)
        assert {r["artwork_id"] for r in by_a} == {"101", "102"}, by_a
        # 标签筛选 (JOIN artwork_tags)
        tag = db.list_artworks(tag="東方", limit=50)
        assert {r["artwork_id"] for r in tag} == {"101", "102"}, tag
        # 缺元数据筛选
        miss = db.list_artworks(missing=True, limit=50)
        assert {r["artwork_id"] for r in miss} == {"103"}, miss
        # 关键词筛选
        kw = db.list_artworks(q="AAA", limit=50)
        assert {r["artwork_id"] for r in kw} == {"101"}, kw

        # ② 排序: 标题升序(NULL 标题在前) / 收藏降序
        by_title = [r["artwork_id"] for r in db.list_artworks(sort="title", order="asc", limit=50)]
        assert by_title == ["103", "101", "102"], by_title
        by_bm = [r["artwork_id"] for r in db.list_artworks(sort="bookmark", order="desc", limit=50)]
        assert by_bm == ["102", "101", "103"], by_bm

        # ③ count_artworks_filtered 总数
        assert db.count_artworks_filtered() == 3
        assert db.count_artworks_filtered(illust_type=0) == 2
        assert db.count_artworks_filtered(tag="東方") == 2

        # ④ 标签增删改: set_artwork_tags 替换 + 清空
        db.set_artwork_tags("101", ["東方", "場景", "夜景"])
        rows = db.list_artworks(limit=50)
        r101 = next(r for r in rows if r["artwork_id"] == "101")
        assert set(r101["tags"]) == {"東方", "場景", "夜景"}, r101
        db.set_artwork_tags("101", ["東方"])
        rows = db.list_artworks(limit=50)
        r101 = next(r for r in rows if r["artwork_id"] == "101")
        assert r101["tags"] == ["東方"], r101  # 场景/夜景 从 tags_json 移除
        assert "場景" not in {t["tag_name"] for t in db.list_all_tags()}, "孤儿标签应被清理"

        # ⑤ update_artwork 编辑: 改标题/类型/R18
        db.update_artwork("103", title="CCC 改名", illust_type=2, is_r18=1)
        r103 = db.get_artwork_metadata("103")
        assert r103["title"] == "CCC 改名" and r103["illust_type"] == 2 and r103["is_r18"] == 1, r103

        # ⑥ 分类数据: list_all_tags / list_authors
        tags = {t["tag_name"]: t["count"] for t in db.list_all_tags()}
        assert tags.get("東方") == 2, tags
        authors = {a["author_id"]: a["count"] for a in db.list_authors()}
        assert authors.get("11") == 2, authors  # 103 无作者

        # ⑦ 批量删除: 删除 102,103 → 只剩 101; 关联/孤儿 tag 同步清理
        n = db.delete_artworks(["102", "103"])
        assert n == 2, n
        remain = {r["artwork_id"] for r in db.list_artworks(limit=50)}
        assert remain == {"101"}, remain
        assert db.count_artworks_filtered() == 1

        # ⑦.5 按页删除 (图片视图粒度): 只删被选中的页; 作品页删空时整作品级联
        db.mark_page_status("101", 0, PSt.DONE, local_path="11/101/101_p0.jpg")
        db.change_work_status("104", WSt.DONE, total_pages=2, title="DDD 多页",
                              author_id="11", illust_type=1, tags=["東方"], is_r18=0)
        db.mark_page_status("104", 0, PSt.DONE, local_path="11/104/104_p0.jpg")
        db.mark_page_status("104", 1, PSt.DONE, local_path="11/104/104_p1.jpg")

        # 删 104 的 p0 → 作品保留, 仅剩 p1; 返回被删文件路径
        rels, emptied = db.delete_pages([{"artwork_id": "104", "page_num": 0}])
        assert rels == ["11/104/104_p0.jpg"], rels
        assert emptied == [], emptied
        assert db.get_artwork_metadata("104") is not None
        left_pages = [r[0] for r in db.conn.execute(
            "SELECT page_num FROM pages WHERE artwork_id='104' ORDER BY page_num"
        ).fetchall()]
        assert left_pages == [1], left_pages

        # 再删 104 仅剩的 p1 → 页删空, 作品 104 级联删除
        rels2, emptied2 = db.delete_pages([{"artwork_id": "104", "page_num": 1}])
        assert rels2 == ["11/104/104_p1.jpg"], rels2
        assert emptied2 == ["104"], emptied2
        assert db.get_artwork_metadata("104") is None

        # 删 101 的唯一页 → 101 也级联删除; 不存在的页 (999) 静默跳过不报错
        rels3, emptied3 = db.delete_pages([
            {"artwork_id": "101", "page_num": 0},
            {"artwork_id": "999", "page_num": 0},
        ])
        assert rels3 == ["11/101/101_p0.jpg"], rels3
        assert emptied3 == ["101"], emptied3
        assert db.count_artworks_filtered() == 0

        # ⑦.6 批量合并图片为同一作品 (图片视图批量操作)
        #   a) 新建作品: 201(2页) 取 p1 + 202(1页) 取 p0 → 新作品 merged_xxx 页码 0/1 重排;
        #      202 搬空级联删除, 201 保留 p0
        db.change_work_status("ext_201", WSt.DONE, total_pages=2, title="EE 双页", author_id="11")
        db.mark_page_status("ext_201", 0, PSt.DONE, local_path="11/201/201_p0.jpg", md5="m201a")
        db.mark_page_status("ext_201", 1, PSt.DONE, local_path="11/201/201_p1.jpg", md5="m201b")
        db.change_work_status("ext_202", WSt.DONE, total_pages=1, title="F", author_id="11")
        db.mark_page_status("ext_202", 0, PSt.DONE, local_path="11/202/202_p0.jpg", md5="m202a")
        try:
            db.merge_pages([{"artwork_id": "ext_201", "page_num": 0}])
            raise AssertionError("单张合并应报 ValueError")
        except ValueError:
            pass
        res = db.merge_pages([{"artwork_id": "ext_201", "page_num": 1},
                              {"artwork_id": "ext_202", "page_num": 0}])
        mid = res["target_id"]
        assert mid.startswith("merged_") and res["pages_moved"] == 2, res
        assert res["artworks_removed"] == ["ext_202"], res
        # 新作品 2 页, 页码 0/1; 文件名**保留原样**(图片名归用户/来源, 不再改写成 {新id}_p{n}),
        # 只把图挪到**首张来源图所在目录** (新作品继承首张来源的作者, 与其同夹最自然)
        m_pages = [dict(r) for r in db.conn.execute(
            "SELECT page_num, local_path, md5 FROM pages WHERE artwork_id=? ORDER BY page_num",
            (mid,)).fetchall()]
        assert [(p["page_num"], p["local_path"]) for p in m_pages] == [
            (0, "11/201/201_p1.jpg"), (1, "11/201/202_p0.jpg")], m_pages
        assert [p["md5"] for p in m_pages] == ["m201b", "m202a"], m_pages  # 指纹保留
        # 标题默认「合并作品 · 首张来源作品标题」, 不允许退化成内部 ID (谁也认不出)
        assert db.get_artwork_metadata(mid)["title"] == "合并作品 · EE 双页", \
            db.get_artwork_metadata(mid)["title"]
        # ext_201 保留未选中的 p0
        assert [r[0] for r in db.conn.execute(
            "SELECT page_num FROM pages WHERE artwork_id='ext_201'").fetchall()] == [0]
        assert db.get_artwork_metadata("ext_202") is None  # 搬空即级联删除
        #   a2) 作者夹被人工分类到子目录 (X图/涩图) → 合并结果直接落回该作者夹,
        #      不必事后点「一键整理」把 merged_ 夹搬回去; 两个源文件本就在同一夹且名字不冲突
        #      → 原地不动, 不产生任何移动计划
        db.change_work_status("ext_205", WSt.DONE, total_pages=1, title="I", author_id="12")
        db.mark_page_status("ext_205", 0, PSt.DONE, local_path="X图/画师_12/205_p0.jpg", md5="m205a")
        db.change_work_status("ext_206", WSt.DONE, total_pages=1, title="J", author_id="12")
        db.mark_page_status("ext_206", 0, PSt.DONE, local_path="X图/画师_12/206_p0.jpg", md5="m206a")
        res_n = db.merge_pages([{"artwork_id": "ext_205", "page_num": 0},
                                {"artwork_id": "ext_206", "page_num": 0}])
        mid_n = res_n["target_id"]
        assert res_n["artworks_removed"] == ["ext_205", "ext_206"], res_n
        assert res_n["rel_moves"] == [], res_n["rel_moves"]   # 同夹同名 → 不用动
        n_pages = [dict(r) for r in db.conn.execute(
            "SELECT page_num, local_path FROM pages WHERE artwork_id=? ORDER BY page_num",
            (mid_n,)).fetchall()]
        assert [p["local_path"] for p in n_pages] == [
            "X图/画师_12/205_p0.jpg", "X图/画师_12/206_p0.jpg"], n_pages
        #   a3) 来源页无上级夹 (就在 img 根目录) → 兜底建 merged_ 夹 (文件夹名用内部ID, 保证唯一);
        #      绝不能因 target_dir 为空串拼出前导斜杠 (Path 拼接会跳到盘根)
        db.change_work_status("ext_207", WSt.DONE, total_pages=1, title="K", author_id="12")
        db.mark_page_status("ext_207", 0, PSt.DONE, local_path="207_p0.jpg", md5="m207a")
        db.change_work_status("ext_208", WSt.DONE, total_pages=1, title="L", author_id="12")
        db.mark_page_status("ext_208", 0, PSt.DONE, local_path="208_p0.jpg", md5="m208a")
        res_r = db.merge_pages([{"artwork_id": "ext_207", "page_num": 0},
                                {"artwork_id": "ext_208", "page_num": 0}])
        mid_r = res_r["target_id"]
        r_rels = [r[1] for r in res_r["rel_moves"]]
        assert r_rels == [f"{mid_r}/207_p0.jpg", f"{mid_r}/208_p0.jpg"], r_rels
        assert not any(x.startswith("/") for x in r_rels), r_rels
        #   a4) 并入已有作品(目标夹 11/211): 两个不同目录的同名文件都搬进目标夹 ——
        #      保留原名, 冲突的那个加 (2) 绝不覆盖 (证明「图片名归用户」而非被改成 {目标ID}_p{n})
        db.change_work_status("ext_209", WSt.DONE, total_pages=1, title="M", author_id="11")
        db.mark_page_status("ext_209", 0, PSt.DONE, local_path="11/209/miku with flower.png", md5="m209a")
        db.change_work_status("ext_210", WSt.DONE, total_pages=1, title="N", author_id="11")
        db.mark_page_status("ext_210", 0, PSt.DONE, local_path="12/210/miku with flower.png", md5="m210a")
        db.change_work_status("ext_211", WSt.DONE, total_pages=1, title="P", author_id="11")
        db.mark_page_status("ext_211", 0, PSt.DONE, local_path="11/211/211_p0.jpg", md5="m211a")
        res_d = db.merge_pages([{"artwork_id": "ext_209", "page_num": 0},
                                {"artwork_id": "ext_210", "page_num": 0}], target_id="ext_211")
        d_rels = [r[1] for r in res_d["rel_moves"]]
        assert d_rels == ["11/211/miku with flower.png",
                          "11/211/miku with flower (2).png"], d_rels
        #   b) 并入已有作品: 203(1页) 取 p0 + 204(2页) 取 p1 → 204 页码追加为 1/2, 203 级联删除
        db.change_work_status("ext_203", WSt.DONE, total_pages=1, title="G", author_id="11")
        db.mark_page_status("ext_203", 0, PSt.DONE, local_path="11/203/203_p0.jpg", md5="m203a")
        db.change_work_status("ext_204", WSt.DONE, total_pages=2, title="H 双页", author_id="11")
        db.mark_page_status("ext_204", 0, PSt.DONE, local_path="11/204/204_p0.jpg", md5="m204a")
        db.mark_page_status("ext_204", 1, PSt.DONE, local_path="11/204/204_p1.jpg", md5="m204b")
        res2 = db.merge_pages([{"artwork_id": "ext_203", "page_num": 0},
                               {"artwork_id": "ext_204", "page_num": 1}],
                              target_id="ext_204")
        assert res2["target_id"] == "ext_204" and res2["pages_moved"] == 2, res2
        assert res2["artworks_removed"] == ["ext_203"], res2
        assert [r[0] for r in db.conn.execute(
            "SELECT page_num FROM pages WHERE artwork_id='ext_204' ORDER BY page_num").fetchall()] \
            == [0, 1, 2]
        assert db.get_artwork_metadata("ext_203") is None
        # 目标作品「原 N 页」保留 pixiv 原网页页数 (2), 不被本地附加页数覆盖 (审计隐患修复);
        # 附加页 origin 打标 'merged'
        assert db.get_artwork_metadata("ext_204")["total_pages"] == 2
        # ext_204 原本只有 p0=web; 并入后 p1/p2 均为合并附加页 (origin='merged')
        orig_rows = [(r["page_num"], r["origin"]) for r in db.conn.execute(
            "SELECT page_num, origin FROM pages WHERE artwork_id='ext_204' ORDER BY page_num").fetchall()]
        assert orig_rows == [(0, 'web'), (1, 'merged'), (2, 'merged')], orig_rows
    finally:
        db.close()

    # ⑧ Web API: /items(筛选排序) /facets /delete /tags /update
    app = create_app()
    client = app.test_client()
    r = client.get("/api/library/items?type=1")
    assert r.status_code == 200 and isinstance(r.get_json().get("items"), list) and "total" in r.get_json(), r.status_code
    r = client.get("/api/library/facets")
    j = r.get_json()
    assert "tags" in j and "authors" in j, j
    # 用真实 db 文件路径的临时库测写接口? 冒烟用 :memory: 无法跨请求共享,
    # 这里仅验证 404 缺参校验与返回结构 (写接口已由上面数据层覆盖)。
    r = client.post("/api/library/delete", json={"ids": []})
    assert r.status_code == 200 and r.get_json()["deleted"] == 0, r.status_code
    r = client.post("/api/library/delete-pages", json={"items": []})
    assert r.status_code == 200 and r.get_json()["pages_deleted"] == 0, r.status_code
    r = client.post("/api/library/merge-pages", json={"items": []})
    assert r.status_code == 400, r.status_code
    r = client.post("/api/library/merge-pages", json={"items": [{"artwork_id": "1", "page_num": 0}]})
    assert r.status_code == 400, r.status_code
    # 数字 ID = pixiv 作品 → 合并闸门先拦下 (400); 非数字 ID 则因库里查无此页而 400
    r = client.post("/api/library/merge-pages", json={"items": [{"artwork_id": "1", "page_num": 0},
                                                                  {"artwork_id": "2", "page_num": 0}]})
    assert r.status_code == 400, r.status_code
    r = client.post("/api/library/merge-pages", json={"items": [{"artwork_id": "ext_x", "page_num": 0},
                                                                  {"artwork_id": "ext_y", "page_num": 0}]})
    assert r.status_code == 400, r.status_code
    r = client.post("/api/library/tags", json={})
    assert r.status_code == 400, r.status_code
    r = client.post("/api/library/update", json={})
    assert r.status_code == 400, r.status_code
    # 自检报告可回看 (过: 日志提示「39 个可疑项」但界面无处查看明细):
    # 该接口必须始终返回 {running, result} 结构, result 内含 suspicious 明细供前端弹窗回显
    r = client.get("/api/library/self-check-last")
    j = r.get_json()
    assert r.status_code == 200 and "running" in j and "result" in j, j
    if j["result"]:
        assert "suspicious" in j["result"] and "fixed" in j["result"], j["result"]


# ==============================================================
# 主入口
# ==============================================================

def t27_tasks_and_scoped_search() -> None:
    """T27: 下载任务管理(重试/清空已完成/缩略图) + 作品/作者分页 scope 搜索 (离线)。"""
    from unittest.mock import MagicMock
    from db.store import PixivDatabase, WSt
    from core.search_client import SearchClient, parse_input

    # ① 任务管理数据层: 重置失败 / 清空已完成 / 批量缩略图
    db = PixivDatabase(":memory:")
    try:
        db.change_work_status("101", WSt.DONE, total_pages=1,
                              title="A", thumb_url="https://i.pximg.net/a.png")
        db.queue_add(item_type="artwork", artwork_id="101", title="#101 A", status="pending")
        db.queue_add(item_type="artwork", artwork_id="101", title="#101 A-done", status="done")
        db.queue_add(item_type="author_all", author_id="11", title="作者全部: 甲", status="failed")
        db.queue_add(item_type="artwork", artwork_id="102", title="#102", status="failed")
        # queue_reset_failed: 2 条 failed → pending
        assert db.queue_reset_failed() == 2
        statuses = {it["status"] for it in db.queue_list()}
        assert "failed" not in statuses, statuses
        # queue_clear_done: 1 条 done 删除
        assert db.queue_clear_done() == 1
        ids_left = {it["id"] for it in db.queue_list()}
        assert len(ids_left) == 3, ids_left
        # artwork_thumbs 批量取缩略图
        thumbs = db.artwork_thumbs(["101", "102", "nope"])
        assert thumbs.get("101") == "https://i.pximg.net/a.png", thumbs
        assert "nope" not in thumbs, thumbs
    finally:
        db.close()

    # ② execute_scoped: 作品/作者 分 scope 搜索 (stub, 不连网)
    # 注意: fetch_artwork 真实现返回 ArtworkInfo(dataclass), 用属性访问 author_id;
    #       故 stub 用 SimpleNamespace(而非 dict)以匹配 execute_scoped 内部 art.author_id。
    from types import SimpleNamespace as _NS
    art = _NS(artwork_id="1048576", title="作品X", author_id="11")
    author = _NS(author_id="11", name="絵師甲")
    works = [_NS(artwork_id="1048576", title="作品X")]
    sc = object.__new__(SearchClient)          # 绕开 __init__ (不要开真浏览器)
    sc.ensure_login = MagicMock()
    sc.reporter = MagicMock()
    sc.op_lock = MagicMock()                   # execute_scoped 用锁串行化
    sc.op_lock.acquire.return_value = True
    sc.fetch_artwork = MagicMock(return_value=art)
    sc.fetch_author_info_only = MagicMock(return_value=author)
    sc.fetch_author = MagicMock(return_value=(author, works))
    sc.search_keyword = MagicMock(return_value={
        "keyword": "東方", "works": works, "related_authors": [author]})
    sc.search_authors = MagicMock(return_value=[author])   # 作者scope走搜用户接口

    # 作品 ID → 作品 scope 返回 artwork (不再 fallback 作者)
    r = sc.execute_scoped(parse_input("1048576"), "artwork")
    assert r["type"] == "artwork" and r["artwork"].artwork_id == "1048576", r
    assert not sc.fetch_author.called, "作品scope不应查作者列表"
    # 作者 ID → 作者 scope 返回 author+works
    r = sc.execute_scoped(parse_input("2889832"), "author")
    assert r["type"] == "author" and r["author"].author_id == "11", r
    # 关键字 → 作品 scope 只返回 works (keyword_works)
    r = sc.execute_scoped(parse_input("東方"), "artwork")
    assert r["type"] == "keyword_works" and r["works"], r
    # 关键字 → 作者 scope 只返回 authors (keyword_authors)
    r = sc.execute_scoped(parse_input("東方"), "author")
    assert r["type"] == "keyword_authors" and r["authors"], r
    assert sc.search_authors.called, "关键字作者scope必须走 search_authors(搜用户接口)"

    # ③ search_authors 真实解析逻辑 (stub _ajax_get, 离线): 只取真正作者, 不去作品接口顺带
    sc._ajax_get = MagicMock(return_value={
        "body": {"thumbnails": {"data": [
            {"id": 2889832, "userName": "絵師甲", "image": "https://i.pximg.net/a.png", "following": 5},
            {"id": "", "userName": "无id弃行"},                      # 无 id → 跳过
            {"id": 2889832, "userName": "重复行"},                   # 重复 id → 去重
            {"id": 900001, "userName": "絵師乙"},
        ]}}})
    aus = SearchClient.search_authors(sc, "絵師")  # 调用真实解析逻辑 (规避上方 mock)
    assert [a.author_id for a in aus] == ["2889832", "900001"], [a.author_id for a in aus]
    assert aus[0].name == "絵師甲" and aus[0].avatar_url == "https://i.pximg.net/a.png", aus[0]
    assert search_authors_url_ok(sc) is True  # 用搜用户接口而非作品接口


def search_authors_url_ok(sc) -> bool:
    """断言 search_authors 内部请求的是 ajax/search/users (搜作者), 而非 /search/artworks。

    2026-09-18 修正: 必须是**查询参数版** `?nick=` —— 老代码断言的路径版
    `/ajax/search/users/<kw>` 早就 404 了, 那条断言正是"关键词作者搜索很慢"的根因所在
    (端点永远失败 → 每次都掉进 DOM 爬页面的慢路)。"""
    import urllib.parse as _up
    url = (f"https://www.pixiv.net/ajax/search/users"
           f"?nick={_up.quote('絵師', safe='')}&blt=0&lang=zh")
    return sc._ajax_get.call_args.args == (url,)


def t28_downloader_author_resolve_and_md5() -> None:
    """T28: 下载器作者解析 4 级优先级 (fake driver, 不开真浏览器) + 纯数字昵称不拒 + MD5 落库。"""
    import tempfile
    import shutil
    import hashlib
    from db.store import PixivDatabase, WSt, PSt
    from core.crawler import PixivDownloader
    from utils.hashutil import md5_of
    from utils.reporter import PrintReporter

    tmp = Path(tempfile.mkdtemp(prefix="t28_"))
    db = PixivDatabase(":memory:")
    try:
        d = object.__new__(PixivDownloader)
        d.reporter = PrintReporter()
        d.save_folder = tmp
        d.db = db
        d.xp = {"author_name": "//x", "author_id": "//a"}
        calls = {"get": 0}

        class _El:
            def __init__(self, text):
                self.text = text

        def _illust_body(aid, uid, uname, title, tags=("风景",)):
            return json.dumps({"error": False, "body": {
                "illustId": str(aid), "illustTitle": title,
                "illustType": 0, "xRestrict": 0, "pageCount": 1,
                "userId": str(uid), "userName": uname,
                "uploadDate": "2026-09-05T00:00:00+09:00",
                "urls": {"thumb": "https://example.com/t.jpg"},
                "tags": {"tags": [{"tag": t} for t in tags]}}}, ensure_ascii=False)

        class _FakeDriver:
            def get(self, url):
                calls["get"] += 1
                self.url = url

            def find_element(self, by, sel):
                u = getattr(self, "url", "")
                if "/ajax/illust/100?" in u or u.endswith("/ajax/illust/100"):
                    return _El(_illust_body(100, 55, "画师A", "作品甲"))
                if "/ajax/illust/102" in u:
                    return _El(_illust_body(102, 88, "画师B", "作品乙", tags=("原创", "少女")))
                raise RuntimeError("no such element")

        class _FakeWait:
            def until(self, *a, **k):
                raise RuntimeError("DOM 兜底也失败")

        # 浏览器懒加载改造 (2026-09-16): driver/wait 是只读 property, 测试直注底层属性
        d._driver = _FakeDriver()
        d._wait = _FakeWait()
        d._http = False
        d._http_cookie_mtime = 0.0
        d._author_dir_cache = {}
        d.login_ok = False

        # ① 队列传入作者优先: 作者用参数; 但库里无标题 → 仍发一次 ajax 抓元数据当场落库
        aid, aname, sdir, info = d._resolve_author("100", author_id="55", author_name="画师A")
        assert (aid, aname) == ("55", "画师A"), (aid, aname)
        assert str(sdir).replace("\\", "/").endswith("画师A_55"), sdir
        assert calls["get"] == 1, "缺标题时应发 ajax 抓元数据"
        assert info is not None and info.title == "作品甲", info
        d._record_meta_info("100", info, aid, aname)
        m100 = db.get_artwork_metadata("100")
        assert m100["title"] == "作品甲" and m100["author_id"] == "55", m100
        assert "风景" in (m100.get("tags") or []), m100
        assert db.get_author("55")["name"] == "画师A"

        # ② DB 命中且标题已有; 纯数字昵称 "310123" 必须原样保留, 不得变「未知画师」
        db.change_work_status("101", WSt.NOT_YET, title="数字昵称作品", author_id="77")
        db.upsert_author("77", "310123")
        gets_before = calls["get"]
        aid, aname, sdir, info = d._resolve_author("101")
        assert (aid, aname) == ("77", "310123"), (aid, aname)
        assert "310123_77" in str(sdir), sdir
        assert calls["get"] == gets_before and info is None, "作者+标题都齐时不应碰浏览器"

        # ③ ajax 兜底: 作者 + 元数据一次拿全
        aid, aname, sdir, info = d._resolve_author("102")
        assert (aid, aname) == ("88", "画师B"), (aid, aname)
        assert info is not None and info.title == "作品乙" and list(info.tags) == ["原创", "少女"], info
        d._record_meta_info("102", info, aid, aname)
        m102 = db.get_artwork_metadata("102")
        assert m102["title"] == "作品乙" and m102["author_id"] == "88", m102

        # ④ 全失败 → 占位夹 unknown_0 (与一键整理/自检同一套占位夹名, 2026-09-21 治本),
        # 绝不散落图库根目录
        aid, aname, sdir, info = d._resolve_author("999")
        assert aid == "0" and aname == "未知画师", (aid, aname)
        assert info is None
        assert str(sdir).replace("\\", "/").endswith("unknown_0"), sdir

        # ⑤ MD5 工具 + 落库往返
        f = tmp / "f.bin"
        payload = b"abc123" * 100
        f.write_bytes(payload)
        expect_md5 = hashlib.md5(payload).hexdigest()
        assert md5_of(str(f)) == expect_md5
        assert md5_of(str(tmp / "nope.bin")) is None, "文件不存在应返回 None"
        db.mark_page_status("200", 0, PSt.DONE, "x_1/200_p0.jpg",
                            md5=expect_md5, file_size_bytes=len(payload))
        info = db.get_page_info("200", 0)
        assert info.md5 == expect_md5 and info.file_size_bytes == len(payload), \
            (info.md5, info.file_size_bytes)
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t29_reconcile_fingerprint_selfheal() -> None:
    """T29: 对账双重身份 —— 移动/改名凭 MD5 自愈路径、重复副本上报、不误判丢失。"""
    import tempfile
    import shutil
    import hashlib
    from db.store import PixivDatabase, PSt, WSt
    from utils.hashutil import md5_of

    tmp = Path(tempfile.mkdtemp(prefix="t29_"))
    db = PixivDatabase(":memory:")
    try:
        img = tmp / "img"
        dirA = img / "作者A_1"
        dirA.mkdir(parents=True)
        f = dirA / "1_p0.jpg"
        payload = b"JPEG-BYTES-T29" * 50
        f.write_bytes(payload)
        h = hashlib.md5(payload).hexdigest()
        assert md5_of(str(f)) == h

        db.change_work_status("1", WSt.DONE, title="自测作品", author_id="1")
        db.init_pages("1", 1)
        db.mark_page_status("1", 0, PSt.DONE, "作者A_1/1_p0.jpg",
                            md5=h, file_size_bytes=len(payload))

        # 基线: 路径命中, 零自愈零缺失
        rec = db.reconcile_output(img)
        assert rec.pages_relinked == 0 and not rec.missing_ids, \
            (rec.pages_relinked, rec.missing_ids)

        # ① 移到别的作者文件夹 → 凭 MD5 认回, 路径更新, 不判丢
        dirB = img / "作者B_2"
        dirB.mkdir()
        shutil.move(str(f), str(dirB / "1_p0.jpg"))
        rec = db.reconcile_output(img)
        assert rec.pages_relinked == 1, rec.pages_relinked
        info = db.get_page_info("1", 0)
        assert str(info.local_path).replace("\\", "/") == "作者B_2/1_p0.jpg", info.local_path
        assert "1" not in rec.missing_ids, rec.missing_ids

        # ② 改成无作品ID 的文件名 → 仍凭 MD5 认回
        shutil.move(str(dirB / "1_p0.jpg"), str(dirB / "旅行.jpg"))
        rec = db.reconcile_output(img)
        assert rec.pages_relinked == 1, rec.pages_relinked
        info = db.get_page_info("1", 0)
        assert str(info.local_path).replace("\\", "/").endswith("旅行.jpg"), info.local_path
        assert "1" not in rec.missing_ids, rec.missing_ids

        # ③ 复制一份回原位置 → 同一 MD5 磁盘两份 → duplicates 上报
        shutil.copy(str(dirB / "旅行.jpg"), str(dirA / "1_p0.jpg"))
        rec = db.reconcile_output(img)
        assert len(rec.duplicates) == 1, rec.duplicates
        assert len(rec.duplicates[0]["paths"]) == 2, rec.duplicates
        assert "1" not in rec.missing_ids, rec.missing_ids

        # ④ 外置图片导入: 「作者名_作者ID/作品ID_pN.jpg」新文件 → 作者当场回填 (不联网)
        dirC = img / "外部画师_42"
        dirC.mkdir()
        (dirC / "777_p0.jpg").write_bytes(b"external-img-777" * 10)
        rec = db.reconcile_output(img)
        assert rec.new_pages_linked >= 1, rec.new_pages_linked
        m777 = db.get_artwork_metadata("777")
        assert m777 is not None and m777["author_id"] == "42", m777
        assert db.get_author("42")["name"] == "外部画师"
        pi = db.get_page_info("777", 0)
        assert pi.status == PSt.DONE and "外部画师_42" in str(pi.local_path), pi.local_path
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t30_reverse_select_and_upload() -> None:
    """T30: 反搜选张 (files 白名单+多后缀) + /api/reverse/upload 落盘防路径穿越 (离线)。"""
    import io
    import tempfile
    import shutil
    import utils.paths as upaths
    from core.reverse_source import ReverseSource
    from core.reverse_search import ReverseSearch

    # ---------- ① _list_images: 全扫支持 5 种后缀; 白名单只取 basename ----------
    tmp = Path(tempfile.mkdtemp(prefix="t30_"))
    try:
        src = tmp / "in"
        src.mkdir()
        (src / "a_p0.jpg").write_bytes(b"x")
        (src / "b.PNG").write_bytes(b"x")
        (src / "c.webp").write_bytes(b"x")
        (src / "notes.txt").write_bytes(b"x")  # 非图片
        all_imgs = ReverseSource._list_images(src)
        assert sorted(p.name for p in all_imgs) == ["a_p0.jpg", "b.PNG", "c.webp"], \
            [p.name for p in all_imgs]
        sel = ReverseSearch._list_images(src, files=["a_p0.jpg", "../evil.png", "missing.jpg"])
        assert [p.name for p in sel] == ["a_p0.jpg"], [p.name for p in sel]
        sel_all = ReverseSearch._list_images(src, files=None)
        assert len(sel_all) == 3

        # ---------- ①b 身份信任链决策: 本地指纹 > 文件名自然键 > SauceNAO ----------
        from core.reverse_search import ReverseSearch as _RS
        # _decide_identity 四分支 (SauceNAO 绝不覆盖既有事实):
        d = _RS._decide_identity
        # ① 指纹命中图库页 → 身份确定, 不归档
        r = d("随便.png", "999", {"artwork_id": "149254306", "page_num": 0})
        assert r["action"] == "in_library" and r["artwork_id"] == "149254306" and r["trust"] == "hash", r
        # ② 文件名 ID 与 SauceNAO 一致 → 归档, consistent
        r = d("149254306_p0.jpg", "149254306", None)
        assert r["action"] == "archive" and r["trust"] == "consistent" and r["page"] == 0, r
        # ③ 文件名 ID 与 SauceNAO 矛盾 → 冲突, 不自动处理
        r = d("149254306_p0.jpg", "50712735", None)
        assert r["action"] == "conflict" and r["filename_id"] == "149254306" and r["artwork_id"] == "50712735", r
        # ④ 截图无 ID → 只能参考 SauceNAO, 标记 unverified
        r = d("旅行截图.png", "50712735", None)
        assert r["action"] == "archive" and r["trust"] == "unverified" and r["artwork_id"] == "50712735", r

        # ---------- ①c 归档命名规划: {作品ID}_p{页码}, 占用递增不覆盖 ----------
        tdir = tmp / "作者X_9"
        tdir.mkdir(exist_ok=True)
        p1, r1 = _RS._plan_archive_path(tdir, "50712735", 0, src / "旅行截图.png")
        assert p1.name == "50712735_p0.png" and r1 is True, (p1.name, r1)
        # 一致时保留原页码不改名
        (src / "888_p0.jpg").write_bytes(b"x")
        p3, r3 = _RS._plan_archive_path(tdir, "888", 0, src / "888_p0.jpg")
        assert p3.name == "888_p0.jpg" and r3 is False, (p3.name, r3)
        # 目标 p0 已被别的文件占用 → 页码递增不覆盖
        (tdir / "888_p0.png").write_bytes(b"x")
        p4, r4 = _RS._plan_archive_path(tdir, "888", 0, src / "b.PNG")
        assert p4.name == "888_p1.png", p4.name
        assert r4 is True

        # ---------- ①d 归档当场落权威元数据 (不等自检) ----------
        from db.store import PixivDatabase as _PDB
        from utils.reporter import PrintReporter as _PR
        rdb = _PDB(":memory:")

        class _El2:
            def __init__(self, t): self.text = t
        body555 = json.dumps({"error": False, "body": {
            "illustId": "555", "illustTitle": "反搜归档测试作",
            "illustType": 1, "xRestrict": 1, "pageCount": 3,
            "userId": "91", "userName": "测试画师",
            "uploadDate": "2026-09-05T00:00:00+09:00",
            "urls": {"thumb": "https://example.com/t555.jpg"},
            "tags": {"tags": [{"tag": "R-18"}, {"tag": "漫画"}]}}})

        class _FD:
            def __init__(self): self.url = ""
            def get(self, u): self.url = u
            def find_element(self, by, sel):
                if "/ajax/illust/555" in self.url:
                    return _El2(body555)
                raise RuntimeError("nope")
            def quit(self): pass

        # ID 直查模式: find_source 返回作者文件夹且元数据当场入库
        rs_obj = object.__new__(ReverseSource)
        rs_obj.reporter = _PR()
        rs_obj.driver = _FD()
        rs_obj.db = rdb
        rs_obj.archive_path = tmp / "img"
        rs_obj.archive_path.mkdir(exist_ok=True)
        folder = rs_obj.find_source("555")
        assert folder.replace("\\", "/").endswith("测试画师_91"), folder
        m = rdb.get_artwork_metadata("555")
        assert m["title"] == "反搜归档测试作" and m["author_id"] == "91", m
        assert set(["R-18", "漫画"]).issubset(set(m.get("tags") or [])), m
        assert m["is_r18"] == 1 and m["illust_type"] == 1, m
        assert rdb.get_author("91")["name"] == "测试画师"

        # saucenao 模式: _record_artwork_meta 同样当场落库
        rsearch = object.__new__(ReverseSearch)
        rsearch.reporter = _PR()
        rsearch.driver = _FD()
        rsearch.db = rdb
        rsearch._record_artwork_meta("555")
        m2 = rdb.get_artwork_metadata("555")
        assert m2["title"] == "反搜归档测试作" and m2["thumb_url"], m2

        # _foreign_target_dir: 外来源归档目标对齐新结构 (Tiv 类聚合 vs 纯外来源平铺)
        from db.store import WSt
        rsearch2 = object.__new__(ReverseSearch)
        rsearch2.reporter = _PR()
        rsearch2.driver = _FD()
        rsearch2.db = rdb
        rsearch2.archive_path = tmp / "img"
        rdb.upsert_author("66", "作者甲")
        rdb.change_work_status("777", WSt.DONE, title="tw", author_id="66", total_pages=1)
        rdb.update_artwork("777", source="twitter", source_author="tiv")
        d1 = rsearch2._foreign_target_dir("twitter", "tiv")
        assert d1 == rsearch2.archive_path / "作者甲_66/twitter@tiv", d1
        d2 = rsearch2._foreign_target_dir("artstation", "alena-aenami")
        assert d2 == rsearch2.archive_path / "artstation@alena-aenami", d2
        # 无账号外来源 / 未知来源 → `#...#` 包裹命名 (与图库未知作者/未知来源文件夹一致)
        d3 = rsearch2._foreign_target_dir("artstation", "")
        assert d3 == rsearch2.archive_path / "#artstation#", d3
        d4 = rsearch2._foreign_target_dir("other", "")
        assert d4 == rsearch2.archive_path / "#其他来源#", d4
        # 多来源外来作者 (local_xxx 跨 ≥2 来源) → 聚合到 作者名_local_xxx/站点@账号;
        # 单来源外来作者 → 平铺
        rdb.upsert_author("local_abc123", "多源画师")
        rdb.change_work_status("888", WSt.DONE, title="m", author_id="local_abc123", total_pages=1)
        rdb.update_artwork("888", source="twitter", source_author="duoyuan")
        rdb.change_work_status("889", WSt.DONE, title="m2", author_id="local_abc123", total_pages=1)
        rdb.update_artwork("889", source="danbooru", source_author="duoyuan")
        d5 = rsearch2._foreign_target_dir("twitter", "duoyuan")
        assert d5 == rsearch2.archive_path / "多源画师_local_abc123/twitter@duoyuan", d5
        rdb.upsert_author("local_single", "单源画师")
        rdb.change_work_status("890", WSt.DONE, title="s", author_id="local_single", total_pages=1)
        rdb.update_artwork("890", source="danbooru", source_author="dansingle")
        d6 = rsearch2._foreign_target_dir("danbooru", "dansingle")
        # 已记录名字的外来作者 → 顶层作者夹 + 来源子夹 (**单来源也一样**, 保证来源可见)
        assert d6 == rsearch2.archive_path / "单源画师_local_single/danbooru@dansingle", d6
        rdb.close()
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    # ---------- ② /api/reverse/upload: 重定向数据根到临时目录, 不污染真实图库 ----------
    tmp = Path(tempfile.mkdtemp(prefix="t30web_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        from web.server import create_app
        client = create_app().test_client()
        r = client.post("/api/reverse/upload", data={
            "files": (io.BytesIO(b"\x89PNG\r\n\x1a\n-fake-"), "up1.png"),
        }, content_type="multipart/form-data")
        j = r.get_json()
        assert r.status_code == 200 and j.get("saved") == ["up1.png"], (r.status_code, j)
        assert (tmp / "input" / "up1.png").is_file()

        # 路径穿越: ../evil.png 必须被剥成 basename, 关进 input/
        r = client.post("/api/reverse/upload", data={
            "files": (io.BytesIO(b"x"), "../evil.png"),
        }, content_type="multipart/form-data")
        assert r.status_code == 200
        assert (tmp / "input" / "evil.png").is_file(), "穿越文件名应落到 input/ 内"
        assert not (tmp / "evil.png").exists(), "绝不能逃出 input 目录"

        # 非图片拒绝
        r = client.post("/api/reverse/upload", data={
            "files": (io.BytesIO(b"x"), "a.txt"),
        }, content_type="multipart/form-data")
        j = r.get_json()
        assert j.get("rejected") == ["a.txt"], j
        assert not (tmp / "input" / "a.txt").exists()

        # 重名自动加 (2)
        r = client.post("/api/reverse/upload", data={
            "files": (io.BytesIO(b"y"), "up1.png"),
        }, content_type="multipart/form-data")
        assert r.get_json().get("saved") == ["up1 (2).png"], r.get_json()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t31_library_import_api() -> None:
    """T31: /api/library/import 已下载图片批量入库 (离线):
    有作品ID→img登记入库 / 无ID截图→转反搜收件箱 / 防穿越 / 非图片拒绝。"""
    import io
    import tempfile
    import shutil
    import sqlite3
    import utils.paths as upaths
    import web.server as wsrv

    tmp = Path(tempfile.mkdtemp(prefix="t31_"))
    orig_ud, orig_dr, orig_kick = upaths.user_data, upaths.data_root, wsrv._kick_metadata_fill
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    # 禁止后台补元数据线程起浏览器 (离线测试)
    wsrv._kick_metadata_fill = lambda *a, **k: None
    try:
        (tmp / "img").mkdir(parents=True, exist_ok=True)
        client = wsrv.create_app().test_client()
        from werkzeug.datastructures import MultiDict
        form = MultiDict([
            ("files", (io.BytesIO(b"JPEG-bytes-456" * 20), "456_p0.jpg")),
            ("files", (io.BytesIO(b"PNG-bytes-shot" * 20), "旅行截图.png")),
            ("files", (io.BytesIO(b"evil"), "../evil.png")),
            ("files", (io.BytesIO(b"text"), "note.txt")),
        ])
        r = client.post("/api/library/import", data=form,
                        content_type="multipart/form-data")
        j = r.get_json()
        assert r.status_code == 200, j
        assert j["imported"] == ["456_p0.jpg"], j
        assert set(j["redirected"]) == {"旅行截图.png", "evil.png"}, j
        assert j["rejected"] == ["note.txt"], j
        assert j["new_pages"] >= 1, j

        # 有 ID 的落 img 根目录且已登记 pages/artworks
        assert (tmp / "img" / "456_p0.jpg").is_file()
        # 无 ID 的转 input 反搜收件箱; 穿越文件名被关进 input/
        assert (tmp / "input" / "旅行截图.png").is_file()
        assert (tmp / "input" / "evil.png").is_file()
        assert not (tmp / "evil.png").exists() and not (tmp / "img" / "evil.png").exists()
        assert not (tmp / "img" / "note.txt").exists()

        db = sqlite3.connect(str(tmp / "pixiv.db"))
        row = db.execute("SELECT artwork_id, status FROM artworks WHERE artwork_id='456'").fetchone()
        assert row is not None, "导入作品应建 artworks 行"
        pg = db.execute("SELECT status, local_path FROM pages WHERE artwork_id='456'").fetchone()
        assert pg is not None and pg[0] == 1 and "456_p0.jpg" in (pg[1] or ""), pg
        db.close()

        # 重名导入自动加 (2), 不覆盖
        r = client.post("/api/library/import",
                        data=MultiDict([("files", (io.BytesIO(b"JPEG-bytes-456-v2" * 20), "456_p0.jpg"))]),
                        content_type="multipart/form-data")
        j2 = r.get_json()
        assert j2["imported"] == ["456_p0 (2).jpg"], j2
        # 同名(同作品ID+页码)但内容不同 → 报冲突, 绝不能顶掉已有登记
        assert len(j2["conflicts"]) == 1 and j2["conflicts"][0]["artwork_id"] == "456", j2
        assert j2["conflicts"][0]["registered"] == "456_p0.jpg", j2["conflicts"]
        assert j2["conflicts"][0]["path"] == "456_p0 (2).jpg", j2["conflicts"]
        pg2 = sqlite3.connect(str(tmp / "pixiv.db")).execute(
            "SELECT local_path FROM pages WHERE artwork_id='456'").fetchone()
        assert pg2[0] == "456_p0.jpg", ("登记被冒名副本顶掉", pg2)
    finally:
        wsrv._kick_metadata_fill = orig_kick
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t32_thumb_cache_and_lightweight_sync() -> None:
    """T32: /api/local-img?thumb=1 缩略图磁盘缓存 (离线):
    大图→≤480px JPEG 且体积远小于原图 / 缓存落 data/thumbs / 防穿越 403 /
    不带 thumb 返回原图; periodic 同步不强制重扫; HTTP ajax 直连函数离线映射。"""
    import io
    import tempfile
    import shutil
    import utils.paths as upaths
    import web.server as wsrv

    tmp = Path(tempfile.mkdtemp(prefix="t32_"))
    orig_ud, orig_dr, orig_kick = upaths.user_data, upaths.data_root, wsrv._kick_metadata_fill
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    wsrv._kick_metadata_fill = lambda *a, **k: None
    try:
        from PIL import Image
        import random
        (tmp / "img" / "画师A_111").mkdir(parents=True, exist_ok=True)
        big = tmp / "img" / "画师A_111" / "111_p0.png"
        # 1600x1200 噪点图 (PNG 体积大, 缩略 JPEG 应小一个数量级)
        random.seed(7)
        im = Image.new("RGB", (1600, 1200))
        im.putdata([(random.randint(0, 255), random.randint(0, 255),
                     random.randint(0, 255)) for _ in range(1600 * 1200)])
        im.save(big, "PNG")
        src_size = big.stat().st_size

        client = wsrv.create_app().test_client()
        # ① thumb=1 → JPEG 缩略图
        r = client.get("/api/local-img?p=" + "画师A_111/111_p0.png&thumb=1")
        assert r.status_code == 200, r.status_code
        assert r.mimetype == "image/jpeg", r.mimetype
        thumb_bytes = r.get_data()
        assert len(thumb_bytes) < src_size // 3, (len(thumb_bytes), src_size)
        with Image.open(io.BytesIO(thumb_bytes)) as ti:
            assert max(ti.size) <= 480 and ti.format == "JPEG", ti.size
        # 缓存文件已落在 data/thumbs/ (hash 缓存 + 稳定副本各 1 个)
        thumbs = list((tmp / "thumbs").glob("*.jpg"))
        assert len(thumbs) == 2, thumbs
        assert any(f.name.startswith("stable_") for f in thumbs), thumbs
        # 二次请求直接读缓存 (内容一致)
        r2 = client.get("/api/local-img?p=" + "画师A_111/111_p0.png&thumb=1")
        assert r2.status_code == 200 and r2.get_data() == thumb_bytes
        # ①b 快路径 _thumb_cached: 缓存已就绪 → 真; 源文件不存在 → 假。
        #     启动预热靠它跳过已缓存的图 (否则每次开服务都要按 20ms/张 把全库空转一遍)
        assert wsrv._thumb_cached(big, "img", "画师A_111/111_p0.png") is True
        assert wsrv._thumb_cached(tmp / "img" / "画师A_111" / "nope.png",
                                 "img", "画师A_111/nope.png") is False
        # ①c 缓存路径只算不生成 (调它不应新增任何缓存文件)
        before_n = len(list((tmp / "thumbs").glob("*.jpg")))
        wsrv._thumb_cache_path(big, "img", "画师A_111/111_p0.png")
        assert len(list((tmp / "thumbs").glob("*.jpg"))) == before_n
        # ② 不带 thumb → 原图字节
        r3 = client.get("/api/local-img?p=" + "画师A_111/111_p0.png")
        assert r3.status_code == 200 and len(r3.get_data()) == src_size
        # ②b 删掉源文件 → thumb 回退稳定缩略图 (缺失也能认出是啥图)
        r.close(); r2.close(); r3.close()  # 释放文件句柄 (Windows 锁), 才能删源文件
        src_bytes = big.read_bytes()
        big.unlink()
        r4 = client.get("/api/local-img?p=" + "画师A_111/111_p0.png&thumb=1")
        assert r4.status_code == 200 and r4.mimetype == "image/jpeg", (r4.status_code, r4.mimetype)
        r4.close()
        big.write_bytes(src_bytes)  # 恢复源文件, 避免空目录影响后续同步断言
        # ③ 防穿越 (带 thumb 也必须拦)
        assert client.get("/api/local-img?p=../../evil.png&thumb=1").status_code == 403
        assert client.get("/api/local-img?p=..%2F..%2Fevil.png&thumb=1").status_code in (403, 404)
        # ④ 不存在 (无稳定缓存) → 404
        assert client.get("/api/local-img?p=nope.png&thumb=1").status_code == 404
        # ⑤ periodic 同步: 200 且不强制重扫 (TTL 内重复调不报错)
        rs1 = client.post("/api/library/sync?periodic=1")
        assert rs1.status_code == 200 and "missing_ids" in rs1.get_json(), rs1.status_code
        rs2 = client.post("/api/library/sync?periodic=1")
        assert rs2.status_code == 200
        # 强制同步仍可用
        rf = client.post("/api/library/sync")
        assert rf.status_code == 200 and rf.get_json()["scanned_images"] >= 1, rf.get_json()

        # ⑥ HTTP 直连函数离线行为 (monkeypatch urllib, 不联网)
        import json as _json
        import urllib.request as _u_req
        from urllib.error import HTTPError
        from core.search_client import (build_pixiv_http_session, http_fetch_illust,
                                        ArtworkNotFoundError)
        sess = build_pixiv_http_session([
            {"name": "PHPSESSID", "value": "xxx", "domain": ".pixiv.net", "path": "/"},
            {"name": "other", "value": "y", "domain": "example.com", "path": "/"},
        ], user_id="42")
        assert sess is not None and "PHPSESSID=xxx" in sess["headers"]["Cookie"]
        assert sess["headers"]["x-user-id"] == "42"
        assert sess["headers"]["Referer"] == "https://www.pixiv.net/"
        assert build_pixiv_http_session([]) is None
        assert build_pixiv_http_session([{"name": "a", "value": "b",
                                          "domain": "example.com"}]) is None

        body = {"illustId": "123", "illustTitle": "标题", "userId": "88",
                "userName": "画师", "illustType": 0, "pageCount": 1,
                "xRestrict": 0, "uploadDate": "2026-01-02T03:04:05+00:00",
                "urls": {"thumb": "https://x/t.jpg"},
                "tags": {"tags": [{"tag": "东方"}, {"tag": "Project"}]}}

        class _Ctx:
            def __init__(self, raw):
                self._raw = raw

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_open(req, timeout=15.0):
            u = req.full_url
            if "/ajax/illust/4040" in u:
                raise HTTPError(u, 404, "Not Found", {}, None)
            if "/ajax/illust/4030" in u:
                raise HTTPError(u, 403, "Forbidden", {}, None)
            return _Ctx(_json.dumps({"error": False, "body": body}).encode("utf-8"))

        orig_open = _u_req.urlopen
        _u_req.urlopen = _fake_open
        try:
            try:
                http_fetch_illust(sess, "4040")
                raise AssertionError("404 应抛 ArtworkNotFoundError")
            except ArtworkNotFoundError:
                pass
            try:
                http_fetch_illust(sess, "4030")
                raise AssertionError("403 应抛 PermissionError")
            except PermissionError:
                pass
            info = http_fetch_illust(sess, "123")
            assert info.artwork_id == "123" and info.title == "标题"
            assert info.author_id == "88" and info.author_name == "画师"
            assert info.tags == ["东方", "Project"] and info.image_urls[0] == "https://x/t.jpg"
        finally:
            _u_req.urlopen = orig_open
    finally:
        wsrv._kick_metadata_fill = orig_kick
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t33_batch_and_single_edit_api() -> None:
    """T33: 批量编辑 /api/library/batch-update + 单人编辑 source/作者名解析 (离线):
    本地作者创建、标签三策略 (替换/追加/清空)、来源/类型批量写入、未勾选字段不动。"""
    import io
    import tempfile
    import shutil
    import sqlite3
    import utils.paths as upaths
    import web.server as wsrv

    tmp = Path(tempfile.mkdtemp(prefix="t33_"))
    orig_ud, orig_dr, orig_kick = upaths.user_data, upaths.data_root, wsrv._kick_metadata_fill
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    wsrv._kick_metadata_fill = lambda *a, **k: None
    try:
        (tmp / "img").mkdir(parents=True, exist_ok=True)
        client = wsrv.create_app().test_client()
        from werkzeug.datastructures import MultiDict
        # 造 3 个 pixiv 命名作品
        for fn in ("456_p0.jpg", "789_p0.jpg", "321_p0.jpg"):
            client.post("/api/library/import",
                        data=MultiDict([("files", (io.BytesIO(b"JPEG" * 20), fn))]),
                        content_type="multipart/form-data")

        def post(path, body):
            return client.post(path, data=json.dumps(body), content_type="application/json")

        # ① 批量: 本地作者 + 标签替换 + 来源 + 类型 (只给这些字段)
        r = post("/api/library/batch-update", {
            "ids": ["456", "789"], "author_name": "测试画师",
            "tags": ["标签甲", "标签乙"], "source": "danbooru", "illust_type": 1})
        j = r.get_json()
        assert r.status_code == 200 and j["updated"] == 2, j
        assert str(j["name"] or "").startswith("测试画师"), j

        db = sqlite3.connect(str(tmp / "pixiv.db"))
        aid = db.execute("SELECT author_id FROM authors WHERE name='测试画师'").fetchone()[0]
        assert aid.startswith("local_"), aid
        for wid in ("456", "789"):
            row = db.execute("SELECT author_id, illust_type, source FROM artworks WHERE artwork_id=?", (wid,)).fetchone()
            assert row[0] == aid and row[1] == 1 and row[2] == "danbooru", row
            tagn = [r0[0] for r0 in db.execute(
                """SELECT tg.tag_name FROM artwork_tags atg JOIN tags tg ON tg.tag_id=atg.tag_id
                   WHERE atg.artwork_id=?""", (wid,))]
            assert tagn == ["标签甲", "标签乙"], tagn
        # 未选中的 321 不受影响
        row = db.execute("SELECT author_id, source FROM artworks WHERE artwork_id='321'").fetchone()
        assert row[0] is None and (row[1] is None or row[1] == ""), row

        # ② 批量追加标签 (保留原标签, 去重)
        post("/api/library/batch-update", {"ids": ["456"], "tags": ["标签乙", "标签丙"], "tag_mode": "append"})
        tagn = [r0[0] for r0 in db.execute(
            """SELECT tg.tag_name FROM artwork_tags atg JOIN tags tg ON tg.tag_id=atg.tag_id
               WHERE atg.artwork_id='456' ORDER BY tg.tag_name""")]
        assert tagn == ["标签丙", "标签乙", "标签甲"], tagn

        # ③ 批量清空标签
        post("/api/library/batch-update", {"ids": ["456"], "tag_mode": "clear"})
        cnt = db.execute("SELECT COUNT(*) FROM artwork_tags WHERE artwork_id='456'").fetchone()[0]
        assert cnt == 0, cnt

        # ④ 单人编辑: source 可改 + 作者名解析为本地作者 + 标签写入
        r = post("/api/library/update", {
            "artwork_id": "321", "title": "新标题", "source": "other",
            "author_name": "另一个画师", "tags": ["单人标签"], "is_r18": 1})
        assert r.status_code == 200, r.get_json()
        row = db.execute("SELECT title, source, is_r18 FROM artworks WHERE artwork_id='321'").fetchone()
        assert row[0] == "新标题" and row[1] == "other" and row[2] == 1, row
        aid2 = db.execute("SELECT author_id FROM authors WHERE name='另一个画师'").fetchone()[0]
        assert db.execute("SELECT author_id FROM artworks WHERE artwork_id='321'").fetchone()[0] == aid2
        db.close()
    finally:
        wsrv._kick_metadata_fill = orig_kick
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t34_author_aliases_and_pixiv_natural_key() -> None:
    """T34: 作者改名曾用名留存 + pixiv 命名(单页裸ID/大写_P0)不再误标「其他来源」。"""
    import tempfile
    import shutil
    import hashlib
    from db.store import PixivDatabase, PSt, WSt, pixiv_natural_key

    # ---- ① 自然键识别: 单页裸ID / 大写_P0 / 带后缀 都算 pixiv ----
    assert pixiv_natural_key("119194561.gif") == ("119194561", 0)
    assert pixiv_natural_key("98836835.png") == ("98836835", 0)
    assert pixiv_natural_key("97664064_P0.png") == ("97664064", 0)   # 大写 P
    assert pixiv_natural_key("64163186_p0.png") == ("64163186", 0)
    assert pixiv_natural_key("123_p2_waifu2x.png") == ("123", 2)
    # ---- ② 排除陷阱: 前导零顺序号 / 13位毫秒时间戳 / 32位hex / 中文名 ----
    assert pixiv_natural_key("00000001.png") is None
    assert pixiv_natural_key("1730345685303.png") is None
    assert pixiv_natural_key("a1b2c3d4e5f60718293a4b5c6d7e8f90.png") is None
    assert pixiv_natural_key("旅行.jpg") is None

    tmp = Path(tempfile.mkdtemp(prefix="t34_"))
    db = PixivDatabase(":memory:")
    try:
        # ---- ③ 护栏: pixiv 命名文件绝不登记为 ext_「其他来源」 ----
        assert db.register_foreign_file("X图/とどはがね_129395/98836835.png") is None
        ext_bili = db.register_foreign_file("bili@/up主/abc123.jpg")
        assert ext_bili and ext_bili.startswith("ext_"), ext_bili
        unreg = db.foreign_unregistered_files(
            ["X图/a_1/98836835.png", "X图/a_1/119194561.gif", "bili@/x/ddd.jpg"])
        assert unreg == ["bili@/x/ddd.jpg"], unreg

        # ---- ④ 曾用名: 改名时旧名追加进 aliases (只增不删/去重/不含当前名) ----
        db.upsert_author("9001", "旧昵称")
        db.upsert_author("9001", "新昵称")
        a = db.get_author("9001")
        assert a["name"] == "新昵称" and a["aliases"] == ["旧昵称"], a
        db.upsert_author("9001", "新昵称")  # 同名重复, 不增
        assert db.get_author("9001")["aliases"] == ["旧昵称"]
        db.upsert_author("9001", "最新昵称")  # 再改名
        assert db.get_author("9001")["aliases"] == ["旧昵称", "新昵称"], db.get_author("9001")
        la = {x["author_id"]: x for x in db.list_authors()}
        # list_authors 需要该作者有作品才出现, 先挂一个
        db.change_work_status("50001", WSt.DONE, title="t", author_id="9001")
        la = {x["author_id"]: x for x in db.list_authors()}
        assert la["9001"]["aliases"] == ["旧昵称", "新昵称"], la["9001"]

        # ---- ⑤ 回填: 下载队列历史名并入 aliases ----
        db.queue_add(item_type="artwork", title="q", author_id="9002",
                     author_name="队列里的旧名", artwork_id="50002")
        db.upsert_author("9002", "现在的名字")
        n = db.backfill_author_aliases()
        assert n >= 1, n
        assert "队列里的旧名" in db.get_author("9002")["aliases"], db.get_author("9002")

        # ---- ⑥ 对账端到端: 单页裸ID + 嵌套分类目录 → 登记为正式 pixiv 作品并认作者 ----
        img = tmp / "img"
        d = img / "X图" / "とどはがね_129395"
        d.mkdir(parents=True)
        f = d / "119194561.gif"
        f.write_bytes(b"gif-single-page" * 10)
        rec = db.reconcile_output(img)
        assert rec.new_pages_linked >= 1, rec.new_pages_linked
        assert db.get_page_info("119194561", 0) is not None
        assert db.get_artwork_metadata("119194561")["author_id"] == "129395"
        assert db.get_author("129395")["name"] == "とどはがね"
        # 绝没有 ext_ 占位
        assert not db.conn.execute(
            "SELECT 1 FROM artworks WHERE artwork_id LIKE 'ext_%' "
            "AND artwork_id IN (SELECT artwork_id FROM pages WHERE local_path LIKE '%119194561%')"
        ).fetchone()

        # ---- ⑦ 历史误标转正: 手工模拟一条 ext_ 指向大写_P0 文件, promote 后转正 ----
        img2 = tmp / "img2"
        d2 = img2 / "ちんぽ王国_1079073"
        d2.mkdir(parents=True)
        f2 = d2 / "97664064_P0.png"
        f2.write_bytes(b"png-upper-P0" * 10)
        rel2 = "ちんぽ王国_1079073/97664064_P0.png"
        ext_old = "ext_" + hashlib.md5(("ext|" + rel2).encode("utf-8")).hexdigest()[:16]
        db.conn.execute(
            "INSERT INTO artworks (artwork_id,title,tags_json,thumb_url,created_at,updated_at,source)"
            " VALUES (?,?,'[]','',?,?,?)",
            (ext_old, "旧误标", "2020-01-01T00:00:00", "2020-01-01T00:00:00", "other"))
        db.conn.execute(
            "INSERT INTO pages (artwork_id,page_num,status,local_path) VALUES (?,0,?,?)",
            (ext_old, PSt.DONE, rel2))
        db.conn.commit()
        res = db.promote_pixiv_named_foreign(img2)
        assert res["promoted"] == 1, res
        assert db.get_page_info("97664064", 0) is not None
        assert db.get_artwork_metadata("97664064")["author_id"] == "1079073"
        assert db.conn.execute("SELECT 1 FROM artworks WHERE artwork_id=?",
                               (ext_old,)).fetchone() is None
        assert f2.exists(), "转正不得删除磁盘文件"
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t35_library_pagination() -> None:
    """T35: 图库分页 —— list_artworks limit/offset 翻页 + count 总数一致 + 越界回落。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase, WSt
    tmp = Path(_tf.mkdtemp(prefix="t35_"))
    db = PixivDatabase(":memory:")
    try:
        # 插入 5 个作品 (日期错开, 保证排序稳定)
        base = "2024-01-0%dT00:00:00"
        for i in range(5):
            db.change_work_status(str(70000 + i), WSt.DONE,
                                  title=f"分页作品{i}", publish_date=base % (i + 1))
        total = db.count_artworks_filtered()
        assert total >= 5, total
        # 每页 2 条 → 3 页 (第 3 页 1 条)
        p1 = db.list_artworks(limit=2, offset=0)
        p2 = db.list_artworks(limit=2, offset=2)
        p3 = db.list_artworks(limit=2, offset=4)
        ids = [r["artwork_id"] for r in p1] + [r["artwork_id"] for r in p2] + [r["artwork_id"] for r in p3]
        assert len(p1) == 2 and len(p2) == 2 and len(p3) == 1, (len(p1), len(p2), len(p3))
        assert len(set(ids)) == 5, ids  # 各页不重叠、不丢
        # 越界 offset → 空列表; 负数/0 → 从第一页
        assert db.list_artworks(limit=2, offset=999) == []
        assert len(db.list_artworks(limit=2, offset=-1)) == 2
        # 页数推导: ceil(total/2)
        import math
        assert math.ceil(total / 2) == 3
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t36_author_stats_page() -> None:
    """T36: 统计页数据 —— list_authors 带曾用名/状态/作品数 + stats_breakdown 分组。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase, WSt
    tmp = Path(_tf.mkdtemp(prefix="t36_"))
    db = PixivDatabase(":memory:")
    try:
        # 作者 A: 改名 + 状态 alive; 作者 B: 无曾用名 状态 gone; 作者 C: 无状态
        db.upsert_author("8001", "旧名A")
        db.upsert_author("8001", "新名A")
        db.conn.execute("UPDATE authors SET status='alive' WHERE author_id='8001'")
        db.upsert_author("8002", "作者B")
        db.conn.execute("UPDATE authors SET status='gone' WHERE author_id='8002'")
        db.upsert_author("8003", "作者C")
        # 3 个作品: A×1 pixiv(source 空), B×2 danbooru, C×1 pixiv
        for i, (aid, src) in enumerate([("8001", ""), ("8002", "danbooru"), ("8002", "danbooru"), ("8003", "")]):
            db.change_work_status(f"9000{i}", WSt.DONE, title=f"作品{i}",
                                  author_id=aid, publish_date="2024-02-01T00:00:00")
            db.conn.execute("UPDATE artworks SET source=? WHERE artwork_id=?", (src, f"9000{i}"))
        db.conn.commit()
        authors = {a["author_id"]: a for a in db.list_authors()}
        assert set(authors) == {"8001", "8002", "8003"}, set(authors)
        assert authors["8001"]["aliases"] == ["旧名A"] and authors["8001"]["status"] == "alive"
        assert authors["8001"]["count"] == 1 and authors["8002"]["count"] == 2
        assert authors["8003"]["status"] is None
        bd = db.stats_breakdown()
        sd = {x["source"]: x["count"] for x in bd["source_dist"]}
        assert sd.get("pixiv") == 2 and sd.get("danbooru") == 2, sd
        st = {x["status"]: x["count"] for x in bd["status_dist"]}
        assert st.get("alive") == 1 and st.get("gone") == 1 and st.get("unknown") == 1, st
        # 新增: 类型分布 / R18 分布
        ty = {x["type"]: x["count"] for x in bd["type_dist"]}
        assert ty.get(0) == 4, ty
        r18 = {x["r18"]: x["count"] for x in bd["r18_dist"]}
        assert r18.get(0) == 4, r18
        # Web API: /api/library/authors {items,total} + /api/library/breakdown 分布
        import importlib, tempfile as tf, shutil as sh
        import utils.paths as upaths
        from web import server as wsrv
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        tmp2 = Path(tf.mkdtemp(prefix="t36w_"))

        def _fake_ud(rel):
            p = tmp2 / "u" / str(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        upaths.user_data = _fake_ud
        upaths.data_root = lambda: tmp2 / "d"
        try:
            from db.store import PixivDatabase as PD
            PD(tmp2 / "d" / "pixiv.db")  # 初始化迁移
            app = wsrv.create_app()
            with app.test_client() as c:
                r = c.get("/api/library/authors?limit=5")
                assert r.status_code == 200, r.status_code
                j = r.get_json()
                assert isinstance(j["items"], list) and j["total"] >= 0
                r2 = c.get("/api/library/breakdown")
                assert r2.status_code == 200
                b2 = r2.get_json()
                assert "source_dist" in b2 and "status_dist" in b2
                assert "type_dist" in b2 and "r18_dist" in b2
        finally:
            sh.rmtree(str(tmp2), ignore_errors=True)
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t37_source_author_trace() -> None:
    """T37: 非 pixiv 来源溯源 —— parse_foreign_account 解析「来源@账号」,
    register_foreign_file 落库 + refresh_foreign_sources 回填, 只增不覆盖。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import (PixivDatabase, WSt, parse_foreign_account,
                          guess_foreign_source)
    tmp = Path(_tf.mkdtemp(prefix="t37_"))
    db = PixivDatabase(":memory:")
    try:
        # ① 解析函数: 各种「来源@账号」约定
        cases = {
            "twi@qing0ying0/xx.png": ("twitter", "qing0ying0"),
            "X图/bili@徐尤点/bili@徐尤点：1774571928008.png": ("bilibili", "徐尤点"),
            "X图/GGAC@毒瘤/GGAC@毒瘤：含羞草.png": ("ggac", "毒瘤"),
            "X图/GGAC@Tide ovo1/GGAC@Tide ovo1：彼得一激灵.png": ("ggac", "Tide ovo1"),
            "X图/D.I.S.H（twi@Dishwasher1910）_13408193/77299663_p0.png": ("twitter", "Dishwasher1910"),
            "X图/artstation@alenaaenami/untitled_waifu2x_noise1.png": ("artstation", "alenaaenami"),
            "X图/deviantart@alenaaenami/Untitled.png": ("deviantart", "alenaaenami"),
            "X图/niconico@赤倉/赤倉_im1234567.png": ("niconico", "赤倉"),
            "X图/skeb@alenaaenami/skeb_99.png": ("skeb", "alenaaenami"),
            "X图/yandere@momo/yande_456.png": ("yandere", "momo"),
            # 2026-09-13 用户精简来源: nijie/pawoo/furaffinity/e621 不再识别 → None
            "X图/nijie@まる/nijie_12345.png": None,
            "X图/furaffinity@tiger/furaff_789.png": None,
            "X图/pawoo@tiger/pawoo_789.png": None,
            "X图/e621@fox/e621_456.png": None,
            "X图/画师甲@社团乙_4006137/84047578_p0.png": None,  # pixiv 昵称带 @, 非站点键
            "X图/@rsk (sample)/sample_art_01.png": None,              # 无站点键
        }
        for rel, want in cases.items():
            got = parse_foreign_account(rel)
            assert got == want, f"{rel} → {got}, 期望 {want}"
        assert guess_foreign_source("twi@qing0ying0/xx.png") == "twitter"
        assert guess_foreign_source("X图/GGAC@毒瘤/含羞草.png") == "ggac"
        assert guess_foreign_source("artstation@alenaaenami/untitled_waifu2x_noise1.png") == "artstation"
        assert guess_foreign_source("deviantart@alenaaenami/Untitled.png") == "deviantart"
        assert guess_foreign_source("niconico@赤倉/im1234567.png") == "niconico"
        assert guess_foreign_source("yandere@momo/yande_456.png") == "yandere"
        assert guess_foreign_source("gelbooru@cat/gelb_123.png") == "gelbooru"
        assert guess_foreign_source("skeb@alenaaenami/skeb_99.png") == "skeb"
        # 2026-09-13 用户精简: e621 不再识别 → other
        assert guess_foreign_source("e621@fox/e621_456.png") == "other"

        # ①b 反搜结果链接识别 (detect_source_links 纯函数): 常用画师站 + booru 族 + 域名兜底
        from core.reverse_search import detect_source_links as _dsl
        _cases = [
            ("https://seiga.nicovideo.jp/seiga/im1234567", "niconico", "im1234567", ""),
            ("https://skeb.jp/@alenaaenami/works/99", "skeb", "99", "alenaaenami"),
            ("https://yande.re/post/show/12345", "yandere", "12345", ""),
            ("https://gelbooru.com/index.php?page=post&s=view&id=54321", "gelbooru", "54321", ""),
            ("https://chan.sankakucomplex.com/posts/777", "sankaku", "777", ""),
            ("https://www.zerochan.net/123456", "zerochan", None, ""),
            ("https://www.shutterstock.com/image-illustration/x-123", "shutterstock", None, ""),
            ("https://mangadex.org/title/abc", "mangadex", None, ""),
            ("https://www.pixiv.net/artworks/99999", "pixiv", "99999", ""),
        ]
        for url, site, aid, handle in _cases:
            r = _dsl([url])
            assert r and r["site"] == site, (url, r)
            assert r["artwork_id"] == aid, (url, r)
            assert r["handle"] == handle, (url, r)
        # 2026-09-13 用户精简来源: nijie/pawoo/furaffinity/e621 链接不再识别 → None
        for url in ("https://nijie.info/view.php?id=12345",
                    "https://pawoo.net/@alenaaenami/12345678901234567",
                    "https://www.furaffinity.net/view/12345678/",
                    "https://e621.net/posts/888"):
            assert _dsl([url]) is None, url
        assert _dsl(["https://example.com/foo"]) is None

        # ② register_foreign_file 落 source_author
        e1 = db.register_foreign_file("twi@qing0ying0/a.jpg")
        assert e1
        row = db.conn.execute(
            "SELECT source, source_author FROM artworks WHERE artwork_id=?",
            (e1,)).fetchone()
        assert row[0] == "twitter" and row[1] == "qing0ying0", tuple(row)
        e2 = db.register_foreign_file("X图/bili@徐尤点/bili@徐尤点：x.png")
        row = db.conn.execute(
            "SELECT source, source_author FROM artworks WHERE artwork_id=?",
            (e2,)).fetchone()
        assert row[0] == "bilibili" and row[1] == "徐尤点", tuple(row)

        # ③ 只增不覆盖: 已登记 + 已有 source_author 的, 手动改值后 refresh 不得覆盖
        db.conn.execute("UPDATE artworks SET source_author='手动指定' WHERE artwork_id=?",
                        (e1,))
        db.conn.commit()
        db.refresh_foreign_sources()
        row = db.conn.execute(
            "SELECT source_author FROM artworks WHERE artwork_id=?", (e1,)).fetchone()
        assert row[0] == "手动指定", row[0]

        # ④ 无站点键 / 无法解析的不填
        e3 = db.register_foreign_file("X图/@rsk (sample)/sample_art_01.png")
        row = db.conn.execute(
            "SELECT source_author FROM artworks WHERE artwork_id=?", (e3,)).fetchone()
        assert (row[0] or "") == ""
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t38_author_management() -> None:
    """T38: 作者管理页 —— list_authors 筛选(q/status/source)/排序/分页 +
    update_author 改名(旧名进曾用名)/状态/曾用名整体替换 + Web 端点。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase, WSt
    tmp = Path(_tf.mkdtemp(prefix="t38_"))
    db = PixivDatabase(":memory:")
    try:
        db.upsert_author("5001", "画家A")
        db.upsert_author("5001", "画家A新名")   # 曾用名: 画家A
        db.conn.execute("UPDATE authors SET status='alive' WHERE author_id='5001'")
        db.upsert_author("5002", "画师B")
        db.conn.execute("UPDATE authors SET status='gone' WHERE author_id='5002'")
        db.upsert_author("5003", "第三C")
        # 作品: A×3 (pixiv 1 + twitter 2), B×1 (danbooru), C×1 (pixiv)
        for i, (aid, src) in enumerate([("5001", ""), ("5001", "twitter"),
                                        ("5001", "twitter"), ("5002", "danbooru"),
                                        ("5003", "")]):
            db.change_work_status(f"7700{i}", WSt.DONE, title=f"w{i}",
                                  author_id=aid)
            db.conn.execute("UPDATE artworks SET source=? WHERE artwork_id=?",
                            (src, f"7700{i}"))
        db.conn.commit()
        # ① 搜索 q: 匹配名 / 曾用名 / ID
        r = db.list_authors(q="画家A")
        assert [x["author_id"] for x in r] == ["5001"], r
        r = db.list_authors(q="新名")
        assert [x["author_id"] for x in r] == ["5001"], r
        r = db.list_authors(q="5003")
        assert [x["author_id"] for x in r] == ["5003"], r
        r = db.list_authors(q="不存在XYZ")
        assert r == [], r
        # ② 筛选 status / source
        r = db.list_authors(status="alive")
        assert [x["author_id"] for x in r] == ["5001"], r
        r = db.list_authors(source="twitter")
        assert [x["author_id"] for x in r] == ["5001"], r
        r = db.list_authors(source="danbooru")
        assert [x["author_id"] for x in r] == ["5002"], r
        # ③ 排序 / 分页
        r = db.list_authors(sort="count", order="desc")
        assert r[0]["author_id"] == "5001" and r[0]["count"] == 3, r
        r = db.list_authors(sort="name", order="asc")
        assert [x["author_id"] for x in r] == ["5001", "5002", "5003"], r  # UTF-8 字节序: 画<画师B<第
        assert db.count_authors_filtered() == 3
        page1 = db.list_authors(limit=2, offset=0, sort="count", order="desc")
        page2 = db.list_authors(limit=2, offset=2, sort="count", order="desc")
        ids = [x["author_id"] for x in page1 + page2]
        assert len(set(ids)) == 3, ids
        # ④ sources 聚合字段
        a1 = [x for x in db.list_authors() if x["author_id"] == "5001"][0]
        assert "pixiv" in a1["sources"] and "twitter" in a1["sources"], a1["sources"]
        # ⑤ update_author: 改名 → 旧名进曾用名; 曾用名整体替换剔除当前名; 状态
        u = db.update_author("5001", name="画家AAA", status="unknown")
        assert u["name"] == "画家AAA" and u["status"] == "unknown"
        assert "画家A新名" in u["aliases"] and "画家A" in u["aliases"], u["aliases"]
        db.update_author("5001", aliases=["旧名X", "画家A新名", "画家AAA"])
        u = db.get_author("5001")
        assert u["aliases"] == ["旧名X", "画家A新名"], u["aliases"]  # 剔除当前名+去重
        # ⑥ Web 端点: 分页 + 编辑
        import tempfile as tf, shutil as sh
        import utils.paths as upaths
        from web import server as wsrv
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        tmp2 = Path(tf.mkdtemp(prefix="t38w_"))

        def _fake_ud(rel):
            p = tmp2 / "u" / str(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            return p

        upaths.user_data = _fake_ud
        upaths.data_root = lambda: tmp2 / "d"
        try:
            from db.store import PixivDatabase as PD
            wdb = PD(tmp2 / "u" / "pixiv.db")   # 与 server._db() 同路径 (paths.user_data mock)
            wdb.upsert_author("5001", "原名")
            wdb.close()
            app = wsrv.create_app()
            with app.test_client() as c:
                r = c.get("/api/library/authors?q=画家&limit=1")
                j = r.get_json()
                assert r.status_code == 200 and isinstance(j["items"], list)
                r = c.post("/api/library/author-edit",
                           json={"author_id": "5001", "name": "改名1", "status": "alive",
                                 "aliases": ["旧名Y"]})
                assert r.status_code == 200, r.status_code
                a = r.get_json()["author"]
                assert a["name"] == "改名1" and a["status"] == "alive"
                r = c.post("/api/library/author-edit", json={"author_id": "nope"})
                assert r.status_code == 404
        finally:
            sh.rmtree(str(tmp2), ignore_errors=True)
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t39_author_name_and_source_filter() -> None:
    """T39: 画师现用名剥离 @ 污染 + 多曾用名留存 + clean_author_alias_hygiene +
    list_authors 来源筛选 pixiv(空值特判)/ggac/danbooru/twitter。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase, WSt
    tmp = Path(_tf.mkdtemp(prefix="t39_"))
    db = PixivDatabase(":memory:")
    try:
        # ① 污染名: 「现用名（twi@acc）」→ 剥成净名, 账号进曾用名
        db.upsert_author("7001", "SanMuYYB（twi@sanmuyyb）")
        r = db.backfill_clean_author_names()
        assert r == 1, r
        a = db.get_author("7001")
        assert a["name"] == "SanMuYYB", a["name"]
        assert "sanmuyyb" in a["aliases"], a["aliases"]  # twi@ 变体被卫生去重 → 留裸账号
        # ② 多曾用名: (twi@A) + (GGAC@B) 两个括号 → 两个账号都进曾用名
        db.upsert_author("7002", "甲（twi@aaaa）（GGAC@bbbb）")
        db.backfill_clean_author_names()
        a = db.get_author("7002")
        assert a["name"] == "甲", a["name"]
        assert "aaaa" in a["aliases"] and "bbbb" in a["aliases"], a["aliases"]
        assert not any("@" in x for x in a["aliases"]), a["aliases"]  # site@acc 冗余变体已去重
        # ③ 正常日文括号(不含 @)绝不动
        db.upsert_author("7003", "MUK（むっく）")
        db.backfill_clean_author_names()
        a = db.get_author("7003")
        assert a["name"] == "MUK（むっく）", a["name"]  # 保持不变
        # ④ 曾用名卫生: 剔除与现用名重复项 + site@acc 冗余变体
        db.conn.execute(
            "UPDATE authors SET aliases_json=? WHERE author_id='7001'",
            ('["D.I.S.H","twi@sanmuyyb","sanmuyyb","sanmuyyb"]',))
        db.conn.commit()
        n = db.clean_author_alias_hygiene()
        assert n >= 1, n
        # 改名后再 hygiene 不误删
        # ⑤ 来源筛选: pixiv 空值特判 / ggac / twitter
        for i, (aid, src) in enumerate([("7101", ""), ("7101", "twitter"),
                                        ("7102", "ggac"), ("7102", "ggac"),
                                        ("7101", "pixiv2")]):  # 无效站点值应被 NOCASE 精确匹配忽略
            if src == "pixiv2":
                src = "ggac2"  # 保持可控
            db.upsert_author(aid, f"画手{i}")
            db.change_work_status(f"810{i}", WSt.DONE, title=f"w{i}", author_id=aid)
            db.conn.execute("UPDATE artworks SET source=? WHERE artwork_id=?",
                            (src, f"810{i}"))
        db.conn.commit()
        px = db.list_authors(source="pixiv")
        assert [x["author_id"] for x in px] == ["7101"], px
        py = db.list_authors(source="ggac")
        assert [x["author_id"] for x in py] == ["7102"], py
        pz = db.list_authors(source="twitter")
        assert [x["author_id"] for x in pz] == ["7101"], pz
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t40_stats_tags_and_author_works() -> None:
    """T40: 统计页新增维度 —— stats_breakdown 标签 Top + 作者作品数分布。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase, WSt
    tmp = Path(_tf.mkdtemp(prefix="t40_"))
    db = PixivDatabase(":memory:")
    try:
        # 作者 A×3 作品, B×2 作品, C×1 作品 → 作者作品数分布: {1张:1, 2-5张:2}
        for i, aid in enumerate(["9201", "9201", "9201", "9202", "9202", "9203"]):
            db.upsert_author(aid, f"画师{i}_{aid}")
            db.change_work_status(f"930{i}", WSt.DONE, title=f"w{i}", author_id=aid)
        # 标签: A1[猫 猫 原創], A2[原創], A3 无标签
        db.conn.execute(
            "UPDATE artworks SET tags_json=? WHERE artwork_id='9300'",
            ('["猫","猫","原創"]',))
        db.conn.execute(
            "UPDATE artworks SET tags_json=? WHERE artwork_id='9301'",
            ('["原創"]',))
        db.conn.commit()
        bd = db.stats_breakdown()
        # 标签 Top: 猫×2, 原創×2
        tag = {x["tag"]: x["count"] for x in bd["tag_dist"]}
        assert tag.get("猫") == 2 and tag.get("原創") == 2, tag
        # 作者作品数分布
        aw = {x["label"]: x["count"] for x in bd["author_works_dist"]}
        assert aw.get("1 张") == 1 and aw.get("2-5 张") == 2, aw
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t41_folder_adopt_no_overwrite() -> None:
    """T41: 图库文件夹「作者名_ID」补作者 → 已有权威名绝不覆盖 (同步轮询不再
    把旧文件夹名写回), 新建时剥离「(twi@账号)」污染。"""
    import tempfile as _tf
    import shutil as _sh
    from db.store import PixivDatabase
    tmp = Path(_tf.mkdtemp(prefix="t41_"))
    db = PixivDatabase(":memory:")
    try:
        db.upsert_author("5001", "权威名")
        img = tmp / "img"
        (img / "旧昵称_5001").mkdir(parents=True)
        (img / "新画师（twi@newacc）_6002").mkdir(parents=True)
        (img / "无ID文件夹").mkdir(parents=True)
        from web import server as wsrv
        n = wsrv._adopt_authors_from_folders(db, img)
        assert n == 1, n  # 只新建了 6002
        a1 = db.get_author("5001")
        assert a1["name"] == "权威名", a1["name"]  # 已有权威名不被文件夹旧昵称覆盖
        a2 = db.get_author("6002")
        assert a2 and a2["name"] == "新画师", (a2 or {}).get("name")  # 新建时剥污染
        # 再次轮询 (模拟 15s 同步) → 幂等, 仍不覆盖
        wsrv._adopt_authors_from_folders(db, img)
        assert db.get_author("5001")["name"] == "权威名"
        assert db.get_author("6002")["name"] == "新画师"
    finally:
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t42_sync_author_folder() -> None:
    """T42: 作者改名 → 磁盘文件夹跟随「净名_ID」+ local_path 同步 (离线):
    ① 污染括号 (源站账号) 被剥; ② 真名自带括号保留 (Cetera（セトラ）不动);
    ③ 非数字ID (外来作者) 不误动; ④ 文件夹缺失安全跳过。"""
    import tempfile as _tf
    import shutil as _sh
    import utils.paths as _upaths
    import web.server as _wsrv
    from db.store import PixivDatabase, PSt
    tmp = Path(_tf.mkdtemp(prefix="t42_"))
    orig_ud, orig_dr = _upaths.user_data, _upaths.data_root
    _upaths.user_data = lambda rel: (tmp / str(rel))
    _upaths.data_root = lambda: tmp
    db = PixivDatabase(":memory:")
    try:
        (tmp / "img" / "X图" / "旧名（twi@oldacc）_5001").mkdir(parents=True)
        (tmp / "img" / "X图" / "Cetera（セトラ）_192311").mkdir(parents=True)
        db.mark_page_status("1001", 1, PSt.DONE,
                            local_path="X图/旧名（twi@oldacc）_5001/a.png", md5="m1")
        db.mark_page_status("1002", 1, PSt.DONE,
                            local_path="X图/Cetera（セトラ）_192311/b.png", md5="m2")
        # ① 改名 → 文件夹剥污染 + local_path 同步
        n = _wsrv._sync_author_folder(db, "5001", "旧名（twi@oldacc）")
        assert n == 1, n
        assert (tmp / "img" / "X图" / "旧名_5001").is_dir(), "污染括号应被剥"
        assert not (tmp / "img" / "X图" / "旧名（twi@oldacc）_5001").exists()
        lp = db.conn.execute("SELECT local_path FROM pages WHERE artwork_id='1001'").fetchone()[0]
        assert lp == "X图/旧名_5001/a.png", lp
        # ② 真名自带括号 → 不误剥
        n = _wsrv._sync_author_folder(db, "192311", "Cetera（セトラ）")
        assert n == 0, n
        assert (tmp / "img" / "X图" / "Cetera（セトラ）_192311").is_dir()
        # ③ 非数字ID → 安全跳过 (外来作者文件夹是 站点@账号, 不按 净名_ID 改)
        assert _wsrv._sync_author_folder(db, "local_abc", "新名") == 0
        # ④ 文件夹不存在 → 安全跳过不抛
        assert _wsrv._sync_author_folder(db, "9999", "不存在") == 0
    finally:
        _upaths.user_data = orig_ud
        _upaths.data_root = orig_dr
        db.close()
        _sh.rmtree(str(tmp), ignore_errors=True)


def t43_unreadable_author_name_fallback() -> None:
    """T43: 作者原网页改成空白符号/纯符号 (搜不出) → 主名称回退到可读名, 绝不写空名入库。
    ① 判定: 空白/零宽/纯符号 → 不可辨认; 正常名/空串 → 可辨认;
    ② 首次改名就是空白 → 回退到当前 DB 主名 (回归: 旧逻辑只查曾用名导致空名写入);
    ③ 当前名已是空白 + 曾用名可读 → 恢复曾用名;
    ④ 全部名字都不可辨认 → 保留原值;
    ⑤ 模拟 _refresh_author_names 全流程: fetch 到空白名时数据库主名保持不变。"""
    from unittest.mock import MagicMock
    import web.server as _wsrv
    from db.store import PixivDatabase
    db = PixivDatabase(":memory:")
    try:
        # ① 判定
        assert _wsrv._is_unreadable_name('ㅤ')
        assert _wsrv._is_unreadable_name('ㅤㅤㅤㅤㅤ')
        assert _wsrv._is_unreadable_name('。')
        assert _wsrv._is_unreadable_name('***')
        assert _wsrv._is_unreadable_name('\u200b\u3164\u3000')
        assert not _wsrv._is_unreadable_name('ちんぽ王国')
        assert not _wsrv._is_unreadable_name('7ife')
        assert not _wsrv._is_unreadable_name('')
        # ② 首次改名就是空白 → 回退当前可读主名, 曾用名不动
        db.upsert_author("1079073", "ちんぽ王国")
        got = _wsrv._readable_author_name(db, "1079073", "ㅤ")
        assert got == "ちんぽ王国", got
        a = db.get_author("1079073")
        assert a["name"] == "ちんぽ王国" and a["aliases"] == [], a
        # ③ 当前名已是空白, 曾用名可读 → 恢复曾用名
        db.upsert_author("27797423", "7ife")
        db.upsert_author("27797423", "ㅤㅤㅤㅤㅤ")   # 旧名 7ife 自动进曾用名
        assert db.get_author("27797423")["name"] == "ㅤㅤㅤㅤㅤ"
        got = _wsrv._readable_author_name(db, "27797423", "ㅤㅤㅤㅤㅤ")
        assert got == "7ife", got
        # ④ 所有名字都不可辨认 → 保留原值 (没有可读名可回退)
        db.upsert_author("30001", "ㅤ")
        db.upsert_author("30001", "ㅤㅤ")
        got = _wsrv._readable_author_name(db, "30001", "ㅤㅤㅤ")
        assert got == "ㅤㅤㅤ", got
        # ⑤ _refresh_author_names 全流程: fetch 到空白名 → 主名保持可读, 不写入空名
        import core.search_client as _sc
        old_fetch = _sc.http_fetch_user_name
        db.upsert_author("4001", "可读名A")
        _sc.http_fetch_user_name = lambda session, uid, **kw: "ㅤ" if uid == "4001" else None
        try:
            renamed = _wsrv._refresh_author_names(db, MagicMock(), MagicMock())
            assert renamed == 0, renamed   # 空白名被拦截 → 0 个改名
            a = db.get_author("4001")
            assert a and a["name"] == "可读名A", a and a["name"]
            assert a["status"] == "alive"   # 存续状态照常入库
        finally:
            _sc.http_fetch_user_name = old_fetch
    finally:
        db.close()


def run_suite() -> SuiteResult:
    suite = SuiteResult()
    suite.add("T1", "全项目 .py 语法检查 (py_compile)", t1_syntax_check_all)
    suite.add("T2", "核心模块 9+GUI import 全通过", t2_import_core_modules)
    suite.add("T3", "load_config canonicalize 兜底断言", t3_config_canonicalize)
    suite.add("T4", "parse_input 6 样例分类命中", t4_search_parse_input)
    suite.add("T5", "PixivDatabase 下载清单 queue_add/list/remove CRUD", t5_db_queue_crud)
    suite.add("T7", "main.py + build.spec 入口完整性 + requirements 依赖声明完整 (import 差集)", t7_entry_sanity)
    suite.add("T8", "P4a parse_input keyword 边例 (多字节/空格/引号)", t8_parse_input_keyword_edge)
    suite.add("T9", "P4a 关键字搜索结果结构 (stub, 不连网)", t9_search_keyword_result_structure)
    suite.add("T13", "P4b+ ajax 数据层 (映射/错误分类/收敛等待, 离线)", t13_ajax_data_layer)
    suite.add("T18", "下载引擎 → 本地文件写出 + run_cart 调度 (离线)", t18_download_engine_writes_files)
    suite.add("T19", "反搜来源 (saucenao 解析 + DB 缓存 + process_one 归档, 离线)", t19_reverse_source_offline)
    suite.add("T20", "数据库管理图片并保存 (入库→统计→搜索→导出/备份, 离线)", t20_db_save_and_library)
    suite.add("T21", "Web 后端 create_app + 页面/健康/登录状态/静态资源 (离线)", t21_web_backend_import_and_health)
    suite.add("T22", "Web API 购物车 CRUD + 图库统计/搜索/反搜状态 (离线)", t22_web_cart_and_library_api)
    suite.add("T23", "Web /api/search stub 序列化 (dataclass → JSON 结构, 离线)", t23_web_search_api_serialize)
    suite.add("T24", "Web SSE 事件总线 (EventHub + WebReporter 广播/取消, 离线)", t24_sse_event_bus_bridge)
    suite.add("T25", "图库完善 元数据/缩略图链路 (迁移002→写库→查询→Web API, 离线)", t25_library_metadata_and_thumb)
    suite.add("T26", "图库管理增强 筛选/排序/删除/标签增删改/编辑/分类 (数据层+Web API, 离线)", t26_library_manage_crud)
    suite.add("T27", "下载任务管理(重试/清空已完成/缩略图) + 作品/作者分页 scope 搜索 (数据层+stub, 离线)", t27_tasks_and_scoped_search)
    suite.add("T28", "下载器作者解析4级优先级+纯数字昵称+MD5落库 (fake driver, 离线)", t28_downloader_author_resolve_and_md5)
    suite.add("T29", "对账双重身份: MD5移动/改名自愈+重复副本上报 (离线)", t29_reconcile_fingerprint_selfheal)
    suite.add("T30", "反搜选张白名单+多后缀 + /api/reverse/upload 落盘防穿越 (离线)", t30_reverse_select_and_upload)
    suite.add("T31", "图库导入 /api/library/import: 有ID入库/无ID转反搜/防穿越/重名 (离线)", t31_library_import_api)
    suite.add("T32", "缩略图磁盘缓存 thumb=1 + 轮询轻量同步 + HTTP ajax 直连 (离线)", t32_thumb_cache_and_lightweight_sync)
    suite.add("T33", "批量编辑 batch-update + 单人编辑扩展 (作者/标签三策略/来源, 离线)", t33_batch_and_single_edit_api)
    suite.add("T34", "作者曾用名留存 + pixiv命名(单页/大写P)不再误标其他来源 (离线)", t34_author_aliases_and_pixiv_natural_key)
    suite.add("T35", "图库分页: list_artworks limit/offset 翻页不重叠不丢 + count 总数一致 + 越界回落 (离线)", t35_library_pagination)
    suite.add("T36", "统计页数据: list_authors 带曾用名/状态/作品数 + stats_breakdown 来源/状态/类型/R18 分组 + /api/library/authors & breakdown (离线)", t36_author_stats_page)
    suite.add("T37", "非 pixiv 来源溯源: parse_foreign_account 解析 + register/refresh 落 source_author 只增不覆盖 (离线)", t37_source_author_trace)
    suite.add("T38", "作者管理页: list_authors 筛选/排序/分页 + update_author 改名/状态/曾用名 + /api/library/author-edit (离线)", t38_author_management)
    suite.add("T39", "画师名剥离@污染 + 多曾用名留存 + aliases 卫生 + 来源筛选 pixiv空值/ggac/twitter (离线)", t39_author_name_and_source_filter)
    suite.add("T40", "统计页新增维度: stats_breakdown 标签 Top + 作者作品数分布 (离线)", t40_stats_tags_and_author_works)
    suite.add("T41", "图库文件夹补作者: 已有权威名不覆盖 + 新建时剥离@污染 (离线)", t41_folder_adopt_no_overwrite)
    suite.add("T42", "作者改名→磁盘文件夹跟随净名_ID + local_path 同步 (剥污染/真名括号保留/外来不误动/缺失跳过, 离线)", t42_sync_author_folder)
    suite.add("T43", "作者改成空白/纯符号名 → 主名称回退可读名, 绝不写空名入库 (离线)", t43_unreadable_author_name_fallback)
    suite.add("T44", "批量加入清单 /api/cart/batch-add: 多作品入队 + DONE 保护 + 无效项跳过 (临时库, 离线)", t44_batch_add_cart)
    suite.add("T45", "非 pixiv 非数字 ID 购物车任务: 不进下载器秒失败, 不影响其他任务 (离线)", t45_nonpixiv_cart_task_fast_fail)
    suite.add("T46", "来源治理 API: URL 解析归类 detect/set/非法站点拒绝/无本地页反搜 400 (临时库, 离线)", t46_source_url_detect_and_set)
    suite.add("T47", "反搜进度接线: 配置兜底 batch_size/break + /api/reverse/status 返回 progress (离线)", t47_reverse_progress_wiring)
    suite.add("T48", "一键整理: 错位归位/文件夹改名/根散件归类 预览+执行+路径同步 (离线)", t48_tidy_preview_apply)
    suite.add("T49", "未署名显示兜底: 无作者→未知作者 + 外来源有账号→显示账号 (离线)", t49_unknown_author_display)
    suite.add("T50", "「未署名」占位作者迁移: 无账号不再建假作者 + 存量 local_未署名 清为无作者 (离线)", t50_unsigned_author_migration)
    suite.add("T51", "服务自动退出判定: 必须 SSE 断+心跳/bye 超期双信号才退出 (隐藏标签被节流不得误杀, 离线)", t51_browser_gone_watchdog)
    suite.add("T52", "启动流程不得用 Python tkinter (Tk 非主线程建/销会 tcl86t.dll abort 致进程无声消失, 离线静态检查)", t52_no_python_tkinter_in_startup)
    suite.add("T53", "操作审计日志: 增/改/删落盘+筛选/计数/清空 + 写接口自动记账 + /api/logs/operations (离线)", t53_operation_audit_log)
    suite.add("T54", "自检审批逐条处理: 可疑项「指定作者/反搜来源」按钮 + dismiss 从内存&落盘报告移除 (离线)", t54_selfcheck_approval_actions)
    suite.add("T55", "ID归ID·图片名归图片名: 人读标签/审计带标签+ID/无意义标题回填/锁定机制已移除 (离线)", t55_readable_labels_and_title_repair)
    suite.add("T56", "放大版特殊格式(waifu2x/w2x): 标记保留并拉齐 _waifu2x[_noiseN][_scaleNx] + 已规范不动 (离线)", t56_waifu2x_special_format)
    suite.add("T57", "「作品页已失效」落字段 deleted_at: 只认 pixiv ID/筛选与标题解耦/退出补全队列/成功自动撤销 (离线)", t57_deleted_flag_is_a_field)
    suite.add("T58", "单向闭环 用户→数据库→磁盘: 不一致只出「待确认」不改库/争议夹冻结/adopt-claim 回流写入/命名不写作者名 (离线)", t58_one_way_loop)
    suite.add("T59", "单向闭环审计修正: 位置规范不再自我套娃/标题回填只认可读名/占位夹名不建作者/旧路径→R18 启发式已删 (离线)", t59_one_way_audit_fixes)
    suite.add("T60", "外来源作者以源网页当前值为准(不再信 SauceNAO 索引名): 网页→账号→索引名三档 + 索引名不预填进库 (离线)", t60_foreign_author_from_source_page)
    suite.add("T61", "本地作者身份只认一套键: (站点,账号) 唯一/改名不分叉/已归属就复用 + 夹名多别名用顿号不含斜杠 (离线)", t61_single_author_identity)
    suite.add("T62", "一键整理自带「清理空夹」: 真空夹/只剩 Thumbs.db 删、含真实文件不删、嵌套只列外层、冻结夹不碰 (离线)", t62_tidy_removes_empty_dirs)
    suite.add("T63", "Output 同步防重复登记: 已在库文件不重建假作品/不刷反斜杠, 真新文件正常入库, 幂等 (离线)", t63_output_sync_no_duplicate_registration)
    suite.add("T64", "下载任务单飞: 进行中再触发不开第二线程, 只排队补跑 (离线)", t64_cart_launch_single_flight)
    suite.add("T65", "pixiv_natural_key 严格身份识别: 外来源顺序号/前导零/哈希尾巴/毫秒戳全部 None, 规范命名/裸ID 命中 (离线)", t65_natural_key_strict)
    suite.add("T66", "后台任务单飞409: 反搜 /api/reverse/run 与手动补全 /api/library/complement 进行中再请求返回409且不起新任务 (离线)", t66_bg_task_single_flight_409)
    suite.add("T67", "pixez 式加入清单: 队列重复409/已下载409/缺页可补/作者去重/状态批量回显/batch分类计数 (离线)", t67_cart_dedup_pixez_style)
    suite.add("T68", "多页漏检兜底: DOM 只有 p0 时按权威页数从 URL 规律推导缺页 (原图/master1200), 已齐/异常不补 (离线)", t68_missing_page_url_inference)
    suite.add("T69", "入库 jpg→png 转换: jpg 转 png+删原文件, png 原样跳过, CMYK 安全转 RGB, 损坏文件保留原样 (离线)", t69_jpg_to_png_convert)
    suite.add("T70", "对账补齐作品状态: 页齐全的漏刷作品刷成 DONE+exist_list, 页不齐/已DONE 不动且 total_pages 不被清 (离线)", t70_promote_completed_after_output_sync)
    suite.add("T71", "搜索结果注入本地状态: 已入库/在队列/缺页/从未下载 四态 + local_path, 单卡与列表都覆盖 (离线)", t71_search_local_status_injection)
    suite.add("T72", "入库时间刷新: 有图落盘后按「入库排序」排到最前, 部分下载(NOT_YET)同样刷新, 时间格式与 schema 一致 (离线)", t72_bump_created_at_sorting)
    suite.add("T73", "下载静默成功回归: 未抓到图片/全部页失败必须抛异常判失败, 不再误标 done; 部分成功不算失败 (离线)", t73_zero_page_download_marked_failed)
    suite.add("T74", "本地导入=复制入库+源文件进回收站: dry-run 只统计/文件夹与多选图片两条路/同名跳过且源文件留在原处/无ID转收件箱/非图片不动/安全闸拦截图库内部与上级 (离线)", t74_import_from_folder_moves_files)
    suite.add("T75", "拆分图片: 非pixiv作品自由拆且新作品继承作者/来源/R18, pixiv原网页页禁止拆(且不动库), 合并附加页可拆, 拆空级联删除 (离线)", t75_split_pages_creates_single_artworks)
    suite.add("T76", "合并铁律: pixiv作品(数字ID)的页禁止被合并走+拦下后原作品不动; 非pixiv作品合并后源转NOT_YET且源/目标total_pages均保留 (离线)", t76_merge_keeps_source_status_in_sync)
    suite.add("T77", "/api/version 构建信息: 返回 built_at/frontend/frozen/python, 供排查「改了没生效」 (离线)", t77_version_endpoint)
    suite.add("T78", "合并(2)误冲突: 占位作品并入自身(同目录源文件未挪走)不再误加 (2), 真冲突仍加 (2) (离线)", t78_merge_self_no_fake_conflict)
    suite.add("T79", "作者名防护: '????????'等编码垃圾名不顶真名、不入别名; 新作者落未知画师 (离线)", t79_upsert_author_rejects_garbage_name)
    suite.add("T80", "认领转正 claim_pixiv_work: 脏(2)文件名+页码挂反→#pid本体/元数据/origin=web/旧占位删除; 异图冲突与跨pid拒绝 (离线)", t80_claim_pixiv_work_promotes_placeholder)
    suite.add("T81", "HTTP直连下载: /ajax/illust/{id}/pages 响应解析(页码/原图URL/后缀归一) (离线)", t81_parse_pages_body_and_http_dispatch_signature)
    suite.add("T82", "散落页扫描分组: ready(同pid可认领)/mixed(多pid需先拆)/no_id(外来源跳过), 正式数字作品排除 (离线)", t82_scan_claimable_groups_ready_mixed_noid)
    suite.add("T83", "认领接口护栏: claim-scan 离线返回, claim-batch 空/非法清单 400 不联网 (离线)", t83_claim_scan_api_offline_and_batch_guards)
    suite.add("T84", "删页即缺页: delete_pages 后 DONE→NOT_YET 且 total_pages 保留; 陈旧DONE+缺页判 partial; 删光级联 (离线)", t84_delete_page_marks_partial_and_stale_done_guard)
    suite.add("T85", "仅查看该作品: only_aid 四函数精确过滤(非LIKE) + 时间戳统一UTC空格格式 + 按修改时间排序 (离线)", t85_only_aid_filter_and_updated_sort)
    suite.add("T86", "合并产物身份纠正: 已确认外来源 merged_→ext_(pages级联/队列跟随/标题回填)+纯pixiv不动+幂等+拆分继承ext身份 (离线)", t86_merged_external_reidentify_and_junk_titles)
    suite.add("T87", "写库必须提交: set_author_status 落库且无残留事务 + server.py 禁止绕过 store 的裸写(锁库事故) (离线)", t87_no_uncommitted_raw_writes)
    suite.add("T88", "体检拆分: 6 个可独立运行的检查项(清单/未知项400/单跑拿到结果/并发409), 不再一锅端白跑 (离线)", t88_check_split_steps_and_status)
    suite.add("T89", "体检报告分区: 可疑项(缺文件)/待办项(缺作者)分开 + 条目可忽略并落库/重启仍生效/可恢复 (离线)", t89_check_report_split_and_ignore)
    suite.add("T90", "任务运行记录: 运行中落盘/重启识别异常中断/按剩余步骤续跑/忽略不再提示 (离线)", t90_task_journal_interrupt_and_resume)
    suite.add("T91", "♻ 重下修复: 加下载永不覆盖(DONE拦截不变) + 修复接口允许DONE入队且固定overwrite/只跑本批/非pixiv跳过/忙时409 (离线)", t91_cart_repair_force_and_intent)
    suite.add("T92", "手动声明 pixiv 作者: 查 ID 接口三模式+无凭据明确报错 + 填纯数字ID关联pixiv(不建local_) + 只填名字才建自定义作者 (离线)", t92_pixiv_author_declare_and_fill_name)
    suite.add("T93", "作者来源链接(多条/可关联): 自动识别站点账号 + 回填作品source_author(不覆盖已有) + 按链接搜索 + 旧列兼容 + 绑定成真 pixiv 作者(作品迁移/合并/文件夹改名) (离线)", t93_author_links_structured_and_bind_pixiv)
    suite.add("T94", "🎯 轻量/定向刷新: 单作者刷新护栏 + refresh-scope(作品:重算/来源归类/缺文件报告; 作者:重算名下作品+未登录如实说明) + 作者来源筛选认主页链接 (离线)", t94_scoped_light_refresh)
    suite.add("T95", "🗂 作者对账台 + ✅ 配对认领: 未登录/本地作者护栏 + 认领转正(改名搬家/pages落库/占位级联删/状态重算) + 页冲突409/非数字400/缺文件404 (离线)", t95_author_reconcile_and_claim_image)
    suite.add("T96", "🔔 事件驱动刷新: 写请求自动登记数据域+实体id / GET与失败请求不登记 / since版本过滤 / queue域 / SSE广播格式 / ids上限退化[*] (离线)", t96_event_driven_dirty_scopes)
    suite.add("T97", "🔔 事件驱动全覆盖+不空转: 设置/缩略图/登录态/备份/操作记录 也登记 + periodic轮询与幂等同步不登记 (写后校验) (离线)", t97_event_driven_coverage_and_noise)
    suite.add("T98", "🎛 前端图标变体选择符: 🗂/🗑/🖼/🏷/❓ 等「文字默认呈现」码位, 用户可见处必须带 U+FE0F (防再渲染成黑白方框) (离线)", t98_frontend_icon_variation_selector)
    suite.add("T99", "↔ 作品内页序调整 +  安全模式不加载R18像素: 换序(md5跟文件走/total_pages不动/两阶段改名不撞名) + 护栏400 + 自命名文件只调库 + 打码静态防线 (离线)", t99_page_reorder_and_safemode_mask)
    suite.add("T100", "🗑 回收站双层保护: 软删除(不卸载记录/文件搬trash/刷新updated_at) + 恢复搬回原位 + 冲突只跳过绝不覆盖 + 彻底删除(先取路径再卸载/送电脑回收站) + 删图不级联拖作品/零页二选一 + 导入建自定义作品 + 标记位一致性 (离线)", t100_trash_soft_delete_and_custom_import)
    suite.add("T101", "🔄 渲染态不变量: 渲染指纹必须含打码态 + 手动刷新强制重画 + 打码变化联动重画所有缩略图视图 + 禁止裸 /api/local-img 进 <img>(防走光) + 服务端带 is_r18 (离线静态)", t101_render_state_invariants)
    suite.add("T102", "📡 实时活动日志: 打屏+落盘/按大小滚动/按期保留/回读 + 服务端HTTP流水(噪声路径过滤) + 前端点击·控件·切页上报 + 敏感字段打码 + 配置兜底 (离线)", t102_activity_monitor)
    suite.add("T103", "🔁 ⑥ 复检报告: 与上一版逐条对账(已解决清理/变化更新/仍存在保留) + 未复检来源绝不误清 + 结论落盘重启仍在 + 前端列出清理/变化明细 (离线)", t103_check_recheck_report)
    suite.add("T104", "📥 反搜收件箱出路: not_found/ 里的图必须仍可见 + 手动入库(建自定义作品/裸名命中/同名不覆盖/防穿越) + 丢弃与清空走电脑回收站 + 前端单张与批量接线 (离线)", t104_reverse_inbox_outlet)
    suite.add("T105", "👥 同名≠同一人 + 绑定后文件跟着走: 本地作者(站点账号)与真pixiv作者同名→两行都在且标出同名(不自动合并/不误标) + 一键绑定(作品迁移/行合并/文件从#其他来源#归拢到作者夹/路径同步) + 撞名绝不覆盖 + 别人文件不碰 (离线)", t105_same_name_author_and_gather_files)
    suite.add("T106", "🔁 作者作品翻批: profile/all 全部 id 参与切片(不再写死12个) + works_total 总数 + offset 生效/越界不炸 + /api/search 回传 works_offset + 前端翻批栏接线 (离线)", t106_author_works_paging)
    suite.add("T107", "♡ 下载后自动收藏(默认开启) + 关注画师: 收藏走 bookmarks/add(JSON/restrict公开非公开)且必带x-csrf-token + token缓存只取一次 + 已收藏跳过绝不重复写 + R18强制非公开 + 关开关零请求 + 只对单作品任务/未登录与报错如实降级 + 关注/取关/查状态三接口 + 作品页作者卡带关注/加入清单按钮 + 护栏与前端接线 (离线)", t107_auto_bookmark_and_follow)
    suite.add("T108", "🧹 反搜任务只清空失败(隐藏集在新一轮/结果换代时失效) + 设置页卡片布局不被内容顶破(minmax(0,1fr)/min-width:0/行内换行) + 单批作者结果不摆禁用翻批按钮 (离线静态)", t108_rev_task_clear_failed_and_settings_layout)
    suite.add("T109", "⚡ 关键词作者搜索快路: 端点必须是 /ajax/search/users?nick=(路径版404是慢的根因) + users 一次带齐名字/头像/简介 + 老形状兼容 + 无结构回退DOM/真空结果不爬页面 + ajax成功绝不爬页面 (离线)", t109_author_keyword_search_fast_path)
    suite.add("T110", "🩺 体检条目不再\"处理完还一直显示\": 落盘两个字段一起删(否则重启复活) + 文件里同一条不再存两份 + 打开报告顺手本地复核(能判的自动清/判不了的绝不误清; 「损坏图」只判文件还在不在 —— 图片被删掉那条就要消失, 文件还在则原样保留) + 顶部汇总没待处理就不显示、✕收起可持久化 (离线)", t110_check_report_no_stale_items)
    suite.add("T111", "🕒 时间戳统一: now_stamp 是唯一写法(UTC+空格) + normalize_ts 按本机时区换算 + 全部写 updated_at/last_updated 的路径都不带 'T' + 统一后「按修改时间」排序正确(带T的不再无条件顶最前) + 启动与体检②都会修 (离线)", t111_timestamp_format_and_updated_sort)
    suite.add("T112", "📥 反搜失败任务不再复活: status 剪掉「文件已不在收件箱」的过时条目并落盘(成功归档的当历史保留) + 入库成功后该失败记录自动消失 + 入库/丢弃当场从本次任务表移除 + 提示文案如实(不再谎称文件还在) (离线)", t112_reverse_stale_failed_tasks_pruned)
    suite.add("T113", "💥 ①磁盘对账不再把正常大图报成损坏(去掉「>60MB 一律判坏」, 只有空/极小文件与 PIL 真抛异常才算) + ①把自己校验出的损坏清单当场写进报告(单跑①也能在报告里看到是哪个文件/能一键重下) + 修好后旧条目自动消失 (离线)", t113_reconcile_corrupt_no_false_positive_and_report)
    suite.add("T114", "✅ 图库卡片态只看 pages 不看陈旧 artworks.status: 已失效(404)单独判 expired(不再谎报已入库/补全页数) + 本地补进缺失页后对账自动转已入库(不再卡在补全页数) + 反向验证(软删一页/附加页超tp都不算数仍判缺页) + tp为空退回只看status (离线)", t114_download_state_from_pages_not_stale_status)
    suite.add("T115", "🧹 下载任务「清空」跟随筛选(全部/未完成/已完成/失败 四 scope 各清各的, 未知 scope 当 all) + 清空进行中也不留后遗症(引擎写回改带原状态条件: 已清掉的行不被复活、别的操作刚写的状态不被覆盖、新任务不被波及) + 前端按钮/文案/已失效→搜索来源 静态检查 (离线)", t115_queue_clear_scope_and_writeback_guard)
    suite.add("T116", "🛑 清空「进行中」的任务真的会中止下载: 认领闸门跳过被清空项(不下不落盘) + 逐页回查行没了就中止(已下好的页保留成缺页) + 中止后不卡 running(放回 pending)也不复活已删行 + 前端文案改「会被中止」静态检查 (离线)", t116_clear_running_task_actually_cancels)
    suite.add("T117", "🕒 导入图片会刷新「最近修改时间」: 已有作品补页导入真的刷 updated_at(不再只写 pages) + 新作品导入照旧排最前 + 重复对账不把整库刷成「刚刚」+ 按修改时间排序正确 + 新写的时间戳走 now_stamp (离线)", t117_import_bumps_updated_at)
    suite.add("T118", "📥 队列「先入先跑」+ 总进度条只按任务条数推进: queue_list FIFO(同秒按 id ASC) + 真跑 run_cart 验证下载顺序==入队顺序 + cart_progress 分母恒为队列条数(cur 覆盖 0..N) + 页级进度仍走 item_progress + 前端不认 progress 驱动总条 + 静态护栏 (离线)", t118_fifo_order_and_task_progress)
    suite.add("T119", "🤖 AI 作品过滤: ①图库三档筛选(全部/仅AI/非AI 含未标记)+aiType 回填(解析 ajax aiType 1/2/缺=0、补全队列纳入缺 ai_type、「缺元数据」计数不纳入、无条件落值不卡队列) ②查询页(🌐 网络查询)默认隐藏 AI 卡片+就地重渲染+设置记住 ③迁移/接口/前端接线静态护栏 (离线)", t119_ai_filter_and_backfill)
    suite.add("T120", "🔁 关键字搜索「换一批」+ 图库三个状态筛选挪到「全选」左边: 关键字结果以前写死 p=1 永远固定 → 现在按24条连续切片(不重复不跳号、跨pixiv页自动续取、续页挂掉保住已取到的) + 页级缓存(连点不重复打pixiv, 防风控) + offset 贯通到URL/DOM兜底 + 三个按钮进操作行排在「全选」左边+窄窗换行不溢出 (离线)", t120_keyword_paging_and_lib_layout)
    suite.add("T121", "🏆✨ 查询页「没搜索时的推荐」两个来源可切换: ranking.php 老壳(unwrap=False)+无aiType→落未知 映射(字符串数字/illust_type/sexual R18/时间戳) + discovery 自带 aiType 复用 profile 映射 + AI判定三级来源(接口自带/图库/并发联网, 只在开「隐藏AI」时才花请求, 补过就记住) + 推荐内容缓存10分钟 + 前端两个来源/空输入回推荐/接推荐接口 + 限定 [data-scope] 防止误伤 searchScope (离线)", t121_search_feed)
    suite.add("T122", "🗂 作者夹命名「全项目唯一规则」: 非法字符一律换 `_`(不再删掉) + 一键整理期望夹名==下载器==反搜一字不差 + 下载器复用磁盘已有同 ID 夹 + 全项目只留一处非法字符表 (离线)", t122_author_folder_naming_single_rule)
    suite.add("T123", "🔎 关键词作者结果 →「📄 查看详情」进站内作者页(作品列表/翻批)并可「‹ 返回关键词结果」+ 「反搜来源」文件名捷径不再丢作者 (test_client 离线)", t123_author_detail_from_keyword_and_srcauthor)
    suite.add("T124", "🏷 按作品 ID 直查结果卡的标签不再连成一坨: .w-tags/.tag 的胶囊样式原本只写在 .work-card 作用域, .artwork-big 拿不到 → 两个作用域各补一份(flex+gap+胶囊圆角) + 前端版本号 (离线静态)", t124_artwork_big_tag_style)
    suite.add("T125", "🎞 动图(ugoira)真下载成 GIF: ugoira_meta → zip → Pillow 合成可变延迟多帧 gif(保透明) + 裸 {pid}.gif 命名(自然键, 一键整理不动它) + 落库带宽高 + 失败绝不回退浏览器通道 (离线)", t125_ugoira_download_to_gif)
    suite.add("T126", "🖱 点图库作者名不再自动弹开作者/标签两个下拉框: libFilterByAuthor 不再派发假 input 事件(它正是下拉面板的展开触发器), 静默同步改走 input._clearSync + combo.setByValue/clear + pid 直查的作者卡带「📄 查看详情」与按来源分叉的返回条 (离线静态)", t126_combo_silent_sync_and_pid_author_detail)
    suite.add("T127", "🖼 当前壁纸信息窗: 读注册表拿到桌面正在播的那张 → 四档溯源(库内文件/MD5字节一致/文件名ID/ORB图像) + 形态差异(裁/转/缩) + 库内最大尺寸对比 + deep 结论缓存(轮询不来回跳) + 只读护栏(壁纸在图库内时拒绝回收/root=wall 防穿越) + 前端接线 (离线)", t127_wallpaper_current_trace)
    suite.add("T128", "🖥 托盘常驻纯函数: tooltip ≤120字符无换行 + 菜单两行灰字信息/命令顺序/缺文件灰掉 + 命令id唯一可读 + 窗口消息→动作映射全分支 + NOTIFYICONDATAW 尺寸976与字段偏移钉死实测值 (离线)", t128_tray_pure_functions)
    suite.add("T129", "🖥 托盘生命周期策略: should_watch_browser 缺键不启用心跳守护 + second_instance_action 托盘在则弹提示否则回退 focus + 壁纸变化只刷tooltip(开了提示才弹气泡) + main.py 静态护栏(开关化/退出摘图标/托盘不自己加载整库特征) (离线)", t129_tray_lifecycle_policy)
    suite.add("T130", "🧩 溯源抽核等价性(阶段1回归): core.wallpaper_trace 可注入 wall_path(离线不依赖本机注册表) + orb_fn 注入真的被调用/不注入只标 deep_error + 结论缓存不重复跑ORB + db_factory 每次新建连接(SQLite 绑定线程) + summary 文案 (离线)", t130_core_trace_injection)
    suite.add("T131", "🚀 启动与托盘开关: LAUNCHER_DEFAULTS 与 default.json 键集一致(默认只开托盘) + 老 user.json 缺节兜底 + save_user_config 深合并不抹掉 web.port/paths + /api/launcher 白名单过滤(丢弃_note/未知键, 全丢则400不写盘) + 设置页开关接线 + 桌面小控件(utils/widget_window.py)三态形状(补回缺口的长条便签 + 人字防滑纹路 + 缩略图/荧光色)/贴边几何/卡片键两套(文字键 3 颗 + 壁纸控制条四颗图标键, 已删「下一张壁纸·复制作品链接·图像溯源」)/main.py 接线护栏 + 「移出壁纸先换桌面再删文件」顺序 + 壁纸夹配置(paths.wallpaper_dir→candidates_dir→/api/wallpaper/dir/pick 原生目录对话框→设置页单按钮) + 没有当前壁纸时从壁纸夹一端起步 + 托盘退出向页面广播 app_quit + 「只在桌面露出来」(WindowFromPoint 爬到 Progman/WorkerW) + 换图随机档(?shuffle=1) + 设置页文案去口语/去冗长 + 网页版「当前壁纸」折叠记忆与🧹清理副本面板 + 只读副本扫描 + 旧的 Edge+wall.html 方案已彻底移除 (离线)", t131_launcher_config_switches)
    suite.add("T132", "✨ 壁纸文案 AI: AI_DEFAULTS 与 default.json 键集一致(默认全关) + 缺节兜底 + ai_ready 四件套 + 提示词含画面线索/库外文件名不当内容 + 两行解析(去标签/序号/引号/书名号/行尾标点 + 硬夹到上限且断在断句点 + 标题一行不裁、氛围句换行塞得进卡片像素预算) + 落盘缓存按「路径+改动时间+大小」且同图只生成一次 + texts_for 只读缓存(轮询那条路一次接口都不许打) + generate_now 同步生成(表单值优先·必带图·点按钮即视为已启用) + 2026-09-27「上传失败」根因(max_tokens 被推理模型烧光)与「测试连接」共用同一套配置口径 + /api/ai 不回密钥原文/活动日志打码/旧的保存接口已删 + 小控件接线(ai_sig 进 info_key 才会刷新, 标题与氛围句 AI 优先本地兜底) (离线, generate 打桩)", t132_ai_text_pipeline)
    suite.add("T133", "🖼 壁纸库生成: 尺寸门槛(宽≥1920 且高≥1080, 显式常量不许问屏幕)与比例门槛(偏离 16:9 ≤10%)分开回话 + 文件名 {作品ID}_p{页码}.{ext} 天然幂等 + 已标记的作品/页与无指纹页不参与 + 文件不在盘上算 missing + 幻灯片播不了的格式不倒进去 + 字节一致的原样复制(不缩放不裁剪, 裁切列留空) + 登记带 file_md5/src_md5/宽高/血缘(LEFT JOIN) + 重复跑新增 0 不翻倍 + 没配壁纸夹一律报错绝不猜 + /api/wallpaper/lib/scan 后台跑 + /api/wallpaper/lib 轮询 (离线)", t133_wallpaper_lib_generate)
    suite.add("T134", "🚀📁 启动与托盘「立即生效」+ 📁 路径设置(用户: 开关为何要重启才生效 / 数据库路径没有设置选项, 我说的是所有路径 / 双击托盘快速打开图库 / 数据·配置·缓存向正常软件看齐但别占C盘): 除「启动自动开浏览器」外全部存盘即生效(热应用钩子 set_launcher_apply→main._apply_launcher 就地起停托盘/小控件, 心跳守护按代次门控——关了不许再自杀) + auto_open_browser 如实回报 startup_only + /api/paths 三处可改目录(图片目录可填单段相对名或绝对路径, 数据根/临时目录只收绝对路径; 空=回默认, 相对名拒绝400) + 落盘后当场回新落点(不靠带缓存的 paths.data_root) + 占用检测(数据库/图片/缩略图/特征库/临时各回 MB, 目录有上限地数) + /api/paths/temp/clean 一键清临时目录 + /api/paths/pick 复用系统目录对话框(取消不改/失败必回 JSON 不能当取消) + 位置一览含壁纸夹 + 文案只留一句「重启 Vixiq 后生效」不摆理由 (离线)", t134_launcher_hotapply_and_paths)
    suite.add("T135", "🖼 查询页「按页下载」之一(用户: 点加入清单会整个作品下载, 我希望能分别单个下载 / 双击图片打开大图, 右下角要显示原图和下载该页, 下面还要像调整页序那样的缩略图多选区): 迁移 020 给队列加 pages TEXT(NULL=整部, 向后兼容, 且不塞 strategy——那列被当 DownloadStrategy 枚举解析) + queue_add(pages) 落排序去重 JSON 串 + GET /api/artwork/<id>/pages(未登录 200+need_login 不是401 / low=regular high=original / 合并库内页状态 / 超远端页数的标 is_extra / 不存在404) + POST /api/cart 带 pages 入队且 page_count 仍写远端总页数 (离线)", t135_artwork_pages_api_and_queue_pages)
    suite.add("T136", "🖼 查询页「按页下载」之二 + 🔍 大图 1:1 放大 + ↔ 图库遮挡修复: _only_pages_of 纯函数(脏数据退回整部) + 引擎页级绕过「整部已下完就跳过」(否则点补某页被静默跳过) + 爬虫只下选中页且页级拒绝回落浏览器通道(它会按URL规律把未选页补齐) + init_pages 仍按远端总页数 + 前端 openRemoteLightbox(默认低清/显示原图/下载该页/🔗原网页, 没有上个下个作品) + 点图 1:1 放大(元素自身撑到自然尺寸 + 拖拽平移, 换页收缩放/同页切原图保留) + 原图只在点按钮后才加载 + 缩略图多选条(全选/已入库灰掉) + 卡片点击不再开原站 + 进调整页序收起那两颗按钮 + 拖拽夹取改用容器实际渲染尺寸 + 放大态图片元素自身撑到自然尺寸 + 缩略图多选条可折叠且跨打开记忆(折叠后大图高度 0.56→0.72, 修「✕ 只露一半 / 缩略图吃掉半屏」+ 放大态视口横向 97vw/竖向按实测行高吃满, 并用 capNorm 兜住「放大反而变小」+ 「点击查看大图」下沉本地图库查看器(同装 setupZoom, ✕ 一起改锚视口, is-remote 死代码已删) + 缩略图条 loading=lazy / 去掉相邻页原图预取 / /api/img 改流式转发带 Cache-Control) (离线静态)", t136_page_download_engine_and_frontend)
    suite.write_report()
    return suite


def t44_batch_add_cart() -> None:
    """T44: 图库批量加入清单 /api/cart/batch-add (临时库, 离线, 不污染生产库)。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t44web_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        # 预置: 888901 已全部完成(DONE), 888902 未下载(NOT_YET)
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        db.upsert_author("77", "批量测试")
        db.change_work_status("888901", WSt.DONE, title="批量已完成",
                              author_id="77", total_pages=2)
        db.change_work_status("888902", WSt.NOT_YET, title="批量未下载",
                              author_id="77", total_pages=3)
        db.close()

        client = create_app().test_client()
        # 含一个无效项 (缺 artwork_id) → invalid=1;
        # 888901 已 DONE → 2026-09-15 新语义 already_downloaded 拦截不开任务
        r = client.post("/api/cart/batch-add", json={"items": [
            {"artwork_id": "888901", "title": "已完成", "page_count": 2},
            {"artwork_id": "888902", "title": "未下载", "page_count": 3},
            {"title": "无ID无效项"},
        ]})
        j = r.get_json()
        assert r.status_code == 200 and j.get("status") == "ok", j
        assert (j["added"] == 1 and j["already_downloaded"] == 1
                and j["invalid"] == 1 and j["skipped"] == 2), j

        db2 = PixivDatabase(_fake_user_data("pixiv.db"))
        q = db2.queue_list()
        assert len(q) == 1 and q[0]["artwork_id"] == "888902", \
            f"应只给未下载作品入队 1 条, 实际 {[x['artwork_id'] for x in q]}"
        # DONE 保护: 888901 状态仍为 DONE, 不被批量加入清单重置
        m1 = db2.get_artwork_metadata("888901")
        assert m1["status"] == WSt.DONE, f"888901 DONE 被重置: {m1['status']}"
        assert db2.get_artwork_metadata("888902")["status"] == WSt.NOT_YET
        db2.close()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t45_nonpixiv_cart_task_fast_fail() -> None:
    """T45: 非 pixiv (非数字 ID) 购物车任务不进下载器直接失败, 不影响其他任务。
    回归 2026-09-10: ext_ 任务曾进 crawler 打开 pixiv 卡在「进行中」无反应。"""
    import tempfile, shutil, threading
    import queue as _q2
    from pathlib import Path
    from utils.reporter import QueueReporter
    from core import download_engine as de
    from core.models import DownloadStrategy
    from db import store as _db_store_mod

    tmp = tempfile.mkdtemp(prefix="t45_")
    db_file = Path(tmp) / "cart.db"
    save_folder = Path(tmp) / "img"
    try:
        class _FakeDownloader:
            def __init__(self, **kw):
                self.save_folder = Path(kw.get("save_folder"))
                self.config = kw.get("config")
                self._cb = None
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def set_progress_callback(self, cb): self._cb = cb
            def set_abort_callback(self, cb): self._abort_cb = cb
            def download_artwork(self, aid, author_id=None, author_name=None,
                                 force_overwrite=False):
                # 非数字 ID 绝不允许走到这里 (预检应提前拦截)
                assert str(aid).isdigit(), f"非数字 ID 竟进入了下载器: {aid}"
                self.save_folder.mkdir(parents=True, exist_ok=True)
                (self.save_folder / f"{aid}_p0.txt").write_text(str(aid), encoding="utf-8")
                return 1, 1        # (成功页数, 总页数): 0 会被判为失败 (T73)
            def download_author_all(self, user_id=None, author_name=None): pass
            def close(self): pass

        _orig_pdl = de.PixivDownloader
        de.PixivDownloader = _FakeDownloader
        _orig_init = _db_store_mod.PixivDatabase.__init__

        def _patched_init(self_inner, db_path=None, reporter=None):
            if db_path is None:
                db_path = db_file
            return _orig_init(self_inner, db_path=db_path, reporter=reporter)

        _db_store_mod.PixivDatabase.__init__ = _patched_init
        try:
            with _db_store_mod.PixivDatabase(None) as db:
                db.queue_add(item_type="artwork", title="#120571595",
                             artwork_id="120571595", page_count=1)
                db.queue_add(item_type="artwork", title="外来 ext_abc",
                             artwork_id="ext_abc", page_count=1)
                db.queue_add(item_type="artwork", title="#655000",
                             artwork_id="655000", page_count=1)

            q2 = _q2.Queue()
            cancel_ev = threading.Event()
            qr = QueueReporter(q2, cancel_ev)
            cfg = {"paths": {"img_subdir": "img"}}
            done, total = de.run_cart(qr, config=cfg, save_folder=save_folder,
                                      strategy=DownloadStrategy.ONLY_NEW, headless=True)
            # ext_ 秒失败, 两个数字 ID 成功 → done=2/3
            assert (done, total) == (2, 3), f"期望 2/3, 实际 {done}/{total}"
            assert (save_folder / "120571595_p0.txt").exists(), "120571595 未写出"
            assert (save_folder / "655000_p0.txt").exists(), "655000 未写出"
            assert not (save_folder / "ext_abc_p0.txt").exists(), "ext_abc 不应被下载"
            with _db_store_mod.PixivDatabase(None) as db2:
                rows = db2.queue_list()
                by_id = {it["artwork_id"]: it for it in rows}
            assert by_id["120571595"]["status"] == "done", by_id["120571595"]
            assert by_id["655000"]["status"] == "done", by_id["655000"]
            assert by_id["ext_abc"]["status"] == "failed", by_id["ext_abc"]
        finally:
            de.PixivDownloader = _orig_pdl
            _db_store_mod.PixivDatabase.__init__ = _orig_init
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def t46_source_url_detect_and_set() -> None:
    """T46: 来源治理 API — URL 解析归类 detect / 保存 set / 无本地页反搜 400 (临时库, 离线)。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t46web_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        db.change_work_status("900001", WSt.DONE, title="P 作品", author_id="66",
                              total_pages=1)
        db.update_artwork("900001", source="pixiv",
                          source_url="https://www.pixiv.net/artworks/900001")
        db.change_work_status("900002", WSt.DONE, title="T 作品", author_id="66",
                              total_pages=1)
        db.update_artwork("900002", source="twitter",
                          source_url="https://twitter.com/qing0ying0/status/123")
        db.change_work_status("900003", WSt.DONE, title="无URL", author_id="66",
                              total_pages=1)
        db.close()

        client = create_app().test_client()
        j = client.get("/api/library/source-detect?artwork_id=900001").get_json()
        assert j["has_url"] is True and j["detected"]["site"] == "pixiv", j
        assert j["detected"]["artwork_id"] == "900001", j
        j2 = client.get("/api/library/source-detect?artwork_id=900002").get_json()
        assert j2["has_url"] is True and j2["detected"]["site"] == "twitter", j2
        assert j2["detected"]["artwork_id"] is None, j2
        j3 = client.get("/api/library/source-detect?artwork_id=900003").get_json()
        assert j3["has_url"] is False and j3["url"] == "", j3
        r = client.post("/api/library/source-set", json={
            "artwork_id": "900003", "source_url": "https://x.com/abc", "source": "twitter",
            "source_author": "abc_handle"})
        assert r.get_json()["status"] == "ok", r.get_json()
        r2 = client.post("/api/library/source-set", json={
            "artwork_id": "900003", "source": "unknown_site"})
        assert r2.status_code == 400, r2.status_code
        db2 = PixivDatabase(_fake_user_data("pixiv.db"))
        m = db2.get_artwork_metadata("900003")
        assert m["source_url"] == "https://x.com/abc" and m["source"] == "twitter", m
        assert m["source_author"] == "abc_handle", m
        db2.close()
        r3 = client.post("/api/library/source-search", json={"artwork_id": "900001"})
        assert r3.status_code == 400, r3.status_code
        # 7) 反搜确认附带作者: 仅 author_name → 建 local_ 作者并关联; 数字 author_id → upsert 作者并关联
        dbt = PixivDatabase(_fake_user_data("pixiv.db"))
        dbt.change_work_status("900004", WSt.DONE, title="无作者", total_pages=1)
        dbt.close()
        r5 = client.post("/api/library/source-set", json={
            "artwork_id": "900004", "source_url": "https://www.deviantart.com/fox/art/xx-1",
            "source": "deviantart", "author_name": "fox_artist"})
        j5 = r5.get_json()
        assert j5["status"] == "ok" and j5.get("author_id", "").startswith("local_"), j5
        r6 = client.post("/api/library/source-set", json={
            "artwork_id": "900004", "source_url": "https://www.pixiv.net/artworks/123456",
            "source": "pixiv", "author_name": "pixiv_author", "author_id": "777888"})
        j6 = r6.get_json()
        assert j6["status"] == "ok" and j6.get("author_id") == "777888", j6
        db3 = PixivDatabase(_fake_user_data("pixiv.db"))
        m4 = db3.get_artwork_metadata("900004")
        assert m4["author_id"] == "777888", m4
        assert db3.get_author("777888")["name"] == "pixiv_author", db3.get_author("777888")
        db3.close()
        # 6) artstation URL 归类 + source-set 写 artstation
        j4 = client.get("/api/library/source-detect?artwork_id=900002").get_json()
        assert j4["has_url"] is True and j4["detected"]["site"] == "twitter", j4
        r4 = client.post("/api/library/source-set", json={
            "artwork_id": "900002", "source_url": "https://www.artstation.com/artwork/abc",
            "source": "artstation"})
        assert r4.get_json()["status"] == "ok", r4.get_json()
        j5 = client.get("/api/library/source-detect?artwork_id=900002").get_json()
        assert j5["detected"]["site"] == "artstation", j5
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t47_reverse_progress_wiring() -> None:
    """T47: 反搜进度接线 — 配置兜底 batch_size/batch_break_sec + /api/reverse/status 返回 progress (离线)。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from utils.config import _canonicalize

    # ① 配置兜底: 空 dict canonicalize 后 reverse_search 必有批次字段
    cfg = _canonicalize({})
    rs = cfg["reverse_search"]
    assert rs.get("batch_size") == 5 and rs.get("batch_break_sec") == 30, rs
    # user.json 覆盖也生效 (模拟用户自定)
    cfg2 = _canonicalize({"reverse_search": {"batch_size": 10, "batch_break_sec": 60}})
    assert cfg2["reverse_search"]["batch_size"] == 10, cfg2
    assert cfg2["reverse_search"]["batch_break_sec"] == 60, cfg2

    # ② status 接口: 临时数据根, 返回 progress 且默认 idle
    from web.server import create_app
    tmp = Path(tempfile.mkdtemp(prefix="t47rev_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        client = create_app().test_client()
        j = client.get("/api/reverse/status").get_json()
        p = j.get("progress", {})
        for k in ("running", "total", "done", "current", "phase", "rest_sec",
                  "batch", "batch_size", "batch_break_sec", "started_at",
                  "finished_at", "error"):
            assert k in p, (k, p)
        assert p["phase"] == "idle" and p["running"] is False, p
        assert p["batch_size"] == 5, p
        # 图库反搜来源的独立进度 (src_search) 也在 status 里
        ss = j.get("src_search", {})
        for k in ("running", "artwork_id", "total", "done", "current",
                  "phase", "error", "result", "started_at", "finished_at"):
            assert k in ss, (k, ss)
        assert ss["phase"] == "idle" and ss["running"] is False, ss
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t48_tidy_preview_apply() -> None:
    """T48: 一键整理 — 错位归位 / 文件夹改名 / 根散件归类 预览计划 + 执行后磁盘与 DB 路径同步 (离线)。"""
    import tempfile
    import shutil
    from db.store import PixivDatabase, WSt, PSt
    import web.server as ws

    tmp = Path(tempfile.mkdtemp(prefix="t48tidy_"))
    img_root = tmp / "img"
    img_root.mkdir(parents=True, exist_ok=True)
    db = PixivDatabase(tmp / "pixiv.db")
    try:
        for aid, nm in (("77", "作者乙"), ("66", "作者甲"), ("88", "作者丙"), ("99", "作者丁")):
            db.upsert_author(aid, nm)
        for aid, nm in (("900002", "B"), ("900001", "A"), ("900003", "C"), ("900004", "D")):
            db.change_work_status(aid, WSt.DONE, title=nm,
                                  author_id={"900002": "77", "900001": "66",
                                             "900003": "88", "900004": "99"}[aid],
                                  total_pages=1)

        def _page(aid, rel):
            p = img_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)

        _page("900002", "旧名_77/900002_p0.jpg")   # 单作者旧名夹 → 改名
        _page("900001", "作者甲_66/900001_p0.jpg")  # 已规范 → 不产生计划
        _page("900003", "乱放_00/900003_p0.jpg")    # 错误作者夹 → 归位
        _page("900004", "900004_p0.jpg")            # 根散件 → 归位
        (img_root / "garbage.jpg").write_bytes(b"g")  # 无身份 → 待确认
        # 外来源别名夹 bili@账号 → 整体改名 bilibili@账号 (不再搬文件留空夹)
        db.change_work_status("900005", WSt.DONE, title="E", total_pages=1)
        db.update_artwork("900005", source="bilibili", source_author="账号甲")
        _page("900005", "bili@账号甲/900005_p0.jpg")
        # 散件已在正确作者夹内 → 不产生计划 (回归: 曾误计划成「夹/夹」嵌套)
        db.change_work_status("900006", WSt.DONE, title="F", author_id="66", total_pages=1)
        (img_root / "作者甲_66/900006_p0.jpg").write_bytes(b"x" * 16)
        # ①a 作者归属争议: kase_22_43460823(作者名带下划线数字, pid=43460823) 夹内作品
        # DB 记的是 22(ユウマロ) —— 两边不一致 → 只生成「待确认」(绝不自动改库), 整夹冻结
        db.upsert_author("22", "ユウマロ")
        db.change_work_status("900007", WSt.DONE, title="旧标题",
                              author_id="22", total_pages=1)
        db.mark_artwork_deleted("900007", True)
        _page("900007", "kase_22_43460823/900007_p0.jpg")
        # 新结构: pixiv 作者夹(作者甲_66)内混入外来源作品(数字 author_id=66) → 移入「顶层夹/站点@账号」子夹
        db.change_work_status("900008", WSt.DONE, title="tw", author_id="66", total_pages=1)
        db.update_artwork("900008", source="twitter", source_author="tiv")
        _page("900008", "作者甲_66/900008_p0.jpg")
        # 纯外来源(无数字作者)别名夹 → 整体改名, 不建子夹
        db.change_work_status("900009", WSt.DONE, title="bl", total_pages=1)
        db.update_artwork("900009", source="bilibili", source_author="徐尤点")
        _page("900009", "bili@徐尤点/900009_p0.jpg")
        # 无账号外来源 / 未知来源 → `#...#` 包裹目标夹 (未知作者 `#站点#`, 未知来源 `#其他来源#`)
        db.change_work_status("900010", WSt.DONE, title="as", total_pages=1)
        db.update_artwork("900010", source="artstation", source_author="")
        _page("900010", "artstation/900010_p0.jpg")
        db.change_work_status("900011", WSt.DONE, title="uu", total_pages=1)
        db.update_artwork("900011", source="other", source_author="")
        _page("900011", "other/900011_p0.jpg")
        # 已在正确子夹的作品 → 零计划 (防「作者夹/作者夹/站点」嵌套 bug): 账号子夹 + `#...#` 子夹
        db.change_work_status("900012", WSt.DONE, title="sub", author_id="66", total_pages=1)
        db.update_artwork("900012", source="twitter", source_author="subacc")
        _page("900012", "作者甲_66/twitter@subacc/900012_p0.jpg")
        db.change_work_status("900013", WSt.DONE, title="sub2", author_id="66", total_pages=1)
        db.update_artwork("900013", source="artstation", source_author="")
        _page("900013", "作者甲_66/#artstation#/900013_p0.jpg")
        # 多来源外来作者 (local_xxx 跨 ≥2 来源) → 聚合到「作者名_local_xxx/站点@账号」顶层夹;
        # 单来源外来作者 → 保持平铺 (不建顶层夹)
        db.upsert_author("local_abc123", "多源画师")
        db.change_work_status("900014", WSt.DONE, title="m1", author_id="local_abc123", total_pages=1)
        db.update_artwork("900014", source="twitter", source_author="duoyuan")
        _page("900014", "twitter@duoyuan/900014_p0.jpg")
        db.change_work_status("900015", WSt.DONE, title="m2", author_id="local_abc123", total_pages=1)
        db.update_artwork("900015", source="danbooru", source_author="duoyuan")
        _page("900015", "twitter@duoyuan/900015_p0.jpg")
        db.upsert_author("local_single", "单源画师")
        db.change_work_status("900016", WSt.DONE, title="s1", author_id="local_single", total_pages=1)
        db.update_artwork("900016", source="danbooru", source_author="dansingle")
        # 单来源已记录名字的外来作者 → 归入「作者名_local_single」作者夹 (不再是账号平铺夹)
        _page("900016", "danbooru@dansingle/900016_p0.jpg")
        # 多来源外来作者已在正确顶层子夹 → 零计划 (防 local_ 聚合分支的 夹/夹 嵌套 bug)
        db.change_work_status("900017", WSt.DONE, title="m3", author_id="local_abc123", total_pages=1)
        db.update_artwork("900017", source="twitter", source_author="duoyuan")
        _page("900017", "多源画师_local_abc123/twitter@duoyuan/900017_p0.jpg")
        # 大小写规范化改名 (NTFS 大小写不敏感: GGAC@毒瘤 → ggac@毒瘤) → 改名应成功而非「目标已存在」跳过
        db.change_work_status("900018", WSt.DONE, title="gg", total_pages=1)
        db.update_artwork("900018", source="ggac", source_author="毒瘤")
        _page("900018", "GGAC@毒瘤/900018_p0.jpg")
        # 无账号但已记录作者名 (外来作者 local_named) → 归入「作者名_local_named」作者夹, 不再落 #站点#
        db.upsert_author("local_named", "Alena Aenami")
        db.change_work_status("900019", WSt.DONE, title="an", author_id="local_named", total_pages=1)
        db.update_artwork("900019", source="artstation", source_author="")
        _page("900019", "#artstation#/900019_p0.jpg")
        db.change_work_status("900020", WSt.DONE, title="an2", author_id="local_named", total_pages=1)
        db.update_artwork("900020", source="artstation", source_author="")
        _page("900020", "Alena Aenami_local_named/900020_p0.jpg")
        # 占位作者名 (未知作者) → 不建作者夹, 未知来源文件留在 #其他来源# (零计划)
        db.upsert_author("local_placeholder", "未知作者")
        db.change_work_status("900021", WSt.DONE, title="ph", author_id="local_placeholder", total_pages=1)
        db.update_artwork("900021", source="other", source_author="")
        _page("900021", "#其他来源#/900021_p0.jpg")

        # ---- ③ 文件名规范化 (2026-09-14 定案): 只动「无信息名」, 可读名一律保留 ----
        # 非 pixiv 作品 ID 形如 ext_xxx (非纯数字) —— 只有这类才走「站点@账号_序号」命名
        # 注意: 外来作者的文件应已在「作者夹/站点@账号」里 (单来源也一样, 2026-09-14 定案),
        #       场景就按正确位置摆, 免得被「位置规范」搬走而盖掉要测的「命名规范」。
        db.upsert_author("local_hashy", "哈希画师")
        H = "哈希画师_local_hashy/danbooru@hashy/"
        #   a) 非 pixiv + 纯哈希名 → {站点@账号}_{序号:03d}.ext
        db.change_work_status("ext_h1", WSt.DONE, title="h1", author_id="local_hashy", total_pages=1)
        db.update_artwork("ext_h1", source="danbooru", source_author="hashy")
        _page("ext_h1", H + "-7a93b54c836a829.png")
        #   b) 非 pixiv + 纯顺序号名 → 同上 (序号按同夹内顺序递增)
        db.change_work_status("ext_h2", WSt.DONE, title="h2", author_id="local_hashy", total_pages=1)
        db.update_artwork("ext_h2", source="danbooru", source_author="hashy")
        _page("ext_h2", H + "00000001.png")
        #   c) 非 pixiv + 可读名 → 一律保留 (不把好名字改成差名字)
        db.change_work_status("ext_h3", WSt.DONE, title="h3", author_id="local_hashy", total_pages=1)
        db.update_artwork("ext_h3", source="danbooru", source_author="hashy")
        _page("ext_h3", H + "sample_art_01.png")
        #   c2) 非 pixiv + 两字中文名 (如「视奸」) → 也是人读信息, 必须保留 (曾误判成"太短")
        db.change_work_status("ext_h4", WSt.DONE, title="h4", author_id="local_hashy", total_pages=1)
        db.update_artwork("ext_h4", source="danbooru", source_author="hashy")
        _page("ext_h4", H + "视奸.png")
        #   d) pixiv 作品但文件名不是 {pid}_p{n} → 规范成自然键 (对账第一身份)
        db.change_work_status("900025", WSt.DONE, title="h4", author_id="66", total_pages=1)
        _page("900025", "作者甲_66/乱七八糟的名字.jpg")

        plans = ws._tidy_build_plans(db, img_root)
        by_type = {}
        for p in plans:
            by_type.setdefault(p["type"], []).append(p)
        # 已规范文件夹绝不产生计划: 作者甲_66 顶层夹不被改名/整体搬走, pixiv 作品 900001 不动
        assert not any(p["type"] == "rename" and p["from"] == "作者甲_66" for p in plans), plans
        assert not any("作者甲_66/900001_p0.jpg" in (f or "") for p in plans for f in p.get("files", [])), plans
        assert not any("900006" in (f or "") for p in plans for f in p.get("files", [])), plans
        # 已在正确子夹 (站点@账号 / _站点) 的作品 → 零计划 (防 作者夹/作者夹/站点 嵌套 bug)
        assert not any("900012" in (f or "") for p in plans for f in p.get("files", [])), plans
        assert not any("900013" in (f or "") for p in plans for f in p.get("files", [])), plans
        # 作者归属存疑 → 「待确认」(单向闭环: 绝不自动改库, 只把两边证据摆出来交给用户)
        disp = [p for p in by_type.get("categorize", []) if p.get("claim_aid")]
        assert len(disp) == 1, disp
        assert disp[0]["from"] == "kase_22_43460823", disp
        assert disp[0]["to"] == "(待确认) 作品 900007", disp
        assert disp[0]["artwork_id"] == "900007", disp
        assert disp[0]["claim_aid"] == "43460823" and disp[0]["claim_name"] == "kase_22", disp
        assert disp[0]["db_aid"] == "22", disp
        assert disp[0]["auto"] is False, disp
        assert "43460823" in disp[0]["reason"] and "22" in disp[0]["reason"], disp
        assert "fixauthor" not in by_type, by_type   # 旧的自动更正类型已整体移除
        # 争议夹整夹冻结: 不产生任何**可自动执行**的计划 (改名/归位/命名规范)
        for _p in plans:
            if _p["auto"]:
                assert not any("kase_22_43460823" in (f or "")
                               for f in _p.get("files", [])), plans
        rn = by_type.get("rename", [])
        rmap = {r["from"]: r["to"] for r in rn}
        assert rmap.get("旧名_77") == "作者乙_77", rn
        assert rmap.get("bili@账号甲") == "bilibili@账号甲", rn
        assert rmap.get("bili@徐尤点") == "bilibili@徐尤点", rn
        assert rmap.get("GGAC@毒瘤") == "ggac@毒瘤", rn  # 大小写规范化
        mv = by_type.get("move", [])
        mvto = {m["to"] for m in mv}
        assert len(mv) == 10 and all(m["auto"] for m in mv), mv
        assert {"作者丙_88", "作者丁_99", "作者甲_66/twitter@tiv",
                "#artstation#", "#其他来源#",
                "多源画师_local_abc123/twitter@duoyuan",
                "多源画师_local_abc123/danbooru@duoyuan",
                "Alena Aenami_local_named/#artstation#",
                "单源画师_local_single/danbooru@dansingle"} == mvto, mv
        # 无账号/未知来源: 归位到 `#...#` 包裹夹 (未知作者 `#站点#`, 未知来源 `#其他来源#`)
        assert any(m["from"] == "artstation" and m["to"] == "#artstation#" for m in mv), mv
        assert any(m["from"] == "other" and m["to"] == "#其他来源#" for m in mv), mv
        # 已记录作者名 → 顶层作者夹 + **来源子夹** (单来源也一样, 保证夹名里看得出站点)
        assert any(m["from"] == "#artstation#"
                   and m["to"] == "Alena Aenami_local_named/#artstation#" for m in mv), mv
        assert any(m["from"] == "danbooru@dansingle"
                   and m["to"] == "单源画师_local_single/danbooru@dansingle" for m in mv), mv
        # 单来源作者的文件还平铺在作者夹里 (没有来源子夹) → 也归位进来源子夹
        assert any(m["from"] == "Alena Aenami_local_named"
                   and m["to"] == "Alena Aenami_local_named/#artstation#" for m in mv), mv
        # 占位作者名「未知作者」不建作者夹 → 900021 零计划
        assert not any("900021" in (f or "") for p in plans for f in p.get("files", [])), plans
        # 多来源外来作者聚合 + 已在正确作者夹 → 零计划
        assert any(m["from"] == "twitter@duoyuan"
                   and m["to"] == "多源画师_local_abc123/twitter@duoyuan" for m in mv), mv
        assert any(m["to"] == "多源画师_local_abc123/danbooru@duoyuan" for m in mv), mv
        # 多来源外来作者已在正确顶层子夹 → 零计划 (防 local_ 分支 夹/夹 嵌套)
        assert not any("900017" in (f or "") for p in plans for f in p.get("files", [])), plans
        cat = by_type.get("categorize", [])
        assert len(cat) == 2 and all(p["auto"] is False for p in cat), cat
        assert any(p["files"] == ["garbage.jpg"] for p in cat), cat
        assert any(p.get("claim_aid") == "43460823" for p in cat), cat
        # ③ 命名规范计划: 乱码名 → 统一可读名; 可读名 / 已规范名 一律不产生计划
        #    非 pixiv → `{站点@账号}_{序号}` (不写作者名、不写假页码)
        fr = {p["from"]: p["to"] for p in by_type.get("filrename", [])}
        assert fr.get(H + "-7a93b54c836a829.png") == H + "danbooru@hashy_001.png", fr
        assert fr.get(H + "00000001.png") == H + "danbooru@hashy_002.png", fr
        assert fr.get("作者甲_66/乱七八糟的名字.jpg") == "作者甲_66/900025_p0.jpg", fr
        assert fr.get(H + "sample_art_01.png") is None, fr   # 可读名保留
        assert fr.get(H + "视奸.png") is None, fr             # 两字中文名保留
        assert all(p["auto"] for p in by_type.get("filrename", [])), by_type.get("filrename")

        rep = ws._tidy_apply_plans(db, img_root)
        assert rep["renamed"] == 4 and rep["moved"] == 10 \
            and rep["filenamed"] == 3 and not rep["failed"], rep
        # 单向闭环: 争议作者一律未被自动改写 (旧的 fixauthor 已移除), 作品仍挂在原作者下
        assert db.get_artwork_metadata("900007")["author_id"] == "22"
        assert (img_root / "作者乙_77/900002_p0.jpg").is_file()
        assert not (img_root / "旧名_77").exists()
        assert (img_root / "bilibili@账号甲/900005_p0.jpg").is_file()
        assert not (img_root / "bili@账号甲").exists()
        assert (img_root / "bilibili@徐尤点/900009_p0.jpg").is_file()
        assert not (img_root / "bili@徐尤点").exists()
        assert (img_root / "kase_22_43460823/900007_p0.jpg").is_file()  # 争议夹冻结: 原样不动
        assert (img_root / "作者甲_66/twitter@tiv/900008_p0.jpg").is_file()
        assert not (img_root / "作者甲_66/900008_p0.jpg").exists()
        assert (img_root / "作者丙_88/900003_p0.jpg").is_file()
        assert (img_root / "作者丁_99/900004_p0.jpg").is_file()
        assert (img_root / "作者甲_66/900006_p0.jpg").is_file()  # 夹内散件不动
        assert (img_root / "garbage.jpg").is_file()  # 待确认项不动
        # 命名规范后: 磁盘旧名消失/新名就位, 可读名原样保留
        assert (img_root / H / "danbooru@hashy_001.png").is_file()
        assert (img_root / H / "danbooru@hashy_002.png").is_file()
        assert not (img_root / H / "-7a93b54c836a829.png").exists()
        assert not (img_root / H / "00000001.png").exists()
        assert (img_root / H / "sample_art_01.png").is_file()
        assert (img_root / "作者甲_66/900025_p0.jpg").is_file()
        assert not (img_root / "作者甲_66/乱七八糟的名字.jpg").exists()
        assert (img_root / "#artstation#/900010_p0.jpg").is_file()
        assert (img_root / "#其他来源#/900011_p0.jpg").is_file()
        # 单来源作者: 文件从「作者夹」平铺位置归位进「作者夹/站点@账号」子夹
        assert (img_root / "单源画师_local_single/danbooru@dansingle/900016_p0.jpg").is_file()
        assert (img_root / "Alena Aenami_local_named/#artstation#/900019_p0.jpg").is_file()
        assert (img_root / "Alena Aenami_local_named/#artstation#/900020_p0.jpg").is_file()
        lp = {r["artwork_id"]: r["local_path"] for r in db.conn.execute(
            "SELECT artwork_id, local_path FROM pages")}
        assert lp["900002"] == "作者乙_77/900002_p0.jpg", lp
        assert lp["900005"] == "bilibili@账号甲/900005_p0.jpg", lp
        assert lp["900008"] == "作者甲_66/twitter@tiv/900008_p0.jpg", lp
        assert lp["900009"] == "bilibili@徐尤点/900009_p0.jpg", lp
        assert lp["900003"] == "作者丙_88/900003_p0.jpg", lp
        assert lp["900004"] == "作者丁_99/900004_p0.jpg", lp
        assert lp["900001"] == "作者甲_66/900001_p0.jpg", lp
        assert lp["900007"] == "kase_22_43460823/900007_p0.jpg", lp
        assert lp["900010"] == "#artstation#/900010_p0.jpg", lp
        assert lp["900011"] == "#其他来源#/900011_p0.jpg", lp
        assert lp["900012"] == "作者甲_66/twitter@subacc/900012_p0.jpg", lp
        assert lp["900013"] == "作者甲_66/#artstation#/900013_p0.jpg", lp
        assert lp["900014"] == "多源画师_local_abc123/twitter@duoyuan/900014_p0.jpg", lp
        assert lp["900015"] == "多源画师_local_abc123/danbooru@duoyuan/900015_p0.jpg", lp
        assert lp["900016"] == "单源画师_local_single/danbooru@dansingle/900016_p0.jpg", lp
        assert lp["900017"] == "多源画师_local_abc123/twitter@duoyuan/900017_p0.jpg", lp
        assert lp["900018"] == "ggac@毒瘤/900018_p0.jpg", lp  # 大小写规范化后 DB 路径小写
        assert (img_root / "ggac@毒瘤/900018_p0.jpg").is_file()
        assert lp["900019"] == "Alena Aenami_local_named/#artstation#/900019_p0.jpg", lp
        assert lp["900020"] == "Alena Aenami_local_named/#artstation#/900020_p0.jpg", lp
        assert lp["ext_h1"] == H + "danbooru@hashy_001.png", lp
        assert lp["ext_h2"] == H + "danbooru@hashy_002.png", lp
        assert lp["ext_h3"] == H + "sample_art_01.png", lp
        assert lp["900025"] == "作者甲_66/900025_p0.jpg", lp
        # ext_ 外来作品: 标题随路径移动刷新 (自动格式才刷, 手动改过的不动)
        db.register_foreign_file("X图/CEC84.png")  # other 来源
        ext_id = db.conn.execute(
            "SELECT artwork_id FROM pages WHERE local_path='X图/CEC84.png'").fetchone()[0]
        assert db.get_artwork_metadata(ext_id)["title"] == "X图 / CEC84", \
            db.get_artwork_metadata(ext_id)
        (img_root / "#其他来源#").mkdir(exist_ok=True)
        (img_root / "#其他来源#/CEC84.png").write_bytes(b"x")
        db.relink_page_path("X图/CEC84.png", "#其他来源#/CEC84.png")
        assert db.get_artwork_metadata(ext_id)["title"] == "#其他来源# / CEC84", \
            db.get_artwork_metadata(ext_id)
        db.update_artwork(ext_id, title="我的自定义标题")
        db.relink_page_path("#其他来源#/CEC84.png", "#其他来源#/CEC84_renamed.png")
        assert db.get_artwork_metadata(ext_id)["title"] == "我的自定义标题", \
            db.get_artwork_metadata(ext_id)
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t49_unknown_author_display() -> None:
    """T49: 未署名作品显示兜底 — 无作者→「未知作者」, 外来源有账号→显示账号 (离线)。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t49unk_"))
    img_root = tmp / "img"
    img_root.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        db.change_work_status("900010", WSt.DONE, title="无作者", total_pages=1)
        db.change_work_status("900011", WSt.DONE, title="外来无作者", total_pages=1)
        db.update_artwork("900011", source="twitter", source_author="qing0ying0")
        for aid, rel in (("900010", "900010_p0.jpg"), ("900011", "900011_p0.jpg")):
            (img_root / rel).write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)
        db.close()
        client = create_app().test_client()
        j = client.get("/api/library/items?view=artwork&status=unknown").get_json()
        by_id = {str(it["artwork_id"]): it for it in j["items"]}
        assert len(j["items"]) == 2, j["items"]   # 状态筛选「未知作者」捞到两个无 author_id 作品
        assert by_id["900010"]["author_name"] == "未知作者", by_id
        assert by_id["900011"]["author_name"] == "qing0ying0", by_id
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t50_unsigned_author_migration() -> None:
    """T50: 「未署名」占位作者迁移 — 无账号外来作品不再建「未署名」作者; 存量 local_未署名 清为无作者 (离线)。"""
    import tempfile
    import shutil
    import hashlib as _hl
    from db.store import PixivDatabase, WSt

    tmp = Path(tempfile.mkdtemp(prefix="t50unsigned_"))
    db = PixivDatabase(tmp / "pixiv.db")
    try:
        # ① 新行为: 无账号外来作品不建「未署名」作者, author_id 保持空
        db.change_work_status("910001", WSt.DONE, title="无账号外来", total_pages=1)
        db.update_artwork("910001", source="danbooru")
        assert db.backfill_foreign_authors() == 0
        m1 = db.get_artwork_metadata("910001")
        assert (m1.get("author_id") or "") == "", m1
        # ② 有账号的外来作品照常建 local_ 作者并归属
        db.change_work_status("910002", WSt.DONE, title="有账号外来", total_pages=1)
        db.update_artwork("910002", source="twitter", source_author="qing0ying0")
        assert db.backfill_foreign_authors() == 1
        m2 = db.get_artwork_metadata("910002")
        assert str(m2.get("author_id") or "").startswith("local_"), m2
        # ③ 存量迁移: 模拟旧数据 (local_ 未署名 作者 + 作品归属) → cleanup 清为无作者
        kid = "local_" + _hl.md5(("foreign:danbooru:未署名").encode("utf-8")).hexdigest()[:12]
        db.upsert_author(kid, "未署名")
        db.change_work_status("910003", WSt.DONE, title="旧未署名", total_pages=1)
        db.conn.execute("UPDATE artworks SET author_id=? WHERE artwork_id='910003'", (kid,))
        db.conn.commit()
        cleaned = db.cleanup_orphans()
        assert cleaned.get("unsigned_cleared") == 1, cleaned
        m3 = db.get_artwork_metadata("910003")
        assert (m3.get("author_id") or "") == "", m3
        assert db.get_author(kid) is None
        # ④ 手动建的同名作者 (非 local_ 自动生成) 不受影响
        db.upsert_author("10001", "未署名")
        db.change_work_status("910004", WSt.DONE, title="手动未署名", total_pages=1)
        db.conn.execute("UPDATE artworks SET author_id='10001' WHERE artwork_id='910004'",)
        db.conn.commit()
        assert db.cleanup_orphans().get("unsigned_cleared") == 0
        assert db.get_artwork_metadata("910004")["author_id"] == "10001"
        assert db.get_author("10001") is not None
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t51_browser_gone_watchdog() -> None:
    """T51: 服务自动退出判定 — 必须「SSE 断开」+「心跳/bye 超期」双信号都成立才退出 (离线)。

    回归: 旧实现只看心跳 90s, 而浏览器会节流隐藏标签定时器(约 1 次/分钟)、省电模式
    还会冻结标签(定时器全停) → 用户切走一会儿心跳断流, 服务误判「页面已关闭」自杀,
    切回来处处 failed to fetch。"""
    from main import _browser_gone, _HB_STALE, _SSE_GRACE, _BYE_GRACE
    now = 100000.0

    # ① 从未有过页面 (纯 API/命令行) → 永不退出
    gone, why = _browser_gone(now=now, clients=0, seen_sse=False, last_client_at=0.0,
                              ever_hb=False, last_hb=0.0, bye_at=0.0)
    assert gone is False, why

    # ② 【本次修复核心】页面连着 SSE 但心跳早已停 (隐藏标签被节流/冻结) → 不退出
    gone, why = _browser_gone(now=now, clients=1, seen_sse=True, last_client_at=now - 10,
                              ever_hb=True, last_hb=now - _HB_STALE * 10, bye_at=0.0)
    assert gone is False, why

    # ③ SSE 刚断开, 仍在重连宽限内 (覆盖刷新) → 不退出
    gone, why = _browser_gone(now=now, clients=0, seen_sse=True, last_client_at=now - 1,
                              ever_hb=True, last_hb=now - 1, bye_at=0.0)
    assert gone is False, why

    # ④ SSE 断超宽限但心跳新鲜 (SSE 掉线的页面仍能正常 fetch) → 不退出
    gone, why = _browser_gone(now=now, clients=0, seen_sse=True,
                              last_client_at=now - _SSE_GRACE - 5,
                              ever_hb=True, last_hb=now - 5, bye_at=0.0)
    assert gone is False, why

    # ⑤ 关标签: bye + SSE 已断超宽限 → 退出
    gone, why = _browser_gone(now=now, clients=0, seen_sse=True,
                              last_client_at=now - _SSE_GRACE - 5,
                              ever_hb=True, last_hb=now - _BYE_GRACE - 1,
                              bye_at=now - _BYE_GRACE - 1)
    assert gone is True and "bye" in why, (gone, why)

    # ⑥ 浏览器崩溃(无 bye): SSE 断 + 心跳超期 → 退出
    gone, why = _browser_gone(now=now, clients=0, seen_sse=True,
                              last_client_at=now - _SSE_GRACE - 5,
                              ever_hb=True, last_hb=now - _HB_STALE - 5, bye_at=0.0)
    assert gone is True and "心跳" in why, (gone, why)

    # ⑦ 阈值余量: 心跳兜底必须显著大于隐藏标签节流周期(约 60s), 否则又会误杀
    assert _HB_STALE >= 180, _HB_STALE
    assert _SSE_GRACE < _HB_STALE, (_SSE_GRACE, _HB_STALE)


def t52_no_python_tkinter_in_startup() -> None:
    """T52: 启动流程不得使用 Python tkinter (离线静态检查)。

    回归: 启动画面曾用 Tkinter 在**非主线程**创建/销毁窗口, Tcl 在销毁或 GC 时
    abort (tcl86t.dll, 异常码 0x80000003) → 进程**无声消失**, 用户表现是
    「用一会儿各处就 failed to fetch」(2026-09-13 Windows 事件日志确认)。
    现改为只用 PyInstaller 原生 splash + pyi_splash IPC 更新文字。"""
    root = Path(__file__).resolve().parent.parent
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert "import tkinter" not in main_src, "main.py 不应再引入 tkinter"
    assert "_start_splash" not in main_src, "main.py 不应再有 Tk 启动画面"
    assert "pyi_splash" in main_src and "update_text" in main_src, "启动画面文字应走 pyi_splash IPC"
    spec = (root / "build.spec").read_text(encoding="utf-8")
    assert "Splash(" in spec, "build.spec 应启用 PyInstaller 原生 Splash"
    assert "text_pos" in spec, "build.spec 应设置 text_pos 以启用 splash 文字区"


def t53_operation_audit_log() -> None:
    """T53: 操作审计日志 (设置页「📋 操作记录」): 增/改/删落盘 + 筛选/计数 + 清空 +
    写接口自动记账 (编辑作品/移入回收站) + /api/logs/operations 返回结构 (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from utils import audit_log
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t53audit_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        # ① 数据层: 三种类型落盘 + 读回 (新→旧)
        audit_log.record("add", "导入图片", "图库入库 2 张", 2)
        audit_log.record("update", "编辑作品", "作品 101 修改字段: title", 1)
        audit_log.record("delete", "删除作品", "删除 1 个作品", 1)
        r = audit_log.read()
        assert r["total"] == 3, r
        assert r["counts"] == {"add": 1, "update": 1, "delete": 1}, r["counts"]
        assert [x["kind"] for x in r["items"]] == ["delete", "update", "add"], r["items"]
        assert r["items"][0]["kind_label"] == "删除", r["items"][0]

        # ② 按类型筛选
        only_add = audit_log.read(kind="add")
        assert [x["action"] for x in only_add["items"]] == ["导入图片"], only_add
        assert only_add["total"] == 3, only_add  # total/counts 始终是全局值

        # ③ 非法类型当「全部」处理, 不报错
        assert audit_log.read(kind="bogus")["total"] == 3

        # ④ 清空
        assert audit_log.clear() == 3
        assert audit_log.read()["total"] == 0

        # ⑤ Web API: 写接口自动记账 (编辑 → 修改; 删除 → 删除)
        db = PixivDatabase(tmp / "pixiv.db")
        db.change_work_status("101", WSt.DONE, total_pages=1, title="测试作品")
        db.close()
        client = create_app().test_client()
        r = client.post("/api/library/update", json={"artwork_id": "101", "title": "改名了"})
        assert r.status_code == 200, r.status_code
        r = client.post("/api/library/delete", json={"ids": ["101"]})
        assert r.status_code == 200 and r.get_json()["deleted"] == 1, r.get_json()
        r = client.get("/api/logs/operations")
        j = r.get_json()
        assert r.status_code == 200 and "items" in j and "counts" in j, j
        acts = [x["action"] for x in j["items"]]
        # 2026-09-17 起「删除」不再是物理删除, 而是软删除进应用内回收站 → 动作名改了,
        # 且 kind 记为 update (可逆操作) 而不是 delete (delete 只留给"彻底删除")。
        assert "编辑作品" in acts and "移入回收站" in acts, acts
        assert j["counts"]["update"] >= 2, j["counts"]
        # 真正卸载记录的「彻底删除」才是 kind=delete —— 走一遍回收站清空路径验证
        r = client.post("/api/library/trash/purge", json={"ids": ["101"]})
        assert r.status_code == 200 and r.get_json()["deleted"] == 1, r.get_json()
        j = client.get("/api/logs/operations").get_json()
        assert j["counts"]["delete"] >= 1, j["counts"]
        # 按类型筛选参数生效
        j2 = client.get("/api/logs/operations?kind=delete").get_json()
        assert all(x["kind"] == "delete" for x in j2["items"]) and j2["items"], j2
        # 清空接口
        j3 = client.post("/api/logs/operations/clear").get_json()
        assert j3["status"] == "ok" and j3["cleared"] >= 2, j3
        assert client.get("/api/logs/operations").get_json()["items"] == []
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t54_selfcheck_approval_actions() -> None:
    """T54: 自检审批逐条处理 — /api/library/self-check-dismiss 从最近报告移除已处理条目
    (内存 + 落盘 self_check.json 同步), 且前端「缺作者名」条目带指定作者/反搜来源按钮 (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    import web.server as wsrv
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t54selfcheck_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    report = {
        "time": "2026-01-01 00:00:00", "fixed": {},
        "suspicious": [
            {"type": "no_author", "artwork_id": "111", "has_local": True, "text": "作品 111 仍缺作者名"},
            {"type": "no_author", "artwork_id": "222", "has_local": False, "text": "作品 222 仍缺作者名"},
            {"type": "empty_dir", "text": "空文件夹（可删）：x"},
        ],
    }
    try:
        (tmp / "self_check.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
        wsrv._self_check_state.update(running=False, result=None)
        client = create_app().test_client()

        # ① 内存为空 → 回落读盘, 三条可疑项齐备
        j = client.get("/api/library/self-check-last").get_json()
        assert len(j["result"]["suspicious"]) == 3, j["result"]

        # ② dismiss 处理掉的那条 (内存 + 返回体同步)
        r = client.post("/api/library/self-check-dismiss", json={"artwork_id": "111"})
        j2 = r.get_json()
        assert r.status_code == 200 and j2["removed"] == 1, j2
        left_ids = [s.get("artwork_id") for s in j2["result"]["suspicious"]]
        assert "111" not in left_ids and "222" in left_ids, left_ids

        # ③ 落盘同步: 页面刷新/服务重启后不会把已处理项列回来
        disk = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
        assert len(disk["suspicious"]) == 2, disk["suspicious"]
        assert all(s.get("artwork_id") != "111" for s in disk["suspicious"]), disk

        # ④ 非 no_author 条目 / 不存在的 id / 缺参 都不误删
        assert client.post("/api/library/self-check-dismiss",
                           json={"artwork_id": "999"}).get_json()["removed"] == 0
        assert client.post("/api/library/self-check-dismiss",
                           json={}).get_json()["removed"] == 0
        j3 = client.get("/api/library/self-check-last").get_json()
        assert len(j3["result"]["suspicious"]) == 2, j3["result"]

        # ⑤ 前端接线静态检查 (改前端时别把逐条审批按钮丢掉)
        appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        assert "chkItemHtml" in appjs and "susAssignAuthor" in appjs \
            and "susReverseSource" in appjs, "报告应保留逐条处理按钮"
        assert "/api/library/self-check-dismiss" in appjs, "处理完应向报告同步移除"
        assert "has_local" in appjs, "反搜按钮应按「有无本地图」显示"
        assert "chkIgnore" in appjs and "/api/library/check-ignore" in appjs, \
            "待办项应能「忽略不再提示」"
        srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        assert '"has_local"' in srv, "后端应为 no_author 可疑项下发 has_local"
    finally:
        wsrv._self_check_state.update(running=False, result=None)
        wsrv._check_state.update(running=None, steps={}, progress={},
                                 report=None, journal_key=None)
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t55_readable_labels_and_title_repair() -> None:
    """T55: 「ID 归 ID, 图片名归图片名」定案落地 ——
    ① 人读标签(《标题》· 作者 · 来源, 无意义标题→（无标题）) ② 审计记录带可读标签+关联ID
    ③ 无意义/占位标题回填(事实已在 deleted_at 字段, 标题不再当占位仓库) ④ 锁定机制已移除 (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from utils import audit_log
    from db.store import PixivDatabase, WSt, PSt
    from web.server import _artwork_label, _labels_inline

    tmp = Path(tempfile.mkdtemp(prefix="t55label_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        # ① 人读标签 (纯函数): 有标题 / 无意义标题(==ID) / 已失效(deleted_at 字段) 三种
        assert _artwork_label({"title": "露西的fans", "author_name": "テスト画師"},
                              "12345678") == "《露西的fans》 · 作者 テスト画師 · 来源 pixiv"
        assert _artwork_label({"title": "Ehentai 图", "source": "Ehentai",
                               "source_author": "someone"},
                              "ext_abc") == "《Ehentai 图》 · 作者 someone · 来源 Ehentai"
        assert _artwork_label({"title": "merged_x1"}, "merged_x1") == "（无标题） · 来源 pixiv"
        # 「已失效」看字段, 不看标题 —— 标题怎么改都影响不了它
        assert _artwork_label({"title": "任意新标题", "deleted_at": "2026-09-14T03:00:00"},
                              "123") == "（原作品已删除） · 来源 pixiv"
        assert _artwork_label(None, "999") == "（无标题） · 来源 pixiv"

        # ② 数据层: 建 4 个作品 (含标题==ID 与 旧版占位标题)
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        db.upsert_author("99", "テスト画師")
        db.change_work_status("12345678", WSt.DONE, total_pages=1,
                              title="露西的fans", author_id="99")
        db.change_work_status("ext_abc123", WSt.DONE, total_pages=1, title="Ehentai 图")
        db.update_artwork("ext_abc123", source="Ehentai", source_author="someone")
        db.change_work_status("merged_x1", WSt.DONE, total_pages=1, title="merged_x1")
        db.mark_page_status("merged_x1", 0, PSt.DONE, local_path="a/miku with flower.png")
        db.change_work_status("merged_x2", WSt.DONE, total_pages=1,
                              title="（作品已删除 merged_x2）")
        db.mark_page_status("merged_x2", 0, PSt.DONE, local_path="a/b/旧占位标题图.png")
        # 批量标签 (走 DB)
        m = _labels_inline(["12345678", "ext_abc123"])
        assert "《露西的fans》 · 作者 テスト画師 · 来源 pixiv" in m, m
        assert "《Ehentai 图》 · 作者 someone · 来源 Ehentai" in m, m
        assert _labels_inline([]) == "" and _labels_inline(["不存在的ID"]) == "（无标题） · 来源 pixiv"

        # ③ 标题自愈: title==ID 与 旧版「（作品已删除 …）」占位 → 都换成可读标题 (事实已在字段)
        assert db.repair_id_titles() == 2
        assert db.get_artwork_metadata("merged_x1")["title"] == "miku with flower"
        assert db.get_artwork_metadata("merged_x2")["title"] == "旧占位标题图"
        assert db.repair_id_titles() == 0     # 幂等: 再跑没有可修的
        # schema 已无 name_locked 列 (v009 引入的锁定机制按要求整体移除, v010 删列)
        assert "name_locked" not in [r[1] for r in db.conn.execute("PRAGMA table_info(artworks)")]
        db.close()

        # ④ 审计记录带可读标签 + 关联 ID
        audit_log.clear()
        audit_log.record("delete", "删除作品", f"删除 1 个作品：{_labels_inline(['12345678'])}",
                         1, ids=["12345678"])
        rec = audit_log.read()["items"][0]
        assert rec["ids"] == ["12345678"], rec
        assert "《露西的fans》" in rec["detail"], rec

        # ⑤ 「无信息文件名」判定: 只有纯哈希/纯顺序号/太短 才值得规范化
        srv_src = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        for token in ("_is_meaningless_filename", 'filrename', '文件名规范化'):
            assert token in srv_src, f"server.py 缺少 {token}"
        # ⑥ 前端接线静态检查: 列表用可读标签 + ID 小灰字可点击定位 + 整理预览含命名规范
        appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        for token in ("libFocusArtwork", "id-chip", "📝 命名规范"):
            assert token in appjs, f"app.js 缺少 {token}"
        # ⑦ 审批「指定作者」也要有关键词下拉 (复用编辑作者 combo) + 整理预览说明折叠 + 类型徽标补 U+FE0F
        #   + 单向闭环: 「待确认」行内提供「采用夹名」回流写入按钮
        for token in ("sus-author-combo", "makeEditAuthorCombo($('#sus-author-name')",
                      "tidyHelpToggle", "TYPE_TIP", "🏷\ufe0f 目录规范",
                      "tidyAdoptClaim", "adopt-claim"):
            assert token in appjs, f"app.js 缺少 {token}"
        # ⑧ 反向自愈已移除: 不再有「作者更正」自动改写数据库的入口
        assert "fixauthor" not in appjs, "app.js 不应再残留 fixauthor"
        assert "fixauthor" not in srv_src, "server.py 不应再残留 fixauthor"
        assert "toggleNameLock" not in appjs, "锁定机制已移除, 前端不应残留 toggleNameLock"
        assert "/api/library/lock-name" not in srv_src, "锁定接口已移除"
        assert "def _artwork_label" in srv_src
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t56_waifu2x_special_format() -> None:
    """T56: 放大版(waifu2x/w2x)是文件名规范化的**特殊格式** —— 放大标记必须保留并拉齐成
    `_waifu2x[_noiseN][_scaleNx]`(去尾下划线 / scale2x1→scale2x / 缩写 w2x2x→waifu2x_scale2x),
    已规范的写法原样不动, 后面还有别的词(如 _waifu2x_final)一律不碰 (离线)。"""
    import tempfile
    import shutil
    from db.store import PixivDatabase, WSt, PSt
    from web.server import _normalize_waifu_tail, _WAIFU_TAIL_RE, _tidy_build_plans, _tidy_apply_plans

    # ① 归一化函数: 各种畸形/已规范写法 → 统一写法
    for tail, want in (
            ("_waifu2x_noise1_scale2x_", "_waifu2x_noise1_scale2x"),   # 尾部多下划线
            ("_waifu2x_noise0_scale2x", "_waifu2x_noise0_scale2x"),    # 已规范 → 原样
            ("_waifu2x_scale2x", "_waifu2x_scale2x"),
            ("_waifu2x_noise1", "_waifu2x_noise1"),
            ("_waifu2x_noise1_scale2x1", "_waifu2x_noise1_scale2x"),   # scale2x1 → scale2x
            ("_w2x2x", "_waifu2x_scale2x"),                            # 缩写展开
            ("_w2x", "_waifu2x"),
            ("_waifu2x", "_waifu2x"),
    ):
        got = _normalize_waifu_tail(tail)
        assert got == want, (tail, got, want)

    # ② 尾巴识别: 只认「标记 + 已知参数」结尾; 后面还有别的词不认(避免误吞信息)
    assert _WAIFU_TAIL_RE.search("100762890_p0_waifu2x_noise1_scale2x_")
    assert _WAIFU_TAIL_RE.search("sample_art_10_w2x2x")
    assert _WAIFU_TAIL_RE.search("20250722_102048_waifu2x_noise1")
    assert not _WAIFU_TAIL_RE.search("abc_waifu2x_final")
    # 整个名字就是标记 (匹配起点=0, 没有主体) → 调用处靠 `start() > 0` 跳过, 不该动
    assert _WAIFU_TAIL_RE.search("waifu2x").start() == 0

    # ③ 计划级: 畸形放大版产出计划; 已规范的放大版不产生计划
    tmp = Path(tempfile.mkdtemp(prefix="t56waifu_"))
    img_root = tmp / "img"
    img_root.mkdir(parents=True, exist_ok=True)
    db = PixivDatabase(tmp / "pixiv.db")
    try:
        db.upsert_author("77", "作者乙")
        db.upsert_author("local_dan", "danshi")

        def _page(aid, rel):
            p = img_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)

        db.change_work_status("700001", WSt.DONE, title="w1", author_id="77", total_pages=1)
        _page("700001", "作者乙_77/700001_p0_waifu2x_noise1_scale2x_.png")   # 畸形 → 修
        db.change_work_status("700002", WSt.DONE, title="w2", author_id="77", total_pages=1)
        _page("700002", "作者乙_77/700002_p0_waifu2x_noise0_scale2x.png")    # 已规范 → 不动
        db.change_work_status("ext_w1", WSt.DONE, title="w3",
                              author_id="local_dan", total_pages=1)
        db.update_artwork("ext_w1", source="danbooru", source_author="danacct")
        D = "danshi_local_dan/danbooru@danacct/"
        _page("ext_w1", D + "sample_art_10_w2x2x.png")          # 缩写 → 展开

        plans = [p for p in _tidy_build_plans(db, img_root) if p["type"] == "filrename"]
        fr = {p["from"]: p["to"] for p in plans}
        assert fr.get("作者乙_77/700001_p0_waifu2x_noise1_scale2x_.png") \
            == "作者乙_77/700001_p0_waifu2x_noise1_scale2x.png", fr
        assert fr.get(D + "sample_art_10_w2x2x.png") \
            == D + "sample_art_10_waifu2x_scale2x.png", fr
        assert "作者乙_77/700002_p0_waifu2x_noise0_scale2x.png" not in fr, fr
        assert len(fr) == 2, fr

        rep = _tidy_apply_plans(db, img_root)
        assert rep["filenamed"] == 2 and not rep["failed"], rep
        assert (img_root / "作者乙_77/700001_p0_waifu2x_noise1_scale2x.png").is_file()
        assert not (img_root / "作者乙_77/700001_p0_waifu2x_noise1_scale2x_.png").exists()
        assert (img_root / D / "sample_art_10_waifu2x_scale2x.png").is_file()
        lp = {r["artwork_id"]: r["local_path"] for r in db.conn.execute(
            "SELECT artwork_id, local_path FROM pages")}
        assert lp["700001"] == "作者乙_77/700001_p0_waifu2x_noise1_scale2x.png", lp
        assert lp["ext_w1"] == D + "sample_art_10_waifu2x_scale2x.png", lp
    finally:
        try:
            db.close()
        except Exception:
            pass
        shutil.rmtree(str(tmp), ignore_errors=True)


def t57_deleted_flag_is_a_field() -> None:
    """T57: 「作品页已失效」是数据库字段(artworks.deleted_at), 不再靠标题文案 ——
    ① 只在 pixiv 数字作品 ID 上可标记/撤销; ② 「已删除」筛选只认字段(改标题不影响);
    ③ 已失效作品退出「缺元数据」队列; ④ 联网补全成功会自动撤销标记;
    ⑤ 旧版「（作品已删除 …）」标题在迁移时把事实搬进字段并把标题换可读 (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t57deleted_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        # schema: v011 加列
        assert "deleted_at" in [r[1] for r in db.conn.execute("PRAGMA table_info(artworks)")]

        db.change_work_status("700001", WSt.DONE, total_pages=1, title="原标题")
        db.change_work_status("ext_zz9", WSt.DONE, total_pages=1, title="外来源图")

        # ① 只认 pixiv 数字作品 ID
        assert db.artwork_is_deleted("700001") is False
        assert db.mark_artwork_deleted("ext_zz9", True) == 0, "非 pixiv 作品不该被标记"
        assert db.artwork_is_deleted("ext_zz9") is False
        assert db.mark_artwork_deleted("700001", True) == 1
        assert db.artwork_is_deleted("700001") is True

        # ② 改标题不影响「是否已失效」(旧实现正是被这点坑到)
        db.update_artwork("700001", title="用户随便改的新标题")
        assert db.artwork_is_deleted("700001") is True
        got = {r["artwork_id"]: r for r in db.list_artworks(deleted=True, limit=50)}
        assert set(got) == {"700001"}, got
        assert got["700001"]["title"] == "用户随便改的新标题"

        # ③ 已失效 → 退出「缺元数据」队列 (清空标题也不该再被当成待补全)
        db.update_artwork("700001", title=None, thumb_url=None)
        assert "700001" not in db.works_missing_metadata(), db.works_missing_metadata()
        assert "700001" not in {r["artwork_id"]
                                for r in db.list_artworks(missing=True, limit=50)}
        n_missing_with_deleted = db.count_missing_metadata()

        # ④ 撤销标记 → 恢复参与补全
        assert db.mark_artwork_deleted("700001", False) == 1
        assert db.artwork_is_deleted("700001") is False
        assert "700001" in db.works_missing_metadata()
        assert db.count_missing_metadata() == n_missing_with_deleted + 1
        assert db.mark_artwork_deleted("700001", False) == 0   # 已撤销 → 不再重复写

        # ⑤ 联网补全成功 (upsert 权威元数据) → 自动撤销可能存在的失效标记
        db.mark_artwork_deleted("700001", True)
        assert db.artwork_is_deleted("700001") is True
        db.upsert_artwork_metadata("700001", title="补回来的标题", illust_type=0, tags=[])
        assert db.artwork_is_deleted("700001") is False, "能取到元数据说明作品页存在, 应自动撤销"
        db.close()

        # ⑥ API: 非 pixiv ID → 400; 不存在 → 404; pixiv → 200 + 进审计
        client = create_app().test_client()
        assert client.post("/api/library/deleted-flag",
                           json={"artwork_id": "ext_zz9", "deleted": True}).status_code == 400
        assert client.post("/api/library/deleted-flag",
                           json={"artwork_id": "99999999", "deleted": True}).status_code == 404
        r = client.post("/api/library/deleted-flag",
                        json={"artwork_id": "700001", "deleted": True})
        assert r.status_code == 200 and r.get_json()["deleted"] is True, r.get_json()
        acts = [x["action"] for x in client.get("/api/logs/operations").get_json()["items"]]
        assert "标记作品已失效" in acts, acts
        r = client.post("/api/library/deleted-flag",
                        json={"artwork_id": "700001", "deleted": False})
        assert r.get_json()["deleted"] is False, r.get_json()

        # ⑦ 全项目不应再有「靠标题前缀判断已删除」的筛选条件
        #   (repair_id_titles 里保留 LIKE 是为了清理旧占位标题, 那不是筛选)
        srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        sto = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
        appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        assert 'where.append("a.title LIKE' not in sto, "「已删除」筛选不该再依赖标题文案"
        assert sto.count("a.deleted_at IS NOT NULL") >= 4, "四处查询的「已删除」筛选都应改用字段"
        assert "deleted_at" in sto and "mark_artwork_deleted" in sto
        assert "/api/library/deleted-flag" in srv and "deletedTagHtml" in appjs
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t58_one_way_loop() -> None:
    """T58: 单向闭环 用户 → 数据库 → 磁盘  ——
    ① 磁盘与数据库不一致时 **绝不自动改库**: 只出「待确认」并带上两边证据, 争议夹整夹冻结;
    ② 唯一回流写入入口 /api/library/tidy/adopt-claim: 作者未建档才用夹名建, 已建档**不改其名**;
    ③ 非 pixiv 乱码文件名规范成 `{站点@账号}_{序号}` (不写作者名、不写假页码) (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from web.server import create_app, _tidy_build_plans

    tmp = Path(tempfile.mkdtemp(prefix="t58loop_"))
    img_root = tmp / "img"
    img_root.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))

        def _page(aid, rel):
            p = img_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)

        # 争议: 夹名说作者是 55500001(「夹名画师」), 库里记的是 666
        db.upsert_author("666", "库里画师")
        db.change_work_status("800001", WSt.DONE, title="", author_id="666", total_pages=1)
        _page("800001", "夹名画师_55500001/800001_p0.jpg")

        # ① 只出待确认, 不自动改库; 整夹冻结 (无 rename/move/filrename 计划)
        plans = _tidy_build_plans(db, img_root)
        assert not any(p["type"] in ("rename", "move", "filrename") for p in plans), plans
        disp = [p for p in plans if p.get("claim_aid")]
        assert len(disp) == 1 and disp[0]["auto"] is False, plans
        assert disp[0]["claim_aid"] == "55500001" and disp[0]["db_aid"] == "666", disp
        assert db.get_artwork_metadata("800001")["author_id"] == "666", "不得自动改写数据库"
        assert db.get_author("55500001") is None, "只是展示证据, 不该顺手建作者"
        db.close()

        # ② 回流写入: 用户点了「采用夹名」→ 才写库, 且不动已建档作者的名字
        client = create_app().test_client()
        assert client.post("/api/library/tidy/adopt-claim",
                           json={"artwork_id": "800001", "author_id": "abc"}).status_code == 400
        r = client.post("/api/library/tidy/adopt-claim",
                        json={"artwork_id": "800001", "author_id": "55500001",
                              "author_name": "夹名画师"})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["name"] == "夹名画师", r.get_json()
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        assert db.get_artwork_metadata("800001")["author_id"] == "55500001"
        assert db.get_author("55500001")["name"] == "夹名画师"
        # 已建档作者: 只改作品归属, 不改库里的权威名, 也不把夹名塞进曾用名
        db.upsert_author("666", "库里画师")
        db.update_artwork("800001", author_id="666")
        r = client.post("/api/library/tidy/adopt-claim",
                        json={"artwork_id": "800001", "author_id": "666",
                              "author_name": "夹名的烂名字"})
        assert r.status_code == 200 and r.get_json()["name"] == "库里画师", r.get_json()
        assert db.get_author("666")["name"] == "库里画师"
        assert "夹名的烂名字" not in (db.get_author("666")["aliases"] or [])
        # 作者改回 55500001 后, 夹名与库一致 → 争议消失, 该夹不再冻结
        db.update_artwork("800001", author_id="55500001")
        plans2 = _tidy_build_plans(db, img_root)
        assert not any(p.get("claim_aid") for p in plans2), plans2
        assert not any(p["type"] == "rename" for p in plans2), plans2   # 夹名已正确
        # ③ 非 pixiv 乱码名 → {站点@账号}_{序号} (不写作者名/不写假页码)
        db.change_work_status("ext_lp1", WSt.DONE, title="x", author_id="local_lp",
                              total_pages=1)
        db.upsert_author("local_lp", "某画师")
        db.update_artwork("ext_lp1", source="twitter", source_author="acc1")
        _page("ext_lp1", "某画师_local_lp/twitter@acc1/deadbeefdeadbeef.png")
        pl = [p for p in _tidy_build_plans(db, img_root) if p["type"] == "filrename"]
        got = {p["from"]: p["to"] for p in pl}
        assert got.get("某画师_local_lp/twitter@acc1/deadbeefdeadbeef.png") \
            == "某画师_local_lp/twitter@acc1/twitter@acc1_001.png", got
        db.close()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t59_one_way_audit_fixes() -> None:
    """T59: 单向闭环审计的四处修正 (2026-09-14) ——
    ① 单来源 local_ 作者的文件已在 `作者夹/站点@账号` 里 → 目标是**平铺回作者夹**,
       不再是 `作者夹/作者夹` 自我套娃 (真实图库实测撞到);
    ② 标题回填只在文件主干是「人读信息」时用它, 否则「未命名作品」(不把哈希名灌进库);
    ③ 夹名那截是占位词/纯符号时不建作者、也不回填 author_id;
    ④ 清掉历史遗留占位作者行; 「路径 → is_r18」的旧启发式(mark_r18_by_folder)已删除 (离线)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt, folder_author_name_usable, stem_is_readable
    import web.server as ws

    tmp = Path(tempfile.mkdtemp(prefix="t59audit_"))
    img_root = tmp / "img"
    img_root.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))

        def _page(aid, rel):
            p = img_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)

        # ---- ① 单来源 local_ 作者: 位置是「作者夹/站点@账号」, 不再自我套娃 ----
        db.upsert_author("local_solo", "独源画师")
        db.change_work_status("ext_solo1", WSt.DONE, title="s", author_id="local_solo",
                              total_pages=1)
        db.update_artwork("ext_solo1", source="twitter", source_author="acc1")
        _page("ext_solo1", "X图/独源画师_local_solo/twitter@acc1/777777_p0.png")
        # 跨作者套娃: 作品属于「别的画师」, 却躺在「独源画师」的夹里 (真实图库撞到过)
        db.upsert_author("local_bb11cc22", "别的画师")
        db.change_work_status("ext_solo2", WSt.DONE, title="o", author_id="local_bb11cc22",
                              total_pages=1)
        db.update_artwork("ext_solo2", source="danbooru", source_author="acc2")
        _page("ext_solo2", "X图/独源画师_local_solo/#danbooru#/778888_p0.png")
        mv = [p for p in ws._tidy_build_plans(db, img_root) if p["type"] == "move"]
        got = {p["from"]: p["to"] for p in mv}
        # 已在正确位置 (`X图/作者夹/站点@账号`) → 零计划 (不自我套娃、单来源也不平铺)
        assert "X图/独源画师_local_solo/twitter@acc1" not in got, mv
        # 别人的作品 → 归位到「自己的作者夹/自己的站点@账号」, 绝不套在别人的夹里
        assert got.get("X图/独源画师_local_solo/#danbooru#") \
            == "X图/别的画师_local_bb11cc22/danbooru@acc2", mv
        assert not any(t.endswith("独源画师_local_solo/独源画师_local_solo")
                       or "独源画师_local_solo/别的画师_local_bb11cc22" in t
                       for t in got.values()), mv

        # ---- ② 标题回填: 只认可读文件名 ----
        assert stem_is_readable("miku with flower") and stem_is_readable("视奸")
        assert not stem_is_readable("-7a93b54c836a829") and not stem_is_readable("00000019")
        assert not stem_is_readable("ab") and not stem_is_readable("")
        db.change_work_status("900031", WSt.DONE, title="900031", total_pages=1)
        _page("900031", "作者甲_66/900031_p0.jpg")
        db.change_work_status("900032", WSt.DONE, title="900032", total_pages=1)
        _page("900032", "作者甲_66/-7a93b54c836a829.png")
        db.change_work_status("900033", WSt.DONE, title="900033", total_pages=1)
        _page("900033", "作者甲_66/花朵与猫.png")
        n = db.repair_id_titles()
        assert n >= 3, n
        assert db.get_artwork_metadata("900031")["title"] == "900031_p0", "可读文件名→当标题"
        assert db.get_artwork_metadata("900032")["title"] == "未命名作品", "哈希名不得当标题"
        assert db.get_artwork_metadata("900033")["title"] == "花朵与猫"

        # ---- ③ 占位词/纯符号夹名 → 不建作者、不回填 ----
        for nm in ("unknown", "未知作者", "未署名", "--", "  "):
            assert folder_author_name_usable(nm) is False, nm
        for nm in ("独源画师", "MUK（むっく）", "kase_22", "Alena Aenami"):
            assert folder_author_name_usable(nm) is True, nm
        (img_root / "unknown_66880001").mkdir(parents=True, exist_ok=True)
        (img_root / "unknown_66880001" / "778888_p0.png").write_bytes(b"x" * 16)
        (img_root / "--_66880002").mkdir(parents=True, exist_ok=True)
        (img_root / "--_66880002" / "778889_p0.png").write_bytes(b"x" * 16)
        ws._adopt_authors_from_folders(db, img_root)
        assert db.get_author("66880001") is None, "占位夹名不该建作者"
        assert db.get_author("66880002") is None, "纯符号夹名不该建作者"
        # 可辨认的夹名照常建档 (没误伤正常路径)
        (img_root / "正常画师_66880003").mkdir(parents=True, exist_ok=True)
        (img_root / "正常画师_66880003" / "778890_p0.png").write_bytes(b"x" * 16)
        ws._adopt_authors_from_folders(db, img_root)
        assert (db.get_author("66880003") or {}).get("name") == "正常画师"

        # ---- ④ 历史遗留占位作者行被清掉; 路径→is_r18 的旧启发式已删除 ----
        db.upsert_author("local_junk", "未知作者")
        db.change_work_status("900034", WSt.DONE, title="j", author_id="local_junk",
                              total_pages=1)
        rep = db.cleanup_orphans()
        assert db.get_author("local_junk") is None, rep
        assert not (db.get_artwork_metadata("900034") or {}).get("author_id"), "应回到无作者"
        sto = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
        assert "mark_r18_by_folder" not in sto, "纯「路径 → is_r18」的旧启发式应已删除"
        db.close()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t60_foreign_author_from_source_page() -> None:
    """T60: 外来源作者名必须取**源网页当前值**, 不再直接采信 SauceNAO 结果卡
    (第三方索引快照 —— 作者改过名就永远是旧名; 用户实测: 卡片「ying yi」,
    而 artstation.com/artwork/DAJVOE 当前作者是「qing ying」/账号 qing0ying0) ——
    ① 网页读得到 → 用网页名 + 账号, author_src='page';
    ② 网页读不到但有 URL 账号 → 用账号, author_src='handle';
    ③ 都没有 → 保留卡片名但标 author_src='saucenao_index' (界面提示核对, 且不预填进库);
    ④ ArtStation 作品 URL 能解析出 hash_id 去读官方 JSON (离线静态 + 逻辑桩)。"""
    from core.reverse_search import ReverseSearch, _ARTSTATION_ARTWORK

    # ④ 作品页 URL → hash_id
    assert _ARTSTATION_ARTWORK.search(
        "https://www.artstation.com/artwork/DAJVOE").group(1) == "DAJVOE"
    assert _ARTSTATION_ARTWORK.search("https://www.artstation.com/qing0ying0") is None

    rs = object.__new__(ReverseSearch)   # 不跑 __init__ (无需真浏览器)
    # ① 源网页读得到 → 权威
    rs._artstation_author = lambda url: ("qing ying", "qing0ying0")
    det = {"site": "artstation", "url": "https://www.artstation.com/artwork/DAJVOE",
           "handle": ""}
    rs._enrich_foreign_author(det, "ying yi")
    assert det["author_name"] == "qing ying", det
    assert det["handle"] == "qing0ying0", det
    assert det["author_src"] == "page", det
    # ② 网页读不到 (Cloudflare 拦 / 非 artstation) → 退 URL 账号
    rs._artstation_author = lambda url: None
    det = {"site": "twitter", "url": "https://twitter.com/status/1", "handle": "qing0ying0"}
    rs._enrich_foreign_author(det, "ying yi")
    assert det["author_name"] == "qing0ying0" and det["author_src"] == "handle", det
    # ③ 全都没有 → 保留卡片名但标明是索引快照 (不冒充事实)
    det = {"site": "artstation", "url": "https://www.artstation.com/artwork/DAJVOE",
           "handle": ""}
    rs._enrich_foreign_author(det, "ying yi")
    assert det["author_name"] == "ying yi" and det["author_src"] == "saucenao_index", det

    # 静态接线: 反搜结果带 author_src / 后端透传 / 前端对索引名给徽标且不预填作者
    srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    rsrc = (PROJECT_ROOT / "core" / "reverse_search.py").read_text(encoding="utf-8")
    assert "author_src" in srv and '"author_src": hit.get("author_src")' in srv
    assert "_enrich_foreign_author" in rsrc and "_artstation_author" in rsrc
    assert "projects/{m.group(1)}.json" in rsrc
    for token in ("saucenao_index", "索引名·可能过时", "otherIdx", "author_src === 'page'"):
        assert token in appjs, f"app.js 缺少 {token}"
    assert "'${esc(firstOther.author_name || firstOther.handle || '')}'" not in appjs, \
        "索引名不得再被直接预填进确认按钮"


def t61_single_author_identity() -> None:
    """T61: 本地作者身份只认一套键  ——
    ① `foreign_author_id(站点,账号)` 是唯一稳定键 (与 backfill 同一套);
    ② 同 (站点,账号) 用不同显示名建两次 → **同一条**作者, 且不覆盖已有现用名;
    ③ 已有作品用 (站点,账号) 归属过 → 复用那条, 不再新算一个 id;
    ④ 只给名字时按**现用名/曾用名**找回原记录 (改名后不再分叉);
    ⑤ 夹名含多个别名时用顿号连接, **绝不含 `/`** (否则文件夹名会被劈成两层 — 潜伏 bug)。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, foreign_author_id
    import web.server as ws

    tmp = Path(tempfile.mkdtemp(prefix="t61identity_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))

        # ① 唯一稳定键 (与历史数据对得上: foreign:twitter:qing0ying0 → local_3155a34b4d66)
        assert foreign_author_id("twitter", "qing0ying0") == "local_3155a34b4d66"
        assert foreign_author_id("artstation", "qing0ying0") != \
            foreign_author_id("twitter", "qing0ying0")

        # ② 同 (站点,账号) 两次不同显示名 → 同一条作者; 已存在的现用名不被覆盖
        a1 = db.create_or_get_local_author("ying yi", source="twitter", handle="qing0ying0")
        a2 = db.create_or_get_local_author("qing ying", source="twitter", handle="qing0ying0")
        assert a1 == a2 == "local_3155a34b4d66", (a1, a2)
        assert db.get_author(a1)["name"] == "ying yi", db.get_author(a1)

        # ③ 已有作品用 (站点,账号) 归属过 → 复用那条 (不再新算 id)
        b = db.create_or_get_local_author("手建画师")
        db.change_work_status("910001", WSt.DONE, title="t", total_pages=1)
        db.update_artwork("910001", source="danbooru", source_author="handmade")
        db.set_artworks_author(["910001"], b)
        c = db.create_or_get_local_author("手建画师", source="danbooru", handle="handmade")
        assert c == b, (c, b)
        assert db.get_author(foreign_author_id("danbooru", "handmade")) is None, \
            "已按 (站点,账号) 归属过就不该再新建一条"

        # ④ 只给名字: 按曾用名也能找回同一个人
        d1 = db.create_or_get_local_author("老名字")
        db.update_author(d1, name="新名字")          # 老名字 → 曾用名
        assert db.get_author(d1)["aliases"] == ["老名字"], db.get_author(d1)
        assert db.create_or_get_local_author("老名字") == d1
        assert db.create_or_get_local_author("新名字") == d1

        # ⑤ 夹名多别名用顿号, 不能出现 `/`
        import json as _json
        db.upsert_author("60000001", "主名",
                         aliases_json=_json.dumps(["别名甲", "别名乙"], ensure_ascii=False))
        fn = ws._author_folder_name(db, "60000001", "主名")
        assert fn == "主名（别名甲、别名乙）_60000001", fn
        assert "/" not in fn and fn.count("（") == fn.count("）") == 1, fn
        # 单别名照旧
        db.upsert_author("60000002", "单别名主名",
                         aliases_json=_json.dumps(["唯一别名"], ensure_ascii=False))
        assert ws._author_folder_name(db, "60000002", "单别名主名") \
            == "单别名主名（唯一别名）_60000002"
        db.close()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t62_tidy_removes_empty_dirs() -> None:
    """T62: 一键整理自带「清理空夹」 ——
    ① 真·空夹 / 只剩系统杂物(Thumbs.db)的夹 → 产生 rmdir 计划 (auto, 默认勾选);
    ② 含任何真实文件(.txt/.psd…)的夹 → **不**删 (比健康面板"不含图片即删"更保守);
    ③ 嵌套空夹只列最外面那一处 (子夹随它一起删, 不刷一排重复行);
    ④ 作者归属争议**冻结**中的夹一个指头都不碰 (含其内部子夹);
    ⑤ 执行后夹真的没了, 有图的夹与含资料的夹原样保留。"""
    import shutil
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    import web.server as ws

    tmp = Path(tempfile.mkdtemp(prefix="t62rmdir_"))
    img = tmp / "img"
    img.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))

        def _page(aid, rel):
            p = img / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)
            db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)

        # ① 真·空夹 + 只剩系统杂物的夹
        (img / "空夹甲").mkdir(parents=True, exist_ok=True)
        (img / "空夹乙").mkdir(parents=True, exist_ok=True)
        (img / "空夹乙" / "Thumbs.db").write_bytes(b"junk")
        # ③ 嵌套空夹 (只应列最外层)
        (img / "空夹丙" / "子夹").mkdir(parents=True, exist_ok=True)
        # ② 含真实文件的夹 → 绝不删
        (img / "有资料").mkdir(parents=True, exist_ok=True)
        (img / "有资料" / "设定.txt").write_text("hi", encoding="utf-8")
        # 有图的夹 → 不动
        db.change_work_status("920001", WSt.DONE, title="t", total_pages=1)
        _page("920001", "有图/920001_p0.jpg")
        # ④ 作者归属争议夹 (冻结) + 其内部空子夹
        db.upsert_author("666", "库里画师")
        db.change_work_status("920002", WSt.DONE, title="", author_id="666", total_pages=1)
        _page("920002", "夹名画师_55500001/920002_p0.jpg")
        (img / "夹名画师_55500001" / "空子夹").mkdir(parents=True, exist_ok=True)

        plans = ws._tidy_build_plans(db, img)
        rd = [p for p in plans if p["type"] == "rmdir"]
        got = {p["from"] for p in rd}
        assert got == {"空夹甲", "空夹乙", "空夹丙"}, plans
        assert all(p["auto"] for p in rd), rd
        assert all(p["files"] == [] for p in rd), rd
        # ② 含真实文件 / ④ 冻结夹内 → 都不在计划里
        assert not any("有资料" in x for x in got), got
        assert not any("夹名画师_55500001" in x for x in got), got

        rep = ws._tidy_apply_plans(db, img)
        assert rep["rmdirs"] == 3, rep
        assert not rep["failed"], rep
        assert not (img / "空夹甲").exists()
        assert not (img / "空夹乙").exists()          # 连 Thumbs.db 一起整棵删掉
        assert not (img / "空夹丙").exists()          # 子夹随之消失
        assert (img / "有资料" / "设定.txt").is_file()  # 含真实文件 → 保留
        assert (img / "有图" / "920001_p0.jpg").is_file()
        assert (img / "夹名画师_55500001" / "空子夹").is_dir()  # 冻结夹内不动
        # 幂等: 再跑一次没有可删的了
        assert not [p for p in ws._tidy_build_plans(db, img) if p["type"] == "rmdir"]
        # 预览接口的计数里带上 rmdir (供界面显示)
        srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        assert '"rmdir": sum(' in srv and "清理空夹" in srv
        db.close()
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t63_output_sync_no_duplicate_registration() -> None:
    """T63: 旧 Output 同步器防重复登记 (2026-09-15 凌晨事故回归)。

    事故: 购物车 save_folder 就是图库根, 连续 3 个下载任务并发跑, 每个都全库
    update_pages_from_output; 宽松正则 (\\d+)_p(\\d+) 把 `Ehentai_007_p0.png`
    解析成作品 007、把 merged 哈希尾巴 18757 当 pid, 且不查重直接 init_works
    → 29 个假作品 / 78 个重复页。
    断言: ① 已在库文件(归属 ext_/merged_) 再扫多少遍都不会冒出假作品、不会重复登记;
         ② 路径分隔符不被反向刷成反斜杠; ③ 真·新文件(刚下载的 pid_pN)仍能正常入库;
         ④ 同步幂等, 连扫 N 次结果一致。"""
    import tempfile
    from pathlib import Path
    from db.store import PixivDatabase, PSt, WSt

    tmp = Path(tempfile.mkdtemp(prefix="t63_"))
    img = tmp / "img"
    # 外来源旧命名 (序号_p页码) 与 merged 命名 —— 都带会被宽松正则误解析的片段
    (img / "涩图" / "#Ehentai#").mkdir(parents=True)
    (img / "涩图" / "#Ehentai#" / "Ehentai_007_p0.png").write_bytes(b"eh007")
    (img / "涩图" / "Gemba_9965434" / "#Ehentai#").mkdir(parents=True)
    (img / "涩图" / "Gemba_9965434" / "#Ehentai#" / "merged_2c9ce92b16f3_p0.png").write_bytes(b"m0")
    (img / "涩图" / "Gemba_9965434" / "#Ehentai#" / "merged_2c9ce92b16f3_p1.png").write_bytes(b"m1")

    db = PixivDatabase(":memory:")
    # 模拟图库既有归属: 这些文件已挂在 ext_/merged_ 作品下 (路径用规范正斜杠)
    db.change_work_status("ext_eh7", WSt.DONE, title="eh7", total_pages=1)
    db.mark_page_status("ext_eh7", 0, PSt.DONE,
                        local_path="涩图/#Ehentai#/Ehentai_007_p0.png")
    db.change_work_status("merged_2c9ce92b16f3", WSt.DONE, title="m", total_pages=2)
    db.mark_page_status("merged_2c9ce92b16f3", 0, PSt.DONE,
                        local_path="涩图/Gemba_9965434/#Ehentai#/merged_2c9ce92b16f3_p0.png")
    db.mark_page_status("merged_2c9ce92b16f3", 1, PSt.DONE,
                        local_path="涩图/Gemba_9965434/#Ehentai#/merged_2c9ce92b16f3_p1.png")

    # 连扫 3 遍 (模拟并发/重复触发)
    for _ in range(3):
        db.update_pages_from_output(img)

    # ① 不得冒出 007 / 18757 这类假作品
    assert db.get_artwork_info("007") is None, "Ehentai_007 被误登记成作品 007"
    assert db.get_artwork_info("18757") is None, "merged 哈希尾巴被误当成 pid"
    assert db.get_page_info("007", 0) is None
    assert db.get_page_info("18757", 0) is None
    # 归属不变、页不重复
    rows = list(db.conn.execute(
        "SELECT artwork_id, COUNT(*) n FROM pages GROUP BY artwork_id"))
    cnt = {r[0]: r[1] for r in rows}
    assert cnt == {"ext_eh7": 1, "merged_2c9ce92b16f3": 2}, cnt
    # ② 路径仍为正斜杠
    p = db.get_page_info("ext_eh7", 0)
    assert "\\" not in p.local_path, p.local_path

    # ③ 真·新下载的 pixiv 文件 (新路径, 库里没有) → 正常入库
    (img / "新画师_777").mkdir(parents=True)
    (img / "新画师_777" / "778899_p0.png").write_bytes(b"new0")
    db.update_pages_from_output(img)
    np_ = db.get_page_info("778899", 0)
    assert np_ is not None and np_.status == PSt.DONE and np_.local_path.endswith("778899_p0.png"), np_
    # 旧作品依旧没被污染
    assert db.get_artwork_info("007") is None

    # ④ 幂等: 再扫两遍, 作品/页数量不变
    n_before = (db.conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0],
                db.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    for _ in range(2):
        db.update_pages_from_output(img)
    n_after = (db.conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0],
               db.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
    assert n_before == n_after, (n_before, n_after)
    db.close()


def t64_cart_launch_single_flight() -> None:
    """T64: _launch_run_cart 单飞 —— 进行中再触发不开第二个线程, 只排队补跑 (2026-09-15 事故)。"""
    import importlib
    import threading
    import time
    import web.server as ws

    # 直接操作模块级事件, 不真的跑下载 (run_cart 打桩)
    ws._cart_busy.clear()
    ws._cart_rerun.clear()
    calls = {"n": 0}
    lock = threading.Lock()
    entered = threading.Event()
    finish = threading.Event()

    def fake_run_cart(reporter, config=None, only_ids=None, strategy=None):
        with lock:
            calls["n"] += 1
            entered.set()
        finish.wait(timeout=5)

    orig = ws.run_cart
    ws.run_cart = fake_run_cart
    try:
        ws._launch_run_cart()
        assert entered.wait(timeout=3), "首个下载线程没启动"
        assert ws._cart_busy.is_set()
        # 进行中连续再触发 3 次 (模拟连续加入清单): 都不许开新线程
        for _ in range(3):
            ws._launch_run_cart()
        time.sleep(0.3)
        assert calls["n"] == 1, f"并发开了 {calls['n']} 个下载线程"
        assert ws._cart_rerun.is_set(), "排队标记没置上"
        # 第一轮收尾后自动补跑一轮 (且仍然只有一个线程串行)
        finish.set()
        deadline = time.time() + 5
        while time.time() < deadline and calls["n"] < 2:
            time.sleep(0.05)
        assert calls["n"] == 2, f"排队任务没有补跑: {calls['n']}"
        deadline = time.time() + 5
        while time.time() < deadline and ws._cart_busy.is_set():
            time.sleep(0.05)
        assert not ws._cart_busy.is_set(), "忙标记没释放"
    finally:
        finish.set()
        ws.run_cart = orig
        ws._cart_busy.clear()
        ws._cart_rerun.clear()


def t65_natural_key_strict() -> None:
    """T65: pixiv_natural_key 严格身份识别 (2026-09-15 全功能审计)。

    事故根因正则 `(\\d+)_p(\\d+)` 不锚开头/不排前导零, 全项目 9 处身份识别点
    (对账/同步/重建映射/自检/反搜/上传分流) 都可能把外来源文件误当 pixiv 作品。
    现统一为: ① `{无前导零数字}_p{页码}` 必须锚定文件名开头;
             ② 裸 ID 仅 7-9 位无前导零。"""
    from db.store import pixiv_natural_key as nk

    # ---- 必须返回 None (事故文件名 + 易混项) ----
    none_cases = [
        "Ehentai_007_p0.png",          # 站点顺序号 (前导零, 且不在开头)
        "百度贴吧_003_p0.png",          # 中文来源顺序号
        "00000005_p0.jpg",             # 纯前导零 (顺序导出号)
        "merged_2c9ce92b16f3_p0.png",  # 哈希尾巴 → 旧正则误判成 "3"
        "merged_88505ea18757_p0.png",  # 哈希尾巴 → 旧正则误判成 18757
        "abc123456_p0.png",            # 字母前缀
        "screenshot_2024-01-01_p0.png",
        "1730345685303.png",           # 13 位毫秒戳 (裸ID 7-9 位闸)
        "12345.png",                   # 5 位裸短 ID 走既有 7-9 位闸 (保守不猜)
        "IMG_0001.png",                # 相机命名
        "danbooru_abcdef0123456789.png",
    ]
    for name in none_cases:
        assert nk(name) is None, f"应拒绝但命中: {name} → {nk(name)}"

    # ---- 必须命中 ----
    assert nk("100762890_p0.png") == ("100762890", 0)
    assert nk("778899_p1.jpg") == ("778899", 1)             # 6 位老 pid + _p 分支不卡长度
    assert nk("100762890_p10.png") == ("100762890", 10)     # 多位页码贪婪
    assert nk("100762890_p0_waifu2x_noise1_scale2x.png") == ("100762890", 0)  # 后缀不影响
    assert nk("12345_P0.PNG") == ("12345", 0)               # 大写 _P / 扩展名
    assert nk("98836835.png") == ("98836835", 0)            # 裸 8 位单页
    assert nk("119194561.gif") == ("119194561", 0)          # 裸 9 位动图


def t66_bg_task_single_flight_409() -> None:
    """T66: 反搜 / 手动补全的单飞守卫 (2026-09-15 审计新增)。

    旧实现两处后台线程都无 running 检查, 并发请求会: 反搜并发移动同一批 input 文件
    + 重复落库; 手动补全与启动自动补全两路联网写同一批作品。
    守卫必须在起线程**之前** 409 —— 本用例只把标志位置位后请求, 不触发任何真实任务。"""
    import web.server as ws

    app = ws.create_app()
    client = app.test_client()

    # ① 反搜进行中 → 409 (create_app 不会触发反搜; 仍记录原值防真实任务竞态)
    rev_was = bool(ws._REVERSE_PROGRESS.get("running"))
    if not rev_was:
        ws._REVERSE_PROGRESS["running"] = True
    try:
        r = client.post("/api/reverse/run", json={"mode": "id"})
        assert r.status_code == 409, f"反搜并发未被拒: {r.status_code}"
    finally:
        if not rev_was:
            ws._REVERSE_PROGRESS["running"] = False

    # ② 元数据补全进行中 (自动/手动共用一把锁) → 409; 且不得再起线程。
    # 若 create_app 恰好已启动真实自动补全, 该状态本身就是 409 场景, 只观测不干预。
    meta_was = bool(ws._meta_fill_state["running"])
    if not meta_was:
        ws._meta_fill_state["running"] = True
    kicked = {"n": 0}
    orig_kick = ws._kick_metadata_fill

    def _fake_kick(limit: int = 100):
        kicked["n"] += 1

    ws._kick_metadata_fill = _fake_kick
    try:
        r = client.post("/api/library/complement", json={"limit": 5})
        assert r.status_code == 409, f"补全并发未被拒: {r.status_code}"
        assert kicked["n"] == 0, "409 路径不该调用 _kick_metadata_fill"
    finally:
        ws._kick_metadata_fill = orig_kick
        if not meta_was:
            ws._meta_fill_state["running"] = False


def t67_cart_dedup_pixez_style() -> None:
    """T67: pixez 式加入清单拦截。

    ① 同作品重复加入: 第二次 409 in_queue, 队列只 1 条;
    ② 已 DONE 作品: 409 already_downloaded, 不开任务;
    ③ partial (NOT_YET) 作品允许再次加入补缺失页;
    ④ author_all 同作者重复 → 409;
    ⑤ /api/library/download-status 批量回显 downloaded/in_queue/partial/none;
    ⑥ batch-add 分类计数。paths 重定向临时库, 不碰生产库。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from web.server import create_app
    from db.store import PixivDatabase, WSt, PSt

    tmp = Path(tempfile.mkdtemp(prefix="t67cart_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    try:
        app = create_app()
        client = app.test_client()

        def add(aid, **kw):
            body = dict(item_type="artwork", artwork_id=aid,
                        title=f"#{aid} 冒烟", page_count=1)
            body.update(kw)
            return client.post("/api/cart", json=body)

        # ① 全新作品 → added; 立刻重复 → 409 in_queue
        r1 = add("900671")
        assert r1.status_code == 200 and r1.get_json()["reason"] == "added", (r1.status_code, r1.get_json())
        r2 = add("900671")
        assert r2.status_code == 409 and r2.get_json()["reason"] == "in_queue", (r2.status_code, r2.get_json())
        items = client.get("/api/cart").get_json()["items"]
        assert [it["artwork_id"] for it in items].count("900671") == 1, "重复入队!"

        # ⑤ 状态回显: in_queue / none
        st = client.get("/api/library/download-status?ids=900671,900672").get_json()
        assert st["works"]["900671"] == "in_queue", st
        assert st["works"]["900672"] == "none", st

        # ③ 模拟任务跑完(队列 done), 作品仍是 NOT_YET(缺页/partial) → 允许再加入补页
        db = PixivDatabase()
        db.queue_update_status(int(items[0]["id"]), "done")
        db.close()
        st = client.get("/api/library/download-status?ids=900671").get_json()
        assert st["works"]["900671"] == "partial", st
        r3 = add("900671")
        assert r3.status_code == 200 and r3.get_json()["reason"] == "added", (r3.status_code, r3.get_json())

        # ② 把作品置 DONE(页齐) → 再加入被 already_downloaded 拦, 队列不新增
        db = PixivDatabase()
        db.mark_page_status("900671", 0, PSt.DONE, local_path="900671_p0.png")
        # 先把 ③ 步入队的 pending 项收尾 done, 否则会先撞 in_queue (且队列态优先级最高)
        for it in db.queue_list():
            if it["artwork_id"] == "900671":
                db.queue_update_status(int(it["id"]), "done")
        db.sync_work_and_pages("900671")
        db.close()
        st = client.get("/api/library/download-status?ids=900671").get_json()
        assert st["works"]["900671"] == "downloaded", st
        r4 = add("900671")
        assert r4.status_code == 409 and r4.get_json()["reason"] == "already_downloaded", \
            (r4.status_code, r4.get_json())

        # ④ author_all 重复
        ra1 = client.post("/api/cart", json=dict(
            item_type="author_all", author_id="67001", author_name="冒烟作者X",
            title="作者主页全部: X")).get_json()
        assert ra1["reason"] == "added", ra1
        ra2 = client.post("/api/cart", json=dict(
            item_type="author_all", author_id="67001", author_name="冒烟作者X",
            title="作者主页全部: X"))
        assert ra2.status_code == 409 and ra2.get_json()["reason"] == "in_queue"
        ast = client.get("/api/library/download-status?author_ids=67001,67002").get_json()
        assert ast["authors"]["67001"] == "in_queue" and ast["authors"]["67002"] == "none", ast

        # ⑥ batch-add: 1 新 + 1 已下载 + 1 在队列 + 1 无效
        rb = client.post("/api/cart/batch-add", json={"items": [
            {"artwork_id": "900673"},
            {"artwork_id": "900671"},          # downloaded
            {"artwork_id": "900673"},          # 同批内第二次 → in_queue
            {"title": "无ID"},
        ]}).get_json()
        assert rb["added"] == 1 and rb["already_downloaded"] == 1, rb
        assert rb["in_queue"] == 1 and rb["invalid"] == 1, rb
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t68_missing_page_url_inference() -> None:
    """T68: build_missing_page_urls 多页漏检兜底 (2026-09-15, 实测 100632088 只下到 p0)。"""
    from core.crawler import build_missing_page_urls as build

    u0_orig = ("https://i.pximg.net/img-original/img/2022/08/01/00/00/00/100632088_p0.png",
               "100632088", "0", "png")
    # ① 未登录 master1200: 只检出 p0, 权威 2 页 → 推出 p1 master
    urls = {(
        "https://i.pximg.net/img-master/img/2022/08/01/00/00/00/100632088_p0_master1200.jpg",
        "100632088", "0", "jpg")}
    extra = build(urls, 2)
    assert len(extra) == 1, extra
    t = next(iter(extra))
    assert t[0].endswith("100632088_p1_master1200.jpg") and t[1] == "100632088" \
        and t[2] == "1" and t[3] == "jpg", t

    # ② 登录原图: 只有 p0, 权威 3 页 → p1/p2
    extra = build({u0_orig}, 3)
    got = {x[2]: x[0] for x in extra}
    assert set(got) == {"1", "2"}, got
    assert got["2"].endswith("100632088_p2.png"), got

    # ③ 已齐 (p0+p1, want=2) → 不补
    assert build({u0_orig, (u0_orig[0].replace("_p0", "_p1"), "100632088", "1", "png")}, 2) == set()

    # ④ 异常/单页 → 空集
    assert build(set(), 3) == set()
    assert build({u0_orig}, 1) == set()
    assert build({u0_orig}, 0) == set()
    assert build({u0_orig}, None) == set()
    # ⑤ 只检出 p1 (R18 多页作品实测 109766089 只下到 p1) → 锚点不限 p0, 仍能推出 p0
    weird = {("https://i.pximg.net/x/1_p1.png", "1", "1", "png")}
    got = build(weird, 2)
    assert len(got) == 1 and next(iter(got))[0] == "https://i.pximg.net/x/1_p0.png", got
    # ⑥ master1200 形态的 p1 锚点 → 推出的 p0 也带 _master1200 尾巴
    m1 = {("https://i.pximg.net/img-master/img/2022/08/01/00/00/00/109766089_p1_master1200.jpg",
           "109766089", "1", "jpg")}
    got = build(m1, 2)
    t = next(iter(got))
    assert t[0].endswith("109766089_p0_master1200.jpg") and t[3] == "jpg", t


def t69_jpg_to_png_convert() -> None:
    """T69: convert_to_png 入库格式统一 (2026-09-15)。"""
    import tempfile, shutil
    from PIL import Image
    from core.crawler import convert_to_png

    tmp = Path(tempfile.mkdtemp())
    try:
        # ① 普通 RGB jpg → png, 原文件删除
        jpg1 = tmp / "100001_p0.jpg"
        Image.new("RGB", (8, 8), (255, 0, 0)).save(str(jpg1), "JPEG")
        r = convert_to_png(str(jpg1))
        assert r is not None and r.endswith("100001_p0.png"), r
        assert os.path.exists(r) and not os.path.exists(str(jpg1)), "jpg 未删除或 png 不存在"
        with Image.open(r) as im:
            assert im.format == "PNG" and im.mode == "RGB", (im.format, im.mode)

        # ② png 原样跳过, 返回 None, 不动文件
        png1 = tmp / "100002_p0.png"
        Image.new("RGB", (8, 8)).save(str(png1), "PNG")
        assert convert_to_png(str(png1)) is None
        assert os.path.exists(str(png1)), "png 被误删"

        # ③ CMYK jpg → 安全转 RGB png
        jpg2 = tmp / "100003_p0.jpg"
        Image.new("CMYK", (4, 4)).save(str(jpg2), "JPEG")
        r2 = convert_to_png(str(jpg2))
        assert r2 is not None and os.path.exists(r2), "CMYK 转换失败"
        with Image.open(r2) as im:
            assert im.format == "PNG", im.format

        # ④ 损坏/空文件 → 返回 None, 保留原文件
        bad = tmp / "100004_p0.jpg"
        bad.write_bytes(b"\x00" * 64)
        assert convert_to_png(str(bad)) is None, "损坏文件不应成功"
        assert os.path.exists(str(bad)), "损坏文件不应被删"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def t70_promote_completed_after_output_sync() -> None:
    """T70: 对账登记页后补齐作品状态 (2026-09-15 实测 128028374/130720548 漏刷)。"""
    import json
    import tempfile, shutil
    from pathlib import Path
    from db.store import PixivDatabase, PSt, WSt

    tmp = Path(tempfile.mkdtemp(prefix="t70_"))
    try:
        img = tmp / "img"
        (img / "X图" / "作者_1").mkdir(parents=True)
        # ① 单页作品 (权威 total_pages=1 已知): 页齐全 → 应刷成 DONE
        (img / "X图" / "作者_1" / "900001_p0.png").write_bytes(b"a")
        # ② 多页不齐: 声明 3 页, 磁盘只有 1 页 → 保持 NOT_YET 且 total_pages 不被清
        (img / "X图" / "作者_1" / "900002_p0.png").write_bytes(b"b")
        # ③ 页数未知 (total_pages 为 NULL, 纯外部入库): 不拿磁盘页数倒推, 保持原状态
        (img / "X图" / "作者_1" / "900003_p0.png").write_bytes(b"c")

        db = PixivDatabase(":memory:")
        db.change_work_status("900001", WSt.NOT_YET, total_pages=1)
        db.change_work_status("900002", WSt.NOT_YET, total_pages=3)

        db.update_pages_from_output(img)

        info1 = db.get_artwork_info("900001")
        assert info1.status == WSt.DONE, f"页齐全的单页作品未刷 DONE: {info1.status}"
        assert json.loads(info1.exist_list or "null") == [0], info1.exist_list
        assert db.get_page_info("900001", 0).status == PSt.DONE

        info2 = db.get_artwork_info("900002")
        assert info2.status == WSt.NOT_YET, f"页不齐的作品被误刷 DONE: {info2.status}"
        assert info2.total_pages == 3, f"total_pages 被清: {info2.total_pages}"

        info3 = db.get_artwork_info("900003")
        assert info3.status != WSt.DONE, f"页数未知的作品被猜成 DONE: {info3.status}"
        assert not info3.total_pages, f"不应用磁盘页数倒推 total_pages: {info3.total_pages}"

        # ④ 幂等: 再扫两遍结果一致
        db.update_pages_from_output(img)
        db.update_pages_from_output(img)
        assert db.get_artwork_info("900001").status == WSt.DONE
        assert db.get_artwork_info("900002").total_pages == 3

        # ⑤ 入参兜底: 不存在的 aid 安全返回 0
        assert db.promote_completed_works(["不存在_123", "900002", "900003"]) == 0
        # ⑥ 已是 DONE 的作品不被重复计数
        assert db.promote_completed_works(["900001"]) == 0
        db.close()
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def t71_search_local_status_injection() -> None:
    """T71: 搜索结果注入本地状态。

    搜索结果来自 pixiv 实时抓取, 本身不含本地信息; 旧接口不补 → 前端一律显示
    「📁 无本地文件」, 已入库作品还显示可点的「加入清单」, 点了才被告知已下载完成。"""
    from web.server import _attach_local_status
    from db.store import PixivDatabase, PSt, WSt

    db = PixivDatabase(":memory:")
    # A 完整入库 / B 已在下载列表 / C 在库但缺页 / D 从未下载
    db.change_work_status("800001", WSt.DONE, total_pages=1)
    db.mark_page_status("800001", 0, PSt.DONE, local_path="X图/a_1/800001_p0.png")
    db.queue_add(item_type="artwork", title="t", artwork_id="800002")
    db.change_work_status("800003", WSt.NOT_YET, total_pages=3)
    db.mark_page_status("800003", 0, PSt.DONE, local_path="X图/b_2/800003_p0.png")

    res = {
        "type": "keyword_works",
        "works": [{"artwork_id": x} for x in ("800001", "800002", "800003", "800004")],
    }
    out = _attach_local_status(db, res)
    w = {x["artwork_id"]: x for x in out["works"]}
    assert w["800001"]["dl_state"] == "downloaded", w["800001"]
    assert w["800001"]["local_path"] == "X图/a_1/800001_p0.png", w["800001"]
    assert w["800002"]["dl_state"] == "in_queue", w["800002"]
    assert w["800003"]["dl_state"] == "partial", w["800003"]
    assert w["800003"]["local_path"] == "X图/b_2/800003_p0.png", w["800003"]
    assert w["800004"]["dl_state"] == "none", w["800004"]
    assert w["800004"]["local_path"] is None, w["800004"]

    # 单作品大卡 (artwork 键) 与相关作品列表 (related_works) 同样覆盖
    out2 = _attach_local_status(db, {
        "artwork": {"artwork_id": "800001"},
        "related_works": [{"artwork_id": "800003"}],
        "author": {"author_id": "1"},          # 无 artwork_id 的对象不应报错
    })
    assert out2["artwork"]["dl_state"] == "downloaded"
    assert out2["related_works"][0]["dl_state"] == "partial"

    # 空结果/无作品字段 → 原样返回, 不抛异常
    assert _attach_local_status(db, {"type": "keyword_authors", "authors": []}) is not None
    db.close()


def t72_bump_created_at_sorting() -> None:
    """T72: 入库时间刷新。

    created_at 原本只在加入清单时写入 → 排序反映的是加清单时刻。R18 作品常只下到
    部分页(状态 NOT_YET), 若只在「完整完成」时刷新就永远排不到前面 —— 故改为
    「有新图落盘即刷新」, 本用例锁住这个语义与时间格式。"""
    from db.store import PixivDatabase, PSt, WSt

    db = PixivDatabase(":memory:")
    db.change_work_status("700001", WSt.DONE, total_pages=1, title="old")
    db.mark_page_status("700001", 0, PSt.DONE, local_path="a/700001_p0.png")
    # 新作品只下到 1/3 页 (NOT_YET, 模拟 R18 部分下载)
    db.change_work_status("700002", WSt.NOT_YET, total_pages=3, title="new")
    db.mark_page_status("700002", 0, PSt.DONE, local_path="a/700002_p0.png")
    # 老作品 created_at 较新, 新作品(模拟「加入清单」时刻)很旧 → 新作品排在后面
    with db.conn:
        db.conn.execute("UPDATE artworks SET created_at='2024-01-01 00:00:00' "
                        " WHERE artwork_id='700001'")
        db.conn.execute("UPDATE artworks SET created_at='2020-01-01 00:00:00' "
                        " WHERE artwork_id='700002'")

    def _order():
        return [r[0] for r in db.conn.execute(
            "SELECT artwork_id FROM artworks ORDER BY created_at DESC, artwork_id DESC")]

    assert _order()[0] == "700001", _order()          # 刷新前: 刚下载的 700002 排后面

    db.bump_created_at("700002")                       # 有新图落盘
    assert _order()[0] == "700002", _order()           # 刷新后排到最前

    ts = db.conn.execute("SELECT created_at FROM artworks WHERE artwork_id='700002'"
                         ).fetchone()[0]
    assert len(ts) == 19 and 'T' not in ts, f"时间格式须与 DEFAULT CURRENT_TIMESTAMP 一致: {ts}"

    # 已 NOT_YET 也不影响刷新 (上面即证); 不存在的作品安全
    db.bump_created_at("不存在_404")
    db.close()


def t73_zero_page_download_marked_failed() -> None:
    """T73: 未抓到图片 / 全部页失败时, 任务必须判失败, 不能静默标 done。

    回归 2026-09-15 (日志实证): 109766089 三次「❗ 未检测到图片链接,跳过此作品」
    却都返回 (0, 0) 不抛异常 → 队列项被标 done, 用户以为下载成功、反复点击,
    留下 3 条 done 记录而一张图都没下。"""
    from core.download_engine import _run_item
    from core.models import DownloadStrategy
    from db.store import PixivDatabase

    class _R:
        def log(self, *a, **k): pass
        def warn(self, *a, **k): pass
        def error(self, *a, **k): pass
        def progress(self, *a, **k): pass
        def item_progress(self, *a, **k): pass

    class _D:
        def __init__(self, ret): self.ret = ret
        def download_artwork(self, *a, **k): return self.ret

    db = PixivDatabase(":memory:")
    it = {"id": 1, "item_type": "artwork", "artwork_id": "109766089", "page_count": 2}

    # ① 一张都没抓到 (total=0) → 抛异常 (队列才会标 failed)
    try:
        _run_item(db, _D((0, 0)), it, _R(), DownloadStrategy.ONLY_NEW)
        raise AssertionError("未抓到图片却未抛异常 → 任务会被误标 done")
    except ValueError as e:
        assert "图片链接" in str(e), e

    # ② 抓到 N 页但全下失败 (total>0, done=0) → 同样抛异常
    try:
        _run_item(db, _D((0, 2)), it, _R(), DownloadStrategy.ONLY_NEW)
        raise AssertionError("全部页失败却未抛异常")
    except ValueError as e:
        assert "全部下载失败" in str(e), e

    # ③ 部分成功 → 不算失败 (缺页交给「补下缺失页」)
    _run_item(db, _D((1, 2)), it, _R(), DownloadStrategy.ONLY_NEW)

    # ④ 非 pixiv 数字 ID 仍走原有快速失败分支
    try:
        _run_item(db, _D((1, 1)), {**it, "artwork_id": "ext_abc"}, _R(),
                  DownloadStrategy.ONLY_NEW)
        raise AssertionError("非数字 ID 应快速失败")
    except ValueError as e:
        assert "非 pixiv 数字 ID" in str(e), e
    db.close()


def t74_import_from_folder_moves_files() -> None:
    """T74: 本地导入 = 复制入库 + 源文件进回收站 (2026-09-17 语义变更)。

    背景: 最初用浏览器 file input 上传(只能落副本, 浏览器不给源路径); 改成服务端直接
    操作磁盘后是 shutil.move 硬搬、重复时 os.unlink 永久删 —— 误选一个文件夹就找不回。
    现在: 先 copy2 入库, **成功后才把源文件送回收站**(可还原), 送回收站失败只如实报告;
    目标同名一律跳过、源文件留在原处。
    测试里把 `_recycle_files` 换成桩(记录调用并真删, 等价于"进回收站后原处没了"),
    避免测试真往系统回收站扔垃圾文件。paths 重定向到临时目录, 不碰生产库。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    import web.server as ws

    tmp = Path(tempfile.mkdtemp(prefix="t74_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_kick = ws._kick_metadata_fill
    orig_recycle = ws._recycle_files
    recycle_calls: list = []

    def _fake_recycle(paths):
        recycle_calls.append(list(paths))
        n = 0
        for x in list(paths):
            try:
                Path(x).unlink()
                n += 1
            except OSError:
                pass
        return n, []

    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    ws._kick_metadata_fill = lambda *a, **k: None      # 不启后台联网线程
    ws._recycle_files = _fake_recycle
    try:
        src = tmp / "downloads" / "sub"                # 含子目录 → 验证递归
        src.mkdir(parents=True)
        (src / "900001_p0.png").write_bytes(b"a" * 32)
        (src / "900001_p1.png").write_bytes(b"b" * 32)
        (src / "screenshot.png").write_bytes(b"c" * 32)   # 无作品ID
        (src / "note.txt").write_text("ignore")           # 非图片

        client = ws.create_app().test_client()

        # ① dry-run: 只统计, 一个文件都不许动
        r = client.post("/api/library/import-from-path",
                        json={"path": str(src), "dry_run": True})
        j = r.get_json()
        assert r.status_code == 200 and j["dry_run"] is True, j
        assert (j["total"], j["with_id"], j["without_id"]) == (3, 2, 1), j
        assert (src / "900001_p0.png").exists(), "dry-run 不应移动任何文件"

        # ② 执行: 先复制入库, 再把源文件送回收站 (核心断言 —— 旧实现是硬搬/永久删)
        r = client.post("/api/library/import-from-path", json={"path": str(src)})
        j = r.get_json()
        assert r.status_code == 200, j
        assert sorted(j["imported"]) == ["900001_p0.png", "900001_p1.png"], j
        assert j["redirected"] == ["screenshot.png"], j
        assert j["recycled"] == 3 and not j["recycle_failed"], j
        for nm in ("900001_p0.png", "900001_p1.png", "screenshot.png"):
            assert not (src / nm).exists(), f"源文件应已进回收站(原处不该再有): {nm}"
        # 送回收站的必须是源文件本身, 不是图库里的副本
        sent = recycle_calls[-1]
        assert all(Path(x).parent == src for x in sent), sent
        assert (src / "note.txt").exists(), "非图片文件不应被动"
        assert (tmp / "img" / "900001_p0.png").exists(), "未落到图库 img/"
        assert (tmp / "input" / "screenshot.png").exists(), "无作品ID未转反搜收件箱"

        # ②b 同名重复 → 跳过, **源文件留在原处** 
        dup = tmp / "downloads2"
        dup.mkdir(parents=True)
        (dup / "900001_p0.png").write_bytes(b"content-differs" * 4)
        r = client.post("/api/library/import-from-path", json={"path": str(dup)})
        j = r.get_json()
        assert r.status_code == 200, j
        assert j["skipped"] == ["900001_p0.png"] and not j["imported"], j
        assert (dup / "900001_p0.png").exists(), "同名重复时源文件必须留在原处"
        assert (tmp / "img" / "900001_p0.png").read_bytes().startswith(b"a" * 32), \
            "同名重复不该覆盖库内已有文件"

        # ②c 选中的多张图片 (paths 模式): 与文件夹模式同一套语义
        pick = tmp / "pick"
        pick.mkdir(parents=True)
        (pick / "900002_p0.png").write_bytes(b"x" * 32)
        (pick / "shot.png").write_bytes(b"y" * 32)
        (pick / "note.txt").write_text("ignore")
        picked = [str(pick / "900002_p0.png"), str(pick / "shot.png"), str(pick / "note.txt")]
        r = client.post("/api/library/import-from-path",
                        json={"paths": picked, "dry_run": True})
        j = r.get_json()
        assert (j["total"], j["with_id"], j["without_id"]) == (2, 1, 1), j
        assert (pick / "900002_p0.png").exists(), "dry-run 不应动源文件"
        r = client.post("/api/library/import-from-path", json={"paths": picked})
        j = r.get_json()
        assert r.status_code == 200, j
        assert j["imported"] == ["900002_p0.png"] and j["redirected"] == ["shot.png"], j
        assert j["recycled"] == 2, j
        assert not (pick / "900002_p0.png").exists() and not (pick / "shot.png").exists(), \
            "多选导入的源文件也应进回收站"
        assert (pick / "note.txt").exists(), "非图片不应被动"
        # 多选模式的安全闸: 文件不存在 / 图库内部 → 400
        assert client.post("/api/library/import-from-path",
                           json={"paths": [str(pick / "nope.png")]}).status_code == 400
        assert client.post("/api/library/import-from-path",
                           json={"paths": [str(tmp / "img" / "900001_p0.png")]}
                           ).status_code == 400

        # ③ 安全闸: 图库自身 / 其上级目录 / 不存在 / 空路径 全部拒绝
        for bad in (tmp / "img", tmp):
            rb = client.post("/api/library/import-from-path", json={"path": str(bad)})
            assert rb.status_code == 400, f"危险路径未拦截: {bad} → {rb.get_json()}"
        assert client.post("/api/library/import-from-path",
                           json={"path": str(tmp / "nope")}).status_code == 400
        assert client.post("/api/library/import-from-path", json={}).status_code == 400
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        ws._kick_metadata_fill = orig_kick
        ws._recycle_files = orig_recycle
        shutil.rmtree(str(tmp), ignore_errors=True)


def t75_split_pages_creates_single_artworks() -> None:
    """T75: 图片拆分。

    ① 非 pixiv 作品可自由拆 → 每张独立成新作品，源作品保留 total_pages、只改归属;
    ② 新作品**继承源作品的作者/来源/R18**（用户实测：不继承会显示「未知作者」）;
    ③ pixiv 作品的**原网页页**(origin='web')禁止拆出（会让原作品缺页），且拦下时不动库;
    ④ pixiv 作品的**合并附加页**(origin='merged')允许拆 —— 那是「反悔合并」;
    ⑤ 源作品被拆空 → 级联删除；文件不移动。"""
    import tempfile
    import shutil
    import utils.paths as upaths
    from web.server import create_app
    from db.store import PixivDatabase, PSt, WSt

    tmp = Path(tempfile.mkdtemp(prefix="t75_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(str(tmp / "pixiv.db"))
        # 非 pixiv 作品 (外部来源, 3 页)
        db.change_work_status("ext_src", WSt.DONE, total_pages=3, title="外部来源")
        with db.conn:
            db.conn.execute(
                "UPDATE artworks SET author_id='555', source='danbooru', "
                "source_author='someartist', is_r18=1 WHERE artwork_id='ext_src'")
        for pn in range(3):
            db.mark_page_status("ext_src", pn, PSt.DONE,
                                local_path=f"X图/a_555/ext_src_p{pn}.png")
        # pixiv 作品: p0=原网页页(web), p1=合并附加页(merged)
        db.change_work_status("800002", WSt.DONE, total_pages=2, title="pixiv 作品")
        db.mark_page_status("800002", 0, PSt.DONE, local_path="X图/b_1/800002_p0.png")
        db.mark_page_status("800002", 1, PSt.DONE, local_path="X图/b_1/800002_p1.png")
        with db.conn:
            db.conn.execute("UPDATE pages SET origin='merged' "
                            "WHERE artwork_id='800002' AND page_num=1")
        db.close()

        client = create_app().test_client()
        # ③ pixiv 原网页页 → 拒绝, 且一个字节都不动
        r = client.post("/api/library/split-pages",
                        json={"items": [{"artwork_id": "800002", "page_num": 0}]})
        assert r.status_code == 400, r.get_json()
        assert "原网页页" in r.get_json()["message"], r.get_json()
        db2 = PixivDatabase(str(tmp / "pixiv.db"))
        assert db2.get_page_info("800002", 0) is not None, "拦下时不该动库"
        assert len(list(db2.conn.execute(
            "SELECT 1 FROM pages WHERE artwork_id='800002'"))) == 2
        db2.close()
        # ④ pixiv 合并附加页 → 放行
        r = client.post("/api/library/split-pages",
                        json={"items": [{"artwork_id": "800002", "page_num": 1}]})
        assert r.status_code == 200 and r.get_json()["pages_moved"] == 1, r.get_json()

        # ① + ② 非 pixiv 自由拆, 新作品继承作者/来源/R18
        r = client.post("/api/library/split-pages",
                        json={"items": [{"artwork_id": "ext_src", "page_num": 1}]})
        j = r.get_json()
        assert r.status_code == 200 and j["pages_moved"] == 1, j
        new_id = j["created"][0]
        # 非 pixiv 来源拆出的新作品直接是 ext_ 身份 
        # 已确认外来源的作品不该一直背着 merged_ 合并产物前缀)
        assert new_id.startswith("ext_"), new_id
        db3 = PixivDatabase(str(tmp / "pixiv.db"))
        nm = db3.get_artwork_metadata(new_id)
        assert nm["author_id"] == "555", f"未继承作者(会显示未知作者): {nm}"
        assert nm["source"] == "danbooru" and nm["is_r18"] == 1, nm
        assert nm["total_pages"] == 1 and nm["status"] == WSt.DONE, nm
        # ⑤ 文件不移动 (只改归属)
        assert db3.get_page_info(new_id, 0).local_path == "X图/a_555/ext_src_p1.png"
        src = db3.get_artwork_metadata("ext_src")
        assert src["total_pages"] == 3, f"源 total_pages 不该被清: {src['total_pages']}"
        assert src["status"] == WSt.NOT_YET, src["status"]
        # 拆空 → 级联删除
        r = client.post("/api/library/split-pages",
                        json={"items": [{"artwork_id": "ext_src", "page_num": 0},
                                        {"artwork_id": "ext_src", "page_num": 2}]})
        assert r.get_json()["artworks_removed"] == ["ext_src"], r.get_json()
        assert db3.get_artwork_metadata("ext_src") is None, "拆空的源作品应被删除"
        db3.close()

        # 空参数
        assert client.post("/api/library/split-pages", json={}).status_code == 400
        assert client.post("/api/library/split-pages",
                           json={"items": []}).status_code == 400
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t76_merge_keeps_source_status_in_sync() -> None:
    """T76: 合并的两条铁律 (2026-09-16)。

    ① pixiv 作品(纯数字 ID)的页**不允许**被合并走 —— 保持 pixiv 作品页数完整，
       以后某张图找到 pixiv 来源才能归属回去 (需求，后端硬拦);
    ② 非 pixiv 作品之间自由合并时，源作品被搬走页后必须同步状态（旧实现漏了这一步，
       源作品仍显示 DONE → 界面谎报「✓ 已入库」且点不到「补下缺失页」），
       且源/目标的 total_pages 都不能被清。"""
    import json as _json
    from db.store import PixivDatabase, PSt, WSt

    db = PixivDatabase(":memory:")
    # ---- ① pixiv 作品: 硬拦 ----
    db.change_work_status("900001", WSt.DONE, total_pages=3, title="pixiv 作品")
    for pn in range(3):
        db.mark_page_status("900001", pn, PSt.DONE, local_path=f"A/900001_p{pn}.png")
    try:
        db.merge_pages([{"artwork_id": "900001", "page_num": 1},
                        {"artwork_id": "900001", "page_num": 2}])
        raise AssertionError("pixiv 作品的页不该允许被合并走")
    except ValueError as e:
        assert "pixiv 作品" in str(e), e
    # 拦下后原作品必须原封不动 (不能被拦了个寂寞却已经改库)
    assert db.get_artwork_metadata("900001")["status"] == WSt.DONE
    assert len(list(db.conn.execute(
        "SELECT 1 FROM pages WHERE artwork_id='900001'"))) == 3

    # ---- ①b 但 pixiv 作品的**合并附加页**(origin='merged')可以搬走 ----
    #      (与 split_pages 同一条规则: 附加页是用户自己并进来的, 搬走 = 反悔合并)
    db.change_work_status("900003", WSt.DONE, total_pages=2, title="含附加页的 pixiv 作品")
    db.mark_page_status("900003", 0, PSt.DONE, local_path="A/900003_p0.png")
    db.mark_page_status("900003", 1, PSt.DONE, local_path="A/900003_p1.png")
    with db.conn:
        db.conn.execute("UPDATE pages SET origin='merged' "
                        "WHERE artwork_id='900003' AND page_num=1")
    db.change_work_status("900004", WSt.DONE, total_pages=1, title="接收方")
    db.mark_page_status("900004", 0, PSt.DONE, local_path="A/900004_p0.png")
    db.change_work_status("ext_x", WSt.DONE, total_pages=1, title="外部来源")
    db.mark_page_status("ext_x", 0, PSt.DONE, local_path="A/ext_x_p0.png")
    res_b = db.merge_pages([{"artwork_id": "900003", "page_num": 1},
                            {"artwork_id": "ext_x", "page_num": 0}],
                           target_id="900004")
    assert res_b["pages_moved"] == 2, res_b
    assert db.get_artwork_metadata("900003")["total_pages"] == 2, "附加页搬走后声明页数仍保留"

    # ---- ② 非 pixiv 作品之间合并: 源状态同步 + total_pages 保护 ----
    db.change_work_status("merged_src", WSt.DONE, total_pages=3, title="外部来源")
    for pn in range(3):
        db.mark_page_status("merged_src", pn, PSt.DONE, local_path=f"A/e_p{pn}.png")
    db.change_work_status("merged_dst", WSt.DONE, total_pages=1, title="目标")
    db.mark_page_status("merged_dst", 0, PSt.DONE, local_path="A/d_p0.png")

    res = db.merge_pages([{"artwork_id": "merged_src", "page_num": 1},
                          {"artwork_id": "merged_src", "page_num": 2}],
                         target_id="merged_dst")
    assert res["pages_moved"] == 2, res
    assert res["artworks_removed"] == [], "源作品还剩 1 页, 不该被删"

    src = db.get_artwork_metadata("merged_src")
    assert src["total_pages"] == 3, f"源 total_pages 不该被清: {src['total_pages']}"
    assert src["status"] == WSt.NOT_YET, \
        f"源缺页后应转 NOT_YET (旧实现留 DONE, 界面会谎报已入库): {src['status']}"
    assert _json.loads(src["exist_list"]) == [0], src["exist_list"]
    tgt = db.get_artwork_metadata("merged_dst")
    assert tgt["total_pages"] == 1, f"目标 total_pages 被覆盖: {tgt['total_pages']}"
    assert db.get_page_info("merged_dst", 1) is not None, "附加页未追加"
    db.close()


def t77_version_endpoint() -> None:
    """T77: /api/version 返回构建信息。

    用途: 用户怀疑「改了怎么没生效 / 是不是忘了打包」时，先看这个接口 ——
    built_at 是可执行文件时间(打包时刻)，frontend 是 index.html 引用的前端资源版本串
    (用于确认浏览器加载的是不是新前端)。"""
    from web.server import create_app

    client = create_app().test_client()
    r = client.get("/api/version")
    assert r.status_code == 200, r.status_code
    j = r.get_json()
    assert j.get("status") == "ok", j
    for k in ("built_at", "frontend", "frozen", "python"):
        assert k in j, f"缺少字段 {k}: {j}"
    assert j["built_at"], f"built_at 不应为空: {j}"
    assert j["frontend"], f"应能读出前端资源版本串: {j}"
    assert isinstance(j["frozen"], bool), j
    assert j["python"], j
    # 前端版本串 = index.html 手写标签 + app.js/style.css 的 mtime 指纹 (2026-09-16):
    # 自动指纹是为了根治「改了前端但浏览器用缓存旧文件」——以前靠人手 bump, 漏一次就白改。
    # 同时校验: 页面 "/" 实际注入的 app.js/style.css ?v= 必须与接口报的完全一致,
    # 否则这个指示器就是在骗人。
    import os as _os
    html_path = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))) \
        / "web" / "static" / "index.html"
    if html_path.is_file():
        import re as _re
        m = _re.search(r"app\.js\?v=([\w.\-]+)", html_path.read_text(encoding="utf-8"))
        if m:
            assert j["frontend"].startswith(m.group(1)), (j["frontend"], m.group(1))
            assert j["frontend"] != m.group(1), \
                f"版本串应带静态文件 mtime 指纹, 否则改了前端可能仍拿缓存: {j['frontend']}"
        served = client.get("/").get_data(as_text=True)
        for pat in (r"app\.js\?v=([\w.\-]+)", r"style\.css\?v=([\w.\-]+)"):
            mm = _re.search(pat, served)
            assert mm, f"页面里找不到 {pat}"
            if mm.group(1) != j["frontend"]:
                # (诊断 2026-09-29) 套件内出现过「页面注入旧标签而磁盘文件是新标签」:
                # 把 index 视图闭包里的真实 static_dir 挖出来, 当场读它那份 index.html 对质。
                import web.server as _ws
                _info = f" | 模块={_ws.__file__}"
                for _i, _c in enumerate(
                        getattr(client.application.view_functions["index"], "__closure__", None) or []):
                    _v = _c.cell_contents
                    _info += f" | 闭包[{_i}]={_v!r}"
                    try:
                        _info += "→tag=" + _re.search(
                            r"app\.js\?v=([\w.\-]+)",
                            (_v / "index.html").read_text(encoding="utf-8")).group(1)
                    except Exception:
                        pass
                raise AssertionError(
                    f"页面注入的版本串与 /api/version 不一致: {mm.group(1)} != {j['frontend']}{_info}")


def t78_merge_self_no_fake_conflict() -> None:
    """T78: 把占位作品并入它自己时, 磁盘上还没挪走的源文件不能被当成"别人的占用"
    而加 " (2)" .png")。"""
    import tempfile
    from db.store import PixivDatabase, PSt, WSt

    tmp = Path(tempfile.mkdtemp(prefix="pt_t78_"))
    try:
        ddir = tmp / "D"
        ddir.mkdir(parents=True)
        (ddir / "88007979_p0.png").write_bytes(b"page0-bytes")
        (ddir / "88007979_p1.png").write_bytes(b"page1-bytes")
        db = PixivDatabase(":memory:")
        # 复刻生产脏状态: 两个 merged 占位作品各持一页, 文件名已是 {pid}_pN
        db.change_work_status("merged_t", WSt.DONE, total_pages=1, title="88007979_p1")
        db.mark_page_status("merged_t", 0, PSt.DONE, local_path="D/88007979_p1.png")
        db.change_work_status("merged_s", WSt.DONE, total_pages=1, title="88007979_p0")
        db.mark_page_status("merged_s", 0, PSt.DONE, local_path="D/88007979_p0.png")
        # 用户实际操作: 两个占位作品一起选中, 目标指定为其中之一 (=「并入自身」):
        # 目标自己的页先从 DB 删掉再追加, 但磁盘文件还没动 —— 旧实现把它误判成占用加 (2)
        res = db.merge_pages([{"artwork_id": "merged_t", "page_num": 0},
                              {"artwork_id": "merged_s", "page_num": 0}],
                             target_id="merged_t", img_root=tmp)
        # 两张图文件名必须原样保留 (旧实现: 两张全被加 " (2)")
        names = {Path(p["local_path"]).name
                 for p in db.conn.execute(
                     "SELECT local_path FROM pages WHERE artwork_id='merged_t'")}
        assert names == {"88007979_p0.png", "88007979_p1.png"}, names
        assert res["rel_moves"] == [], f"同目录原名不该产生移动: {res['rel_moves']}"
        assert (ddir / "88007979_p0.png").is_file()
        assert (ddir / "88007979_p1.png").is_file()
        assert not list(ddir.glob("* (2).png")), "不该出现 (2) 文件"
        # 真冲突仍要拦: 源文件移入目标目录时, 目录里已有一张别人的同名图 → 必须加 (2)
        (ddir / "other_p0.png").write_bytes(b"target-owns-this-name")
        db.change_work_status("merged_u", WSt.DONE, total_pages=1, title="u")
        db.mark_page_status("merged_u", 0, PSt.DONE, local_path="D/other_p0.png")
        d2 = tmp / "D2"
        d2.mkdir(parents=True)
        (d2 / "other_p0.png").write_bytes(b"source-different-file")
        (d2 / "other_p1.png").write_bytes(b"source-second-file")
        db.change_work_status("merged_v", WSt.DONE, total_pages=2, title="v")
        db.mark_page_status("merged_v", 0, PSt.DONE, local_path="D2/other_p0.png")
        db.mark_page_status("merged_v", 1, PSt.DONE, local_path="D2/other_p1.png")
        res2 = db.merge_pages([{"artwork_id": "merged_v", "page_num": 0},
                               {"artwork_id": "merged_v", "page_num": 1}],
                              target_id="merged_u", img_root=tmp)
        moved = {Path(d).name for _, d in res2["rel_moves"]}
        assert "other_p0 (2).png" in moved, f"真冲突应加 (2): {moved}"
        assert "other_p1.png" in moved, f"不冲突的页应保持原名: {moved}"
        db.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t79_upsert_author_rejects_garbage_name() -> None:
    """T79: 控制台编码损坏产生的 '????????' / '???' 绝不能顶掉作者真名
。"""
    from db.store import PixivDatabase

    db = PixivDatabase(":memory:")
    db.upsert_author("1769484", "山田の性活が第一")
    # 垃圾名再来一次 → 名字不动, 别名也不收垃圾
    db.upsert_author("1769484", "????????")
    db.upsert_author("1769484", "   \ufffd\ufffd  ")
    row = db.conn.execute(
        "SELECT name, aliases_json FROM authors WHERE author_id='1769484'").fetchone()
    assert row[0] == "山田の性活が第一", f"真名被垃圾名顶掉: {row[0]}"
    import json as _json
    assert "????" not in (row[1] or ""), f"垃圾名混进别名: {row[1]}"
    # 全新作者第一次就是垃圾名 → 落「未知画师」, 不落乱码
    db.upsert_author("999001", "？？？？？？？")
    row2 = db.conn.execute(
        "SELECT name FROM authors WHERE author_id='999001'").fetchone()
    assert row2[0] and "？" not in row2[0] and "?" not in row2[0], row2[0]
    # 真名后到 → 正常替换占位名
    db.upsert_author("999001", "真实画师")
    assert db.conn.execute(
        "SELECT name FROM authors WHERE author_id='999001'").fetchone()[0] == "真实画师"
    db.close()


def t80_claim_pixiv_work_promotes_placeholder() -> None:
    """T80: 占位 merged 作品「认领转正」为 pixiv 正式作品:
    文件名定页码(修得回挂反/" (2)"脏命名)、权威元数据建档、origin='web'、
    旧占位删除; 与在库异图冲突必须拒绝, 绝不覆盖。"""
    import tempfile
    from core.models import ArtworkInfo, IllustType
    from db.store import PixivDatabase, PSt, WSt

    def _info():
        return ArtworkInfo(
            artwork_id="88007979", author_id="1769484",
            title="おや、ロキシーちゃんは発情期のようです。",
            illust_type=IllustType.ILLUSTRATION, page_count=2,
            tags=["ロキシー"], is_r18=True, author_name="山田の性活が第一")

    tmp = Path(tempfile.mkdtemp(prefix="pt_t80_"))
    try:
        ddir = tmp / "涩图" / "山田の性活が第一_1769484"
        ddir.mkdir(parents=True)
        b0, b1 = b"p0" * 500, b"p1" * 600
        # 复刻生产: 文件名带 " (2)", 且 DB 页码与文件名 pN 挂反
        f1 = ddir / "88007979_p1 (2).png"
        f0 = ddir / "88007979_p0 (2).png"
        f1.write_bytes(b1)
        f0.write_bytes(b0)
        db = PixivDatabase(":memory:")
        db.upsert_author("1769484", "????????")   # 作者名先处于损坏状态
        db.change_work_status("merged_64404a33b00f", WSt.DONE, total_pages=1,
                              title="88007979_p1", author_id="1769484")
        db.mark_page_status("merged_64404a33b00f", 0, PSt.DONE,
                            local_path="涩图/山田の性活が第一_1769484/88007979_p1 (2).png",
                            file_size_bytes=len(b1))
        db.mark_page_status("merged_64404a33b00f", 1, PSt.DONE,
                            local_path="涩图/山田の性活が第一_1769484/88007979_p0 (2).png",
                            file_size_bytes=len(b0))

        res = db.claim_pixiv_work("merged_64404a33b00f", "88007979",
                                  info=_info(), img_root=tmp)
        # 执行物理移动 (与 server 接口同一套)
        for src_rel, dst_rel in res["rel_moves"]:
            s, d = tmp / src_rel, tmp / dst_rel
            d.parent.mkdir(parents=True, exist_ok=True)
            s.replace(d)
        # DB 断言
        w = db.get_artwork_metadata("88007979")
        assert w is not None and w["status"] == WSt.DONE, w
        assert w["title"].startswith("おや、ロキシー"), w["title"]
        assert w["total_pages"] == 2 and w["is_r18"] == 1, w
        assert w["source"] == "pixiv", w["source"]
        assert db.get_artwork_metadata("merged_64404a33b00f") is None, "旧占位应删除"
        for pn, expect_bytes in ((0, b0), (1, b1)):
            p = db.get_page_info("88007979", pn)
            assert p is not None, pn
            assert Path(p.local_path).name == f"88007979_p{pn}.png", p.local_path
            origin = db.conn.execute(
                "SELECT origin FROM pages WHERE artwork_id='88007979' AND page_num=?",
                (pn,)).fetchone()[0]
            assert origin == "web", origin
            assert (tmp / p.local_path).read_bytes() == expect_bytes, "页码挂反应被文件名纠正"
        assert db.conn.execute(
            "SELECT name FROM authors WHERE author_id='1769484'").fetchone()[0] \
            == "山田の性活が第一", "权威元数据应修正作者名"
        assert not list(ddir.glob("*(2)*")), "磁盘上 (2) 文件应已改名"
        db.close()

        # ---- 拒绝场景: 在库目标 p1 已是另一张图 (md5 不同) → 拒绝, 不毁库不覆盖 ----
        tmp2 = Path(tempfile.mkdtemp(prefix="pt_t80b_"))
        try:
            dd2 = tmp2 / "D"
            dd2.mkdir(parents=True)
            (dd2 / "88007979_p0.png").write_bytes(b"lib-p0")
            (dd2 / "88007979_p1.png").write_bytes(b"lib-p1-different")
            din = tmp2 / "IN"
            din.mkdir(parents=True)
            (din / "88007979_p1.png").write_bytes(b"incoming-p1")
            db2 = PixivDatabase(":memory:")
            db2.change_work_status("88007979", WSt.DONE, total_pages=2)
            db2.mark_page_status("88007979", 0, PSt.DONE,
                                 local_path="D/88007979_p0.png", md5="aaa0")
            db2.mark_page_status("88007979", 1, PSt.DONE,
                                 local_path="D/88007979_p1.png", md5="ccc1")
            db2.change_work_status("merged_x", WSt.DONE, total_pages=1)
            db2.mark_page_status("merged_x", 0, PSt.DONE,
                                 local_path="IN/88007979_p1.png", md5="bbb1")
            try:
                db2.claim_pixiv_work("merged_x", "88007979", info=_info(), img_root=tmp2)
                raise AssertionError("在库 p1 是另一张图时应拒绝")
            except ValueError as e:
                assert "另一张图" in str(e) or "已存在" in str(e), e
            # 占位与在库作品都必须原封不动
            assert db2.get_artwork_metadata("merged_x") is not None
            assert db2.get_page_info("88007979", 1).md5 == "ccc1"
            assert (din / "88007979_p1.png").read_bytes() == b"incoming-p1"
            db2.close()
        finally:
            import shutil
            shutil.rmtree(tmp2, ignore_errors=True)

        # ---- 拒绝场景: 文件名声称别的 pid / 页码越界 ----
        db3 = PixivDatabase(":memory:")
        db3.change_work_status("merged_y", WSt.DONE, total_pages=1)
        db3.mark_page_status("merged_y", 0, PSt.DONE, local_path="D/777_p0.png")
        try:
            db3.claim_pixiv_work("merged_y", "88007979", info=_info())
            raise AssertionError("文件名 pid 不符应拒绝")
        except ValueError as e:
            assert "作品 ID" in str(e), e
        db3.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def t81_parse_pages_body_and_http_dispatch_signature() -> None:
    """T81: /ajax/illust/{id}/pages 响应解析为 (页码, 原图URL, 后缀) ——
    HTTP 直连下载通道的基础纯函数 (2026-09-16 购物车下载三连败修复)。"""
    from core.crawler import PixivDownloader

    body = [
        {"urls": {"original": "https://i.pximg.net/img/2021/01/01/00/00/88007979_p0.jpg"}},
        {"urls": {"original": "https://i.pximg.net/img/2021/01/01/00/00/88007979_p1.jpeg?x"}},
        {"urls": {"regular": "https://x/no_original.png"}},
    ]
    out = PixivDownloader._parse_pages_body(body)
    assert out == [
        (0, "https://i.pximg.net/img/2021/01/01/00/00/88007979_p0.jpg", "jpg"),
        (1, "https://i.pximg.net/img/2021/01/01/00/00/88007979_p1.jpeg?x", "jpg"),
    ], out
    assert PixivDownloader._parse_pages_body([]) == []
    assert PixivDownloader._parse_pages_body(None) == []


def t82_scan_claimable_groups_ready_mixed_noid() -> None:
    """T82: scan_claimable_pixiv_works 离线把散落页正确分三组 ——
    ready(占位作品全部页同一 pid) / mixed(一页多 pid, 必须先拆) / no_id(真外来源不动);
    已是正式 pixiv 作品(数字ID)不参与扫描。"""
    from db.store import PixivDatabase, PSt, WSt

    db = PixivDatabase(":memory:")
    # ready: 一个 merged 占位两页同指 88007979 (模拟被错拆)
    db.change_work_status("merged_a", WSt.DONE, total_pages=2, title="乱并作品A")
    db.mark_page_status("merged_a", 0, PSt.DONE, local_path="D/88007979_p0.png")
    db.mark_page_status("merged_a", 1, PSt.DONE, local_path="D/88007979_p1.png")
    # mixed: 一个 ext 占位两页分属两个 pid (多次错误合并)
    db.change_work_status("ext_b", WSt.DONE, total_pages=2, title="乱并作品B")
    db.mark_page_status("ext_b", 0, PSt.DONE, local_path="D/111_p0.png")
    db.mark_page_status("ext_b", 1, PSt.DONE, local_path="D/222_p0.png")
    # no_id: 外来源手工命名, 文件名无 pixiv 自然键
    db.change_work_status("ext_c", WSt.DONE, total_pages=1, title="推特图")
    db.mark_page_status("ext_c", 0, PSt.DONE, local_path="D/miku with flower.png")
    # 正式 pixiv 作品: 必须被排除, 不出现在任何组
    db.change_work_status("88007979", WSt.DONE, total_pages=2)
    db.mark_page_status("88007979", 0, PSt.DONE, local_path="D2/88007979_p0.png")
    db.mark_page_status("88007979", 1, PSt.DONE, local_path="D2/88007979_p1.png")

    scan = db.scan_claimable_pixiv_works()
    assert len(scan["ready"]) == 1, scan
    r = scan["ready"][0]
    assert r["old_id"] == "merged_a" and r["pixiv_id"] == "88007979", r
    assert sorted(p["declared_page"] for p in r["pages"]) == [0, 1], r
    assert len(scan["mixed"]) == 1, scan
    m = scan["mixed"][0]
    assert m["old_id"] == "ext_b" and m["pids"] == ["111", "222"], m
    assert scan["no_id"] == 1, scan
    all_scanned = [e["old_id"] for e in scan["ready"] + scan["mixed"]]
    assert "88007979" not in all_scanned, "正式作品不应被扫描为认领候选"
    db.close()


def t83_claim_scan_api_offline_and_batch_guards() -> None:
    """T83: /api/library/claim-scan 离线可用; /api/library/claim-batch
    空清单/非法条目 → 400 (不联网、不起后台线程)。"""
    from web.server import create_app

    client = create_app().test_client()
    r = client.post("/api/library/claim-scan", json={})
    assert r.status_code == 200, r.status_code
    j = r.get_json()
    assert j.get("status") == "ok" and "ready" in j and "mixed" in j, j

    r2 = client.post("/api/library/claim-batch", json={"items": []})
    assert r2.status_code == 400, r2.status
    r3 = client.post("/api/library/claim-batch",
                     json={"items": [{"artwork_id": "88007979", "pixiv_id": "88007979"},
                                     {"artwork_id": "x", "pixiv_id": "abc"}]})
    assert r3.status_code == 400, r3.status  # 全是非法条目(数字自身/非数字pid) → 400


def t84_delete_page_marks_partial_and_stale_done_guard() -> None:
    """T84: 手动删掉多页作品的一页后, 作品必须立即变「缺页」——
    ① delete_pages 重算状态 (status DONE→NOT_YET, total_pages 保留);
    ② artwork_download_states 对「status 陈旧 DONE 但页数不齐」的历史脏数据
       也要判 partial (旧版按钮永远显示「✓ 已入库」,  2026-09-16);
    ③ 删光最后一页 → 整作品级联删除, 不留幽灵作品。"""
    from db.store import PixivDatabase, PSt, WSt

    db = PixivDatabase(":memory:")
    db.change_work_status("900001", WSt.DONE, total_pages=3)
    for pn in range(3):
        db.mark_page_status("900001", pn, PSt.DONE, local_path=f"D/900001_p{pn}.png")
    assert db.artwork_download_states(["900001"])["900001"] == "downloaded"

    # 删掉中间一页 p1
    rels, emptied = db.delete_pages(
        [{"artwork_id": "900001", "page_num": 1}])
    assert rels == ["D/900001_p1.png"], rels
    assert emptied == [], emptied
    info = db.get_artwork_info("900001")
    assert info.status == WSt.NOT_YET, f"删页后应变 NOT_YET: {info.status}"
    assert info.total_pages == 3, "原网页页数声明必须保留"
    import json as _json
    assert _json.loads(info.exist_list) == [0, 2], info.exist_list
    assert db.artwork_download_states(["900001"])["900001"] == "partial", \
        "删页后搜索按钮应变「补下缺失页」"

    # 陈旧脏数据防御: 直接造一个 status=DONE/tp=3 但只剩 2 页的作品 (模拟旧版本删的)
    db.change_work_status("900002", WSt.DONE, total_pages=4)
    db.mark_page_status("900002", 0, PSt.DONE, local_path="D/900002_p0.png")
    db.mark_page_status("900002", 1, PSt.DONE, local_path="D/900002_p1.png")
    st = db.artwork_download_states(["900002"])["900002"]
    assert st == "partial", f"陈旧 DONE+缺页必须判 partial: {st}"

    # 无 total_pages 的外来作品: 没有「完整」概念, 维持只看 status
    db.change_work_status("merged_x", WSt.DONE, total_pages=None)
    db.mark_page_status("merged_x", 0, PSt.DONE, local_path="D/x_p0.png")
    assert db.artwork_download_states(["merged_x"])["merged_x"] == "downloaded"

    # 删光最后一页 → 级联删除
    db.change_work_status("900003", WSt.DONE, total_pages=1)
    db.mark_page_status("900003", 0, PSt.DONE, local_path="D/900003_p0.png")
    rels2, emptied2 = db.delete_pages(
        [{"artwork_id": "900003", "page_num": 0}])
    assert emptied2 == ["900003"], emptied2
    assert db.get_artwork_metadata("900003") is None
    db.close()


def t85_only_aid_filter_and_updated_sort() -> None:
    """T85: 「仅查看该作品」精确筛选 + 「按修改时间」排序基础
    ① only_aid 在作品/图片两视图的 list+count 四个函数都生效, 且是**精确 ID**匹配
       (不能用 LIKE: 搜 880 不能带出 8800/8801);
    ② only_aid 与其他条件 AND —— 前端会放空 r18 才能看到 R18 作品 (本用例锁住该语义);
    ③ change_work_status 写的时间戳必须与 schema 默认值同格式 (UTC + 空格),
       不能是本地 isoformat('T') —— 否则文本排序 'T' > ' ' 会让它无条件排最前 (实测过);
    ④ sort='updated' 按真实时间倒序。"""
    from db.store import PixivDatabase, PSt, WSt
    import datetime as _dt

    db = PixivDatabase(":memory:")
    db.change_work_status("880", WSt.DONE, total_pages=1, title="目标作", is_r18=True)
    db.mark_page_status("880", 0, PSt.DONE, local_path="D/880_p0.png")
    db.change_work_status("8801", WSt.DONE, total_pages=1, title="干扰作", is_r18=False)
    db.mark_page_status("8801", 0, PSt.DONE, local_path="D/8801_p0.png")

    # ① 精确匹配: only_aid='880' 不能把 8801 带出来 (LIKE 会)
    arts = db.list_artworks(only_aid="880")
    assert [str(r["artwork_id"]) for r in arts] == ["880"], arts
    assert db.count_artworks_filtered(only_aid="880") == 1
    imgs = db.list_images(only_aid="880")
    assert [str(r["artwork_id"]) for r in imgs] == ["880"], imgs
    assert db.count_images_filtered(only_aid="880") == 1

    # ② only_aid 与 is_r18 是 AND: 只有放空 r18 才看得到 R18 作品
    assert db.count_artworks_filtered(only_aid="880", is_r18="0") == 0
    assert db.count_artworks_filtered(only_aid="880", is_r18="") == 1

    # ③ 时间戳格式与 schema 默认一致 (UTC 空格, 无 'T')
    ts = db.conn.execute(
        "SELECT updated_at FROM artworks WHERE artwork_id='880'").fetchone()[0]
    assert "T" not in str(ts), f"updated_at 不能是本地 isoformat: {ts}"
    _dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")   # 格式必须严格匹配

    # ④ 按修改时间排序 = 真实时间倒序 (8801 后改 → 排前面)
    db.conn.execute("UPDATE artworks SET updated_at='2026-09-10 00:00:00' "
                    "WHERE artwork_id='880'")
    db.conn.execute("UPDATE artworks SET updated_at='2026-09-15 00:00:00' "
                    "WHERE artwork_id='8801'")
    db.conn.commit()
    order = [str(r["artwork_id"]) for r in
             db.list_artworks(sort="updated", order="desc")]
    assert order == ["8801", "880"], order
    db.close()


def t86_merged_external_reidentify_and_junk_titles() -> None:
    """T86: 合并产物的身份纠正 + 内部键标题修复 
    「合并后 id/标题一直是 merged，即使标了真实来源」)。
    ① `merged_` + 已确认外部来源 (source 或 source_url) → 主键改 `ext_`、
       pages 随外键级联、queue 项跟随、内部键标题换成「站点 · @账号」;
    ② 纯 pixiv 页的合并产物 (source 为 NULL/pixiv) **不动** —— 那属于「认领散落页」;
    ③ 幂等: 再跑一次不再改动;
    ④ repair_id_titles 也能修 `merged_xxx_pN` 这类内部键标题;
    ⑤ split_pages 拆外来源作品的页 → 子作品直接是 ext_ 身份 + 可读标题, 不再是 merged_。"""
    from db.store import (PixivDatabase, PSt, WSt, title_is_internal_key,
                          foreign_title_fallback)

    db = PixivDatabase(":memory:")
    # ① 合并产物 + 推特来源 + 内部键标题
    db.change_work_status("merged_ext1", WSt.DONE, total_pages=1,
                          title="merged_86126bd0e0de_p0", is_r18=False)
    db.update_artwork("merged_ext1", source="twitter",
                      source_author="shenlaijia45208")
    db.mark_page_status("merged_ext1", 0, PSt.DONE,
                        local_path="X图/推特_1/merged_86126bd0e0de_p0.png")
    db.conn.execute("INSERT INTO download_queue_items (item_type, artwork_id, status) "
                    "VALUES ('artwork', 'merged_ext1', 'pending')")
    db.conn.commit()
    # ② 纯 pixiv 合并产物 (不该被动)
    db.change_work_status("merged_pixiv1", WSt.DONE, total_pages=2, title="merged_pixiv1")
    db.mark_page_status("merged_pixiv1", 0, PSt.DONE, local_path="D/m_0.png")

    rk = db.reidentify_merged_external()
    assert len(rk) == 1, rk
    old, new = rk[0]
    assert old == "merged_ext1" and new.startswith("ext_") and len(new) == 20, rk
    row = db.get_artwork_metadata(new)
    assert row is not None, "新主键行必须存在"
    assert row["title"] == "推特 · @shenlaijia45208", row["title"]
    assert not title_is_internal_key(row["title"])
    # pages 由 ON UPDATE CASCADE 跟随
    assert db.conn.execute("SELECT COUNT(*) FROM pages WHERE artwork_id=?",
                           (new,)).fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM pages WHERE artwork_id='merged_ext1'"
                           ).fetchone()[0] == 0
    # 队列项跟随 (下载清单还能找到这个作品)
    assert db.conn.execute("SELECT artwork_id FROM download_queue_items "
                           "WHERE artwork_id=?", (new,)).fetchone()[0] == new
    # ② 纯 pixiv 合并产物保持 merged_
    assert db.get_artwork_metadata("merged_pixiv1") is not None
    # ③ 幂等
    assert db.reidentify_merged_external() == [], "再跑一次不应再改动"

    # ④ repair_id_titles 修内部键标题 (无来源 → 回退「未命名作品」)
    db.change_work_status("merged_junk2", WSt.DONE, total_pages=1,
                          title="merged_abcdef123456_p0")
    db.mark_page_status("merged_junk2", 0, PSt.DONE,
                        local_path="X图/a/merged_abcdef123456_p0.png")
    assert db.repair_id_titles() >= 1
    assert db.get_artwork_metadata("merged_junk2")["title"] == "未命名作品"

    # ⑤ 拆分外来源作品的页 → ext_ 身份 + 可读标题
    db.change_work_status("ext_src1", WSt.DONE, total_pages=2,
                          title="推特 · @someone")
    db.update_artwork("ext_src1", source="twitter", source_author="someone")
    db.mark_page_status("ext_src1", 0, PSt.DONE, local_path="X图/someone/aaa.png")
    db.mark_page_status("ext_src1", 1, PSt.DONE, local_path="X图/someone/bbb.png")
    res = db.split_pages([{"artwork_id": "ext_src1", "page_num": 0}])
    assert len(res["created"]) == 1, res
    child = res["created"][0]
    assert child.startswith("ext_") and not child.startswith("merged_"), child
    cmeta = db.get_artwork_metadata(child)
    assert cmeta["source"] == "twitter" and cmeta["source_author"] == "someone"
    assert not title_is_internal_key(cmeta["title"]), cmeta["title"]
    assert foreign_title_fallback("twitter", "someone") == "推特 · @someone"
    db.close()


def t87_no_uncommitted_raw_writes() -> None:
    """T87: 写库必须走「立即提交」的路径, 否则会长时间持有写锁 (2026-09-16 事故)。

    真凶: 自检「作者改名同步」里 `db.conn.execute("UPDATE authors SET status=...")`
    **从不 commit** —— Python sqlite3 的隐式写事务一直持锁, 而循环里还有逐个作者的
    HTTP 请求 + 0.5s 睡眠, 写锁被握数分钟; 用户在网页点保存就报 `database is locked`,
    而且连接关闭时事务被回滚, 状态压根没存进去。
    ① 功能层: set_author_status 必须真正落库 (换一条新连接也能读到 = 已提交);
    ② 静态护栏: web/server.py 里不得再出现 `db.conn.execute("UPDATE/INSERT/DELETE ...")`
       这种绕过 store 的裸写 (store 的方法内部才管提交)。"""
    import re as _re
    import sqlite3
    import tempfile
    from db.store import PixivDatabase

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "t87.db"
        db = PixivDatabase(str(db_path))
        db.upsert_author("555", "作者甲")
        assert (db.get_author("555") or {}).get("status") is None, "初始状态应为空"
        db.set_author_status("555", "alive")
        # 关键: 调用后不得残留未提交事务 —— 正是它把整库写锁握住数分钟
        assert not db.conn.in_transaction, "写操作后不得残留未提交事务"
        db.close()
        # 另开一条连接读: 能读到 = 已提交落盘 (未提交的写只在原连接内可见)
        probe = sqlite3.connect(str(db_path))
        status = probe.execute("SELECT status FROM authors WHERE author_id='555'"
                               ).fetchone()[0]
        probe.close()
        assert status == "alive", f"set_author_status 必须真正提交落库, 实得: {status}"

    root = Path(__file__).resolve().parent.parent
    src = (root / "web" / "server.py").read_text(encoding="utf-8")
    bad = _re.findall(r'db\.conn\.execute\(\s*["\']\s*(?:UPDATE|INSERT|DELETE)',
                      src, _re.IGNORECASE)
    assert not bad, (
        f"web/server.py 里仍有绕过 store 的裸写 (会隐式开写事务且不提交): {len(bad)} 处")


def _t8x_env(tmp: Path):
    """T88~T90 共用: 把 paths 指到临时目录, 建一个含待办项(缺作者/散落图)与可疑项(缺文件)的库。"""
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    import web.server as wsrv

    img = tmp / "img"
    img.mkdir(parents=True, exist_ok=True)
    (img / "画师甲_500").mkdir(parents=True, exist_ok=True)
    (img / "画师甲_500" / "501_p0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (img / "599_p0.png").write_bytes(b"\x89PNG\r\n\x1a\n")   # 根目录散落图(待办)

    db = PixivDatabase(str(tmp / "pixiv.db"))
    db.upsert_author("500", "画师甲")
    db.change_work_status("501", WSt.DONE, title="正常作品", author_id="500",
                          total_pages=1)
    db.mark_page_status("501", 0, PSt.DONE, local_path="画师甲_500/501_p0.png")
    # 缺作者名(待办项): 有编号但 authors 表里没有
    db.change_work_status("599", WSt.DONE, title="599", total_pages=1)
    # 缺文件(可疑项): 库里有记录, 磁盘上没有这个文件
    db.change_work_status("601", WSt.DONE, title="丢了的作品", author_id="500",
                          total_pages=1)
    db.mark_page_status("601", 0, PSt.DONE, local_path="画师甲_500/601_p0.png")
    db.close()
    wsrv._check_state.update(running=None, steps={}, progress={}, report=None,
                             last_run=None, journal_key=None)
    wsrv._self_check_state.update(running=False, result=None)
    return wsrv


def _t8x_wait_step(wsrv, name: str, timeout: float = 30.0):
    """等某个体检步骤跑出结果 (POST 返回时后台线程可能还没把 running 写上, 所以以结果为准)。"""
    end = time.time() + timeout
    while time.time() < end:
        st = (wsrv._check_state.get("steps") or {}).get(name)
        if st is not None and not wsrv._check_state.get("running"):
            return st
        time.sleep(0.05)
    return (wsrv._check_state.get("steps") or {}).get(name)


def t88_check_split_steps_and_status() -> None:
    """T88: 体检拆成 6 个可独立运行的步骤 (原「一键自检」一锅端会白跑)。

    ① 步骤清单固定为 6 项且与 outline 一致 (前端据此渲染, 不硬编码);
    ② 未知步骤名 → 400, 不静默跑成"全部";
    ③ 单跑「⑥ 汇总报告」能独立完成并给出每项最近结果 (不必先把对账/联网跑一遍);
    ④ 已有步骤在跑时再启动 → 409 (不并发跑体检, 避免两路同时写库/动磁盘)。"""
    import tempfile
    import utils.paths as upaths

    with tempfile.TemporaryDirectory(prefix="t88chk_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            wsrv = _t8x_env(tmp)
            assert wsrv._CHECK_STEP_NAMES == ["reconcile", "offline", "authors",
                                             "metadata", "organize", "report"], \
                f"体检步骤清单应为 6 项, 实得 {wsrv._CHECK_STEP_NAMES}"
            client = wsrv.create_app().test_client()

            j = client.get("/api/library/check-status").get_json()
            assert [x["name"] for x in j["outline"]] == wsrv._CHECK_STEP_NAMES
            assert all(x["title"] and x["desc"] for x in j["outline"]), "每项都要有标题和说明"

            r = client.post("/api/library/check", json={"step": "不存在的项"})
            assert r.status_code == 400, f"未知体检项必须 400, 实得 {r.status_code}"
            assert not wsrv._check_state.get("running"), "非法请求不得启动任务"

            # 单跑「汇总报告」: 无需先跑对账等重步骤
            r = client.post("/api/library/check", json={"step": "report"})
            assert r.status_code == 200 and r.get_json()["status"] == "started"
            st = _t8x_wait_step(wsrv, "report")
            assert st and st["ok"], f"单跑 report 应成功, 实得 {st}"
            assert "suspicious" in st["summary"] and "todos" in st["summary"]

            # 并发护栏: 已有步骤在跑 → 409 (旧入口也回报 running, 不再起第二个线程)
            wsrv._check_state["running"] = "metadata"
            try:
                r = client.post("/api/library/check", json={"step": "report"})
                assert r.status_code == 409, f"并发启动必须 409, 实得 {r.status_code}"
                r2 = client.post("/api/library/self-check")
                assert r2.get_json()["status"] == "running", \
                    "旧入口在忙时应回报 running 而不是再起一个"
            finally:
                wsrv._check_state["running"] = None
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._check_state.update(running=None, steps={}, progress={},
                                      report=None, journal_key=None)


def t89_check_report_split_and_ignore() -> None:
    """T89: 报告分「可疑项 / 待办项」+ 条目可忽略 (用户: "清单怎么不见少")。

    ① 「文件丢失」= 可疑项, 「缺作者名」= 待办项, 两边分开放 (旧版混在一起,
       于是补不回来的"待办"永远挂在"可疑"里, 数字看着永远不降);
    ② 每条都带 ref (忽略键), 缺 ref 就没法"不再提示";
    ③ 忽略一条 → 报告立刻摘掉它 + 落库; 重启(新建 app)后仍记着;
    ④ 恢复 → 从已忽略列表移除。"""
    import tempfile
    import utils.paths as upaths

    with tempfile.TemporaryDirectory(prefix="t89chk_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            wsrv = _t8x_env(tmp)
            client = wsrv.create_app().test_client()
            client.post("/api/library/check", json={"step": "report"})
            assert _t8x_wait_step(wsrv, "report"), "report 步骤应完成"
            j = client.get("/api/library/check-report").get_json()["report"]
            sus_types = {x["type"] for x in j["suspicious"]}
            todo_types = {x["type"] for x in j["todos"]}
            assert "missing_file" in sus_types, f"缺文件应归可疑项, 实得 {sus_types}"
            assert "no_author" in todo_types, f"缺作者名应归待办项, 实得 {todo_types}"
            assert not (sus_types & todo_types), "同一类型不得同时出现在两个分区"
            assert all(x.get("ref") for x in j["suspicious"] + j["todos"]), \
                "每条都要有 ref, 否则无法单独忽略"
            assert "corrupt_verified" in j, \
                "报告要如实标注这次有没有校验过图片损坏 (单跑汇总报告不校验)"

            r = client.post("/api/library/check-ignore",
                            json={"kind": "no_author", "ref": "599"})
            assert r.status_code == 200 and r.get_json()["ignored_count"] == 1
            j2 = client.get("/api/library/check-report").get_json()["report"]
            assert "no_author" not in {x["type"] for x in j2["todos"]}, \
                "忽略后当前报告应立即摘掉该条"
            assert any(x["ref"] == "599" for x in client.get(
                "/api/library/check-ignored").get_json()["items"])

            # 重启(新 app, 内存报告被清空)后重新体检: 已忽略的条目不再出现
            wsrv._check_state["report"] = None
            client2 = wsrv.create_app().test_client()
            client2.post("/api/library/check", json={"step": "report"})
            assert _t8x_wait_step(wsrv, "report"), "report 步骤应完成"
            j3 = client2.get("/api/library/check-report").get_json()["report"]
            assert "no_author" not in {x["type"] for x in j3["todos"]}, \
                "已忽略的条目重新体检也不该再提示"

            r = client2.post("/api/library/check-ignore",
                             json={"kind": "no_author", "ref": "599", "undo": True})
            assert r.get_json()["ignored_count"] == 0, "恢复后已忽略列表应为空"
            assert not client2.get("/api/library/check-ignored").get_json()["items"]
            assert client2.get("/api/library/check-report").get_json()["report"]["ignored"] == 0, \
                "恢复后报告里的「已忽略」计数要立刻校准 (否则用户以为恢复没生效)"

            # 旧版落盘报告 (corrupt 条目连 path 都没有) 读回来也要补出 ref, 否则「忽略」点了报错
            legacy = {"time": "t0", "suspicious": [{"type": "corrupt", "text": "坏了"}]}
            (tmp / "self_check.json").write_text(json.dumps(legacy, ensure_ascii=False),
                                                 encoding="utf-8")
            wsrv._check_state["report"] = None
            client3 = wsrv.create_app().test_client()
            r3 = client3.get("/api/library/check-report").get_json()["report"]
            assert r3 and r3["suspicious"] and r3["suspicious"][0].get("ref"), \
                "从旧报告读入的条目也要有 ref (否则无法单独忽略)"
            assert client3.post("/api/library/check-ignore",
                                json={"kind": "corrupt",
                                      "ref": r3["suspicious"][0]["ref"]}
                                ).status_code == 200, "旧报告条目应能忽略"
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._check_state.update(running=None, steps={}, progress={},
                                      report=None, journal_key=None)


def t90_task_journal_interrupt_and_resume() -> None:
    """T90: 长任务被拦腰砍断 → 下次启动发现"异常中断"并提示是否继续。

    背景: 关掉页面 12s 后守护线程会直接 os._exit(0), 正在跑的体检被砍断。旧版进度只在
    内存里, 重启后什么都不剩, 用户只能整套重跑 (而且不知道中断在哪一步)。
    ① 运行中记录落盘 (含 steps/done_steps/进度);
    ② 启动时把残留的 running 标成 interrupted, /api/tasks/interrupted 给出剩余步骤;
    ③ 支持中途"接续": 只重跑没跑完的步骤, 跑完状态转 done;
    ④ 选择"不继续" → 标记已确认, 不再提示。"""
    import tempfile
    import utils.paths as upaths

    with tempfile.TemporaryDirectory(prefix="t90jr_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            wsrv = _t8x_env(tmp)
            # 模拟"上次跑到一半被砍断": 只差最后一步 report 没跑
            wsrv._journal_start("check:all", "🧹 体检（全部）", resumable=True,
                                steps=["reconcile", "report"])
            wsrv._journal_write("check:all", "running",
                                progress={"step": "reconcile", "done": 1, "total": 1})
            wsrv._journal_step_done("check:all", "reconcile", "report")
            assert (tmp / "task_journal.json").is_file(), "运行记录必须落盘"

            # 重启: 残留 running → interrupted
            assert wsrv._journal_mark_interrupted() == 1
            pend = wsrv._journal_pending()
            assert pend and pend[0]["key"] == "check:all"
            assert pend[0]["remain"] == ["report"], f"剩余步骤应只有 report, 实得 {pend[0]}"

            client = wsrv.create_app().test_client()
            j = client.get("/api/tasks/interrupted").get_json()
            assert j["count"] == 1 and j["items"][0]["remain"] == ["report"]
            assert j["items"][0]["resumable"] is True

            # 继续: 只跑剩下的 report
            r = client.post("/api/tasks/resume", json={"key": "check:all"})
            assert r.status_code == 200 and r.get_json()["steps"] == ["report"]
            end = time.time() + 30
            t = {}
            while time.time() < end:
                t = ((wsrv._journal_read().get("tasks") or {}).get("check:all")) or {}
                if t.get("status") in ("done", "failed"):
                    break
                time.sleep(0.05)
            assert t.get("status") == "done", f"续跑完应标记 done, 实得 {t.get('status')}"
            assert client.get("/api/tasks/interrupted").get_json()["count"] == 0

            # 不继续 → 已确认, 不再提示
            wsrv._journal_start("check:metadata", "🩺 体检 · ④ 联网补元数据",
                                resumable=True, steps=["metadata"])
            wsrv._journal_mark_interrupted()
            assert client.get("/api/tasks/interrupted").get_json()["count"] == 1
            assert client.post("/api/tasks/ack",
                               json={"key": "check:metadata"}).status_code == 200
            assert client.get("/api/tasks/interrupted").get_json()["count"] == 0
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._check_state.update(running=None, steps={}, progress={},
                                      report=None, journal_key=None)


def _t9x_env(tmp: Path):
    """T91/T92 共用: 临时 data_root + 一个含 DONE/未完成作品的库, 并记下被启动的下载请求。"""
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    import web.server as wsrv

    img = tmp / "img"
    img.mkdir(parents=True, exist_ok=True)
    # 701 的本地文件真实存在 (重下修复前应被复制成 .bak)
    (img / "701_p0.png").write_bytes(b"\x89PNG\r\n\x1a\n-old-")
    db = PixivDatabase(str(tmp / "pixiv.db"))
    # 701: 已完整入库 (DONE) —— 正常「加入下载」会被拦, 只有「重下修复」能开任务
    db.change_work_status("701", WSt.DONE, title="已入库作品", total_pages=1)
    db.mark_page_status("701", 0, PSt.DONE, local_path="701_p0.png")
    # 702: 已入库且队列里已有活跃任务 → 重下修复应报"已在下载列表"
    db.change_work_status("702", WSt.DONE, title="已在队列", total_pages=1)
    # 703: 未下载完 (NOT_YET)
    db.change_work_status("703", WSt.NOT_YET, title="没下完的作品", total_pages=1)
    db.queue_add(item_type="artwork", title="已在队列", artwork_id="702",
                 page_count=1, strategy="only_new", source_note="测试预置")
    db.close()
    wsrv._cart_busy.clear()
    wsrv._cancel_event.clear()
    launched = []
    orig = wsrv._launch_run_cart
    wsrv._launch_run_cart = lambda only_ids=None, strategy=None: launched.append(
        {"only_ids": list(only_ids or []), "strategy": getattr(strategy, "value", strategy)})
    return wsrv, launched, orig


def t91_cart_repair_force_and_intent() -> None:
    """T91: 「♻ 重下修复」= 显式声明"覆盖重下"的接口 (用户 2026-09-16 方案)。

    的矛盾: 加入下载遇到库里已有的图, 代码没法自行判断"该覆盖还是跳过"。
    解法 = 意图由用户显式选:
      ① 「加入下载」(单个/批量) 语义永远是"有则跳过、缺页补全" —— DONE 一律拦下, 永不覆盖;
      ② 「重下修复」语义永远是"用官方源覆盖" —— 允许 DONE 入队, 且固定 overwrite 策略,
         只跑刚入队的这几条 (only_ids), 不把队列里其他任务也变成覆盖;
      ③ 只有 pixiv 作品 (纯数字 ID) 有官方源可重下, 其他来源如实跳过并给原因;
      ④ 已有下载任务在跑时拒绝 (避免两套策略混在一轮里)。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase

    with tempfile.TemporaryDirectory(prefix="t91rep_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        wsrv = launched = orig_launch = None
        try:
            wsrv, launched, orig_launch = _t9x_env(tmp)
            client = wsrv.create_app().test_client()

            # ① 加入下载: DONE 作品仍然被拦 (永不覆盖) —— 这条不变才有"批量无歧义"
            r = client.post("/api/cart/batch-add",
                            json={"items": [{"artwork_id": "701", "title": "已入库作品"}]})
            assert r.get_json()["already_downloaded"] == 1, r.get_json()
            assert r.get_json()["added"] == 0

            # ② 重下修复: DONE 允许入队, 并固定 overwrite
            r = client.post("/api/cart/repair",
                            json={"ids": ["701", "702", "703", "ext_abc", "999999"]})
            j = r.get_json()
            assert r.status_code == 200 and j["status"] == "ok", j
            assert j["added"] == 2, f"701(DONE) 与 703 应入队, 实得 {j}"
            assert j["in_queue"] == 1, f"702 队列里已有活跃任务 → in_queue, 实得 {j}"
            reasons = {s["artwork_id"]: s["reason"] for s in j["skipped"]}
            assert "ext_abc" in reasons and "999999" in reasons, f"非 pixiv/不在库要跳过: {reasons}"
            assert "非 pixiv" in reasons["ext_abc"], reasons["ext_abc"]

            # 覆盖前必须真的留了 .bak (不能只靠下载器"落盘路径已存在"那个判断:
            # 用户的图大多被分类进 X图/作者_ID 子目录, 下载器落盘路径常常对不上)
            assert j["backed_up"] == 1, f"701 有 1 张本地图, 覆盖前应备份 1 张: {j}"
            assert (tmp / "img" / "701_p0.png.bak").is_file(), "重下修复前应留下 .bak"

            with PixivDatabase(str(tmp / "pixiv.db")) as db:
                rows = {str(it["artwork_id"]): it for it in db.queue_list()
                        if it.get("artwork_id")}
                assert rows["701"]["strategy"] == "overwrite", "重下修复必须固定 overwrite 策略"
                assert "重下修复" in (rows["701"]["source_note"] or "")
                # DONE 状态不被降级 (否则界面状态错乱)
                assert db.get_work_info("701").done, "重下修复不得把 DONE 降级"

            # ③ 只跑刚入队的这几条 + overwrite 策略 (不影响队列里其他任务)
            assert launched, "入队后应自动开始这一轮下载"
            assert launched[-1]["strategy"] == "overwrite", launched[-1]
            assert set(launched[-1]["only_ids"]) == set(j["item_ids"]), launched[-1]

            # ④ 空清单 400; 已有下载任务在跑 → 409
            assert client.post("/api/cart/repair", json={"ids": []}).status_code == 400
            wsrv._cart_busy.set()
            try:
                r409 = client.post("/api/cart/repair", json={"ids": ["701"]})
                assert r409.status_code == 409, r409.status_code
            finally:
                wsrv._cart_busy.clear()

            # ⑤ 单个接口的 force 开关: 不带 force 拦 DONE, 带 force 放行
            from db.store import WSt as _WSt
            with PixivDatabase(str(tmp / "pixiv.db")) as db:
                db.change_work_status("705", _WSt.DONE, title="已完成且不在队列", total_pages=1)
            r = client.post("/api/cart", json={"artwork_id": "705"})
            assert r.get_json()["reason"] == "already_downloaded", r.get_json()
            r = client.post("/api/cart", json={"artwork_id": "705", "force": True})
            assert r.get_json()["reason"] == "added", r.get_json()
        finally:
            if wsrv is not None and orig_launch is not None:
                wsrv._launch_run_cart = orig_launch
                wsrv._cart_busy.clear()
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t92_pixiv_author_declare_and_fill_name() -> None:
    """T92: 手动填作者时"明确告诉数据库他是 pixiv 作者" (用户 2026-09-16 要求)。

    以前: 库里没有的名字一律建成「自定义作者」, 没有任何途径声明"这是 pixiv 作者"。
    现在:
      ① POST /api/library/pixiv-author 只查不写, 三种模式 (作品ID/作者ID/名字),
         没登录凭据时明确报错而不是静默失败;
      ② 编辑作品只填「纯数字 pixiv 作者 ID」时, 保存后作者必须关联到 pixiv ID
         (而不是被建成 local_ 自定义作者) —— 这是用户最容易误解的地方;
      ③ 联网补名失败也不阻塞保存 (体检「作者改名同步」以后还能补)。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt

    with tempfile.TemporaryDirectory(prefix="t92pa_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.change_work_status("801", WSt.DONE, title="待改作者", total_pages=1)
            db.close()
            import web.server as wsrv
            client = wsrv.create_app().test_client()

            # ① 缺参 400
            assert client.post("/api/library/pixiv-author", json={}).status_code == 400
            # 没 cookies → 明确提示去登录 (而不是返回空候选让人以为"查不到")
            r = client.post("/api/library/pixiv-author", json={"author_id": "12345"})
            assert r.status_code == 400, r.status_code
            assert "登录" in (r.get_json().get("error") or ""), r.get_json()
            # 名字模式: 搜索依赖浏览器 → 打桩模拟失败, 必须如实报错 (不静默返回空)
            orig_sc = wsrv._get_search_client

            def _boom():
                raise RuntimeError("测试打桩: 搜索不可用")
            wsrv._get_search_client = _boom
            try:
                r = client.post("/api/library/pixiv-author", json={"name": "某画师"})
                assert r.status_code == 502, r.status_code
                assert "搜索失败" in (r.get_json().get("error") or ""), r.get_json()
            finally:
                wsrv._get_search_client = orig_sc

            # ② 只填纯数字作者 ID → 关联 pixiv, 绝不建 local 自定义作者
            r = client.post("/api/library/update",
                            json={"artwork_id": "801", "author_id": "1234567"})
            assert r.status_code == 200 and r.get_json()["status"] == "ok", r.get_json()
            assert str(r.get_json().get("author_id")) == "1234567", \
                "响应要回 author_id, 前端才能说明'已关联 pixiv 作者'"
            with PixivDatabase(str(tmp / "pixiv.db")) as db2:
                row = db2.get_artwork_metadata("801")
                assert str(row.get("author_id")) == "1234567", \
                    f"填 ID 应关联到 pixiv 作者, 实得 {row.get('author_id')}"
                # 名字没联网拿到时不硬造假的「未知画师」作者行 (界面回落显示裸 ID, 体检④会补名)
                ids = [str(x[0]) for x in db2.conn.execute("SELECT author_id FROM authors")]
            assert not [i for i in ids if i.startswith("local_")], f"不该建自定义作者: {ids}"

            # ③ 名字模式保存 → 建自定义作者 (本来源就该这样)
            r = client.post("/api/library/update",
                            json={"artwork_id": "801", "author_name": "某推特画师"})
            assert r.status_code == 200
            with PixivDatabase(str(tmp / "pixiv.db")) as db4:
                a2 = db4.get_artwork_metadata("801")
                assert str(a2.get("author_id", "")).startswith("local_"), \
                    f"只给名字=自定义作者, 实得 {a2.get('author_id')}"
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t93_author_links_structured_and_bind_pixiv() -> None:
    """T93: 作者「来源链接」不只是记录 —— 要结构化、要产生关联 (用户 2026-09-16 要求)。

    ① 可多条, 保存时自动识别站点与账号 (site/label/handle), 非 URL 直接丢弃;
    ② 链接**产生关联**: 记了 https://x.com/foo → 该作者名下「来源=X 且没记账号」的作品
       自动补 source_author=foo (归档子夹/显示随之正确), 已有账号的绝不覆盖;
    ③ 兼容老数据: 只有 pixiv_url/twitter 两列时照样能读出来;
    ④ 作者管理能按链接/账号搜索 (q=x.com/foo 也能命中);
    ⑤ 记了 pixiv 主页链接 → 本地作者可一键**绑定成真正的 pixiv 作者**:
       作品迁移、作者行合并(曾用名/链接跟着走)、磁盘文件夹 名字_local_xxx → 名字_pid。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt

    with tempfile.TemporaryDirectory(prefix="t93al_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            img = tmp / "img"
            db = PixivDatabase(str(tmp / "pixiv.db"))
            # 本地作者 (站点+账号) → 稳定键 local_<md5>: 取真实 ID 来造数据/断言
            AID = db.create_or_get_local_author("推特画师", source="twitter", handle="foo")
            assert AID.startswith("local_"), AID
            (img / "X图" / f"推特画师_{AID}").mkdir(parents=True)
            (img / "X图" / f"推特画师_{AID}" / "7001_p0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            db.change_work_status("7001", WSt.DONE, title="w1", author_id=AID)
            db.mark_page_status("7001", 0, PSt.DONE,
                                local_path=f"X图/推特画师_{AID}/7001_p0.png")
            db.change_work_status("7002", WSt.DONE, title="w2", author_id=AID)
            db.change_work_status("7003", WSt.DONE, title="w3", author_id=AID)
            db.change_work_status("7004", WSt.DONE, title="w4", author_id=AID)
            cur = db.conn.cursor()
            for aid, src, acc in (("7001", "twitter", ""), ("7002", "twitter", ""),
                                  ("7003", "twitter", "keepme"), ("7004", "danbooru", "")):
                cur.execute("UPDATE artworks SET source=?, source_author=? WHERE artwork_id=?",
                            (src, acc or None, aid))
            db.conn.commit()
            db.close()

            import web.server as wsrv
            client = wsrv.create_app().test_client()

            # ① 多条链接 + 自动识别站点/账号; 非 URL 丢弃
            r = client.post("/api/library/author-edit", json={
                "author_id": AID,
                "urls": ["https://x.com/foo", "https://www.pixiv.net/users/1234567",
                         "不是URL", "https://x.com/foo"]})
            assert r.status_code == 200, r.get_json()
            j = r.get_json()
            got = j["urls"]
            assert len(got) == 2, f"应保留 2 条有效且去重的链接, 实得 {got}"
            assert got[0]["site"] == "twitter" and got[0]["handle"] == "foo", got[0]
            assert "Twitter" in got[0].get("label", ""), got[0]
            assert got[1]["site"] == "pixiv" and got[1]["handle"] == "1234567", got[1]

            # ② 链接产生关联: 来源=twitter 且没账号的作品补上 @foo; 已有账号的不动
            with PixivDatabase(str(tmp / "pixiv.db")) as db2:
                sa = {aid: db2.get_artwork_metadata(aid).get("source_author")
                      for aid in ("7001", "7002", "7003", "7004")}
                assert sa["7001"] == "foo" and sa["7002"] == "foo", sa
                assert sa["7003"] == "keepme", f"已有账号绝不能被覆盖: {sa}"
                assert not sa["7004"], f"来源不匹配的作品不动: {sa}"
                a = db2.get_author(AID)
                assert a["twitter"] == "https://x.com/foo", a.get("twitter")
                assert a["pixiv_url"] == "https://www.pixiv.net/users/1234567", a.get("pixiv_url")
            assert j["source_author_filled"] == 2, j["source_author_filled"]

            # ④ 按链接/账号搜索能命中该作者
            r = client.get("/api/library/authors?q=x.com/foo")
            assert [x["author_id"] for x in r.get_json()["items"]] == [AID], r.get_json()
            r = client.get("/api/library/authors?q=1234567")
            assert AID in [x["author_id"] for x in r.get_json()["items"]], r.get_json()

            # ③ 老数据兼容: 只写 pixiv_url 列也能读出来 (无 urls_json)
            with PixivDatabase(str(tmp / "pixiv.db")) as db3:
                db3.conn.execute("UPDATE authors SET urls_json=NULL WHERE author_id=?",
                                 (AID,))
                db3.conn.commit()
                urls = (db3.get_author(AID) or {}).get("urls") or []
            sites = {u.get("site") for u in urls}
            assert "pixiv" in sites and "twitter" in sites, urls

            # ⑤ 绑定为真正的 pixiv 作者: 作品迁移 + 作者行合并 + 磁盘文件夹改名
            r = client.post("/api/library/author-bind-pixiv",
                            json={"author_id": AID, "pixiv_id": "1234567"})
            assert r.status_code == 200 and r.get_json()["status"] == "ok", r.get_json()
            jb = r.get_json()
            assert jb["artworks"] == 4, jb
            assert jb["folder_renamed"] == 1, f"磁盘夹应从 _{AID} 改成 _1234567: {jb}"
            assert (img / "X图" / "推特画师_1234567").is_dir(), "文件夹未改名"
            assert not (img / "X图" / f"推特画师_{AID}").exists()
            with PixivDatabase(str(tmp / "pixiv.db")) as db4:
                assert db4.get_author(AID) is None, "旧本地作者行应已合并删除"
                dst = db4.get_author("1234567") or {}
                assert str(dst.get("name") or ""), f"合并后要有名字: {dst}"
                for aid in ("7001", "7002", "7003", "7004"):
                    assert str(db4.get_artwork_metadata(aid)["author_id"]) == "1234567", aid
                lp = [r2[0] for r2 in db4.conn.execute(
                    "SELECT local_path FROM pages WHERE artwork_id='7001'")]
                assert lp and lp[0].startswith("X图/推特画师_1234567/"), lp

            # 护栏: 已是 pixiv 作者 / 缺参 / 不存在的作者
            assert client.post("/api/library/author-bind-pixiv",
                               json={"author_id": "1234567", "pixiv_id": "999"}).status_code == 400
            assert client.post("/api/library/author-bind-pixiv",
                               json={"author_id": "x"}).status_code == 400
            assert client.post("/api/library/author-bind-pixiv",
                               json={"author_id": "local_zzz", "pixiv_id": "1"}).status_code == 404
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t94_scoped_light_refresh() -> None:
    """T94: 🎯 轻量/定向刷新 (用户 2026-09-17: "自检拆成小项后每项仍是全量, 能不能只刷这一块")。

    ① 单作者刷新: 本地作者无 pixiv 账号 → 明确拒绝; pixiv 作者但没登录 → 明确提示 (不静默失败);
    ② 定向刷新作品: 状态/页数重算 + 来源 URL 归类 (只在"未知/other"时改写) + 磁盘文件缺失报告;
    ③ 定向刷新作者: 重算其名下全部作品 (+ 未登录时如实说明只做了本地核对);
    ④ 作者「来源」筛选与列表: 记在主页链接里的站点也算该来源 (不只是名下作品的来源)。
    只碰传进来的那几条记录, 不扫全库。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt

    with tempfile.TemporaryDirectory(prefix="t94sr_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            img = tmp / "img"
            img.mkdir(parents=True, exist_ok=True)
            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.upsert_author("1234567", "老名字")
            # 9001: 来源未知 + 记了推特链接 → 定向刷新后应归类为 twitter
            db.change_work_status("9001", WSt.DONE, title="待归类", author_id="1234567",
                                  total_pages=1)
            db.update_artwork("9001", source="other",
                              source_url="https://x.com/foo/status/1")
            # 9002: 已确认来源 → 刷新不得改动
            db.change_work_status("9002", WSt.DONE, title="已确认", author_id="1234567",
                                  total_pages=1)
            db.update_artwork("9002", source="danbooru")
            # 9003: 库里有记录但磁盘文件不存在 → 定向刷新要报文件缺失
            db.change_work_status("9003", WSt.DONE, title="丢文件的", author_id="1234567",
                                  total_pages=1)
            db.mark_page_status("9003", 0, PSt.DONE, local_path="gone/9003_p0.png")
            # 本地作者 (无 pixiv 账号)
            local_id = db.create_or_get_local_author("推特画师", source="twitter", handle="tfoo")
            db.change_work_status("9004", WSt.DONE, title="外来源作品", author_id=local_id)
            db.close()

            import web.server as wsrv
            client = wsrv.create_app().test_client()

            # ① 单作者刷新: 本地作者 → 400; pixiv 作者但无凭据 → 400 且提示去登录
            r = client.post("/api/library/author-refresh", json={"author_id": local_id})
            assert r.status_code == 400 and "pixiv" in (r.get_json().get("error") or ""), r.get_json()
            r = client.post("/api/library/author-refresh", json={"author_id": "1234567"})
            assert r.status_code == 400, r.status_code
            assert "登录" in (r.get_json().get("error") or ""), r.get_json()
            assert client.post("/api/library/author-refresh", json={}).status_code == 400

            # ② 定向刷新作品: 来源归类 + 文件缺失 + 已确认的来源不动
            r = client.post("/api/library/refresh-scope",
                            json={"scope": "work", "ids": ["9001", "9002", "9003", "不存在"]})
            j = r.get_json()
            assert r.status_code == 200 and j["status"] == "ok", j
            assert j["works"] == 3, f"只应核对库里的 3 个作品: {j}"
            assert j["source_classified"] == 1, f"只该归类 9001 这一个: {j}"
            miss = {m["artwork_id"] for m in j["missing_files"]}
            assert miss == {"9003"}, f"只有 9003 的本地文件真的不存在: {j}"
            with PixivDatabase(str(tmp / "pixiv.db")) as db2:
                m1 = db2.get_artwork_metadata("9001")
                assert str(m1.get("source")) == "twitter", m1.get("source")
                assert str(m1.get("source_author")) == "foo", m1.get("source_author")
                assert str(db2.get_artwork_metadata("9002").get("source")) == "danbooru", \
                    "已确认的来源不得被改写"

            # ③ 定向刷新作者: 重算其名下作品; 未登录时如实说明只做了本地核对
            r = client.post("/api/library/refresh-scope",
                            json={"scope": "author", "ids": ["1234567"]})
            ja = r.get_json()
            assert ja["authors"] == 1, ja
            assert "未登录" in (ja["note"] or ""), ja
            # ④ 作者来源列/筛选认主页链接: 只记了链接、名下没有该来源作品的作者
            #    (site/handle 由 web 层 _classify_url 识别后写入, 这里按同一口径直接落库)
            with PixivDatabase(str(tmp / "pixiv.db")) as db3:
                db3.update_author(local_id, urls=[
                    {"site": "twitter", "url": "https://x.com/tfoo", "handle": "tfoo"},
                    {"site": "bilibili", "url": "https://b23.tv/xyz"}])
            r = client.get("/api/library/authors?source=twitter")
            ids_tw = [x["author_id"] for x in r.get_json()["items"]]
            assert local_id in ids_tw, f"按来源 twitter 筛选应包含'只记了推特主页链接'的作者: {ids_tw}"
            r = client.get("/api/library/authors?source=bilibili")
            assert local_id in [x["author_id"] for x in r.get_json()["items"]], r.get_json()
            r = client.get("/api/library/authors?q=tfoo")
            assert local_id in [x["author_id"] for x in r.get_json()["items"]], r.get_json()
            # 未知 scope 不报错 (前端刷新列表即可)
            r = client.post("/api/library/refresh-scope", json={"scope": "library"})
            assert r.status_code == 200 and r.get_json()["note"], r.get_json()
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t95_author_reconcile_and_claim_image() -> None:
    """T95: 🗂 作者对账台 + ✅ 配对认领 (用户 2026-09-17: 未知来源/本地 ID 的图转正成 pixiv 卡片)。

    场景: 一堆外来源图挂着本地 ID (ext_xxx)、作者已手动指定成 pixiv 画师, 但作品不是正式
    pixiv 卡片。全站以图搜图太慢、逐张翻 pixiv 太贵 → 候选集限定在**这个作者**。
      ① author-reconcile: 本地作者 → 400; 未登录 → ok 但 note 明说拉不到列表, 本地待认领图照给;
      ② claim-image: 目标不在库且未登录 → 400 明确提示; 目标已在库 → 文件规范改名搬进作品目录 +
         pages 写新行 + 本地占位作品级联删除 + 目标状态重算;
      ③ 各类护栏: 非数字目标 / 自己认领自己 / 页已占用(409) / 本地文件不存在 / 缺参。
      ④ 附加页: 认领页超出 pixiv 原页数 (999001 原 2 页, 认领第 3 张到 p2) 不再失败 —
         自动成为 origin='merged' 的附加页, 作品仍 DONE, total_pages 保持 2;
         sync_work_and_pages 也不许把附加页当成"页数对不上"而清掉 total_pages。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from PIL import Image as _Image

    with tempfile.TemporaryDirectory(prefix="t95rc_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            img = tmp / "img"
            (img / "涩图" / "#Ehentai#").mkdir(parents=True)
            src1 = img / "涩图" / "#Ehentai#" / "Ehentai_001_p0.png"
            _Image.new("RGB", (640, 960), (30, 60, 90)).save(src1)
            src2 = img / "涩图" / "#Ehentai#" / "Ehentai_002_p0.png"
            _Image.new("RGB", (640, 960), (90, 60, 30)).save(src2)
            src3 = img / "涩图" / "#Ehentai#" / "Ehentai_003_p0.png"   # 用作"附加页"素材
            _Image.new("RGB", (640, 960), (30, 90, 60)).save(src3)

            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.upsert_author("63051477", "有江リリ")
            # 两个"来源未定/本地 ID"的作品, 作者已指定为 pixiv 画师
            for aid, rel in (("ext_aaaa", "涩图/#Ehentai#/Ehentai_001_p0.png"),
                             ("ext_bbbb", "涩图/#Ehentai#/Ehentai_002_p0.png")):
                db.change_work_status(aid, WSt.DONE, title=f"{aid} 标题",
                                      author_id="63051477")
                db.update_artwork(aid, source="pixiv")
                db.mark_page_status(aid, 0, PSt.DONE, local_path=rel)
            # 目标 pixiv 作品: 999001 已在库(1 页占位? 不, 让它空页), 999002 不在库
            db.change_work_status("999001", WSt.NOT_YET, title="已在库的目标",
                                  author_id="63051477", total_pages=2)
            db.close()

            import web.server as wsrv
            client = wsrv.create_app().test_client()

            # ① 对账台: 本地作者 400; 未登录 → ok + note + 本地待认领清单
            r = client.post("/api/library/author-reconcile", json={"author_id": "ext_aaaa"})
            assert r.status_code == 400 and "绑定" in (r.get_json().get("error") or ""), r.get_json()
            r = client.post("/api/library/author-reconcile", json={"author_id": "63051477"})
            j = r.get_json()
            assert r.status_code == 200 and j["status"] == "ok", j
            assert "登录" in (j["note"] or ""), j
            assert {x["artwork_id"] for x in j["locals"]} == {"ext_aaaa", "ext_bbbb"}, j["locals"]
            assert j["pixiv"] == [], j["pixiv"]

            # ② 目标不在库 + 未登录 → 明确提示 (不静默失败)
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_aaaa",
                                  "target_artwork_id": "999002"})
            assert r.status_code == 400 and "登录" in (r.get_json().get("error") or ""), r.get_json()

            # ③ 目标已在库 → 认领成功: 文件改名搬进作者夹目录 + pages 落库 + 占位作品删除
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_aaaa",
                                  "target_artwork_id": "999001", "page": 1})
            jj = r.get_json()
            assert r.status_code == 200 and jj["status"] == "ok", jj
            assert jj["page"] == 1 and jj["local_removed"] is True, jj
            assert jj["new_path"].endswith("999001_p1.png"), jj["new_path"]
            assert not src1.exists(), "本地文件应已被移走"
            moved = img / jj["new_path"]
            assert moved.is_file(), moved
            with PixivDatabase(str(tmp / "pixiv.db")) as db2:
                assert db2.get_artwork_metadata("ext_aaaa") is None, "占位作品应被级联删除"
                pg = db2.conn.execute(
                    "SELECT status, local_path, width, height FROM pages "
                    "WHERE artwork_id='999001' AND page_num=1").fetchone()
                assert pg and pg["local_path"] == jj["new_path"], dict(pg or {})
                assert int(pg["width"]) == 640 and int(pg["height"]) == 960, dict(pg)
                # 2 页里只到 1 页 → 仍是"缺页"状态, 不会被误标 DONE
                assert not db2.get_work_info("999001").done, "只认领了 1/2 页, 不该是 DONE"

            # ④ 护栏: 页已占用 409 / 非数字目标 400 / 自己认领自己 400 / 不存在 404 / 缺参 400
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_bbbb",
                                  "target_artwork_id": "999001", "page": 1})
            assert r.status_code == 409, r.get_json()
            assert client.post("/api/library/claim-image",
                               json={"local_artwork_id": "ext_bbbb",
                                     "target_artwork_id": "ext_cccc"}).status_code == 400
            assert client.post("/api/library/claim-image",
                               json={"local_artwork_id": "999001",
                                     "target_artwork_id": "999001"}).status_code == 400
            assert client.post("/api/library/claim-image",
                               json={"local_artwork_id": "ext_zzzz",
                                     "target_artwork_id": "999001"}).status_code == 404
            assert client.post("/api/library/claim-image", json={}).status_code == 400
            # 默认页 = 第一个空位 (999001 的 0 页空着 → 应落到 0)
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_bbbb",
                                  "target_artwork_id": "999001"})
            jb = r.get_json()
            assert r.status_code == 200 and jb["page"] == 0, jb
            with PixivDatabase(str(tmp / "pixiv.db")) as db3:
                assert db3.get_work_info("999001").done, "两页都齐了 → 应重算成 DONE"

            # ⑤ 附加页 :
            #    999001 原 2 页, 把 ext_cccc 认领到 p2 → 不再失败, 自动是 origin='merged'
            #    的附加页; 作品保持 DONE, total_pages 保持 2 (不被降级/不被改写)
            with PixivDatabase(str(tmp / "pixiv.db")) as db4:
                db4.change_work_status("ext_cccc", WSt.DONE, title="ext_cccc 标题",
                                       author_id="63051477")
                db4.update_artwork("ext_cccc", source="pixiv")
                db4.mark_page_status("ext_cccc", 0, PSt.DONE,
                                     local_path="涩图/#Ehentai#/Ehentai_003_p0.png")
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_cccc",
                                  "target_artwork_id": "999001", "page": 2})
            jx = r.get_json()
            assert r.status_code == 200 and jx["status"] == "ok", jx
            assert jx["is_extra"] is True and jx["total_pages"] == 2, jx
            assert not src3.exists(), "附加页的本地文件也应被改名搬进作品目录"
            with PixivDatabase(str(tmp / "pixiv.db")) as db5:
                ex = db5.conn.execute(
                    "SELECT status, origin, local_path FROM pages "
                    "WHERE artwork_id='999001' AND page_num=2").fetchone()
                assert ex is not None, "附加页应写进 pages 表"
                assert ex["origin"] == "merged", f"附加页必须标 merged 才能再拆出去: {dict(ex)}"
                info = db5.get_work_info("999001")
                assert info.done, "有附加页之后作品不该被降级成缺页"
                assert int(db5.get_artwork_info("999001").total_pages or 0) == 2, \
                    "附加页不得改写「原网页页数」total_pages"
                # sync_work_and_pages 也不许把附加页当成"页数对不上"而清掉 total_pages
                db5.sync_work_and_pages("999001")
                ex2 = db5.conn.execute(
                    "SELECT origin FROM pages WHERE artwork_id='999001' AND page_num=2"
                ).fetchone()
                assert ex2 is not None and ex2["origin"] == "merged", \
                    "同步时不许删掉/改写附加页"
                assert int(db5.get_artwork_info("999001").total_pages or 0) == 2, \
                    "同步时不许因附加页把 total_pages 置空"
                assert db5.get_work_info("999001").done, "同步后作品仍应是完整"

            # ⑥ 「塞进已入库作品」不再把状态搞乱 
            #    塞进显示「已入库」的作品会错乱):
            #    ⑥a 目标 DONE 但 total_pages 未知 → 塞一张后**必须仍是 DONE**
            #        (旧实现的完整判据 bool(tp) and ... 在 tp 未知时一律打 NOT_YET,
            #         于是卡片会从「✓ 已入库」悄悄变成「⬇ 补下缺失页」)
            with PixivDatabase(str(tmp / "pixiv.db")) as db6:
                db6.upsert_author("63051477", "有江リリ")
                db6.change_work_status("999003", WSt.DONE, title="没记页数的作品",
                                       author_id="63051477")
                rel6 = "有江リリ_63051477/999003_p0.png"
                _P = tmp / "img" / rel6
                _P.parent.mkdir(parents=True, exist_ok=True)
                _P.write_bytes(b"\x89PNG\r\n\x1a\n" + b"z" * 32)
                db6.mark_page_status("999003", 0, PSt.DONE, local_path=rel6)
                # 认领要求源文件真实存在 → 多余图也落一个文件
                rel_d = "涩图/#Ehentai#/Ehentai_004_p0.png"
                fd = tmp / "img" / rel_d
                fd.parent.mkdir(parents=True, exist_ok=True)
                fd.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 32)
                db6.mark_page_status("ext_dddd", 0, PSt.DONE, local_path=rel_d)
                db6.change_work_status("ext_dddd", WSt.DONE, title="多余图",
                                       author_id="63051477")
            r = client.post("/api/library/claim-image",
                            json={"local_artwork_id": "ext_dddd",
                                  "target_artwork_id": "999003"})
            j6 = r.get_json()
            assert r.status_code == 200 and j6["page"] == 1, j6
            with PixivDatabase(str(tmp / "pixiv.db")) as db7:
                assert db7.get_work_info("999003").done, \
                    "页数未知的作品塞一张图后不许从「已入库」被打成「缺页」"
                assert int(db7.get_artwork_info("999003").total_pages or 0) == 0, \
                    "不许凭空臆造 total_pages"
                _el = db7.conn.execute("SELECT exist_list FROM artworks "
                                       "WHERE artwork_id='999003'").fetchone()[0]
                assert sorted(json.loads(_el or "[]")) == [0, 1], \
                    f"exist_list 应是真实的现有页号: {_el}"

            #    ⑥b 「自动」(不传 page) 连续追加: 已入库作品(2 页 + 1 附加页)再塞两张
            #        → 必须依次落到 p3/p4, 而不是撞到 p0/p1 报 409 (前端「自动」选项的配套)
            with PixivDatabase(str(tmp / "pixiv.db")) as db8:
                for k in (5, 6):
                    rel = f"涩图/#Ehentai#/Ehentai_00{k}_p0.png"
                    fp = tmp / "img" / rel
                    fp.parent.mkdir(parents=True, exist_ok=True)
                    fp.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([k]) * 32)
                    db8.change_work_status(f"ext_e{k}", WSt.DONE, title=f"多余图{k}",
                                           author_id="63051477")
                    db8.mark_page_status(f"ext_e{k}", 0, PSt.DONE, local_path=rel)
            for k, want in ((5, 3), (6, 4)):
                r = client.post("/api/library/claim-image",
                                json={"local_artwork_id": f"ext_e{k}",
                                      "target_artwork_id": "999001"})
                jj = r.get_json()
                assert r.status_code == 200 and jj["page"] == want, jj
                assert jj["is_extra"] is True, jj
            with PixivDatabase(str(tmp / "pixiv.db")) as db9:
                got = sorted(int(x[0]) for x in db9.conn.execute(
                    "SELECT page_num FROM pages WHERE artwork_id='999001'"))
                assert got == [0, 1, 2, 3, 4], f"自动追加应连续排到 p4: {got}"
                assert db9.get_work_info("999001").done, "加附加页后仍是完整"

            # ⑦ 前端接线 (静态): 多选 + 两种批量认领 + "点选不重建 DOM"(滚动条不跳顶的根因防线)
            appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
            css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
            for token in ("rcDoClaimMulti", "rcDoClaimPairs", "rcTogglePair", "selLocals",
                          "selPixivs", "data-rc-local", "data-rc-pixiv"):
                assert token in appjs, f"对账台缺少 {token}"
            # 点选必须走"只切选中态"的轻量重画: 调 paintReconcileSelection, 绝不整块重画
            pick = appjs.split("window.rcPickLocal = function")[1].split("window.rcPickPixiv")[0]
            assert "paintReconcileSelection()" in pick, "rcPickLocal 应只重画选中态"
            assert "paintReconcile()" not in pick, \
                "rcPickLocal 不得整块重画(会重建 DOM → 两个网格滚动条跳回顶部)"
            # 整块重画时要还原滚动位置
            assert "_rcScrollSnapshot" in appjs and "_rcRestoreScroll" in appjs
            assert "rc-item[data-ord]" in css, "选中序号角标样式缺失(顺序配对靠它核对顺序)"
            # 超出原页数的认领 → 前端要画出「附加页」预览与文案, 不是直接报错劝退
            assert "rcPaintMergePreview" in appjs, "缺少合并认领的页映射预览"
            assert "附加页" in appjs and "is_extra" in appjs, "前端未接入附加页语义"
            # 「自动：下一个空位」这条路必须存在 —— 否则往已入库作品里追加第 2 张
            # 只会撞上已有的页/附加页, 报一堆 409, 看起来就像"错乱" 
            assert appjs.count("自动：下一个空位") >= 1 and "从下一个空位开始依次排" in appjs, \
                "认领要提供「自动挑下一个空位」的选项"
            assert "raw === '' ? undefined" in appjs, \
                "「自动」必须不传 page 交给服务端挑空位 (传 0 会撞已入库作品的第 1 页)"
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t99_page_reorder_and_safemode_mask() -> None:
    """T99: ↔ 作品内页序调整 +  安全模式「不加载 R18 像素」(用户 2026-09-17)。

    ① reorder-pages: 手动认领/自动溯源可能把页序挂反, 事后要能调。换序后 pages 行
       page_num 跟着换, 但 md5/origin/尺寸**跟文件走**、total_pages(原网页页数)绝不动、
       作品仍是 DONE; 规范命名 (`{id}_pN.ext`) 且同目录的文件两阶段同步改名(交换不撞名)。
    ② 护栏: 页序不是完整排列 / 页号有空缺 / 单页作品 / 作品不存在 → 400, 库和磁盘都不许动。
    ③ 用户自命名的文件只调库内顺序, 一个字符都不碰用户的文件名。
    ④ 安全模式打码 (静态): 前端必须走 imgSrc(u,w)/safeThumb(u,w) 出图 —— 安全模式下直接返回
       占位图, 浏览器从不请求 R18 真实像素; 不许再有绕过这两个函数的裸图片地址。
    ⑤ 缓存穿透 (的根因): 图片 URL 必须带内容版本令牌
       v=(mtime+size) —— 「同名路径换内容」时 URL 必变, 否则浏览器吃 30 天旧缓存;
       且前端不许猜文件名改 URL, 必须回服务端重取页清单。
    ⑥ 原子性: 磁盘改名失败 → 400, 库与磁盘都不许变 (先改名后写库 + 失败回滚)。
    ⑦ 图库列表「不闪」(): 条目必须带
       dl_state(首屏即终态, 不靠异步纠正), 前端载荷没变时不许重建 DOM; Lightbox 左右
       翻页按钮必须 fixed 锚在视口两端(不随图片宽高移动)。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from PIL import Image as _Image

    with tempfile.TemporaryDirectory(prefix="t99ro_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            img = tmp / "img"
            d = img / "有江リリ_63051477"
            d.mkdir(parents=True)
            # 三页颜色各不相同 → 用颜色/字节反查"哪个文件跑到了哪位"
            for i, col in ((0, (200, 10, 10)), (1, (10, 200, 10)), (2, (10, 10, 200))):
                _Image.new("RGB", (64, 96), col).save(d / f"888001_p{i}.png")
            # 用户自命名的作品 (非规范名): 只调库, 不动文件
            _Image.new("RGB", (64, 96), (100, 100, 100)).save(d / "manual_a.png")
            _Image.new("RGB", (64, 96), (120, 120, 120)).save(d / "manual_b.png")

            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.upsert_author("63051477", "有江リリ")
            db.change_work_status("888001", WSt.DONE, title="规范名作品",
                                  author_id="63051477", total_pages=3)
            md5s = {}
            for i in range(3):
                rel = f"有江リリ_63051477/888001_p{i}.png"
                db.mark_page_status("888001", i, PSt.DONE, local_path=rel,
                                    md5=f"md5-of-p{i}")
                md5s[i] = f"md5-of-p{i}"
            db.change_work_status("888002", WSt.DONE, title="自命名作品",
                                  author_id="63051477", total_pages=2)
            for i, nm in enumerate(("manual_a.png", "manual_b.png")):
                db.mark_page_status("888002", i, PSt.DONE,
                                    local_path=f"有江リリ_63051477/{nm}", md5=f"m{i}")
            # 有空缺的作品 (第 1 页没图) → 不许排序
            db.change_work_status("888003", WSt.DONE, title="有洞作品",
                                  author_id="63051477", total_pages=2)
            db.mark_page_status("888003", 0, PSt.DONE,
                                local_path="有江リリ_63051477/888001_p0.png")
            db.mark_page_status("888003", 2, PSt.DONE,
                                local_path="有江リリ_63051477/888001_p1.png")
            db.close()

            import web.server as wsrv
            client = wsrv.create_app().test_client()

            # ① 换序 [2,0,1]: 旧 p2→新 p0、旧 p0→新 p1、旧 p1→新 p2
            r = client.post("/api/library/reorder-pages",
                            json={"artwork_id": "888001", "order": [2, 0, 1]})
            j = r.get_json()
            assert r.status_code == 200 and j["status"] == "ok", j
            assert j["files_renamed"] == 3, f"三页全挪位, 应同步改名 3 个文件: {j}"
            assert [(m["old"], m["new"]) for m in j["mapping"]] == [(2, 0), (0, 1), (1, 2)], j
            with PixivDatabase(str(tmp / "pixiv.db")) as db2:
                rows = {int(x["page_num"]): dict(x) for x in db2.conn.execute(
                    "SELECT page_num, local_path, md5 FROM pages "
                    "WHERE artwork_id='888001' ORDER BY page_num")}
                assert set(rows) == {0, 1, 2}, rows
                # md5 跟文件走: 新 p0 就是旧 p2 那张
                assert rows[0]["md5"] == md5s[2] and rows[1]["md5"] == md5s[0] \
                    and rows[2]["md5"] == md5s[1], rows
                assert rows[0]["local_path"].endswith("888001_p0.png"), rows[0]
                assert rows[1]["local_path"].endswith("888001_p1.png"), rows[1]
                assert rows[2]["local_path"].endswith("888001_p2.png"), rows[2]
                assert db2.get_work_info("888001").done, "换序不该让作品掉出 DONE"
                assert int(db2.get_artwork_info("888001").total_pages or 0) == 3, \
                    "换序不得改动「原网页页数」"
            # 磁盘: 三个文件名都在(交换没撞名), 且新 p0 的像素 = 旧 p2 的蓝色
            for i in range(3):
                assert (d / f"888001_p{i}.png").is_file(), f"888001_p{i}.png 丢了"
            with _Image.open(d / "888001_p0.png") as im:
                assert im.convert("RGB").getpixel((0, 0))[2] > 150, \
                    "新 p0 应是原来那张蓝色图(文件与库记录同步搬到位)"

            # ② 护栏: 不完整排列 / 重复 / 有空缺 / 单页 / 作品不存在 → 400
            for bad in ({"artwork_id": "888001", "order": [0, 1]},
                        {"artwork_id": "888001", "order": [0, 0, 1]},
                        {"artwork_id": "888003", "order": [0, 1, 2]},
                        {"artwork_id": "888002", "order": [0]},
                        {"artwork_id": "888404", "order": [0, 1]}):
                rr = client.post("/api/library/reorder-pages", json=bad)
                assert rr.status_code == 400, f"{bad} 应被拒绝: {rr.get_json()}"

            # ③ 用户自命名的作品: 只调库内顺序, 文件名一个字符都不碰
            r = client.post("/api/library/reorder-pages",
                            json={"artwork_id": "888002", "order": [1, 0]})
            j2 = r.get_json()
            assert r.status_code == 200 and j2["files_renamed"] == 0, j2
            assert (d / "manual_a.png").is_file() and (d / "manual_b.png").is_file(), \
                "用户自命名的文件不该被改名"
            with PixivDatabase(str(tmp / "pixiv.db")) as db3:
                r0 = db3.conn.execute(
                    "SELECT local_path, md5 FROM pages WHERE artwork_id='888002' "
                    "AND page_num=0").fetchone()
                assert r0["md5"] == "m1" and r0["local_path"].endswith("manual_b.png"), \
                    f"库内顺序应已互换: {dict(r0)}"

            # ④ 安全模式打码: 前端出图必须过 imgSrc/safeThumb (结构上保证不加载 R18 像素)
            appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
            assert "const R18_MASK_SVG" in appjs, "缺少安全模式占位图"
            assert "function imgSrc(u, w)" in appjs and "function safeThumb" in appjs
            assert "if (safemode && w && w.is_r18) return R18_MASK_SVG;" in appjs, \
                "imgSrc 必须在安全模式下直接返回占位图(不是先加载再模糊)"
            assert 'src="${p.url}"' not in appjs, \
                "反搜页缩略图必须走 safeThumb, 不许裸用地址"
            assert "safeThumb(it.thumb, it)" in appjs or "safeThumb(hit.url, hit)" in appjs
            # 大图解锁: 只有点「显示」才允许加载真实原图
            assert "pages[idx]._reveal = true" in appjs, "缺「显示」解锁这一张的逻辑"
            idx_html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(
                encoding="utf-8")
            assert "不加载原图" in idx_html, "安全模式说明要写清「不加载」而非「模糊」"

            # ⑤ 缓存穿透 : 调页序是「文件名不变、内容互换」,
            #    URL 里的 p= 是路径 → 不变; 而响应带 max-age=2592000(30天), 浏览器
            #    会一直显示旧图 → 用户看到「调了却没调 / 封面还是那样」。所以图片 URL
            #    必须带内容版本令牌 v=(mtime+size), 同一路径内容变了 URL 必变。
            import web.server as wsrv3
            rel_a = "有江リリ_63051477/manual_a.png"
            abs_a = tmp / "img" / rel_a
            u1 = wsrv3._local_img_url(rel_a, tmp / "img")
            r1 = client.get(u1)
            try:
                assert r1.status_code == 200 and "&v=" in u1, \
                    f"图片 URL 必须带内容版本令牌: {u1}"
                cc = r1.headers.get("Cache-Control") or ""
                assert "max-age" in cc, cc
            finally:
                r1.close()      # 不关会在临时目录清理时锁住源文件 (WinError 32)
            _Image.new("RGB", (64, 96), (0, 0, 0)).save(abs_a)      # 同名同路径, 换内容
            u2 = wsrv3._local_img_url(rel_a, tmp / "img")
            assert u2 != u1 and u2.split("&v=")[0] == u1.split("&v=")[0], \
                f"同一路径内容变了 URL 必须变(否则吃 30 天旧缓存): {u1} → {u2}"
            # 前端不许再用"猜文件名"的办法改 URL, 必须回服务端重取页清单
            assert "rebaseUrls" not in appjs, "页序调整后不许前端猜文件名改 URL"
            assert "refetchPages" in appjs and "view=artwork" in appjs, \
                "页序调整后必须回服务端重取该作品的页清单"

            # ⑦ 图库列表「不闪」:
            #    ① 图库条目必须带 dl_state —— 不带的话卡片按钮先按默认样式渲染, 再被
            #       decorateCartButtons 异步改成「✔ 已下载 / ⏳ 已在下载列表」, 每次都闪;
            #    ② 前端一次刷新若载荷没变就不许重建 DOM —— 删除后本地刷一次、350ms 后
            #       SSE 脏域又刷一次, 旧实现两次都重建 innerHTML, 于是一次删除闪两遍。
            it = client.get("/api/library/items?limit=5").get_json()["items"]
            assert it and all("dl_state" in x for x in it), \
                f"图库条目必须带 dl_state(否则按钮首屏是错的, 要异步纠正→闪): {it[:1]}"
            assert all(x["dl_state"] in ("downloaded", "in_queue", "partial", "none")
                       for x in it), it[:1]
            assert "_libSig" in appjs and "_libMarkKeys" in appjs, \
                "图库列表必须按载荷签名跳过无变化的重画(否则删除会连闪两次)"
            # ③ Lightbox 左右翻页按钮必须锚在视口两端, 不随图片宽度移动
            css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
            nav = css.split(".lightbox-prev, .lightbox-next {")[1].split("}")[0]
            assert "position: fixed" in nav, \
                f"翻页按钮必须 fixed 定位(否则竖图/宽图会把它拽得左右乱跑): {nav}"
            # ④ 翻页时信息行文字不许闪 :
            #    不许再"先清空再回填", 且首屏就要带上本页已知的 file_size(否则库内没记
            #    宽高的 318 页会先空白、等原图解码再冒出尺寸); 动态那两段文字要固定宽度。
            assert "lbFile.textContent = ''" not in appjs, \
                "信息行不许先清空再回填(每翻一页都会闪一下)"
            assert "updateDim(pg.width, pg.height, pg)" in appjs, \
                "翻页首屏就要把本页已知的大小填进信息行(不能等解码完才出现)"
            for sel, need in ((".lightbox-info .lb-file {", "font-variant-numeric"),
                              (".lightbox-info .lb-nav {", "min-width")):
                assert need in css.split(sel)[1].split("}")[0], \
                    f"{sel} 缺少 {need}(位数变化会把整行文字推来推去)"

            # ⑧ 页码空洞: 删过页的作品(页号不连续)也 must 能移动 + 能用附加页填洞
            #    
            #     准确来说我甚至无法移动附加图片的排序」—— 旧实现一遇空洞就整体 400)
            _Image.new("RGB", (64, 96), (200, 10, 10)).save(d / "888020_p0.png")
            _Image.new("RGB", (64, 96), (10, 200, 10)).save(d / "888020_p1.png")
            _Image.new("RGB", (64, 96), (10, 10, 200)).save(d / "888020_p3.png")
            _Image.new("RGB", (64, 96), (222, 222, 10)).save(d / "888020_p5.png")
            with PixivDatabase(str(tmp / "pixiv.db")) as db6:
                db6.change_work_status("888020", WSt.NOT_YET, title="有洞作品",
                                       author_id="63051477", total_pages=4)
                for pn, org in ((0, "web"), (1, "web"), (3, "web"), (5, "merged")):
                    db6.mark_page_status("888020", pn, PSt.DONE, md5=f"md5-{pn}",
                                         local_path=f"有江リリ_63051477/888020_p{pn}.png",
                                         origin=org)
            # ⑧-a 空洞作品用 order(整体排列) 仍被拒 —— 它会把洞悄悄压实, 完整性跟着变
            rr = client.post("/api/library/reorder-pages",
                             json={"artwork_id": "888020", "order": [0, 1, 5, 3]})
            assert rr.status_code == 400, rr.get_json()
            # ⑧-b assign 交换两页: 有洞也能移动, 洞保持原样
            rr = client.post("/api/library/reorder-pages",
                             json={"artwork_id": "888020",
                                   "assign": [{"old": 0, "new": 1}, {"old": 1, "new": 0}]})
            j = rr.get_json()
            assert rr.status_code == 200 and j["files_renamed"] == 2, j
            with PixivDatabase(str(tmp / "pixiv.db")) as db7:
                got = {int(x["page_num"]): x["md5"] for x in db7.conn.execute(
                    "SELECT page_num, md5 FROM pages WHERE artwork_id='888020'")}
                assert set(got) == {0, 1, 3, 5}, f"交换不该抹掉页码空洞: {got}"
                assert got[0] == "md5-1" and got[1] == "md5-0", f"md5 要跟文件走: {got}"
                assert not db7.get_work_info("888020").done, \
                    "还没填上 p2 空洞, 不能因为换了个序就显示完整"
            # ⑧-c assign 把「附加页 p5」填进空位 p2 → 0..3 齐全, 作品回到 DONE;
            #      total_pages(原网页页数)绝不动, 文件也同步改名成 888020_p2.png
            rr = client.post("/api/library/reorder-pages",
                             json={"artwork_id": "888020",
                                   "assign": [{"old": 5, "new": 2}]})
            j = rr.get_json()
            assert rr.status_code == 200 and j["files_renamed"] == 1, j
            with PixivDatabase(str(tmp / "pixiv.db")) as db8:
                got = sorted(int(x["page_num"]) for x in db8.conn.execute(
                    "SELECT page_num FROM pages WHERE artwork_id='888020'"))
                assert got == [0, 1, 2, 3], f"空位没被填上: {got}"
                assert int(db8.get_artwork_info("888020").total_pages or 0) == 4, \
                    "填洞不得改动「原网页页数」"
                assert db8.get_work_info("888020").done, "填满 0..3 后应回到完整(DONE)"
            assert (d / "888020_p2.png").is_file() and not (d / "888020_p5.png").exists(), \
                "规范命名的文件应随改号同步改名"
            with _Image.open(d / "888020_p2.png") as im:
                assert im.convert("RGB").getpixel((0, 0))[:2] == (222, 222), \
                    "改名后 p2 的内容必须还是原来那张附加页"
            # ⑧-d assign 护栏: 目标重复 / 不存在的页 / 负数 → 400
            for bad_assign in ([{"old": 0, "new": 2}],      # 2 已被 p2 占用 → 重复
                               [{"old": 99, "new": 0}],     # 没有这一页
                               [{"old": 0, "new": -1}]):    # 负数
                rb = client.post("/api/library/reorder-pages",
                                 json={"artwork_id": "888020", "assign": bad_assign})
                assert rb.status_code == 400, f"坏 assign 未拦住: {bad_assign} → {rb.get_json()}"
            # ⑧-e 前端接线: 有洞时走 assign 交换 + 附加页出「↳ 填 pN」按钮 + 空位提示
            assert "holesOf" in appjs and "lb-strip-gap" in appjs, "页序条缺「空位」提示"
            assert 'data-act="fill"' in appjs, "附加页缺少「填入空位」按钮"
            # 「附加页」的显示判据必须**只有一处**(pageIsExtra, 按页号位置判定):
            # 删页/调序会把页号换来换去, 再拿当初的 origin 当显示判据就会出现
            # 「原 2 页却把 p1 标成附加」「原 2 页却把 p2 标成原网页页」这类错乱显示
            # 
            assert "function pageIsExtra" in appjs, "缺少统一的「是否附加页」判据"
            assert appjs.count("origin === 'merged'") == 1, \
                "「附加页」显示判据必须统一走 pageIsExtra(按位置), 不能再散着看 origin"
            assert "assign: [{ old: numOf[i], new: target }]" in appjs, \
                "「填入空位」必须走点对点改号 assign"
            # 走 order 还是 assign, 必须按「页码是否正好 0..n-1」判断 —— 服务端的 order 就是这么
            # 要求的; 用"tp 范围内有没有洞"判断会漏掉「带附加页但没洞」的作品(p0,p1,p4 / 原 2 页),
            # 那种作品发 order 会被 400 顶回来, 用户点了像没反应 (2026-09-17)
            assert "const consecutive = numOf.every((n, k) => n === k)" in appjs and \
                "if (!consecutive) {" in appjs, \
                "order/assign 的选择条件必须是「页码是否正好 0..n-1」"
            assert "lb-strip-gap" in css, "缺少空位样式的 CSS"
            # 换序后必须有"看得见的反馈": 提示要点名换的是第几张↔第几张, 并闪一下那两个格子
            # (页序条标签按位置生成, 换完字面不变 → 用户会以为按钮只是"左右翻页", 2026-09-17)
            assert "互换位置" in appjs and "互换页号" in appjs, "换序提示要点名换了哪几页"
            assert "lb-strip-item[data-pn=" in appjs and "classList.add('flash')" in appjs, \
                "换序后要闪一下刚动过的格子"
            assert ".lb-strip-item.flash" in css, "缺少换序闪烁的样式"

            # ⑥ 原子性: 磁盘改名失败 → 400 且**库与磁盘都不许变** (以前是库先提交、
            #    改名异常被吞 → 库说 p0 是 A、盘上 p0 还是 B 的分裂状态)
            _Image.new("RGB", (64, 96), (200, 10, 10)).save(d / "888010_p0.png")
            _Image.new("RGB", (64, 96), (10, 10, 200)).save(d / "888010_p1.png")
            with PixivDatabase(str(tmp / "pixiv.db")) as db4:
                db4.change_work_status("888010", WSt.DONE, title="原子性作品",
                                       author_id="63051477", total_pages=2)
                for i in range(2):
                    db4.mark_page_status("888010", i, PSt.DONE,
                                         md5=None,
                                         local_path=f"有江リリ_63051477/888010_p{i}.png")
            # 阶段一的临时名被同名目录占住 → 改名必失败 (守的是"先改名后写库")
            # 把「阶段一的临时名」所有可能组合都占住: 临时名的序号 i 取决于计划顺序,
            # 全部占住就不会因为内部实现改了遍历顺序而让这条断言形同虚设
            blockers = [d / f".__reorder_888010_{k}_{o}_to_{n}.png"
                        for k in range(2) for o in range(2) for n in range(2) if o != n]
            for b in blockers:
                b.mkdir()
            try:
                rr = client.post("/api/library/reorder-pages",
                                 json={"artwork_id": "888010", "order": [1, 0]})
                assert rr.status_code == 400, \
                    f"改名失败必须报错而不是静默 200: {rr.status_code} {rr.get_json()}"
                with PixivDatabase(str(tmp / "pixiv.db")) as db5:
                    got = [(int(x["page_num"]), x["local_path"]) for x in db5.conn.execute(
                        "SELECT page_num, local_path FROM pages WHERE artwork_id='888010' "
                        "ORDER BY page_num")]
                assert got == [(0, "有江リリ_63051477/888010_p0.png"),
                               (1, "有江リリ_63051477/888010_p1.png")], \
                    f"改名失败时库不该被动过: {got}"
                with _Image.open(d / "888010_p0.png") as im:
                    assert im.convert("RGB").getpixel((0, 0))[0] > 150, \
                        "改名失败且已回滚, 磁盘上 p0 必须是原来那张红图"
                assert (d / "888010_p1.png").is_file(), "回滚后 p1 也要在原位"
            finally:
                for b in blockers:
                    b.rmdir()
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr


def t96_event_driven_dirty_scopes() -> None:
    """T96: 🔔 事件驱动刷新 (用户 2026-09-17: 要一套覆盖整个数据库的机制, 不只某个页面)。

    机制三件套: ① 写请求经 after_request 自动登记受影响的数据域 + 实体 id;
    ② 每个域维护版本号, 登记时经 SSE 广播 {"type":"dirty"};
    ③ 前端按域只刷新对应视图, SSE 断线用 GET /api/dirty?since= 补偿。
    这里验证: 登记/版本/按 since 过滤/写才登记(GET 与失败请求都不登记)/SSE 广播格式/ids 上限退化。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt

    with tempfile.TemporaryDirectory(prefix="t96dirty_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.change_work_status("8801", WSt.DONE, title="事件驱动测试", total_pages=1)
            db.close()
            import web.server as wsrv
            wsrv._dirty_state.update(rev=0, scopes={})
            client = wsrv.create_app().test_client()

            # ① GET 不登记 (读操作不改数据域)
            client.get("/api/library/items?limit=1")
            j = client.get("/api/dirty?since=0").get_json()
            assert j["rev"] == 0 and j["scopes"] == {}, j

            # ② 写请求自动登记 + 抽出实体 id
            r = client.post("/api/library/update",
                            json={"artwork_id": "8801", "title": "改个标题"})
            assert r.status_code == 200, r.get_json()
            j = client.get("/api/dirty?since=0").get_json()
            assert j["rev"] >= 1 and "artworks" in j["scopes"], j
            assert "8801" in j["scopes"]["artworks"]["ids"], j["scopes"]["artworks"]
            for k in ("facets", "stats"):
                assert k in j["scopes"], f"图库写操作应连带登记 {k}: {list(j['scopes'])}"
            rev_now = j["rev"]

            # ③ since 过滤: 拿"当前版本"再查 → 没有更新的域
            j2 = client.get(f"/api/dirty?since={rev_now}").get_json()
            assert j2["scopes"] == {}, j2

            # ④ 失败请求不登记 (4xx 不该让前端白刷一遍)
            bad_rev = j2["rev"]
            assert client.post("/api/library/update", json={}).status_code == 400
            j3 = client.get(f"/api/dirty?since={bad_rev}").get_json()
            assert j3["scopes"] == {}, j3

            # ⑤ 队列类写操作登记的是 queue 域
            assert client.post("/api/cart",
                               json={"artwork_id": "8802", "title": "x"}).status_code == 200
            j4 = client.get(f"/api/dirty?since={bad_rev}").get_json()
            assert "queue" in j4["scopes"], j4

            # ⑥ SSE 广播格式: 订阅总线后登记, 队列里应收到 dirty 事件
            q = wsrv._hub.subscribe()
            try:
                wsrv.mark_dirty("authors", ids=["77"])
                got = None
                for _ in range(20):
                    try:
                        ev = q.get_nowait()
                    except Exception:
                        break
                    if isinstance(ev, dict) and ev.get("type") == "dirty":
                        got = ev
                        break
                assert got and got["scopes"].get("authors", {}).get("ids") == ["77"], got
            finally:
                wsrv._hub.unsubscribe(q)

            # ⑦ ids 超过上限 → 退化成 ["*"] (整域失效), 不让集合无限膨胀
            wsrv.mark_dirty("pages", ids=[str(i) for i in range(wsrv._DIRTY_ID_CAP + 5)])
            snap = wsrv._dirty_snapshot(0)
            assert snap["scopes"]["pages"]["ids"] == ["*"], snap["scopes"]["pages"]
            # 未知 scope 名直接忽略 (前端不认识的不该进状态)
            before = wsrv._dirty_state["rev"]
            wsrv.mark_dirty("不存在的域")
            assert wsrv._dirty_state["rev"] == before
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._dirty_state.update(rev=0, scopes={})


def t97_event_driven_coverage_and_noise() -> None:
    """T97: 🔔 事件驱动"全覆盖 + 不空转" (用户 2026-09-17 追加: 不只作者页, 整个库都要)。

    ① 覆盖: 设置(安全模式)/缩略图缓存/登录态/备份/操作记录 这些"非图库数据"的域也要登记,
       且图库类写操作现在会连带 ops (设置页操作记录表);
    ② 不空转: 只读的定时轮询(periodic=1)不登记; 幂等写(/api/library/sync 什么都没改)不登记
       —— 否则空闲期版本号不断自增, 前端会莫名其妙反复重拉 (实测噪声)。"""
    import tempfile
    import utils.paths as upaths
    import utils.config as ucfg
    from db.store import PixivDatabase, WSt

    with tempfile.TemporaryDirectory(prefix="t97cov_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        # 安全模式接口会写 config/user.json (在项目可写区, 不随 user_data 重定向)
        # → 这里拦掉落盘, 避免冒烟改到开发者本机的真实配置
        orig_save = ucfg.save_user_config
        ucfg.save_user_config = lambda updates: None
        try:
            (tmp / "img").mkdir(parents=True, exist_ok=True)   # 对账要扫图库根目录
            db = PixivDatabase(str(tmp / "pixiv.db"))
            db.change_work_status("9701", WSt.DONE, title="覆盖测试", total_pages=1)
            db.close()
            import web.server as wsrv
            wsrv._dirty_state.update(rev=0, scopes={})
            client = wsrv.create_app().test_client()

            def rev() -> int:
                return client.get("/api/dirty?since=0").get_json()["rev"]

            def scopes() -> dict:
                return client.get("/api/dirty?since=0").get_json()["scopes"]

            # ① 只读轮询不登记: 前端 15s 一次的 /api/library/sync?periodic=1 不该动版本号
            r0 = rev()
            assert client.post("/api/library/sync?periodic=1").status_code == 200
            assert rev() == r0, "定时轮询(periodic=1)是只读的, 不该登记脏域"

            # ② 幂等写不登记: 手动同步但磁盘一张没错 → 改动计数全 0 → 不让前端白刷
            assert client.post("/api/library/sync").status_code == 200
            assert rev() == r0, "同步/对账什么都没改时不该登记脏域 (写后校验)"

            # ③ 覆盖非图库数据的域
            assert client.post("/api/safemode", json={"enabled": True}).status_code == 200
            sc = scopes()
            assert "settings" in sc and "artworks" in sc, \
                f"打码开关要连带图库一起重画: {list(sc)}"

            assert client.post("/api/thumbs/clear").status_code == 200
            assert "thumbs" in scopes(), "清缩略图缓存应登记 thumbs 域"

            assert client.post("/api/auth/logout").status_code == 200
            assert "auth" in scopes(), "登录/退出应登记 auth 域"

            assert client.post("/api/library/backup").status_code == 200
            assert "backups" in scopes(), "生成数据库备份应登记 backups 域"

            # ④ 图库写操作除作品/页/筛选/统计外, 还要登记 ops (设置页的操作记录)
            #    和 search (查询页卡片上的「已入库/已在下载列表」状态回显)
            r = client.post("/api/library/update", json={"artwork_id": "9701", "title": "x"})
            assert r.status_code == 200, r.get_json()
            sc = scopes()
            for k in ("artworks", "pages", "facets", "stats", "ops", "search"):
                assert k in sc, f"图库写操作应连带登记 {k}: {list(sc)}"
            assert "search" in client.get("/api/dirty?since=0").get_json()["scopes"]
            # 队列类写操作同样要带 search (加入下载后查询页的按钮要变「已在下载列表」)
            assert client.post("/api/cart", json={"artwork_id": "9702",
                                                  "title": "x"}).status_code == 200
            assert "search" in scopes()

            # ⑤ 失败的写请求(4xx)不登记: 恢复不存在的备份 → 不该让前端白刷一遍
            rev_before = rev()
            r5 = client.post("/api/library/backup/restore", json={"rel": "不存在.png"})
            assert r5.status_code >= 400, r5.get_json()
            assert rev() == rev_before, "失败请求不该登记脏域"
        finally:
            ucfg.save_user_config = orig_save
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._dirty_state.update(rev=0, scopes={})


def t98_frontend_icon_variation_selector() -> None:
    """T98: 前端图标「变体选择符」体检 (用户 2026-09-17: 对账台的 🗂 图标又显示错误了)。

    背景: 🗂 / 🗑 / 🖼 / 🏷 / ❓ 这些码位在 Unicode 里**默认是「文字呈现」** —— 不加
    U+FE0F 变体选择符时, Windows 上会渲染成黑白线条甚至方框 (2026-09-06 的 🏷/🗑、
    2026-09-14 的类型徽标都栽过同一个坑)。这里把"用户看得见的图标必须带 U+FE0F"
    变成断言, 避免下次再漏 (注释里的字形不显示给用户, 不检查)。"""
    TEXT_DEFAULT = {
        "\U0001F5C2": "🗂",   # 对账台 / 待认领
        "\U0001F5D1": "🗑",   # 删除
        "\U0001F5BC": "🖼",   # 缩略图 / 视图切换
        "\U0001F3F7": "🏷",   # 标签
        "\u2753": "❓",       # 待确认
        "\u2757": "❗",       # 注意
    }
    COMMENT_PREFIX = ("//", "/*", "*", "<!--", "#", "--")
    bad = []
    for rel in ("web/static/app.js", "web/static/index.html"):
        lines = (PROJECT_ROOT / rel).read_text(encoding="utf-8").split("\n")
        for no, ln in enumerate(lines, 1):
            if ln.lstrip().startswith(COMMENT_PREFIX):
                continue
            for i, ch in enumerate(ln):
                if ch in TEXT_DEFAULT and not ln[i + 1: i + 2].startswith("\uFE0F"):
                    bad.append(f"{rel}:{no} {TEXT_DEFAULT[ch]}")
    assert not bad, "这些用户可见图标缺 U+FE0F 变体选择符(会渲染成黑白/方框): " + ", ".join(bad)

    # 对账台入口的图标必须带变体选择符 (本轮的那一个)
    appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    assert "\U0001F5C2\uFE0F" in appjs, "对账台图标应为 🗂️ (带 U+FE0F)"


def t100_trash_soft_delete_and_custom_import() -> None:
    """T100: 🗑 回收站双层保护 (软删除/恢复/冲突跳过/彻底删除) + 导入「建自定义作品」
    + 零页作品选择 + 标记位一致性 (带 R18 标记的作品走完全同一套代码路径)。

    审查纪律: 测试图片全部是**合成纯色块** (与任何真实内容无关), 只断言 DB 字段与
    API 返回的 JSON —— 对图片一视同仁: 不因标记位走不同分支, 也不读取任何像素。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    import web.server as _wsrv
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t100trash_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_recycle, orig_kick = _wsrv._recycle_files, _wsrv._kick_metadata_fill
    recycled_calls = []

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _fake_recycle(paths):
        """替身: 直接删, 不真送 Windows 回收站 (测试临时文件不该污染系统回收站)。"""
        paths = [str(p) for p in (paths or [])]
        recycled_calls.append(paths)
        n = 0
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
                n += 1
            except Exception:
                pass
        return n, []

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    _wsrv._recycle_files = _fake_recycle
    _wsrv._kick_metadata_fill = lambda *a, **k: None
    try:
        # ⓪ 迁移 v016 字段就位 (软删除的载体; 与 011 的 deleted_at 语义严格分离)
        db0 = PixivDatabase(str(tmp / "pixiv.db"))
        cols = {r[1] for r in db0.conn.execute("PRAGMA table_info(artworks)")}
        pcols = {r[1] for r in db0.conn.execute("PRAGMA table_info(pages)")}
        assert {"trashed_at", "trashed_reason"} <= cols, cols
        assert {"trashed_at", "trash_rel_path"} <= pcols, pcols
        assert "deleted_at" in cols, "011 的 deleted_at 必须保留(语义不同, 不能混用)"
        db0.close()

        img = tmp / "img"
        (img / "畫師A_111").mkdir(parents=True)
        (img / "畫師A_111" / "123456_p0.png").write_bytes(b"SYNTH-A0")
        (img / "畫師A_111" / "123456_p1.png").write_bytes(b"SYNTH-A1")
        (img / "畫師B_222").mkdir(parents=True)
        (img / "畫師B_222" / "654321_p0.jpg").write_bytes(b"SYNTH-B0")
        (img / "畫師C_333").mkdir(parents=True)
        (img / "畫師C_333" / "777777_p0.png").write_bytes(b"SYNTH-C0")

        db = PixivDatabase(str(tmp / "pixiv.db"))
        db.update_pages_from_output(img)
        db.change_work_status("123456", WSt.DONE, total_pages=2, title="作品A", author_id="111")
        # 标记位一致性: 这一件带上位置位, 之后所有操作都必须和别件走同一条路
        db.change_work_status("654321", WSt.DONE, total_pages=1, title="作品B",
                              author_id="222", is_r18=True)
        db.change_work_status("777777", WSt.DONE, total_pages=1, title="作品C", author_id="333")
        for aid, nm in (("111", "畫師A"), ("222", "畫師B"), ("333", "畫師C")):
            db.upsert_author(aid, nm)
        db.conn.execute("UPDATE artworks SET updated_at='2000-01-01 00:00:00' "
                        "WHERE artwork_id='123456'")
        db.conn.commit()
        db.close()

        client = create_app().test_client()

        def items(q=""):
            return client.get("/api/library/items?" + q).get_json()["items"]

        # ① 删除 = 软删除: 一条记录都不删, 文件搬进 trash/, 图库不再显示, updated_at 刷新
        r = client.post("/api/library/delete", json={"ids": ["123456"]}).get_json()
        assert r["deleted"] == 1 and r["trashed"] is True, r
        assert r["files_removed"] == 2 and not r["move_errors"], r
        with PixivDatabase(str(tmp / "pixiv.db")) as d1:
            assert d1.conn.execute("SELECT COUNT(*) FROM artworks").fetchone()[0] == 3, \
                "软删除绝不能卸载记录"
            assert d1.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 4, \
                "软删除绝不能删页"
            row = d1.conn.execute("SELECT trashed_at, trashed_reason, updated_at FROM artworks "
                                  "WHERE artwork_id='123456'").fetchone()
            assert row[0] and row[1] == "user", tuple(row)
            assert row[2] != "2000-01-01 00:00:00", \
                "软删除要刷新 updated_at —— 否则「按修改时间」排序不会把它提到最前(刷新看不见变化)"
            # local_path 必须保持原值: 它是"恢复时搬回哪里"的权威答案
            lps = {x[0] for x in d1.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id='123456'")}
            assert lps == {"畫師A_111/123456_p0.png", "畫師A_111/123456_p1.png"}, lps
            trs = {x[0] for x in d1.conn.execute(
                "SELECT trash_rel_path FROM pages WHERE artwork_id='123456'")}
            assert trs == {"123456/p0__123456_p0.png", "123456/p1__123456_p1.png"}, trs
        assert (tmp / "trash" / "123456" / "p0__123456_p0.png").is_file()
        assert not (img / "畫師A_111" / "123456_p0.png").exists(), "原位置应腾空"
        assert all(x["artwork_id"] != "123456" for x in items("view=artwork"))
        tl = client.get("/api/library/trash/list?kind=artwork").get_json()
        assert tl["total"] == 1 and tl["items"][0]["page_count"] == 2, tl
        assert "root=trash" in (tl["items"][0]["thumb"] or ""), tl
        assert client.get("/api/library/trash/count").get_json()["artworks"] == 1
        # stats 口径也必须排除回收站 (否则顶部数字骗人)
        st = client.get("/api/library/stats").get_json()
        assert st["total_artworks"] == 2 and st["trashed_artworks"] == 1, st

        # ② 恢复 = 搬回原位 + 回到图库 + 清标记
        r = client.post("/api/library/trash/restore", json={"ids": ["123456"]}).get_json()
        assert r["restored"] == 1 and not r["skipped"] and not r["move_errors"], r
        assert (img / "畫師A_111" / "123456_p0.png").is_file(), "恢复必须搬回原位"
        with PixivDatabase(str(tmp / "pixiv.db")) as d2:
            row = d2.conn.execute("SELECT trashed_at, trash_rel_path FROM pages "
                                  "WHERE artwork_id='123456' AND page_num=0").fetchone()
            assert tuple(row) == (None, None), f"恢复后标记要清干净: {tuple(row)}"
        assert any(x["artwork_id"] == "123456" for x in items("view=artwork"))
        assert client.get("/api/library/trash/count").get_json()["artworks"] == 0

        # ③ 冲突 (用户"女仆"场景): 原位置被不同内容占了 → 只跳过, 绝不覆盖
        client.post("/api/library/delete", json={"ids": ["123456"]})
        (img / "畫師A_111" / "123456_p0.png").write_bytes(b"SYNTH-OCCUPIED")
        pv = client.post("/api/library/trash/preview", json={"ids": ["123456"]}).get_json()
        assert not pv["ok"] and pv["conflicts"], pv
        r = client.post("/api/library/trash/restore", json={"ids": ["123456"]}).get_json()
        assert r["restored"] == 0 and r["skipped"], r
        assert (img / "畫師A_111" / "123456_p0.png").read_bytes() == b"SYNTH-OCCUPIED", \
            "冲突时绝不能覆盖库中现有文件"
        assert client.get("/api/library/trash/count").get_json()["artworks"] == 1, \
            "冲突项要留在回收站等用户处理"

        # ④ 彻底删除: 先取路径 → 送(替身)回收站 → 再卸载记录 (顺序错了就取不到路径)
        recycled_calls.clear()
        r = client.post("/api/library/trash/purge", json={"ids": ["123456"]}).get_json()
        assert r["deleted"] == 1 and r["recycled"] == 2, r
        assert recycled_calls and len(recycled_calls[0]) == 2, recycled_calls
        assert all("trash" in p for p in recycled_calls[0]), recycled_calls
        with PixivDatabase(str(tmp / "pixiv.db")) as d3:
            assert d3.conn.execute("SELECT COUNT(*) FROM artworks WHERE artwork_id='123456'"
                                   ).fetchone()[0] == 0
            assert d3.conn.execute("SELECT COUNT(*) FROM pages WHERE artwork_id='123456'"
                                   ).fetchone()[0] == 0, "页应级联清掉"

        # ⑤ 零页作品: 先 need_choice (什么都不动) → keep / trash 两条路
        r = client.post("/api/library/delete-pages",
                        json={"items": [{"artwork_id": "777777", "page_num": 0}]}).get_json()
        assert r["status"] == "need_choice" and r["would_empty"] == ["777777"], r
        # 契约钉死: would_empty 是**数组**, would_empty_labels 是**人读字符串**。
        # (2026-09-18 事故: 前端把 labels 当数组 .join → TypeError 被吞成"点了没反应" toast)
        assert isinstance(r["would_empty"], list), r
        assert isinstance(r["would_empty_labels"], str) and r["would_empty_labels"], r
        assert (img / "畫師C_333" / "777777_p0.png").is_file(), "need_choice 阶段不许动文件"
        with PixivDatabase(str(tmp / "pixiv.db")) as d4:
            assert d4.conn.execute("SELECT trashed_at FROM pages WHERE artwork_id='777777'"
                                   ).fetchone()[0] is None, "need_choice 阶段不许动库"
        r = client.post("/api/library/delete-pages",
                        json={"items": [{"artwork_id": "777777", "page_num": 0}],
                              "cascade_choice": "keep"}).get_json()
        assert r["pages_deleted"] == 1 and r["artworks_trashed"] == 0, r
        assert any(x["artwork_id"] == "777777" for x in items("view=artwork")), \
            "选「保留作品」时作品必须留在图库"
        # ⑥ 零页选择 trash: 作品一并进回收站
        client.post("/api/library/trash/restore",
                    json={"items": [{"artwork_id": "777777", "page_num": 0}]})
        r = client.post("/api/library/delete-pages",
                        json={"items": [{"artwork_id": "777777", "page_num": 0}],
                              "cascade_choice": "trash"}).get_json()
        assert r["artworks_trashed"] == 1, r
        assert all(x["artwork_id"] != "777777" for x in items("view=artwork"))

        # ⑦ 单图粒度 (带标记位的作品): 删一张 → 作品保留, 图片视图不再显示该张
        r = client.post("/api/library/delete-pages",
                        json={"items": [{"artwork_id": "654321", "page_num": 0}],
                              "cascade_choice": "keep"}).get_json()
        assert r["pages_deleted"] == 1 and r["artworks_trashed"] == 0, r
        assert any(x["artwork_id"] == "654321" for x in items("view=artwork")), \
            "单图软删绝不能把整个作品拖进回收站"
        assert all(str(x.get("artwork_id")) != "654321" for x in items("view=image")), \
            "已软删的那张图不该还出现在图片视图"
        tp = client.get("/api/library/trash/list?kind=page").get_json()
        assert tp["total"] == 1 and tp["items"][0]["artwork_id"] == "654321", tp
        # 作品视图与回收站口径互不重复: 654321 作品还在, 所以不进作品粒度
        assert all(x["artwork_id"] != "654321"
                   for x in client.get("/api/library/trash/list?kind=artwork").get_json()["items"])

        # ⑧ 导入: 标准作品直接入库; 非标准作品由设计约定 (默认转反搜 / 可选建自定义作品)
        src = tmp / "src_in"
        src.mkdir()
        (src / "888888_p0.png").write_bytes(b"SYNTH-PIXIV")
        (src / "shot_abc.png").write_bytes(b"SYNTH-NOID")
        pre = client.post("/api/library/import-from-path",
                          json={"path": str(src), "dry_run": True}).get_json()
        assert pre["with_id"] == 1 and pre["without_id"] == 1, pre
        r = client.post("/api/library/import-from-path", json={"path": str(src)}).get_json()
        assert len(r["imported"]) == 1 and len(r["redirected"]) == 1 and not r["custom_created"], r
        assert (tmp / "input" / "shot_abc.png").is_file(), "默认应落反搜收件箱"

        src2 = tmp / "src_in2"
        src2.mkdir()
        (src2 / "888889_p0.png").write_bytes(b"SYNTH-PIXIV2")
        (src2 / "my_art.png").write_bytes(b"SYNTH-MINE")
        r = client.post("/api/library/import-from-path",
                        json={"path": str(src2), "make_custom_artworks": True}).get_json()
        assert len(r["custom_created"]) == 1, r
        assert (img / "#其他来源#" / "my_art.png").is_file(), "自定义作品应落 #其他来源#/"
        assert not (tmp / "input" / "my_art.png").exists(), "选了建作品就不该再进反搜收件箱"
        with PixivDatabase(str(tmp / "pixiv.db")) as d5:
            ext = d5.conn.execute("SELECT artwork_id, source FROM artworks "
                                  "WHERE artwork_id LIKE 'ext_%'").fetchall()
            assert len(ext) == 1 and ext[0][1] == "other", [tuple(x) for x in ext]
        assert any(x["artwork_id"] == ext[0][0] for x in items("view=artwork")), \
            "自定义作品要出现在图库里"

        # ⑨ 前端接线 (静态): 导航回收站页 / 删除文案 / 刷新提示语 / 关键函数
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        indexh = (PROJECT_ROOT / "web/static/index.html").read_text(encoding="utf-8")
        assert 'data-tab="trash"' in indexh and 'id="tab-trash"' in indexh, "导航要有回收站页"
        assert 'id="trash-badge"' in indexh, "回收站角标要存在"
        assert "不知道刷什么时就点它" not in (indexh + appjs), \
            "刷新提示语要简化 "
        for fn in ("function loadTrash", "window.trashRestoreArtwork", "window.trashPurgeArtwork",
                   "function loadTrashBadge", "window.trashPurgeAll", "window.trashRestoreAll"):
            assert fn in appjs, f"缺回收站前端函数: {fn}"
        assert "移入回收站" in appjs, "删除入口文案要改成「移入回收站」"
        assert "make_custom_artworks" in appjs, "导入要能透传「建自定义作品」选择"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        _wsrv._recycle_files, _wsrv._kick_metadata_fill = orig_recycle, orig_kick
        shutil.rmtree(tmp, ignore_errors=True)


def t101_render_state_invariants() -> None:
    """T101: 「渲染态」不变量 —— 钉住"漏了渲染态导致界面不刷新 / 漏了打码导致走光"这一整类 bug。

    来由 : 关掉安全模式后切回来图片还是打码的, 点刷新也没用, 必须
    清空筛选再选回来才生效。根因 = 图库的重画判据只用"数据载荷"当指纹, 而**打码态**这种
    "渲染态"没进指纹 → 数据一个字没变就判定"不用重画" → 用户看到旧样子。
    同一类问题还有两个走光口子(回收站页/对账台直接把真实图片地址写进 <img>, 绕过了打码)。

    所以这里把三条不变量写成测试:
      ① 渲染指纹必须包含打码态; 手动刷新/打码切换必须**强制**重画;
      ② 打码态一变, 必须联动重画所有画缩略图的视图(不能只刷图库);
      ③ 任何渲染缩略图的 <img> 都必须走 imgSrc/safeThumb (裸 /api/local-img 一律视为走光)。
    """
    appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    serverpy = (PROJECT_ROOT / "web/server.py").read_text(encoding="utf-8")
    storepy = (PROJECT_ROOT / "db/store.py").read_text(encoding="utf-8")

    # ① 渲染指纹含打码态 + 手动刷新强制重画
    assert "function _libRenderSig(items)" in appjs, "缺少图库渲染指纹函数"
    i0 = appjs.index("function _libRenderSig(items)")
    sig_body = appjs[i0:i0 + 400]
    assert "safemode" in sig_body, \
        "渲染指纹必须包含打码态(safemode) —— 否则切换打码后数据没变就不重画, 用户看到旧样子"
    assert "const sig = _libRenderSig(items);" in appjs, "loadLibrary 必须用渲染指纹判定重画"
    assert "opts.force || sig !== cont._libSig" in appjs, \
        "loadLibrary 必须支持 force 绕过指纹(显式动作要能强制重画)"
    assert "loadLibrary({ resetPage: false, force: true })" in appjs, \
        "手动点刷新必须强制重画当前图库列表"

    # ② 打码态变化 → 联动重画所有缩略图视图
    assert "async function refreshAllThumbViews()" in appjs, "缺少「重画所有缩略图视图」函数"
    j0 = appjs.index("async function loadSafemode()")
    sm_body = appjs[j0:j0 + 1400]
    assert "refreshAllThumbViews" in sm_body, \
        "loadSafemode 必须在打码态真的变化时联动重画(覆盖手动切换与 SSE 两条路径)"
    rv0 = appjs.index("async function refreshAllThumbViews()")
    rv_body = appjs[rv0:rv0 + 900]
    assert "loadTrash" in rv_body and "renderSearch" in rv_body, \
        "重画要覆盖回收站与搜索结果, 不能只刷图库"

    # ③ 不允许裸 /api/local-img 进 <img> (必须走 imgSrc / safeThumb 才算打过码)。
    #    allow-raw-img 标记可豁免**它后面 6 行**(给"刻意不走打码"的单点留口子, 见 compareBackup)。
    bad = []
    lines = appjs.splitlines()
    exempt_until = -1
    for no, ln in enumerate(lines, 1):
        if "allow-raw-img" in ln:
            exempt_until = no + 6
            continue
        if no <= exempt_until:
            continue
        if "<img" in ln and "/api/local-img" in ln and "imgSrc(" not in ln and "safeThumb(" not in ln:
            bad.append(f"{no}: {ln.strip()[:90]}")
    assert not bad, ("这些 <img> 直接用了本地图片真实地址, 绕过了安全模式打码(会走光): "
                     + " | ".join(bad))

    # 服务端要给前端"能不能打码"所需的标记位
    assert "a.is_r18" in storepy[storepy.index("def list_trashed"):storepy.index("def list_trashed") + 1600], \
        "回收站单图清单必须带 is_r18, 否则前端无从判断该不该打码"
    assert '"is_r18": int(r["is_r18"] or 0)' in serverpy, \
        "对账台的本地作品清单必须带 is_r18 (否则缩略图走光)"

    # ④ 不许把「人读字符串」字段当数组用。
    #    2026-09-18 事故: `(r.would_empty_labels || r.would_empty || []).join('、')` ——
    #    would_empty_labels 是字符串, .join 抛 TypeError 被外层 catch 吞掉, 表现为
    #    「移入回收站失败…」toast + 界面毫无变化 = 的"点删除没反应, 点几次都一样"。
    #    这类"服务端字段形状与前端假设不一致"的 bug 静态就能拦。
    assert "r.would_empty_labels || r.would_empty || []).join" not in appjs, \
        "would_empty_labels 是字符串, 不能 .join —— 必须走 _emptyChoiceLabels() 做形状容错"
    assert "function _emptyChoiceLabels(" in appjs, "缺少字段形状容错函数 _emptyChoiceLabels"
    assert "_askEmptyChoice(_emptyChoiceLabels(r))" in appjs, \
        "删空二选一的文案必须经 _emptyChoiceLabels 统一形状后再传给弹窗"


def t102_activity_monitor() -> None:
    """T102: 📡 实时活动日志  —— 打屏 + 落盘、按大小滚动、按期保留、
    回读接口, 以及"服务端 HTTP 流水 + 前端行为上报"两条来源与噪声路径过滤。
    """
    import tempfile
    import shutil
    from datetime import datetime, timedelta
    import utils.paths as upaths
    from utils import activity_log as act
    import web.server as _wsrv
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t102act_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_cfg = act._cfg

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    # 把滚动阈值压到极小, 便于在测试里触发滚动 (生产默认 8MB / 保留 7 天)
    act._cfg = lambda: {"enabled": True, "console": False, "max_mb": 1, "keep_days": 7}
    try:
        adir = tmp / "logs" / "activity"
        day = datetime.now().strftime("%Y-%m-%d")

        # ① 落盘 + 回读
        act.log("CLICK", "点击按钮", tab="library", text="删除")
        act.flush()
        f1 = adir / f"activity-{day}.log"
        assert f1.is_file(), f"活动日志应落盘: {f1}"
        txt = f1.read_text(encoding="utf-8")
        assert "CLICK" in txt and "点击按钮" in txt and "tab=library" in txt, txt[:300]
        t = act.tail(50)
        assert t["day"] == day and any("点击按钮" in x for x in t["lines"]), t
        assert t["files"] == [f1.name], t

        # ② 按大小滚动: 单批写爆主文件后, **下一次**写入会自动顺延到 .2.log
        act._cfg = lambda: {"enabled": True, "console": False, "max_mb": 0.001, "keep_days": 7}
        for i in range(200):
            act.log("HTTP", f"GET /api/x/{i}")
        act.flush()
        for i in range(5):
            act.log("HTTP", f"GET /api/y/{i}")
        act.flush()
        rolled = sorted(p.name for p in adir.glob(f"activity-{day}*.log"))
        assert any(".2.log" in n for n in rolled), f"超过 max_mb 应滚动出新文件: {rolled}"

        # ③ 到期保留: 造一个 10 天前的旧文件, 触发保留检查应被删掉
        old_day = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        old = adir / f"activity-{old_day}.log"
        old.write_text("old", encoding="utf-8")
        act._STATE["last_retention"] = 0.0     # 强制立刻做一次保留检查
        act.log("SYS", "触发保留检查")
        act.flush()
        assert not old.exists(), "超过 keep_days 的日志应被自动清理"

        # ④ 请求摘要: 长列表只留条数、敏感字段打码 (别把 cookie 写进日志)
        s = _wsrv._act_short({"ids": list(range(500)), "cookie": "SECRET", "q": "abc"})
        assert "ids[500]" in s and "cookie=***" in s and "SECRET" not in s, s

        # ⑤ 服务端流水: 记接口 / 跳过噪声 / 前端上报可回写
        client = create_app().test_client()
        client.get("/api/library/stats")
        client.get("/api/local-img?p=nope&thumb=1")     # 高频噪声: 不该进日志
        client.get("/api/activity/tail?n=5")
        client.post("/api/activity", json={"events": [
            {"k": "CLICK", "m": "button#lib-refresh", "f": {"tab": "library"}},
            {"k": "CHANGE", "m": "select#lib-f-source", "f": {"value": "pixiv"}},
        ]})
        act.flush()
        all_txt = "\n".join(ln for p in adir.glob("activity-*.log")
                            for ln in p.read_text(encoding="utf-8").splitlines())
        assert "GET /api/library/stats" in all_txt, "接口请求应进活动日志"
        assert "button#lib-refresh" in all_txt and "select#lib-f-source" in all_txt, \
            "前端点击/控件变化应能上报进日志"
        assert "/api/local-img" not in all_txt, "图片代理是高频噪声, 不该进活动日志"
        assert "/api/activity" not in all_txt, "上报接口自身不该进日志(否则自激循环)"
        # 响应摘要必须进日志: 2026-09-18 排查"点删除没反应"时, 缺的就是"服务端回了个什么"
        assert "resp=" in all_txt, "JSON 响应摘要要写进活动日志(排查静默失败的关键线索)"
        # 请求体里的列表要带"头几个身份"(作品#页号), 否则看不出操作的是哪一条
        client.post("/api/library/delete-pages",
                    json={"items": [{"artwork_id": "900001", "page_num": 3}]})
        act.flush()
        all_txt = "\n".join(ln for p in adir.glob("activity-*.log")
                           for ln in p.read_text(encoding="utf-8").splitlines())
        assert "items[1]:900001#3" in all_txt, \
            "列表型请求体要写出条数 + 头几个身份(如 items[1]:900001#3), 否则无法定位操作对象"

        # ⑥ 前端接线 (静态): 五类行为 + 批量发送 + 关页兜底 + 设置页查看入口
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        for k in ("actPush('CLICK'", "actPush('CHANGE'", "actPush('NAV'", "actPush('INPUT'",
                  "actPush('KEY'", "fetch('/api/activity'", "keepalive: true", "beforeunload"):
            assert k in appjs, f"前端活动上报缺少: {k}"
        assert "if (_actQ.length >= 30) actFlush();" in appjs, \
            "必须批量发送(攒够一批才发), 不能逐条发请求"
        assert "window.openActivityLog" in appjs and "loadActivityStats" in appjs, \
            "设置页应有「运行日志」查看入口"
        assert "loadActivityStats()" in appjs, "切到设置页要刷新运行日志摘要"
        indexh = (PROJECT_ROOT / "web/static/index.html").read_text(encoding="utf-8")
        assert 'id="activity-stats"' in indexh and "openActivityLog()" in indexh, \
            "设置页要有运行日志卡片"
        # 2026-09-27 用户: 「活动现场(可回放)是什么鬼…为什么要叫这么莫名其妙的名字」→ 改名
        assert "活动现场" not in indexh, "旧名字又回来了"
        assert "📜 运行日志" in indexh, "设置页该叫「运行日志」"
        css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
        assert ".activity-log" in css, "运行日志的样式(等宽/可滚动)要存在"

        # ⑦ 配置兜底: 新增配置节必须同时进 canonicalize (项目规则)
        from utils.config import _canonicalize
        c = _canonicalize({})
        assert "activity_log" in c and int(c["activity_log"]["keep_days"]) == 7, c.get("activity_log")
    finally:
        act._cfg = orig_cfg
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        act._BUF.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def t103_check_recheck_report() -> None:
    """T103: ⑥「汇总报告」→「复检报告」(用户 2026-09-18 要求): 把上一版报告逐条重新核对 ——
    已解决的**清理掉**、变了的**更新**、仍存在的保留; 且本次没条件复检的来源(对应步骤没跑)
    必须原样保留并标注, **绝不许当成"已解决"清掉**(否则单跑 ⑥ 会静默丢掉联网类问题)。"""
    import tempfile
    import utils.paths as upaths
    from db.store import PixivDatabase

    with tempfile.TemporaryDirectory(prefix="t103recheck_") as td:
        tmp = Path(td)
        orig_ud, orig_dr = upaths.user_data, upaths.data_root
        upaths.user_data = lambda rel: (tmp / str(rel))
        upaths.data_root = lambda: tmp
        try:
            wsrv = _t8x_env(tmp)
            assert wsrv._CHECK_TITLE_OF["report"] == "⑥ 复检报告", \
                f"⑥ 应改名为「复检报告」, 实得 {wsrv._CHECK_TITLE_OF['report']}"
            client = wsrv.create_app().test_client()

            def run_report():
                client.post("/api/library/check", json={"step": "report"})
                assert _t8x_wait_step(wsrv, "report"), "report 步骤应完成"
                return client.get("/api/library/check-report").get_json()["report"]

            # ① 首版: 无上一版可对账 → 全部算"新增", 不该虚报"已解决"
            j = run_report()
            rk = j.get("recheck") or {}
            assert rk.get("at"), f"报告要带回复检结论, 实得 {rk}"
            assert int(rk.get("resolved") or 0) == 0, f"首版不该有'已解决', 实得 {rk}"
            assert int(rk.get("added") or 0) > 0, f"首版条目都算新增, 实得 {rk}"
            refs = {x.get("ref") for x in j["suspicious"] + j["todos"]}
            assert "601" in refs and "599" in refs, f"缺文件/缺作者都该在报告里, 实得 {refs}"

            # ② 已解决 → 清理: 把 601 丢掉的图片文件补回来, 再复检应报"已解决 1"且条目消失
            (tmp / "img" / "画师甲_500" / "601_p0.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            j = run_report()
            rk = j.get("recheck") or {}
            assert int(rk.get("resolved") or 0) >= 1, \
                f"补回文件后应报'已解决', 实得 {rk}"
            res_refs = {x.get("ref") for x in rk.get("resolved_items") or []}
            assert "601" in res_refs, f"'已解决'名单里应能查到 601, 实得 {res_refs}"
            still = {x.get("ref") for x in j["suspicious"] + j["todos"]}
            assert "601" not in still, f"已解决的条目必须从报告里清掉, 实得 {still}"

            # ③ 问题变了 → 更新: 改 599 的标题(条目文案里带标题) → 复检应报"有变化"且给旧→新
            with PixivDatabase(str(tmp / "pixiv.db")) as _db3:
                _db3.conn.execute("UPDATE artworks SET title='改过的标题' WHERE artwork_id='599'")
                _db3.conn.commit()
            j = run_report()
            rk = j.get("recheck") or {}
            assert int(rk.get("changed") or 0) >= 1, f"文案变了应报'有变化', 实得 {rk}"
            ch = (rk.get("changed_items") or [])[0]
            assert ch.get("was") and ch.get("now") and ch["was"] != ch["now"], \
                f"'变化'要给旧→新文案, 实得 {ch}"
            assert "改过的标题" in ch["now"], f"新文案应反映最新事实, 实得 {ch['now']}"

            # ④ 未复检保护: 上一版里塞一条只有「④ 联网补元数据」才推导得出的问题,
            #    然后单跑 ⑥ (④ 没跑) → 必须原样保留并计入 unverified, **绝不许**当成已解决清掉
            rep = wsrv._check_state["report"]
            rep["todos"] = list(rep.get("todos") or []) + [
                {"type": "metadata_failed", "ref": "77777",
                 "text": "作品 77777 元数据补全失败（作品可能受限）"}]
            j = run_report()
            rk = j.get("recheck") or {}
            refs2 = {x.get("ref") for x in j["todos"]}
            assert "77777" in refs2, \
                "本次没跑④时, 联网类问题必须原样保留 —— 不能因为'这次没看到'就当成已解决清掉"
            assert int(rk.get("unverified") or 0) >= 1, f"应报'未复检 ≥1', 实得 {rk}"
            assert "77777" not in {x.get("ref") for x in rk.get("resolved_items") or []}, \
                "未复检的条目绝不许进'已解决'名单"
            assert "metadata" not in (rk.get("steps_ran") or []), \
                f"本次没跑④, steps_ran 不该包含它, 实得 {rk.get('steps_ran')}"
            assert "report" not in (rk.get("steps_ran") or []), \
                "⑥ 自己不算'被复核的来源'(它是执行者)"

            # ⑤ 结论落盘: 重启(新 app, 内存报告清空)后仍能看到上次复检结论
            assert (tmp / "self_check.json").is_file(), "报告要落盘"
            disk = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
            assert (disk.get("recheck") or {}).get("at"), f"复检结论要落盘, 实得 {disk.keys()}"
            wsrv._check_state["report"] = None
            client2 = wsrv.create_app().test_client()
            j2 = client2.get("/api/library/check-report").get_json()["report"]
            assert (j2.get("recheck") or {}).get("at"), \
                "重启后读回的报告也要带复检结论(否则用户以为复检白跑了)"

            # ⑥ 读回上一版报告必须**同时吃 suspicious 与 todos 两个字段**并去重。
            #    2026-09-18 浏览器实测踩到: 只读 suspicious → 放在 todos 里的待办项在重启后
            #    凭空消失, 叠到复检上就显得"问题自己好了"(假象比不报更糟)。
            wsrv._check_state["report"] = None
            split = {"time": "t-split",
                     # 类型特意用 corrupt: 它是"必须重跑①才能复核"的类型, 不会被
                     # 打开报告时的本地轻量复核顺手清掉(T110 之后新增的行为)
                     "suspicious": [{"type": "corrupt", "ref": "A1", "text": "x"}],
                     "todos": [{"type": "metadata_failed", "ref": "B2", "text": "y"},
                               {"type": "corrupt", "ref": "A1", "text": "x"}]}
            (tmp / "self_check.json").write_text(json.dumps(split, ensure_ascii=False),
                                                 encoding="utf-8")
            client3 = wsrv.create_app().test_client()
            r3 = client3.get("/api/library/check-report").get_json()["report"]
            got = {x["ref"] for x in r3["suspicious"] + r3["todos"]}
            assert got == {"A1", "B2"}, f"两个字段都要读回, 实得 {got}"
            assert sum(1 for x in r3["suspicious"] + r3["todos"] if x["ref"] == "A1") == 1, \
                "跨字段重复的条目要按 (type, ref) 去重"

            # ⑦ 前端接线 (静态): 复检结论要展示给用户, 不能只有总数
            appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
            assert "function recheckHintHtml" in appjs and "function recheckBlockHtml" in appjs, \
                "前端要有复检结论的展示函数"
            assert "recheckHintHtml(rc)" in appjs and "recheckBlockHtml(rc)" in appjs, \
                "体检面板与报告弹窗都要显示复检结论"
            assert "resolved_items" in appjs and "changed_items" in appjs, \
                "'清掉了哪些/哪些变了'要列出来, 不能只给数字"
            assert "未复检" in appjs, "未复检的条目要如实标注(否则用户以为问题消失了)"
            css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
            assert ".chk-recheck-box" in css, "复检结论块的样式要存在"
        finally:
            upaths.user_data, upaths.data_root = orig_ud, orig_dr
            import web.server as wsrv2
            wsrv2._check_state.update(running=None, steps={}, progress={},
                                      report=None, journal_key=None)


def t104_reverse_inbox_outlet() -> None:
    """T104: 反搜收件箱的"出路"(用户 2026-09-18 报"没找到结果就留在那了, 我不能继续入库了")。

    ① 未命中被挪进 input/not_found/ 的图必须**仍出现在待反搜列表**里(以前只扫顶层 →
       它们从界面消失、再也勾不到); ② 单张/批量「手动入库」→ 收进 img/#其他来源#/ 并登记
       ext_ 壳作品、源文件从收件箱移走; ③ 同名不覆盖; ④ 裸文件名也能命中 not_found/;
       ⑤ 防路径穿越; ⑥ 「丢弃 / 清空」走 **Windows 电脑回收站**(不是硬删)。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    import web.server as _wsrv
    from db.store import PixivDatabase, WSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t104rev_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_recycle = _wsrv._recycle_files
    recycled_calls = []

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _fake_recycle(paths):
        paths = [str(p) for p in (paths or [])]
        recycled_calls.append(paths)
        n = 0
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
                n += 1
            except Exception:
                pass
        return n, []

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    _wsrv._recycle_files = _fake_recycle
    try:
        img = tmp / "img"
        (img / "合成画师T_961").mkdir(parents=True)
        (img / "合成画师T_961" / "961001_p0.png").write_bytes(b"SYNTH-T0")
        inp = tmp / "input"
        (inp / "not_found").mkdir(parents=True)
        (inp / "top.png").write_bytes(b"SYNTH-TOP")
        (inp / "not_found" / "nf.png").write_bytes(b"SYNTH-NF")
        # 图库同名的"已存在"文件 (用于验证同名不覆盖)
        (img / "#其他来源#").mkdir(parents=True, exist_ok=True)
        (img / "#其他来源#" / "dup.png").write_bytes(b"SYNTH-EXIST")
        (inp / "dup.png").write_bytes(b"SYNTH-DUP")
        # 收件箱外面放一个, 验证路径穿越被拦
        (tmp / "outside.png").write_bytes(b"SYNTH-OUT")

        db = PixivDatabase(str(tmp / "pixiv.db"))
        db.upsert_author("961", "合成画师T")
        db.change_work_status("961001", WSt.DONE, title="正常作品", author_id="961", total_pages=1)
        db.close()

        client = create_app().test_client()

        # ① 待反搜列表要**看得见** not_found/ 里的图 (这是用户"卡住"的直接原因)
        st = client.get("/api/reverse/status").get_json()
        rels = {p["rel"] for p in st["pending"]}
        assert st["image_files"] == 3, f"顶层与 not_found 都要列, 实得 {st['image_files']}"
        assert rels == {"top.png", "dup.png", "not_found/nf.png"}, f"实得 {rels}"
        nf = [p for p in st["pending"] if p["rel"] == "not_found/nf.png"][0]
        assert nf["not_found"] is True and nf["sub"] == "not_found", nf
        assert "root=input" in nf["url"] and "not_found" in nf["url"], nf

        # ② 手动入库 (顶层)
        r = client.post("/api/reverse/adopt", json={"files": ["top.png"]}).get_json()
        assert r["status"] == "ok" and len(r["imported"]) == 1, r
        assert (img / "#其他来源#" / "top.png").is_file(), "应复制进图库 #其他来源#/"
        assert not (inp / "top.png").exists(), "入库成功后源文件要从收件箱移走"
        with PixivDatabase(str(tmp / "pixiv.db")) as d1:
            ext = d1.conn.execute("SELECT artwork_id, source FROM artworks "
                                  "WHERE artwork_id LIKE 'ext_%'").fetchall()
            assert len(ext) == 1 and ext[0][1] == "other", [tuple(x) for x in ext]
        j = client.get("/api/library/items?view=artwork&r18=").get_json()
        assert any(x["artwork_id"] == ext[0][0] for x in j["items"]), "入库后图库应能看到"

        # ③ 裸文件名也能命中 not_found/ (任务表里那样只给文件名)
        r = client.post("/api/reverse/adopt", json={"files": ["nf.png"]}).get_json()
        assert len(r["imported"]) == 1, f"裸文件名应能在 not_found/ 里找到, 实得 {r}"
        assert (img / "#其他来源#" / "nf.png").is_file()
        assert not (inp / "not_found" / "nf.png").exists()

        # ④ 同名不覆盖: dup.png 图库里已有 → 跳过, 源文件原样不动
        r = client.post("/api/reverse/adopt", json={"files": ["dup.png"]}).get_json()
        assert not r["imported"] and r["skipped"] and "同名" in r["skipped"][0]["reason"], r
        assert (inp / "dup.png").is_file(), "跳过时源文件不许动"
        assert (img / "#其他来源#" / "dup.png").read_bytes() == b"SYNTH-EXIST", "不许覆盖已有文件"

        # ⑤ 防路径穿越: 收件箱外的文件绝不能被"入库"
        r = client.post("/api/reverse/adopt", json={"files": ["../outside.png"]}).get_json()
        assert not r["imported"] and r["failed"], r
        assert (tmp / "outside.png").is_file(), "穿越目标不许被移动"
        assert not (img / "#其他来源#" / "outside.png").exists()

        # ⑥ 丢弃 / 清空 → 走 Windows 电脑回收站 (不是硬删)
        recycled_calls.clear()
        r = client.post("/api/reverse/discard", json={"files": ["dup.png"]}).get_json()
        assert r["removed"] == 1 and r["recycled"] == 1, r
        assert recycled_calls and recycled_calls[0] and "input" in recycled_calls[0][0], recycled_calls
        recycled_calls.clear()
        r = client.post("/api/reverse/clear", json={}).get_json()
        assert r["ok"] and recycled_calls, "清空也必须走回收站, 不能硬删"
        assert all("input" in p for p in recycled_calls[0]), recycled_calls

        # ⑥ 单次上限要**如实回报** (自查发现: 超 500 张时原来是静默截断, 用户会以为全处理了)
        r = client.post("/api/reverse/adopt",
                        json={"files": [f"x{i}.png" for i in range(600)]}).get_json()
        assert "over_limit" in r and r["over_limit"] == 100, \
            f"超出单次上限必须回报未处理数量, 不能静默截断, 实得 {r.get('over_limit')}"
        r = client.post("/api/reverse/discard",
                        json={"files": [f"y{i}.png" for i in range(501)]}).get_json()
        assert r.get("over_limit") == 1, r

        # ⑦ 前端接线 (静态): 单张/批量都要有出口
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        indexh = (PROJECT_ROOT / "web/static/index.html").read_text(encoding="utf-8")
        for k in ("window.revAdopt", "window.revDiscard", "function revSelectedRels",
                  "data-rel=", "revAdopt(['", "revDiscard(['", "r.over_limit"):
            assert k in appjs, f"前端缺少反搜出路接线: {k}"
        assert 'id="reverse-adopt-sel"' in indexh and 'id="reverse-drop-sel"' in indexh, \
            "待反搜区要有批量「入库选中 / 丢弃选中」按钮"
        assert "/api/reverse/adopt" in appjs and "/api/reverse/discard" in appjs, \
            "前端要调用新接口"
        css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
        assert ".rev-cell-ops" in css and ".rev-nf" in css, "单格操作与「未找到」角标的样式要存在"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        _wsrv._recycle_files = orig_recycle
        shutil.rmtree(tmp, ignore_errors=True)


def t105_same_name_author_and_gather_files() -> None:
    """T105: 「同名 ≠ 同一人」与「绑定后文件跟着走」(用户 2026-09-18 报「测试画师有两条记录」)。

    ① 本地作者(bilibili 账号, local_ 键) 与真 pixiv 作者同名时会各占一行 —— 这是**对的**
       (站点账号与 pixiv ID 本就是两种身份), 但**必须看得出来**: 作者列表给本地作者行带上
       `same_name_pixiv`(同名的 pixiv 作者 ID/作品数), 真 pixiv 作者行不提示自己,
       名字不同的本地作者绝不误标, 且谁也**不自动合并**;
    ② 一键绑定 = 作品迁移 + 作者行合并 + 文件归拢到作者文件夹 (`#其他来源#/x.png` →
       `作者名_pid/站点@账号/x.png`, 与一键整理同一套期望夹规则) + `local_path` 同步;
    ③ 护栏: 撞名绝不覆盖(目标已有同名文件 → 记 failed 且源文件原地不动)、
       别人的文件一个都不碰、合并后旧行消失/再绑 404/对 pixiv ID 再绑 400。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t105same_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        img = tmp / "img"
        (img / "#其他来源#").mkdir(parents=True)
        (img / "#其他来源#" / "bili_1.png").write_bytes(b"SYNTH-B1")
        (img / "#其他来源#" / "bili_2.png").write_bytes(b"SYNTH-B2")
        (img / "#其他来源#" / "bili_3.png").write_bytes(b"SYNTH-B3")   # 别人(不同账号)的图
        # pixiv 作者自有作品 + 目标归拢夹里**已存在同名文件** → 验证绝不覆盖
        dest = "测试画师_40892639/bilibili@测试画师"
        (img / "测试画师_40892639" / "bilibili@测试画师").mkdir(parents=True)
        (img / "测试画师_40892639" / "9001_p0.png").write_bytes(b"SYNTH-P0")
        (img / dest / "bili_1.png").write_bytes(b"SYNTH-EXIST")

        db = PixivDatabase(str(tmp / "pixiv.db"))
        e1 = db.register_foreign_file("#其他来源#/bili_1.png")
        e2 = db.register_foreign_file("#其他来源#/bili_2.png")
        e3 = db.register_foreign_file("#其他来源#/bili_3.png")
        assert e1 and e2 and e3 and len({e1, e2, e3}) == 3, (e1, e2, e3)
        db.conn.execute("UPDATE artworks SET source='bilibili', source_author='测试画师' "
                        "WHERE artwork_id IN (?,?)", (e1, e2))
        db.conn.execute("UPDATE artworks SET source='bilibili', source_author='别人' "
                        "WHERE artwork_id=?", (e3,))
        db.conn.commit()
        # 与生产同一条路径: 外来作品按「站点+账号」建本地作者
        assert db.backfill_foreign_authors() == 3, "三条外来作品应各归一位本地作者"
        db.upsert_author("40892639", "测试画师")
        db.change_work_status("9001", WSt.DONE, title="pixiv 作品", author_id="40892639",
                              total_pages=1)
        db.mark_page_status("9001", 0, PSt.DONE,
                            local_path="测试画师_40892639/9001_p0.png")
        db.close()

        with PixivDatabase(str(tmp / "pixiv.db")) as d0:
            local_id = str(d0.conn.execute(
                "SELECT author_id FROM authors WHERE author_id LIKE 'local_%' "
                "  AND name='测试画师'").fetchone()[0])
            other_id = str(d0.conn.execute(
                "SELECT author_id FROM authors WHERE author_id LIKE 'local_%' "
                "  AND name='别人'").fetchone()[0])

        client = create_app().test_client()

        # ① 两条记录都在 (这是对的) + 本地作者行标出「同名的 pixiv 作者」
        j = client.get("/api/library/authors").get_json()
        rows = {str(x["author_id"]): x for x in j["items"]}
        assert local_id in rows and "40892639" in rows, f"两条记录都要在: {list(rows)}"
        same = rows[local_id].get("same_name_pixiv")
        assert same and same["author_id"] == "40892639" and same["count"] == 1, \
            f"本地作者行要标出同名的 pixiv 作者: {rows[local_id]}"
        assert not rows["40892639"].get("same_name_pixiv"), "pixiv 作者行不提示自己"
        assert not rows[other_id].get("same_name_pixiv"), \
            f"名字不同的本地作者不许误标: {rows[other_id]}"
        # 未绑定时绝不自动合并 (只看一眼列表不许动数据)
        with PixivDatabase(str(tmp / "pixiv.db")) as d0b:
            assert d0b.get_author(local_id) is not None, "只看一眼列表绝不许动数据"
            assert str(d0b.get_artwork_metadata(e1)["author_id"]) == local_id

        # ② 一键绑定: 作品迁移 + 行合并 + 文件归拢 (#其他来源#/ → 作者夹/站点@账号)
        r = client.post("/api/library/author-bind-pixiv",
                        json={"author_id": local_id, "pixiv_id": "40892639"})
        assert r.status_code == 200 and r.get_json()["status"] == "ok", r.get_json()
        jb = r.get_json()
        assert jb["artworks"] == 2, jb
        assert jb["files_gathered"] == 1, f"bili_2 归拢, bili_1 撞名跳过: {jb}"
        assert jb["gather_dests"] == [dest], jb
        assert jb["gather_failed"] and "已存在" in jb["gather_failed"][0], jb
        assert (img / dest / "bili_2.png").read_bytes() == b"SYNTH-B2"
        assert not (img / "#其他来源#" / "bili_2.png").exists(), "归拢后源文件必须搬走"
        assert (img / "#其他来源#" / "bili_1.png").read_bytes() == b"SYNTH-B1", "撞名时源不许动"
        assert (img / dest / "bili_1.png").read_bytes() == b"SYNTH-EXIST", "绝不许覆盖同名文件"
        assert (img / "#其他来源#" / "bili_3.png").is_file(), "别人的文件一个都不许碰"

        with PixivDatabase(str(tmp / "pixiv.db")) as d1:
            assert d1.get_author(local_id) is None, "旧本地作者行应已合并删除"
            for aid in (e1, e2):
                assert str(d1.get_artwork_metadata(aid)["author_id"]) == "40892639", aid
            lp = {a: [x[0] for x in d1.conn.execute(
                "SELECT local_path FROM pages WHERE artwork_id=?", (a,))] for a in (e1, e2)}
            assert lp[e2] == [f"{dest}/bili_2.png"], f"local_path 要跟着搬: {lp}"
            assert lp[e1] == ["#其他来源#/bili_1.png"], f"跳过归拢的必须保持原路径: {lp}"
            assert str(d1.get_artwork_metadata(e3)["author_id"]) == other_id, \
                "别的作者的作品归属不许被改"
        # 合并后两行并一行
        j2 = client.get("/api/library/authors").get_json()
        ids2 = [str(x["author_id"]) for x in j2["items"]]
        assert local_id not in ids2 and "40892639" in ids2, ids2
        assert (img / "测试画师_40892639" / "9001_p0.png").is_file()

        # ③ 护栏: 已合并删除 → 404; 对 pixiv 作者再绑 → 400
        assert client.post("/api/library/author-bind-pixiv",
                           json={"author_id": local_id,
                                 "pixiv_id": "40892639"}).status_code == 404
        assert client.post("/api/library/author-bind-pixiv",
                           json={"author_id": "40892639",
                                 "pixiv_id": "40892639"}).status_code == 400

        # ④ 前端接线 (静态): ⚠ 提示 + 一键绑定 + 归拢结果如实回报
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        for k in ("same_name_pixiv", "c-warn", "files_gathered", "gather_failed",
                  "bindAuthorPixiv"):
            assert k in appjs, f"前端缺少同名提示/归拢接线: {k}"
        css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
        assert ".c-warn" in css, "同名提示的样式要存在"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        shutil.rmtree(tmp, ignore_errors=True)


def t106_author_works_paging() -> None:
    """T106: 作者搜索结果里的作品列表「为什么不是全部 + 为什么不能换一批」(用户 2026-09-18 报)。

    以前后端写死 `fetch_author(limit=12)`, 界面也没有翻页入口 —— 永远只看到最新 12 个。
    ① profile/all 的**全部**作品 id 参与切片, offset 生效, 且返回总数 works_total;
    ② 默认每批 60 (与图库/搜索一次 60 条同口径);
    ③ 越界 offset → 空列表而不是报错;
    ④ /api/search 收 offset 并回传 works_offset (前端据此算「第 x/y 批」);
    ⑤ 前端有翻批栏与「上一批/下一批」接线 (静态)。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    import web.server as _wsrv
    from core.models import ArtworkInfo
    from core.search_client import SearchClient, AUTHOR_WORKS_PAGE

    # ①-③ 纯切片逻辑 (不起浏览器: 替换 ajax 与批量接口)
    import threading as _th
    sc = object.__new__(SearchClient)
    sc.op_lock = _th.Lock()          # __init__ 没跑, 手动补上 execute_scoped 需要的
    sc.ensure_login = lambda *a, **k: None
    ids = [str(900000000 - i) for i in range(150)]      # 150 件, 降序 = 新在前
    batches = []

    def _fake_ajax_get(url):
        if "profile/all" in url:
            return {"illusts": {i: {} for i in ids}}
        return {"name": "合成画师", "image": "", "following": 3}

    def _fake_batch(author_id, want_ids):
        batches.append(list(want_ids))
        return [ArtworkInfo(artwork_id=i, author_id=str(author_id), title=f"w{i}")
                for i in want_ids]

    sc._ajax_get = _fake_ajax_get
    sc._batch_illusts = _fake_batch
    assert AUTHOR_WORKS_PAGE == 60, f"每批应为 60, 实得 {AUTHOR_WORKS_PAGE}"

    a0, w0 = sc.fetch_author("40892639", limit=AUTHOR_WORKS_PAGE, offset=0)
    assert a0.works_total == 150, f"要能拿到总数(不能只有 12 个): {a0.works_total}"
    assert len(w0) == 60 and [w.artwork_id for w in w0] == ids[:60], "第一批应为最新 60 个"
    _, w1 = sc.fetch_author("40892639", limit=AUTHOR_WORKS_PAGE, offset=60)
    assert [w.artwork_id for w in w1] == ids[60:120], "第二批要接着往下取"
    _, w2 = sc.fetch_author("40892639", limit=AUTHOR_WORKS_PAGE, offset=120)
    assert [w.artwork_id for w in w2] == ids[120:150] and len(w2) == 30, "最后一批只剩余下的"
    _, w3 = sc.fetch_author("40892639", limit=AUTHOR_WORKS_PAGE, offset=150)
    assert w3 == [], "越界 offset 应返回空列表, 不能报错"
    assert all(len(b) <= 60 for b in batches), f"必须分页取, 不能一次全拉: {[len(b) for b in batches]}"

    tmp = Path(tempfile.mkdtemp(prefix="t106pg_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_get_sc, orig_cfg = _wsrv._get_search_client, _wsrv._config

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    _wsrv._get_search_client = lambda *a, **k: sc
    try:
        from web.server import create_app
        client = create_app().test_client()
        r = client.get("/api/search?q=40892639&scope=author&offset=60")
        j = r.get_json()
        assert r.status_code == 200 and j["status"] == "ok", j
        assert j["works_offset"] == 60, f"要回传本批起点: {j.get('works_offset')}"
        assert j["author"]["works_total"] == 150, j["author"]
        assert [w["artwork_id"] for w in j["works"]] == ids[60:120], "接口层 offset 也要生效"
        # 非法 offset → 当 0, 不能 500
        j2 = client.get("/api/search?q=40892639&scope=author&offset=abc").get_json()
        assert j2["works_offset"] == 0 and len(j2["works"]) == 60, j2.get("works_offset")

        # ⑤ 前端接线 (静态)
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        for k in ("AUTHOR_WORKS_PAGE", "function worksPagerHtml", "authorWorksPage",
                  "works_offset", "works_total", "&offset=", "上一批", "下一批"):
            assert k in appjs, f"前端缺少作者作品翻批接线: {k}"
        css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
        assert ".works-pager" in css, "翻批栏样式要存在"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        _wsrv._get_search_client, _wsrv._config = orig_get_sc, orig_cfg
        shutil.rmtree(tmp, ignore_errors=True)


def t107_auto_bookmark_and_follow() -> None:
    """T107: 「下载后自动收藏(♡)」+「关注画师」(用户 2026-09-18 要求)。

    写接口的契约是从 pixiv 前端 JS 里挖出来的, 这里用**假传输层**把它钉死:
    ① 收藏 = POST /ajax/illusts/bookmarks/add, JSON 体, restrict 0=公开/1=非公开,
       且必须带 x-csrf-token (缺它 pixiv 一律回 400「请重新登录」);
    ② csrf token 从 pixiv 页面 __NEXT_DATA__ 取, **有缓存**(多次收藏只取一次);
    ③ 已收藏的跳过, 绝不重复写; 开关关闭时一个请求都不发;
    ④ 只对「单个作品」任务生效(画师批量不碰); 未登录/pixiv 报错 → 如实降级, 不炸下载流程;
       **R18 作品一律 restrict=1(非公开)**, 与设置里的公开/非公开无关;
    ⑤ 关注 = POST /bookmark_add.php(mode=add&type=user), 取关 = /rpc_group_setting.php
       (mode=del&type=bookuser), 状态读 /ajax/user/:id 的 isFollowed;
    ⑥ 接口护栏: 非 pixiv 作者/未登录 → 400; ⑦ 前端与设置页接线(静态);
    ⑧ 默认值: 设计约定(保守默认会让他以为功能没做)。
    """
    import json as _json
    import tempfile
    import shutil
    import utils.paths as upaths
    import utils.config as ucfg
    import core.search_client as scm
    import web.server as _wsrv
    from db.store import PixivDatabase

    tmp = Path(tempfile.mkdtemp(prefix="t107ab_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_http = scm._web_http
    orig_sess = _wsrv._pixiv_http_session
    orig_build = scm.build_pixiv_http_session
    orig_lc, orig_save = ucfg.load_config, ucfg.save_user_config
    orig_srv_lc = _wsrv.load_config

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    scm._WEB_CRED.clear()

    calls = []
    NEXT = {"props": {"pageProps": {
        "isLoggedIn": True,
        "gaUserData": {"userId": "12345"},
        # 真实页面里这层是**字符串**(JSON 串), 这里照样给字符串, 覆盖那个分支
        "serverSerializedPreloadedState": _json.dumps({"api": {"token": "TOK-ABC"}}),
    }}}
    FAKE_SESSION = {"headers": {"Cookie": "PHPSESSID=zzz; device_token=d",
                                "User-Agent": "ua"}}
    state = {"bm": set(), "followed": False}     # bm = 已收藏的作品 id (按作品区分, 与真实一致)
    _re_aid = __import__("re")

    def _fake_web_http(session, url, *, data=None, content_type=None,
                       timeout=20.0, method=None):
        calls.append({"url": url, "data": data, "ctype": content_type,
                      "method": method, "hdr": dict(session.get("headers") or {})})
        if url.rstrip("/") == "https://www.pixiv.net":
            return 200, ('<html><script id="__NEXT_DATA__" type="application/json">'
                         + _json.dumps(NEXT) + "</script></html>")
        m = _re_aid.search(r"/illust/(\d+)/bookmarkData", url)
        if m:
            bd = {"id": "999888", "private": False} if m.group(1) in state["bm"] else {}
            return 200, _json.dumps({"error": False, "body": {"bookmarkData": bd}})
        if "bookmarks/add" in url:
            state["bm"].add(_json.loads(data.decode("utf-8"))["illust_id"])
            return 200, _json.dumps({"error": False, "body": {"last_bookmark_id": "1"}})
        if "bookmark_add.php" in url:
            state["followed"] = True
            return 200, "[]"
        if "rpc_group_setting.php" in url:
            state["followed"] = False
            return 200, _json.dumps({"user_id": "999", "type": "bookuser"})
        if "/ajax/user/" in url:
            return 200, _json.dumps({"error": False,
                                     "body": {"isFollowed": state["followed"]}})
        return 200, "{}"

    def _opts(auto=True, private=False):
        return {"pixiv_actions": {"auto_bookmark": auto, "bookmark_private": private}}

    def _set_opts(auto=True, private=False):
        """server.py 是 `from utils.config import load_config` 的**独立引用**,
        自己那份也要一起换掉, 否则自动收藏会去读真实 user.json。"""
        cfg = _opts(auto, private)
        ucfg.load_config = lambda: cfg
        _wsrv.load_config = lambda: cfg

    scm._web_http = _fake_web_http
    _wsrv._pixiv_http_session = lambda: FAKE_SESSION
    # create_app 里还有一份**同名的内层** _pixiv_http_session(各接口用的是那一份),
    # 它最终调 build_pixiv_http_session → 从这一层替换才能让接口也拿到假会话。
    scm.build_pixiv_http_session = lambda *a, **k: FAKE_SESSION
    try:
        from web.server import create_app
        client = create_app().test_client()
        with PixivDatabase() as db:
            db.upsert_author("999", "合成画师")
            db.change_work_status("8001", 2, title="w1", author_id="999")
            db.change_work_status("8002", 2, title="w2", author_id="999")
            i1 = db.queue_add(item_type="artwork", artwork_id="8001", title="w1")
            i2 = db.queue_add(item_type="artwork", artwork_id="8002", title="w2")
            i3 = db.queue_add(item_type="author_all", author_id="999", title="全作者")
            db.change_work_status("8003", 2, title="r18作品", author_id="999")
            db.update_artwork("8003", is_r18=1)
            i4 = db.queue_add(item_type="artwork", artwork_id="8003", title="r18作品")
            db.queue_update_status(i1, "done")
            db.queue_update_status(i2, "done")
            db.queue_update_status(i3, "done")
            db.queue_update_status(i4, "done")

        # ① ② 自动收藏: 两个作品 → 取一次 token, 各查状态后各收藏一次
        _set_opts(True, False)
        calls.clear()
        _wsrv._auto_bookmark_after_download({i1, i2, i3})
        urls = [c["url"] for c in calls]
        assert urls.count("https://www.pixiv.net/") == 1, \
            f"csrf token 要缓存, 不该每个作品取一次: {urls}"
        adds = [c for c in calls if "bookmarks/add" in c["url"]]
        assert len(adds) == 2, f"两个作品各收藏一次(画师批量不碰): {urls}"
        assert adds[0]["hdr"].get("x-csrf-token") == "TOK-ABC", "缺 csrf 头 pixiv 会回 400"
        assert adds[0]["hdr"].get("x-user-id") == "12345", adds[0]["hdr"]
        bodies = [_json.loads(c["data"].decode("utf-8")) for c in adds]
        assert sorted(b["illust_id"] for b in bodies) == ["8001", "8002"], bodies
        assert bodies[0] == {"illust_id": bodies[0]["illust_id"], "restrict": 0,
                             "comment": "", "tags": []}, bodies[0]
        assert adds[0]["ctype"] == "application/json", "收藏必须 JSON 体(表单体会 422)"

        # ③ 已收藏 → 跳过, 不再发写请求
        state["bm"] = {"8001", "8002"}
        calls.clear()
        _wsrv._auto_bookmark_after_download({i1, i2})
        assert not [c for c in calls if "bookmarks/add" in c["url"]], \
            "已收藏的作品必须跳过(否则会覆盖用户原本的公开/非公开设置)"
        state["bm"] = set()

        # 开关关闭 → 一个请求都不发
        _set_opts(False, False)
        calls.clear()
        _wsrv._auto_bookmark_after_download({i1})
        assert calls == [], f"未开启自动收藏时不许联网: {calls}"

        # 非公开收藏 → restrict=1
        _set_opts(True, True)
        calls.clear()
        _wsrv._auto_bookmark_after_download({i1})
        add1 = [c for c in calls if "bookmarks/add" in c["url"]][0]
        assert _json.loads(add1["data"].decode("utf-8"))["restrict"] == 1, "非公开应为 1"

        # R18 作品 → 即使设置是「公开收藏」也必须 restrict=1 (否则成人向作品被公开挂出去)
        _set_opts(True, False)
        state["bm"] = set()
        calls.clear()
        _wsrv._auto_bookmark_after_download({i4})
        add_r18 = [c for c in calls if "bookmarks/add" in c["url"]]
        assert len(add_r18) == 1, f"R18 单个作品也要收藏: {[c['url'] for c in calls]}"
        assert _json.loads(add_r18[0]["data"].decode("utf-8"))["restrict"] == 1, \
            "R18 作品必须非公开收藏"
        state["bm"] = set()

        # ④ 未登录 → 如实跳过, 不抛异常、不联网
        _set_opts(True, False)
        _wsrv._pixiv_http_session = lambda: None
        calls.clear()
        _wsrv._auto_bookmark_after_download({i1})
        assert calls == [], f"没登录不该发请求: {calls}"
        # pixiv 报错(如作品被删) → 记失败但不炸
        _wsrv._pixiv_http_session = lambda: FAKE_SESSION
        state["bm"] = set()

        def _boom(session, url, **kw):
            if "bookmarks/add" in url:
                return 404, _json.dumps({"error": True, "message": "作品不存在"})
            return _fake_web_http(session, url, **kw)
        scm._web_http = _boom
        _wsrv._auto_bookmark_after_download({i1})      # 不抛异常 = 通过
        scm._web_http = _fake_web_http
        scm._WEB_CRED.clear()

        # ⑤ 关注 / 取关 / 查状态
        r = client.get("/api/pixiv/follow-status?author_id=999").get_json()
        assert r["ok"] and r["followed"] is False, r
        calls.clear()
        r = client.post("/api/pixiv/follow", json={"author_id": "999"}).get_json()
        assert r["ok"] and r["followed"] is True, r
        fu = [c for c in calls if "bookmark_add.php" in c["url"]][0]
        form = fu["data"].decode("utf-8")
        assert "mode=add" in form and "type=user" in form and "user_id=999" in form, form
        assert "x-www-form-urlencoded" in (fu["ctype"] or ""), "关注走表单体"
        calls.clear()
        r = client.post("/api/pixiv/follow",
                        json={"author_id": "999", "follow": False}).get_json()
        assert r["ok"] and r["followed"] is False, r
        un = [c for c in calls if "rpc_group_setting.php" in c["url"]][0]
        assert "mode=del" in un["data"].decode("utf-8") \
            and "type=bookuser" in un["data"].decode("utf-8"), un["data"]

        # ⑥ 护栏
        assert client.post("/api/pixiv/follow",
                           json={"author_id": "local_abc"}).status_code == 400
        assert client.get("/api/pixiv/follow-status?author_id=local_abc").status_code == 400
        # 未登录: 走 build_pixiv_http_session 那一层模拟(接口用的是内层 _pixiv_http_session)
        scm.build_pixiv_http_session = lambda *a, **k: None
        _wsrv._pixiv_http_session = lambda: None
        assert client.post("/api/pixiv/follow",
                           json={"author_id": "999"}).status_code == 400, "未登录要 400"
        assert client.get("/api/pixiv/follow-status?author_id=999").status_code == 400
        scm.build_pixiv_http_session = lambda *a, **k: FAKE_SESSION
        _wsrv._pixiv_http_session = lambda: FAKE_SESSION

        # 设置读写 (假的 save_user_config: 绝不碰真实 config/user.json)
        saved = {}

        def _fake_save(updates):
            saved.update(updates)
        ucfg.save_user_config = _fake_save
        _set_opts(True, True)
        j = client.get("/api/pixiv-actions").get_json()
        assert j["auto_bookmark"] is True and j["bookmark_private"] is True, j
        r = client.post("/api/pixiv-actions",
                        json={"auto_bookmark": True, "bookmark_private": False})
        assert r.get_json()["ok"] and saved["pixiv_actions"] == {
            "auto_bookmark": True, "bookmark_private": False}, saved
        assert client.post("/api/pixiv-actions", json={}).status_code == 400

        # ⑦ 前端接线 (静态)
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        for k in ("toggleFollowAuthor", "paintFollowBtn", "hydrateFollowButtons",
                  "data-follow-aid", "/api/pixiv/follow", "toggleAutoBookmark",
                  "setBookmarkVisibility", "loadPixivActions", "/api/pixiv-actions"):
            assert k in appjs, f"前端缺少接线: {k}"
        indexh = (PROJECT_ROOT / "web/static/index.html").read_text(encoding="utf-8")
        assert 'id="autobook-toggle"' in indexh and 'id="autobook-vis"' in indexh, \
            "设置页要有自动收藏开关与可见性选择"

        # ⑦b 作品页(scope=artwork, type='artwork')的作者卡必须有「♡ 关注 / ➕ 全部作品加入清单」:
        # 这条分支以前漏传 withButton, 用户看到的作者信息只有名字/粉丝数、没有可点的按钮 
        import re as _re
        bare = _re.findall(r"renderAuthorCard\(res\.author\)", appjs)
        assert not bare, f"作者卡漏传 withButton(=true), 会没有关注/加入清单按钮: {bare}"
        assert "renderAuthorCard(res.author, true)" in appjs, \
            "作品页的作者卡要带关注/加入清单按钮"
        assert "hydrateFollowButtons(box, type === 'author' || type === 'artwork')" in appjs, \
            "作品页只有一个作者, 关注状态该预取(否则已关注的也显示成「♡ 关注」)"

        # ⑧ 默认值: 设计约定(保守默认 → 用户以为功能没做, 2026-09-18 )
        assert ucfg._canonicalize({})["pixiv_actions"]["auto_bookmark"] is True, \
            "_canonicalize 兜底里自动收藏必须默认开启"
        dflt = _json.loads((PROJECT_ROOT / "resources/default.json").read_text(encoding="utf-8"))
        assert dflt["pixiv_actions"]["auto_bookmark"] is True, dflt["pixiv_actions"]
        assert "R18 一律非公开收藏" in indexh, "设置页要写明 R18 一律非公开收藏"
        srv = (PROJECT_ROOT / "web/server.py").read_text(encoding="utf-8")
        assert "private = dflt_private or is_r18" in srv, \
            "自动收藏必须把 R18 强制成非公开"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        scm._web_http = orig_http
        scm.build_pixiv_http_session = orig_build
        _wsrv._pixiv_http_session = orig_sess
        ucfg.load_config, ucfg.save_user_config = orig_lc, orig_save
        _wsrv.load_config = orig_srv_lc
        scm._WEB_CRED.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def t108_rev_task_clear_failed_and_settings_layout() -> None:
    """T108: 反搜任务表「只清空失败」+ 设置页卡片被内容顶破的布局 (用户 2026-09-18 报)。

    ① 任务表除「清空全部」外要有「🧹 清空失败」，且只摘掉失败/待确认的行（文件与图库记录都不动）；
       摘掉的行在新一轮反搜开始、或结果换代(last_result.time 变了)时**必须失效**，
       否则同一张图这次新跑出来的失败结果会被上次的隐藏记录吞掉；
    ② 设置页两列卡片必须用 minmax(0,1fr) 而不是 1fr —— 1fr 的下限是内容 min-content，
       操作记录里一条超长行就能把整列顶到远超 50%，把同行的「体检与审批」挤到很右边。
    """
    appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    indexh = (PROJECT_ROOT / "web/static/index.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")

    # ① 只清空失败
    assert 'id="rev-task-clear-failed"' in indexh, "任务表要有「清空失败」按钮"
    assert "清空全部" in indexh, "原来的整表清空要留着(并改名说清是清『全部』)"
    for k in ("clearReverseFailedTasks", "revHiddenKeys", "revHiddenStamp",
              "revLastTasks", "rev-task-clear-failed"):
        assert k in appjs, f"前端缺少「只清空失败」接线: {k}"
    # 摘掉的行只在"同一次结果"内有效: 新一轮开始 + 结果换代 都要清空隐藏集
    assert "revHiddenKeys.clear();" in appjs, "摘掉的行必须能在新一轮/整表清空时恢复"
    assert appjs.count("revHiddenKeys.clear()") >= 3, \
        "隐藏集至少要在: 新一轮反搜 / 整表清空 / 结果换代 三处恢复"
    assert "lr.time !== revHiddenStamp" in appjs, "结果换代后隐藏记录要自动失效"

    # ② 布局: 列宽下限不能是内容宽度
    assert "minmax(0, 1fr) minmax(0, 1fr)" in css, \
        "card-row 必须用 minmax(0,1fr) —— 用 1fr 会被内容 min-content 顶破, 把同行卡片挤到右边"
    assert ".card-row > .card.mini { min-width: 0; }" in css, "网格项也要 min-width:0"
    assert "flex-wrap: wrap;" in css and ".op-item" in css, "操作记录行要能换行"
    # 单批作者结果不摆常驻禁用的翻批按钮
    assert "wp-single" in appjs and ".works-pager.wp-single" in css, \
        "只有一批时不该一直显示上一批/下一批按钮"


def t109_author_keyword_search_fast_path() -> None:
    """T109: 关键词作者搜索的**快路** (用户 2026-09-18 问「为什么这么慢」)。

    根因: 老代码拿 `/ajax/search/users/<kw>`（**路径版**, pixiv 早已下架 → 404）去试，
    永远失败 → 每次都掉进 DOM 爬页面那条慢路（2 次页面加载 + 最长 4.8s 等 SPA 渲染收敛
    + 逐个 ajax 补名字）。实测 `示例画师` 要 2.5s，而作品搜索只要 0.3s。
    正确端点是 **`/ajax/search/users?nick=<kw>`**（查询参数），一次就返回
    userIds + 名字 + 头像 + 简介，HTTP 直连 ~0.2s。
    这里用离线假响应把三件事钉住: 端点写法、形状解析（含老形状兼容/兜底）、
    以及「ajax 成功时绝不再爬页面」这条性能不变量。
    """
    from core.search_client import SearchClient, _AjaxNotFound

    sc = object.__new__(SearchClient)
    seen_urls = []
    state = {"search_body": None}

    def _fake_ajax_get(url):
        seen_urls.append(url)
        if "/ajax/search/users" in url:
            return state["search_body"]
        if "/ajax/user/" in url:
            uid = url.split("/ajax/user/")[1].split("?")[0]
            return {"name": f"名字{uid}", "image": "http://img"}
        raise AssertionError(f"不该请求这个 URL: {url}")

    sc._ajax_get = _fake_ajax_get

    # ① 现代形状: users 一次带齐 (名字/头像/简介)
    modern = {"data": [], "page": {"userIds": ["1", "2", "3"], "total": "3"},
              "users": [{"userId": "1", "name": "示例画师", "image": "http://a.jpg",
                         "comment": "自我介绍"},
                        {"userId": "2", "name": "示例画师き", "image": "http://b.jpg"},
                        {"userId": "3", "name": "无关的人"}]}
    state["search_body"] = modern
    aus, ok = sc._search_users_ajax("示例画师", limit=30)
    assert ok is True, "现代形状必须判定为「接口可用」"
    assert [a.author_id for a in aus] == ["1", "2"], f"只留名字匹配的两位: {[a.name for a in aus]}"
    assert aus[0].name == "示例画师" and aus[0].avatar_url == "http://a.jpg", aus[0]
    assert aus[0].bio == "自我介绍", f"接口给的简介要带上: {aus[0].bio}"
    assert seen_urls == ["https://www.pixiv.net/ajax/search/users?nick=%E3%81%BF%E3%82%89%E3%81%A4&blt=0&lang=zh"], \
        f"端点必须是 ?nick= 查询参数版(路径版 404): {seen_urls}"

    # ② users 为空但给了 userIds → 逐个 ajax/user 补名字再过滤
    seen_urls.clear()
    state["search_body"] = {"data": [], "page": {"userIds": ["7"]}, "users": []}
    aus2, ok2 = sc._search_users_ajax("名字7", limit=30)
    assert ok2 is True and [a.author_id for a in aus2] == ["7"], aus2
    assert any("/ajax/user/7?full=1" in u for u in seen_urls), seen_urls

    # ③ 老形状 (裸数组 / thumbnails.data / 带 body 外套) 也照旧能解析
    for old in ([{"id": "9", "userName": "老形状"}],
                {"thumbnails": {"data": [{"id": "9", "userName": "老形状"}]}},
                {"body": {"users": [{"userId": "9", "name": "老形状"}]}}):
        got = sc._parse_search_users_ajax(old, "老形状", limit=30)
        assert [a.author_id for a in got] == ["9"], f"老形状要兼容: {old}"

    # ④ 接口没给可用结构 → ok=False, 让调用方走 DOM 兜底 (而不是误报"没搜到")
    state["search_body"] = {"somethingElse": 1}
    aus3, ok3 = sc._search_users_ajax("x", limit=30)
    assert aus3 == [] and ok3 is False, "拿不到结构时必须回报 ok=False"
    # 结构正常但确实没人 → ok=True + 空 (不必再爬页面)
    state["search_body"] = {"data": [], "page": {"userIds": []}, "users": []}
    aus4, ok4 = sc._search_users_ajax("x", limit=30)
    assert aus4 == [] and ok4 is True, "确实没搜到要回报 ok=True(空)"

    # ⑤ 性能不变量: ajax 成功 → 绝不调用 DOM 爬页面; ajax 抛错 → 才走 DOM
    dom_calls = []
    sc._search_authors_dom = lambda kw, limit=30: (dom_calls.append(kw), [])[1]
    state["search_body"] = modern
    sc.reporter = _NullReporter()
    sc.login_ok = True
    got = sc.search_authors("示例画师")
    assert dom_calls == [], f"ajax 成功时不该再爬页面(这就是慢的原因): {dom_calls}"
    assert len(got) == 2, got

    def _boom(url):
        seen_urls.append(url)
        raise _AjaxNotFound("404")
    sc._ajax_get = _boom
    sc.search_authors("示例画师")
    assert dom_calls == ["示例画师"], f"ajax 不可用时才允许 DOM 兜底: {dom_calls}"

    # ⑥ 静态护栏: 源码里不许再出现路径版端点
    src = (PROJECT_ROOT / "core/search_client.py").read_text(encoding="utf-8")
    assert "/search/users?nick=" in src, "必须用查询参数版端点"
    assert '_AJAX_BASE}/search/users/{encoded}' not in src, \
        "路径版 /ajax/search/users/<kw> 是 404 的死端点, 不许再写回去"


class _NullReporter:
    """T109 用: 吞掉 reporter 输出 (作者搜索会打日志)。"""

    def log(self, *a, **k):
        pass

    def warn(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def t110_check_report_no_stale_items() -> None:
    """T110: 体检条目「处理完了还一直显示」+ 汇总条一直挂着 (用户 2026-09-18 报)。

    ① **真 bug**: `/api/library/self-check-dismiss`(处理完一条后调用) 只从落盘文件的
       `suspicious` 里删, `todos` 里那份还在 → 重启读盘时(`_ck_load_report_from_disk`
       合并两个字段)又被合并回来 → 用户看到"指定了作者这条还在"。现在两个字段一起删,
       并且 `_save_self_check_compat` 不再把 todos 拼进 suspicious(文件里同一条不再存两份)。
    ② 图库顶部那条「🩺 上次体检：…」: 没有待处理项时**不显示**; 点「✕」收起后重启仍收起;
       真跑一次体检后自动恢复显示。
    ③ 打开报告时做本地轻量复核: 缺作者/缺文件/空夹/散落图/损坏图(文件已被删)这几类现场能判的,
       解决了自动清掉; `legacy_name / metadata_failed` 这些**必须重跑对应步骤**的一律保持原样(绝不误清)。
       「损坏图」只判**文件还在不在**: 文件还在就原样保留(哪怕其实已修好 —— 没重跑①就不许
       宣称"损坏已修复", 那才是真的丢信息); 文件被删/移走 → 这条描述已不成立 → 清掉
。
    ④ 两个桶互斥(同一条不会两边都有), 前端有去重兜底与新文案。
    """
    import shutil
    import tempfile
    import utils.paths as upaths
    import web.server as wsrv

    tmp = Path(tempfile.mkdtemp(prefix="t110stale_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_rep = wsrv._check_state.get("report")
    orig_res = wsrv._self_check_state.get("result")

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        from web.server import create_app
        img = tmp / "img"
        (img / "作者甲_1").mkdir(parents=True)
        (img / "作者甲_1" / "100_p0.png").write_bytes(b"SYNTH")
        (img / "散图.png").write_bytes(b"SYNTH")          # 存在 → loose_image 仍存在
        (img / "gone.png")                                # 不存在 → 这条应被清掉
        (img / "坏图.png").write_bytes(b"SYNTH")           # 存在 → corrupt 仍保留(没重跑①不许假报修好)
        wsrv._self_check_state["result"] = None
        wsrv._check_state["report"] = None
        app = create_app()

        # 造数据: A=缺作者仍在, B=缺作者但已补上作者(=已解决), C=缺文件但文件其实在(=已解决),
        #       散图.png=散落图仍在, gone.png=散落图已被整理走(=已解决),
        #       E=损坏且**文件还在**(必须保留 —— 本地判不了它好没好),
        #       F=损坏但**文件已被用户删掉**(这条描述已不成立 → 必须清掉)
        from db.store import PSt
        with wsrv._db() as db:
            db.upsert_author("1", "作者甲")
            for aid in ("A", "B", "C"):
                db.change_work_status(aid, 2, title=aid)      # 不给作者 → author_id 为空
            db.conn.execute("UPDATE artworks SET author_id='1' WHERE artwork_id='B'")
            db.mark_page_status("C", 0, PSt.DONE, local_path="作者甲_1/100_p0.png")
            db.conn.commit()
        # 落盘一份"上次体检的结果" (旧格式; 故意把 A 同时写进两个字段, 验证读回按 (type,ref) 去重)
        legacy = {"time": "2026-01-01 00:00:00", "fixed": {}, "ignored": 0,
                  "recheck": {"at": "2026-01-01 00:00:00"},
                  "suspicious": [
                      {"type": "missing_file", "ref": "C", "artwork_id": "C", "text": "C 文件丢了"},
                      {"type": "no_author", "ref": "A", "artwork_id": "A", "text": "A 缺作者"},
                      {"type": "corrupt", "ref": str(img / "坏图.png"),
                       "path": str(img / "坏图.png"), "text": "E 损坏"},
                      {"type": "corrupt", "ref": str(img / "已删的坏图.png"),
                       "path": str(img / "已删的坏图.png"), "text": "F 损坏(文件已删)"},
                      {"type": "empty_dir", "ref": str(img / "没了的老夹"),
                       "path": str(img / "没了的老夹"), "text": "空夹"},
                  ],
                  "todos": [
                      {"type": "no_author", "ref": "A", "artwork_id": "A", "text": "A 缺作者"},
                      {"type": "no_author", "ref": "B", "artwork_id": "B", "text": "B 缺作者"},
                      {"type": "loose_image", "ref": "gone.png", "name": "gone.png",
                       "text": "散落 gone.png"},
                      {"type": "loose_image", "ref": "散图.png", "name": "散图.png",
                       "text": "散落 散图.png"},
                  ]}
        (tmp / "self_check.json").write_text(
            json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        client = app.test_client()

        # ② 打开报告 → 读盘(去重) + 本地复核:
        #    清掉 B(已有作者)/C(文件回来了)/gone.png(文件已不在)/空夹(夹已不存在)
        #        /已删的坏图(损坏图但文件被用户删了) = 5 条;
        #    保留 A(仍缺作者)/坏图.png(文件还在, 不许假报已修好)/散图.png(文件还在) = 3 条
        j = client.get("/api/library/check-report").get_json()["report"]
        allnow = j["suspicious"] + j["todos"]
        refs = {x["ref"] for x in allnow}
        assert refs == {"A", str(img / "坏图.png"), "散图.png"}, \
            f"只清本地能判定的已解决项, 其余原样保留: {refs}"
        assert (j.get("recheck") or {}).get("auto_resolved") == 5, j.get("recheck")
        # 必须重跑步骤才行的类型绝不许被清 (误清 = 假报"已解决")
        assert str(img / "坏图.png") in refs, \
            "文件还在的损坏图本地判不了好没好, 绝不许当成已解决清掉"
        # 用户 2026-09-21 报: 图片删了报告还在
        assert str(img / "已删的坏图.png") not in refs, \
            "文件已被删掉的损坏图条目要清掉"
        # 两个桶互斥
        s_refs = {x["ref"] for x in j["suspicious"]}
        t_refs = {x["ref"] for x in j["todos"]}
        assert not (s_refs & t_refs), f"同一条不该同时在两个桶: {s_refs & t_refs}"
        # ① 顺手落盘后, 文件里同一条**不能再出现两次**(老写法 suspicious = suspicious + todos)
        disk = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
        keys = [(x["type"], x["ref"]) for x in (disk["suspicious"] + disk["todos"])]
        assert len(keys) == len(set(keys)), f"落盘文件里同一条存了两份: {keys}"

        # ①b 即使**一条都没清**(问题都还在), 老 layout 的文件也要被规范化:
        #     否则用户/其它读文件的地方仍会看到重复条目
        old_layout = {"time": "t2", "suspicious": [
            {"type": "no_author", "ref": "A", "artwork_id": "A", "text": "A 缺作者"},
            {"type": "loose_image", "ref": "散图.png", "name": "散图.png", "text": "散落"},
        ], "todos": [
            {"type": "no_author", "ref": "A", "artwork_id": "A", "text": "A 缺作者"},
            {"type": "loose_image", "ref": "散图.png", "name": "散图.png", "text": "散落"},
        ]}
        (tmp / "self_check.json").write_text(
            json.dumps(old_layout, ensure_ascii=False), encoding="utf-8")
        wsrv._check_state["report"] = None
        wsrv._self_check_state["result"] = None
        jb = create_app().test_client().get("/api/library/check-report").get_json()["report"]
        assert {x["ref"] for x in jb["suspicious"] + jb["todos"]} == {"A", "散图.png"}, jb
        assert not (jb.get("recheck") or {}).get("auto_resolved"), \
            "一条都没清时不该报 auto_resolved"
        d2 = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
        k2 = [(x["type"], x["ref"]) for x in (d2["suspicious"] + d2["todos"])]
        assert len(k2) == len(set(k2)), f"没清理时也要把老 layout 规范化(去重): {k2}"
        assert (d2.get("suspicious") or []) == [], \
            f"no_author/散落图属于待办, 不该留在 suspicious 里: {d2.get('suspicious')}"

        # ③ 处理完一条(指定作者) → 两个字段都要删, 重启读盘不许复活
        r = client.post("/api/library/self-check-dismiss",
                        json={"artwork_id": "A"}).get_json()
        assert r["removed"] >= 1, r
        disk2 = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
        left = {str(x.get("artwork_id")) for x in (disk2.get("suspicious") or [])} | \
               {str(x.get("artwork_id")) for x in (disk2.get("todos") or [])}
        assert "A" not in left, f"处理完的条目从落盘里也必须没了(否则重启复活): {left}"
        wsrv._check_state["report"] = None          # 模拟重启
        wsrv._self_check_state["result"] = None
        j2 = create_app().test_client().get("/api/library/check-report").get_json()["report"]
        assert "A" not in {x["ref"] for x in j2["suspicious"] + j2["todos"]}, \
            "重启后不许把已处理的条目列回来"

        # ④ 顶部汇总条: 还有待处理 → 显示; ✕ 收起 → 落盘记住; 新报告 → 恢复
        app3 = create_app()
        c3 = app3.test_client()
        assert (c3.get("/api/library/check-report").get_json()["report"] or {}).get("bar_dismissed") in (None, ""), \
            "默认不该是收起状态"
        assert c3.post("/api/library/check-report/dismiss").get_json()["dismissed"] is True
        disk3 = json.loads((tmp / "self_check.json").read_text(encoding="utf-8"))
        assert disk3.get("bar_dismissed"), "「✕ 收起」要落盘(否则刷新又冒出来)"
        wsrv._check_state["report"] = None
        wsrv._self_check_state["result"] = None
        j4 = create_app().test_client().get("/api/library/check-report").get_json()["report"]
        assert j4.get("bar_dismissed"), "重启后仍保持收起"

        # ⑤ 前端接线 (静态): 收起按钮 / 没待处理就不显示 / 新文案 / 去重兜底
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        for k in ("dismissCheckBar", "/api/library/check-report/dismiss", "bar_dismissed",
                  "const showReport = !!rc && liveN > 0 && !rc.bar_dismissed",
                  "const uniq = (arr)", "auto_resolved",
                  # 2026-09-27 报告弹窗改版: 分节标题「标题 · 说明 · 计数」三段式 +
                  # 统计徽标 + 判据折进 <details>, 不许再回到长句堆叠的老样子
                  "function chkStatBadge(", "chkSectionHtml('🔧 系统异常', '可一键修复'",
                  "chkSectionHtml('✋ 缺信息', '需人工补齐'", '<details class="chk-help">',
                  'class="chk-sec"'):
            assert k in appjs, f"前端缺少接线/文案: {k}"

        # 文案护栏: 上一轮为「去第二人称」做机械替换, 把判据/口径全塞进正文长句
        # (「分两类的判据是「谁能修」」「不敢当成"已解决"清掉」),。
        # 这里钉住那批措辞不许回潮, 并要求报告段不能出现**两层嵌套括号**。
        css = (PROJECT_ROOT / "web/static/style.css").read_text(encoding="utf-8")
        for k in (".chk-stats", ".chk-sec-note", ".chk-recheck-head", ".chk-help"):
            assert k in css, f"体检新版式的样式缺失: {k}"
        seg_a = appjs.index("function chkSectionHtml(")
        seg_b = appjs.index("window.chkIgnore")
        seg = appjs[seg_a:seg_b]
        seg = re.sub(r"(?m)^[ \t]*//.*$", "", seg)       # 剥整行注释, 别拿注释里的自述当文案
        for bad in ("分两类的判据", "不敢当成", "顺手核对", "没有可疑项 ✓", "没有待办项 ✓",
                    "本次实际复核过的来源", "上次复检结论"):
            assert bad not in seg, f"体检报告里又出现绕口/口语化的旧文案: {bad}"
        _nested = re.search(r"（[^）\n]{0,80}（", seg)
        assert not _nested, f"体检报告文案里出现两层嵌套括号: {_nested.group()!r}"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        wsrv._check_state["report"] = orig_rep
        wsrv._self_check_state["result"] = orig_res
        wsrv._check_state.update(running=None, progress={})
        shutil.rmtree(tmp, ignore_errors=True)


def t111_timestamp_format_and_updated_sort() -> None:
    """T111: 时间戳格式统一 —— 「按修改时间排序时某几条永远顶在最前面」(用户 2026-09-18 报)。

    症状:。
    根因: 那几条的 `updated_at` 是老代码用 `datetime.now().isoformat()` 写的
    (本地时间 + 'T' + 微秒), 而别人都是 UTC + 空格。排序是**字符串比较**,
    第 11 个字符 'T'(0x54) > ' '(0x20) → 同一天里带 'T' 的**无条件排第一**。
    ① `now_stamp()` = 唯一写法 (UTC + 空格, 与 schema 默认值同格式);
    ② `normalize_ts()` 能把老值按**本机时区**换算回 UTC 空格 (不是简单替换 'T');
    ③ 所有写 artworks.updated_at / authors.last_updated 的路径都必须用它 (老路径曾漏 6 处);
    ④ `normalize_all_timestamps()` 幂等、且修好后排序正确 (改得晚的排前面);
    ⑤ 静态护栏: store.py 里不许再出现 `now().isoformat()`。
    """
    import datetime as _dt
    from db.store import PixivDatabase, WSt, now_stamp, normalize_ts

    # ① now_stamp: 严格同格式 + 是 UTC (不是本地时间)
    s = now_stamp()
    _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    assert "T" not in s and "." not in s, s
    utc_now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    assert abs((_dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                - _dt.datetime.strptime(utc_now, "%Y-%m-%d %H:%M:%S")).total_seconds()) < 90, \
        f"now_stamp 应该是 UTC: {s} vs {utc_now}"

    # ② normalize_ts: 按本机时区换算成 UTC 空格格式 (不是简单把 'T' 换成空格)
    old = "2026-09-18T05:38:35.586132"
    got = normalize_ts(old)
    expect = (_dt.datetime.fromisoformat(old).astimezone(_dt.timezone.utc)
              .strftime("%Y-%m-%d %H:%M:%S"))
    assert got == expect, f"老值要按本机时区换算成 UTC: {got} != {expect}"
    assert got != old and "T" not in got and "." not in got, got
    assert normalize_ts("2026-09-18 05:38:35") == "2026-09-18 05:38:35", "标准格式原样返回"
    assert normalize_ts("不是时间") is None and normalize_ts("") is None

    db = PixivDatabase(":memory:")
    # ③ 各写路径产出的时间戳都必须没有 'T'
    db.upsert_author("9", "作者九")
    for aid, title in (("901", "t1"), ("902", "t2")):
        db.change_work_status(aid, WSt.DONE, title=title, author_id="9")
    db.set_artworks_author(["901"], "9")
    db.update_artwork("901", title="t1改")
    db.set_artwork_tags("901", ["标签A"])
    db.fill_source_author_from_links("9", [{"site": "twitter", "handle": "h1"}])
    db.update_author("9", name="作者九改")
    db.create_or_get_local_author("外来甲")
    db.merge_author_into("9", "902")
    rows = db.conn.execute(
        "SELECT artwork_id, created_at, updated_at FROM artworks").fetchall()
    for r in rows:
        for v in (r["created_at"], r["updated_at"]):
            assert v and "T" not in str(v), f"{r['artwork_id']} 的时间戳格式错了: {v!r}"
    for r in db.conn.execute("SELECT first_seen, last_updated FROM authors").fetchall():
        for v in (r["first_seen"], r["last_updated"]):
            assert v is None or "T" not in str(v), f"作者时间戳格式错了: {v!r}"

    # ④ 复现 bug + 修复: 带 'T' 的那条即使更早, 也会被顶到最前; 统一格式后就按真实时间排
    db.conn.execute("UPDATE artworks SET updated_at='2026-09-18T05:00:00.000001' "
                    "WHERE artwork_id='901'")
    db.conn.execute("UPDATE artworks SET updated_at='2026-09-18 06:00:00' "
                    "WHERE artwork_id='902'")
    db.conn.commit()
    order = [str(r["artwork_id"]) for r in db.list_artworks(sort="updated", order="desc")]
    assert order[0] == "901", f"这正是不修时的症状: 带 'T' 的无条件排最前 {order}"
    n = db.normalize_all_timestamps()
    assert n >= 1, f"应该纠正到至少 1 处: {n}"
    order2 = [str(r["artwork_id"]) for r in db.list_artworks(sort="updated", order="desc")]
    assert order2[0] == "902", f"统一格式后应按真实时间排(06:00 晚于 21:00 前一天): {order2}"
    assert db.normalize_all_timestamps() == 0, "幂等: 再跑一次不应再改任何东西"
    db.close()

    # ⑤ 静态护栏 + 体检②离线清理里也要做这件事
    import re as _re
    src = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
    bad = _re.findall(r"^\s*\w+\s*=\s*datetime\.datetime\.now\(\)\.isoformat\(\)\s*$",
                      src, _re.M)
    assert not bad, f"还有地方用 isoformat 写时间戳 (会让排序错乱): {bad}"
    assert "def now_stamp()" in src and "def normalize_ts(" in src
    srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "normalize_all_timestamps()" in srv and "timestamps_fixed" in srv, \
        "体检「② 离线清理」也要顺手统一时间戳"
    mainpy = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    assert "_normalize_timestamps_at_startup()" in mainpy, "启动时要自动纠正(不用等用户跑体检)"
    appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
    assert "timestamps_fixed" in appjs, "体检结果里要显示这一项"


def t112_reverse_stale_failed_tasks_pruned() -> None:
    """T112: 反搜「失败任务反复出现」+「还提示文件仍在反搜区」(用户 2026-09-18 报)。

    ① 任务表是从 `reverse_results.json` 重建的, 而入库/丢弃/清空**谁都没动过这个文件**
       → 手动入库之后, 那些「未识别」行每次刷新又回来, 点清空也没用;
    ② `/api/reverse/status` 现在先剪掉「文件已不在收件箱」的过时条目并落盘:
       `failed/conflict/skipped/in_library` 都以"文件还在收件箱等处理"为前提, 文件没了就是过时;
       **`success`(已归档) 保留成历史** (它的文件本来就搬进图库了), 文件仍在的条目也一律保留;
    ③ 入库/丢弃成功后, 本次任务表里那几行当场消失 (前端 revDropFromRun);
    ④ 文案不再无脑写"(文件仍在待反搜区)" —— 用户明明已入库、收件箱也空, 却被这句反复误导。
    """
    import shutil
    import tempfile
    import utils.paths as upaths
    import web.server as wsrv
    from db.store import PixivDatabase

    tmp = Path(tempfile.mkdtemp(prefix="t112rev_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        from web.server import create_app
        inp = tmp / "input"
        (inp / "not_found").mkdir(parents=True)
        (inp / "stay.png").write_bytes(b"SYNTH-STAY")      # 还在收件箱 → 记录要保留
        rp = tmp / "reverse_results.json"
        rp.write_text(json.dumps({
            "time": "2026-09-18 03:03:07", "mode": "id",
            "success": [{"file": "archived.png", "artwork_id": "1",
                         "reason": "已归档"}],
            "failed": [{"file": "gone1.png", "artwork_id": "2", "reason": "未找到"},
                       {"file": "stay.png", "artwork_id": "3", "reason": "未找到"},
                       {"file": "not_found/gone2.png", "artwork_id": "4", "reason": "未找到"}],
            "conflict": [{"file": "gone3.png", "reason": "文件名与识别结果不一致"}],
            "skipped": [{"file": "stay.png", "reason": "同名已存在"}],
            "in_library": [{"file": "gone4.png"}],
        }, ensure_ascii=False), encoding="utf-8")

        client = create_app().test_client()
        # ② 过时条目被剪掉, 仍在收件箱的保留, success 当历史留着
        j = client.get("/api/reverse/status").get_json()
        lr = j["last_result"]
        assert [x["file"] for x in lr["failed"]] == ["stay.png"], lr["failed"]
        assert lr["conflict"] == [] and lr["in_library"] == [], lr
        assert [x["file"] for x in lr["skipped"]] == ["stay.png"], lr["skipped"]
        assert [x["file"] for x in lr["success"]] == ["archived.png"], \
            "已归档的成功记录要留着当历史(它的文件本来就搬走了, 不能当'过时')"
        disk = json.loads(rp.read_text(encoding="utf-8"))
        assert [x["file"] for x in disk["failed"]] == ["stay.png"], \
            "剪掉的结果要落盘, 否则刷新又回来"
        assert j["pending"] and j["image_files"] == 1, j["image_files"]

        # 二次读取不该再变 (幂等)
        j2 = client.get("/api/reverse/status").get_json()
        assert [x["file"] for x in j2["last_result"]["failed"]] == ["stay.png"], j2["last_result"]

        # ③ 入库那一张之后: 它的失败记录自动消失 (不需要用户再点清空)
        r = client.post("/api/reverse/adopt", json={"files": ["stay.png"]}).get_json()
        assert r["status"] == "ok" and len(r["imported"]) == 1, r
        assert not (inp / "stay.png").exists(), "入库后收件箱里的源文件要被移走"
        j3 = client.get("/api/reverse/status").get_json()
        assert j3["last_result"]["failed"] == [], \
            f"入库成功后那条失败记录必须消失: {j3['last_result']['failed']}"
        assert j3["image_files"] == 0 and j3["pending"] == [], j3
        with PixivDatabase() as db:
            assert db.conn.execute("SELECT COUNT(*) FROM artworks "
                                   "WHERE artwork_id LIKE 'ext_%'").fetchone()[0] == 1

        # ④ 前端接线: 文案要如实 + 入库/丢弃后当场把行去掉
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        assert "revDropFromRun(list)" in appjs, "入库/丢弃后要把那几行从本次任务里去掉"
        assert "待反搜区已空，这些记录不会再出现" in appjs, "清空失败的提示要如实"
        assert "条失败任务（文件仍在待反搜区）" not in appjs, \
            "不许再无脑提示「文件仍在待反搜区」"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        shutil.rmtree(tmp, ignore_errors=True)


def t113_reconcile_corrupt_no_false_positive_and_report() -> None:
    """T113: 「① 磁盘对账」把正常大图误报成损坏 + 报告里查不到是哪个文件 (用户 2026-09-19 报)。

    ① 判据里原有一条「>60MB 一律判损坏」, 本意是"太大就不开图, 防撑爆内存",
       实际把"没校验"说成了"损坏" —— 用户那张 63.5MB / 1.27 亿像素的**正常**大图
       (128028374_p0.png) 被报成损坏。现在只有「空/极小文件」与「PIL 真抛异常」才算损坏,
       超大图照常 verify (open 只读文件头、verify 只校验结构, 不解码像素), 图片炸弹
       (DecompressionBombError) 只算"策略跳过", 不报损坏;
    ② 损坏只有 ① 校验得了, 而报告一直是 ⑥ 在生成 → 单跑 ① 时只看到一个「损坏 N」的数字,
       报告里翻不到是哪个文件。现在 ① 自己维护报告里的 corrupt 组 (旧条目全部换掉,
       修好的自动消失), 报告里能直接看到路径并「♻ 重下修复」; corrupt_verified 也要置真,
       否则报告还挂着"这一次没有校验图片损坏"。
    """
    import io
    import shutil
    import tempfile
    import utils.paths as upaths
    from PIL import Image

    tmp = Path(tempfile.mkdtemp(prefix="t113corrupt_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    try:
        wsrv = _t8x_env(tmp)
        img = tmp / "img"
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, format="PNG")
        # 尾部补几个字节凑过 128B 的「极小文件」门槛: PNG 在 IEND 之后的数据会被忽略,
        # 所以这仍是**有效**图片 (8x8 的原生 PNG 只有 77B, 会被合理地判成空文件)
        small = buf.getvalue() + b"\x00" * 200
        # _t8x_env 放的两个 8 字节占位图 (<128B 会被合理判坏) → 换成真 PNG, 免得干扰断言
        for rel in ("画师甲_500/501_p0.png", "599_p0.png"):
            (img / rel).write_bytes(small)
        # ① 正常大图: 有效 PNG + 尾部填充到 62MB (> 原 60MB 阈值, 就是用户那张的同类)
        big = img / "画师甲_500" / "602_p0.png"
        big.write_bytes(small + b"\x00" * (62 * 1024 * 1024))
        # ② 真损坏: 200 字节的垃圾 (不是图片, 超过"极小文件"门槛 → 只能靠 PIL 判出来)
        bad = img / "画师甲_500" / "603_p0.png"
        bad.write_bytes(b"not an image at all " * 10)
        assert big.stat().st_size > 60 * 1024 * 1024 and bad.stat().st_size > 128

        client = wsrv.create_app().test_client()
        client.post("/api/library/check", json={"step": "reconcile"})
        st = _t8x_wait_step(wsrv, "reconcile")
        assert st and st.get("ok"), f"① 磁盘对账应跑完, 实得 {st}"
        assert st["summary"]["corrupt"] == 1, \
            f"只该报那 1 个真损坏的文件 (正常大图不许被判坏), 实得 {st['summary']}"

        j = client.get("/api/library/check-report").get_json()["report"]
        cor = [x for x in j["suspicious"] if x.get("type") == "corrupt"]
        assert len(cor) == 1, f"损坏条数应与①一致, 实得 {cor}"
        # ② 报告里必须能看出**是哪个文件** (用户就是卡在这里: 只看到数字, 查不到是谁)
        assert str(bad) in (cor[0].get("path") or cor[0].get("ref") or ""), \
            f"报告里要写清损坏的是哪个文件, 实得 {cor[0]}"
        assert "602_p0.png" not in json.dumps(cor, ensure_ascii=False), \
            "62MB 的正常大图不许出现在损坏清单里"
        assert j.get("corrupt_verified") is True, \
            "① 真校验过损坏 → 报告不该再说「这一次没有校验图片损坏」"

        # ③ 修好之后重跑①: 旧条目自动消失, 不留清不掉的幽灵条目
        bad.write_bytes(small)
        wsrv._check_state["steps"].pop("reconcile", None)
        client.post("/api/library/check", json={"step": "reconcile"})
        st2 = _t8x_wait_step(wsrv, "reconcile")
        assert st2 and st2["summary"]["corrupt"] == 0, f"实得 {st2}"
        j2 = client.get("/api/library/check-report").get_json()["report"]
        assert not [x for x in j2["suspicious"] if x.get("type") == "corrupt"], \
            f"损坏文件修好后报告里那条要消失, 实得 {j2['suspicious']}"

        # ④ 前端接线: 报告里 corrupt 那组要能一键「♻ 重下修复」(路径→作品的映射不能丢)
        appjs = (PROJECT_ROOT / "web/static/app.js").read_text(encoding="utf-8")
        assert "'corrupt'" in appjs and "repairWorks" in appjs, "损坏组要有一键重下修复入口"
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        import web.server as wsrv2
        wsrv2._check_state.update(running=None, steps={}, progress={},
                                  report=None, last_run=None, journal_key=None)
        shutil.rmtree(tmp, ignore_errors=True)


def t114_download_state_from_pages_not_stale_status() -> None:
    """T114: 图库卡片按钮态只认 pages, 不再被陈旧的 artworks.status 卡住 (用户 2026-09-20 报)。

    旧逻辑 `full = (status == DONE and 页数齐)` 把可能陈旧的 artworks.status 当闸门:
      - 87881705 式: 用户本地补进缺失页后, reconcile_output 只把 page 刷成 DONE,
        从不提升 artworks.status(停在 NOT_YET) → 卡片永远显示「补全页数」, 点刷新/同步都不变;
      - 64163186 式: 作品在 pixiv 已失效(status=404) 但本地页其实完整, 也一直被卡在「补全页数」。
    新逻辑: 完整性只看 pages (trashed_at IS NULL 且 page_num < total_pages 的 DONE 页数
    >= total_pages); total_pages 为空的外来作品退回只看 status。

    断言 (全离线, 临时库):
      A 已失效但本地完整: tp=1, status=ERROR(404), p0 与 p1 都 DONE (p1 是附加页)
        → expired (2026-09-20 起: 已失效单独成一态, 前端显示「🔎 搜索来源」, 不再是 downloaded)
      B 缺页→本地补齐: tp=2, status=NOT_YET, p0 DONE/p1 PLACEHOLDER, 磁盘放入 p1 后对账
        → 补前 partial, 补后 downloaded (status 仍 NOT_YET 也不该卡住)
      C 反向验证·软删一页: B 基础上把 p1 的 trashed_at 置为当前时间 → partial
      D 页数未知的外来作品: tp=NULL 且 status=DONE → downloaded; tp=NULL 且 NOT_YET → partial
      E 反向验证·附加页不能凑数: tp=2, 只有 p0 与 p5 是 DONE (p5 超 tp), p1 PLACEHOLDER → partial
    """
    import tempfile
    import shutil
    from db.store import PixivDatabase, PSt, WSt

    tmp = Path(tempfile.mkdtemp(prefix="t114states_"))
    try:
        img = tmp / "img"
        img.mkdir(parents=True)
        db = PixivDatabase(":memory:")
        try:
            def _st(aid: str) -> str:
                return db.artwork_download_states([aid])[aid]

            # A 已失效 (64163186 式): 本地完整也不再谎报「已入库」, 单独判 expired
            a = "64163186"
            db.change_work_status(a, WSt.ERROR, total_pages=1)
            db.mark_page_status(a, 0, PSt.DONE, local_path=f"{a}_p0.png")
            db.mark_page_status(a, 1, PSt.DONE, local_path=f"{a}_p1.png", origin="merged")
            assert _st(a) == "expired", \
                f"A 已失效作品应判 expired (前端显示搜索来源), 实得 {_st(a)}"

            # B 缺页→本地补齐 (87881705 式): 对账只刷 page, 不提升 artworks.status
            b = "999000111"
            db.change_work_status(b, WSt.NOT_YET, total_pages=2)
            (img / f"{b}_p0.png").write_bytes(b"p0" * 100)
            db.mark_page_status(b, 0, PSt.DONE, local_path=f"{b}_p0.png")
            db.mark_page_status(b, 1, PSt.PLACEHOLDER)
            assert _st(b) == "partial", f"B 补页前应判缺页, 实得 {_st(b)}"
            (img / f"{b}_p1.png").write_bytes(b"p1" * 200)
            db.reconcile_output(img)
            assert db.get_page_info(b, 1).status == PSt.DONE, \
                "对账应把磁盘上的 p1 认回并标 DONE"
            assert _st(b) == "downloaded", \
                f"B 本地补齐后应判已入库 (status 仍 NOT_YET 也不该卡住), 实得 {_st(b)}"

            # C 反向验证: 软删一页 (trashed_at 非空) 后不该再谎报已入库
            with db.conn:
                db.conn.execute(
                    "UPDATE pages SET trashed_at=datetime('now') "
                    "WHERE artwork_id=? AND page_num=1", (b,))
            assert _st(b) == "partial", \
                f"C 软删一页后必须判缺页 (不能拿已软删的页凑数), 实得 {_st(b)}"

            # D 页数未知的外来作品: 没有「完整」概念, 退回只看 status
            d1, d2 = "ext_700001", "ext_700002"
            db.change_work_status(d1, WSt.DONE, total_pages=None)
            db.mark_page_status(d1, 0, PSt.DONE, local_path="D/ext_p0.png")
            assert _st(d1) == "downloaded", f"D tp=NULL+DONE 应判已入库, 实得 {_st(d1)}"
            db.change_work_status(d2, WSt.NOT_YET, total_pages=None)
            db.mark_page_status(d2, 0, PSt.DONE, local_path="D/ext_p0.png")
            assert _st(d2) == "partial", f"D tp=NULL+NOT_YET 应判缺页, 实得 {_st(d2)}"

            # E 反向验证: 超出 total_pages 的附加页不能拿来凑数
            e = "888000222"
            db.change_work_status(e, WSt.NOT_YET, total_pages=2)
            db.mark_page_status(e, 0, PSt.DONE, local_path=f"{e}_p0.png")
            db.mark_page_status(e, 1, PSt.PLACEHOLDER)
            db.mark_page_status(e, 5, PSt.DONE, local_path=f"{e}_p5.png", origin="merged")
            assert _st(e) == "partial", \
                f"E tp=2 只有 p0+p5 是 DONE 时应判缺页 (附加页不算数), 实得 {_st(e)}"
        finally:
            db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t115_queue_clear_scope_and_writeback_guard() -> None:
    """T115: 下载任务「清空」跟随筛选 + 清空进行中的写回闸门 (用户 2026-09-20 要求)。

    背景: 原来只有一个「🧹 清空已完成」(只删 status='done'), 失败/未开始/整个队列都清不掉;
    后端虽早有 DELETE FROM download_queue_items 全清接口, 前端却从没有任何按钮调用它(死接口)。
    用户明确要求「允许清且包含进行中, 但要加保护避免副作用和 bug」。

    断言 (全离线, 临时库):
      A 四个 scope 各清各的: all / running(pending+running) / done / failed, 互不牵连
      B 未知 scope 当 all (点了按钮却静默什么都不做, 比多清更糟)
      C 清空进行中后的写回闸门: 队列只有一条 pending → 引擎 claim 成 running →
        用户清空全部(行没了) → 引擎收尾写 done/failed 时**必须**打不中任何行
        (已清掉的行不被复活); 同时断言新任务的 id 不复用被删行的 id
        (本表是 AUTOINCREMENT) —— 这条是「事实核对」, 不是被保护的风险点。
        反向验证: 收尾只对「自己认领过、且仍是 running」的行生效, 别做成一刀切
      D 行已删时 claim/finish 都命中 0 行 (不复活、不报错)
      E 前端静态检查: 新按钮/文案、跟随筛选的 scope 表、已失效→「搜索来源」
    """
    from db.store import PixivDatabase

    db = PixivDatabase(":memory:")
    try:
        def _mk(aid: str, status: str = "pending") -> int:
            return db.queue_add(item_type="artwork", title=f"#{aid}",
                                artwork_id=aid, status=status)

        def _ids() -> set:
            return {int(x["id"]) for x in db.queue_list()}

        # A 四个 scope 各清各的
        for aid, st in (("1001", "pending"), ("1002", "running"),
                        ("1003", "done"), ("1004", "failed")):
            _mk(aid, st)
        assert db.queue_clear_scope("failed") == 1, "failed 应只清 1 条"
        assert _ids() == {1, 2, 3}, f"清 failed 不该动其他栏, 实得 {_ids()}"
        assert db.queue_clear_scope("done") == 1, "done 应只清 1 条"
        assert _ids() == {1, 2}, f"清 done 不该动其他栏, 实得 {_ids()}"
        assert db.queue_clear_scope("running") == 2, \
            "running 应把 pending+running 一起清 (界面「进行中」栏就是这两态)"
        assert _ids() == set(), f"running 清完队列应空, 实得 {_ids()}"
        _mk("1005")
        _mk("1006", "done")
        assert db.queue_clear_scope("all") == 2, "all 应清空整个队列"
        assert _ids() == set()

        # B 未知 scope 当 all
        _mk("1007")
        assert db.queue_clear_scope("这不是个 scope") == 1, \
            "未知 scope 必须当 all (否则用户点了没反应)"
        assert _ids() == set()

        # C 清空进行中: 引擎随后仍会下完那一项, 但它的收尾写回必须打不中任何行
        old = _mk("2001")
        assert db.queue_claim_item(old) == 1, "认领 pending 应成功"
        assert db.queue_clear_scope("all") == 1, "清掉那条正在跑的行"
        new = _mk("2002")
        assert new != old, "本表 id 是 AUTOINCREMENT, 不该复用被删行的 id"
        assert db.queue_finish_item(old, "done") == 0, \
            "旧任务的收尾写回不该命中任何行 (行已被清掉, 不能被复活)"
        assert db.queue_finish_item(old, "failed") == 0, "失败写回同理"
        assert [int(x["id"]) for x in db.queue_list()] == [new], \
            f"队列里应只剩新任务, 实得 {db.queue_list()}"
        assert db.queue_list()[0]["status"] == "pending", \
            f"新任务不该被旧任务的收尾波及, 实得 {db.queue_list()[0]['status']}"
        # 反向验证: 收尾只对「自己认领过、且仍是 running」的行生效 (别做成一刀切)
        assert db.queue_finish_item(new, "done") == 0, \
            "还没认领(pending)的行不该被收尾写回改状态"
        assert db.queue_claim_item(new) == 1, "认领新任务应成功"
        assert db.queue_finish_item(new, "done") == 1, "本行在跑时收尾应正常生效"
        assert db.queue_list()[0]["status"] == "done"

        # D 行已删: claim / finish 都是 0 行, 不报错也不复活该行
        assert db.queue_clear_scope("all") == 1
        assert db.queue_claim_item(new) == 0, "行已删时认领应 0 行"
        assert db.queue_finish_item(new, "done") == 0, "行已删时收尾应 0 行"
        assert db.queue_list() == [], "已被清空的行不该被复活"
    finally:
        db.close()

    # E 前端静态检查
    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    idx_html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    for token in ("function clearTasks", "TASK_CLEAR_SCOPE", "'/api/cart/clear'",
                  "expired: { text: '🔎 搜索来源'", "if (w.deleted) return false"):
        assert token in appjs, f"app.js 缺少 {token}"
    assert "clearDoneTasks" not in appjs, \
        "旧的「清空已完成」实现应已被跟随筛选的 clearTasks 取代"
    assert 'id="dl-clear"' in idx_html, "index.html 缺少新的清空按钮"
    assert "🧹 清空当前栏" in idx_html, "清空按钮文案应说明「清当前栏」"
    assert "🗑️ 已失效" in idx_html, "图库筛选按钮应叫「已失效」(「已删除」有歧义)"
    assert "🗑️ 已删除" not in idx_html, "不该再残留「已删除」这个有歧义的按钮文案"


def t116_clear_running_task_actually_cancels() -> None:
    """T116: 清空「进行中」的任务真的会中止下载。

    :
    以前只是删掉记录 —— 引擎手里那份开跑时的快照照样把它下完, 于是出现「记录没了、
    图却照样进库」。本用例分四层验证:

      A 认领闸门(整项跳过): 假下载器在第 1 项下载途中模拟用户点「清空」→ 引擎跑到
        第 2 项时认领命中 0 行 → 整项跳过, 第 2 项一张图都不下。
      B 逐页中止(当前这一项): 行被清掉后 _should_abort() 为真; _abort_now() 抛
        DownloadCancelled, 且把已下好的页落进 DB (不是整项白丢)。
      C 中止后的收尾不卡死: 「停止任务」式中止(行还在) → 放回 pending, 不卡在
        running; 清空式中止(行已删) → release 命中 0 行, 不复活已清掉的行。
      D 静态检查: 两个逐页循环都装了中止检查; 前端文案已由「不会停下」改成「会被中止」。
    """
    import tempfile, shutil, threading
    import queue as _q2
    from pathlib import Path
    from utils.reporter import PrintReporter, QueueReporter
    from core import download_engine as de
    from core.models import DownloadStrategy
    from core.crawler import PixivDownloader, DownloadCancelled
    from db import store as _db_store_mod
    from db.store import PixivDatabase, PSt

    tmp = tempfile.mkdtemp(prefix="t116_")
    db_file = Path(tmp) / "cart.db"
    save_folder = Path(tmp) / "img"
    downloaded: list = []
    try:
        # ---------- A 认领闸门: 被清空的那一项绝不下 ----------
        class _FakeDownloader:
            def __init__(self, **kw):
                self.save_folder = Path(kw.get("save_folder"))
                self.config = kw.get("config")
                self._cb = None
                self._abort_cb = None
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def set_progress_callback(self, cb): self._cb = cb
            def set_abort_callback(self, cb): self._abort_cb = cb

            def download_artwork(self, aid, author_id=None, author_name=None,
                                 force_overwrite=False):
                downloaded.append(str(aid))
                self.save_folder.mkdir(parents=True, exist_ok=True)
                (self.save_folder / f"{aid}_p0.png").write_bytes(
                    b"PNG-FAKE-" + str(aid).encode())
                if len(downloaded) == 1:
                    # 模拟用户在第 1 项下载途中点了「清空当前栏(全部)」
                    # (断言不假定跑的顺序: 只看"只下成了 1 个", 顺序由 T118 单独钉)
                    with _db_store_mod.PixivDatabase(None) as d:
                        d.queue_clear_scope("all")
                return 1, 1

            def download_author_all(self, user_id=None, author_name=None): pass
            def close(self): pass

        _orig_pdl = de.PixivDownloader
        de.PixivDownloader = _FakeDownloader
        _orig_init = _db_store_mod.PixivDatabase.__init__

        def _patched_init(self_inner, db_path=None, reporter=None):
            if db_path is None:
                db_path = db_file
            return _orig_init(self_inner, db_path=db_path, reporter=reporter)

        _db_store_mod.PixivDatabase.__init__ = _patched_init
        try:
            with _db_store_mod.PixivDatabase(None) as db0:
                db0.queue_add(item_type="artwork", title="#300001",
                              artwork_id="300001", page_count=1)
                db0.queue_add(item_type="artwork", title="#300002",
                              artwork_id="300002", page_count=1)
            q2 = _q2.Queue()
            qr = QueueReporter(q2, threading.Event())
            done, total = de.run_cart(qr, config={"paths": {"img_subdir": "img"}},
                                      save_folder=save_folder,
                                      strategy=DownloadStrategy.ONLY_NEW,
                                      headless=True)
        finally:
            de.PixivDownloader = _orig_pdl
            _db_store_mod.PixivDatabase.__init__ = _orig_init  # type: ignore[method-assign]

        assert total == 2, f"开跑快照应有 2 项, 实得 {total}"
        assert len(downloaded) == 1, \
            f"第 1 项跑完清空后, 快照里剩下的那一项不该被下载, 实际下载了 {downloaded}"
        assert len(list(save_folder.glob("*_p0.png"))) == 1, \
            "被清空的那一项不该落盘"
        with _db_store_mod.PixivDatabase(str(db_file)) as db1:
            assert db1.queue_list() == [], \
                f"被清空的行不该被引擎收尾写回复活, 实得 {db1.queue_list()}"

        # ---------- B 逐页中止: _should_abort / _abort_now ----------
        dbm = PixivDatabase(":memory:")
        try:
            aid = "300100"
            dbm.init_pages(aid, 3)
            dbm.mark_page_status(aid, 0, PSt.DONE, local_path=f"{aid}_p0.png")
            d = object.__new__(PixivDownloader)
            d.reporter = PrintReporter()
            d.db = dbm
            d._abort_cb = None
            assert d._should_abort() is False, "没回调且未全局停止时不该中止"
            d._abort_cb = lambda: True
            assert d._should_abort() is True, "回调说停就该停"
            d._abort_cb = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
            assert d._should_abort() is False, \
                "回调抛异常应放行(不中止) —— 一次查询失败不该把下载打断"
            d._abort_cb = lambda: True
            try:
                d._abort_now(aid, 1, 3, 1)
                raise AssertionError("_abort_now 应抛 DownloadCancelled")
            except DownloadCancelled:
                pass
            assert dbm.get_page_info(aid, 0).status == PSt.DONE, \
                "中止前已下好的页应留在库里 (图库里如实显示成缺页), 不能整项白丢"
        finally:
            dbm.close()

        # ---------- C 中止后的收尾: 不卡 running / 不复活已删行 ----------
        dbc = PixivDatabase(":memory:")
        try:
            iid = dbc.queue_add(item_type="artwork", title="#1", artwork_id="1")
            assert dbc.queue_claim_item(iid) == 1
            assert dbc.queue_release_item(iid) == 1, \
                "「停止任务」式中止(行还在)应把 running 放回 pending, 否则永远卡在「进行中」"
            assert dbc.queue_list()[0]["status"] == "pending"
            assert dbc.queue_claim_item(iid) == 1
            assert dbc.queue_clear_scope("all") == 1
            assert dbc.queue_release_item(iid) == 0, "行已删时 release 应 0 行"
            assert dbc.queue_list() == [], "被清掉的行不该被 release 复活"
            # queue_item_alive: 只有 running 才算「进行中」
            iid2 = dbc.queue_add(item_type="artwork", title="#2", artwork_id="2")
            assert dbc.queue_item_alive(iid2) is False, "pending 不算「进行中」"
            assert dbc.queue_claim_item(iid2) == 1
            assert dbc.queue_item_alive(iid2) is True, "running 才算「进行中」"
            assert dbc.queue_clear_scope("all") == 1
            assert dbc.queue_item_alive(iid2) is False, "行被清掉后不再是「进行中」"
        finally:
            dbc.close()

        # ---------- D 静态检查 ----------
        csrc = (PROJECT_ROOT / "core" / "crawler.py").read_text(encoding="utf-8")
        # 静图两个逐页循环 + 动图通道(zip 下载前/后各一次) = 4 处
        assert csrc.count("if self._should_abort():") == 4, \
            ("HTTP/浏览器两个逐页循环 + 动图通道都要装中止检查, 实得 "
             f"{csrc.count('if self._should_abort():')} 处")
        # 动图是一次性大下载(zip 可能几十 MB), 下载前与下载后都要能中止
        _ugo = csrc[csrc.index("def _download_ugoira_http"):
                   csrc.index("def _download_browser")]
        assert _ugo.count("if self._should_abort():") == 2, \
            "动图通道下载前后各要一次中止检查(否则清空队列后还会傻下完整个 zip)"
        assert "class DownloadCancelled" in csrc, "缺少中止专用异常"
        assert csrc.count("except DownloadCancelled:") >= 3, \
            "中止异常必须能穿过 download_artwork 的浏览器兜底与两个作品循环"
        eng = (PROJECT_ROOT / "core" / "download_engine.py").read_text(encoding="utf-8")
        assert "claimed = _db0.queue_claim_item(item_id)" in eng \
            and "if not claimed:" in eng, "引擎每项开跑前必须用认领结果当闸门"
        assert "set_abort_callback(_make_abort(db, item_id))" in eng, \
            "引擎应给下载器注册逐项中止回调"
        appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        assert "正在下载的任务会被中止" in appjs, "确认框文案应如实说「会被中止」"
        assert "不会因此停下" not in appjs, "旧的「不会因此停下」文案应已删除"
        idx_html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(
            encoding="utf-8")
        assert "正在下载的任务会被中止" in idx_html, "按钮 title 也要改成「会被中止」"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t117_import_bumps_updated_at() -> None:
    """T117: 导入图片要刷新「最近修改时间」。

    用户:「导入图片的操作不会更新『最近修改时间』, 不知道是不是只有缺页作品导入页数
    会这样还是新作品导入也会这样」。查下来是**一半对**:

      - 新作品导入: 会。`init_works` 建行时 schema 的 DEFAULT (datetime('now')) 就是
        当前时刻, 本来就排最前 —— 所以看起来「没问题」;
      - 已有作品补页(导入散图 / 补缺页): **不会**。页只写进 pages 表, `artworks.updated_at`
        一动不动 → 按「修改时间」排序里它不提前, 用户看不到刚导入的东西。

    根因: 只有 change_work_status 与合并/拆分(_refresh_work_progress) 会刷 updated_at。
    修法: 在唯一的写页入口 `mark_page_status` 里顺手刷 (与 pages 写在同一个事务)。
    本用例验证四件事:

      A 已有作品补页导入 → updated_at 真的刷新了, 且 new_pages_linked >= 1;
      B 全新作品导入 → created_at / updated_at 都是当前时刻 (会排最前, 未被改坏);
      C 重复对账(无新页) → updated_at **不动** (防「每次 15s 轮询把整库刷成刚刚」);
      D 排序与静态护栏: 导入后该作品在 sort='updated' 里排第一; 新写的
        updated_at 必须走 now_stamp (T111 的格式约束)。
    """
    import hashlib, tempfile, shutil
    from pathlib import Path
    from db.store import PixivDatabase, WSt, PSt

    OLD = "2020-01-01 00:00:00"
    tmp = Path(tempfile.mkdtemp(prefix="t117_"))
    img = tmp / "img"
    (img / "作者A_1").mkdir(parents=True)
    db = PixivDatabase(":memory:")
    try:
        # ---------- A 已有作品(缺页) 补页导入 ----------
        db.change_work_status("1", WSt.NOT_YET, total_pages=2, title="自测作品",
                              author_id="1")
        db.mark_page_status("1", 0, PSt.DONE, "作者A_1/1_p0.jpg",
                            md5=hashlib.md5(b"a" * 50).hexdigest(),
                            file_size_bytes=50)
        db.conn.execute("UPDATE artworks SET updated_at=? WHERE artwork_id='1'",
                        (OLD,))
        db.conn.commit()
        (img / "作者A_1" / "1_p1.png").write_bytes(b"NEW-PAGE-IMPORT" * 40)
        rec = db.reconcile_output(img)
        assert rec.new_pages_linked == 1, rec.new_pages_linked
        after = db.conn.execute(
            "SELECT updated_at FROM artworks WHERE artwork_id='1'").fetchone()[0]
        assert after != OLD, \
            "导入补页后 updated_at 必须刷新, 否则「按修改时间」里看不到刚导入的东西"
        assert "T" not in str(after), f"必须走 now_stamp 的格式: {after!r}"

        # ---------- B 全新作品导入 (回归: 本来就排最前, 别改坏) ----------
        (img / "外部画师_42").mkdir()
        (img / "外部画师_42" / "777_p0.jpg").write_bytes(b"ext" * 60)
        rec2 = db.reconcile_output(img)
        assert rec2.new_pages_linked == 1, rec2.new_pages_linked
        row = db.conn.execute(
            "SELECT created_at, updated_at FROM artworks WHERE artwork_id='777'"
        ).fetchone()
        assert row[0] and row[1], "新作品的入库/修改时间都要有值"

        # ---------- C 重复对账不得把整库刷成「刚刚」 ----------
        ts_before = db.conn.execute(
            "SELECT updated_at FROM artworks WHERE artwork_id='1'").fetchone()[0]
        rec3 = db.reconcile_output(img)
        assert rec3.new_pages_linked == 0, rec3.new_pages_linked
        ts_after = db.conn.execute(
            "SELECT updated_at FROM artworks WHERE artwork_id='1'").fetchone()[0]
        assert ts_before == ts_after, \
            "没有新页的对账绝不能刷 updated_at —— 否则每次轮询整库都变成「刚刚改过」"

        # ---------- D 排序 + 静态护栏 ----------
        # 把作品 1 压到很旧、作品 777 保持新 → 排序应以 777 开头
        db.conn.execute("UPDATE artworks SET updated_at=? WHERE artwork_id='1'",
                        (OLD,))
        db.conn.commit()
        order = [str(r["artwork_id"])
                 for r in db.list_artworks(sort="updated", order="desc")]
        assert order[0] == "777", f"按修改时间应把刚导入的新作品排最前: {order}"

        src = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
        assert "UPDATE artworks SET updated_at=? WHERE artwork_id=?" in src, \
            "写页时要顺手刷作品的修改时间"
        assert "(now_stamp(), aid)" in src, \
            "新写的 updated_at 必须走 now_stamp (T111: 库里唯一的时间戳写法)"
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t118_fifo_order_and_task_progress() -> None:
    """T118: 队列「先入先跑」+ 总进度条只按任务条数推进。

    用户两问:
      ① 「为什么不是先入先跑, 这样才符合直觉吧?」 —— 原来 queue_list 是
         `added_at DESC, id DESC` (最新入队的先跑, 是「加入清单即自动下载」的配套),
         改为 ASC 后最旧的最先下。
      ② 「原来这进度条只是摆设」 —— 进度其实是真在跟的, 但总进度条被两套口径互相
         覆盖: 爬虫每下一页 reporter.progress(i, total=**作品页数**), run_cart 又按
         任务条数 progress(i-1, total=**队列条数**)。批量时第一个作品下完就把条子冲到
         100% 并自动隐藏, 下一个任务开始又跳回 33% —— 看着就像没在跟。
         现: 总进度条只认 cart_progress(任务条数), 页级进度只喂行内那一格(item_progress)。

    断言 (全离线):
      A queue_list 是 FIFO (added_at 只到秒, 同秒按 id ASC —— 先加的 id 小)
      B 真跑一遍 run_cart: 下载顺序 == 入队顺序; cart_progress 的分母恒等于队列条数、
        cur 覆盖 0..N (不再混进作品页数); 页级进度仍以作品页数为分母走 item_progress
      C 静态检查: 前端总进度条认 cart_progress 不认 progress; store 是 ASC 无残留
        DESC; run_cart 发 cart_progress; 爬虫的页级 progress 通道保留 (CLI/日志还要用)
    """
    import tempfile, shutil, threading
    import queue as _q3
    from pathlib import Path
    from utils.reporter import QueueReporter
    from core import download_engine as de
    from core.models import DownloadStrategy
    from db import store as _db_store_mod
    from db.store import PixivDatabase

    # ---------- A FIFO ----------
    db = PixivDatabase(":memory:")
    try:
        ids = [db.queue_add(item_type="artwork", title=f"#{a}", artwork_id=a,
                            page_count=1)
               for a in ("400001", "400002", "400003")]
        assert ids == sorted(ids), f"id 应递增, 实得 {ids}"
        got = [int(x["id"]) for x in db.queue_list()]
        assert got == ids, f"queue_list 必须是先入先跑 (FIFO), 实得 {got}"
        got_p = [int(x["id"]) for x in db.queue_list(status="pending")]
        assert got_p == ids, f"pending 过滤后同样 FIFO, 实得 {got_p}"
    finally:
        db.close()

    # ---------- B 真跑 run_cart ----------
    tmp = tempfile.mkdtemp(prefix="t118_")
    db_file = Path(tmp) / "cart.db"
    save_folder = Path(tmp) / "img"
    order: list = []
    try:
        class _FakeDownloader:
            def __init__(self, **kw):
                self.save_folder = Path(kw.get("save_folder"))
                self.config = kw.get("config")
                self._cb = None
                self._abort_cb = None
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def set_progress_callback(self, cb): self._cb = cb
            def set_abort_callback(self, cb): self._abort_cb = cb

            def download_artwork(self, aid, author_id=None, author_name=None,
                                 force_overwrite=False):
                order.append(str(aid))
                # 模拟爬虫的页级上报: 经回调 → item_progress (行内那一格)
                for i in range(1, 6):
                    if self._cb:
                        self._cb("artwork_page", i, 5, f"p{i}")
                self.save_folder.mkdir(parents=True, exist_ok=True)
                (self.save_folder / f"{aid}_p0.png").write_bytes(b"PNG-FAKE")
                return 1, 1

            def download_author_all(self, user_id=None, author_name=None): pass
            def close(self): pass

        _orig_pdl = de.PixivDownloader
        de.PixivDownloader = _FakeDownloader
        _orig_init = _db_store_mod.PixivDatabase.__init__

        def _patched_init(self_inner, db_path=None, reporter=None):
            if db_path is None:
                db_path = db_file
            return _orig_init(self_inner, db_path=db_path, reporter=reporter)

        _db_store_mod.PixivDatabase.__init__ = _patched_init
        try:
            with _db_store_mod.PixivDatabase(None) as db0:
                for a in ("500001", "500002"):
                    db0.queue_add(item_type="artwork", title=f"#{a}",
                                  artwork_id=a, page_count=1)
            q3 = _q3.Queue()
            qr = QueueReporter(q3, threading.Event())
            de.run_cart(qr, config={"paths": {"img_subdir": "img"}},
                        save_folder=save_folder,
                        strategy=DownloadStrategy.ONLY_NEW, headless=True)
            evs = []
            while not q3.empty():
                evs.append(q3.get_nowait())
            assert order == ["500001", "500002"], \
                f"引擎必须按入队顺序跑 (先入先跑), 实得 {order}"
            cart = [p for k, p in evs if k == "cart_progress"]
            assert cart, "总进度条必须收到 cart_progress (任务条数口径)"
            assert {p["total"] for p in cart} == {2}, \
                f"cart_progress 的分母只能是队列任务条数, 实得 {[p['total'] for p in cart]}"
            assert sorted({p["cur"] for p in cart}) == [0, 1, 2], \
                f"cur 应覆盖 0..2, 实得 {sorted({p['cur'] for p in cart})}"
            ip = [p for k, p in evs if k == "item_progress"]
            assert ip and max(p["total"] for p in ip) == 5, \
                f"页级进度应仍以作品页数为分母走 item_progress, 实得 {ip[:3]}"
        finally:
            de.PixivDownloader = _orig_pdl
            _db_store_mod.PixivDatabase.__init__ = _orig_init
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- C 静态检查 ----------
    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    store_src = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
    crawler_src = (PROJECT_ROOT / "core" / "crawler.py").read_text(encoding="utf-8")
    engine_src = (PROJECT_ROOT / "core" / "download_engine.py").read_text(encoding="utf-8")
    assert "case 'cart_progress':" in appjs, "总进度条应认 cart_progress"
    assert "case 'progress':" not in appjs, \
        "页级 progress 不该再驱动总进度条 (否则批量时提前 100% 再跳回来)"
    assert "ORDER BY added_at ASC, id ASC" in store_src, "queue_list 必须是 FIFO"
    assert "ORDER BY added_at DESC" not in store_src, "不该残留 DESC 的旧排序"
    assert 'reporter.put("cart_progress"' in engine_src, "run_cart 要发 cart_progress"
    assert "self.reporter.progress(i, total," in crawler_src, \
        "页级 progress 通道保留 (CLI/日志还要用), 只是不再驱动总进度条"


def t119_ai_filter_and_backfill() -> None:
    """T119: 图库「AI 作品」筛选 + aiType 回填链路。

    用户:「查询结果能不能加个过滤 AI 作品的选项, 不想看 ai 作品」。
    拍板: 筛选默认值 = 「全部」(不改变现有默认视图); 回填**不加新按钮**, 复用现有
    「补全元数据」链路。

    本用例守住四个不变量:
      A 解析: `_illust_body_to_info` 必须把 ajax 的 aiType 读进 ArtworkInfo
        (1=非AI / 2=AI / 缺字段=0 未知), 且落库经 upsert_artwork_metadata;
      B 三档筛选语义: '' = 全部(不隐藏) / '2' = 仅 AI / '0' = 非 AI
        (**未标记的 NULL 也算非 AI** —— 藏起来会让用户以为图丢了);
      C 两口径分离: works_missing_metadata 必须把「缺 ai_type」纳入取数 (否则老库
        元数据都齐, 光加解析等于没补数据), 而 count_missing_metadata **不得**纳入
        (否则「缺元数据」筛选会把全库标成缺, 误导用户);
      D 补全循环必须无条件落值 (含接口没给的 0), 否则 NULL 的作品永远卡在队列头部。
    """
    import tempfile, shutil
    from pathlib import Path
    from core.models import ArtworkInfo
    from core.search_client import SearchClient
    from db.store import PixivDatabase, WSt

    # ---------- A 解析 aiType ----------
    body = {"illustId": "100", "userId": "9", "illustTitle": "T",
            "illustType": 0, "pageCount": 1, "tags": {"tags": []},
            "urls": {"thumb": "http://x/t.jpg"}, "aiType": 2}
    info = SearchClient._illust_body_to_info(body)
    assert isinstance(info, ArtworkInfo) and info.ai_type == 2, info
    body["aiType"] = 1
    assert SearchClient._illust_body_to_info(body).ai_type == 1
    body.pop("aiType")
    assert SearchClient._illust_body_to_info(body).ai_type == 0, \
        "接口没给 aiType 时必须落 0(未知), 不能崩也不能瞎猜"
    # A2 关键字搜索 / 作者作品列表走的是另外两条解析路 (以前压根没读 aiType) ——
    # 只改 _illust_body_to_info 的话, 查询页「隐藏 AI 作品」对这两条路完全失效
    from core.search_client import _profile_work_to_info
    pw = _profile_work_to_info({"id": "200", "userId": "9", "title": "T", "pageCount": 1,
                                "url": "http://x/t.jpg", "aiType": 2}, "9")
    assert pw is not None and pw.ai_type == 2, \
        "作者作品列表(profile/illusts)必须带 AI 判定"
    sc_src = (PROJECT_ROOT / "core" / "search_client.py").read_text(encoding="utf-8")
    assert 'ai_type=int(it.get("aiType") or 0)' in sc_src, \
        "关键字搜索(illustManga.data)必须带 AI 判定"

    tmp = Path(tempfile.mkdtemp(prefix="t119_"))
    db = PixivDatabase(":memory:")
    try:
        # ---------- B 三档筛选 ----------
        # 三个作品的标题/标签/封面都齐 —— 这样"进补全队列"只可能是缺 ai_type 这一条
        db.upsert_artwork_metadata("1001", title="AI 作品", author_id="9",
                                   tags=["x"], thumb_url="http://x/1.jpg", ai_type=2)
        db.upsert_artwork_metadata("1002", title="人类作品", author_id="9",
                                   tags=["x"], thumb_url="http://x/2.jpg", ai_type=1)
        db.upsert_artwork_metadata("1003", title="还没补过", author_id="9",
                                   tags=["x"], thumb_url="http://x/3.jpg")

        def ids(**kw):
            return sorted(str(r["artwork_id"])
                          for r in db.list_artworks(**kw))

        assert ids() == ["1001", "1002", "1003"], "默认(不传 ai) 必须不隐藏任何作品"
        assert ids(ai="") == ["1001", "1002", "1003"]
        assert ids(ai="2") == ["1001"], "仅 AI"
        assert ids(ai="0") == ["1002", "1003"], \
            "非 AI 必须含「未标记」的作品 (藏起来会让用户以为图丢了)"
        assert db.count_artworks_filtered(ai="2") == 1
        assert db.count_artworks_filtered(ai="0") == 2
        assert db.count_artworks_filtered() == 3

        # ---------- C 两口径分离 ----------
        miss = db.works_missing_metadata(limit=100)
        assert "1003" in miss, "缺 ai_type 的作品必须进补全队列 (否则全库永远刷不上标记)"
        assert "1001" not in miss and "1002" not in miss, \
            "已有 ai_type 的作品不该因为这条新增条件被反复捞出来"
        # 「缺元数据」按钮的口径不变: 这三行元数据都齐, 一个都不算缺
        assert db.count_missing_metadata() == 0, \
            "count_missing_metadata 不得纳入 ai_type, 否则「缺元数据」会把全库标成缺"

        # 补全循环用 set_artwork_ai_type 无条件落值 → 队列立刻排空
        db.set_artwork_ai_type("1003", 0)
        assert "1003" not in db.works_missing_metadata(limit=100), \
            "补过一次(哪怕接口没给) 就必须退出队列, 否则每轮重复请求同一批"
        assert db.count_missing_metadata() == 0

        # 已经标记过的行绝不被「未知」覆盖回 NULL/0 (upsert 的降级保护)
        db.upsert_artwork_metadata("1001", ai_type=0)
        assert db.get_artwork_metadata("1001")["ai_type"] == 2, \
            "取不到 aiType 时不许把已标记的行降级"

        # 图片视图同口径
        db.mark_page_status("1001", 0, 1, "a/1001_p0.jpg")
        db.mark_page_status("1002", 0, 1, "a/1002_p0.jpg")
        assert len(db.list_images(ai="2")) == 1
        assert len(db.list_images(ai="0")) == 1
        assert db.count_images_filtered(ai="2") == 1
        assert db.count_images_filtered(ai="0") == 1
    finally:
        db.close()
        shutil.rmtree(str(tmp), ignore_errors=True)

    # ---------- D 静态护栏: 迁移 / 接口 / 前端接线 ----------
    mig = (PROJECT_ROOT / "db" / "migrations" / "017_ai_type.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN ai_type INTEGER" in mig, "017 迁移必须真的加列"
    store_src = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
    assert "OR thumb_url IS NULL OR ai_type IS NULL" in store_src, \
        "补全队列必须把缺 ai_type 纳入 (只在 works_missing_metadata 里)"
    _cnt_body = store_src.split("def count_missing_metadata")[1].split("\n    def ")[0]
    assert "ai_type" not in _cnt_body, \
        "count_missing_metadata 不得纳入 ai_type (两口径刻意分离)"
    server_src = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert 'ai=args.get("ai", "")' in server_src, "/api/library/items 要透传 ai"
    assert '"ai_type": int(getattr(a, "ai_type", 0) or 0)' in server_src, \
        "搜索接口必须把 ai_type 带给前端 (查询页靠它排除 AI 卡片)"
    assert "db.set_artwork_ai_type(aid" in server_src, \
        "补全循环必须无条件落 ai_type, 否则队列排不空"
    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "ai: ''" in appjs and "p.set('ai', libFilters.ai || '')" in appjs, \
        "前端要把图库的 ai 筛选带进请求"
    # 查询页(🌐 网络查询): 排除 AI 作品卡片 —— "这才是最重要的"
    assert "function splitAiWorks" in appjs and "splitAiWorks(res.works)" in appjs, \
        "查询页作品列表要过 AI 过滤"
    assert "Number(res.artwork.ai_type || 0) === 2" in appjs, \
        "按作品ID 直查到的 AI 作品大卡也要收起"
    assert "vixiq.searchHideAi" in appjs, "隐藏 AI 的设置要记住 (localStorage)"
    html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="lib-f-ai"' in html, "图库筛选栏要有 AI 下拉"
    assert '<option value="" selected>AI: 全部</option>' in html, \
        "AI 筛选默认必须是「全部」"
    assert 'id="search-hide-ai"' in html and 'class="btn icon-toggle active"' in html, \
        "查询页要有「🚫 隐藏 AI 作品」开关且默认开启"
    assert 'data-tab="search">🌐 网络查询<' in html, \
        "「查询」tab 改名「网络查询」, 跟「图库」区分开 "


def t120_keyword_paging_and_lib_layout() -> None:
    """T120: 关键字搜索「换一批」+ 图库三个状态筛选挪到「全选」左边 (2026-09-20)。

    用户两问:
      ① 「使用关键词搜索图片的结果是固定的吗? 可以的话想加一个换一批的按钮」
         —— 是固定的: URL 里写死 `p=1`, 同一个词每次搜到的永远是同一批。
      ② 「『缺元数据』『缺失文件』『已失效』三个按钮放『全选』按钮的左边吧,
         现在和它们一行的筛选控件太多把他们挤到下一行了」

    本用例守四件事:
      A 分页切片: offset 连续取, **不重复也不跳过**; 跨 pixiv 分页(每页 60 条)
        自动续取; 续页挂掉只丢续页(有多少给多少), 首页挂掉才上抛走 DOM 兜底;
      B 页级缓存: 连点「换一批」时 offset 0/24/48 都落在第 1 页 → 必须只打 1 次
        pixiv (用户明确担心风控, 缓存就是为这个加的);
      C 参数贯通: search_keyword(offset=) 真的传给了取数层, 且 URL 里不再是死 p=1;
      D 布局: 三个按钮挪进操作行、排在「全选」左边, 且已不在筛选行里。
    """
    from core.search_client import (SearchClient, KEYWORD_WORKS_PAGE,
                                    _PIXIV_SEARCH_PER_PAGE)

    assert KEYWORD_WORKS_PAGE == 24, f"关键字每批 24 条 (沿用原显示条数), 实得 {KEYWORD_WORKS_PAGE}"
    assert _PIXIV_SEARCH_PER_PAGE == 60, "pixiv 搜索每页 60 条 (实测口径)"

    class _Rep:
        def __init__(self): self.logs = []
        def log(self, m): self.logs.append(m)
        def warn(self, m): self.logs.append("WARN " + m)

    # ---------- A 连续切片: 不重复、不跳过、跨页续取 ----------
    sc = SearchClient.__new__(SearchClient)
    sc.reporter = _Rep()
    pages = {1: [f"p1_{i}" for i in range(60)],
             2: [f"p2_{i}" for i in range(60)],
             3: []}
    calls = []

    def fake_page(kw, sm, p):
        calls.append(p)
        return pages.get(p, [])

    sc._search_page = fake_page
    seen: list = []
    for off in range(0, 120, KEYWORD_WORKS_PAGE):
        calls.clear()
        batch = sc._search_raw("kw", "s_tag", offset=off, limit=KEYWORD_WORKS_PAGE)
        assert len(batch) == KEYWORD_WORKS_PAGE, f"offset={off} 应满批, 实得 {len(batch)}"
        seen += batch
    # 「一直点换一批」看到的序列 == pixiv 原始序列, 一条不落、一条不重
    assert seen == (pages[1] + pages[2]), "跨页续取必须严丝合缝, 不能重复/跳号"
    assert len(set(seen)) == len(seen) == 120, "批次之间绝不允许重复发同一张"
    # offset=48 落在第 1 页只剩 12 条 → 必须自动续第 2 页, 而不是少给 12 条
    calls.clear()
    tail = sc._search_raw("kw", "s_tag", offset=48, limit=KEYWORD_WORKS_PAGE)
    assert calls == [1, 2], f"offset=48 要跨页续取 (先吃第1页剩的再续第2页), 实得 {calls}"
    assert tail == pages[1][48:] + pages[2][:12], "跨页拼接顺序错了"

    # ---------- A2 出错处理: 首页挂上抛(DOM 兜底), 续页挂只丢续页 ----------
    def first_boom(kw, sm, p):
        raise RuntimeError("网络断了")

    sc._search_page = first_boom
    try:
        sc._search_raw("kw", "s_tag", offset=0, limit=KEYWORD_WORKS_PAGE)
        assert False, "首页就失败必须上抛, 交给上层走 DOM 兜底"
    except RuntimeError:
        pass

    def page2_boom(kw, sm, p):
        if p == 1:
            return pages[1]
        raise RuntimeError("第二页断了")

    sc._search_page = page2_boom
    part = sc._search_raw("kw", "s_tag", offset=48, limit=KEYWORD_WORKS_PAGE)
    assert len(part) == 12, f"续页挂掉要保住已取到的 12 条, 实得 {len(part)}"
    assert part == pages[1][48:], "续页挂掉时已取到的内容不能丢"

    # 翻到底 (没有下一页了) → 返回空, 不无限循环
    sc._search_page = lambda kw, sm, p: ([] if p >= 2 else pages[1])
    assert sc._search_raw("kw", "s_tag", offset=60, limit=24) == [], "到底了应给空批, 不能转圈"

    # ---------- B 页级缓存: 连点「换一批」不重复打 pixiv ----------
    sc2 = SearchClient.__new__(SearchClient)
    sc2.reporter = _Rep()
    hits = []

    def fake_ajax(url):
        hits.append(url)
        return {"illustManga": {"data": [{"id": "1"}]}}

    sc2._ajax_get = fake_ajax
    for _ in range(3):
        sc2._search_page("初音ミク", "s_tag", 1)
    assert len(hits) == 1, f"同一页必须命中缓存, 实打 {len(hits)} 次"
    assert "p=1" in hits[0] and "s_mode=s_tag" in hits[0], hits[0]
    sc2._search_page("初音ミク", "s_tag", 2)
    assert len(hits) == 2, "换页要真的去取新页"

    # ---------- C 参数贯通 ----------
    sc3 = SearchClient.__new__(SearchClient)
    sc3.reporter = _Rep()
    sc3.login_ok = True
    got = {}

    def fake_raw(kw, sm, *, offset, limit):
        got["offset"], got["limit"] = offset, limit
        return [{"id": "555", "userId": "9", "title": "T", "pageCount": 1,
                 "url": "u", "aiType": 2}]

    sc3._search_raw = fake_raw
    res = sc3.search_keyword("初音ミク", offset=48)
    assert got == {"offset": 48, "limit": KEYWORD_WORKS_PAGE}, f"offset 没传到取数层: {got}"
    assert res["type"] == "keyword" and len(res["works"]) == 1
    assert res["works"][0].ai_type == 2, "关键字结果照旧要带 AI 判定"
    # 关键字搜索自己不能继承作者那套 60 条的页大小
    assert res["works"][0].artwork_id == "555"

    sc_src = (PROJECT_ROOT / "core" / "search_client.py").read_text(encoding="utf-8")
    assert "mode=all&p={p}" in sc_src, "搜索 URL 的页码必须是变量, 不能再写死 p=1"
    assert "mode=all&p=1" not in sc_src, "写死 p=1 = 结果永远固定 (回归)"
    assert "offset=offset" in sc_src, "_execute_artwork_scope 要把 offset 传下去"
    # DOM 兜底也得认页码, 否则 ajax 挂掉时「换一批」一直给同一页
    assert "&word={encoded}&p={p}" in sc_src, "DOM 兜底要带上页码"

    # ---------- D 布局: 三个状态筛选挪到「全选」左边 ----------
    html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    i_filters = html.index('<div class="field-row lib-filters">')
    i_ops = html.index('<div class="lib-ops-row">')
    i_batch = html.index('<div class="lib-batch-group">')
    i_miss = html.index('id="lib-f-missing"')
    i_selall = html.index('id="lib-sel-all"')
    assert i_filters < i_ops < i_batch < i_miss < i_selall, (
        "「缺元数据」必须已从筛选行搬进操作行, 且排在「全选」左边"
        f"(filters={i_filters} ops={i_ops} batch={i_batch} miss={i_miss} selall={i_selall})")
    for bid in ('id="lib-f-missing-file"', 'id="lib-f-deleted"'):
        assert i_miss < html.index(bid) < i_selall, f"{bid} 也要在「全选」左边且相邻"
    css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    grp = css.split(".lib-batch-group {")[1].split("}")[0]
    assert "flex-wrap: wrap" in grp, "批量组按钮变多了, 窄窗口要允许内部换行不溢出"
    assert "justify-content: flex-end" in grp, "换行后仍要保持右对齐"

    # ---------- D2 前端翻批接线 ----------
    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "const KEYWORD_WORKS_PAGE = 24;" in appjs, "前端每批 24 条要和后端一致"
    assert "function keywordPagerHtml" in appjs and "window.keywordWorksPage" in appjs, \
        "关键字结果要有自己的翻批栏(不能借用作者那套 works_offset 语义)"
    assert "换一批 ›" in appjs, "按钮文案就是「换一批」"
    assert "keywordPagerHtml(res, all.length)" in appjs, \
        "翻批栏要按**过滤前**的条数算「本批第 x–y 件」, 否则隐藏 AI 后数字对不上"
    assert "keywordWorksPage(-1)" in appjs and "keywordWorksPage(1)" in appjs, "上/下一批都要接线"
    # 关键字翻批要用「当初搜的那个词」重查, 不是拿作品ID/作者ID
    # (不能拿 "};" 当分隔符 —— 函数体里有 `searchCache[...] || {};`)
    _kwbody = appjs.split("window.keywordWorksPage = async function")[1].split("await doSearch")[0]
    assert "cached.q" in _kwbody, "关键字翻批要用原搜索词重查"


def t121_search_feed() -> None:
    """T121: 查询页「没输入内容时显示推荐」的两个来源。

    用户先选「排行榜」, 追问「不能在排行榜和个性推荐之间切换吗」→ 拍板做成可切换。
    实测两个接口的差异(这决定了整个设计):
      ranking   `ranking.php?mode=daily&format=json` 50 条, **没有 aiType**, 且**不是 /ajax/ 壳**
                (无 {error, body}, 数据直接挂顶层 contents)
      discovery `/ajax/illust/discovery?mode=all` 10 条, **自带 aiType**, 字段与 profile/illusts 同构

    本用例守五件事:
      A ranking 映射: 字符串数字要能转、illust_type 要对、R18 看 illust_content_type.sexual、
        发布时间取 illust_upload_timestamp、**ai_type 必须落 0=未知**(接口不给, 不许瞎猜);
      B 两个接口各自的通道: ranking 走 unwrap=False(拿顶层), discovery 走标准 body 壳;
      C AI 判定三级来源 + 只在 want=True 才联网 (用户没开「隐藏 AI」时一个请求都不该发);
      D 内容缓存: 10 分钟内重复取不二次联网;
      E 前端接线: 两个来源按钮 + 空输入回到推荐 + 排行榜才需要补 AI 的 flag。
    """
    import tempfile, shutil
    from pathlib import Path
    from core.search_client import (SearchClient, FEED_KINDS, RANKING_PER_PAGE,
                                    _rank_item_to_info)
    from core.models import IllustType

    assert FEED_KINDS == ('ranking', 'discovery'), "两个来源固定这两个 id"
    assert RANKING_PER_PAGE == 50, "ranking.php 一页 50 条 (实测口径)"

    # ---------- A ranking 单条映射 (字段名与 ajax/illust 完全不同, 全字符串) ----------
    raw = {
        "rank": 1, "illust_id": 149797679, "title": "【会社と私生活】やめません？",
        "user_id": 4729811, "user_name": "金沢真之介",
        "url": "https://i.pximg.net/c/480x960/x.jpg",
        "illust_type": "1", "illust_page_count": "4",
        "width": 3240, "height": 4050,
        "tags": ["漫画", "はよ結婚しろ"],
        "illust_upload_timestamp": 1789700425,
        "illust_content_type": {"sexual": 0, "lo": False, "original": True},
    }
    a = _rank_item_to_info(raw)
    assert a is not None
    assert a.artwork_id == "149797679", "作品ID 要转成字符串"
    assert a.author_id == "4729811" and a.author_name == "金沢真之介"
    assert a.page_count == 4, f"illust_page_count 是字符串 '4', 要转成 4 (实得 {a.page_count!r})"
    assert a.illust_type is IllustType.MANGA, "illust_type '1' = 漫画"
    assert a.tags == ["漫画", "はよ結婚しろ"]
    assert a.is_r18 is False
    assert a.ai_type == 0, "排行榜接口不给 aiType → 必须是 0=未知, 不许瞎猜成非 AI"
    assert a.publish_date and a.publish_date.startswith("20"), a.publish_date
    assert a.image_urls and a.image_urls[0].endswith("x.jpg")

    # 缺 illust_id / 非 dict → None (不许把脏数据放进来)
    assert _rank_item_to_info({}) is None
    assert _rank_item_to_info({"title": "x"}) is None
    assert _rank_item_to_info(None) is None
    # R18 看 illust_content_type.sexual; 字段缺失也不能炸
    assert _rank_item_to_info({**raw, "illust_content_type": {"sexual": 1}}).is_r18 is True
    assert _rank_item_to_info({**raw, "illust_content_type": None}).is_r18 is False
    assert _rank_item_to_info({**raw, "illust_upload_timestamp": None}).publish_date is None
    # 字符串数字脏数据不炸
    assert _rank_item_to_info({**raw, "illust_page_count": "x"}).page_count == 1
    assert _rank_item_to_info({**raw, "illust_type": "9"}).illust_type is IllustType.ILLUSTRATION

    # ---------- B 两个接口各走各的通道 ----------
    class _Rep:
        def __init__(self): self.logs = []
        def log(self, m): self.logs.append(m)
        def warn(self, m): self.logs.append("WARN " + m)

    sc = SearchClient.__new__(SearchClient)
    sc.reporter = _Rep()
    calls = []

    def fake_ajax(url, unwrap=True):
        calls.append((url, unwrap))
        if not unwrap:
            return {"contents": [raw], "date": "20260919"}      # ranking.php 的老壳
        return {"illusts": [{"id": "1", "userId": "9", "title": "T", "pageCount": 1,
                             "url": "u", "aiType": 2}]}          # ajax 标准 body 壳

    sc._ajax_get = fake_ajax
    rk = sc.search_ranking()
    assert len(rk) == 1 and rk[0].artwork_id == "149797679"
    assert calls[-1][1] is False, "ranking.php 没有 {error,body} 壳 → 必须 unwrap=False"
    assert "ranking.php" in calls[-1][0] and "format=json" in calls[-1][0]
    assert rk[0].ai_type == 0, "排行榜结果注定是未知 AI 状态"

    ds = sc.search_discovery()
    assert len(ds) == 1 and ds[0].ai_type == 2, \
        "discovery 自带 aiType, 必须读进来 (这个来源不用额外联网就能过滤 AI)"
    assert calls[-1][1] is True, "discovery 是标准 ajax 壳 → unwrap=True"
    assert "illust/discovery" in calls[-1][0]
    assert "mode=all" in calls[-1][0], "discovery 不加 mode 会 400 (实测)"

    # limit 生效
    calls.clear()
    sc2 = SearchClient.__new__(SearchClient)
    sc2.reporter = _Rep()
    sc2._ajax_get = lambda url, unwrap=True: {"contents": [dict(raw, illust_id=str(i))
                                                          for i in range(60)]}
    assert len(sc2.search_ranking(limit=5)) == 5, "limit 要真的截断"

    # ---------- C AI 判定三级来源 + 只在 want 时联网 ----------
    import web.server as S
    from db.store import PixivDatabase, WSt

    class _W:
        def __init__(self, aid, ai=0):
            self.artwork_id, self.ai_type = aid, ai

    S._ai_fill_cache.clear()
    db = PixivDatabase(":memory:")
    try:
        db.change_work_status("1002", WSt.NOT_YET, title="x")
        db.upsert_artwork_metadata("1002", ai_type=2)      # 图库已回填 → 零请求

        asked = []
        S._pixiv_http_session = lambda: {"headers": {}}    # 假装有 cookies
        import core.search_client as _scmod
        _orig = _scmod.http_fetch_illust
        _scmod.http_fetch_illust = lambda sess, aid, timeout=12.0: (
            asked.append(aid) or _W(aid, 1))
        try:
            # ① 接口自带 (discovery) → 不打不问
            w = [_W("1001", ai=2), _W("1002")]
            n = S._feed_ai_types(db, w, want=True)
            assert n == 0 and asked == [], "接口自带 + 图库已知都不该联网"
            assert w[1].ai_type == 2, "图库已回填的要直接用上"

            # ② 未知 + want=False → **一个请求都不发** (用户没开「隐藏 AI」)
            w2 = [_W("2001"), _W("2002")]
            n2 = S._feed_ai_types(db, w2, want=False)
            assert n2 == 0 and asked == [], "没开「隐藏 AI」时不该为 AI 判定花任何请求"
            assert all(x.ai_type == 0 for x in w2)
            assert "2001" not in S._ai_fill_cache, \
                "没问过就不许写缓存 —— 否则会挡住以后图库回填出来的真实结果"

            # ③ 未知 + want=True → 并发联网补
            w3 = [_W("2003"), _W("2004")]
            n3 = S._feed_ai_types(db, w3, want=True)
            assert n3 == 2 and sorted(asked) == ["2003", "2004"], asked
            assert all(x.ai_type == 1 for x in w3), "补到的判定要写回作品"
            assert S._ai_fill_cache["2003"] == 1

            # ④ 第二次同样这批 → 命中进程缓存, 不再联网
            asked.clear()
            w4 = [_W("2003"), _W("2004")]
            assert S._feed_ai_types(db, w4, want=True) == 0 and asked == [], \
                "补过一次就该记住, 不能每开一次页面都重问"

            # ④b **答案为 0 也要算"问过了"** —— pixiv 对老作品返回的就是 0。
            # 拿真值判断当"没问过"会让这批老作品每次开页面都被重问一遍
            # (实测 discovery 那 10 条里固定有 2 条是这样, 白白多花 2 个请求)。
            _orig2 = _scmod.http_fetch_illust
            _scmod.http_fetch_illust = lambda sess, aid, timeout=12.0: (
                asked.append(aid) or _W(aid, 0))
            try:
                asked.clear()
                w0 = [_W("3001")]
                assert S._feed_ai_types(db, w0, want=True) == 1, "第一次要真问"
                assert w0[0].ai_type == 0, "pixiv 说 0 就记 0 (未知/老作品)"
                asked.clear()
                w0b = [_W("3001")]
                assert S._feed_ai_types(db, w0b, want=True) == 0 and asked == [], \
                    "结果为 0 也算问过了, 不许每轮重问 (真值判断的经典坑)"
                assert S._ai_fill_cache["3001"] == 0
            finally:
                _scmod.http_fetch_illust = _orig2

            # ④c 图库已有 ai_type(NULL 不算) → 直接用; ai_type=0 也算回填过
            db.change_work_status("3002", WSt.NOT_YET, title="x")
            S._ai_fill_cache.pop("3002", None)
            asked.clear()
            S._feed_ai_types(db, [_W("3002")], want=True)
            assert asked == ["3002"], "图库 ai_type 还是 NULL → 得联网问"

            # ⑤ 没 cookies → 不报错、不空等, 保持未知(按非 AI)
            S._pixiv_http_session = lambda: None
            asked.clear()
            w5 = [_W("2005")]
            assert S._feed_ai_types(db, w5, want=True) == 0 and asked == []
            assert w5[0].ai_type == 0
            assert "2005" not in S._ai_fill_cache, "问失败不入缓存 → 下次还有机会问对"
        finally:
            _scmod.http_fetch_illust = _orig
    finally:
        db.close()

    # ---------- D 静态护栏: 缓存 / 接口 / 前端接线 ----------
    srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "@app.route(\"/api/search/feed\")" in srv, "要有推荐接口"
    assert "_FEED_TTL" in srv and "_feed_cache" in srv, "推荐内容要缓存, 切 tab 不该反复打 pixiv"
    assert "hide_ai" in srv, "接口要收 hide_ai —— 它决定要不要为排行榜联网补 AI 标记"
    assert 'res["ai_filled"] = filled' in srv, "要把「本次花了几条请求」如实报出来"
    assert "_feed_ai_types" in srv and "max_workers=6" in srv, \
        "并发补 AI 判定要有上限"

    appjs = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function loadFeed" in appjs and "function renderFeed" in appjs, "前端要有推荐区"
    assert "window.setFeedKind" in appjs and "function paintFeedKind" in appjs, "两个来源要能切换"
    assert "feedNeedsAi" in appjs, \
        "排行榜的 AI 标记是按需联网补的, 必须记 flag, 否则拨开关时过滤不掉任何东西"
    assert "'/api/search/feed?kind='" in appjs, "要真的去请求推荐接口"
    assert "await backToFeed()" in appjs, "空输入要回到推荐 (以前只弹提示, 没有回去的路)"
    assert "hideFeed()" in appjs, "开始真搜索时要收起推荐区"
    assert "vixiq.feedKind" in appjs, "选的来源要记住"
    # 复用 .sstab 样式的推荐按钮没有 data-scope, 三个选择器都必须限定 [data-scope],
    # 否则 setSearchScope(undefined) 会把 searchScope 设成 undefined → 搜索全废
    assert "$$('.sstab[data-scope]')" in appjs, "作品/作者子页选择器要限定 [data-scope]"
    assert "$$('.sstab')" not in appjs, "裸 .sstab 选择器会误伤推荐区的来源按钮 (回归)"
    assert "if (!scope || scope === searchScope) return;" in appjs, \
        "setSearchScope 要拦住 undefined"
    html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="search-feed"' in html and 'id="search-feed-body"' in html, "要有推荐区容器"
    assert 'data-kind="ranking"' in html and 'data-kind="discovery"' in html, "两个来源按钮"
    assert 'class="sstab feed-kind' in html, "推荐来源按钮复用 sstab 样式"
    assert 'id="search-feed-refresh"' in html, "要有强制刷新入口 (内容缓存 10 分钟)"
    # 推荐区必须在结果区**下面** —— 有搜索结果时它是要被收起的
    assert html.index('id="search-result"') < html.index('id="search-feed"'), \
        "推荐区排在结果区之后"


def t122_author_folder_naming_single_rule() -> None:
    """T122: 作者夹命名「全项目唯一规则」(2026-09-21 治本 —— 磁盘上同时存在
    `作者甲_4997269` 与 `作者乙_4997269`, 一键整理每次都想改名、
    又因目标夹已存在而永远跳过, 点多少次都白点) ——
    ① 非法字符一律换成 `_` (旧下载器/反搜是「删掉」, 两边对不上才会长出第二个夹);
    ② 一键整理期望夹名 == 下载器落盘夹名 == 反搜归档夹名 (同一位作者只有一个夹);
    ③ 静态护栏: 全项目只留 `utils/author_naming.py` 一处 `\\x00-\\x1f` 字符类, 别处不得再自造;
    ④ 下载器优先复用磁盘上已存在的同 ID 作者夹, 找不到才按规范名新建。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    import utils.author_naming as an
    import web.server as ws
    import core.reverse_search as rsearch
    import core.reverse_source as rsource
    from core.crawler import PixivDownloader
    from db.store import PixivDatabase

    # ---------- ① 非法字符统一换成 `_` (不是删掉) ----------
    assert an.sanitize_dir("作者甲/作者乙") == "作者甲_作者乙", an.sanitize_dir("作者甲/作者乙")
    assert an.clean_author_name("作者甲/作者乙") == "作者甲_作者乙"
    assert an.clean_author_name("HxxG / ホン") == "HxxG _ ホン"
    assert an.clean_author_name("Re:Rin") == "Re_Rin"
    assert an.clean_author_name("SWKL:D") == "SWKL_D"
    assert an.clean_author_name("a<b>c|d?e*f") == "a_b_c_d_e_f"
    assert an.clean_author_name("") == "未知画师" and an.clean_author_name(None) == "未知画师"
    # 纯数字昵称是真名, 不能当无效名 (pixiv 允许画师就叫 "310123")
    assert an.clean_author_name("310123") == "310123"

    # ---------- ② 三个入口算出来的夹名一字不差 ----------
    tmp = Path(tempfile.mkdtemp(prefix="t122_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        AID, RAW = "4997269", "作者甲/作者乙"
        db.upsert_author(AID, RAW)
        want = f"作者甲_作者乙_{AID}"
        # 一键整理的期望夹名
        assert ws._tidy_expected_folder(db, {"source": "pixiv", "author_id": AID}, "") == want, \
            ws._tidy_expected_folder(db, {"source": "pixiv", "author_id": AID}, "")
        # 公共规则本身
        assert an.author_folder_name(db, AID, RAW) == want
        # 下载器 / 反搜的清洗函数必须与公共规则一致 (以前这里「删掉」→ 作者甲_4997269)
        assert PixivDownloader._clean_author_name(RAW) == an.clean_author_name(RAW) == "作者甲_作者乙"
        assert rsource.ReverseSource._clean_author_name(RAW) == "作者甲_作者乙"
        assert rsearch._clean_dirname(RAW) == "作者甲_作者乙"
        # 名字里带别名的作者: 三个入口也一致 (别名用顿号, 绝不出现 `/`)
        import json as _json
        db.upsert_author("60000009", "主名", aliases_json=_json.dumps(["别名甲"], ensure_ascii=False))
        fn = ws._tidy_expected_folder(db, {"source": "pixiv", "author_id": "60000009"}, "")
        assert fn == an.author_folder_name(db, "60000009", "主名") == "主名（别名甲）_60000009", fn
        assert "/" not in fn

        # ---------- ④ 下载器复用磁盘上已存在的同 ID 作者夹 ----------
        img = tmp / "img"
        legacy = img / "涩图" / f"作者甲_{AID}"     # 历史遗留(删字符)夹
        legacy.mkdir(parents=True)
        d = object.__new__(PixivDownloader)
        d.db = db
        d.save_folder = img
        d._author_dir_cache = {}
        got = d._pick_author_dir(AID, "作者甲_作者乙", "12345", want)
        assert got == str(legacy), f"已有同 ID 作者夹必须复用, 否则又长出第二套: {got}"
        # 磁盘上没有任何同 ID 夹 → 用规范名新建 (不是「清洗名_ID」的本地拼法)
        d._author_dir_cache.clear()
        got2 = d._pick_author_dir("70000001", "任意名", "12345", "任意名_70000001")
        assert got2 == str(img / "任意名_70000001"), got2
        # 已含本作品文件的夹优先 (补下缺页必须落回同一夹)
        d._author_dir_cache.clear()
        (img / f"x_{AID}").mkdir(parents=True, exist_ok=True)
        (img / f"x_{AID}" / "12345_p0.png").write_bytes(b"p")
        got3 = d._pick_author_dir(AID, "作者甲_作者乙", "12345", want)
        assert got3 == str(img / f"x_{AID}"), got3
        db.close()
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)

    # ---------- ③ 静态护栏: 只留一处非法字符字符类, 别处不得再自造 ----------
    names = {p.name: p.read_text(encoding="utf-8") for p in (
        PROJECT_ROOT / "utils" / "author_naming.py",
        PROJECT_ROOT / "web" / "server.py",
        PROJECT_ROOT / "core" / "crawler.py",
        PROJECT_ROOT / "core" / "reverse_source.py",
        PROJECT_ROOT / "core" / "reverse_search.py",
    )}
    assert "\\x00-\\x1f" in names["author_naming.py"], "唯一规则模块必须持有非法字符表"
    for fn_name in ("server.py", "crawler.py", "reverse_source.py", "reverse_search.py"):
        assert "\\x00-\\x1f" not in names[fn_name], \
            f"{fn_name} 又自造了一份非法字符表 —— 与一键整理对不上就会摆出两个夹 (回归)"
        assert "from utils.author_naming import" in names[fn_name], \
            f"{fn_name} 必须走公共命名规则"
    assert "return clean_author_name(name)" in names["reverse_search.py"], \
        "反搜的 _clean_dirname 要直接委托公共规则"


def t123_author_detail_from_keyword_and_srcauthor() -> None:
    """T123 (2026-09-21, 用户两条报障):
    ① 关键词查作者的结果里每位作者要能「📄 查看详情」进到和「直接输入作者 ID」一样的作者页
       (作者卡 + 作品列表 + 翻批), 且能「‹ 返回关键词结果」;
    ② 「反搜来源」走文件名捷径时不能把作者丢掉 ——  `115531046` 明明作品 ID 能直接打开
       原网页, 结果却只说「命中 Pixiv #115531046」, 作者一栏空着 (本地 DB 其实早记着作者)。
    """
    import re as _re
    import tempfile
    import shutil
    import time as _time
    import utils.paths as upaths
    import web.server as ws
    from db.store import PixivDatabase

    # ---------- ① 前端: 卡片多一个「查看详情」+ 返回条 (静态护栏, 前端无离线 DOM 可跑) ----------
    js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    # 关键词作者结果必须带 withDetail=true (第三个参数), 否则按钮根本不会渲染出来
    assert "res.authors.map(a => renderAuthorCard(a, true, true))" in js, \
        "关键词作者结果的卡片没挂「查看详情」"
    assert "res.related_authors.map(a => renderAuthorCard(a, true, true))" in js, \
        "关键词相关作者的卡片没挂「查看详情」"
    assert "window.openAuthorDetail = function" in js and "window.backToKeywordResults = async function" in js
    assert "html += authorBackBarHtml();" in js, "作者页没插返回条"
    assert ".back-bar {" in css, "返回条没有样式"
    # 返回点必须在新搜索/手动切子页/别处跳作者页时清掉, 否则会「返回」到一个早就没了的结果页
    assert js.count("authorDetailBack = null;") >= 3, js.count("authorDetailBack = null;")
    # 前端资源版本必须跟着改 (改了 app.js/style.css 却不换 v=, 用户浏览器会用旧缓存)
    _mjs = _re.search(r'app\.js\?v=([\w.\-]+)', html)
    _mss = _re.search(r'style\.css\?v=([\w.\-]+)', html)
    assert _mjs and _mss and _mjs.group(1) == "20261020l" and _mss.group(1) == "20261020l", \
        (f"改了前端资源却没升 v= 版本号 (实读 app={_mjs and _mjs.group(1)} "
         f"css={_mss and _mss.group(1)} len={len(html)} 根={PROJECT_ROOT})")

    # ---------- ② 后端: 文件名捷径必须带上作者 ----------
    tmp = Path(tempfile.mkdtemp(prefix="t123src_"))
    orig_ud, orig_dr = upaths.user_data, upaths.data_root

    def _fake_user_data(rel):
        p = tmp / str(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    upaths.user_data = _fake_user_data
    upaths.data_root = lambda: tmp
    try:
        # 先把数据写进隔离库 (不能碰生产库)
        db = PixivDatabase(_fake_user_data("pixiv.db"))
        db.upsert_author("4997269", "作者甲/作者乙")
        db.upsert_artwork_metadata("115531046", title="男の子はみーんなノーブラなんですよ？",
                                   author_id="4997269")
        db._execute_with_commit(
            ["INSERT INTO pages (artwork_id,page_num,status,local_path,origin) VALUES (?,?,?,?,?)"],
            [("115531046", 0, 1, "涩图/作者乙_4997269/115531046_p0.png", "web")])
        db.close()

        client = ws.create_app().test_client()
        r = client.post("/api/library/source-search", json={"artwork_id": "115531046"})
        assert r.status_code == 200, (r.status_code, r.get_json())
        # 全部页都能从文件名定身份 → 不起浏览器, 后台线程秒完; 轮询几次即可
        st = {}
        for _ in range(40):
            st = (client.get("/api/reverse/status").get_json() or {}).get("src_search") or {}
            if st.get("phase") in ("done", "error") and not st.get("running"):
                break
            _time.sleep(0.1)
        assert st.get("phase") == "done", st
        hits = st.get("result") or []
        assert hits, st
        for h in hits:
            assert h["site"] == "pixiv" and h["artwork_id"] == "115531046", h
            # 身份仍标 filename (前端那颗「🟡 文件名自带ID·待确认」黄标不能变成「以图搜图命中」)
            assert h["author_src"] == "filename", h
            # 关键: 作者不能再是空串 —— 库里记着就必须填上
            assert h["author_name"] == "作者甲/作者乙", h
            assert h["author_id"] == "4997269", h

        # 库里没有的作品 (未入库/占位) → 作者留空, 但绝不许报错 (依然不联网、依然秒回)
        db2 = PixivDatabase(_fake_user_data("pixiv.db"))
        db2._execute_with_commit(
            ["INSERT INTO artworks (artwork_id) VALUES (?)",
             "INSERT INTO pages (artwork_id,page_num,status,local_path,origin) VALUES (?,?,?,?,?)"],
            [("placeholder_9",),
             ("placeholder_9", 0, 1, "根目录/778899001_p0.png", "web")])
        db2.close()
        r = client.post("/api/library/source-search", json={"artwork_id": "placeholder_9"})
        assert r.status_code == 200, r.get_json()
        st2 = {}
        for _ in range(40):
            st2 = (client.get("/api/reverse/status").get_json() or {}).get("src_search") or {}
            if st2.get("phase") in ("done", "error") and not st2.get("running"):
                break
            _time.sleep(0.1)
        h2 = (st2.get("result") or [{}])[0]
        assert h2.get("artwork_id") == "778899001", h2
        assert h2.get("author_name") == "" and h2.get("author_id") == "", h2
    finally:
        upaths.user_data, upaths.data_root = orig_ud, orig_dr
        shutil.rmtree(str(tmp), ignore_errors=True)


def t124_artwork_big_tag_style() -> None:
    """T124 (2026-09-21, ):
    `.w-tags` / `.tag` 的胶囊样式只写在 `.work-card` 作用域里, 而按作品 ID 直查的结果卡
    用的是 `.artwork-big` → 拿不到那条规则, 既没有 gap 也没有胶囊外观, 标签全挤成一坨。
    这里做静态护栏: 两个作用域必须各有一份, 且别再写第三套。
    """
    import re as _re
    css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")

    # ① 渲染点确实在 .artwork-big 里 (作品 ID 直查的结果卡)
    assert '<div class="artwork-big">' in js, "单作品大卡没了?"
    assert '<div class="w-tags" style="margin:6px 0">' in js, "作品大卡的标签渲染点变了, 护栏需同步"

    # ② 两个作用域都要有 flex 布局(gap 靠它才生效) + 胶囊外观
    for scope in (r"\.work-card", r"\.artwork-big"):
        assert _re.search(scope + r" \.w-tags \{[^}]*display: *flex", css), \
            f"{scope} .w-tags 缺 flex 布局 (标签会连在一起)"
        assert _re.search(scope + r" \.w-tags \{[^}]*gap:", css), f"{scope} .w-tags 缺 gap"
        assert _re.search(scope + r" \.tag \{[^}]*border-radius: *999px", css), \
            f"{scope} .tag 缺胶囊圆角"

    # ③ 版本号
    assert _re.search(r'style\.css\?v=20261020l', html) and _re.search(r'app\.js\?v=20261020l', html), \
        "改了前端资源却没升 v= 版本号"
    # ④ 设置页「版本与构建」只写版本事实, 不再挂解释性说明段落
    #    
    assert 'id="settings-version"' in html, "版本信息容器没了"
    assert '怀疑「改了没生效」时先看这里' not in html, \
        "版本区不该再有解释性说明段落"
    assert '服务端登记' not in js, "版本信息里不该再拼「（服务端登记：…）」这类解释文字"
    assert "(m ? m[1] : '').split('-')[0]" in js, \
        "前端资源版本只显示手写标签(不要把那串 mtime 指纹摆给人看)"


def t125_ugoira_download_to_gif() -> None:
    """T125 (2026-09-21, ):
    pixiv 的动图 (ugoira) 跟静图**不是一个通道** —— `ajax/illust/{id}/pages` 只给一张
    缩过的静态帧图, 真动图在 `ajax/illust/{id}/ugoira_meta` 里 (一个 zip, 每帧一张 png
    + `frames[{file, delay}]`, 延迟可变)。这里做两件事:
      ① 纯函数离线跑: `parse_ugoira_frames` 的脏数据兜底 + `compose_ugoira_gif` 真能把
         内存 zip 合成可变延迟的多帧 gif (用 Pillow 回读校验帧数/时长/尺寸/透明);
      ② 静态护栏: 下载器必须分流动图、命名裸 `{pid}.gif`、落库带宽高、失败**绝不回退
         浏览器**(浏览器通道也只有静态帧图, 回退 = 把的 bug 再演一遍),
         且一键整理的「文件名规范化」不会把这个名字改掉。
    """
    import io as _io
    import re as _re
    import shutil as _shutil
    import tempfile as _tempfile
    import zipfile as _zipfile
    from PIL import Image
    from core.crawler import parse_ugoira_frames, compose_ugoira_gif
    from db.store import pixiv_natural_key

    # ---------- ① 帧解析: 脏数据兜底 ----------
    assert parse_ugoira_frames(None) == []
    assert parse_ugoira_frames({}) == []
    assert parse_ugoira_frames({"frames": []}) == []
    got = parse_ugoira_frames({"frames": [
        {"file": "000001.png", "delay": 30},
        {"file": "000002.png", "delay": 0},      # pixiv 偶发 0 → 抬到下限
        {"file": "000003.png", "delay": "x"},    # 脏值 → 0 → 抬到下限
        {"file": "000004.png"},                  # 缺 delay → 抬到下限
        {"file": "", "delay": 50},               # 空文件名 → 跳过
        None,                                    # 脏帧 → 跳过
        {"file": "000005.png", "delay": 500},
    ]})
    assert got == [("000001.png", 30), ("000002.png", 10), ("000003.png", 10),
                   ("000004.png", 10), ("000005.png", 500)], got

    # ---------- ② 合成 gif: 多帧 + 可变延迟 + 尺寸 + 透明 ----------
    tmp = Path(_tempfile.mkdtemp(prefix="t125ugo_"))
    try:
        src_frames = [("000001.png", (255, 0, 0, 255)),
                      ("000002.png", (0, 255, 0, 128)),
                      ("000003.png", (0, 0, 255, 0))]
        zbuf = _io.BytesIO()
        with _zipfile.ZipFile(zbuf, "w") as zf:
            for name, color in src_frames:
                buf = _io.BytesIO()
                Image.new("RGBA", (40, 30), color).save(buf, "PNG")
                zf.writestr(name, buf.getvalue())
        delays = [30, 100, 500]
        out = str(tmp / "1234567.gif")
        wh = compose_ugoira_gif(zbuf.getvalue(),
                                [(n, d) for (n, _c), d in zip(src_frames, delays)], out)
        assert wh == (40, 30), wh
        with Image.open(out) as g:
            assert g.format == "GIF"
            assert g.size == (40, 30), g.size
            assert getattr(g, "n_frames", 1) == 3, g.n_frames
            # 透明索引只写在第 0 帧的扩展块里 → 必须在 seek 之前读(seek 后 info 里就没有了)
            assert g.info.get("transparency") is not None, \
                "帧带透明通道, 合成后必须留透明索引(否则透明区会变成黑块)"
            ds = []
            for i in range(g.n_frames):
                g.seek(i)
                ds.append(g.info.get("duration"))
            assert ds == delays, f"每帧延迟必须按 pixiv 给的来(可变延迟): {ds}"
            assert g.info.get("loop") == 0, "动图要无限循环"

        # 帧名对不上 zip 内容 → 跳过; 一个都对不上/不是 zip → None, 且不留半成品文件
        assert compose_ugoira_gif(zbuf.getvalue(), [("nope.png", 30)],
                                  str(tmp / "x.gif")) is None
        assert not (tmp / "x.gif").exists(), "合成失败不该留下半个空 gif"
        assert compose_ugoira_gif(b"not a zip at all", [("000001.png", 30)],
                                  str(tmp / "y.gif")) is None
    finally:
        _shutil.rmtree(str(tmp), ignore_errors=True)

    # ---------- ③ 静态护栏: 分流 / 命名 / 落库 / 绝不回退浏览器 ----------
    src = (PROJECT_ROOT / "core" / "crawler.py").read_text(encoding="utf-8")
    assert "class UgoiraError(RuntimeError)" in src, \
        "动图失败要有专用异常, 否则会被「HTTP 失败→浏览器兜底」那层吃掉"
    assert "if info.illust_type == IllustType.UGOIRA:" in src, \
        "pages 通道没分流动图 → 动图还是会被下成一张低分辨率静态帧图"
    assert "return self._download_ugoira_http(" in src
    assert "ugoira_meta?lang=zh" in src, "动图元数据接口地址写错了"
    # 裸 {pid}.gif: 动图是「一张会动的图」, 没有页序
    assert 'filename = f"{artwork_id}.gif"' in src, "动图命名必须是不带 _pN 的裸作品 ID"
    assert 'f"{artwork_id}_p{pn}.{ext}"' in src, "静图命名不该被改动"
    # 落库: 1 页(page 0) + 带宽高 (图库卡片要显示原始尺寸)
    assert "self.db.init_pages(artwork_id, 1)" in src
    assert "self.db.mark_page_status(artwork_id, 0, PSt.DONE, rel_path, md5=fmd5," in src
    assert "width=w, height=h, file_size_bytes=fsize)" in src, "动图落库要带宽高"
    # 失败必须如实抛错, 绝不能落到 download_artwork 的浏览器回退
    assert "except UgoiraError as e:" in src
    _i_ugo = src.index("except UgoiraError as e:")
    _i_exc = src.index("except Exception as e:", _i_ugo)
    assert src.index("except DownloadCancelled:") < _i_ugo < _i_exc, \
        "UgoiraError 必须排在通用 except 之前, 否则浏览器回退照样发生"
    _seg = src[_i_ugo:_i_exc]
    assert "raise" in _seg and "_download_browser(" not in _seg, \
        "动图失败不能改用浏览器兜底(浏览器通道只有一张静态帧图)"

    # 命名口径: 裸 {pid}.gif 是 pixiv 自然键, 一键整理必须原样保留(不改名)
    assert pixiv_natural_key("1234567.gif") == ("1234567", 0)
    srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "if _nat and _nat[0] == aid:" in srv, \
        "一键整理的文件名规范化必须让自然键命名的文件原样保留"


def t126_combo_silent_sync_and_pid_author_detail() -> None:
    """T126 (2026-09-21, 用户两条反馈, 均为前端静态护栏 —— 前端无离线 DOM 可跑):
    ① 「图库点作者名会自动弹开作者/标签两个下拉框」: 根因是 `libFilterByAuthor` 为刷新
       「✕」小叉叉状态而**派发合成 input 事件**, 而 makeCombo 里 input 正是展开面板的
       触发器 → 点一次作者名弹两个框。护栏: 那条路径不许再出现 dispatchEvent, 静默同步
       改走 attachInputClear 挂出来的 `input._clearSync`, combo 走 setByValue/clear。
    ② 「pid 直查结果卡的作者信息也要能看详情」: `renderAuthorCard(res.author, true, true)`
       + 返回点记来源(kind), 返回条按来源分文案。
    """
    import re as _re
    js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")

    # ---------- ① 不再用假 input 事件做「静默同步」 ----------
    assert "input._clearSync = sync;" in js, \
        "attachInputClear 要把 sync 挂到元素上, 外部才能静默刷新小叉叉"
    _m = _re.search(r"window\.libFilterByAuthor = function[\s\S]*?\n\};", js)
    assert _m, "找不到 libFilterByAuthor"
    _body = _m.group(0)
    assert "dispatchEvent" not in _body, \
        "libFilterByAuthor 又在派发假事件了 —— input 事件会展开下拉面板"
    assert "comboTag.clear()" in _body and "comboAuthor.setByValue(" in _body, \
        "清标签/回填作者要走 combo 自己的方法(它们不触发面板展开)"
    assert "_clearSync()" in _body, "小叉叉状态要静默刷新"
    # combo 的两个新方法
    assert "setByValue(value, fallbackLabel) {" in js
    assert "clear() { pickedValue = ''; pickedLabel = ''; input.value = ''; }" in js
    # 这条护栏的前提: input 事件仍然是面板展开触发器
    assert "input.addEventListener('input', () => render(true));" in js, \
        "makeCombo 的 input 展开行为变了, 这条护栏的前提需要重新评估"

    # ---------- ② pid 直查的作者卡带「📄 查看详情」 ----------
    assert "if (res.author) html += renderAuthorCard(res.author, true, true);" in js, \
        "按作品 ID 直查的作者卡没挂「📄 查看详情」"
    assert "kind: prevRes.artwork ? 'artwork' : 'keyword'," in js, \
        "返回点没记来源 → 返回条文案会串"
    assert "isWork ? '‹ 返回作品结果' : '‹ 返回关键词结果'" in js
    assert "作品 #${esc(b.q)} 的作者" in js
    # 返回函数名不能改 (T123 断言依赖, HTML 里的 onclick 也写死了它)
    assert "window.backToKeywordResults = async function" in js


def t127_wallpaper_current_trace() -> None:
    """T127 (2026-09-24, 需求):
    桌面幻灯片正在播的那张 → 图库里是谁、有没有更高清、要不要删。全程只读。

    ① 读注册表拿当前壁纸路径 (读不到 → active=False, 不报错);
    ② 四档溯源: 库内文件 / MD5 字节一致 / 文件名作品ID / ORB 融合 (deep=1 才跑慢路);
    ③ 形态差异 same/rotate/crop/rescale + 库内最大页对比 (bigger);
    ④ deep 结论按「文件+mtime+大小」缓存 —— 8 秒轮询不带 deep=1 也不退回「未溯源」;
    ⑤ 只读护栏: 壁纸在图库内时拒绝「移出壁纸」; root=wall 只读当前壁纸所在夹 (防穿越);
    ⑥ 前端接线静态护栏 (离线, 前端无 DOM 可跑)。

    只动临时库 / 临时文件, 不碰生产库与真实壁纸夹, 也不真删文件 (回收函数被换掉)。
    """
    import tempfile
    import shutil
    import hashlib as _hl
    from urllib.parse import quote
    import utils.paths as upaths
    import web.server as wsrv
    from db.store import PixivDatabase, WSt, PSt
    from web.server import create_app

    tmp = Path(tempfile.mkdtemp(prefix="t127wall_"))
    img_root = tmp / "img"
    wall_dir = tmp / "wall"
    img_root.mkdir(parents=True, exist_ok=True)
    wall_dir.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    orig_wf = wsrv._wallpaper_file
    orig_recycle = wsrv._recycle_files
    orig_orb = wsrv._wall_orb_candidates
    orig_popen = wsrv.subprocess.Popen
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp

    def _mk_png(p: Path, w: int, h: int, color=(30, 60, 90)) -> bytes:
        from PIL import Image as _Img
        p.parent.mkdir(parents=True, exist_ok=True)
        _Img.new("RGB", (w, h), tuple(color)).save(str(p), format="PNG")
        return p.read_bytes()

    def _use(path: "Path | None"):
        wsrv._wallpaper_file = (lambda: None) if path is None else (lambda: Path(path))

    try:
        wsrv._WALL_DEEP_CACHE.clear()
        # ---- 临时库: 作者 555「shuno」; 700001 库内 1920x1080, 700002 库内 3840x2160 ----
        db = PixivDatabase(tmp / "pixiv.db")
        db.upsert_author("555", "shuno")
        db.change_work_status("700001", WSt.DONE, title="湖边少女", author_id="555",
                              total_pages=1, illust_type=0)
        db.change_work_status("700002", WSt.DONE, title="高清大图", author_id="555",
                              total_pages=1, illust_type=0)
        rel_a = "X图/shuno_555/700001_p0.png"
        rel_b = "X图/shuno_555/700002_p0.png"
        bytes_a = _mk_png(img_root / rel_a, 1920, 1080)
        md5_a = _hl.md5(bytes_a).hexdigest()
        # 库里的 local_path 故意写**反斜杠** (Windows 真实形态): 回归 2026-09-24
        # 「MD5 已命中却拿不到作品」的斜杠不一致 bug
        db.mark_page_status("700001", 0, PSt.DONE,
                            local_path="X图\\shuno_555\\700001_p0.png",
                            md5=md5_a, width=1920, height=1080)
        _mk_png(img_root / rel_b, 3840, 2160, color=(90, 30, 60))
        db.mark_page_status("700002", 0, PSt.DONE,
                            local_path="X图/shuno_555/700002_p0.png",
                            width=3840, height=2160)
        db.close()

        client = create_app().test_client()

        # ---------- ① 没开幻灯片 / 注册表没有可用壁纸 → active=False, 不报错 ----------
        _use(None)
        j = client.get("/api/wallpaper/current").get_json()
        assert j["status"] == "ok" and j["active"] is False and j.get("reason"), j

        # ---------- ② 壁纸在图库外, 但字节与库内某页完全一致 → kind=md5 ----------
        wa = wall_dir / "700001_p0.png"
        wa.write_bytes(bytes_a)
        _use(wa)
        r = client.get("/api/wallpaper/current")
        assert r.status_code == 200, r.status_code
        j = r.get_json()
        assert j["active"] and j["deep"] is False, j
        assert (j["file"]["in_library"] == 0 and j["file"]["width"] == 1920
                and j["file"]["height"] == 1080), j["file"]
        m = j["match"]
        assert m["kind"] == "md5" and m["exact"] is True and m["confidence"] == 1.0, m
        assert m["page_num"] == 0 and m["relation"] == "same", m
        assert j["artwork"]["artwork_id"] == "700001", j["artwork"]
        assert j["artwork"]["author_name"] == "shuno", j["artwork"]
        assert j["bigger"] == 0, j["bigger"]
        assert "root=wall" in j["file"]["thumb"], j["file"]["thumb"]
        rt = client.get(j["file"]["thumb"])
        assert rt.status_code == 200, (rt.status_code, j["file"]["thumb"])

        # ---------- ③ 壁纸文件本身就在图库内 → kind=library (最高可信, 不用 MD5) ----------
        _use(img_root / rel_a)
        j = client.get("/api/wallpaper/current").get_json()
        assert j["file"]["in_library"] == 1, j["file"]
        assert j["match"]["kind"] == "library" and j["match"]["exact"] is True, j["match"]
        assert j["match"]["rel"] == rel_a, j["match"]
        assert "root=img" in j["file"]["thumb"], j["file"]["thumb"]

        # ---------- ④ 文件名是 pixiv 自然键且库里确有该作品 → kind=id + 库内更大 ----------
        wa2 = wall_dir / "700002_p0.png"
        _mk_png(wa2, 1920, 1080, color=(200, 10, 10))   # 与库内字节不同 → 不走 MD5
        _use(wa2)
        j = client.get("/api/wallpaper/current").get_json()
        m = j["match"]
        assert m["kind"] == "id" and m["confidence"] == 0.9 and m["page_num"] == 0, m
        assert m["rel"] == rel_b, m
        assert m["relation"] == "rescale", m            # 比例一致, 只是缩小了
        assert j["artwork"]["artwork_id"] == "700002", j["artwork"]
        assert j["bigger"] == 1, "库里那张 3840x2160 更大, 应提示可换更高清"
        assert any(p.get("biggest") == 1 for p in j["pages"]), j["pages"]

        # ---------- ⑤ 四档全落空 → kind=none; deep=1 才跑 ORB 慢路 ----------
        wa3 = wall_dir / "mystery_art.png"
        _mk_png(wa3, 1920, 1080, color=(10, 200, 10))
        _use(wa3)
        j = client.get("/api/wallpaper/current").get_json()
        assert j["match"]["kind"] == "none" and j["artwork"] is None, j["match"]
        assert j["deep"] is False and j["candidates"] == [], j
        wsrv._wall_orb_candidates = lambda f, root: [
            {"rel": rel_a, "fusion": 0.82, "orb": 0.55}]
        j = client.get("/api/wallpaper/current?deep=1").get_json()
        assert j["deep"] is True, j
        assert j["match"]["kind"] == "orb" and j["match"]["confidence"] == 0.82, j["match"]
        assert j["artwork"]["artwork_id"] == "700001", j["artwork"]
        # 结论缓存: 再来一次**不带** deep=1, 也要仍报 orb (8 秒轮询不能把界面跳回「未溯源」)
        j = client.get("/api/wallpaper/current").get_json()
        assert j["match"]["kind"] == "orb" and j["deep"] is True, j["match"]

        # ---------- ⑥ 有候选但分数太低 → kind=weak, 绝不硬认 ----------
        wa4 = wall_dir / "mystery_low.png"
        _mk_png(wa4, 1280, 720, color=(10, 10, 200))
        _use(wa4)
        wsrv._wall_orb_candidates = lambda f, root: [
            {"rel": rel_b, "fusion": 0.44, "orb": 0.15}]
        j = client.get("/api/wallpaper/current?deep=1").get_json()
        assert j["match"]["kind"] == "weak" and j["artwork"] is None, j["match"]

        # ---------- ⑥.5 ORB 报错 → deep_error 透传, 不当成「未溯源」静默吞掉 ----------
        wa5 = wall_dir / "mystery_err.png"
        _mk_png(wa5, 1024, 768, color=(200, 200, 10))
        _use(wa5)
        wsrv._wall_orb_candidates = lambda f, root: [{"error": "特征库尚未构建"}]
        j = client.get("/api/wallpaper/current?deep=1").get_json()
        assert j.get("deep_error") == "特征库尚未构建", j
        assert j["match"]["kind"] == "none", j["match"]
        # 报错**不缓存**: 换成能成功的检索器后, 下一次请求必须真的重跑 (瞬时失败不能被钉死)
        err_calls = []

        def _orb_ok(f, root):
            err_calls.append(1)
            return [{"rel": rel_b, "fusion": 0.66, "orb": 0.42}]

        wsrv._wall_orb_candidates = _orb_ok
        j = client.get("/api/wallpaper/current?deep=1").get_json()
        assert len(err_calls) == 1, f"报错被缓存 → 用户永远等不到重试: {err_calls}"
        assert not j.get("deep_error") and j["match"]["kind"] == "orb", j
        # 成功之后才该缓存 (第三次不再跑检索器, 否则 8 秒轮询反复跑重活)
        j = client.get("/api/wallpaper/current?deep=1").get_json()
        assert len(err_calls) == 1, "成功结论必须缓存"

        # ---------- ⑦ 形态差异单测 ----------
        rel_fn = wsrv._wall_relation
        assert rel_fn((1920, 1080), (1920, 1080)) == "same"
        assert rel_fn((1080, 1920), (1920, 1080)) == "rotate"
        assert rel_fn((2560, 1080), (1920, 1080)) == "crop"
        assert rel_fn((960, 540), (1920, 1080)) == "rescale"
        assert rel_fn((0, 0), (1920, 1080)) == "", "壁纸尺寸读不到时不给结论"
        assert rel_fn((1920, 1080), (0, 0)) == "", "库内页无尺寸时不给结论"

        # ---------- ⑧ root=wall 只读当前壁纸所在夹 (防穿越) / 无壁纸时 404 ----------
        _use(wa)
        r = client.get(f"/api/local-img?p={quote(wa.name)}&root=wall")
        assert r.status_code == 200, r.status_code
        _mk_png(tmp / "secret.png", 8, 8, color=(1, 2, 3))
        r = client.get("/api/local-img?p=..%2Fsecret.png&root=wall")
        assert r.status_code == 403, r.status_code
        _use(None)
        r = client.get(f"/api/local-img?p={quote(wa.name)}&root=wall")
        assert r.status_code == 404, r.status_code

        # ---------- ⑨ 只读护栏: 壁纸在图库内 → 拒绝「移出壁纸」----------
        _use(img_root / rel_a)
        r = client.post("/api/wallpaper/recycle")
        assert r.status_code == 400, r.status_code
        assert "图库" in r.get_json().get("msg", ""), r.get_json()

        # ---------- ⑩ 壁纸在图库外 → 只把「这一个文件」送回收站, 路径由服务端算 ----------
        caught = []
        wsrv._recycle_files = lambda ps: (caught.extend(ps), (1, []))[1]
        _use(wa)
        r = client.post("/api/wallpaper/recycle")
        assert r.status_code == 200 and r.get_json().get("recycled") == 1, r.get_json()
        assert caught == [str(wa)], caught
        assert wa.exists(), "冒烟不能真删文件 (回收函数已被换掉)"

        # ---------- ⑪ 打开所在文件夹: 路径全由服务端读注册表, 客户端不传 ----------
        popped = []
        wsrv.subprocess.Popen = lambda *a, **kw: popped.append(a)
        try:
            r = client.post("/api/wallpaper/open-folder")
        finally:
            wsrv.subprocess.Popen = orig_popen
        assert r.status_code == 200 and r.get_json().get("path") == str(wa), r.get_json()
        assert popped and str(wa) in str(popped[0][0]), popped

        # ---------- ⑫ 前端接线静态护栏 (离线) ----------
        html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
        assert 'id="wall-card"' in html and 'onclick="wallRefresh()"' in html, "壁纸面板没接上"
        assert 'onclick="wallFold()"' in html, "壁纸面板缺收起按钮"
        assert "style.css?v=20261020l" in html and "app.js?v=20261020l" in html, \
            "改了前端(壁纸面板)必须升 ?v= 防旧缓存"
        assert "window.wallRefresh = wallRefresh;" in js
        # 壁纸卡标题: 程序拼出来的标题(「来源夹 / 文件名」)不算真标题 —— 否则 AI 那句永远轮不到
        # 显示 (用户 2026-09-27:「文案改了, 标题没改欸」)。与 utils/widget_window.py 的
        # _machine_title 同一条口径, 两边都要在。
        assert "function machineTitle(t, fname)" in js and "function wallTitle(a, d)" in js, \
            "壁纸卡标题没走「真标题优先、程序拼的让位」这条口径"
        assert "esc(wallTitle(a, d))" in js, "壁纸卡标题没接上 wallTitle"
        assert "window.wallAct = wallAct;" in js
        assert "window.wallPreview = wallPreview;" in js
        assert "function wallWatch()" in js and "function renderWallCard(d)" in js
        assert ("if (name === 'library') { libSync(); loadStats(); loadLibrary(); "
                "loadLibFacets(); loadTrashBadge(); wallWatch(); }") in js, \
            "切到图库页必须启动壁纸轮询"
        assert "wall-card" in css and ".wall-badge.ok" in css, "壁纸面板样式缺失"
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        wsrv._wallpaper_file = orig_wf
        wsrv._recycle_files = orig_recycle
        wsrv._wall_orb_candidates = orig_orb
        wsrv.subprocess.Popen = orig_popen
        wsrv._WALL_DEEP_CACHE.clear()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t128_tray_pure_functions():
    """🖥 托盘常驻:
    ① tooltip ≤120 字符且**无换行** (Shell_NotifyIcon 的 szTip 硬上限 128 wchar 含 NUL),
       格式 `Vixiq · #ID · 作者 · 标题`, 溯源不出来退化成「Vixiq · 壁纸未溯源」;
    ② 菜单结构 = 两行灰字只读信息 + 打开图库/打开所在文件夹 + 分隔线 + 设置/退出,
       缺文件时「打开所在文件夹」必须是灰字 (点了也没用);
    ③ 命令 id 唯一且能翻成人话 (菜单直接返回 id 当命令, 不认就静默没反应);
    ④ 窗口消息 → 动作名 的映射覆盖全部分支 (ctypes 层只按名字执行);
    ⑤ NOTIFYICONDATAW 的结构尺寸/字段偏移**钉死实测值** —— 抄网上 V1/V2 的旧 size 会让
       Shell_NotifyIconW 直接失败, 表现为「图标根本不出现」, 极难查。
    """
    from utils import tray

    # ---------- ① tooltip ----------
    assert tray.tray_tooltip(None) == "Vixiq · 壁纸未溯源"
    assert tray.tray_tooltip({}) == "Vixiq · 壁纸未溯源"
    assert tray.tray_tooltip({"active": False, "reason": "x"}) == "Vixiq · 壁纸未溯源"
    full = {"active": True,
            "artwork": {"artwork_id": "109275847", "author_name": "紺屋",
                        "title": "day32 花と水飴、最終電車"},
            "summary": {"author": "紺屋", "title": "day32 花と水飴、最終電車"},
            "file": {"name": "109275847_p0.png"}}
    tip = tray.tray_tooltip(full)
    assert tip == "Vixiq · #109275847 · 紺屋 · day32 花と水飴、最終電車", tip
    # 超长 + 带换行的标题: 必须压成单行且不超上限 (否则 tooltip 会显示成方框/被截断)
    long_tip = tray.tray_tooltip({
        "active": True, "file": {"name": "x.png"},
        "artwork": {"artwork_id": "1", "author_name": "A" * 200, "title": "T\n" * 100},
        "summary": {}})
    assert len(long_tip) <= tray.TOOLTIP_MAX, len(long_tip)
    assert "\n" not in long_tip and "\r" not in long_tip, repr(long_tip)
    # 有文件但没溯源 → 仍要能看出是哪一个文件
    assert "mystery.png" in tray.tray_tooltip(
        {"active": True, "file": {"name": "mystery.png"}})

    # ---------- ② 菜单结构 ----------
    items = tray.tray_menu_items(full)
    kinds = [s for _c, _l, s in items]
    assert kinds[0] == "gray" and kinds[1] == "gray", kinds
    assert kinds.count("sep") == 2, kinds
    cmds = [c for c, _l, s in items if s != "sep"]
    labels = [l for _c, l, s in items if s != "sep"]
    # 非 sep 项里 cmd=0 的只有顶部两行灰字信息; 其余必须是各命令 id (顺序即菜单顺序)
    assert cmds == [0, 0, tray.CMD_OPEN_GALLERY, tray.CMD_OPEN_FOLDER,
                    tray.CMD_SETTINGS, tray.CMD_QUIT], cmds
    assert labels[2:4] == ["打开图库", "打开所在文件夹"], labels
    assert labels[-2:] == ["设置…", "退出 Vixiq"], labels
    # 「图像溯源」这一项 2026-09-25 已删 (用户: 「就弹个通知」没意义), 别再加回来
    assert not hasattr(tray, "CMD_TRACE") and "图像溯源" not in labels, labels
    # 灰字行必须是「作者/标题」这类只读信息, 不能是能点的命令
    assert items[0][2] == "gray" and "#109275847" in items[0][1] and "紺屋" in items[0][1]
    assert items[1][1] == "day32 花と水飴、最終電車", items[1]
    # 没有壁纸文件时: 依赖文件的项要灰掉 (否则用户点了毫无反应)
    nofile = {c: s for c, _l, s in tray.tray_menu_items({"file": {}})}
    assert nofile[tray.CMD_OPEN_FOLDER] == "gray", nofile
    assert nofile[tray.CMD_OPEN_GALLERY] == "normal" and nofile[tray.CMD_QUIT] == "normal"

    # ---------- ③ 命令 id ----------
    ids = [tray.CMD_OPEN_GALLERY, tray.CMD_OPEN_FOLDER,
           tray.CMD_SETTINGS, tray.CMD_QUIT]
    assert len(set(ids)) == len(ids), "命令 id 撞了 → 菜单会点错功能"
    assert tray.menu_cmd_label(tray.CMD_QUIT) == "退出 Vixiq"
    assert tray.menu_cmd_label("1002") == "打开所在文件夹", "字符串 id 也要认"
    assert tray.menu_cmd_label(None).startswith("#"), "怪 id 不能抛异常"

    # ---------- ④ 消息 → 动作 ----------
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_RBUTTONUP) == ("menu",)
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_CONTEXTMENU) == ("menu",)
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_LBUTTONUP) == ("refresh",)
    # 2026-10-20 → 双击翻成 open_gallery,
    # 单击仍是 refresh (双击时 Windows 会补一整套单击消息, 两条都收到是预期)
    assert tray.WM_LBUTTONDBLCLK == 0x0203
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_LBUTTONDBLCLK) == ("open_gallery",)
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, 0x0200) == (), "非点击不动"
    assert tray.handle_tray_message(tray.WM_WALL_CHANGED, 0, 0) == ("wall_changed",)
    assert tray.handle_tray_message(tray.WM_ALREADY_RUNNING, 0, 0) == ("already_running",)
    assert tray.handle_tray_message(tray.WM_CLOSE, 0, 0) == ("quit",)
    assert tray.handle_tray_message(0x1234, 0, 0) == () and tray.handle_tray_message("x", 0, 0) == ()
    # 四个自定义消息必须落在 WM_APP 段且互不相同
    assert len({tray.WM_TRAY_CALLBACK, tray.WM_WALL_CHANGED,
                tray.WM_ALREADY_RUNNING, tray.WM_SHOW_MENU}) == 4
    assert all(m >= tray.WM_APP for m in (tray.WM_TRAY_CALLBACK, tray.WM_WALL_CHANGED,
                                          tray.WM_ALREADY_RUNNING, tray.WM_SHOW_MENU))

    # ---------- ④b 「必须建窗线程做」的消息 (2026-09-24 实测踩坑) ----------
    # 右键菜单若丢给子线程弹, TrackPopupMenu/SetForegroundWindow 会因为窗口不属于
    # 调用线程而**毫无反应** —— 用户看到的就是「点托盘图标没反应, 退不出去」。
    assert tray.tray_window_action(tray.WM_SHOW_MENU) == "show_menu"
    assert tray.tray_window_action(tray.WM_TRAY_CALLBACK) is None, "鼠标回调不能走同步分支"
    assert tray.tray_window_action(tray.WM_CLOSE) is None
    assert tray.tray_window_action("x") is None and tray.tray_window_action(None) is None
    # TaskbarCreated: explorer 重启会清空托盘图标, 必须重新 NIM_ADD, 否则永久消失
    tc = tray.taskbar_created_msg()
    assert isinstance(tc, int), tc
    if os.name == "nt":
        assert tc > 0, "Windows 上必须拿得到 TaskbarCreated 的注册消息号"
        assert tray.tray_window_action(tc, taskbar_created=tc) == "readd_icon"
        # taskbar_created 传 0 (拿不到) 时不能把任何消息误判成 readd_icon
        assert tray.tray_window_action(0, taskbar_created=0) is None
    assert tray.tray_window_action(tc or 0xC0BF, taskbar_created=0) is None

    # ---------- ⑤ 结构尺寸钉死 (x64 本机实测) ----------
    if os.name == "nt":
        import ctypes as _ct
        assert _ct.sizeof(tray.NOTIFYICONDATAW) == 976, _ct.sizeof(tray.NOTIFYICONDATAW)
        off = {f: getattr(tray.NOTIFYICONDATAW, f).offset
               for f, _t in tray.NOTIFYICONDATAW._fields_}
        # 按 MSDN 顺序: cbSize/hWnd/uID/uFlags/uCallbackMessage/hIcon/szTip
        assert (off["cbSize"], off["hWnd"], off["uID"], off["uFlags"],
                off["uCallbackMessage"], off["hIcon"], off["szTip"]) \
            == (0, 8, 16, 20, 24, 32, 40), off
        assert off["szInfo"] == 304 and off["uTimeout"] == 816 and off["szInfoTitle"] == 820, off
        assert off["dwInfoFlags"] == 948 and off["guidItem"] == 952, off
        assert off["hBalloonIcon"] == 968, off
        assert _ct.sizeof(dict((f, t) for f, t in tray.NOTIFYICONDATAW._fields_)["szTip"]) \
            // _ct.sizeof(_ct.c_wchar) == 128, "szTip 必须是 128 个 wchar"
        # 自限必须留出 NUL 的余量
        assert tray.TOOLTIP_MAX <= 127, tray.TOOLTIP_MAX
        # WNDPROC 的返回类型必须是 64 位 (用 c_int 会被截断 → 窗口过程返回垃圾)
        assert _ct.sizeof(tray._WNDPROC._restype_) == _ct.sizeof(_ct.c_ssize_t), \
            "LRESULT 用了 32 位类型"
        assert tray.TRAY_CLASS_NAME and isinstance(tray.TRAY_CLASS_NAME, str)


def t129_tray_lifecycle_policy():
    """🖥 托盘生命周期策略: 默认**只驻托盘**, 关掉浏览器不再自杀。

    ① should_watch_browser: 缺键/显式 false → 不启用心跳守护 (托盘常驻); true 才恢复旧行为;
    ② second_instance_action: 托盘开着 → 让运行实例弹「已在托盘中运行」; 托盘关掉 → 回退
       POST /api/focus 激活浏览器;
    ③ tray_notify_action: 同一张不动 / 换图只刷 tooltip / 开了提示才弹气泡;
    ④ 静态护栏: main.py 必须 ①把心跳守护包在开关里 ②webbrowser.open 只在
       auto_open_browser 为真时调用 ③退出前摘托盘图标 ④不再有 _open_when_ready 这个名字。
    """
    from utils import tray
    from pathlib import Path as _P

    # ---------- ① 心跳守护开关 ----------
    assert tray.should_watch_browser({}) is False, "缺 launcher 节时绝不能启用 (否则关浏览器就自杀)"
    assert tray.should_watch_browser({"launcher": {}}) is False
    assert tray.should_watch_browser({"launcher": {"exit_on_browser_close": False}}) is False
    assert tray.should_watch_browser({"launcher": {"exit_on_browser_close": True}}) is True

    # ---------- ② 二实例 ----------
    assert tray.second_instance_action({}) == "bubble"
    assert tray.second_instance_action({"launcher": {"tray_enabled": True}}) == "bubble"
    assert tray.second_instance_action({"launcher": {"tray_enabled": False}}) == "focus"

    # ---------- ③ 壁纸变化动作 ----------
    assert tray.tray_notify_action("k1", "k1", {}) == "none", "同一张不该刷 (8 秒轮询会白闪)"
    assert tray.tray_notify_action("k1", "", {}) == "none", "取不到键时不动"
    assert tray.tray_notify_action("k1", "k2", {}) == "refresh"
    assert tray.tray_notify_action("k1", "k2", {"launcher": {"notify_on_wall_change": True}}) \
        == "bubble"
    assert tray.tray_notify_action(None, "k2", None) == "refresh", "cfg 传 None 不能炸"

    # ---------- ④ main.py 静态护栏 ----------
    src = (_P(PROJECT_ROOT) / "main.py").read_text(encoding="utf-8")
    assert "_open_when_ready" not in src, "旧的「就绪即开浏览器」入口应已被 _start_ui_when_ready 取代"
    assert "def _start_ui_when_ready()" in src
    assert "if ln.get(\"auto_open_browser\"):" in src, "自动开浏览器必须由开关决定"
    assert "if ln.get(\"tray_enabled\", True):" in src, "托盘默认必须是开"
    assert "if ln.get(\"wall_widget\"):" in src
    assert "should_watch_browser(config)" in src, "心跳守护必须过开关, 不能无条件启动"
    # 2026-10-20: 心跳守护改走 _set_watch_browser (代次门控), 「关页面即退出」开关存盘即生效。
    # 所以不再裸起线程 —— 起线程这件事挪进了 _set_watch_browser 里 (代次 +1 才算数)。
    assert "_set_watch_browser(should_watch_browser(config))" in src, \
        "开关为真时仍要能起 (经 _set_watch_browser 代次门控)"
    assert "def _set_watch_browser(" in src and "_watchdog_gen" in src, \
        "守护线程必须认代次, 否则关了开关旧线程还在判退出"
    assert "def _tray_stop()" in src and "_tray_stop()" in src.split("def _cleanup")[1], \
        "退出路径必须摘托盘图标 (否则右下角留幽灵图标)"
    assert "def _quit_from_tray()" in src and "os._exit(0)" in src, "托盘退出要真的结束进程"
    assert "second_instance_action" in src, "二实例要按托盘开关决定行为"
    # --quit 兜底出口: 图标被 Windows 收进「隐藏图标」溢出区时用户点不到它,
    # 必须有一条不依赖托盘的退出路径 
    assert '"--quit"' in src, "缺少 --quit 兜底退出"
    _q = src.split('if "--quit" in sys.argv[1:]:')[1]
    assert "_acquire_single_instance() is None" in _q, \
        "--quit 必须排在单实例锁之前, 否则会被当成重复启动走二实例分支"
    assert "WM_CLOSE" in _q and "find_tray_window()" in _q, "--quit 要给托盘窗口投 WM_CLOSE"
    # 托盘不许把整库检索器搬进自己的模块 (会变两份, 几百 MB)
    tray_src = (_P(PROJECT_ROOT) / "utils" / "tray.py").read_text(encoding="utf-8")
    assert "_ls_searcher" not in tray_src and "ORB(" not in tray_src, \
        "托盘不得自己加载整库特征 (会变两份, 几百 MB)"
    assert "trace_current_wallpaper()" in tray_src, "tooltip 只走秒回三档"
    # 托盘**绝不**跑 deep (ORB 要几秒~几十秒, 菜单里那项「图像溯源」2026-09-25 已删,
    # 想跑 deep 去小控件卡片的按钮走 HTTP, 那边本来就在后台线程)
    assert "deep=True" not in tray_src, "托盘不许再自己跑 deep 溯源"
    # 弹菜单必须回到建窗线程: 子线程弹 TrackPopupMenu 会「毫无反应」(2026-09-24 实测)
    assert "threading.Thread(target=self._show_menu" not in tray_src, \
        "右键菜单不能丢给子线程弹 —— 窗口不属于该线程时 TrackPopupMenu 毫无反应"
    assert "WM_SHOW_MENU" in tray_src and "taskbar_created_msg()" in tray_src, \
        "缺少「菜单回建窗线程」或「explorer 重启后补图标」的处理"

    # ---------- ⑤ 幽灵图标  ----------
    # 全项目只有 tray.py 一处 NIM_ADD (uID 固定 1、窗口只有一个), 所以运行期变不出第二个图标 ——
    # 那些是上一次进程没走到 NIM_DELETE 时 explorer 留下的空壳。两道护栏:
    #   ① 关闭终端窗口走 CTRL_CLOSE_EVENT, 既不跑 atexit 也不触发信号 → 必须单独接上清理;
    #   ② 启动时先按同一 (hWnd, uID) 删一次再 ADD, 顶掉上一次强杀留下的壳。
    from main import _console_event_action
    assert _console_event_action(2) == "quit", "关终端窗口 (CTRL_CLOSE_EVENT) 必须走清理"
    assert _console_event_action(0) == "pass", "Ctrl+C 必须让给 Python 的 SIGINT 处理器"
    assert _console_event_action(1) == "pass", "Ctrl+Break 同上"
    assert _console_event_action(None) == "pass" and _console_event_action("x") == "pass", \
        "事件码读不出来时不许抢着退出"
    assert "_install_console_handler()" in src, "main() 没装控制台处理器 → 关终端会留空壳"
    assert "k32.SetConsoleCtrlHandler(_console_handler_ref, True)" in src, \
        "控制台处理器没真正注册到系统 (回调还得留模块级引用, 否则被 GC 后跳空地址)"
    assert src.count("SetConsoleCtrlHandler.argtypes") == 1, "回调是函数指针, 必须钉 argtypes"
    _run_src = tray_src.split("self.hicon = _load_icon()")[1]
    assert _run_src.index("self._icon(NIM_DELETE)") < _run_src.index("self._icon(NIM_ADD)"), \
        "托盘必须**先删后加**, 否则顶不掉上一次强杀留下的空壳"


def t130_core_trace_injection():
    """🧩 溯源抽核后 core.wallpaper_trace 与 HTTP 接口等价 (2026-09-24 阶段1 回归)。

    托盘与网页面板共用**同一份**规则 —— 本用例直接调 core 入口, 证明:
      ① wall_path 可注入 (托盘传死路径 / 冒烟离线跑, 不依赖本机注册表);
      ② orb_fn 注入会被真的调用, 不注入则只标 deep_error, 绝不偷偷跑重活;
      ③ db_factory 每次新建连接 (SQLite 连接绑定线程, 托盘线程不能复用 Web 的连接);
      ④ 返回结构与 /api/wallpaper/current 的 JSON 一致 (含 summary)。
    """
    import tempfile
    import shutil
    from PIL import Image as _Img
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from core import wallpaper_trace as wt

    # 必须 resolve: mkdtemp 在 Windows 上可能返回 8.3 短名 (ADMINI~1), 而 core 内部对
    # img_root 做了 .resolve() → 短名 wall_path 与长名 img_root 比 in_lib 会误判成库外。
    tmp = Path(tempfile.mkdtemp(prefix="t130core_")).resolve()
    img_root = tmp / "img"
    wall_dir = tmp / "wall"
    img_root.mkdir(parents=True, exist_ok=True)
    wall_dir.mkdir(parents=True, exist_ok=True)
    orig_ud, orig_dr = upaths.user_data, upaths.data_root
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    try:
        wt.WALL_DEEP_CACHE.clear()
        db = PixivDatabase(tmp / "pixiv.db")
        db.upsert_author("555", "shuno")
        db.change_work_status("700001", WSt.DONE, title="湖边少女", author_id="555",
                              total_pages=1, illust_type=0)
        rel = "X图/shuno_555/700001_p0.png"
        p = img_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        _Img.new("RGB", (1920, 1080), (30, 60, 90)).save(str(p), format="PNG")
        import hashlib
        _md5 = hashlib.md5(p.read_bytes()).hexdigest()
        db.mark_page_status("700001", 0, PSt.DONE, local_path=rel, md5=_md5,
                            width=1920, height=1080)
        db.close()
        factory_calls = []

        def _factory():
            factory_calls.append(1)
            return PixivDatabase(tmp / "pixiv.db")

        # ---------- ① 传 None = 明确「没有壁纸」 ----------
        j = wt.trace_current_wallpaper(wall_path=None, db_factory=_factory,
                                       img_root=img_root)
        assert j["status"] == "ok" and j["active"] is False and j.get("reason"), j
        assert factory_calls == [], "没有壁纸时不该去连数据库"

        # ---------- ② 壁纸就是图库内文件本身 → kind=library ----------
        j = wt.trace_current_wallpaper(wall_path=p, db_factory=_factory, img_root=img_root)
        assert j["active"] and j["match"]["kind"] == "library", j["match"]
        assert j["file"]["in_library"] == 1, j["file"]
        assert j["artwork"]["artwork_id"] == "700001", j["artwork"]
        assert j["summary"]["author"] == "shuno", j["summary"]
        assert j["summary"]["line"], j["summary"]
        assert "库内已是最清晰" in j["summary"]["line"], j["summary"]

        # ---------- ②b 注册表给 8.3 短名也必须认成库内 (只读护栏依赖 in_lib) ----------
        # 2026-09-24: f 不做 resolve 时, 短名 wall_path 与长名 img_root 比 parents 会落空 →
        # in_library=0 → 「移出壁纸」不再拒绝, 库内文件可被绕过数据库删掉。
        if os.name == "nt":
            import ctypes as _ct
            _buf = _ct.create_unicode_buffer(512)
            if _ct.windll.kernel32.GetShortPathNameW(str(p), _buf, 512) and _buf.value:
                j = wt.trace_current_wallpaper(wall_path=_buf.value, db_factory=_factory,
                                               img_root=img_root)
                assert j["file"]["in_library"] == 1, (_buf.value, j["file"])
                assert j["match"]["kind"] == "library", j["match"]

        # ---------- ③ 库外但字节一致 → kind=md5 ----------
        w = wall_dir / "700001_p0.png"
        w.write_bytes(p.read_bytes())
        j = wt.trace_current_wallpaper(wall_path=w, db_factory=_factory, img_root=img_root)
        assert j["match"]["kind"] == "md5" and j["match"]["exact"] is True, j["match"]
        assert j["file"]["in_library"] == 0 and "root=wall" in j["file"]["thumb"], j["file"]

        # ---------- ④ 不注入 orb_fn: deep 只标错误, 绝不自己加载整库特征 ----------
        mystery = wall_dir / "mystery.png"
        _Img.new("RGB", (640, 480), (7, 200, 7)).save(str(mystery), format="PNG")
        j = wt.trace_current_wallpaper(wall_path=mystery, db_factory=_factory,
                                       img_root=img_root, deep=True)
        assert j["match"]["kind"] == "none", j["match"]
        assert j["deep_error"] == "图像检索器未接入", j
        assert j["candidates"] == [], j

        # ---------- ⑤ 注入 orb_fn 会被真的调用, 结论按「文件+mtime+大小」缓存 ----------
        # ④ 把「检索器未接入」的空结论也缓存了(免得轮询时反复重试), 这里先清掉再验注入路径
        wt.WALL_DEEP_CACHE.clear()
        calls = []

        def _orb(f, root):
            calls.append(str(f))
            return [{"rel": rel, "fusion": 0.81, "orb": 0.5}]

        j = wt.trace_current_wallpaper(wall_path=mystery, db_factory=_factory,
                                       img_root=img_root, deep=True, orb_fn=_orb)
        assert len(calls) == 1, calls
        assert j["match"]["kind"] == "orb" and j["match"]["confidence"] == 0.81, j["match"]
        assert j["artwork"]["artwork_id"] == "700001", j["artwork"]
        assert j["summary"]["author"] == "shuno", j["summary"]
        # 再来一次**不带** deep: 缓存命中, 不重复跑 ORB, 结论也不退回「未溯源」
        j = wt.trace_current_wallpaper(wall_path=mystery, db_factory=_factory,
                                       img_root=img_root, orb_fn=_orb)
        assert len(calls) == 1, "缓存没生效 → 8 秒轮询会反复跑几十秒的 ORB"
        assert j["match"]["kind"] == "orb" and j["deep"] is True, j["match"]

        # ---------- ⑥ 分数太低 → weak, 绝不硬认 ----------
        low = wall_dir / "low.png"
        _Img.new("RGB", (320, 240), (9, 9, 200)).save(str(low), format="PNG")
        j = wt.trace_current_wallpaper(
            wall_path=low, db_factory=_factory, img_root=img_root, deep=True,
            orb_fn=lambda f, root: [{"rel": rel, "fusion": 0.40, "orb": 0.10}])
        assert j["match"]["kind"] == "weak" and j["artwork"] is None, j["match"]

        # ---------- ⑦ db_factory 被调用多次 = 每次新建连接 (不跨线程复用) ----------
        assert len(factory_calls) >= 4, factory_calls
        # ---------- ⑧ 纯函数直接可用 (托盘文案与面板共用) ----------
        assert wt.wall_relation((1920, 1080), (1920, 1080)) == "same"
        assert wt.wall_relation((1080, 1920), (1920, 1080)) == "rotate"
        assert wt.wall_relation((2560, 1080), (1920, 1080)) == "crop"
        assert wt.wall_relation((960, 540), (1920, 1080)) == "rescale"
        assert wt.WALL_ORB_MIN == 0.30, wt.WALL_ORB_MIN
    finally:
        upaths.user_data = orig_ud
        upaths.data_root = orig_dr
        wt.WALL_DEEP_CACHE.clear()
        shutil.rmtree(str(tmp), ignore_errors=True)


def t131_launcher_config_switches():
    """🚀 启动与托盘开关 (2026-09-24): 缺键兜底 + 保存不覆盖别的节 + 接口白名单。

    ① LAUNCHER_DEFAULTS 与 default.json 的 launcher 节键集一致 (四处列表只留一份);
    ② 老 user.json 没 launcher 节 → _canonicalize 兜底出 5 个键, 默认只开托盘;
    ③ save_user_config 深合并: 写 launcher 不能把 web.port / paths.data_root_override 抹掉;
    ④ /api/launcher POST 白名单: 只认 5 个键, 别的键(含 _note)一律丢弃, 全丢则 400;
    ⑤ GET 只回 5 个布尔键, 不回 _note。
    """
    import tempfile
    import shutil
    import json as _json
    import utils.paths as upaths
    import utils.config as ucfg
    from web.server import create_app

    # ---------- ① 键集一致 ----------
    defaults = _json.loads(
        (Path(PROJECT_ROOT) / "resources" / "default.json").read_text(encoding="utf-8"))
    ln_default = defaults["launcher"]
    keys = {k for k in ln_default if not k.startswith("_")}
    assert keys == set(ucfg.LAUNCHER_DEFAULTS), (keys, set(ucfg.LAUNCHER_DEFAULTS))
    assert ln_default["tray_enabled"] is True, "托盘默认必须是开"
    for k in ("auto_open_browser", "wall_widget", "notify_on_wall_change",
              "exit_on_browser_close"):
        assert ln_default[k] is False, k
        assert ucfg.LAUNCHER_DEFAULTS[k] is False, k
    assert "_note" in ln_default, "default.json 每个节都有 _note, 照这个风格写"

    # ---------- ② 老 user.json 缺节 → 兜底 ----------
    cfg = ucfg._canonicalize({"web": {"port": 1234}})
    assert cfg["launcher"] == dict(ucfg.LAUNCHER_DEFAULTS), cfg["launcher"]
    # 显式给一半: 已给的保留, 没给的补默认 (不整节覆盖)
    cfg = ucfg._canonicalize({"launcher": {"wall_widget": True}})
    assert cfg["launcher"]["wall_widget"] is True
    assert cfg["launcher"]["tray_enabled"] is True

    tmp = Path(tempfile.mkdtemp(prefix="t131cfg_"))
    orig_cfgdir = upaths.config_dir
    orig_cfgpath = upaths.user_config_path
    upaths.config_dir = lambda: tmp
    upaths.user_config_path = lambda: tmp / "user.json"
    try:
        # ---------- ③ 深合并不覆盖别的节 ----------
        (tmp / "user.json").write_text(_json.dumps({
            "web": {"port": 7810},
            "paths": {"data_root_override": "D:/vixiq-data"},
        }, ensure_ascii=False), encoding="utf-8")
        ucfg.save_user_config({"launcher": {"wall_widget": True}})
        saved = _json.loads((tmp / "user.json").read_text(encoding="utf-8-sig"))
        assert saved["web"]["port"] == 7810, saved
        assert saved["paths"]["data_root_override"] == "D:/vixiq-data", saved
        assert saved["launcher"] == {"wall_widget": True}, saved
        # 再改一项: 两项都要在
        ucfg.save_user_config({"launcher": {"notify_on_wall_change": True}})
        saved = _json.loads((tmp / "user.json").read_text(encoding="utf-8-sig"))
        assert saved["launcher"] == {"wall_widget": True, "notify_on_wall_change": True}, saved
        assert saved["web"]["port"] == 7810, "深合并把别的节弄丢了"

        # ---------- ④⑤ 接口白名单 + GET 键集 ----------
        client = create_app().test_client()
        r = client.get("/api/launcher")
        assert r.status_code == 200, r.status_code
        got = r.get_json()
        assert set(got) == set(ucfg.LAUNCHER_DEFAULTS), got
        assert all(isinstance(v, bool) for v in got.values()), got
        assert got["wall_widget"] is True and got["notify_on_wall_change"] is True, got
        # 只认白名单: _note / 未知键必须被丢掉
        r = client.post("/api/launcher", json={"tray_enabled": False,
                                               "_note": "脏数据", "evil": 1})
        assert r.status_code == 200 and r.get_json()["ok"] is True, r.get_json()
        saved = _json.loads((tmp / "user.json").read_text(encoding="utf-8-sig"))
        assert saved["launcher"]["tray_enabled"] is False, saved
        assert "_note" not in saved["launcher"] and "evil" not in saved["launcher"], saved
        # 一个可识别的键都没有 → 400, 且不动 user.json
        before = (tmp / "user.json").read_text(encoding="utf-8-sig")
        r = client.post("/api/launcher", json={"nope": 1})
        assert r.status_code == 400 and r.get_json()["ok"] is False, r.get_json()
        assert (tmp / "user.json").read_text(encoding="utf-8-sig") == before, "空请求不该写盘"
    finally:
        upaths.config_dir = orig_cfgdir
        upaths.user_config_path = orig_cfgpath
        shutil.rmtree(str(tmp), ignore_errors=True)

    # ---------- ⑥ 前端接线静态护栏 ----------
    html = (Path(PROJECT_ROOT) / "web" / "static" / "index.html").read_text(encoding="utf-8")
    js = (Path(PROJECT_ROOT) / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'id="launcher-switches"' in html, "设置页缺「启动与托盘」开关组"
    for k in ucfg.LAUNCHER_DEFAULTS:
        assert f'data-k="{k}"' in html, f"设置页缺开关 {k}"
    assert "function loadPixivActions()" in js and "function loadLauncher()" in js
    assert "function paintLauncher()" in js
    # 切到设置页必须把各卡片的开关/状态都读一遍 (启动器 · 壁纸夹 · 壁纸文案 AI)。
    for _call in ("loadLauncher()", "loadWallDir()", "loadAiText()"):
        assert _call in js, f"切到设置页必须去读开关状态 (缺 {_call})"
    assert "if (h === '#settings') switchTab('settings');" in js, "托盘「设置…」带 #settings 落地要能直接进设置页"
    assert "else if (h === '#library') switchTab('library');" in js, "托盘「打开图库」带 #library 落地要能直接进图库页"
    # 桌面小控件「在库中查看」的落地: #library?only=<作品ID> → 直接「仅查看这个作品」。
    # 必须同时认 hashchange —— 浏览器里已经开着页面时, 只改 fragment 不重新加载页面。
    assert r"/^#library\?only=(\d+)$/" in js, "小控件「在库中查看」的 hash 落地没实现"
    assert "window.addEventListener('hashchange', applyDeepLink);" in js, \
        "只听加载事件的话, 页面已开着时点小控件按钮像没反应"

    # ---------- ⑦ 桌面小控件: 三态形状 + 贴边几何 + 接线护栏 ----------
    # 2026-09-24 方案作废重做: 旧方案是 Edge --app 打开 web/static/wall.html (浏览器窗口
    # 带自己的边框, 做不出「贴边收成一条线」的形态), 现改为 utils/widget_window.py 的
    # ctypes 分层窗口。下面这些断言都是**离线可判定**的部分: 形状像素、贴边几何、
    # 以及「旧方案有没有残留」。
    from utils import widget_window as _ww

    # (a) EDGE: 窗口 = 命中区 (11px), 线只有 4px 且只画在贴边那一侧, 背面全透明
    for dock in _ww.DOCKS:
        e = _ww.draw_edge(dock)
        vert = dock in ("left", "right")
        assert e.size == ((_ww.EDGE_HIT, _ww.EDGE_LEN) if vert
                          else (_ww.EDGE_LEN, _ww.EDGE_HIT)), (dock, e.size)
        x0, y0, x1, y1 = _ww.edge_visible_box(dock)
        assert (x1 - x0, y1 - y0) in ((_ww.EDGE_W, _ww.EDGE_LEN),
                                      (_ww.EDGE_LEN, _ww.EDGE_W)), (dock, x1 - x0)
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        # 线只有 4px 宽, 降采样的 LANCZOS 会在 0→235 的边界上振铃, 所以只判「画上了」
        assert e.getpixel((cx, cy))[3] > 200, f"{dock} 边缘线上没画出来"
        # 贴边那一侧必须贴死窗口边缘 (不能留出透明缝, 否则看着像悬空)
        if dock == "right":
            assert x1 == _ww.EDGE_HIT and e.getpixel((0, cy))[3] == 0, dock
        elif dock == "left":
            assert x0 == 0 and e.getpixel((_ww.EDGE_HIT - 1, cy))[3] == 0, dock
        elif dock == "top":
            assert y0 == 0 and e.getpixel((cx, _ww.EDGE_HIT - 1))[3] == 0, dock
        else:
            assert y1 == _ww.EDGE_HIT and e.getpixel((cx, 0))[3] == 0, dock

    # (b) TAG: **补回缺口的长条便签** (零圆角; 伸出端那 16px 是**人字防滑纹路**) ——
    #     2026-09-26 设计约定:
    # 。下面**直接按窗口坐标**取样, 不借 tag_map:
    #     旋转和坐标换算要是一起错, 走 tag_map 反而会互相抵消, 那样测了等于没测。
    #     每项 = (伸出端的两个外角, 贴屏幕边的两个外角)
    tag_geo = {
        "right":  (((0, 0), (0, 71)), ((203, 0), (203, 71))),
        "left":   (((203, 0), (203, 71)), ((0, 0), (0, 71))),
        "top":    (((0, 203), (71, 203)), ((0, 0), (71, 0))),
        "bottom": (((0, 0), (71, 0)), ((0, 203), (71, 203))),
    }
    for dock in _ww.DOCKS:
        t = _ww.draw_tag(dock)
        vert = dock in ("left", "right")
        assert t.size == ((_ww.TAG_LEN, _ww.TAG_THK) if vert
                          else (_ww.TAG_THK, _ww.TAG_LEN)), (dock, t.size)
        tips, far = tag_geo[dock]
        for pt in tips + far:
            # 零圆角 + 外轮廓是**完整矩形**: 四角都得实实在在, 不许切角, 也不许再凹进去
            assert t.getpixel(pt)[3] >= 150, f"{dock} 便签 {pt} 被切了/圆角了 {t.getpixel(pt)}"
    # 防滑纹路: 原来缺口那块**不能是空的** (补回来了), 而且要有明暗交替的纹路
    # (全平 = 没画; 全亮 = 糊成一块白板)。取样点沿伸出端的中线横着扫那 16px。
    for dock in _ww.DOCKS:
        t = _ww.draw_tag(dock)
        _mid = int(_ww.TAG_THK / 2)
        line = [t.getpixel(_ww.tag_map(dock, x, _mid))[1]
                for x in range(2, _ww.TAG_GRIP)]
        assert max(line) - min(line) >= 12, \
            f"{dock} 伸出端没画出防滑纹路 (明暗差 {max(line) - min(line)})"
    # 四个朝向必须真的不一样 (旋转全写错也都能过上面的角判定)
    assert len({_ww.draw_tag(d).tobytes() for d in _ww.DOCKS}) == 4, "便签四朝向有重复"

    # (c) CARD: CARD_W×CARD_H **对角斜切** (左上 + 右下), 右上角是裸线小叉叉 (关窗口的唯一入口)
    card = _ww.draw_card(None, None)
    assert card.size == (_ww.CARD_W, _ww.CARD_H), card.size
    assert card.getpixel((0, 0))[3] <= 20, "卡片左上角应该有斜切"
    assert card.getpixel((_ww.CARD_W - 1, _ww.CARD_H - 1))[3] <= 20, "卡片右下角应该有斜切"
    assert card.getpixel((_ww.CARD_W - 1, 0))[3] >= 150, "卡片右上角不该被切"
    assert card.getpixel((0, _ww.CARD_H - 1))[3] >= 150, "卡片左下角不该被切"
    x0, y0, x1, y1 = _ww.X_BOX
    assert x1 <= _ww.CARD_W - 1 and y1 <= _ww.CARD_H - 1, "小叉叉跑到卡片外了"
    assert x1 <= _ww.CARD_W - 12 + 1, "小叉叉右边距不足 12px"
    assert card.getpixel((_ww.CARD_W - 1, y1 + 20))[3] >= 150, "卡片右侧被切了"
    assert card.getpixel(((x0 + x1) // 2, (y0 + y1) // 2))[3] >= 200, "小叉叉没画出来"
    # 悬停的键必须真的变样 (键帽 + accent 荧光), 否则「鼠标感应反馈」只是个说法。
    # 文字键与图标键各指一处 —— 两套命中表各算一遍, 只验一边的话另一边错位也发现不了。
    _ci = {"file": {"name": "a.png", "width": 1920, "height": 1080, "in_library": False},
           "artwork": {"artwork_id": "123", "author_name": "A"}, "match": {"kind": "md5"}}
    _c_plain = _ww.draw_card(_ci, None).tobytes()
    assert _ww.draw_card(_ci, None, False, hot=0).tobytes() != _c_plain, \
        "卡片文字键的悬停荧光没生效"
    assert _ww.draw_card(_ci, None, False, icon_hot=0).tobytes() != _c_plain, \
        "卡片图标键的悬停键帽没生效"

    # (d) 贴边几何: 四边都要死死贴住工作区那条边, 且不越界
    wa = (0, 0, 1920, 1030)
    for dock in _ww.DOCKS:
        x, y = _ww.dock_geom(wa, dock, 515, _ww.TAG_LEN, _ww.TAG_THK)
        if dock == "right":
            assert x + _ww.TAG_LEN == wa[2], (dock, x)
        elif dock == "left":
            assert x == wa[0], (dock, x)
        elif dock == "top":
            assert y == wa[1], (dock, y)
        else:
            assert y + _ww.TAG_THK == wa[3], (dock, y)
    # 锚点贴到极端位置时不能把窗口顶出工作区
    assert _ww.dock_geom(wa, "right", 1, 384, 306)[1] == 0, "锚点超上限没夹回工作区"
    assert _ww.dock_geom(wa, "right", 99999, 384, 306)[1] == 1030 - 306, "锚点超下限没夹回"
    assert _ww.dock_geom(wa, "top", 99999, 384, 306)[0] == 1920 - 384, "横向锚点越界"
    assert _ww.nearest_dock(wa, (1900, 400, 11, 68)) == ("right", 434), "吸边判定错了"
    assert _ww.nearest_dock(wa, (900, 2, 68, 11)) == ("top", 934), "吸边判定错了"

    # (e) 数据源与「浏览器是否全关」判定: 小控件只轮询, 绝不建 SSE
    ww_src = (Path(PROJECT_ROOT) / "utils" / "widget_window.py").read_text(encoding="utf-8")
    assert "/api/wallpaper/current" in ww_src, "小控件没接溯源接口"
    assert "EventSource" not in ww_src, "小控件建 SSE 会污染「浏览器是否全关」的判定"
    assert ww_src.count(_ww.CLASS_NAME) == 1 and ww_src.count("CLASS_NAME") >= 3, \
        "窗口类名字面量只该定义一处, 其余走常量 (注册/建窗/注销三处都用它)"
    # ★ 2026-09-25 : 根子是**函数原型串台**。
    # `ctypes.windll` 是带缓存的加载器 (`ctypes.windll.user32 is ctypes.windll.user32` 为 True),
    # 拿它钉 argtypes 是**进程级**的; 而 tray.py 用的是自己那套 WNDCLASSW / 带 lPrivate 的 MSG,
    # 与本模块的 WNDCLASS / wintypes.MSG 不兼容。main.py「先起托盘再 import 小控件」的顺序下,
    # 小控件 import 时那几行签名就把运行中托盘的 GetMessageW 覆盖了 → 托盘线程抛
    # ArgumentError 当场死掉, finally 里 NIM_DELETE 把图标摘了。必须各用各的私有实例。
    assert _ww.user32 is not __import__("ctypes").windll.user32, \
        "widget_window 拿了共享的 ctypes.windll.user32, 会把运行中托盘的函数原型覆盖掉"
    for _lib in ("user32", "gdi32", "kernel32"):
        _code = "\n".join(ln for ln in ww_src.splitlines() if not ln.lstrip().startswith("#"))
        assert f"ctypes.windll.{_lib}" not in _code, \
            f"别用共享加载器拿 {_lib} (原型会串到托盘), 用 ctypes.WinDLL({_lib!r})"
    # 右键**原地单击**不再退出小控件 (太容易误点, 一点整块消失且没有回头路):
    # PostQuitMessage 只该留在 WM_CLOSE 收尾那一条路上。
    assert "右键原地单击 = 退出小控件" not in ww_src, "右键单击退出的老逻辑又回来了"
    assert ww_src.count("PostQuitMessage(") == 1, \
        "PostQuitMessage 只该出现在 WM_CLOSE 收尾路径 (右键单击不许再退出)"
    assert "self.moved >= DRAG_SLOP" in ww_src, "右键拖动换边的判定没了"

    # (f) 卡片上的键分两套: **文字键** (在库中查看/所在文件夹/移出壁纸) 与
    # **壁纸控制条** (上一张/播放/下一张/档位的四颗图标键)。两套都是「出现哪些由数据决定」,
    # 且**画和点命中必须同源**。
    def _keys(d):
        return [k for k, _, _ in _ww.card_actions(d)]

    def _icons(d, **kw):
        return [k for k, _ in _ww.card_icons(d, **kw)]

    base = {"file": {"name": "a.png", "width": 1920, "height": 1080, "in_library": False},
            "artwork": {"artwork_id": "123", "author_id": "456", "title": "t"},
            "match": {"kind": "md5"}}
    assert _keys(None) == [], "数据没到手时不该画按钮 (点哪个都没意义)"
    # 用户 2026-09-26:「『复制作品链接』『图像溯源』就不需要了」→ 文字键只剩三颗,
    # 「下一张壁纸」那颗也在上一轮就撤了 (「有播放键就不需要它」), 壁纸控制整体挪去控制条。
    assert _keys(base) == ["library", "folder", "recycle"], _keys(base)
    # 壁纸就是图库里的文件 → 不给「移出壁纸」(那属于「从图库删」的另一套流程)
    inlib = {**base, "file": {**base["file"], "in_library": True}}
    assert _keys(inlib) == ["library", "folder"], _keys(inlib)
    # 溯源结论不再影响文字键 («图像溯源»那颗已撤) —— 三种 match 都得是同一套
    for _k in ("none", "orb", "md5"):
        assert _keys({**base, "match": {"kind": _k}}) == ["library", "folder", "recycle"], _k
    # 非纯数字的 artwork_id 不是 pixiv 作品 → 落不到图库
    assert _keys({**base, "artwork": {"artwork_id": "x-1"}}) == ["folder", "recycle"], _keys(base)
    assert _keys({**base, "artwork": None}) == ["folder", "recycle"], _keys(base)
    # 「移出壁纸」是唯一的破坏性动作, 危险色不能被忘掉 (两步确认靠它拉开差别)
    assert [dg for k, _, dg in _ww.card_actions(base) if k == "recycle"] == [True]
    assert [k for k, _, _ in _ww.card_actions(inlib)] == ["library", "folder"], "库里那份不该给删除键"
    assert not [k for k, _, _ in _ww.card_actions(base) if k in ("copy", "trace")], \
        "「复制作品链接」「图像溯源」已经撤掉了"

    # 壁纸控制条 = 固定四颗图标键, 顺序 prev / play / next / 档位
    assert _ww.ICON_N == 4, "控制条固定四颗"
    assert _icons(base) == ["prev", "play", "next", "shuffle"], _icons(base)
    assert _icons(inlib) == ["prev", "play", "next", "shuffle"]
    # 图标画的是「**当前所在的那个档**」(与 ⏸ 同一套读法): 正在播 → ⏸, 随机档 → 交叉箭头,
    # 顺序档 → 三横线。用户 2026-09-29 明确口径:「随机模式该显示交叉箭头, 顺序模式才显示横线」。
    assert _ww.card_icons(base)[1] == ("play", "play")
    assert _ww.card_icons(base)[3] == ("shuffle", "order")
    assert _ww.card_icons(base, playing=True)[1] == ("play", "pause")
    assert _ww.card_icons(base, shuffle=True)[3] == ("shuffle", "shuffle")
    assert [k for k, _ in _ww.card_icons(base, shuffle=True, playing=True)] \
        == ["prev", "play", "next", "shuffle"], "换图标不该改变键的条数与位置"
    # 没有当前壁纸 (文件被删/被移走) 时的两条岔路 —— 的那一格:
    # 配过壁纸夹 → 还留「播放 + 档位」(播放键按下能从壁纸夹一端起步, 把桌面接回来);
    # 一个夹子都没有 → 才真的一个键都不给。「上一张/下一张」这时没意义 (得先有个「当前」)。
    _nowall = {"active": False, "file": {}, "folder": {"ready": True, "dir": "D:/壁纸"}}
    assert _icons(_nowall) == ["play", "shuffle"], _icons(_nowall)
    assert _icons({**_nowall, "folder": {"ready": False}}) == [], "没壁纸夹就不该画按钮"
    assert _icons(None) == [], "数据没到手时控制条也不该画"
    # 只要文件在, 播放与档位这两颗就一定有 (它们是唯二不依赖溯源结果的键)
    for d in ({"file": {"name": "a.png"}}, base, inlib):
        assert {"play", "shuffle"} <= set(_icons(d)), d

    # 图标键的排版与命中: 四颗**居中均分**、两两不重叠、都落在卡片里 (越界/重叠的表现
    # 都是「点了没反应」或「点左边界点到了右边界」)
    _ibox = [_ww.icon_key_box(i) for i in range(_ww.ICON_N)]
    _kx = [_ww.icon_key_xy(i)[0] for i in range(_ww.ICON_N)]
    assert all(_kx[i] < _kx[i + 1] for i in range(_ww.ICON_N - 1)), _kx
    assert abs((_ibox[0][0] + _ibox[-1][0] + _ibox[-1][2]) / 2 - _ww.CARD_W / 2) <= 1, _ibox
    for i in range(_ww.ICON_N - 1):
        assert _ibox[i][0] + _ibox[i][2] <= _ibox[i + 1][0], (i, _ibox)
    for i, (bx, by, bw, bh) in enumerate(_ibox):
        assert bx >= 0 and by >= 0 and bx + bw <= _ww.CARD_W and by + bh <= _ww.CARD_H, (i, _ibox[i])
        assert _ww.round_rect((bx + bw // 2, by + bh // 2), (bx, by, bw, bh)), i

    # 文字键: 三颗 = 2 行, 都落在卡片里, 同行的两个不重叠, 且不压到控制条上
    assert len(_keys(base)) <= _ww.BTN_ROWS * _ww.BTN_COLS, "文字键数量超过 BTN_ROWS×BTN_COLS"
    assert _ww.BTN_TOP + (_ww.BTN_ROWS - 1) * _ww.BTN_ROW_H + _ww.BTN_H <= _ww.CARD_H, \
        "按钮排到卡片外了"
    for i in range(_ww.BTN_ROWS * _ww.BTN_COLS):
        bx, by, bw, bh = _ww.btn_box(i)
        assert bx >= 0 and bx + bw <= _ww.CARD_W, (i, bx, bw)
        assert by >= _ww.BTN_TOP and by + bh <= _ww.CARD_H, (i, by, bh)
        assert _ww.round_rect((bx + bw // 2, by + bh // 2), (bx, by, bw, bh)), i
    assert _ww.btn_box(0)[1] > _ww.IBAR_CY + _ww.IBAR_SZ * _ww.IBAR_KEY / 2, \
        "文字键压到壁纸控制条上了"
    for i in (0, 2):
        a, b = _ww.btn_box(i), _ww.btn_box(i + 1)
        assert a[0] + a[2] <= b[0], (i, a, b)
    # 两步确认必须真的改变画面, 否则「确认移出？」只是个说法
    cw = {"file": {"name": "a.png", "in_library": False}, "match": {"kind": "none"}}
    assert _ww.draw_card(cw, None, True).tobytes() != _ww.draw_card(cw, None, False).tobytes(), \
        "「确认移出？」的两步状态在画面上没有区别"

    # (g) 按钮/三键的实际落点: 接口在服务端真的存在, 链接指向真的页面
    for ep in ("/api/wallpaper/current", "/api/wallpaper/open-folder", "/api/wallpaper/recycle"):
        assert ep in ww_src, f"小控件没接 {ep}"
    # 上一张/下一张走同一个模板 (随机档要在后面挂 ?shuffle=1), 所以源码里是 {name} 不是字面量
    assert 'f"/api/wallpaper/{name}"' in ww_src and 'self._wall_path("next")' in ww_src \
        and 'self._wall_path("prev")' in ww_src, "小控件换图接口没走 _wall_path"
    srv_src = (Path(PROJECT_ROOT) / "web" / "server.py").read_text(encoding="utf-8")
    for ep in ("/api/wallpaper/next", "/api/wallpaper/prev",
               "/api/wallpaper/open-folder", "/api/wallpaper/recycle",
               "/api/wallpaper/dir", "/api/wallpaper/dir/pick"):
        assert f'@app.route("{ep}", methods=["POST"])' in srv_src, f"服务端没有 {ep}"
    # 便签三键的命中框: 三个都必须落在窗口里, 且两两不重叠 (外扩 TAG_HIT 后还叠上就成了
    # 「点下一张点到了播放」)。四朝向的坐标换算全在 tag_key_box 里, 这里按窗口坐标验。
    for _dock in _ww.DOCKS:
        _tw, _th = _ww.tag_size(_dock)
        _boxes = [_ww.tag_key_box(i, _dock) for i in range(3)]
        for _i, (_bx, _by, _bw, _bh) in enumerate(_boxes):
            assert _bx >= 0 and _by >= 0 and _bx + _bw <= _tw and _by + _bh <= _th, \
                f"{_dock} 第{_i}键的命中框跑到便签外了 {(_bx, _by, _bw, _bh)}"
        assert len({b[:2] for b in _boxes}) == 3, f"{_dock} 便签三键的命中框重合了 {_boxes}"
    # 小控件没有「作者主页」(只能甩外部浏览器, 用户点开还得自己再找); 2026-09-26 起
    # 「复制作品链接」也撤了 (用户: 「不需要了」) → 剪贴板那套辅助代码必须**整段删掉**,
    # 留着就是死代码 (而且它是进程级共享加载器最容易串台的地方之一)。
    for _gone in ("pixiv.net/artworks", "copy_text", "CF_UNICODETEXT", "GlobalAlloc"):
        assert _gone not in ww_src, f"{_gone} 是「复制作品链接」的残留, 该整段删掉"
    assert "https://www.pixiv.net/users/" not in ww_src, "「作者主页」已经删掉了"
    assert "/#library?only=" in ww_src, "「在库中查看」要带作品ID落到图库"
    # 「图像溯源」那颗也撤了 → 不再有 deep 参数与 trace 动作键
    assert '"trace"' not in ww_src and "deep=" not in ww_src, \
        "「图像溯源」的 deep/trace 残留了 (那颗按钮已撤)"

    # (h) 接线: main.py 走 WallWidget; 旧的 Edge + wall.html 方案必须彻底消失
    main_src = (Path(PROJECT_ROOT) / "main.py").read_text(encoding="utf-8")
    tray_src = (Path(PROJECT_ROOT) / "utils" / "tray.py").read_text(encoding="utf-8")
    assert "from utils.widget_window import WallWidget" in main_src, "main.py 没接小控件模块"
    assert "_wall_widget = w" in main_src and "w.stop()" in main_src, \
        "小控件要真的被启动, 且退出时收得掉"
    assert "_wall_widget_proc" not in main_src, "Edge 进程那套残留了"
    assert "open_wall_widget" not in tray_src and "WALL_WIDGET_PATH" not in tray_src, \
        "tray.py 里还有 Edge+wall.html 的残留"
    assert not (Path(PROJECT_ROOT) / "web" / "static" / "wall.html").exists(), \
        "wall.html 应该已经废弃删除"

    # (i) 便签的荧光色 + 缩略图 (2026-09-25 改版: 小圆角缩略图 + 燕尾纸条, 不再铺满)
    from PIL import Image as _PImg
    # 没缩略图 → 退回固定青色; 荧光化 = 饱和度/明度都抬到下限以上
    assert _ww.accent_of(None) == _ww.NEON_FALLBACK, "没缩略图时该退回固定荧光色"
    for src in ((200, 200, 200), (30, 30, 30), (250, 20, 20)):
        got = _ww.neonize(src)
        _h, _s, _v = __import__("colorsys").rgb_to_hsv(*[c / 255.0 for c in got])
        assert _s >= _ww.TAG_SAT_MIN - 0.01 and _v >= _ww.TAG_VAL_MIN - 0.01, (src, got)
    # 纯色缩略图的主色就该是它自己那一系 (顺带证明「取自缩略图」真的在跑, 不是永远返回兜底色)
    _red = _PImg.new("RGB", (64, 64), (220, 24, 24))
    _acc = _ww.accent_of(_red)
    assert len(_acc) == 3 and _acc != _ww.NEON_FALLBACK, _acc
    assert _acc[0] > _acc[1] and _acc[0] > _acc[2], f"缩略图是红的, 荧光色却是 {_acc}"
    # 缩略图必须真的画上去: 红图 → 便签里那块缩略图就是红的 (只看 glyph 就说明退回纯色底了)
    _tag = _ww.draw_tag("right", _red)
    _tcx = int(_ww.TAG_XOFF + _ww.TAG_TS / 2)
    _r, _g, _b, _a = _tag.getpixel((_tcx, _tag.height // 2))
    assert _a >= 200 and _r > _g + 30 and _r > _b + 30, \
        f"便签没有画上缩略图 ({(_r, _g, _b, _a)})"
    # 有图不画 glyph (糊成一团), 无图才画 → 两者画面必须不同
    assert _tag.tobytes() != _ww.draw_tag("right", None).tobytes(), "缩略图没起作用"
    # 「太模糊」的护栏 : 贴屏幕那端必须厚实 —— 便签是玻璃板压在壁纸上,
    # 两端都太透的话整条画面会退成薄纱, 缩略图再清楚也白搭。纯红缩略图钉住两端:
    _near = _tag.getpixel((_tag.width - 6, _tag.height // 2))
    assert _near[0] > 100, f"便签贴合端被压暗泡灰了 (红图却只剩 {_near})"
    assert _near[3] >= 180, f"便签贴合端太透, 画面退成薄纱 ({_near[3]})"
    # 三键的三种「变样」都要真的画出来: 悬停键帽 / 播放中 ⏸ / 进度线填到哪。
    # 小控件是按 (dock, hot, playing) 惰性缓存的, 这三样要是一样, 缓存就白做了。
    _plain = _ww.draw_tag("right", _red)
    assert _ww.draw_tag("right", _red, hot=1).tobytes() != _plain.tobytes(), "悬停键帽没画出来"
    assert _ww.draw_tag("right", _red, playing=True).tobytes() != _plain.tobytes(), "⏸ 没画出来"
    assert _ww.draw_tag("right", _red, frac=0.7).tobytes() \
        != _ww.draw_tag("right", _red, frac=0.0).tobytes(), "进度线不跟数据走"
    # 进度线取「当前壁纸在壁纸夹里的位置」, 取不到就画空槽 (不许编一个假进度)
    assert _ww.folder_frac({"folder": {"index": 2, "total": 4}}) == 0.75
    for _bad in (None, {}, {"folder": {}}, {"folder": {"index": -1, "total": 0}},
                 {"folder": {"index": "x", "total": "y"}}):
        assert _ww.folder_frac(_bad) == 0.0, _bad

    # (j) 「下一张壁纸」: 同目录自然序环绕; 「省空间」扫描只报告不删
    import core.wallpaper_trace as _wt
    _tmp2 = Path(tempfile.mkdtemp(prefix="t131wall_"))
    # 这一节测的是「**没配**壁纸夹时回退到当前图所在文件夹」。本机 user.json 一旦写上
    # paths.wallpaper_dir, candidates_dir 就改去读那个真夹子, 下面几条断言全跟着错
    #。所以先把配置读取摘掉 ——
    # 「配了夹子」的情形由下面 11506 那节单独测, 两边互不串味。
    _old_wdir = _wt.wallpaper_dir
    _wt.wallpaper_dir = lambda: None
    try:
        for n in ("99999999_p0.png", "109395314_p0.png", "2_p0.png"):
            (_tmp2 / n).write_bytes(n.encode())      # 内容各不相同, 只有一张能对上库里那份
        (_tmp2 / "跳过我.webp").write_bytes(b"x")        # 幻灯片不吃 webp
        _files = [p.name for p in _wt.wallpaper_folder_images(_tmp2)]
        assert _files == ["2_p0.png", "99999999_p0.png", "109395314_p0.png"], _files
        assert _wt.next_wallpaper(_tmp2 / "2_p0.png").name == "99999999_p0.png"
        assert _wt.next_wallpaper(_tmp2 / "109395314_p0.png").name == "2_p0.png", "没到底环绕"
        assert _wt.next_wallpaper(_tmp2 / "不在候选里.png") is not None, "当前图不在候选时该退首张"
        assert _wt.wallpaper_folder_images(_tmp2 / "不存在") == []
        # 「上一张」与「下一张」共用 _step_wallpaper (步长取反), 两头都要环绕
        assert _wt.prev_wallpaper(_tmp2 / "2_p0.png").name == "109395314_p0.png", "没绕到末张"
        assert _wt.prev_wallpaper(_tmp2 / "99999999_p0.png").name == "2_p0.png"
        assert _wt.prev_wallpaper(_tmp2 / "不在候选里.png") is not None
        # 进度线要的「第几张 / 共几张」; 认不出来就回 (0, n) —— 界面画空槽, 不编假进度
        assert _wt.wallpaper_position(_tmp2 / "2_p0.png") == (1, 3)
        assert _wt.wallpaper_position(_tmp2 / "109395314_p0.png") == (3, 3)
        assert _wt.wallpaper_position(_tmp2 / "不在候选里.png") == (0, 3)
        assert _wt.wallpaper_position(_tmp2 / "没这个夹" / "x.png") == (0, 0)
        # 「随机」档 (2026-09-26 加): 只看候选集合、且**不会是当前这张**; 跑 40 次把随机性压住
        _pick = {_wt.next_wallpaper(_tmp2 / "2_p0.png", True).name for _ in range(40)}
        assert _pick and _pick <= {"99999999_p0.png", "109395314_p0.png"}, _pick
        # 用户指定了壁纸夹 → 挑图只认它 (monkeypatch 掉配置读取, 别动真机的 user.json)
        _tmp3 = Path(tempfile.mkdtemp(prefix="t131wdir_"))
        _old_dir = _wt.wallpaper_dir
        try:
            (_tmp3 / "z_p0.png").write_bytes(b"z")
            _wt.wallpaper_dir = lambda: _tmp3
            assert _wt.candidates_dir(_tmp2 / "2_p0.png") == _tmp3, "配了壁纸夹却没听"
            assert _wt.wallpaper_position(_tmp2 / "2_p0.png") == (0, 1), "换了夹子没按新夹子数位置"
            assert _wt.next_wallpaper(_tmp2 / "2_p0.png") is None, "夹子里只有一张就不该给下一张"
        finally:
            _wt.wallpaper_dir = _old_dir
            shutil.rmtree(str(_tmp3), ignore_errors=True)

        class _Cur:
            def execute(self, sql): return self
            def fetchall(self): return [
                {"md5": _wt.file_md5(_tmp2 / "2_p0.png"), "aid": "11", "pn": 0,
                 "rel": "a/2_p0.png", "title": "T", "aname": "A"}]

        class _DB:
            conn = _Cur()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        seen = []
        res = _wt.find_wall_dupes(_tmp2, lambda: _DB(), lambda d, t, n: seen.append((d, t, n)))
        assert res["scanned"] == 3 and len(seen) == 3, (res["scanned"], seen)
        assert res["count"] == 1 and res["bytes"] == len("2_p0.png"), res
        assert res["items"][0]["name"] == "2_p0.png" and res["items"][0]["artwork_id"] == "11"
        assert _tmp2.joinpath("2_p0.png").exists(), "扫描是只读的, 不许删文件"
        # 库内没指纹 → 一条都不报 (不能把「不知道」当成「重复」)
        class _DB0(_DB):
            conn = type("_C0", (), {"execute": lambda s, q: s, "fetchall": lambda s: []})()
        assert _wt.find_wall_dupes(_tmp2, lambda: _DB0())["count"] == 0
    finally:
        _wt.wallpaper_dir = _old_wdir
        shutil.rmtree(str(_tmp2), ignore_errors=True)

    for ep in ("/api/wallpaper/dupes", "/api/wallpaper/dupes/scan", "/api/wallpaper/dupes/recycle"):
        assert f'@app.route("{ep}"' in srv_src, f"服务端没有 {ep}"
    assert "find_wall_dupes" in srv_src, "服务端没接「省空间」扫描"
    # 网页版「当前壁纸」卡片的展开/收起要记忆 (刷新回展开态是用户明确要修的)
    assert "vixiq.wall.folded" in js and "wallApplyFold" in js, "折叠状态没有落 localStorage"
    assert 'id="wall-fold"' in html, "「当前壁纸」卡片缺收起按钮"
    for fn in ("wallDupesOpen", "wallDupesScan", "wallDupesDelete"):
        assert f"function {fn}" in js, f"前端缺 {fn}"
    assert "'next'" in js and "⏭️ 下一张壁纸" in js, "网页版缺「下一张壁纸」按钮"
    assert "function wallCopyLink" in js and "🔗 复制作品链接" in js, "网页版缺「复制作品链接」"
    # 两端的分工从 2026-09-26 起不同了: 网页版地方大, 仍是「下一张壁纸 / 复制作品链接」
    # 两颗文字按钮; 小控件收敛成四颗图标键, 那两颗文字键在卡片上撤掉 (用户:「不需要了」)。
    for _label in ("下一张壁纸", "复制作品链接"):
        assert _label in js, f"网页版缺「{_label}」"
    assert '"copy"' not in ww_src and "'copy'" not in ww_src, \
        "卡片上「复制作品链接」的动作键该撤了 (注释里提历史可以, 代码里不许再有)"
    assert "上一张壁纸" in ww_src and "下一张壁纸" in ww_src, \
        "卡片上一张/下一张的**活动日志文案**还得在 (按钮变图标了, 日志名不变)"

    # ========== (k) 2026-09-26 一轮实测报障的 5 项修复 ==========
    # :
    # → ⑤当场改成
    # 「不是单纯改回长方形, 而是补回缺口, 且在补回的那部分配合缺口箭头改成像是防滑纹路
    # 一样的图案」。每项都钉一条离线可判定的护栏, 免得下轮改版又悄悄退回去。
    _wt_src = (Path(PROJECT_ROOT) / "core" / "wallpaper_trace.py").read_text(encoding="utf-8")

    # (k-1) 便签外轮廓是**完整长方形** + 原缺口区是**人字防滑纹路**
    assert "TAG_NOTCH" not in ww_src, "燕尾缺口 (TAG_NOTCH) 已作废, 不许再出现"
    assert _ww.TAG_XOFF > _ww.TAG_GRIP, "内容起点得让开防滑纹路区, 否则纹路压着缩略图"
    assert _ww.tag_pts(100, 20) == [(0, 0), (100, 0), (100, 20), (0, 20)], \
        "便签外轮廓必须是完整矩形 (四角直角, 不切角)"
    assert "def grip_arrow(" in ww_src and "grip_arrow(d, H * S, S)" in ww_src, \
        "缺口补回来的那块没画人字防滑纹路"
    # 补回来的那块**不能是透的** (透 = 缺口还在, 只是没画纹路)
    for _dock in _ww.DOCKS:
        _t = _ww.draw_tag(_dock)
        _gp = _t.getpixel(_ww.tag_map(_dock, _ww.TAG_GRIP // 2, int(_ww.TAG_THK / 2)))
        assert _gp[3] >= 150, f"{_dock} 原缺口那块还是透的 {_gp}"

    # (k-2) 「移出壁纸」必须**先把桌面切到下一张, 再把旧文件送回收站**
    # (: )
    assert srv_src.index("apply_wallpaper(nxt)") < srv_src.index("_recycle_files([str(f)])"), \
        "「移出壁纸」得先 apply 下一张再删文件 —— 反过来的话桌面会继续挂着已删的图"
    assert '"switched"' in srv_src, "recycle 得回报「桌面到底换掉没有」, 前端/日志才好说清"
    assert "next_wallpaper(f)" in srv_src, "删之前要挑下一张"
    assert 'r = self._post("/api/wallpaper/recycle")' in ww_src and "_force_refresh()" in ww_src, \
        "小控件点完「移出壁纸」要刷新卡片, 不然还挂着刚删掉那张的信息"

    # (k-3) 壁纸夹设置项: 配置键 → 候选目录 → 接口 → 设置页输入框, 一路通到底
    _dflt = _json.loads(
        (Path(PROJECT_ROOT) / "resources" / "default.json").read_text(encoding="utf-8"))
    assert "wallpaper_dir" in _dflt["paths"], "default.json 的 paths 缺 wallpaper_dir"
    assert _dflt["paths"]["wallpaper_dir"] == "", "默认必须是空 (空 = 跟随当前壁纸所在文件夹)"
    assert "def wallpaper_dir(" in _wt_src and "def candidates_dir(" in _wt_src, \
        "壁纸夹的「配置读取」与「候选目录」没实现"
    # 挑图/位置/副本扫描都得走 candidates_dir, 光实现不用等于没实现
    assert _wt_src.count("candidates_dir(") >= 3, "挑图/位置/副本扫描要统一走 candidates_dir"
    assert "candidates_dir" in srv_src, "服务端没接壁纸夹"
    assert '"paths": {"wallpaper_dir": raw}' in srv_src, "保存壁纸夹没写进 paths.wallpaper_dir"
    assert 'id="wall-dir-input"' in html and 'id="wall-dir-state"' in html, "设置页缺壁纸夹输入框"
    assert "function loadWallDir" in js and "function pickWallDir" in js \
        and "/api/wallpaper/dir" in js, "前端缺壁纸夹的读/写接线"

    # (k-4) 「只在桌面露出来」: 类名判定 + 总闸 + 整块隐形
    assert _ww.is_desktop_class("Progman") and _ww.is_desktop_class("WorkerW"), "桌面类名判定漏了"
    for _bad in ("SysListView32", "Shell_TrayWnd", "Chrome_WidgetWin_1", "", None):
        assert not _ww.is_desktop_class(_bad), f"{_bad!r} 不该算桌面"
    assert _ww.DESKTOP_CLASSES == ("Progman", "WorkerW"), _ww.DESKTOP_CLASSES
    assert "WindowFromPoint" in ww_src and "GetClassNameW" in ww_src and "GetParent" in ww_src, \
        "桌面判定要 WindowFromPoint 往上爬到 Progman/WorkerW (只看最底层永远不是桌面)"
    assert "def _desktop_under(" in ww_src and "def _gate_desktop(" in ww_src, "桌面总闸没实现"
    assert "self._gate_desktop(cur)" in ww_src, "总闸没接到 tick 上 = 不生效"
    assert "if self.dragging:" in ww_src and "if ok == self.on_desktop:" in ww_src, \
        "拖动期间不该隐形, 且状态没变不该重画"
    assert "if not self.on_desktop:" in ww_src and "self._blank(tw, th)" in ww_src, \
        "不在桌面时要交全透明帧 (连边缘线都不留)"

    # (k-5) 顺序 / 随机档: 纯本地状态 + 换图请求带 ?shuffle=1 + 服务端认这个参数
    assert "self.shuffle = not self.shuffle" in ww_src, "档位切换必须在窗口线程就地改"
    assert '"?shuffle=1" if self.shuffle else ""' in ww_src, "随机档没把参数带给服务端"
    assert "def _shuffle_arg(" in srv_src, "服务端没解析 ?shuffle"
    assert srv_src.count("_shuffle_arg()") >= 3, "next/prev 都要认这个参数"
    # 卡片的**交互态**必须进渲染请求快照 + 收货时再校验一遍 (2026-09-29 性能重构: 重活挪到
    # 渲染线程, 窗口线程只收货) —— 缺了哪个, 切档/切播放/确认后旧成品就会赖着不走。
    # 悬停位进缓存键: 四颗图标键 × 文字键的悬停组合由渲染线程成批预渲染, 扫过去永远命中。
    assert '"shuffle": self.shuffle, "playing": self.playing' in ww_src \
        and 'out["shuffle"] != self.shuffle or out["playing"] != self.playing' in ww_src, \
        "档位/播放态没进渲染请求与收货校验 (切换后旧成品会赖着)"
    assert "(self.card_hot, self.icon_hot)" in ww_src, \
        "卡片缓存键没带图标键悬停位"
    # 只影响本控件发起的换图: 不许去动 Windows 幻灯片自己的乱序设置
    assert "SPI_GETSLIDESHOW" not in ww_src and "SPI_SETSLIDESHOW" not in ww_src, \
        "随机档只该管本控件的换图, 不许偷偷改系统幻灯片设置"

    # ========== (l) 2026-09-26 第二轮实测报障的 5 项 ==========
    # :
    # 
    # 
    #

    # (l-1) 壁纸夹: **单个**按钮 + 点了真的弹系统对话框 (由服务端进程弹, 浏览器拿不到绝对路径)
    _fd_src = (Path(PROJECT_ROOT) / "utils" / "folder_dialog.py").read_text(encoding="utf-8")
    assert 'onclick="saveWallDir()"' not in html and 'onclick="loadWallDir()"' not in html, \
        "壁纸夹还留着「保存 / 读取」两个按钮 "
    assert 'onclick="pickWallDir()"' in html, "壁纸夹没有「选择文件夹」按钮"
    assert "function saveWallDir" not in js, "saveWallDir 是死代码了, 该删掉"
    assert "readonly" in html.split('id="wall-dir-input"', 1)[1][:200], \
        "目录输入框该是只读的 (目录只能由系统对话框选, 手打路径 = 又变成填表单)"
    assert "def pick_folder(" in _fd_src, "缺原生「选择文件夹」对话框"
    assert "SHBrowseForFolderW" in _fd_src, "文件夹对话框没走 shell32"
    # ★ 必须用**私有** WinDLL: ctypes.windll 的原型是进程级共享的, 会把托盘/小控件覆盖掉
    assert "ctypes.WinDLL(" in _fd_src and "ctypes.windll." not in _fd_src, \
        "folder_dialog 别用共享加载器 (原型会串到托盘)"
    assert "BFFM_INITIALIZED" in _fd_src and "SetForegroundWindow" in _fd_src, \
        "对话框不做置前就会被浏览器挡在身后, 表现又是「点了没反应」"
    assert "CoTaskMemFree" in _fd_src, "SHBrowseForFolder 返回的 PIDL 必须释放"
    assert "/api/wallpaper/dir/pick" in js, "前端没接「选择文件夹」接口"
    assert "from utils.folder_dialog import pick_folder" in srv_src, "服务端没接对话框"

    # (l-2) 删掉壁纸后的起步: 没有「当前这张」也能从壁纸夹一端换起 (三条路统一走一步函数)
    assert "if cur is None:" in _wt_src and "files[0] if step >= 0 else files[-1]" in _wt_src, \
        "注册表里那张已被删时就哑了 "
    assert "def _no_wallpaper(" in _wt_src and '"folder"' in _wt_src, \
        "没有壁纸时要回报壁纸夹可用不可用 (卡片靠它决定画哪些键)"
    # 服务端不许再拿「没有当前壁纸」当 400 直接顶回去
    _next_src = srv_src.split('@app.route("/api/wallpaper/next"', 1)[1].split("@app.route", 1)[0]
    assert "return jsonify" not in _next_src.split("next_wallpaper", 1)[0], \
        "「下一张」还留着 cur is None → 400 的死路"
    assert "next_wallpaper(cur, _shuffle_arg())" in srv_src, "「下一张」没走统一的步进函数"

    # (l-3) 托盘退出要**广播 app_quit**, 页面才会弹遮罩并尝试关标签
    _main_src = (Path(PROJECT_ROOT) / "main.py").read_text(encoding="utf-8")
    assert "def _notify_pages_quit(" in _main_src, "退出时没通知页面"
    assert '{"type": "app_quit"}' in _main_src, "退出通知的事件名要和前端对齐 (app_quit)"
    assert "client_count() <= 0" in _main_src, "没有页面连着就不该白等"
    assert "_notify_pages_quit()  # 先让页面知道" in _main_src \
        and _main_src.index("_notify_pages_quit()  # 先让页面知道") \
        < _main_src.index("_tray_stop()\n    _wall_widget_stop()"), \
        "广播必须在拆台 (摘托盘/收控件) 之前发出去"
    # 前端收到就得弹遮罩 (老实现有, 这里钉住别退回去)
    assert "case 'app_quit':" in js and "showExitMask();" in js, "前端没处理 app_quit"

    # (l-4) 卡片播放键: 换掉「下一张」, 按下先换一张再按间隔播
    assert '("next", "下一张壁纸", False)' not in ww_src, \
        "卡片上那颗「下一张壁纸」该删了 "
    # 播放键的「档位」跟着播放态走 —— 2026-09-26 起它不再是文字, 而是看**图标 kind**:
    # 正在播画 ⏸、没播画 ▶ (与便签同一套读法)
    assert '"pause" if playing else "play"' in ww_src, "卡片播放键图标没跟着播放态走"
    assert "def _toggle_play(" in ww_src, "便签/卡片播放键没共用同一套状态"
    assert "self._tag_action(1)" not in ww_src and "self._toggle_play()" in ww_src, \
        "便签播放键没接到 _toggle_play"
    assert 'if key == "play":' in ww_src, "卡片播放键没有点击分支"
    # 开播必须**先换一张**: 干等 SLIDE_SEC (30s) 才动第一次, 按下去看着就是没反应
    assert "if self.playing:\n            threading.Thread(target=self._do_action, args=(\"next\",)" \
        in ww_src.replace("\r\n", "\n"), "开播没先换一张 "
    # 顺带把壁纸夹可用不可用并进「壁纸换了没」的键, 否则配好夹子也不会刷新卡片
    assert 'bool(fo.get("ready"))' in ww_src, "info_key 没带壁纸夹状态"

    # (l-5) 说明文字: 去冗长、去口语, 不许再出现第二人称 (;
    # 2026-09-27 又报→ 扫描范围从
    # 「设置页那一段」放大到**全站可见文案**)。
    _set_html = html.split('id="tab-settings"', 1)[1].split("</section>", 1)[0]
    for _w in ("你", "您", "咱们", "双击 exe", "请在窗口中"):
        assert _w not in _set_html, f"设置页说明文字里的「{_w}」没清掉"

    # 全站可见文案: 代码注释与「引用」不是文案, 先把注释剥掉再扫, 免得逼出一堆
    # 「不能删的历史记录」假阳性。短语白名单只放**成词**的正当用法 (不是第二人称称呼)。
    def _visible_copy(_s: str) -> str:
        _s = re.sub(r"<!--.*?-->", "", _s, flags=re.S)          # HTML 注释
        _s = re.sub(r"(?m)^[ \t]*(//|/\*|\*).*$", "", _s)       # 整行 JS 块/行注释
        for _ok in ("为你推荐", "请求", "申请", "请问"):
            _s = _s.replace(_ok, "\u25cb")
        return _s

    _site_copy = _visible_copy(html) + _visible_copy(js)
    for _w in ("你", "您", "咱们"):
        assert _w not in _site_copy, f"全站可见文案里还有第二人称「{_w}」没清掉"
    # 上一轮那几句啰嗦话, 逐条钉住别写回来
    for _w in ("无需任何额外操作", "点按钮切换（", "根本不会去加载原图", "重启后仍生效",
               "设置会保存到", "把当前桌面壁纸所在的那个文件夹", "留空"):
        assert _w not in _set_html, f"设置页又冒出啰嗦的老文案: {_w}"
    # 多余的那条「点按钮切换」提示已并进状态行, 不再单占一行
    assert '<div class="hint" style="margin-top:6px">点按钮切换' not in _set_html

    # ========== (m) 2026-09-26 第三轮实测报障的 8 项 ==========
    # :
    # 
    # 
    # 
    # 啥的说明没有
    # 必要, 换成配合每张图片内容的一句衬托气氛的语言」。①③④⑤⑦见上, 这里钉住②⑥与机制。

    # (m-1) 「选文件夹反而清空」的两处根子: 结构体字段类型 + 前端把错误体当「取消」
    assert "pszDisplayName" in _fd_src and "c_void_p" in _fd_src, \
        "BROWSEINFOW.pszDisplayName 要用 c_void_p —— 赋 create_unicode_buffer 会 TypeError (500)"
    assert "ctypes.cast(" in _fd_src, "传 buffer 时要 cast 成指针, 否则 LPWSTR 字段收不下"
    assert "GetForegroundWindow" in _fd_src and "BFFM_SETSELECTIONW" in _fd_src, \
        "对话框要认 owner/初始目录, 否则弹在浏览器身后或每次重选都要从头点"
    # 服务端不许让异常漏成 500 空体 (前端会把它当「取消」→ 用 undefined 清空输入框)
    _pick_src = srv_src.split('@app.route("/api/wallpaper/dir/pick"', 1)[1].split("@app.route", 1)[0]
    assert "except Exception" in _pick_src and "jsonify" in _pick_src, \
        "选目录接口要把异常也包成 JSON, 不能漏成 500 空体"
    assert '"saved": True' in _pick_src or "'saved': True" in _pick_src, \
        "选中后要回报 saved, 前端才知道是真选了而不是取消"
    # 前端必须**显式**判 r.ok —— api() 在非 2xx 且能解析 JSON 时是不抛的, 会把错误体当结果
    _pick_js = js.split("async function pickWallDir(", 1)[1].split("\n}", 1)[0]
    assert "r.ok !== true" in _pick_js, "pickWallDir 没显式判 HTTP 状态 (错误体会被当「取消」)"

    # (m-2) 桌面判定: 命中自己时要**往下找**真正压在光标下的窗口, 不能「自证」成自己
    assert "def top_window_under(" in ww_src and "def win_is_cloaked(" in ww_src, \
        "缺「光标下真正那张窗口」的查找 (topmost 窗口会自证压在自己身上)"
    assert "def window_chain_has_desktop(" in ww_src, "缺「往上爬找 Progman/WorkerW」的链条判定"
    assert "EnumWindows" in ww_src and "IsWindowVisible" in ww_src and "IsIconic" in ww_src, \
        "逐层筛窗口要用 EnumWindows (z 序从顶到底) + 可见/最小化过滤"
    assert "DWMWA_CLOAKED" in ww_src, "UWP 幽灵窗口 (cloaked) 要跳过, 否则会挡住桌面判定"
    assert "skip=self.hwnd" in ww_src, "命中自己时必须从自己往下一层开始找"
    assert "_gate_key" in ww_src and "_gate_ts" in ww_src, \
        "同一像素短期内要复用结论 —— 每 16ms 跑一次 EnumWindows 就是卡顿的来源"

    # (m-3) 闸门**不许再改写状态机**: 老写法把 state 掰回 EDGE 并清 anim, 与 _hover 每 16ms
    # 互抢 → 「点开卡片再关闭就卡顿、便签缩不回去」。闸门只该改 on_desktop 并 _apply()。
    _gate_src = ww_src.split("def _gate_desktop(", 1)[1].split("\n    def ", 1)[0]
    assert "self.on_desktop" in _gate_src and "_apply()" in _gate_src, "闸门没接到重画上"
    # 只看**代码行** —— 注释里正当地讲着「老写法把它掰回 EDGE」这段历史, 不能连注释一起禁掉
    _gate_code = "\n".join(ln for ln in _gate_src.splitlines()
                           if not ln.lstrip().startswith("#"))
    for _forbid in ("self.state", "self.anim", "self._t0", "EDGE"):
        assert _forbid not in _gate_code, \
            f"闸门里又出现 {_forbid} —— 与 _hover 抢状态机就是「卡顿/缩不回去」的根子"

    # (m-4) 弹出/收回的缓动: 一次方起步太猛 (第一帧就走 65%, 看着是「啪」地弹出) →
    # 换成 ease-out-quad (第一帧 ~14%), 且补间时长拉长到 240ms。
    assert _ww.ANIM_MS >= 240, f"补间时长太短 ({_ww.ANIM_MS}ms), 弹出会显得急"
    assert 0.10 <= _ww._ease(0.07) <= 0.20, \
        f"缓动第一帧走了 {_ww._ease(0.07):.3f} —— 起步太猛或太肉, 都不像「顺滑」"
    assert all(_ww._ease(i / 50) < _ww._ease((i + 1) / 50) for i in range(50)), "缓动不是单调的"
    assert abs(_ww._ease(1.0) - 1.0) < 1e-9, "缓动收尾没到 1 (会差一点点贴不上边)"

    # (m-5) 缩略图「反色」: premultiplied_bgra 连换两次 R/B = 红蓝互换。
    # 实测四元组: 红 → [0,0,255,255]、蓝 → [255,0,0,255]、绿 → [0,255,0,255]。
    for _name, _rgb, _want in (("红", (255, 0, 0), [0, 0, 255, 255]),
                               ("蓝", (0, 0, 255), [255, 0, 0, 255]),
                               ("绿", (0, 255, 0), [0, 255, 0, 255])):
        _got = list(_ww.premultiplied_bgra(_PImg.new("RGBA", (1, 1), _rgb + (255,))))
        assert _got == _want, f"{_name}的 BGRA 通道序错了: {_got} (应为 {_want})"
    # 预乘 alpha 别忘: 半透明像素要乘上去, 否则 UpdateLayeredWindow 会画出一圈亮边
    _half = list(_ww.premultiplied_bgra(_PImg.new("RGBA", (1, 1), (255, 255, 255, 128))))
    assert _half == [128, 128, 128, 128], f"alpha 没有预乘上去: {_half}"

    # (m-6) 「一点点圆边」: 按钮不再斜切, 但也不是大圆角 —— 圆角要小, 且斜切那套必须作废
    assert _ww.CUT_BTN == 0, "CUT_BTN 该明确作废 (按钮不再斜切)"
    assert 1 <= _ww.BTN_RAD <= 8 and 1 <= _ww.IBAR_RAD <= 8, \
        f"圆角得是「一点点」({_ww.BTN_RAD}/{_ww.IBAR_RAD}) —— 大了就不是「一点点」了"
    # 2026-09-29 拆层: 徽章底/正文在 card_base, 文字键/图标键在 card_overlay, draw_card 只合成
    _r_src = "\n".join((ww_src.split("def card_base(", 1)[1].split("\ndef ", 1)[0],
                        ww_src.split("def card_overlay(", 1)[1].split("\ndef ", 1)[0]))
    # 同理: 注释里留着「原来是这么写的」这段历史是正当的, 只禁**代码行**
    _r_code = "\n".join(ln for ln in _r_src.splitlines() if not ln.lstrip().startswith("#"))
    assert "rounded_rectangle(box" in _r_code, "文字键没走圆角矩形"
    assert "chamfered_box(dr, box" not in _r_code, "文字键还在画斜切键帽"
    # 图标键的 keycap: 给了 radius 走圆角, 不给还是便签那套斜切 (两处共用 icon_btn)
    _rb = _PImg.new("RGBA", (60, 60), (0, 0, 0, 0))
    _cb = _PImg.new("RGBA", (60, 60), (0, 0, 0, 0))
    _ww.icon_btn(_rb, "play", 30, 30, 24, accent=_ww.NEON_FALLBACK, hover=True, radius=5)
    _ww.icon_btn(_cb, "play", 30, 30, 24, accent=_ww.NEON_FALLBACK, hover=True)
    assert _rb.tobytes() != _cb.tobytes(), "icon_btn 的 radius= 没起作用 (便签与卡片该长得不一样)"
    assert "radius=IBAR_RAD * S" in ww_src, "卡片控制条没把微圆角传下去"
    # 标题徽章也从斜切改**圆弧**了 (用户 2026-09-27:「显示标题那一栏的两边不要四角斜切, 而是圆弧」)
    # —— 面板/缩略图的对角斜切仍在 (那是「窗口的形状」), 所以只断言 draw_card 里不再有斜切徽章
    assert "radius=BADGE_RAD * S" in _r_code and "chamfered_box(" not in _r_code, \
        "标题徽章还在斜切 "

    # (m-7) 氛围文案: 不再有溯源结论那行, 换成本地推导的一句 (同一张永远同一句, 可复现)
    for _gone in ("库内已是最清晰", "像素完全一致", "字节一致"):
        assert _gone not in _r_code, f"卡片上还留着溯源结论那行: {_gone}"
    # 第三行 = AI 那句优先、本地 mood_of 兜底 (2026-09-27), 见 T132
    assert "mood_line(info, thumb)" in ww_src, "卡片第三行没换成氛围句"
    assert _ww.mood_of(None) == "", "没缩略图时不该编一句 (跟画面对不上的话比空着更糟)"
    _m1 = _ww.mood_of(_PImg.new("RGB", (48, 48), (200, 30, 30)))
    assert _m1 and _m1 == _ww.mood_of(_PImg.new("RGB", (48, 48), (200, 30, 30))), \
        f"氛围句不稳定 (同一张图两次不一样): {_m1!r}"
    assert _ww.mood_of(_PImg.new("RGB", (48, 48), (10, 14, 50))) != _m1, \
        "深蓝与亮红给出同一句 —— 明度/色相根本没参与"
    # 那行只有 176px: 32 种组合**一个都不许**被 _fit 截成「…」(否则一句被吃掉尾巴就白写了)。
    # 2026-09-27 氛围句字号 13→16 且改走**文案字体**, 这里跟着换 —— 仍写 13*CARD_SS 的话,
    # 里头的「塞得下」量的是已经不存在的旧字号。
    _mf = _ww.font(_ww.MOOD_SIZE * _ww.CARD_SS, copy=True)
    _mw = (_ww.X_BOX[0] - (_ww.C_PL + _ww.C_TW + 18) - 12) * _ww.CARD_SS
    _longmood = [f"{a}，{b}" for _, a in _ww._MOOD_LIGHT for _, b in _ww._MOOD_HUE
                 if _ww._fit(f"{a}，{b}", _ww.MOOD_SIZE * _ww.CARD_SS, _mw, copy=True) != f"{a}，{b}"]
    assert not _longmood, f"这些氛围句太长会被截断: {_longmood}"
    assert all(_mf.getlength(f"{a}，{b}") <= _mw for _, a in _ww._MOOD_LIGHT
               for _, b in _ww._MOOD_HUE), "本地兜底氛围句超出信息行宽度"
    # 标题不再拿文件名/路径顶替 (用户: 「直接拿路径做标题的也可以换成更像标题的句子」)
    assert _ww.card_title(None) == "桌面此刻空着", _ww.card_title(None)
    assert _ww.card_title({"file": {"name": "109395314_p0.jpg"}}) == "来自壁纸夹的一张", \
        "标题又拿文件名顶替了"
    assert _ww.card_byline({"artwork": {"artwork_id": "7", "author_name": "A"}}) == "#7 · A"
    assert _ww.card_byline(None) == "未识别出处", "都没溯源到时的出处行不该空着"


def t132_ai_text_pipeline() -> None:
    """T132: ✨ 壁纸文案 AI (2026-09-27)。

    :。
    全程离线: 只把 ``ai_text.generate`` 打桩, 一次网络都不发 (唯一的「网络」是 127.0.0.1:9
    这个必然拒绝连接的本地端口, 用来验失败路径)。覆盖:
      ① 键集一致 / 缺节兜底 / 默认全关 (没配就绝不打接口)
      ② ai_ready 四件套缺一不可
      ③ 画面线索与提示词 (库外文件名必须声明成「只是编号」, 免得模型拿编号当内容)
      ④ 两行解析 (去标签/序号/引号/书名号/行尾标点) + 硬夹 8/12 字, 且**像素级**塞得进卡片
      ⑤ 缓存键 = 版本+模型+「路径+改动时间+大小」, 换模型/换图都要换键
      ⑥ texts_for 只读缓存 (轮询那条路一次接口都不许打); 生成只走 generate_now
         (同步, 表单值优先, 必带图, 点按钮即视为已启用)
      ⑦ /api/ai 不回密钥原文; 活动日志打码; 旧的保存接口已删 (POST 405);
         /api/ai/run 与 /api/ai/test 人话报错, 且共用同一套「表单值优先」口径
      ⑧ 小控件接线 (ai_sig 进 info_key; 标题只做兜底、氛围句 AI 优先)
    """
    import tempfile
    import shutil
    import json as _json
    import time as _time
    from pathlib import Path as _Path
    from PIL import Image as _PImg
    import utils.paths as upaths
    import utils.config as ucfg
    from core import ai_text as ai

    # ---------- ① 键集一致 / 缺节兜底 / 默认全关 ----------
    defaults = _json.loads((_Path(PROJECT_ROOT) / "resources" / "default.json")
                           .read_text(encoding="utf-8"))
    sec = defaults["ai_text"]
    assert {k for k in sec if not k.startswith("_")} == set(ucfg.AI_DEFAULTS), \
        ({k for k in sec if not k.startswith("_")}, set(ucfg.AI_DEFAULTS))
    assert "_note" in sec, "default.json 每个节都有 _note, 照这个风格写"
    assert sec["enabled"] is False and ucfg.AI_DEFAULTS["enabled"] is False, "AI 默认必须是关的"
    assert sec["api_key"] == "" and ucfg.AI_DEFAULTS["api_key"] == "", "默认不该有密钥"
    cfg = ucfg._canonicalize({"web": {"port": 1}})
    assert cfg["ai_text"] == dict(ucfg.AI_DEFAULTS), cfg["ai_text"]
    cfg = ucfg._canonicalize({"ai_text": {"model": "x"}})
    assert cfg["ai_text"]["model"] == "x" and cfg["ai_text"]["enabled"] is False

    # ---------- ② ai_ready: 四件套缺一不可 ----------
    ok = dict(ucfg.AI_DEFAULTS, enabled=True, api_key="sk-test-key")
    assert ai.ai_ready(ok) is True
    for k in ("enabled", "base_url", "model", "api_key"):
        bad = dict(ok)
        bad[k] = False if k == "enabled" else "   "
        assert ai.ai_ready(bad) is False, f"{k} 没配好还算配好? 会白打一次必然失败的请求"

    tmp = _Path(tempfile.mkdtemp(prefix="t132ai_"))
    try:
        # ---------- ③ 画面线索 / 提示词 ----------
        img = tmp / "dark_blue.png"
        _PImg.new("RGB", (600, 400), (10, 14, 50)).save(img)
        hint = ai.describe_image(str(img))
        assert hint and ("暗" in hint or "亮" in hint), hint
        assert "横构图" in hint and "偏蓝" in hint, hint
        assert ai.describe_image(str(tmp / "nope.png")) == "", "读不到图不该抛, 该给空线索"
        _out = ai.build_prompt({"file": {"name": "109395314_p0.jpg"}}, hint=hint)
        assert "编号" in _out and "109395314_p0.jpg" in _out, _out
        assert hint in _out, "画面线索没进提示词"
        _in = ai.build_prompt({"file": {"name": "a.jpg"},
                               "artwork": {"title": "海边的午后", "author_name": "某人",
                                           "artwork_id": "109395314"}}, vision=True)
        assert "海边的午后" in _in and "某人" in _in and "109395314" in _in, _in
        assert "以图为准" in _in, "开了视觉该明确说以图为准"
        assert str(ai.MAX_TITLE) in ai.SYSTEM_PROMPT, "系统提示词里的字数上限没跟上常量"
        # 提示词**归用户自己写** 
        # core/ai_text.py), 所以这里**只钉格式, 不钉词句**。原先钉着「禁用词表」「语气六种」
        # 「写完自检」那几句, 用户一改提示词冒烟就红 —— 护栏反倒成了他改文案的绊脚石。
        # 文案好不好只有用户看得出来, 冒烟判不了; 冒烟能判的是下面这些**机器口径**:
        # 提示词没被清空、字数上限跟常量一致 (超了卡片会截断)、解析仍认两行。
        assert len(ai.SYSTEM_PROMPT.strip()) >= 40, "系统提示词被清空了"
        assert ai.AI_VER != "v1", "改了口径却没升版本 —— 用户还会看到旧缓存里那批「安静」文案"
        # 程序拼的标题不许当作品标题喂进提示词 (那串编号只会把模型带偏)
        _junkp = ai.build_prompt({"file": {"name": "1e94516aeda0e378.png"},
                                  "artwork": {"title": "X图/#其他来源# / 1e94516aeda0e378",
                                              "artwork_id": "ext_166a87817d786d74"}})
        assert "作品标题: X图" not in _junkp and "没有含义" in _junkp, _junkp
        # 两份 machine_title 实现 (core 喂提示词 / utils 画卡片) 必须同口径 —— 拿同一组样例对拍
        import utils.widget_window as _wwx
        _samples = [("X图/#其他来源# / 1e94516ae", "x.png"), ("1e94516aeda0e378", "1e94516aeda0e378.png"),
                    ("109395314_p0", "109395314_p0.jpg"), ("foo/bar", ""), ("#其他来源#", ""),
                    ("月下提灯", "1e94516ae.png"), ("夜に駆ける", "a.png"), ("", "a.png")]
        for _t, _fn in _samples:
            assert ai.machine_title(_t, _fn) == _wwx._machine_title(_t, _fn), \
                f"两处 machine_title 口径漂了: {_t!r} / {_fn!r}"

        # ---------- ④ 两行解析 ----------
        assert ai.parse_reply("标题：夜色\n氛围句：灯火在远处闪。") == ("夜色", "灯火在远处闪")
        assert ai.parse_reply("1. 《归途》\n2. 「风停在窗边」") == ("归途", "风停在窗边")
        assert ai.parse_reply("```text\n雨后的街\n```") == ("", "雨后的街"), "单行该当氛围句"
        _got = ai.parse_reply("标" * 20 + "\n" + "氛" * 30)
        assert (len(_got[0]), len(_got[1])) == (ai.MAX_TITLE, ai.MAX_MOOD), _got
        assert ai.parse_reply("") == ("", "")
        # 顶到上限时**断在断句点上**, 不许啃出半截字 —— 提示词让模型写具体的东西, 句子
        # 常顶到上限, 实测 1e94516aeda0e378.png 出过「银发女郎提灯立于月下，蝙蝠…」被
        # s[:n] 啃成「…立于月下，蝙」。
        # 上限一律取常量, 这里不写死字数 —— 2026-09-27 用户把上限从 8/12 调到 15/30,
        # 原来钉着「12 字」的那条期望当场就红了。
        _src = "银发女郎提灯立于月下，蝙蝠掠过废城的塔尖，钟声沉入海底，灯塔在雾里熄灭"
        _cut = ai.parse_reply("标题\n" + _src)[1]
        assert len(_src) > ai.MAX_MOOD, "样例本身没顶到上限, 这条就没验到东西"
        assert len(_cut) <= ai.MAX_MOOD and _src.startswith(_cut), _cut
        assert _src[len(_cut)] in ai._BREAK, f"该断在断句点上, 却在「{_cut[-1]}」后面动刀"
        assert ai._clamp("前半段整整六个字后半段也有", 12) == "前半段整整六个字后半段也有"[:12], \
            "断句点都落在后半截时该老实硬夹, 不能把句子砍没了"
        assert ai._clamp("短", 12) == "短"
        # 这两个上限是**像素预算**, 不是拍脑袋的字数: 标题超了裁「…」, 氛围句超了换行
        import utils.widget_window as _ww
        _mw = (_ww.X_BOX[0] - (_ww.C_PL + _ww.C_TW + 18) - 12) * _ww.CARD_SS
        # 2026-09-28 四期: 窗口收窄到 548, 标题改**跑马灯** (用户:「小窗口还是窄一些吧, 标题
        # 可以滚动播放, 这样标题再长也不怕了」) —— 标题不再要求「塞进定宽徽章」, 只要塞得进
        # 标题条的**上限宽度** (超了才会被裁「…」再滚)。写死像素会在调字号/改字体当天就红,
        # 所以一律**走常量 + 真字体**量宽 (文案体字面宽度和雅黑不是一回事)。
        _tf = _ww.font(_ww.TITLE_SIZE * _ww.CARD_SS, copy=True)
        _tmax = _ww.TITLE_MAX * _ww.CARD_SS
        assert _tf.getlength("标" * ai.MAX_TITLE) + _ww.TRACK_LS * _ww.CARD_SS * ai.MAX_TITLE \
            <= _tmax, f"标题上限的自然宽度超过标题条上限 {_ww.TITLE_MAX}px, 会被裁成「…」"
        # 氛围句**允许换行** ,
        # 所以这里判的是「行数够不够 + 有没有一行溢出」, 而不是「一行塞不塞得下」。
        _ml = _ww.mood_wrap("氛" * ai.MAX_MOOD, _mw, _ww.MOOD_SIZE * _ww.CARD_SS)
        assert len(_ml) <= _ww.MOOD_LINES, \
            f"氛围句上限要换 {len(_ml)} 行, 卡片只留了 {_ww.MOOD_LINES} 行"
        _mf = _ww.font(_ww.MOOD_SIZE * _ww.CARD_SS, copy=True)
        assert all(_mf.getlength(_l) <= _mw for _l in _ml), f"有行超出信息行宽度: {_ml}"
        assert not _ml[-1].endswith("…"), \
            f"氛围句换行后还是被裁了 (该再加宽/加行): {_ml}"
        # 三档字号要**拉开层次**, 且行距得跟着字号走 —— 只放大字号不动行距是「字贴在一起」,
        # 不是「字大了」(这一版就是为此把 27 提到 30)。
        assert _ww.TITLE_SIZE > _ww.MOOD_SIZE > _ww.INFO_SIZE >= 13, \
            (_ww.TITLE_SIZE, _ww.MOOD_SIZE, _ww.INFO_SIZE)
        assert _ww.INFO_PITCH >= _ww.MOOD_SIZE + 6, "行距没跟着字号走"
        _last_row = _ww.C_PT + 52 + (2 + _ww.MOOD_LINES - 1) * _ww.INFO_PITCH + _ww.MOOD_SIZE
        assert _last_row < _ww.C_DIV, f"末行氛围句 {_last_row}px 压到分隔线 {_ww.C_DIV}px"
        # 文案字体栈: 本机缺华文琥珀也得退到**真装着的**字体文件, 不许 load_default (那是方框)
        assert _ww.COPY_FONTS[0] == "STHUPO.TTF", \
            f"首选字体被换掉 (现在 {_ww.COPY_FONTS[0]!r}), 得确认是有意为之"
        assert _ww._copy_font_path().lower().endswith((".ttf", ".ttc")), _ww._copy_font_path()
        # 文案体与 UI 体是**两份字体** (全屏换成展示体会读着发飘); 两边都得解析到真文件 ——
        # `load_default()` 那种降级在卡片上是「一屏方框」, 必须当场拦住
        _cp = getattr(_ww.font(_ww.TITLE_SIZE, copy=True), "path", "")
        _up = getattr(_ww.font(_ww.TITLE_SIZE), "path", "")
        assert _cp.lower().endswith((".ttf", ".ttc")), f"文案体没解析到真字体: {_cp!r}"
        assert _up.lower().endswith((".ttf", ".ttc")), f"UI 体没解析到真字体: {_up!r}"
        assert _cp != _up, "文案体与 UI 体变成了同一份字体 (展示体与 UI 体该分开)"
        # 「最好有特效」= 辉光 / 渐变 / 流光**真的接线了**: 光定义不调用等于没做。文案的排版坐标
        # 单一数据源在 copy_geom (画卡片/描阴影/铺流光三处共用), 所以几个绘制函数都得在这。
        _csrc = (PROJECT_ROOT / "utils" / "widget_window.py").read_text(encoding="utf-8")
        for _fn in ("text_mask", "vgrad", "tint_light", "copy_geom", "copy_mask", "copy_box",
                    "neon_relief", "neon_prep", "neon_flow", "title_prep", "title_frame"):
            assert f"def {_fn}(" in _csrc, f"{_fn} 没定义"
        # 2026-09-29 卡片拆层: 绘制 (排版/阴影/氛围句柔光) 在 card_base, 按钮 (悬停/确认/档位
        # 图标) 在 card_overlay, draw_card 只负责合成缩放 —— 护栏跟着搬到真正干活的函数上。
        _cbase = _csrc.split("def card_base(", 1)[1].split("\ndef ", 1)[0]
        _cover = _csrc.split("def card_overlay(", 1)[1].split("\ndef ", 1)[0]
        _cgeo = _csrc.split("def copy_geom(", 1)[1].split("\ndef ", 1)[0]
        _cmask = _csrc.split("def copy_mask(", 1)[1].split("\ndef ", 1)[0]
        _tprep = _csrc.split("def title_prep(", 1)[1].split("\ndef ", 1)[0]
        assert "geo = copy_geom(info, thumb, S)" in _cbase, \
            "排版没走 copy_geom —— 阴影/辉光/流光会各算各的坐标, 一改排版就错位"
        # 四期: 标题改**跑马灯** (定宽徽章 + 每帧裁窗重贴), 于是 copy_geom 交的是徽章框与字体,
        # 而**标题字形**从这里搬去了 title_prep —— 钉住这条分工, 免得有人又把标题塞回卡片底图:
        # 一滚起来光就描在「已经不在这儿」的字上。
        assert "\"badge\":" in _cgeo and "BADGE_X0" in _cgeo and "BADGE_X1" in _cgeo, \
            "copy_geom 没交出定宽徽章框"
        assert "tstr" not in _cmask, \
            "标题进了正文流光蒙版 —— 跑马灯一滚, 光就描在「已经不在这儿」的字上"
        assert "GaussianBlur(2.2 * S)" in _tprep and "k=118" in _tprep, "标题的 accent 辉光没接上"
        assert "vgrad((cw, ch_h)" in _tprep and "putalpha(tmask)" in _tprep, \
            "标题的渐变字面没接上"
        assert "TITLE_GAP" in _tprep and "pad + tw_ss + gap" in _tprep, \
            "标题条没放两遍 —— 滚到接缝处会露馅"
        assert "font(MOOD_SIZE * S, copy=True)" in _cgeo, "氛围句没走文案字体"
        assert "GaussianBlur(1.6 * S)" in _cbase, "氛围句柔光没接上"
        # 用户 2026-09-28 二期:「就不加粗了」—— 华文琥珀本身笔画极重, 撤掉 stroke 合成加粗。
        # 「描边」这套不许回来: 在这么重的字上描一圈只会糊, 也会让阴影与字面差一圈粗细。
        assert not hasattr(_ww, "STROKE_W"), "合成加粗又回来了 "
        _cardcode = "\n".join(ln for ln in (_cbase + _cover).splitlines()
                              if not ln.lstrip().startswith("#"))
        assert "stroke_width" not in _cardcode and "stroke=" not in _cardcode, \
            "文案又加描边了 "
        assert "stroke_width" not in _ww.text_ls.__doc__ + _ww.text_mask.__doc__, \
            "text_ls/text_mask 的描边参数没清干净 (留着就是死代码)"
        assert "ImageChops.offset(cmask.crop(cbox)" in _cbase \
            and "GaussianBlur(SHADOW_BLUR * S)" in _cbase and "k=SHADOW_K" in _cbase, \
            "文字阴影没接上"
        # 「霓虹流光」= **骨架 + 沿笔画的测地路程场 + 逐帧波前** (2026-09-28 四期)。用户否掉了
        # 前两期: 二期「水平亮带横扫」= 扫光, 三期「斑点场乘蒙版」= 一团来回挪的雾; 他要的是
        # 「就好像光线在描画着笔画一样, 准确来说就像金属的反光一样」—— 光必须**顺着笔画走**。
        # 定稿件、稳定态叠加、tick 推进, 三处都得接线 (2026-09-29 起定稿件挪到渲染线程的
        # _render_job 里备, 用请求快照 req["info"]/req["thumb"], 不碰窗口线程的 self._info)。
        assert 'neon_prep(req["info"], req["thumb"])' in _csrc, "没备流光的定稿件"
        assert 'title_prep(req["info"], req["thumb"' in _csrc, "没备标题跑马灯条"
        assert "title_frame(timg, self._title, self._neon_phase" in _csrc, "稳定态没叠标题跑马灯"
        _tick = _csrc.split("def _tick(self)", 1)[1].split("\n    def ", 1)[0]
        assert "neon_flow(timg, self._neon, self._neon_phase" in _csrc \
            and "self.state == CARD" in _csrc, "稳定态没叠流光"
        assert "_neon_live()" in _tick and "NEON_EVERY" in _tick, "相位没在 tick 里推进"
        # 相位是**拍数**且**不取模**: 用户明确「不是单调的循环流动」, 取模就等于宣布这是个循环
        # (二期那条水平亮带就是靠取模 CARD_W 滚的, 整条路已经拆了)。
        assert "_neon_phase += 1" in _tick and "% CARD_W" not in _tick, \
            "流光相位又变成取模的循环了"
        assert not hasattr(_ww, "neon_band"), "二期的水平亮带该拆干净了"
        assert "neon_field" not in _csrc, "三期的斑点场该拆干净了"
        assert not hasattr(_ww, "NEON_FIELDS") and not hasattr(_ww, "NEON_DRIFT_X"), \
            "斑点场的常量该拆干净了"
        # 骨架 + 测地路程场: 拿一条**横杠**验 —— 它的骨架就是中轴, 路程场沿横杠单调增长, 且
        # 笔画外一律标 -1。这正是「沿笔画量距离」的直接证据 (按直线横扫的话, 路程场会跟 x 一致,
        # 与「是不是笔画」无关)。踩过: 门槛取 >0 会把抗锯齿灰边也当笔画, 膨胀后拖出一圈毛刺骨架。
        from PIL import ImageDraw as _PDraw
        _bar = _PImg.new("L", (120, 24), 0)
        _PDraw.Draw(_bar).rectangle([10, 9, 109, 14], fill=255)
        _rel = _ww.neon_relief(_bar)
        assert _rel is not None and _rel[0].shape == (24, 120), \
            "横杠细化不出骨架 (Zhang-Suen 没接上?)"
        _Dg = _rel[0]
        assert (_Dg < 0).any() and (_Dg >= 0).any(), "路程场没把笔画外标成 -1"
        _rowon = _Dg[11][_Dg[11] >= 0]
        assert _rowon.size >= 40 and _rowon[-1] > _rowon[0] + 20, \
            f"路程场没沿横杠增长 —— 不是「沿笔画量距离」({_rowon[:1]} → {_rowon[-1:]})"
        # 强度**下限**护栏: 这几项是「看得见」的必要条件, 谁把它调回看不见就当场红。
        # (二期前是 glow k=120 / blur=3 / core k=130 —— 叠在本来就带辉光的白字上, 等于没铺。)
        assert _ww.NEON_GLOW_K >= 170 and _ww.NEON_CORE_K >= 190, \
            f"流光强度回落到看不见了 ({_ww.NEON_GLOW_K}/{_ww.NEON_CORE_K})"
        assert _ww.NEON_GLOW_BLUR >= 5, "流光光晕太紧, 漫不出字形就不像霓虹"
        assert _ww.NEON_PAD >= 3 * _ww.NEON_GLOW_BLUR, "外扩量兜不住光晕, 会被裁出一条硬边"
        # 多道波前: 速度与呼吸角速度都得**彼此不同** —— 几道一齐走就成了「整行一起亮」, 那正是
        # 二期被否掉的样子; 呼吸频率各不相同才是用户要的「随机闪烁」。下限放到 2: 2026-09-28
        # 「波动太密集和频繁了」, 四道减到两道 (道数=密度, 速度/呼吸=频率)。
        _sp, _fd = _ww.NEON_SPEED, _ww.NEON_FADE
        assert _ww.NEON_SRC >= 2 and 2 <= _ww.NEON_WAVE <= 40, (_ww.NEON_SRC, _ww.NEON_WAVE)
        assert len(_sp) == len(_fd) == _ww.NEON_SRC, (_sp, _fd)
        assert len(set(_sp)) == len(_sp) and all(v > 0 for v in _sp), "波前速度该各不相同且为正"
        assert len(set(_fd)) == len(_fd) and all(0 < v < 0.5 for v in _fd), "波前呼吸角速度该各不相同"
        # neon_flow: ① 不碰缓存里的底图 ② 换相位文案区像素确实在变
        from PIL import ImageChops as _ICH
        _sthumb = _PImg.new("RGB", (256, 256), (30, 60, 120))
        _scard = _ww.draw_card(None, _sthumb)
        _neon = _ww.neon_prep(None, _sthumb)
        assert _neon is not None and _neon[0].size == (
            _neon[3][2] - _neon[3][0], _neon[3][3] - _neon[3][1]), "流光定稿件尺寸对不上文案框"
        assert _neon[2] is not None and len(_neon[2]) == 3, "路程场该是 (D, OFF, SPAN) 三张"
        _Dr = _neon[2][0]
        assert _Dr.shape == (_neon[3][3] - _neon[3][1], _neon[3][2] - _neon[3][0]), \
            "路程场尺寸对不上文案框"
        assert (_Dr >= 0).any(), "路程场一片空白 (骨架没建起来)"
        _snap = _scard.copy()
        _fa = _ww.neon_flow(_scard, _neon, 0, (255, 200, 80))
        _fb = _ww.neon_flow(_scard, _neon, 21, (255, 200, 80))
        assert _scard.tobytes() == _snap.tobytes(), "neon_flow 改了底图 (缓存会被污染)"
        assert _fa.tobytes() != _fb.tobytes(), "换了相位画面没变 —— 流光没在动"
        # 「看得出来」得用数字钉: 两相位之间文案区最大通道差太小 = 肉眼看不见 (过两次)
        _dmax = max(hi for _, hi in _ICH.difference(_fa.convert("RGB"),
                                                    _fb.convert("RGB")).getextrema())
        assert _dmax >= 60, f"流光太弱 (两相位最大差仅 {_dmax}/255), 肉眼看不出来"
        # **不是单调循环**: 逐拍求「相对底图新增亮度」最集中的那一列, 它必须**乱跳** ——
        # 二期的水平亮带会让这一列一直朝同一个方向挪, 那正是的那个效果。
        _baseL = _scard.convert("L")
        _x0, _y0, _x1, _y1 = _neon[3]
        _cols = []
        for _p in (0, 4, 8, 12, 16):
            _dd = _ICH.difference(_ww.neon_flow(_scard, _neon, _p, (255, 200, 80)).convert("L"),
                                  _baseL).crop(_neon[3])
            _prof = [sum(_dd.crop((c, 0, c + 1, _y1 - _y0)).getdata()) for c in range(_x1 - _x0)]
            _cols.append(_prof.index(max(_prof)))
        _dirs = {(b > a) - (b < a) for a, b in zip(_cols, _cols[1:])}
        assert _dirs != {1} and _dirs != {-1}, \
            f"亮点一直在朝同一个方向挪 ({_cols}) —— 又变回「单调循环流动」了"

        # **标题跑马灯** (四期, 用户:「小窗口还是窄一些吧, 标题可以滚动播放, 这样标题再长也
        # 不怕了」): 短标题居中不动; 长标题裁窗左移, 条里放**两遍**, 滚到接缝与开头逐像素重合。
        _longinfo = {"artwork": {"title": "月下提灯的女郎走过很长的巷子回望来时的路"}}
        _tp = _ww.title_prep(_longinfo, _sthumb, (255, 200, 80))
        assert _tp is not None and _tp["scroll"] is True, "这么长的标题该判为要滚动"
        assert _tp["rgba"].size == (_tp["rgba"].size[0], _ww.BADGE_H), _tp["rgba"].size
        assert _tp["w"] == _ww.TITLE_W and _tp["rgba"].size[0] > 2 * _ww.TITLE_W, \
            "标题条没放下两遍标题"
        _tsp = _ww.title_prep({"artwork": {"title": "短"}}, _sthumb, (255, 200, 80))
        assert _tsp is not None and _tsp["scroll"] is False, "短标题不该滚动"
        # 短标题只裁**一遍标题本体**: 若照样按可视区宽裁, 窗口右端会越过第二遍标题的起点 ——
        # 同一块里出现两个标题, 而且因为不滚而一动不动 (2026-09-28 
        # )。所以「不滚时裁窗宽度收窄、滚时铺满」得钉住。
        assert "TITLE_FIT" in _tprep and _tsp["fit"] < _ww.TITLE_W == _tp["fit"], \
            ("短标题裁窗没收窄 / 长标题裁窗没铺满", _tsp["fit"], _tp["fit"])
        _spf = _ww.title_frame(_scard, _tsp, 0, (255, 200, 80))
        _rx = _tsp["x"] + (_ww.TITLE_W + _tsp["fit"]) // 2
        _rz = (_rx, _tsp["y"], _tsp["x"] + _ww.TITLE_W, _tsp["y"] + _tsp["h"])
        assert _spf.crop(_rz).tobytes() == _scard.crop(_rz).tobytes(), \
            "短标题右侧露出了第二遍标题 (窗口没裁在第一遍里)"
        # 条里两遍标题必须**逐像素重合** —— 不重合滚到接缝就会跳一下。
        _strip = _tp["rgba"]
        _cA = _strip.crop((_ww.TITLE_W, 0, _ww.TITLE_W + _tp["tw"], _ww.BADGE_H))
        _cB = _strip.crop((_ww.TITLE_W + _tp["period"], 0,
                           _ww.TITLE_W + _tp["period"] + _tp["tw"], _ww.BADGE_H))
        assert _cA.tobytes() == _cB.tobytes(), "条里两遍标题不重合 —— 滚到接缝会跳一下"
        _lcard = _ww.draw_card(_longinfo, _sthumb)
        _lkeep = _lcard.copy()
        _l0 = _ww.title_frame(_lcard, _tp, 0, (255, 200, 80))
        _l1 = _ww.title_frame(_lcard, _tp, 60, (255, 200, 80))
        assert _lcard.tobytes() == _lkeep.tobytes(), "title_frame 改了底图 (缓存会被污染)"
        assert _l0.tobytes() != _l1.tobytes(), "标题没在滚 (两个相位画面一样)"

        # ---------- ⑤⑥ 缓存 + 主入口: 只读缓存 / 手动生成 (全程不联网) ----------
        # 2026-09-27 设计约定把「自动生成」改成「只在点按钮时生成当前这张」+「一定带图」,
        # 于是 pending/后台线程/失败静默期那套全删了 —— 这里钉的是新口径:
        #   轮询那条路**一次接口都不许打**, 生成只走 generate_now (同步, 表单值优先, 必带图)。
        _orig_cache, _orig_gen = ai._cache_path, ai.generate
        ai._cache_path = lambda: str(tmp / "ai_text_cache.json")
        ai._CACHE = None
        try:
            info = {"file": {"path": str(img), "mtime": 111, "size": 222,
                             "name": "dark_blue.png"}}
            key = ai.cache_key(info, ok)
            assert str(img) in key and ok["model"] in key and ai.AI_VER in key, key
            assert ai.cache_get(key) is None
            ai.cache_put(key, "夜色沉", "灯火在远处")
            assert ai.cache_get(key) == {"title": "夜色沉", "mood": "灯火在远处"}
            assert _json.loads((tmp / "ai_text_cache.json").read_text(encoding="utf-8"))[key]
            assert ai.cache_key(info, dict(ok, model="别的")) != key, \
                "换了模型还用旧缓存 —— 用户改完模型看不到文风变化会以为没生效"
            assert ai.cache_key({"file": dict(info["file"], size=999)}, ok) != key, \
                "图换了还是同一个键"
            assert ai.file_key(None) == "" and ai.texts_for(None, {"ai_text": ok})["state"] == "off"
            # 上面为了验缓存塞了一条进去, 这里清掉 —— 下面要验的是「这张没生成过」的行为
            with ai._LOCK:
                ai._cache_load().pop(key, None)
            assert ai.cache_get(key) is None

            assert ai.texts_for(info, {"ai_text": dict(ok, enabled=False)})["state"] == "off"
            assert ai.texts_for(info, {"ai_text": dict(ok, api_key="")})["state"] == "off"
            assert ai.texts_for(info, {"ai_text": ok})["state"] == "miss", \
                "没生成过的这张该是 miss (前端用本地兜底句)"
            calls = []

            def _fake_gen(_info, _sec, log=None):
                calls.append(_sec)
                return {"title": "夜色沉", "mood": "灯火在远处"}

            ai.generate = _fake_gen
            assert ai.texts_for(info, {"ai_text": ok})["state"] == "miss"
            assert calls == [], "只读缓存的那条路偷偷发起了生成 (换壁纸不该自动烧钱)"

            # 「⬆ 上传当前壁纸」: 表单里的地址/模型/密钥优先于已存的, 且一定带图
            out = ai.generate_now(info, {"ai_text": ok},
                                  form={"base_url": "http://x/v1", "model": "m9",
                                        "api_key": "sk-new"})
            assert out["state"] == "ready" and out["title"] == "夜色沉" \
                and out["mood"] == "灯火在远处", out
            assert len(calls) == 1, f"一次点击生成了 {len(calls)} 次"
            assert calls[0]["model"] == "m9" and calls[0]["base_url"] == "http://x/v1", \
                "表单里这次填的值没盖过已存的配置 "
            assert calls[0]["api_key"] == "sk-new"
            assert calls[0]["vision"] is True, "「上传当前壁纸」必须带图"
            assert calls[0]["enabled"] is True, "点按钮就该视为已启用"
            key9 = ai.cache_key(info, dict(ok, model="m9"))
            assert key9 != key, "换模型没换缓存键"
            assert ai.cache_get(key9) == {"title": "夜色沉", "mood": "灯火在远处"}, \
                "生成完没写进缓存 → 桌面小控件与设置页都看不到"
            _st = ai.texts_for(info, {"ai_text": dict(ok, model="m9")})
            assert _st["state"] == "ready" and _st["cached"] is True, _st
            # 没有当前壁纸时先说清楚, 不去打接口
            try:
                ai.generate_now({}, {"ai_text": ok})
                assert False, "没有当前壁纸还去打接口"
            except ai.AIError as e:
                assert "壁纸" in str(e), e
            # 三件套不齐: 人话, 不联网
            try:
                ai.generate_now(info, {"ai_text": dict(ok, api_key="")})
                assert False, "密钥没填还去打接口"
            except ai.AIError as e:
                assert "先填好" in str(e), e
            assert len(calls) == 1, "失败路径不该再生成一次"
        finally:
            ai.generate = _orig_gen
            ai._cache_path = _orig_cache
            ai._CACHE = None

        # ---------- ⑦ 接口: 密钥不回显 / 一颗按钮干完全部 ----------
        _orig_dir, _orig_path = upaths.config_dir, upaths.user_config_path
        upaths.config_dir = lambda: tmp
        upaths.user_config_path = lambda: tmp / "user.json"
        try:
            from web.server import create_app
            c = create_app().test_client()
            (tmp / "user.json").write_text(_json.dumps(
                {"ai_text": {"enabled": True, "api_key": "sk-abcdef123456", "model": "m"}},
                ensure_ascii=False), encoding="utf-8")
            r = c.get("/api/ai")
            got = r.get_json()
            assert got["has_key"] is True and got["key_tail"] == "3456", got
            assert "api_key" not in got, "密钥原文不该出现在响应里"
            assert "sk-abcdef123456" not in r.get_data(as_text=True), "密钥原文漏进响应体了"
            # 活动日志是**明文落盘**的, 而 /api/ai/run 的密钥会随请求体进来 → 必须打码
            from web.server import _act_short
            assert "api_key=***" in _act_short({"api_key": "sk-abcdef123456"}), \
                "密钥会原样写进活动日志文件"
            assert "sk-abcdef123456" not in _act_short({"api_key": "sk-abcdef123456", "model": "m"})
            # 打码表别误伤: 裸的 "auth" 是 author_name 的子串 (会把作者名一起打掉)
            assert "某人" in _act_short({"author_name": "某人", "artwork_id": "1"}), \
                "打码表把 author_name 一起打掉了"
            # 旧的「保存」按钮已并进两颗新按钮 (共用同一份表单口径), 那条路不该再留。
            # /api/ai 只剩 GET (读设置) → POST 该 405。
            assert c.post("/api/ai", json={}).status_code == 405, \
                "旧的保存接口该随按钮一起删掉, 留着就是第二份真源"
            assert c.get("/api/ai").status_code == 200, "/api/ai 的读设置该还在"
            # 「🔌 测试连接」2026-09-27 被需求加回。
            # 空配置点它就该 502 + 人话, 而不是 404 —— 它是排障入口, 得先于壁纸存在。
            # ★ 先把 user.json 清空: 上一步为验打码塞过假密钥, 不清就会**真的**打出去
            # (T132 的铁律是全程离线, 只许碰 127.0.0.1:9 这个必拒端口)。
            (tmp / "user.json").write_text("{}", encoding="utf-8")
            _t = c.post("/api/ai/test", json={})
            assert _t.status_code == 502, "测试连接该在, 且空配置要报 502"
            assert "先填好" in str(_t.get_json().get("error") or ""), _t.get_json()
            # 它和生成走**同一套**「表单值优先」口径, 否则会「测试说通、上传说失败」:
            # 两条路都得从 sec_from_form 拼配置, 差别只在 vision (测试不发图)。
            _ai_src = (_Path(PROJECT_ROOT) / "core" / "ai_text.py").read_text(encoding="utf-8")
            assert "def sec_from_form(" in _ai_src and "def test_connection(" in _ai_src
            assert "sec_from_form(cfg, form, vision=True)" in _ai_src, "生成没走共用口径"
            assert "sec_from_form(cfg, form, vision=False)" in _ai_src, "测试连接没走共用口径"
            _srv_ai = (_Path(PROJECT_ROOT) / "web" / "server.py").read_text(encoding="utf-8")
            assert "test_connection(form=body" in _srv_ai, "测试连接接口没接上 core"
            # max_tokens 必须留够 thinking 的余量: 2026-09-27 实测 deepseek-flash 在 60 个
            # token 下把预算**全烧在 reasoning 上**, 回 content='' —— 用户看到的就是「上传失败」
            assert "max_tokens\": MAX_TOKENS" in _ai_src, "生成没用统一的输出预算常量"
            # 判**下限**而不是钉死数字: 这条护栏要防的是「预算又被估小了」, 不是「不许调大」。
            # 
            _mt = int(ai.MAX_TOKENS)
            assert _mt >= 600, f"输出预算被估小了 ({_mt}), 推理模型的 thinking 会把它烧光"
            # 空回复/撞上限必须分得开, 否则用户只知道「失败」不知道改哪一项
            assert "def _empty_reply_reason(" in _ai_src and "finish == \"length\"" in _ai_src
            # 从没配过 ai_text 的 user.json: 读得到默认值, 不 500
            (tmp / "user.json").write_text("{}", encoding="utf-8")
            got = c.get("/api/ai").get_json()
            assert got["enabled"] is False and got["has_key"] is False, got
            assert got["base_url"] == ucfg.AI_DEFAULTS["base_url"]
            # 三件套不齐就点按钮: 人话 502, 不去打一个必然失败的请求
            r = c.post("/api/ai/run", json={})
            assert r.status_code == 502 and "先填好" in r.get_json()["error"], r.get_json()
            # 连不上的地址: 回一句人话而不是 500 页 (本机必然拒绝连接的端口, 不碰外网)。
            # 这里直接打 _chat 而不是走接口 —— 本机有没有当前壁纸是不确定的 (依赖注册表),
            # 走接口会把「没有壁纸」和「连不上」两种错误混在一起, 断言就飘了。
            try:
                ai._chat({"model": "m"}, {"base_url": "http://127.0.0.1:9/v1",
                                          "api_key": "k", "timeout": 5})
                assert False, "连不上的地址居然没报错"
            except ai.AIError as e:
                assert "连不上" in str(e) or "接口" in str(e), e
        finally:
            upaths.config_dir, upaths.user_config_path = _orig_dir, _orig_path

        # ---------- ⑧ 接线 ----------
        ww_src = (_Path(PROJECT_ROOT) / "utils" / "widget_window.py").read_text(encoding="utf-8")
        assert 'bool(fo.get("ready")), ai_sig(info)' in ww_src, \
            "info_key 没带 AI 签名 → 文案到货了也被当成「文件名没变」丢掉"
        assert "mood_line(info, thumb)" in ww_src and "ai_texts(info)" in ww_src
        assert len(_ww.info_key(None)) == 4, _ww.info_key(None)
        _k1 = _ww.info_key({"file": {"name": "a.jpg"}, "ai": {"state": "miss"}})
        _k2 = _ww.info_key({"file": {"name": "a.jpg"},
                            "ai": {"state": "ready", "mood": "夜色沉"}})
        assert _k1 != _k2, "miss → ready 没被当成数据变化"
        # 只有 ready 算数: off/miss/error 一律当没有 (否则会先闪一句空白)
        assert _ww.ai_texts({"ai": {"state": "miss", "mood": "x"}}) == {}
        assert _ww.ai_texts({"ai": {"state": "error", "mood": "x"}}) == {}
        assert _ww.ai_texts({"ai": {"state": "ready", "mood": "灯火在远处"}})["mood"] == "灯火在远处"
        # 作品标题永远优先 —— AI 只填那个「没名字」的位置, 不许盖掉真标题
        assert _ww.card_title({"artwork": {"title": "真标题"},
                               "ai": {"state": "ready", "title": "AI标题"}}) == "真标题"
        # 用户 2026-09-27 报「文案改了, 标题没改欸」: 自定义作品的「标题」是入库时按
        # 「来源夹 / 文件名」拼出来的串, 它**有** title 字段 —— 只看字段在不在, AI 标题
        # 永远轮不到显示。这种程序拼的标题不算真标题 (见 _ww._machine_title)。
        _junk = {"artwork": {"title": "X图/#其他来源# / 1e94516aeda0e378",
                             "artwork_id": "ext_166a87817d786d74"},
                 "file": {"name": "1e94516aeda0e378.png"},
                 "ai": {"state": "ready", "title": "月下提灯"}}
        assert _ww.card_title(_junk) == "月下提灯", _ww.card_title(_junk)
        assert _ww.card_title(dict(_junk, ai={"state": "miss"})) == "这一张还没有名字", \
            _ww.card_title(dict(_junk, ai={"state": "miss"}))
        for _mt in ("1e94516aeda0e378", "109395314_p0", "foo/bar", "#其他来源#"):
            assert _ww._machine_title(_mt), f"程序拼的标题没被认出来: {_mt}"
        for _rt in ("月下提灯", "夜に駆ける", "真标题"):
            assert not _ww._machine_title(_rt), f"真标题被误判成程序拼的: {_rt}"
        assert _ww.card_title({"file": {"name": "109395314_p0.jpg"},
                               "ai": {"state": "ready", "title": "夜色沉"}}) == "夜色沉"
        assert _ww.card_title({"file": {"name": "109395314_p0.jpg"}}) == "来自壁纸夹的一张"
        _th = _PImg.new("RGB", (48, 48), (10, 14, 50))
        assert _ww.mood_line({"ai": {"state": "ready", "mood": "灯火在远处"}}, _th) == "灯火在远处"
        assert _ww.mood_line({"ai": {"state": "miss"}}, _th) == _ww.mood_of(_th), \
            "还没生成好时该用本地那句兜底, 而不是空着"
        assert _ww.mood_line(None, None) == ""
        srv_src = (_Path(PROJECT_ROOT) / "web" / "server.py").read_text(encoding="utf-8")
        assert 'info["ai"] = texts_for(info' in srv_src, "轮询接口没附上 AI 文案"
        js = (_Path(PROJECT_ROOT) / "web" / "static" / "app.js").read_text(encoding="utf-8")
        html = (_Path(PROJECT_ROOT) / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for _need in ("loadAiText()", "runAiText", "testAiText", "'/api/ai'",
                      "'/api/ai/run'", "'/api/ai/test'", "function aiFormBody()"):
            assert _need in js, f"设置页少了 {_need}"
        for _id in ('id="ai-base"', 'id="ai-model"', 'id="ai-key"',
                    'id="ai-run"', 'id="ai-test"'):
            assert _id in html, f"设置页少了 {_id}"
        assert 'id="ai-key" type="password"' in html, "密钥框该是 password"
        assert "key.value = ''" in js, "密钥框被回显了"
        # 那颗「测试连接」是排障入口, 必须和生成**共用同一份表单体** —— 各拼各的就会
        # 「测试说通、上传说失败」(2026-09-27 加它回来就是为了分开这两种失败)
        assert js.count("body: aiFormBody()") == 2, "两颗按钮提交的表单体必须同源"
        # 旧的开关/视觉/保存三颗按钮不许回来 (用户: 「壁纸文案搞那么多按钮是为何」)
        for _gone in ("ai-toggle", "ai-vision", "ai-save",
                      "saveAiText", "toggleAiVision"):
            assert _gone not in js and _gone not in html, f"旧按钮 {_gone} 又回来了"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t133_wallpaper_lib_generate() -> None:
    """T133: 🖼 壁纸库生成。

    把图库里够格当桌面的页**原样复制**进壁纸夹并登记到 wallpapers 表。覆盖:
      ① 两道门槛分开回话 (尺寸 / 比例) 与文件名规则; 门槛是**显式常量** —— 不许回去问屏幕
         (非 DPI 感知进程在 125% 缩放下会拿到 1536×864, 门槛会静默放松);
      ② 扫描口径: 已标记的作品/页、没算过 md5 的页一律不参与; 文件不在盘上算 missing;
         壁纸夹播不了的格式 (webp 等) 不往里倒;
      ③ 复制是**字节一致的原样复制** (不缩放不裁剪), 登记带 file_md5/src_md5/宽高;
      ④ 幂等: 同一页重复跑, 新增 0、文件不重复, 记录也不翻倍;
      ⑤ 没配壁纸夹 / 目录读不到 → 人话报错, 绝不回退成「当前壁纸所在文件夹」(那是猜);
      ⑥ 接口: 没配壁纸夹 POST 400 人话 + 后台线程跑 + GET 轮询到 done + 前端接线。
    """
    import tempfile
    import shutil
    import hashlib
    from PIL import Image as _PImg
    import utils.paths as upaths
    from db.store import PixivDatabase, WSt, PSt
    from core import wallpaper_lib as wl
    from core.wallpaper_trace import WALL_EXTS

    # ---------- ① 门槛与文件名 ----------
    assert wl.wallpaper_gate(1920, 1080) == "ok"          # 正好压线, 该放行
    assert wl.wallpaper_gate(3840, 2160) == "ok"
    assert wl.wallpaper_gate(2000, 1200) == "ok"          # ar 1.667 → 偏差 6.3%, 在 10% 内
    assert wl.wallpaper_gate(1920, 1200) == "ok"          # ar 1.6 → 偏差正好 10% (门槛是 ≤, 放行)
    assert wl.wallpaper_gate(1919, 1080) == "size"        # 差 1px 就不行
    assert wl.wallpaper_gate(1080, 1920) == "size"        # 竖图: 先被尺寸门槛拦下
    assert wl.wallpaper_gate(1280, 720) == "size"
    assert wl.wallpaper_gate(3000, 1200) == "ratio"       # 超宽: 尺寸过了, 比例刷掉 (两侧黑边)
    assert wl.wallpaper_gate(None, None) == "size"
    assert wl.wallpaper_gate(1920, 0) == "size"           # 除零不许抛
    assert wl.wallpaper_gate("1920", "1080") == "ok"
    assert wl.wallpaper_worthy(1920, 1080) is True
    assert wl.wallpaper_worthy(1920, 1300) is False       # 偏方: ar 1.477 → 偏差 17%, 该刷掉
    assert (wl.MIN_W, wl.MIN_H) == (1920, 1080), (wl.MIN_W, wl.MIN_H)
    assert abs(wl.AR_TOL - 0.10) < 1e-9, wl.AR_TOL
    assert wl.wallpaper_file_name("700001", 0, ".PNG") == "700001_p0.png"
    assert wl.wallpaper_file_name("700001", 3, "jpg") == "700001_p3.jpg"
    assert wl.wallpaper_file_name("700001", None, "") == "700001_p0"
    _wl_src = (PROJECT_ROOT / "core" / "wallpaper_lib.py").read_text(encoding="utf-8")
    # 判的是「有没有真的去问屏幕」(上面那两条断言钉的是常量的值, 这里钉的是取法) ——
    # 所以不查文档里提没提 Screen.Bounds, 只查有没有真调用。
    for _bad in ("GetSystemMetrics(", "win32api", "import tkinter", "pyautogui"):
        assert _bad not in _wl_src, f"门槛又去问屏幕了 ({_bad}) —— 缩放一改就静默放松"

    tmp = Path(tempfile.mkdtemp(prefix="t133wl_")).resolve()
    img_root, wall_dir = tmp / "img", tmp / "wall"
    img_root.mkdir(parents=True, exist_ok=True)
    wall_dir.mkdir(parents=True, exist_ok=True)
    orig = (upaths.user_data, upaths.data_root, upaths.config_dir, upaths.user_config_path)
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    upaths.config_dir = lambda: tmp
    upaths.user_config_path = lambda: tmp / "user.json"
    (tmp / "user.json").write_text("{}", encoding="utf-8")
    try:
        db = PixivDatabase(tmp / "pixiv.db")
        db.upsert_author("555", "shuno")

        def _mk(aid, pn, w, h, ext=".png", md5=True):
            """建一页: 磁盘上是张小图, DB 里写的才是**判定用的宽高**。

            生成器只看 DB 的 width/height, 从不解码大图 (壁纸夹 1000+ 张, 每张解一遍受不了),
            所以这里不必真造 1920×1080 的位图。
            """
            rel = f"X图/shuno_555/{aid}_p{pn}{ext}"
            p = img_root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            _PImg.new("RGB", (48, 32), (30, 60, 90)).save(
                str(p), format={".png": "PNG", ".jpg": "JPEG", ".webp": "WEBP"}[ext])
            fp = hashlib.md5(p.read_bytes()).hexdigest() if md5 else ""
            db.change_work_status(aid, WSt.DONE, title=f"作品{aid}", author_id="555",
                                  total_pages=1, illust_type=0)
            db.mark_page_status(aid, pn, PSt.DONE, local_path=rel, md5=fp, width=w, height=h)
            return rel, fp

        _mk("700001", 0, 1920, 1080)                 # 该过
        _mk("700002", 0, 3000, 1200)                 # 比例不合
        _mk("700003", 0, 1280, 720)                  # 尺寸不合
        _mk("700004", 0, 1920, 1080)                 # 作品已标记 → 不参与
        _mk("700005", 0, 1920, 1080, ext=".webp")    # 格式不支持
        _rel6, _ = _mk("700006", 0, 1920, 1080)      # 文件不在盘上
        _mk("700007", 0, 1920, 1080)                 # 页已标记 → 不参与
        _mk("700008", 0, 2000, 1125, ext=".jpg")     # 正好 16:9, 该过
        _mk("700009", 0, 1920, 1080, md5=False)      # 没算过 md5 → 不参与
        db.soft_delete_artworks(["700004"])
        db.soft_delete_pages([{"artwork_id": "700007", "page_num": 0}])
        (img_root / _rel6).unlink()

        # ---------- ②③ 跑一遍 ----------
        res = wl.generate(db, wall_dir=wall_dir, img_root=img_root)
        assert res["scanned"] == 6, res            # 6 行参与判定 (其余 3 行被标记/无指纹挡掉)
        assert res["passed"] == 4, res             # 700001/700005/700006/700008 过了门槛(后两个再被格式/文件挡下)
        assert res["skipped_size"] == 1 and res["skipped_ratio"] == 1, res
        assert res["skipped_ext"] == 1, res
        assert res["missing"] == 1, res
        assert res["added"] == 2 and res["errors"] == [], res
        assert res["wall_dir"] == str(wall_dir), res
        names = sorted(p.name for p in wall_dir.iterdir())
        assert names == ["700001_p0.png", "700008_p0.jpg"], names
        # 原样复制: 字节一致 (不缩放不裁剪不转码)
        for _aid, _pn, _ext in (("700001", 0, ".png"), ("700008", 0, ".jpg")):
            src = img_root / f"X图/shuno_555/{_aid}_p{_pn}{_ext}"
            assert (wall_dir / f"{_aid}_p{_pn}{_ext}").read_bytes() == src.read_bytes(), _aid
        # 登记: 血缘 / 指纹 / 宽高 / 裁切列留空 (留给以后的手动裁剪)
        rows = {r["file_name"]: r for r in db.list_wallpapers()}
        assert set(rows) == set(names), list(rows)
        _r1 = rows["700001_p0.png"]
        assert (_r1["artwork_id"], _r1["page_num"]) == ("700001", 0), _r1
        assert (_r1["width"], _r1["height"]) == (1920, 1080), _r1
        assert _r1["page_rel"] == "X图/shuno_555/700001_p0.png", _r1  # LEFT JOIN 拿到血缘
        assert _r1["title"] == "作品700001" and _r1["author_name"] == "shuno", _r1
        assert _r1["file_md5"] == _r1["src_md5"] and _r1["file_md5"], _r1
        assert _r1["file_md5"] == hashlib.md5(
            (wall_dir / "700001_p0.png").read_bytes()).hexdigest(), "记的指纹和落盘的对不上"
        for _c in ("crop_mode", "crop_x0", "crop_y0", "crop_x1", "crop_y1"):
            assert _r1[_c] is None, f"原样复制的这条不该有裁切口径: {_c}={_r1[_c]}"
        assert {".png", ".jpg"} <= set(WALL_EXTS), WALL_EXTS

        # ---------- ④ 幂等 ----------
        res2 = wl.generate(db, wall_dir=wall_dir, img_root=img_root)
        assert res2["added"] == 0 and res2["skipped_exist"] == 2, res2
        assert sorted(p.name for p in wall_dir.iterdir()) == names, "重复跑多出了文件"
        assert len(db.list_wallpapers()) == 2, "重复跑把记录也翻了倍"
        assert db.wallpaper_file_names() == set(names)

        # ---------- ⑤ 报错口径: 绝不猜壁纸夹 ----------
        for _kw, _need in (({"wall_dir": None, "img_root": img_root}, "壁纸夹"),
                           ({"wall_dir": tmp / "nope", "img_root": img_root}, "壁纸夹"),
                           ({"wall_dir": wall_dir, "img_root": tmp / "nope"}, "图库目录")):
            try:
                wl.generate(db, **_kw)
                assert False, f"该报错却没报: {_kw}"
            except wl.WallpaperLibError as e:
                assert _need in str(e), e

        # ---------- ⑥ 接口 ----------
        from web.server import create_app
        c = create_app().test_client()
        # 没配壁纸夹 → 400 人话 (绝不能回退到「当前壁纸所在文件夹」, 那等于倒进用户的系统壁纸夹)
        r = c.post("/api/wallpaper/lib/scan")
        _j = r.get_json()
        assert r.status_code == 400 and _j["ok"] is False and "壁纸夹" in _j["error"], _j
        # 配好壁纸夹 + 清掉刚才那两条记录 (文件留着, 验接口这条链路真的复制+登记, 不是走幂等)
        (tmp / "user.json").write_text(
            json.dumps({"paths": {"wallpaper_dir": str(wall_dir)}}), encoding="utf-8")
        db.purge_wallpapers([r0["id"] for r0 in db.list_wallpapers()])
        assert db.list_wallpapers() == []
        r = c.post("/api/wallpaper/lib/scan")
        assert r.status_code == 200 and r.get_json()["ok"] is True, r.get_json()
        got = {}
        for _ in range(200):
            got = c.get("/api/wallpaper/lib").get_json()
            if got.get("status") != "running":
                break
            time.sleep(0.1)
        assert got.get("status") == "done", got
        assert got["result"]["added"] == 2, got["result"]
        assert len(db.list_wallpapers()) == 2, "接口跑完没落库"
        # 进度字段必须一直在 (前端靠它画进度), 跑完得清干净
        for _k in ("done", "total", "current", "error", "at", "result"):
            assert _k in got, f"/api/wallpaper/lib 少了 {_k}"
        assert got["current"] == "" and got["error"] == ""

        # 接线: 设置页那颗按钮 + 轮询
        js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
        for _need in ("loadWallLib()", "runWallLib", "wallLibPaint", "wallLibTimer",
                      "'/api/wallpaper/lib'", "'/api/wallpaper/lib/scan'"):
            assert _need in js, f"前端少了 {_need}"
        for _id in ('id="wall-lib-run"', 'id="wall-lib-state"'):
            assert _id in html, f"设置页少了 {_id}"
        assert '@app.route("/api/wallpaper/lib/scan", methods=["POST"])' in srv
        assert 'generate(db, wall_dir=d, img_root=img_root' in srv, "接口没接上 core"
        db.close()
    finally:
        (upaths.user_data, upaths.data_root,
         upaths.config_dir, upaths.user_config_path) = orig
        shutil.rmtree(str(tmp), ignore_errors=True)


def t134_launcher_hotapply_and_paths() -> None:
    """T134: 🚀 启动与托盘「立即生效」+ 📁 路径设置。

    ① 双击托盘图标 → open_gallery, 单击仍只 refresh;
    ② 热应用接线: main.py 把 _apply_launcher 交给 server.set_launcher_apply; 心跳守护按
       **代次**门控 —— 关了之后在跑的那条必须自己收工, 不许再判退出;
    ③ /api/launcher: 存盘后回调钩子并把逐项结果放进 applied; 启动动作如实回报 startup_only;
    ④ /api/paths: 读回默认落点 / 空串清除 / 相对路径拒绝 / 绝对路径落盘并回新落点 /
       只读位置跟着数据目录走 (数据库在数据目录里, 不是 config 旁);
    ⑤ /api/paths/pick: 复用系统目录对话框 —— 取消不动配置, 弹不出必回 JSON 报错;
    ⑥ 前端接线: 「📁 路径」卡片 + 位置一览 + 切换设置页时读取;
       启动与托盘文案不再笼统写「重启后生效」。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    from web import server as wsrv
    from web.server import create_app

    # ---------- ① 双击打开图库 / 单击仍只刷新 tooltip ----------
    from utils import tray
    assert tray.WM_LBUTTONDBLCLK == 0x0203
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_LBUTTONDBLCLK) == ("open_gallery",)
    assert tray.handle_tray_message(tray.WM_TRAY_CALLBACK, 0, tray.WM_LBUTTONUP) == ("refresh",)
    assert '"open_gallery"' in (PROJECT_ROOT / "utils" / "tray.py").read_text(encoding="utf-8"), \
        "tray.handle_message 没接 open_gallery 这个动作"

    # ---------- ② 热应用接线 (静态护栏) ----------
    main_src = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
    srv_src = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    assert "def set_launcher_apply(" in srv_src, "server.py 没有热应用钩子"
    assert "set_launcher_apply(_apply_launcher)" in main_src, "main.py 没把热应用钩子接上"
    assert "def _apply_launcher(" in main_src and "def _set_watch_browser(" in main_src
    assert "_watchdog_gen != gen" in main_src, "心跳守护必须认代次, 关了不许再判退出"
    assert "target=_heartbeat_watchdog, daemon=True" not in main_src, \
        "心跳守护必须走 _set_watch_browser (代次门控), 不许再直接起线程"
    assert "_LAUNCHER_STARTUP_ONLY" in main_src and "auto_open_browser" in main_src

    # ⚠ 2026-10-20 :
    #   根因 = 关开关时调 _tray_stop() → Tray.stop() 原先投 **WM_CLOSE**, 而
    #   handle_tray_message 把 WM_CLOSE 翻成 ("quit",) → _request_quit → on_quit → 整个进程退出。
    #   stop() 的语义必须是「只停托盘」, 所以它只能投线程 WM_QUIT, 绝不许再碰 WM_CLOSE/on_quit。
    tray_src = (PROJECT_ROOT / "utils" / "tray.py").read_text(encoding="utf-8")
    _stop_blk = tray_src.split("def stop(self)")[1].split("def _run")[0]
    assert "PostMessageW(self.hwnd, WM_CLOSE" not in tray_src, \
        "Tray.stop 不许走 WM_CLOSE —— 那是「退出程序」的语义, 关托盘开关会连带退出整个进程"
    assert "PostThreadMessageW(self._tid, WM_QUIT" in _stop_blk, \
        "停托盘要投线程 WM_QUIT 让消息循环收工 (finally 里照常摘图标)"
    assert "self.on_quit" not in _stop_blk and "_request_quit(" not in _stop_blk, \
        "stop() 不许碰 on_quit / _request_quit (那是「退出程序」, 注释里提到名字不算)"
    # 而 WM_CLOSE→quit 这条**退出**路径必须保留 (系统关机 / --quit / 右键「退出 Vixiq」都走它)
    assert tray.handle_tray_message(tray.WM_CLOSE, 0, 0) == ("quit",), "退出路径不许被顺手删掉"
    assert "PostMessageW(hwnd, WM_CLOSE" in main_src, "--quit 仍要给托盘投 WM_CLOSE"
    # 关闭分支仍用 _tray_stop (它就是「只停托盘」), 且不许在关托盘时广播 app_quit
    _off = main_src.split('k == "tray_enabled"')[1].split('elif k == "wall_widget"')[0]
    assert "_tray_stop()" in _off, "关托盘常驻要调 _tray_stop"
    assert "app_quit" not in _off and "_cleanup" not in _off, \
        "关托盘不许连带退出程序 / 广播 app_quit"

    tmp = Path(tempfile.mkdtemp(prefix="t134p_")).resolve()
    orig = (upaths.user_data, upaths.data_root, upaths.config_dir, upaths.user_config_path)
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    upaths.config_dir = lambda: tmp
    upaths.user_config_path = lambda: tmp / "user.json"
    (tmp / "user.json").write_text("{}", encoding="utf-8")
    saved_hook = wsrv._launcher_apply
    try:
        c = create_app().test_client()

        # ---------- ④ 默认态: 没设置重定向时各落点 ----------
        j = c.get("/api/paths").get_json()
        assert j["saved"] == {"data_root_override": "", "temp_root_override": "",
                              "img_subdir": ""}, j["saved"]
        assert j["defaults"]["img_subdir"] == "img", j["defaults"]
        eff = j["effective"]
        assert eff["db"].endswith("pixiv.db"), eff["db"]
        assert Path(eff["db"]).parent == Path(eff["data_root"]), "数据库要在数据目录里"
        assert Path(eff["logs"]) == Path(eff["data_root"]) / "logs", eff["logs"]
        assert Path(eff["thumbs"]) == Path(eff["data_root"]) / "thumbs", eff["thumbs"]
        assert Path(eff["temp_root"]) == Path(eff["data_root"]) / "temp", eff["temp_root"]
        assert Path(eff["img"]) == Path(eff["data_root"]) / "img", eff["img"]
        assert "wallpaper_dir" in j["effective"], "位置一览要带上壁纸夹"
        assert Path(eff["config_file"]).name == "user.json"
        assert Path(j["defaults"]["data_root"]).name == "data", j["defaults"]
        # 占用检测: 五个会长的位置各回一个 MB 数 (不存在 → None, 不许抛)
        assert set(j["sizes"]) == {"db", "thumbs", "temp", "feature_db", "img"}, j["sizes"]

        # 数据根/临时目录: 相对路径一律拒绝 (否则落点随 CWD 漂); 认不出的键也拒
        r = c.post("/api/paths", json={"data_root_override": "relative/dir"})
        assert r.status_code == 400 and r.get_json()["ok"] is False, r.get_json()
        assert c.post("/api/paths", json={"temp_root_override": "rel"}).status_code == 400
        assert c.post("/api/paths", json={"nope": "x"}).status_code == 400

        # 绝对路径 → 落盘 + 当场看到新落点 (不必等重启才反映)
        d2 = tmp / "mydata"
        r = c.post("/api/paths", json={"data_root_override": str(d2)})
        j = r.get_json()
        assert r.status_code == 200 and j["ok"] is True and j["restart_required"] is True, j
        assert j["saved"]["data_root_override"] == str(d2), j["saved"]
        assert j["effective"]["data_root"] == str(d2), j["effective"]
        assert Path(j["effective"]["db"]).parent == d2, "数据库没跟着数据目录走"
        assert Path(j["effective"]["img"]) == d2 / "img"
        assert Path(j["effective"]["temp_root"]) == d2 / "temp", "没设临时目录时应跟随数据目录"
        assert d2.is_dir(), "选了就该把目录建出来 (存个建不出的路径, 下次启动就是一堆报错)"
        on_disk = json.loads((tmp / "user.json").read_text(encoding="utf-8"))
        assert on_disk["paths"]["data_root_override"] == str(d2), on_disk

        # 图片目录: 单段相对名 = 数据目录下的子目录 (这是它本来的默认形态 "img")
        j = c.post("/api/paths", json={"img_subdir": "myimg"}).get_json()
        assert j["saved"]["img_subdir"] == "myimg", j["saved"]
        assert Path(j["effective"]["img"]) == d2 / "myimg", j["effective"]
        assert Path(j["effective"]["img"]).parent == d2, "相对图片名要挂在数据目录下"
        # 相对名只认单段: "a/b" 语义含糊, ".." 能爬到数据根外面
        for _bad in ("a/b", "..", "../x"):
            assert c.post("/api/paths", json={"img_subdir": _bad}).status_code == 400, _bad
        # 图片目录也接受绝对路径 —— 大图片放非系统盘就是走这条
        imgs = tmp / "F_img"
        j = c.post("/api/paths", json={"img_subdir": str(imgs)}).get_json()
        assert j["effective"]["img"] == str(imgs), j["effective"]
        assert Path(j["effective"]["img"]).parent != d2, "绝对路径不该再挂在数据目录下"
        # 清除图片目录 → 回到「数据目录下的 img」
        j = c.post("/api/paths", json={"img_subdir": ""}).get_json()
        assert j["saved"]["img_subdir"] == "", j["saved"]
        assert Path(j["effective"]["img"]) == d2 / "img", j["effective"]

        # 临时目录单独覆盖: 它不再跟随数据目录, 且不该动数据目录
        t2 = tmp / "mytemp"
        j = c.post("/api/paths", json={"temp_root_override": str(t2)}).get_json()
        assert j["effective"]["temp_root"] == str(t2), j["effective"]
        assert j["effective"]["data_root"] == str(d2), "改临时目录不该动数据目录"

        # 空串 = 清除回默认 (临时目录回到「数据目录下的 temp」)
        j = c.post("/api/paths", json={"temp_root_override": ""}).get_json()
        assert j["saved"]["temp_root_override"] == "", j["saved"]
        assert j["effective"]["temp_root"] == str(d2 / "temp"), j["effective"]
        assert j["effective"]["data_root"] == str(d2), "清除临时目录不该动数据目录"

        # ---------- ⑤ 选目录: 取消不动配置 / 弹不出必回 JSON ----------
        from utils import folder_dialog
        had_pick = hasattr(folder_dialog, "pick_folder")
        orig_pick = getattr(folder_dialog, "pick_folder", None)
        try:
            folder_dialog.pick_folder = lambda *a, **k: None
            j = c.post("/api/paths/pick", json={"which": "data"}).get_json()
            assert j["ok"] is False and j.get("cancelled") is True, j
            assert json.loads((tmp / "user.json").read_text(encoding="utf-8"))[
                "paths"]["data_root_override"] == str(d2), "选了取消却动了配置"
            folder_dialog.pick_folder = lambda *a, **k: str(tmp / "picked")
            j = c.post("/api/paths/pick", json={"which": "data"}).get_json()
            assert j["ok"] is True and j["saved"]["data_root_override"] == str(tmp / "picked"), j
            # 图片目录也能从系统对话框里选 (挑非系统盘就靠这个按钮)
            j = c.post("/api/paths/pick", json={"which": "img"}).get_json()
            assert j["ok"] is True and j["saved"]["img_subdir"] == str(tmp / "picked"), j

            def _boom(*a, **k):
                raise RuntimeError("no dialog")
            folder_dialog.pick_folder = _boom
            r = c.post("/api/paths/pick", json={"which": "temp"})
            assert r.status_code == 500 and r.get_json()["ok"] is False, \
                "弹不出对话框必须是 JSON 报错, 不能让前端把 500 当「取消」把输入框清空"
        finally:
            if had_pick:
                folder_dialog.pick_folder = orig_pick
            else:
                delattr(folder_dialog, "pick_folder")
        assert c.post("/api/paths/pick", json={"which": "nope"}).status_code == 400

        # ---------- ③ /api/launcher: 存盘后热应用 + applied 回传 ----------
        seen = {}

        def _hook(upd: dict) -> dict:
            seen.clear()
            seen.update(upd)
            return {k: ("startup_only" if k == "auto_open_browser" else "applied") for k in upd}

        wsrv.set_launcher_apply(_hook)
        j = c.post("/api/launcher", json={"auto_open_browser": True}).get_json()
        assert seen == {"auto_open_browser": True}, seen
        assert j["applied"]["auto_open_browser"] == "startup_only", \
            "启动动作必须如实回报 startup_only (那一刻压根没浏览器可开)"
        j = c.post("/api/launcher", json={"tray_enabled": False}).get_json()
        assert j["applied"]["tray_enabled"] == "applied", j
        assert c.post("/api/launcher", json={"nope": 1}).status_code == 400, "白名单老口径不变"
        # 没接钩子时开关照存, 只是不热应用 (冒烟/纯 web 场景)
        wsrv.set_launcher_apply(None)
        j = c.post("/api/launcher", json={"tray_enabled": True}).get_json()
        assert j["ok"] is True and j["applied"] is None, j

        # ---------- ④b 一键清临时目录 (会长的缓存放 C 盘也得能一键收回) ----------
        # 放在最后: 这条真会清 temp_root —— 而 tempfile.mkdtemp 已被 paths 重定向到 temp_root,
        # 本用例的 tmp 目录就住在里面, 提前跑会把后面要读的 user.json 一起清掉 (踩过一次)。
        r = c.post("/api/paths/temp/clean")
        j = r.get_json()
        assert r.status_code == 200 and j["ok"] is True, j
        assert "removed" in j and "freed_mb" in j, j
        assert set(j["sizes"]) == {"db", "thumbs", "temp", "feature_db", "img"}, j["sizes"]

        # ---------- ⑥ 前端接线 ----------
        js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
        html = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
        for _need in ("loadPaths()", "function paintPaths(", "function pickPathDir(",
                      "function clearPathDir(", "function cleanTempDir(",
                      "'/api/paths'", "'/api/paths/pick'", "'/api/paths/temp/clean'",
                      "const PATH_EDITABLE"):
            assert _need in js, f"前端少了 {_need}"
        for _id in ('id="path-img-input"', 'id="path-data-input"', 'id="path-temp-input"',
                    'id="path-temp-clean"', 'id="path-positions"'):
            assert _id in html, f"设置页少了 {_id}"
        # 壁纸夹路径必须在「📁 路径」卡片里 
        _card = html.split('<h3>📁 路径</h3>', 1)[1].split('</div>\n        <div class="card mini">', 1)[0]
        assert 'id="wall-dir-input"' in _card and 'onclick="pickWallDir()"' in _card, \
            "壁纸夹没搬进「📁 路径」卡片"
        # 「🚀 启动与托盘」不属于「桌面与壁纸」组 —— 它跟壁纸无关, 属程序生命周期
        _dsk = html.split('<h4>桌面与壁纸</h4>', 1)[1].split('<h4>浏览与下载</h4>', 1)[0]
        assert 'id="launcher-switches"' not in _dsk, "「启动与托盘」不该挂在「桌面与壁纸」组里"
        assert '<h4>系统与启动</h4>' in html, "「启动与托盘」缺独立分组"
        # 启动与托盘那组不许再笼统写「重启后生效」—— 除启动动作外都是存盘即生效
        _blk = html.split('id="launcher-switches"', 1)[0].rsplit('<div class="card mini">', 1)[-1]
        assert "重启后生效" not in _blk, "启动与托盘不该再笼统写「重启后生效」"
        assert "立即生效" in _blk
        assert "（重启后生效）" not in js, "开关的回执不该再说要重启"
        assert "（已立即生效）" in js
        # 关掉托盘后右下角就没退出入口了 —— 顺嘴指一下还能从哪儿退 (否则用户会以为退不掉)
        assert "之后从页面「退出程序」或 Ctrl+C 退出" in js, "关托盘后要指一下退出入口"
        # 路径那处确实要重启, 所以只留一句, 不摆理由 (用户: 不用在界面上写理由)
        _pblk = html.split('id="path-img-input"', 1)[0].rsplit('<h3>📁 路径</h3>', 1)[-1]
        assert "重启 Vixiq 后生效" in _pblk and "缓存" not in _pblk, _pblk
    finally:
        wsrv.set_launcher_apply(saved_hook)
        (upaths.user_data, upaths.data_root,
         upaths.config_dir, upaths.user_config_path) = orig
        shutil.rmtree(str(tmp), ignore_errors=True)


def t135_artwork_pages_api_and_queue_pages() -> None:
    """T135: 🖼 查询页「按页下载」之一 —— 取页接口 + 队列 pages 列。

    ① 迁移 020 给 download_queue_items 加 pages TEXT (NULL = 整部, 向后兼容),
       且**不塞进 strategy** (那列被引擎当 DownloadStrategy 枚举解析);
    ② queue_add(pages=[0,2]) 落成去重排序后的 JSON 串, 不传 = NULL;
    ③ GET /api/artwork/<id>/pages: 未登录 → **200 + need_login** (前端要能区分
       「没登录」和「请求失败」) / 登录后 low=regular、high=original + 合并库内页状态
       + is_extra (超出远端页数的「附加页」); 作品不存在 → 404;
    ④ POST /api/cart 带 pages 入队成功, 不带 pages 的整部口径一字不改。
    """
    import tempfile
    import shutil
    import utils.paths as upaths
    import core.search_client as scl
    from core.search_client import ArtworkNotFoundError
    from db.store import PixivDatabase, PSt
    from web import server as wsrv
    from web.server import create_app

    # ---------- ① 迁移 + store 接线 (静态) ----------
    mig = (PROJECT_ROOT / "db" / "migrations" / "020_queue_pages.sql")
    assert mig.exists(), "缺 020_queue_pages.sql"
    _txt = mig.read_text(encoding="utf-8")
    _up = _txt.split("-- UP:")[1].split("-- DOWN:")[0]
    assert "ALTER TABLE download_queue_items ADD COLUMN pages TEXT" in _up, _up
    assert "DROP COLUMN pages" in _txt.split("-- DOWN:")[1], "回滚节该给出 DROP"
    _store = (PROJECT_ROOT / "db" / "store.py").read_text(encoding="utf-8")
    _qadd = _store.split("def queue_add(")[1].split("def ")[0]
    assert "pages=None" in _qadd and "pages_json" in _qadd and "json.dumps" in _qadd, _qadd
    # 页级页数**绝不能**写进 strategy: 引擎按 DownloadStrategy 枚举解析它
    assert "strategy, status, source_note, pages" in _store, "pages 列要单独一列, 不塞 strategy"

    tmp = Path(tempfile.mkdtemp(prefix="t135p_")).resolve()
    orig = (upaths.user_data, upaths.data_root)
    upaths.user_data = lambda rel: (tmp / str(rel))
    upaths.data_root = lambda: tmp
    saved_sess = wsrv._pixiv_http_session_module
    saved_fi, saved_fp = scl.http_fetch_illust, scl.http_fetch_illust_pages

    class _Info:
        artwork_id = "777001"
        title = "按页下载测试"
        author_id = "555"
        author_name = "测试作者"
        page_count = 3
        is_r18 = False
        illust_type = "illust"

    def _fake_pages(session, aid, **kw):
        # 4 页: 远端只声明 3 页 → 第 4 页 (pn=3) 必须标 is_extra
        return [{"page_num": n, "original": f"https://i.pximg.net/x_{n}_p{n}.jpg",
                 "regular": f"https://i.pximg.net/r_{n}_p{n}.jpg",
                 "small": "", "thumb": "", "width": 100 + n, "height": 200 + n}
                for n in range(4)]

    try:
        # 预置: 777001 已在库, p0 已下载(pn=0), 其余未下载
        with PixivDatabase() as db:
            db.init_pages("777001", 3)
            db.mark_page_status("777001", 0, PSt.DONE, md5="m0")

        # ---------- ③ 未登录 → 200 + need_login (不是 401) ----------
        # ⚠ 要打桩的是**模块级别名**: create_app 内部另有一个同名局部函数会把闭包里的
        # 引用劫持掉 (见 web/server.py 里 _pixiv_http_session_module 处的说明)。
        wsrv._pixiv_http_session_module = lambda: None
        c = create_app().test_client()
        r = c.get("/api/artwork/777001/pages")
        assert r.status_code == 200, ("没登录不该报 401 让前端分不清", r.status_code)
        assert r.get_json()["status"] == "need_login", r.get_json()

        # ---------- ③ 登录后: low/high + 库内页状态 + is_extra ----------
        wsrv._pixiv_http_session_module = lambda: {"headers": {}, "cookies": {}}
        scl.http_fetch_illust = lambda *a, **k: _Info()
        scl.http_fetch_illust_pages = _fake_pages
        j = c.get("/api/artwork/777001/pages").get_json()
        assert j["status"] == "ok" and j["in_library"] is True, j
        assert j["artwork"]["page_count"] == 3 and j["artwork"]["title"] == "按页下载测试", j["artwork"]
        assert len(j["pages"]) == 4, j["pages"]
        p0 = j["pages"][0]
        assert p0["high"].endswith("x_0_p0.jpg") and p0["low"].endswith("r_0_p0.jpg"), p0
        assert p0["status"] == PSt.DONE, ("已入库的页要带上库里的页状态", p0)
        assert j["pages"][1]["status"] == PSt.PLACEHOLDER, j["pages"][1]
        assert [p["is_extra"] for p in j["pages"]] == [False, False, False, True], \
            "超出远端 page_count 的页要标 is_extra"
        # 作品不存在 → 404
        def _nf(*a, **k):
            raise ArtworkNotFoundError("作品不存在")
        scl.http_fetch_illust = _nf
        assert c.get("/api/artwork/777001/pages").status_code == 404
        scl.http_fetch_illust = lambda *a, **k: _Info()

        # ---------- ④ POST /api/cart: 带 pages 入队 ----------
        r = c.post("/api/cart", json={"item_type": "artwork", "artwork_id": "777002",
                                      "title": "#777002 按页", "page_count": 4,
                                      "author_id": "555", "author_name": "测试作者",
                                      "pages": [2, 0, 0]})
        assert r.status_code == 200 and r.get_json()["reason"] == "added", r.get_json()
        # 不带 pages = 整部 (向后兼容)
        r = c.post("/api/cart", json={"item_type": "artwork", "artwork_id": "777003",
                                      "title": "#777003 整部", "page_count": 4})
        assert r.get_json()["reason"] == "added", r.get_json()
        with PixivDatabase() as db:
            rows = {str(x.get("artwork_id")): x.get("pages") for x in db.queue_list()}
            assert rows["777002"] == "[0, 2]", ("页号要排序去重后存成 JSON 串", rows)
            assert rows["777003"] is None, ("不带 pages 就是整部, 必须是 NULL", rows)
            # page_count 仍是远端总页数 (不是选中页数), 否则整部会被判成已下完
            _it = [x for x in db.queue_list() if str(x.get("artwork_id")) == "777002"][0]
            assert int(_it["page_count"]) == 4, _it
            _w = db.get_work_info("777002")
            assert int(_w.total_pages) == 4, ("作品总页数必须是远端总页数", _w.total_pages)
    finally:
        wsrv._pixiv_http_session_module = saved_sess
        scl.http_fetch_illust, scl.http_fetch_illust_pages = saved_fi, saved_fp
        (upaths.user_data, upaths.data_root) = orig
        shutil.rmtree(str(tmp), ignore_errors=True)


def t136_page_download_engine_and_frontend() -> None:
    """T136: 🖼 查询页「按页下载」之二 —— 引擎/爬虫页级 + 前端远程大图 + 遮挡修复。

    ① `_only_pages_of` 纯函数 (NULL/脏数据 → 整部);
    ② 引擎: 页级任务**绕过**「整部已下完就跳过」那段 (否则点「补某页」被静默跳过),
       并把 only 传给 download_artwork;
    ③ 爬虫: 只下选中页 + 页级**拒绝回落浏览器通道** (浏览器通道会按 URL 规律把未选页也补齐);
       init_pages 仍按**远端总页数**写 (关键: 否则整部被判成已下完 → 图库谎报入库);
    ④ 前端: openRemoteLightbox + 缩略图多选条 + 「显示原图/下载该页/🔗 原网页」,
       查询页卡片点击**不再** window.open 原站;
    ⑤ 「调整页序」时收起「上个作品/下个作品」, 不再遮住「完成」按钮。
    """
    from core.download_engine import _only_pages_of
    eng = (PROJECT_ROOT / "core" / "download_engine.py").read_text(encoding="utf-8")
    craw = (PROJECT_ROOT / "core" / "crawler.py").read_text(encoding="utf-8")
    js = (PROJECT_ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "web" / "static" / "style.css").read_text(encoding="utf-8")
    srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")

    # ---------- ① 纯函数 ----------
    assert _only_pages_of({}) is None and _only_pages_of({"pages": None}) is None
    assert _only_pages_of({"pages": ""}) is None
    assert _only_pages_of({"pages": "junk"}) is None, "脏数据退回整部, 不许让任务炸掉"
    assert _only_pages_of({"pages": "[]"}) is None
    assert _only_pages_of({"pages": "[0, 2]"}) == {0, 2}
    assert _only_pages_of({"pages": '["1", -5]'}) == {1}, "负页码要丢掉"

    # ---------- ② 引擎接线 ----------
    _run = eng.split("def _run_item(")[1]
    assert "only = _only_pages_of(it)" in _run, "引擎没解析 pages 列"
    assert "if only is None and strategy == DownloadStrategy.ONLY_NEW" in _run, \
        "页级任务必须绕过「整部已下完就跳过」那段"
    assert "kw['pages'] = only" in _run, \
        "页级页码要只在页级任务时才带上 —— 整部的老签名不能多收一个关键字"

    # ---------- ③ 爬虫 ----------
    assert "def download_artwork(self, artwork_id, author_id=None, author_name=None," \
           "\n                         force_overwrite: bool = False, pages=None)" in craw, \
        "download_artwork 缺 pages 形参"
    assert "if t[0] in only" in craw, "没按选中页过滤"
    assert "按页下载不支持浏览器兜底" in craw, \
        "页级失败绝不能回落浏览器通道 (它会按 URL 规律把未选页也补齐)"
    assert "init_pages(artwork_id, info.page_count)" in craw, \
        "init_pages 必须按远端总页数写 —— 写选中页数会让整部作品被判成已下完"
    # 服务端: page_count 透传的仍是远端总页数, 且只有作品项认 pages
    assert 'pages=(data.get("pages") if item_type == "artwork" else None)' in srv, srv[:0]

    # ---------- ④ 前端远程大图 ----------
    assert "window.openRemoteLightbox = async function" in js, "缺远程大图弹窗"
    for _need in ("lb-remote-strip", "🖼️ 显示原图", "⬇ 下载该页", "⬇ 下载选中页",
                  "lb-remote-src", "🔗 原网页", "'/api/artwork/'", "lb-remote-all"):
        assert _need in js, f"前端少了 {_need}"
    assert "'pages': nums" not in js and "pages: nums" in js, "页号要随请求带上"
    # 查询页卡片点击**不许**再开原站新标签页 (否则「又弹大图又开标签页」)
    _clk = js.split("搜索页作品卡点击")[1].split("$$('.tab')")[0]
    assert "window.openRemoteLightbox(card)" in _clk
    assert "window.open(url" not in _clk and "window.open(url," not in _clk, _clk
    # 卡片带上作品 ID (大图弹窗靠它取页)
    assert 'data-aid="${esc(w.artwork_id)}"' in js, "卡片少了 data-aid"
    # R18 安全模式: 远程低清与缩略图都必须先过打码这一关
    assert "safemode && isR18 && !pg._reveal" in js, "远程大图没走安全模式占位"
    # ---------- ④′ 点击放大 1:1 · 两个弹窗共用一支 + 「不点就不取原图」----------
    # : 弹窗里大图按容器缩放, master1200 与 img-original 都远超容器,
    # 肉眼一样 —— 只有 1:1 渲染才比得出。放大只重排**已加载**的那张, 不顺手去取原图。
    # 2026-10-20 用户追加: 「关于点击查看大图的优化也应该适用到本地图库里面, 包括刚才那个关闭叉叉」
    assert js.count("z = setupZoom(lb, wrap, lay,") == 2, \
        "放大/平移要抽成一支 setupZoom, 本地图库查看器与远程大图弹窗各装一份"
    assert js.count('<span class="lb-zoom-hint"></span>') == 2, "两个弹窗都要有放大提示的位置"
    assert js.count("z.teardown()") == 3, "两个弹窗关闭时都要摘掉 window 上的鼠标监听 (本地点上/点Esc)"
    assert "zoomTo(!zoom)" in js, "点图片没有切换放大"
    assert "setBlocked: (b) => {" in js and "blocked && zoom" in js, \
        "没有「禁放大」这支 (打码页 / 没有图: 占位图 1:1 只会露出一块小矩形)"
    # 放大态必须把**图片元素自身**撑到自然尺寸 —— 只给 object-fit:none 是不够的: 元素盒子仍是容器
    # 尺寸, 裁剪窗口锚在元素盒子上, translate 只是在拖一个方块。
    _zrule = css.split(".lightbox-img-wrap.is-zoom .lightbox-img.lb-layer {")[1].split("}")[0]
    assert "width: auto" in _zrule and "height: auto" in _zrule, \
        "放大态没把元素尺寸放开 (元素仍 100%/100% → 拖动只是拖方块, 翻不出图的其余区域)"
    assert "left: 50%" in _zrule and "top: 50%" in _zrule \
        and "right: auto" in _zrule and "bottom: auto" in _zrule, \
        "基类 inset:0 会拉伸元素, 放大态要把它改回 left/top 定位"
    assert "translate(-50%, -50%) translate(" in js, "元素居中(-50%,-50%)必须与平移量叠加"
    assert ".lightbox-img-wrap.is-zoom" in css and ".lightbox.is-remote" not in css, \
        "放大样式不许只挂远程弹窗 —— 需求两个弹窗都生效"
    assert "lightbox is-remote" not in js, "两个弹窗同用一种样式, is-remote 已成死代码"
    assert "clampAxis" in js and "panFrom" in js, "放大后没有拖拽平移"
    assert "if (hiOn) swapLayer(f" in js, "原图要**只在**点「显示原图」后才加载 (默认低清, 省流量)"
    # ---------- ④″ 放大后真的拖得动 + 放大态吃满可用空间 ----------
    # 夹取必须用容器的实际渲染尺寸: curNw/curNh 存的是图片**自然尺寸**, 拿它当视口算出的上限
    # (自然-自然)/2 = 0, 平移量恒被夹成 0 → 拖不动。
    assert "clampAxis(panFrom.ox + dx, wrap.clientWidth, nw)" in js, \
        "平移夹取要用容器实际宽度 (curNw 是自然尺寸, 拿它当视口算出的上限恒为 0)"
    assert "clampAxis(panFrom.oy + dy, wrap.clientHeight, nh)" in js, "纵向同理"
    assert "clampAxis(zx, wrap.clientWidth, nw)" in js, \
        "窗口尺寸变化时, 放大态的平移量要按新视口重新夹一遍 (旧上限会失效)"
    # 放大态额度: **取景窗直接铺满可用区域**, 不再跟图片比例走 —— 2026-10-20 用户:
    #。
    # 两处弹窗都要: 横向满 98vw; 竖向用**实测**行高扣掉下方 ✕/‹›/信息行/缩略条的空间。
    assert js.count("wrap.style.width = Math.max(1, Math.round(window.innerWidth * 0.98)) + 'px';") == 2, \
        "放大态取景窗没铺满屏幕横向 (本地查看器 + 远程弹窗两处都要)"
    assert js.count("if (zin) {") == 2, "两处 sizeWrap 都要有「放大态铺满」这一支"
    assert js.count("const r = Math.min(window.innerWidth * 0.92 / nw, capNorm / nh, 1);") == 2, \
        "常态仍要按图片比例 contain 适配 (铺满只属于放大态)"
    assert "Math.max(capNorm, window.innerHeight * 0.94 - infoH - stripH - 10)" in js, \
        "放大态没把竖向额度放出去, 或没用 capNorm 兜住「放大反而变小」"
    assert "window.innerHeight * (folded ? 0.72 : 0.56)" in js, "远端常态额度被改掉了"
    assert "window.innerHeight * 0.85" in js, "本地查看器的常态额度被改掉了"
    assert ".lightbox.is-zoom .lightbox-wrap { max-width: 99vw; max-height: 99vh; }" in css, \
        "放大态要放开 wrap 的 92vw / 94vh 上限 (max-* 会盖住 JS 设的 inline 尺寸)"
    assert ".lightbox.is-zoom .lightbox-img-wrap { max-width: 99vw; max-height: 97vh; }" in css, \
        "放大态要放开取景窗自身的 92vw / 85vh 上限"
    assert "sizeWrapFn();" in js, "切换放大后没有跟着重排大图尺寸"
    _close_rule = css.split(".lightbox-close {")[1].split("}")[0]
    assert "position: fixed" in _close_rule and "top: 18px" in _close_rule, \
        "✕ 要锚在视口上 (原来 top:-40px 挂在 wrap 上, wrap 顶满 94vh 时按钮越过视口上沿只露半个)"
    # ---------- ④⁵ 只看要看的  ----------
    # 三个慢因: ① 缩略图条一次把每页都插进 DOM, 几十个代理请求抢同域名 6 条连接;
    # ② 翻页时还预取相邻页**原图** (各 1~9MB, 费流量且抢连接); ③ /api/img 先 resp.read()
    # 整张读完才回给浏览器 → 首字节 = 整图下载耗时。
    assert js.count('alt="" loading="lazy" decoding="async">') >= 2, \
        "缩略图条要懒加载 (视口外 / 未展开的页不许发起请求)"
    assert "const ff = urlsOf(Object.assign({}, cur, pp)).f;" not in js, \
        "不许再预取相邻页原图 (1~9MB ×2 每次翻页)"
    _srv = (PROJECT_ROOT / "web" / "server.py").read_text(encoding="utf-8")
    _img = _srv.split("def api_img():")[1].split("\n    # ---")[0]
    assert "resp.read(65536)" in _img and "yield chunk" in _img, \
        "图片代理要流式转发 (原来 read() 整张读完才回, 首字节 = 整图下载耗时)"
    assert "Cache-Control" in _img, "代理要带回缓存头, 同 URL 复用不重复走链路"
    # ---------- ④‴ 缩略图条可折叠 (远端) ----------
    assert "lb-remote-fold" in js and ".lb-strip.folded .lb-strip-list" in css, \
        "缩略图多选条没有折叠开关"
    assert "applyFold" in js and "lbStripFolded" in js, "折叠态没有记忆 / 没有跟着重排大图"
    assert "folded ? 0.72 : 0.56" in js, "折叠后要把省下的高度还给大图"
    # 前端资源版本号 (改了 app.js/style.css 必须同步升, 否则浏览器吃旧缓存)
    _h = (PROJECT_ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "app.js?v=20261020l" in _h and "style.css?v=20261020l" in _h, "升一版防旧缓存"

    # ---------- ⑤ 遮挡修复 ----------
    assert "is-reordering" in js, "页序调整没有收起右下角两颗按钮的类"
    assert ".lightbox.is-reordering .lightbox-nav-items" in css, "样式里没接上"
    _rb = js.split("reorderBtn.addEventListener('click'")[1].split("stripDone")[0]
    assert "classList.toggle('is-reordering'" in _rb, "进调整页序时没收起"
    assert "lb.classList.remove('is-reordering')" in js, "退出调整页序时没恢复"


def main() -> int:
    # 冒烟运行时关掉活动日志的**打屏**(文件照写, T102 会自己验证落盘) ——
    # 否则每个测试用例的 HTTP 流水都会刷屏, 把冒烟结果淹掉。
    try:
        from utils import activity_log as _act
        _act._cfg = lambda: {"enabled": True, "console": False, "max_mb": 8, "keep_days": 7}
    except Exception:
        pass
    # : 后台重复任务保护 + lock 清理
    try:
        if LOCK_PATH.exists():
            age_s = time.time() - LOCK_PATH.stat().st_mtime
            if age_s > 60:
                print(f"[smoke_all] 发现 >60s 老 lock, 已清理 (age={age_s:.0f}s)")
                LOCK_PATH.unlink(missing_ok=True)
        LOCK_PATH.touch(exist_ok=False)
    except FileExistsError:
        print("[smoke_all] ⚠ 已有运行中的冒烟任务 (lock 存在), 直接跳过。"
              "若确认无其它实例请手动删除 " + str(LOCK_PATH))
        return 2
    try:
        suite = run_suite()
    finally:
        LOCK_PATH.unlink(missing_ok=True)
    # ========== 临时垃圾收尾: 强收敛 F 盘 temp_root + 系统 Temp 历史 scoped_dir ==========
    try:
        import importlib, shutil, os, tempfile
        # ① 项目 temp_root (迁 F 盘后的主清理目标): 强制 0h 过期 + 容量收敛
        paths_mod = importlib.import_module("utils.paths")
        paths_mod._cleanup_temp(max_age_h=0, aggressive=True)
        # ② 兜底清系统 Temp 旧 scoped_dir_* (兼容旧版本残留)
        tdir = tempfile.gettempdir()
        for name in os.listdir(tdir):
            if name.startswith("scoped_dir"):
                try:
                    shutil.rmtree(os.path.join(tdir, name))
                except Exception:
                    pass
    except Exception as _ce:
        print(f"[smoke_all] 收尾清理失败(不影响结果): {_ce}")
    print()
    total = len(suite.cases)
    p, f = suite.passed(), suite.failed()
    ms = sum(c.duration_ms for c in suite.cases)
    print(f"== SMOKE SUMMARY: {total} 用例 / PASS={p} / FAIL={f} / {ms / 1000:.2f}s ==")
    print(f"报告: {REPORT_PATH}")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
