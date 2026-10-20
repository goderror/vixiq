# db/__init__.py
"""数据库存储层: 所有对 pixiv.db 的读写都经由本层导出的 API 完成。

禁止 core/gui 里直接 sqlite3.connect('data/pixiv.db') 写 SQL ——。
"""
from .store import PixivDatabase, PSt, WSt
from .migrator import MigrationManager

__all__ = ['PixivDatabase', 'PSt', 'WSt', 'MigrationManager']
