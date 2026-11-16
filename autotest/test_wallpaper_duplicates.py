"""同图判重回归：使用临时图库和合成图，不读取真实图片或设置桌面。"""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from PIL import Image
from db.store import PixivDatabase, WSt, PSt
from core.wallpaper_index import group_dupes, matching_wall_files, scan_wall_dir, upsert_wall_file
import core.wallpaper_trace as trace
import utils.paths as paths
from utils import activity_log
import web.server as server


class WallpaperFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='wall-image-test-'))).resolve()
        self.wall = self.root / 'wall'; self.wall.mkdir()
        self.img = self.root / 'img'; self.img.mkdir()
        self.db_file = self.root / 'pixiv.db'
        self.enterContext(patch.object(paths, 'user_data', lambda rel: self.root / str(rel)))
        self.enterContext(patch.object(paths, 'data_root', lambda: self.root))
        self.enterContext(patch.object(server, '_db', self.db))
        self.enterContext(patch.object(server, '_wallpaper_file', lambda: None))
        self.enterContext(patch.object(server, 'candidates_dir', lambda *a: self.wall))
        self.enterContext(patch.object(trace, 'wallpaper_dir', lambda: self.wall))
        self.enterContext(patch.object(activity_log, 'start', lambda: None))
        self.enterContext(patch.object(activity_log, 'log', lambda *a, **k: None))
        self.apply = self.enterContext(patch.object(server, 'apply_wallpaper', return_value=True))
        with self.db() as db:
            db.upsert_author('555', '测试作者')
            for aid in ('700001', '1700001'):
                db.change_work_status(aid, WSt.DONE, title='测试作品', author_id='555', total_pages=11)
                for pn in (0, 1, 10):
                    name = f'{aid}_p{pn}.png'
                    Image.new('RGB', (320, 240), (int(aid) % 255, pn * 20, 90)).save(self.img / name)
                    db.mark_page_status(aid, pn, PSt.DONE, local_path=name,
                                        md5=hashlib.md5((self.img / name).read_bytes()).hexdigest(),
                                        width=320, height=240)
        self.client = server.create_app().test_client()

    def db(self):
        return PixivDatabase(self.db_file)

    def add_wall(self, name, page=0, aid='700001', indexed=True):
        f = self.wall / name
        Image.new('RGB', (100, 200), (15, 32, 91)).save(f)
        if indexed:
            with self.db() as db:
                upsert_wall_file(db, self.wall, f, aid, page, 'visual', 0.9)
        return f


class WallpaperDuplicateTests(WallpaperFixture):
    def test_different_images_in_same_work_are_not_duplicates(self):
        rows = [{'name': str(p), 'artwork_id': '700001', 'page_num': p,
                 'md5': str(p), 'author_name': '同作者', 'source': 'pixiv'} for p in (0, 1, 10)]
        self.assertEqual(group_dupes(rows), [])
        self.assertEqual(group_dupes([dict(r, page_num=None) for r in rows]), [])

    def test_cropped_versions_group_by_exact_image(self):
        rows = [{'name': 'original', 'artwork_id': '700001', 'page_num': 0, 'md5': 'a'},
                {'name': 'crop', 'artwork_id': '700001', 'page_num': 0, 'md5': 'b'},
                {'name': 'different', 'artwork_id': '700001', 'page_num': 1, 'md5': 'c'}]
        groups = group_dupes(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]['files']), {'original', 'crop'})
        self.assertEqual(groups[0]['page_num'], 0)

    def test_content_copy_and_crops_form_one_complete_group(self):
        rows = [{'name': 'a', 'artwork_id': '700001', 'page_num': 0, 'md5': 'same'},
                {'name': 'a-crop', 'artwork_id': '700001', 'page_num': 0, 'md5': 'crop-a'},
                {'name': 'b', 'artwork_id': '1700001', 'page_num': 1, 'md5': 'same'},
                {'name': 'b-crop', 'artwork_id': '1700001', 'page_num': 1, 'md5': 'crop-b'},
                {'name': 'other', 'artwork_id': '700001', 'page_num': 10, 'md5': 'other'}]
        groups = group_dupes(rows)
        self.assertEqual([set(g['files']) for g in groups], [{'a', 'a-crop', 'b', 'b-crop'}])
        self.assertEqual({r['name'] for r in matching_wall_files(rows, '700001', 0)},
                         {'a', 'a-crop', 'b', 'b-crop'})

    def test_work_only_and_empty_hash_are_not_evidence(self):
        rows = [{'name': 'a', 'artwork_id': '700001', 'page_num': None, 'md5': ''},
                {'name': 'b', 'artwork_id': '700001', 'page_num': None, 'md5': ''}]
        self.assertEqual(group_dupes(rows), [])
        self.assertEqual(matching_wall_files(rows, '700001', None), [])
        self.assertEqual(group_dupes([dict(r, md5='identical') for r in rows])[0]['files'], ['a', 'b'])

    def test_existing_index_and_both_apis_use_exact_image(self):
        self.add_wall('first.png', page=0)
        self.add_wall('first-crop.png', page=0)
        self.add_wall('second.png', page=1)
        # Different visual contents, despite the helper initially using one filler color.
        Image.new('RGB', (110, 220), (99, 88, 77)).save(self.wall / 'second.png')
        with self.db() as db:
            upsert_wall_file(db, self.wall, self.wall / 'second.png', '700001', 1, 'visual', 0.9)
        result = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()
        self.assertEqual({r['name'] for r in result['items']}, {'first.png', 'first-crop.png'})
        listing = self.client.get('/api/wallpaper/files').get_json()
        self.assertEqual([set(g['files']) for g in listing['groups']], [{'first.png', 'first-crop.png'}])
        response = self.client.post('/api/wallpaper/apply', json={'artwork_id': '700001', 'page_num': 10})
        self.assertEqual(response.status_code, 200)
        self.apply.assert_called_once()
        response = self.client.post('/api/wallpaper/apply', json={'artwork_id': '700001', 'page_num': 0})
        self.assertEqual(response.status_code, 409)

    def test_filename_fallback_requires_exact_id_and_page(self):
        for name in ('700001_p1.png', '700001_p10.png', '1700001_p1.png', '700001.png'):
            self.add_wall(name, indexed=False)
        result = server._wall_dupes_for('700001', deep=False, page_num=1)
        self.assertEqual([r['name'] for r in result], ['700001_p1.png'])
        self.assertEqual(server._wall_dupes_for('700001', deep=False), [])

    def test_missing_page_does_not_borrow_another_images_hash(self):
        (self.wall / 'renamed.png').write_bytes((self.img / '700001_p0.png').read_bytes())
        result = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=99').get_json()
        self.assertEqual(result['items'], [])

    def test_visual_fallback_checks_page_and_allows_portrait_crops(self):
        self.add_wall('unknown-crop.png', indexed=False)
        orb = MagicMock(return_value=[{'rel': '700001_p0.png', 'orb': 0.8}])
        with patch.object(server, '_wall_orb_candidates', orb):
            self.assertEqual(server._wall_dupes_for('700001', target_wh=(1920, 1080),
                             page_num=1), [])
            result = server._wall_dupes_for('700001', target_wh=(1920, 1080), page_num=0)
            self.assertEqual([r['name'] for r in result], ['unknown-crop.png'])
        self.assertEqual(orb.call_count, 2)

    def test_deep_scan_completes_legacy_work_only_owner(self):
        self.add_wall('700001_unknown.png', page=None)
        self.add_wall('700001_p1_crop.png', page=1)
        orb = MagicMock(return_value=[{'rel': '700001_p1.png', 'orb': 0.8}])
        result = scan_wall_dir(self.db, self.wall, self.img, orb_fn=orb, deep=True)
        self.assertEqual(result['dupes'], 1)
        self.assertEqual(orb.call_count, 1)
        rows = self.client.get('/api/wallpaper/files').get_json()['items']
        self.assertTrue(all(r['page_num'] == 1 for r in rows))

    def test_duplicate_crop_does_not_overwrite_before_confirmation(self):
        f = self.add_wall('700001_p0_crop.jpg')
        before = f.read_bytes()
        result = self.client.post('/api/wallpaper/apply', json={
            'artwork_id': '700001', 'page_num': 0, 'crop': {'x': 0, 'y': 0, 'w': 1, 'h': 1}})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(f.read_bytes(), before)
        self.apply.assert_not_called()

    def test_old_directory_or_deleted_index_does_not_match(self):
        self.add_wall('old.png')
        with self.db() as db:
            db.conn.execute('UPDATE wallpaper_files SET dir=?', (str(self.root / 'old'),))
            db.conn.commit()
        self.assertEqual(server._wall_dupes_for('700001', deep=False, page_num=0), [])
        (self.wall / 'old.png').unlink()
        self.assertEqual(server._wall_dupes_for('700001', deep=False, page_num=0), [])

    def test_real_orb_identifies_crop_without_merging_different_image(self):
        import cv2
        import numpy as np
        from core.local_search import ImageSearcher
        rng = np.random.default_rng(12345)
        with self.db() as db:
            db.conn.execute('UPDATE pages SET local_path=NULL WHERE artwork_id=?', ('1700001',))
            db.conn.commit()
        for pn in (0, 1, 10):
            im = rng.integers(0, 256, (480, 640, 3), dtype=np.uint8)
            # 固定的合成纹理，不包含用户图像。
            Image.fromarray(im).save(self.img / f'700001_p{pn}.png')
        Image.open(self.img / '700001_p0.png').crop((160, 40, 420, 430)).save(self.wall / 'portrait.png')
        cfg = copy.deepcopy(server._config)
        cfg['local_search']['candidate_pool'] = 2
        searcher = ImageSearcher(str(self.img), config=cfg, reporter=MagicMock())
        def orb(file, root):
            return [{'rel': h['name'], 'orb': h['orb_score']} for h in searcher.search(str(file), top_k=3)]
        scan_wall_dir(self.db, self.wall, self.img, orb_fn=orb, deep=True)
        rows = self.client.get('/api/wallpaper/files').get_json()['items']
        self.assertEqual((rows[0]['artwork_id'], rows[0]['page_num']), ('700001', 0))
        self.assertEqual(rows[0]['match_kind'], 'visual')
        with patch.object(server, '_wall_orb_candidates', side_effect=AssertionError('索引查重不应再跑视觉检索')):
            self.assertEqual(server._wall_dupes_for('700001', page_num=1), [])
            self.assertEqual([r['name'] for r in server._wall_dupes_for('700001', page_num=0)], ['portrait.png'])


if __name__ == '__main__':
    unittest.main()
