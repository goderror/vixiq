# core/download_engine.py
"""
P3 拆分: 下载执行引擎。消费 🛒 购物车 download_queue_items 表中的 pending 项。

设计原则: 不重写下载逻辑, 只做「队列 → PixivDownloader 能力」的薄调度。
  - author_all: 原 crawler.download_author_all()
  - artwork   : 原 crawler.download_artwork(artwork_id) + 循环多个作品
  - reverse_sourced: 走 reverse_source ReverseSource 单张补齐 (同 on_reverse_source 语义)
进度统一: reporter.progress(cur, total, status) 上到主线程 StatusBar。
取消: 两条路, 都是「每步检查」——
  - 全局「■ 停止任务」→ reporter.is_cancelled() (项与项之间)
  - 用户清空队列里这一条 → 每项开跑前认领闸门 + 逐页回查 (见 _make_abort, 2026-09-20)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from utils.reporter import Reporter
from utils.config import load_config
from utils import paths

from db.store import PixivDatabase

from core.crawler import PixivDownloader, DownloadCancelled
from core.reverse_source import ReverseSource
from core.models import DownloadStrategy


def run_cart(
    reporter: Reporter,
    *,
    config: Optional[dict] = None,
    save_folder: Optional[Path] = None,
    strategy: DownloadStrategy = DownloadStrategy.ONLY_NEW,
    headless: Optional[bool] = None,
    only_ids: Optional[list[int]] = None,
) -> tuple[int, int]:
    """
    执行购物车全部 pending 任务。
    only_ids: 若给定, 只执行这些 id 且 status=pending 的任务 (单条重试/重试失败用)。
    返回 (成功任务数, 总任务数)。
    """
    config = config or load_config()
    # 全程持有同一个活连接: 之前用 with 读完 items 就 close, 再把已关闭的 db 传进
    # _run_item → artwork 项 get_work_info 必炸 → 购物车只有 author_all 成功。经验记此。
    db = PixivDatabase(reporter=reporter)
    try:
        items = [it for it in db.queue_list(status='pending')]
        if only_ids:
            ids = set(int(x) for x in only_ids)
            items = [it for it in items if int(it.get('id') or 0) in ids]
        if not items:
            reporter.warn("指定任务已处理完或不在待下载队列中。")
            return 0, 0

        total = len(items)
        reporter.log(f"🛒 开始执行购物车中的 {total} 项任务 (策略={strategy.value})...")

        def _task_progress(cur: int, status: str) -> None:
            """总进度条: **只按「第几个任务 / 共几个任务」推进**。

            「进度条像摆设」: 爬虫每下一页也会 reporter.progress
            (那个 total 是**作品的页数**), 和这里的任务条数口径互相覆盖 —— 批量下载时
            第一个作品下完就把条子冲到 100% 并自动隐藏, 下一个任务开始又跳回 33%,
            看着就像没在跟进度。现改为: 页级进度只走 item_progress 喂行内那一格,
            总进度条认这个 cart_progress 事件 (前端 handleEvent 只认它)。
            """
            reporter.progress(cur, total, status=status)
            try: reporter.put("cart_progress",
                              {"cur": cur, "total": total, "status": status})
            except Exception: pass

        done = 0
        # 构造全局 PixivDownloader + ReverseSource (Selenium driver 复用, 不每项开一次浏览器)
        pcfg = config["paths"]
        sf = Path(save_folder) if save_folder else paths.user_data(pcfg["img_subdir"])
        crawler_kwargs = dict(
            save_folder=sf,
            headless=headless,
            reporter=reporter,
            config=config,
        )
        # 我们只用到 download_artwork + download_author_all 两种方法, 复用 PixivDownloader 内部登录/解析/下载
        with PixivDownloader(**crawler_kwargs) as d:
            for i, it in enumerate(items, 1):
                if reporter.is_cancelled():
                    reporter.warn(f"用户取消, 剩余 {total - i + 1} 项未处理。")
                    break
                _task_progress(i - 1,
                    f"[{i}/{total}] 开始: {it.get('title') or it.get('item_type','')[:20]}")
                item_id = int(it.get('id') or 0)
                # 「认领」= 每项开跑前的闸门: 引擎手里这份 items 是开跑时的
                # 快照, 用户清空队列改不到它 —— 所以每项都先回查一次:
                # 行还在且仍是 pending → 认领成功, 照跑; 行已被清空/已被别人改过 →
                # 命中 0 行 → 直接跳过。**这就是「清空 = 撤销该次操作」的兑现点。**
                claimed = 1
                with PixivDatabase(None) as _db0:
                    try: claimed = _db0.queue_claim_item(item_id)
                    except Exception: pass
                if not claimed:
                    reporter.warn(f"⏭ 任务 #{item_id} 已被清空, 跳过")
                    continue
                # 逐页回查 (见 _make_abort): 跑到一半被清空 → 当前这一项立刻中止
                d.set_abort_callback(_make_abort(db, item_id))
                # P4b: PixivDownloader 的 scope 级回调 → item_progress(item_id, cur, total, status)
                # scope 映射:
                #  - artwork (单作品 item) → artwork_page (页数)
                #  - author_all        → artwork_in_list (作者收集到的作品数)
                #  - reverse_sourced   → 手动 0/1 (单张补齐)
                def _make_cb(p_item_id: int, itype_: str):
                    def _cb(scope: str, cur: int, total_: int, status: str):
                        # 只接受对应 scope, 防止 author_all 的子作品页进度和外层混起来
                        if itype_ == 'artwork' and scope != 'artwork_page':
                            return
                        if itype_ == 'author_all' and scope != 'artwork_in_list':
                            return
                        try:
                            reporter.item_progress(p_item_id, cur, total_, status)
                        except Exception:
                            pass
                    return _cb
                d.set_progress_callback(_make_cb(item_id, it.get('item_type') or ''))
                try:
                    _run_item(db, d, it, reporter, strategy)
                except DownloadCancelled:
                    # 用户在跑到一半清空了这一条 (或按了「停止任务」): 已下好的页保留,
                    # 图库里如实显示成「缺页」, 随时能补。
                    reporter.warn(f"⏹ 任务 #{item_id} 已中止")
                    # 放回 pending: 清空导致的命中 0 行 (行没了, 正是我们要的);
                    # 「停止任务」导致的把行放回队列, 免得它永远卡在「进行中」。
                    with PixivDatabase(None) as db2:
                        try: db2.queue_release_item(item_id)
                        except Exception: pass
                    try: reporter.item_progress(item_id, 0, 1, "已中止")
                    except Exception: pass
                    d.set_abort_callback(None)
                    d.set_progress_callback(None)
                    continue
                except Exception as e:
                    reporter.error(f"任务失败 #{it.get('id')}: {str(e).splitlines()[0]}")
                    with PixivDatabase(None) as db2:
                        try: db2.queue_finish_item(item_id, 'failed')
                        except Exception: pass
                    # P4b: 失败也推一次 item_progress(100%) —— 让行快速进入最终态
                    try: reporter.item_progress(item_id, 1, 1, "失败")
                    except Exception: pass
                    d.set_progress_callback(None)
                    continue
                with PixivDatabase(None) as db2:
                    try: db2.queue_finish_item(item_id, 'done')
                    except Exception: pass
                # P4b: 完成推 100%, 再清回调 (下一个 item 会重置)
                try: reporter.item_progress(item_id, 1, 1, "完成")
                except Exception: pass
                d.set_progress_callback(None)
                done += 1
                _task_progress(i,
                    f"[{i}/{total}] 完成: {it.get('title') or it.get('item_type','')[:20]}")
                # 每项之间缓一下
                time.sleep(0.25)
    finally:
        db.close()

    reporter.log(f"🛒 购物车执行完成 {done}/{total} 项。")
    return done, total


def _make_abort(db: PixivDatabase, item_id: int):
    """构造「这条任务是否已被用户清空」的回调, 注册给 PixivDownloader **逐页**回查。

    走引擎全程持有的那条 db 连接 (不是每页新开): 引擎线程顺序调用, 没有并发问题。
    行被清掉 / 状态被别人改过 → 返回 True → 当前作品立刻中止。
    """
    def _cb() -> bool:
        return not db.queue_item_alive(item_id)
    return _cb


def _only_pages_of(it: dict) -> "set | None":
    """队列项 pages 列 → 只下哪几页的集合; NULL/空/坏数据一律当「整部」(返回 None)。

    按页下载: pages 存 JSON 数组字符串 ("[0, 3]")。解析失败不报错而是退回
    整部 —— 队列项是用户能看见的历史数据, 一条脏值不该让整个任务失败。"""
    raw = it.get('pages')
    if not raw:
        return None
    try:
        lst = json.loads(raw)
    except Exception:
        return None
    if not isinstance(lst, list):
        return None
    out = set()
    for x in lst:
        try:
            n = int(x)
            if n >= 0:
                out.add(n)
        except (TypeError, ValueError):
            continue
    return out or None


def _run_item(db: PixivDatabase, d: PixivDownloader, it: dict,
              reporter: Reporter, strategy: DownloadStrategy):
    itype = it.get('item_type') or ''
    item_id = int(it.get('id') or 0)

    if itype == 'author_all':
        author_id = it.get('author_id')
        if not author_id:
            raise ValueError(f"author_all 项缺少 author_id: id={item_id}")
        reporter.log(f"👤 开始下载作者主页全部作品 (uid={author_id})")
        d.download_author_all(user_id=str(author_id), author_name=it.get('author_name'))

    elif itype == 'artwork':
        aid = it.get('artwork_id')
        if not aid:
            raise ValueError(f"artwork 项缺少 artwork_id: id={item_id}")
        # 非数字 ID (ext_/merged_/其他来源): 不是 pixiv 作品, 走 pixiv 必 404/卡浏览器 ——
        # 不进浏览器直接判失败 (2026-09-10: 曾因 ext_ 任务进 crawler 打开 pixiv 卡在
        # "进行中"无反应)。数字 ID 的 pixiv 作品才走下载。
        if not str(aid).isdigit():
            raise ValueError(f"作品 {aid} 非 pixiv 数字 ID, 无法从原站下载")
        page_count = int(it.get('page_count') or 1)
        # 按页下载: pages 列 = JSON 数组字符串,
        # NULL/空 = 整部作品 (老口径, 一字不改)。
        only = _only_pages_of(it)
        # 仅下载本地没有的: 策略先在 db 里跳过的判断。
        # ⚠ 页级任务**必须绕过**这段: 整部已入库的作品点「补某几页」时, 这条件成立会
        # 被静默跳过 —— 用户点了下载却什么都没发生。
        if only is None and strategy == DownloadStrategy.ONLY_NEW and page_count > 0:
            info = db.get_work_info(str(aid))
            # info.status 是 int (WSt.DONE=2), 不能用 .value 比较 (PSt/WSt 是 int 常量类)
            if info and info.done and info.total_pages \
                    and int(info.total_pages) == page_count:
                reporter.progress(0, 1, f"作品 {aid} 已全部下载, 跳过")
                try: reporter.item_progress(item_id, 1, 1, "已下过")
                except Exception: pass
                return
        if only is not None:
            reporter.log(f"⬇ 按页下载作品 {aid}: 共 {len(only)} 页 "
                         f"({', '.join('p' + str(p) for p in sorted(only))})")
        # 作者信息随队列项透传, 下载器直接建「作者名_ID」文件夹, 不再依赖爬 DOM
        # 按页下载: pages 只在页级任务才带上 —— 整部的老签名一字不改
        # (外部的打桩/子类 download_artwork 未必收 pages 形参, 多传一个关键字会炸)。
        kw = dict(author_id=it.get('author_id'),
                  author_name=it.get('author_name'),
                  force_overwrite=(strategy == DownloadStrategy.OVERWRITE))
        if only is not None:
            kw['pages'] = only
        done_cnt, total_cnt = d.download_artwork(str(aid), **kw)
        # 静默「成功」是「点了下载、任务显示完成、图库里却没有」的根因:
        # 未抓到图片时 download_artwork 只 warn 然后 return (0, 0), 不抛异常, 而本函数
        # 只在抛异常时才把队列项标 failed → 任务显示 done, 用户反复点击(实测 109766089
        # 连点 3 次, 队列留下 3 条 done 却一张图都没下)。
        if total_cnt == 0:
            raise ValueError("未检测到任何图片链接 (登录可能已失效, 或作品受限/已删除)")
        if done_cnt == 0:
            raise ValueError(f"该作品 {total_cnt} 页全部下载失败")

    elif itype == 'reverse_sourced':
        # 反搜补齐: 需要 target_dir 与原反搜流程一致
        # 从 source_note 解析 input 路径 JSON (加清单时写入的)
        try: reporter.item_progress(item_id, 0, 1, "补齐中")
        except Exception: pass
        src = it.get('source_note') or ''
        inp = None
        if src.startswith('{'):
            try:
                data = json.loads(src)
                inp = data.get('input_path')
            except Exception:
                pass
        if not inp:
            raise ValueError("reverse_sourced 项缺少 input_path 元数据 (source_note JSON)")
        pcfg = d.config["paths"]
        target = Path(d.save_folder)
        reporter.log(f"🔗 反搜补齐:{Path(inp).name} → {it.get('artwork_id')}")
        # ReverseSource 会自己处理 Selenium, 这里只调单张
        with ReverseSource(target_dir=str(target), headless=True,
                           reporter=reporter, config=d.config) as rs:
            rs.process_one(Path(inp),
                           force_artwork_id=str(it.get('artwork_id')) if it.get('artwork_id') else None,
                           force_author_id=str(it.get('author_id')) if it.get('author_id') else None)
        try: reporter.item_progress(item_id, 1, 1, "完成")
        except Exception: pass

    else:
        raise ValueError(f"未知 item_type={itype!r}, 无法下载")
