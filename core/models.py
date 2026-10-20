# core/models.py
"""
统一数据模型 (体系化设计先于零散代码)
所有 core/ 模块的交互都基于这 4 个 dataclass, 不再裸 dict 互传,
保证 crawler / reverse_search / download_engine / db 之间的字段一致。
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# =============================================================
# 枚举
# =============================================================
class IllustType(int, enum.Enum):
    ILLUSTRATION = 0   # 单幅插画/组图
    MANGA = 1          # 多页漫画
    UGOIRA = 2         # 动图/zip 动图


class DownloadStrategy(str, enum.Enum):
    ONLY_NEW = 'only_new'    # 仅下载本地没有的页 (默认; 实际执行=页级"已存在跳过", 部分下载作品重下时只补缺失页)
    OVERWRITE = 'overwrite'  # 危险: 强制重下原图覆盖本地同名文件 (任务页下拉可选, 前端需弹窗确认后才执行)


class TaskStatus(str, enum.Enum):
    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'


# =============================================================
# 核心实体
# =============================================================
@dataclass(slots=True)
class AuthorInfo:
    author_id: str
    name: str
    avatar_url: Optional[str] = None
    followers: int = 0
    following: int = 0
    twitter: Optional[str] = None
    bio: Optional[str] = None
    pixiv_url: Optional[str] = None
    # 名下作品总数: 作者搜索结果「共 N 件 · 换一批」用; 未知时为 None
    works_total: Optional[int] = None

    def display_name(self) -> str:
        return f"{self.name}_{self.author_id}"

@dataclass(slots=True)
class ArtworkInfo:
    artwork_id: str
    author_id: str
    title: str
    illust_type: IllustType = IllustType.ILLUSTRATION
    page_count: int = 1
    tags: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)  # 每页原图直链
    publish_date: Optional[str] = None
    bookmark_count: int = 0
    is_r18: bool = False
    # pixiv 官方 AI 判定 (ajax 的 aiType): 0=未知/未标记, 1=非 AI, 2=AI
    # (图库能过滤 AI 作品, 走 pixiv 权威字段而非猜标签)
    ai_type: int = 0
    author_name: Optional[str] = None  # P4c: ajax 作品接口直接带的作者名
    # P4a 渲染还原: SearchPage.apply_result() 按 author_id 匹配后会把 AuthorInfo 引用
    # 挂到 artwork.author, 让 ArtworkCard 显示「👤 作者名字」跳转按钮。
    # 以前裸 dict 可以直接 setattr; 现在 @dataclass(slots=True) 必须显式声明 slot。
    # init=False / repr=False / compare=False 避免污染构造签名 / repr / 比较。
    author: "Optional[AuthorInfo]" = field(
        default=None, init=False, repr=False, compare=False
    )

    # ---------- helpers ----------
    @property
    def author_member_url(self) -> str:
        return f"https://www.pixiv.net/users/{self.author_id}"

    @property
    def artwork_url(self) -> str:
        return f"https://www.pixiv.net/artworks/{self.artwork_id}"


@dataclass(slots=True)
class ReverseSearchResult:
    input_image: Path
    confidence: float                # 0.0 ~ 1.0, 搜索源给的相似度
    search_source: str               # "SauceNAO" / "TraceMoe" / "ASCII2D" / "manual"
    hit_author: Optional[AuthorInfo] = None
    hit_artwork: Optional[ArtworkInfo] = None
    hit_page_num: Optional[int] = None     # 命中作品的第几页, 组图/漫画用
    manually_corrected: bool = False       # 是否用户【修正ID】过
    raw_response: Optional[dict] = None    # 原搜索响应 JSON, 留作 debug

    @property
    def status_label(self) -> str:
        tag = 'OK' if self.confidence >= 0.85 else ('⚠ 低置信度' if self.confidence >= 0.55 else '❌ 失败')
        if self.manually_corrected:
            tag += ' ✓人工修正'
        return f"{int(self.confidence*100)}% {tag}"


@dataclass(slots=True)
class DownloadTask:
    """🛒 购物车 / 下载队列 中的 1 项。"""
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # 任务内容: 三种互斥 (二选一填)
    author: Optional[AuthorInfo] = None       # 类型 = 'author_all': 抓该作者全部作品
    artwork: Optional[ArtworkInfo] = None     # 类型 = 'artwork': 单幅作品
    reverse_sourced: Optional[ReverseSearchResult] = None  # 类型 = 'reverse_sourced'
    # 执行策略
    strategy: DownloadStrategy = DownloadStrategy.ONLY_NEW
    status: TaskStatus = TaskStatus.PENDING
    # UI 展示 / 备注
    added_at: str = field(default_factory=lambda: __import__('datetime').datetime.now().isoformat(timespec='seconds'))
    source_note: str = ''

    # ---------- helpers ----------
    @property
    def item_type(self) -> str:
        if self.author is not None:
            return 'author_all'
        if self.artwork is not None:
            return 'artwork'
        return 'reverse_sourced'

    def display_title(self) -> str:
        if self.author:
            n = self.author.display_name()
            return f"作者全部作品: {n}"
        if self.artwork:
            return f"#{self.artwork.artwork_id} {self.artwork.title} ({self.artwork.page_count}P)"
        if self.reverse_sourced and self.reverse_sourced.hit_artwork:
            a = self.reverse_sourced.hit_artwork
            return f"反搜:{self.reverse_sourced.input_image.name}→#{a.artwork_id} {a.title}"
        return f"反搜(未命中):{self.reverse_sourced.input_image.name if self.reverse_sourced else '未知项'}"
