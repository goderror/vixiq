"""已有壁纸选择流程的后端回归，复用隔离图库并替换系统设置调用。"""
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from autotest.test_wallpaper_duplicates import WallpaperFixture
import web.server as server


class ExistingWallpaperTests(WallpaperFixture):
    def test_current_badge_only_marks_exact_version(self):
        first = self.add_wall('original.png')
        self.add_wall('crop.png')
        with patch.object(server, '_wallpaper_file', lambda: first):
            data = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()
        self.assertEqual({r['name'] for r in data['items'] if r['current']}, {'original.png'})
        self.apply.assert_not_called()

    def test_same_filename_outside_wallpaper_folder_is_not_current(self):
        self.add_wall('original.png')
        outside = self.root / 'original.png'; outside.write_bytes(b'outside')
        with patch.object(server, '_wallpaper_file', lambda: outside):
            data = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()
        self.assertFalse(any(r['current'] for r in data['items']))

    def test_more_than_twenty_versions_remain_selectable(self):
        for i in range(24):
            self.add_wall(f'version-{i}.png')
        data = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()
        self.assertEqual(data['count'], 24)

    def test_payload_safe_rating_and_encoded_preview_urls(self):
        self.add_wall("with space & #'.png")
        data = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()
        item = data['items'][0]
        self.assertEqual(item['is_r18'], 0)
        self.assertEqual(parse_qs(urlsplit(item['thumb']).query)['p'], ["with space & #'.png"])
        self.assertEqual(parse_qs(urlsplit(item['url']).query)['root'], ['wall'])
        self.assertNotIn('thumb', parse_qs(urlsplit(item['url']).query))
        with self.client.get(item['thumb']) as response:
            self.assertEqual(response.status_code, 200)
        with self.client.get(item['url']) as response:
            self.assertEqual(response.status_code, 200)
        with self.db() as db:
            db.conn.execute('UPDATE artworks SET is_r18=1 WHERE artwork_id=?', ('700001',))
            db.conn.commit()
        item = self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0').get_json()['items'][0]
        self.assertEqual(item['is_r18'], 1)

    def test_unknown_rating_stays_unknown(self):
        file = self.add_wall('unknown.png', indexed=False)
        item = server._wall_existing_payload([{'name': file.name, 'path': str(file), 'artwork_id': ''}])[0]
        self.assertIsNone(item['is_r18'])

    def test_confirm_sets_selected_file_without_copying_or_changing_owner(self):
        first = self.add_wall('original.png')
        self.add_wall('crop.png')
        with self.db() as db:
            before_rows = [tuple(r) for r in db.conn.execute('SELECT * FROM wallpaper_files ORDER BY file_name')]
        before_files = {p.name: p.read_bytes() for p in self.wall.iterdir()}
        self.client.get('/api/wallpaper/dupes/check?artwork_id=700001&page_num=0')
        self.apply.assert_not_called()
        response = self.client.post('/api/wallpaper/use', json={'name': first.name})
        self.assertEqual(response.status_code, 200)
        self.apply.assert_called_once_with(first.resolve())
        self.assertEqual(before_files, {p.name: p.read_bytes() for p in self.wall.iterdir()})
        with self.db() as db:
            self.assertEqual(before_rows, [tuple(r) for r in db.conn.execute('SELECT * FROM wallpaper_files ORDER BY file_name')])

    def test_missing_and_outside_paths_are_rejected(self):
        file = self.add_wall('deleted.png'); file.unlink()
        for value in ('deleted.png', '../outside.png', str(self.img / '700001_p0.png'), '', None):
            response = self.client.post('/api/wallpaper/use', json={'name': value})
            self.assertEqual(response.status_code, 400)
        self.apply.assert_not_called()

    def test_non_image_and_library_files_are_rejected(self):
        (self.wall / 'notes.txt').write_text('not an image', encoding='utf-8')
        self.assertEqual(self.client.post('/api/wallpaper/use', json={'name': 'notes.txt'}).status_code, 400)
        with patch.object(server, 'candidates_dir', lambda *a: self.img):
            self.assertEqual(self.client.post('/api/wallpaper/use', json={'name': '700001_p0.png'}).status_code, 400)
        self.apply.assert_not_called()

    def test_system_failure_reports_error_and_allows_retry(self):
        self.add_wall('retry.png')
        self.apply.return_value = False
        failed = self.client.post('/api/wallpaper/use', json={'name': 'retry.png'})
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.get_json()['status'], 'error')
        self.apply.return_value = True
        self.assertEqual(self.client.post('/api/wallpaper/use', json={'name': 'retry.png'}).status_code, 200)
