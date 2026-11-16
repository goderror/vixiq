"""壁纸整理/归属/跨盘回收的离线回归，全部使用临时合成图。"""
import hashlib
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from PIL import Image
from autotest.test_wallpaper_duplicates import WallpaperFixture
from core.wallpaper_index import scan_wall_dir
import web.server as server
import web.wallpaper_admin as admin


class WallpaperAdminTests(WallpaperFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(server._WALL_FILES, {'status': 'idle'}))
        self.a = self.add_wall('700001_p0.png')
        self.b = self.add_wall('unowned.png', aid='', page=None)

    def post(self, path, **data):
        r = self.client.post('/api/wallpaper/' + path, json=data)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json().get('ok'), r.get_json())
        return r.get_json()

    def items(self):
        return {f['name']: f for f in self.client.get('/api/wallpaper/files').get_json()['items']}

    def test_batch_owner_updates_old_and_new_counts_without_inventing_image(self):
        self.post('files/edit', names=[self.a.name, self.b.name], artwork_id='1700001')
        rows = self.items()
        self.assertTrue(all(r['artwork_id'] == '1700001' and r['page_num'] is None for r in rows.values()))
        works = self.client.get('/api/wallpaper/works?q=700001').get_json()['items']
        counts = {w['artwork_id']: w['wallpaper_count'] for w in works}
        self.assertEqual(counts, {'700001': 0, '1700001': 2})

    def test_specific_image_assignment_and_invalid_page_is_atomic(self):
        self.post('files/edit', names=[self.a.name], artwork_id='1700001', page_num=10)
        bad = self.client.post('/api/wallpaper/files/edit', json={'names': [self.a.name, self.b.name], 'artwork_id': '700001', 'page_num': 99, 'favorite': True})
        self.assertEqual(bad.status_code, 400)
        row = self.items()[self.a.name]
        self.assertEqual((row['artwork_id'], row['page_num'], row['favorite']), ('1700001', 10, False))

    def test_manual_reassignment_and_unassignment_survive_deep_scan(self):
        self.post('files/edit', names=[self.a.name], artwork_id='1700001')
        self.post('files/edit', names=[self.b.name], artwork_id='')
        orb = MagicMock(return_value=[{'rel': '700001_p0.png', 'orb': 0.99}])
        scan_wall_dir(self.db, self.wall, self.img, orb_fn=orb, deep=True)
        rows = self.items()
        self.assertEqual(rows[self.a.name]['artwork_id'], '1700001')
        self.assertEqual(rows[self.b.name]['artwork_id'], '')
        orb.assert_not_called()

    def test_manual_owner_survives_timestamp_change_but_not_replaced_content(self):
        self.post('files/edit', names=[self.a.name], artwork_id='1700001', page_num=1)
        st = self.a.stat(); os.utime(self.a, (st.st_atime, st.st_mtime + 5))
        scan_wall_dir(self.db, self.wall, self.img, deep=False)
        self.assertEqual(self.items()[self.a.name]['artwork_id'], '1700001')
        Image.new('RGB', (180, 70), 'red').save(self.a)
        scan_wall_dir(self.db, self.wall, self.img, deep=False)
        self.assertEqual(self.items()[self.a.name]['artwork_id'], '700001')

    def test_groups_independent_favorites_labels_and_rescan_persistence(self):
        gid = self.post('groups/save', name='风景')['id']
        self.post('files/edit', names=[self.a.name, self.b.name], group_id=gid, favorite=True, labels=['待整理', '待整理', ' 横图 '])
        scan_wall_dir(self.db, self.wall, self.img, deep=False)
        row = self.items()[self.a.name]
        self.assertEqual((row['artwork_id'], row['group_name'], row['favorite'], row['labels']), ('700001', '风景', True, ['待整理', '横图']))
        self.post('groups/save', id=gid, name='山水')
        self.assertEqual(self.items()[self.a.name]['group_name'], '山水')
        self.post('groups/delete', id=gid)
        row = self.items()[self.a.name]
        self.assertIsNone(row['group_id']); self.assertTrue(row['favorite'])

    def test_group_duplicate_and_invalid_metadata_rejected(self):
        self.post('groups/save', name='风景')
        bad = self.client.post('/api/wallpaper/groups/save', json={'name': '风景'})
        self.assertEqual(bad.status_code, 400)
        for values in ({'group_id': 999}, {'group_id': []}, {'labels': 'a'}, {'favorite': 'true'}, {'artwork_id': 'missing'}):
            with self.subTest(values=values):
                r = self.client.post('/api/wallpaper/files/edit', json={'names': [self.a.name], **values})
                self.assertEqual(r.status_code, 400, r.get_json())

    def test_rename_preserves_content_and_manual_owner(self):
        before = self.a.read_bytes()
        self.post('files/rename', names=[self.a.name], new_name="山景 ' 副本.png")
        new = self.wall / "山景 ' 副本.png"
        self.assertFalse(self.a.exists()); self.assertEqual(new.read_bytes(), before)
        scan_wall_dir(self.db, self.wall, self.img, deep=True)
        self.assertEqual(self.items()[new.name]['artwork_id'], '700001')

    def test_rename_refuses_collision_path_and_extension_change(self):
        before = self.a.read_bytes()
        for name in (self.b.name, '../x.png', 'CON.png', 'x.jpg', 'bad?.png'):
            with self.subTest(name=name):
                r = self.client.post('/api/wallpaper/files/rename', json={'names': [self.a.name], 'new_name': name})
                self.assertEqual(r.status_code, 400)
        self.assertEqual(self.a.read_bytes(), before)

    def test_current_wallpaper_is_protected_from_file_moves_and_disk_recycle(self):
        with patch.object(server, '_wallpaper_file', lambda: self.a), patch.object(server, '_recycle_files') as recycle:
            for path, extra in [('files/rename', {'new_name': 'new.png'}), ('files/trash', {}), ('files/recycle', {})]:
                r = self.client.post('/api/wallpaper/' + path, json={'names': [self.a.name], **extra})
                self.assertEqual(r.status_code, 400, r.get_json())
            recycle.assert_not_called()
        self.assertTrue(self.a.exists())

    def test_trash_restore_roundtrip_keeps_file_and_metadata(self):
        gid = self.post('groups/save', name='风景')['id']
        self.post('files/edit', names=[self.a.name], group_id=gid, favorite=True, labels=['已检查'])
        before = self.a.read_bytes()
        self.post('files/trash', names=[self.a.name])
        self.assertFalse(self.a.exists()); self.assertNotIn(self.a.name, self.items())
        item = self.client.get('/api/wallpaper/trash').get_json()['items'][0]
        self.assertEqual(self.client.get('/api/library/trash/count').get_json()['wallpapers'], 1)
        self.post('trash/restore', ids=[item['id']])
        self.assertEqual(self.a.read_bytes(), before)
        row = self.items()[self.a.name]
        self.assertEqual((row['artwork_id'], row['group_id'], row['favorite'], row['labels']), ('700001', gid, True, ['已检查']))
        self.assertEqual(self.client.get('/api/wallpaper/trash').get_json()['items'], [])

    def test_restore_conflict_never_overwrites_and_keeps_trash(self):
        self.post('files/trash', names=[self.a.name])
        item = self.client.get('/api/wallpaper/trash').get_json()['items'][0]
        self.a.write_bytes(b'occupied')
        r = self.post('trash/restore', ids=[item['id']])
        self.assertEqual(r['done'], 0); self.assertTrue(r['failed'])
        self.assertEqual(self.a.read_bytes(), b'occupied')
        self.assertEqual(len(self.client.get('/api/wallpaper/trash').get_json()['items']), 1)

    def test_restore_deleted_group_clears_only_group(self):
        gid = self.post('groups/save', name='分组')['id']
        self.post('files/edit', names=[self.a.name], group_id=gid, favorite=True)
        self.post('files/trash', names=[self.a.name])
        item = self.client.get('/api/wallpaper/trash').get_json()['items'][0]
        self.post('groups/delete', id=gid)
        self.post('trash/restore', ids=[item['id']])
        row = self.items()[self.a.name]
        self.assertIsNone(row['group_id']); self.assertTrue(row['favorite'])

    def test_purge_failure_keeps_record_and_success_recycles_before_db_delete(self):
        self.post('files/trash', names=[self.a.name])
        item = self.client.get('/api/wallpaper/trash').get_json()['items'][0]
        with patch.object(server, '_recycle_files', return_value=(0, ['failed'])):
            result = self.post('trash/purge', ids=[item['id']])
        self.assertEqual(result['done'], 0)
        with self.db() as db:
            rel = db.conn.execute('SELECT trash_rel FROM wallpaper_trash').fetchone()[0]
        file = self.root / 'trash' / rel
        def recycle(files):
            self.assertTrue(file.exists())
            with self.db() as db:
                self.assertEqual(db.conn.execute('SELECT COUNT(*) FROM wallpaper_trash').fetchone()[0], 1)
            Path(files[0]).unlink()  # 仅删除测试临时文件，模拟系统回收成功
            return 1, []
        with patch.object(server, '_recycle_files', side_effect=recycle):
            result = self.post('trash/purge', ids=[item['id']])
        self.assertEqual(result['done'], 1)
        self.assertEqual(self.client.get('/api/wallpaper/trash').get_json()['items'], [])

    def test_trash_move_failure_keeps_source_and_index(self):
        with patch.object(admin, '_copy_move', side_effect=OSError('模拟磁盘写入失败')):
            r = self.post('files/trash', names=[self.a.name])
        self.assertEqual(r['moved'], 0); self.assertTrue(r['failed'])
        self.assertTrue(self.a.exists()); self.assertIn(self.a.name, self.items())

    def test_database_failure_rolls_back_file_move(self):
        with self.db() as db, db.conn:
            db.conn.executescript("CREATE TRIGGER stop_trash BEFORE INSERT ON wallpaper_trash BEGIN SELECT RAISE(ABORT,'模拟数据库失败'); END;")
        r = self.post('files/trash', names=[self.a.name])
        self.assertEqual(r['moved'], 0); self.assertTrue(self.a.exists())
        self.assertIn(self.a.name, self.items())
        self.assertEqual(list((self.root / 'trash' / 'wallpapers').glob('*')), [])

    def test_scanning_rejects_mutation_without_touching_file(self):
        with patch.dict(server._WALL_FILES, {'status': 'scanning'}):
            r = self.client.post('/api/wallpaper/files/edit', json={'names': [self.a.name], 'favorite': True})
            self.assertEqual(r.status_code, 400)
        self.assertFalse(self.items()[self.a.name]['favorite'])

    def test_invalid_bodies_and_paths_do_not_touch_library(self):
        before = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in self.img.iterdir()}
        for path in ('files/edit', 'files/rename', 'files/trash', 'trash/restore'):
            self.assertEqual(self.client.post('/api/wallpaper/' + path, json=['bad']).status_code, 400)
        for name in ('../img/700001_p0.png', str(self.img / '700001_p0.png'), 'a/../700001_p0.png'):
            r = self.client.post('/api/wallpaper/files/trash', json={'names': [name]})
            self.assertEqual(r.status_code, 400)
        self.assertEqual(before, {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in self.img.iterdir()})

    def test_bitmap_and_tiff_full_previews_keep_dimensions(self):
        for suffix in ('bmp', 'tiff'):
            name = 'format.' + suffix
            Image.new('RGB', (800, 600), 'blue').save(self.wall / name)
            with self.client.get('/api/local-img?root=wall&p=' + name) as response:
                self.assertEqual(response.status_code, 200)
                from io import BytesIO
                with Image.open(BytesIO(response.data)) as image:
                    self.assertEqual(image.size, (800, 600))
