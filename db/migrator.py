# db/migrator.py
"""
极简 SQLite 迁移器 (Rails/Django migrations 的迷你版)
- 目录: db/migrations/NNN_*.sql, 文件名前缀数字升序执行
- 每个 SQL 文件内部用  "-- UP:"  和  "-- DOWN:"  分节
- 仅执行 UP 节; DOWN 节留作文档
- 已执行版本记入 schema_migrations 表, 下次启动跳过

用法:
    MigrationManager(db_path).ensure_migrations_applied()
    → 返回 list[applied_version_str]
"""
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


_SCHEMA_VER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


@dataclass
class MigrationFile:
    version: str          # '001'
    path: Path            # 绝对路径
    name: str             # 文件名 001_init.sql

    @classmethod
    def from_path(cls, p: Path) -> "MigrationFile":
        m = re.match(r'(\d+)', p.stem)
        if not m:
            raise ValueError(f"Migration file name must start with digits, got: {p.name}")
        return cls(version=m.group(1), path=p, name=p.name)


class MigrationManager:
    def __init__(self, db_path: "str | Path", migrations_dir: "str | Path | None" = None):
        self.db_path = str(db_path)
        if migrations_dir is None:
            migrations_dir = Path(__file__).resolve().parent / 'migrations'
        self.migrations_dir = Path(migrations_dir)

    # ---------- public ----------
    def ensure_migrations_applied(self, conn: "sqlite3.Connection | None" = None) -> list[str]:
        """扫描 + 按顺序执行未完成的迁移。返回本次新执行的版本号列表。

        conn: 可选。传入时复用该连接(解决 :memory: 库"不同连接=不同库"的隔离问题);
              不传则自建连接(与旧版行为一致)。
        """
        own_conn = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA foreign_keys = ON")
            own_conn = True
        try:
            return self._ensure_on(conn)
        finally:
            if own_conn:
                conn.close()

    def _ensure_on(self, conn: sqlite3.Connection) -> list[str]:
        # 先跑"旧 works/pages → 新 artworks/pages"的 Python 侧 bootstrap
        self._bootstrap_legacy(conn=conn)
        applied: list[str] = []
        pending = self._pending_migrations(conn=conn)
        if not pending:
            return applied
        conn.executescript(_SCHEMA_VER_SQL)
        for mf in pending:
            up_sql = self._extract_section(mf.path, 'UP')
            try:
                conn.executescript(up_sql)
                conn.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (mf.version,),
                )
                conn.commit()
                applied.append(mf.version)
            except sqlite3.Error as e:
                conn.rollback()
                raise RuntimeError(
                    f"Migration {mf.name} (v{mf.version}) failed: {e}\n"
                    f"请检查 SQL 语法或手工修复 DB 后重试。"
                ) from e
        return applied

    def _bootstrap_legacy(self, conn: "sqlite3.Connection | None" = None) -> None:
        """
        运行 SQL 迁移前处理「旧库结构 → 新库结构」的兼容性改造,只在需要时执行:
        - 检测旧表名 works 存在 且 表 artworks 不存在 → RENAME TABLE works TO artworks
             + ALTER 补 artworks 扩展列 (title/author_id/tags_json/...)
        - 检测旧 pages 表存在 且 缺新列(md5/width/height/file_size_bytes)  → ALTER 补列
        - 检测有旧 works 表、也有新 artworks(空) → 数据 copy 后删旧 works

        conn: 可选。传入时复用该连接(配合 :memory: 共享连接),不传则自建。
        """
        own_conn = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            own_conn = True
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()}
            has_old_works = 'works' in tables and 'artworks' not in tables
            has_old_pages_simple = (
                'pages' in tables
                and not self._has_column(conn, 'pages', 'md5')
            )

            # A) works → artworks
            if has_old_works:
                # 先改表名
                conn.execute("ALTER TABLE works RENAME TO artworks")
                # 再补 artworks 扩展列
                new_cols = [
                    ("author_id", "TEXT"),
                    ("title", "TEXT"),
                    ("illust_type", "INTEGER DEFAULT 0"),
                    ("tags_json", "TEXT"),
                    ("bookmark_count", "INTEGER DEFAULT 0"),
                    ("publish_date", "TEXT"),
                    ("is_r18", "INTEGER DEFAULT 0 CHECK (is_r18 IN (0,1))"),
                    ("created_at", "TEXT DEFAULT (datetime('now'))"),
                    ("updated_at", "TEXT DEFAULT (datetime('now'))"),
                ]
                for col, typ in new_cols:
                    if not self._has_column(conn, 'artworks', col):
                        conn.execute(f"ALTER TABLE artworks ADD COLUMN {col} {typ}")
                conn.commit()

            # B) pages 补新列 (若 old pages 存在但缺扩展列)
            if has_old_pages_simple:
                for col, typ in [
                    ("md5", "TEXT"), ("width", "INTEGER"),
                    ("height", "INTEGER"), ("file_size_bytes", "INTEGER"),
                ]:
                    if not self._has_column(conn, 'pages', col):
                        conn.execute(f"ALTER TABLE pages ADD COLUMN {col} {typ}")
                conn.commit()

            # C) 兼容视图 works: 如果 artworks 存在但 works 视图不存在, 建视图
            if 'artworks' in tables and 'works' not in tables:
                try:
                    conn.execute(
                        "CREATE VIEW works AS "
                        "SELECT artwork_id, total_pages, exist_list, status, last_check "
                        "FROM artworks"
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass  # 视图重复等无妨
        finally:
            if own_conn:
                conn.close()

    @staticmethod
    def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)

    def current_versions(self, conn: "sqlite3.Connection | None" = None) -> list[str]:
        """返回已应用的版本号列表(升序)。conn 可选, 复用连接则不关闭。"""
        own_conn = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            own_conn = True
        try:
            conn.execute(_SCHEMA_VER_SQL)
            rows = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.Error:
            return []
        finally:
            if own_conn:
                conn.close()

    # ---------- internal ----------
    def _pending_migrations(self, conn: "sqlite3.Connection | None" = None) -> list[MigrationFile]:
        files = sorted(
            (MigrationFile.from_path(p)
             for p in self.migrations_dir.glob('*.sql')),
            key=lambda m: m.version,
        )
        versions_done = set(self.current_versions(conn=conn))
        return [f for f in files if f.version not in versions_done]

    @staticmethod
    def _extract_section(sql_path: Path, section: str) -> str:
        """从 '-- UP:' 到 '-- DOWN:' 之间的 SQL 语句块。"""
        text = sql_path.read_text(encoding='utf-8')
        section_upper = section.upper()
        # 匹配 -- UP: 或 -- DOWN: 及后面的冒号/空格
        markers = {m.group(1).upper(): m.start()
                   for m in re.finditer(r'^--\s*(UP|DOWN)\s*:', text, flags=re.M)}
        start = markers.get(section_upper)
        if start is None:
            return ''
        # 找下一个对立节的起点
        other = 'DOWN' if section_upper == 'UP' else 'UP'
        end = markers.get(other) if other != section_upper else None
        if end is None:
            end = len(text)
        chunk = text[start:end]
        # 去掉首行 marker
        lines = chunk.splitlines()
        if lines and re.match(r'^--\s*(UP|DOWN)\s*:', lines[0], flags=re.I):
            lines = lines[1:]
        return '\n'.join(lines).strip()
