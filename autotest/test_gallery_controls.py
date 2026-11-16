"""图库工具栏与统计回归；临时数据库、合成图片，下载与桌面均不真实执行。"""
import json
from pathlib import Path
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from autotest.test_wallpaper_duplicates import WallpaperFixture
from utils import proc
import web.server as server


class GalleryControlsTests(WallpaperFixture):
    def test_counts_cover_all_pages_instead_of_current_page(self):
        for view in ('artwork', 'image'):
            r = self.client.get('/api/library/items?view=' + view + '&limit=1&offset=1').get_json()
            self.assertEqual(len(r['items']), 1)
            self.assertEqual(r['counts'], {'artworks': 2, 'images': 6})
            self.assertEqual(r['total'], 2 if view == 'artwork' else 6)

    def test_counts_follow_work_and_filename_filters(self):
        works = self.client.get('/api/library/items?only=700001&view=artwork').get_json()
        images = self.client.get('/api/library/items?only=700001&view=image').get_json()
        self.assertEqual(works['counts'], {'artworks': 1, 'images': 3})
        self.assertEqual(images['counts'], {'artworks': 1, 'images': 3})
        r = self.client.get('/api/library/items?view=image&q=700001_p10').get_json()
        self.assertEqual(r['counts'], {'artworks': 2, 'images': 2})

    def test_undownloaded_placeholders_are_not_counted_as_images(self):
        with self.db() as db:
            db.conn.execute("INSERT INTO pages(artwork_id,page_num,status) VALUES('700001',9,0)")
            db.conn.commit()
        for view in ('artwork', 'image'):
            r = self.client.get('/api/library/items?view=' + view).get_json()
            self.assertEqual(r['counts'], {'artworks': 2, 'images': 6})
            if view == 'image':
                self.assertNotIn(9, [p['page_num'] for p in r['items']])

    def test_soft_deleted_work_and_page_are_excluded(self):
        with self.db() as db, db.conn:
            db.conn.execute("UPDATE artworks SET trashed_at=datetime('now') WHERE artwork_id='1700001'")
            db.conn.execute("UPDATE pages SET trashed_at=datetime('now') WHERE artwork_id='700001' AND page_num=1")
        for view in ('artwork', 'image'):
            r = self.client.get('/api/library/items?view=' + view).get_json()
            self.assertEqual(r['counts'], {'artworks': 1, 'images': 2})

    def test_tag_join_does_not_double_count_images(self):
        with self.db() as db, db.conn:
            for name in ('共同标签A', '共同标签B'):
                gid = db.conn.execute('INSERT INTO tags(tag_name) VALUES(?)', (name,)).lastrowid
                db.conn.execute("INSERT INTO artwork_tags(artwork_id,tag_id) VALUES('700001',?)", (gid,))
        for view in ('artwork', 'image'):
            r = self.client.get('/api/library/items?q=共同标签&view=' + view).get_json()
            self.assertEqual(r['counts'], {'artworks': 1, 'images': 3})

    def test_empty_result_has_zero_counts(self):
        for view in ('artwork', 'image'):
            r = self.client.get('/api/library/items?q=不存在的作品&view=' + view).get_json()
            self.assertEqual(r['counts'], {'artworks': 0, 'images': 0})

    def test_missing_file_filter_and_counts_match_in_both_views(self):
        for missing, expected in [(['700001'], {'artworks': 1, 'images': 3}), ([], {'artworks': 0, 'images': 0})]:
            with patch.object(server, '_cached_reconcile', return_value=SimpleNamespace(missing_ids=missing)):
                for view in ('artwork', 'image'):
                    r = self.client.get('/api/library/items?file_missing=1&view=' + view).get_json()
                    self.assertEqual(r['counts'], expected)
                    self.assertEqual({x['artwork_id'] for x in r['items']}, set(missing))

    @unittest.skipUnless(shutil.which('node'), '前端行为回归需要 Node.js')
    def test_frontend_selection_and_download_behavior(self):
        result = proc.run(['node', str(Path(__file__).with_name('test_gallery_toolbar.cjs'))], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
