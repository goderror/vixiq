"""壁纸文件的整理与回收。只接受当前壁纸夹的索引文件，不操作图库原图。"""
import json
from functools import wraps
from pathlib import Path
import re
import shutil
import threading
import uuid
from urllib.parse import quote

from flask import jsonify, request

_LOCK = threading.RLock()


def exclusive(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapped


def _copy_move(source, target):
    """跨盘可用，目标以独占方式创建；失败时保留源文件，不覆盖现有文件。"""
    created = False
    try:
        with source.open('rb') as inp, target.open('xb') as out:
            created = True
            shutil.copyfileobj(inp, out)
        shutil.copystat(source, target)
        if target.stat().st_size != source.stat().st_size:
            raise OSError('文件复制不完整')
        source.unlink()
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise


def register(app, server, notify):
    # 传入模块而不是绑定函数，兼容运行时配置和隔离测试的替身。
    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('请求必须为 JSON 对象')
        return value

    def names(data):
        value = data.get('names')
        if not isinstance(value, list) or not 1 <= len(value) <= 5000 or any(not isinstance(n, str) for n in value):
            raise ValueError('请选择壁纸（每次最多 5000 张）')
        return list(dict.fromkeys(value))

    def targets(data):
        if server._WALL_FILES.get('status') == 'scanning':
            raise ValueError('归属扫描进行中，请结束后再整理')
        wanted = names(data)
        files, refused = server._wall_targets(wanted)
        if refused or len(files) != len(wanted):
            raise ValueError('；'.join(refused) or '文件已变化，请刷新列表')
        with server._db() as db:
            result = []
            for name, file in files:
                row = db.conn.execute('SELECT * FROM wallpaper_files WHERE file_name=?', (name,)).fetchone()
                if not row or Path(row['dir']).resolve() != file.parent.resolve():
                    raise ValueError('索引与当前壁纸夹不一致，请先扫描：' + name)
                result.append((name, file, dict(row)))
        return result

    def current_guard(files):
        current = server._wallpaper_file()
        if current and any(p.resolve() == Path(current).resolve() for _, p, _ in files):
            raise ValueError('包含当前桌面壁纸，请先切换桌面壁纸再重命名或删除')

    def endpoint(fn):
        def wrapped(*args, **kwargs):
            try:
                with _LOCK:
                    return jsonify(fn(*args, **kwargs))
            except (ValueError, OSError) as exc:
                return jsonify(ok=False, msg=str(exc)), 400
        wrapped.__name__ = 'wall_admin_' + fn.__name__
        return wrapped

    @app.get('/api/wallpaper/groups')
    @endpoint
    def groups():
        with server._db() as db:
            rows = db.conn.execute('SELECT g.id,g.name,COUNT(w.file_name) AS count FROM wallpaper_groups g '
                                   'LEFT JOIN wallpaper_files w ON w.group_id=g.id GROUP BY g.id ORDER BY g.name').fetchall()
        return {'ok': True, 'items': [dict(r) for r in rows]}

    @app.post('/api/wallpaper/groups/save')
    @endpoint
    def group_save():
        data = body()
        name = str(data.get('name') or '').strip()
        if not 1 <= len(name) <= 80:
            raise ValueError('分组名须为 1～80 个字符')
        with server._db() as db, db.conn:
            gid = data.get('id')
            exists = db.conn.execute('SELECT id FROM wallpaper_groups WHERE name=? COLLATE NOCASE', (name,)).fetchone()
            if exists and str(exists[0]) != str(gid):
                raise ValueError('已有同名分组')
            if gid is None:
                gid = db.conn.execute('INSERT INTO wallpaper_groups(name) VALUES(?)', (name,)).lastrowid
            elif not db.conn.execute('UPDATE wallpaper_groups SET name=? WHERE id=?', (name, gid)).rowcount:
                raise ValueError('分组不存在')
        return {'ok': True, 'id': gid}

    @app.post('/api/wallpaper/groups/delete')
    @endpoint
    def group_delete():
        gid = body().get('id')
        with server._db() as db, db.conn:
            db.conn.execute('UPDATE wallpaper_files SET group_id=NULL WHERE group_id=?', (gid,))
            db.conn.execute('DELETE FROM wallpaper_groups WHERE id=?', (gid,))
        return {'ok': True}

    @app.get('/api/wallpaper/works')
    @endpoint
    def works():
        query = request.args.get('q', '').strip()[:200]
        with server._db() as db:
            rows = db.conn.execute(
                "SELECT a.artwork_id,a.title,COALESCE(au.name,'') author_name,"
                '(SELECT COUNT(*) FROM wallpaper_files w WHERE w.artwork_id=a.artwork_id) wallpaper_count '
                'FROM artworks a LEFT JOIN authors au ON a.author_id=au.author_id WHERE a.trashed_at IS NULL '
                "AND (a.artwork_id LIKE ? OR a.title LIKE ? OR au.name LIKE ?) ORDER BY a.updated_at DESC LIMIT 60",
                ('%' + query + '%',) * 3).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item['pages'] = [r[0] for r in db.conn.execute('SELECT page_num FROM pages WHERE artwork_id=? '
                                                              'AND trashed_at IS NULL ORDER BY page_num', (row['artwork_id'],))]
                items.append(item)
        return {'ok': True, 'items': items}

    @app.post('/api/wallpaper/files/edit')
    @endpoint
    def edit():
        data = body()
        files = targets(data)
        values = {}
        if 'favorite' in data:
            if not isinstance(data['favorite'], bool):
                raise ValueError('收藏状态无效')
            values['favorite'] = int(data['favorite'])
        if 'labels' in data:
            labels = data['labels']
            if not isinstance(labels, list) or len(labels) > 20 or any(not isinstance(x, str) or len(x) > 40 for x in labels):
                raise ValueError('最多 20 个标记，每个最多 40 字')
            values['labels_json'] = json.dumps(list(dict.fromkeys(x.strip() for x in labels if x.strip())), ensure_ascii=False)
        with server._db() as db:
            if 'group_id' in data:
                gid = data['group_id']
                if gid is not None and type(gid) is not int:
                    raise ValueError('分组无效')
                if gid is not None and not db.conn.execute('SELECT 1 FROM wallpaper_groups WHERE id=?', (gid,)).fetchone():
                    raise ValueError('分组不存在')
                values['group_id'] = gid
            if 'artwork_id' in data:
                aid = str(data['artwork_id'] or '').strip()
                if aid and not db.conn.execute('SELECT 1 FROM artworks WHERE artwork_id=? AND trashed_at IS NULL', (aid,)).fetchone():
                    raise ValueError('所属作品不存在或已在回收站')
                page = data.get('page_num') if aid else None
                if page is not None and (type(page) is not int or not db.conn.execute(
                        'SELECT 1 FROM pages WHERE artwork_id=? AND page_num=? AND trashed_at IS NULL', (aid, page)).fetchone()):
                    raise ValueError('作品中不存在这张图片')
                values.update(artwork_id=aid or None, page_num=page, match_kind='manual', confidence=1)
            if not values:
                raise ValueError('没有修改内容')
            with db.conn:
                for name, _, _ in files:
                    db.conn.execute('UPDATE wallpaper_files SET ' + ','.join(k + '=?' for k in values) + ' WHERE file_name=?',
                                    (*values.values(), name))
        notify()
        return {'ok': True, 'updated': len(files)}

    @app.post('/api/wallpaper/files/rename')
    @endpoint
    def rename():
        data = body()
        files = targets(data)
        if len(files) != 1:
            raise ValueError('重命名每次选择一张壁纸')
        current_guard(files)
        old, source, row = files[0]
        new = data.get('new_name')
        if not isinstance(new, str) or not new.strip() or len(new) > 180 or re.search(r'[<>:"/\\|?*\x00-\x1f]', new) or new.endswith((' ', '.')):
            raise ValueError('文件名无效，不得包含路径或 Windows 禁用字符')
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', new, re.I):
            raise ValueError('文件名为 Windows 保留名称')
        if Path(new).suffix.lower() != source.suffix.lower():
            raise ValueError('重命名须保留原扩展名')
        if new == old:
            return {'ok': True, 'name': old}
        dest = source.parent / new
        with server._db() as db:
            if dest.exists() or db.conn.execute('SELECT 1 FROM wallpaper_files WHERE file_name=? COLLATE NOCASE', (new,)).fetchone():
                raise ValueError('已有同名文件，不覆盖')
            _copy_move(source, dest)
            try:
                with db.conn:
                    db.conn.execute('UPDATE wallpaper_files SET file_name=?,match_kind=? WHERE file_name=?', (new, 'manual', old))
            except Exception:
                _copy_move(dest, source)
                raise
        return {'ok': True, 'name': new}

    @app.post('/api/wallpaper/files/trash')
    @endpoint
    def trash():
        files = targets(body())
        current_guard(files)
        root = server._trash_root().resolve()
        done, failed = [], []
        with server._db() as db:
            for name, file, row in files:
                tid = uuid.uuid4().hex
                rel = 'wallpapers/' + tid + file.suffix.lower()
                dest = root / rel
                if root not in dest.resolve().parents:
                    failed.append(name + '：回收目录路径无效'); continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _copy_move(file, dest)
                    try:
                        with db.conn:
                            db.conn.execute('INSERT INTO wallpaper_trash(id,original_name,original_dir,trash_rel,metadata_json) VALUES(?,?,?,?,?)',
                                            (tid, name, str(file.parent.resolve()), rel, json.dumps(row, ensure_ascii=False)))
                            db.conn.execute('DELETE FROM wallpaper_files WHERE file_name=?', (name,))
                    except Exception:
                        _copy_move(dest, file)
                        raise
                    done.append(name)
                except Exception as exc:
                    failed.append(name + '：' + str(exc))
        notify()
        try:
            server.audit_log.record('delete', '移出壁纸库', f'{len(done)} 张壁纸移入应用回收站')
        except OSError:
            pass
        return {'ok': True, 'moved': len(done), 'failed': failed}

    @app.get('/api/wallpaper/trash')
    @endpoint
    def trash_list():
        with server._db() as db:
            rows = db.conn.execute('SELECT * FROM wallpaper_trash ORDER BY trashed_at DESC').fetchall()
            items = []
            for row in rows:
                metadata = json.loads(row['metadata_json'])
                work = db.conn.execute('SELECT is_r18 FROM artworks WHERE artwork_id=?', (metadata.get('artwork_id'),)).fetchone()
                items.append({'id': row['id'], 'name': row['original_name'], 'trashed_at': row['trashed_at'],
                              'is_r18': work[0] if work else None, 'size': metadata.get('size', 0),
                              'thumb': '/api/local-img?root=trash&thumb=1&p=' + quote(row['trash_rel'], safe='')})
        return {'ok': True, 'items': items}

    @app.post('/api/wallpaper/trash/<action>')
    @endpoint
    def trash_action(action):
        if action not in ('restore', 'purge'):
            raise ValueError('无效操作')
        if action == 'restore' and server._WALL_FILES.get('status') == 'scanning':
            raise ValueError('归属扫描进行中，请结束后再恢复')
        data = body()
        ids = data.get('ids')
        if not isinstance(ids, list) or not ids or len(ids) > 5000 or any(not isinstance(i, str) for i in ids):
            raise ValueError('请选择回收记录')
        root = server._trash_root().resolve()
        wall_dir = server.candidates_dir(server._wallpaper_file())
        done, failed = 0, []
        with server._db() as db:
            for tid in dict.fromkeys(ids):
                row = db.conn.execute('SELECT * FROM wallpaper_trash WHERE id=?', (tid,)).fetchone()
                if not row:
                    failed.append(tid + '：记录已不存在'); continue
                source = (root / row['trash_rel']).resolve()
                if root not in source.parents or not source.is_file():
                    failed.append(row['original_name'] + '：回收文件不存在或路径无效'); continue
                if action == 'purge':
                    n, bad = server._recycle_files([str(source)])
                    if n != 1 or bad or source.exists():
                        failed.append(row['original_name'] + '：Windows 回收失败'); continue
                    with db.conn:
                        db.conn.execute('DELETE FROM wallpaper_trash WHERE id=?', (tid,))
                else:
                    parent = Path(row['original_dir']).resolve()
                    name = row['original_name']
                    if not wall_dir or parent != Path(wall_dir).resolve() or Path(name).name != name:
                        failed.append(name + '：原壁纸夹已变更，请恢复原文件夹设置后重试'); continue
                    dest = parent / name
                    if dest.exists() or db.conn.execute('SELECT 1 FROM wallpaper_files WHERE file_name=? COLLATE NOCASE', (name,)).fetchone():
                        failed.append(name + '：原位置已被占用，未覆盖'); continue
                    metadata = json.loads(row['metadata_json'])
                    if metadata.get('group_id') and not db.conn.execute('SELECT 1 FROM wallpaper_groups WHERE id=?', (metadata['group_id'],)).fetchone():
                        metadata['group_id'] = None
                    try:
                        _copy_move(source, dest)
                        try:
                            keys = list(metadata)
                            with db.conn:
                                db.conn.execute('INSERT INTO wallpaper_files(' + ','.join(keys) + ') VALUES(' + ','.join('?' for _ in keys) + ')', tuple(metadata.values()))
                                db.conn.execute('DELETE FROM wallpaper_trash WHERE id=?', (tid,))
                        except Exception:
                            _copy_move(dest, source)
                            raise
                    except Exception as exc:
                        failed.append(name + '：' + str(exc)); continue
                done += 1
        try:
            server.audit_log.record('restore' if action == 'restore' else 'delete', '恢复壁纸' if action == 'restore' else '回收壁纸文件', f'{done} 张')
        except OSError:
            pass
        notify()
        return {'ok': True, 'done': done, 'failed': failed}
