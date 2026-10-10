"""Read-only Lightroom catalog migration, with atomic writes and source archives.

Adobe develop text is data: it is never evaluated. Optional translation is
reported as approximate Luma controls, with unsupported fields retained.
An unchanged source record reuses its previous import. A changed record creates
a new copy, preserving edits the photographer made in Luma after importing.
"""
from __future__ import annotations

import base64
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


REQUIRED = {
    'AgLibraryRootFolder': {'id_local', 'absolutePath'},
    'AgLibraryFolder': {'id_local', 'rootFolder', 'pathFromRoot'},
    'AgLibraryFile': {'id_local', 'folder', 'baseName', 'extension'},
    'Adobe_images': {'id_local', 'rootFile'},
}
ARCHIVE_TABLES = tuple(REQUIRED) + (
    'AgLibraryKeyword', 'AgLibraryKeywordImage', 'AgLibraryKeywordSynonym',
    'AgLibraryCollection', 'AgLibraryCollectionImage', 'AgLibraryCollectionContent',
    'AgLibraryFolderStack', 'AgLibraryFolderStackImage',
    'AgLibraryCollectionStack', 'AgLibraryCollectionStackImage',
    'AgLibraryIPTC', 'AgHarvestedExifMetadata', 'AgHarvestedIptcMetadata',
    'AgInternedExifCameraModel', 'AgInternedExifCameraSN', 'AgInternedExifLens', 'AgInternedIptcCreator',
    'AgInternedIptcCity', 'AgInternedIptcCountry', 'AgInternedIptcState',
    'AgInternedIptcLocation', 'AgInternedIptcJobIdentifier', 'Adobe_imageDevelopSettings',
    'Adobe_libraryImageDevelopHistoryStep', 'Adobe_libraryImageDevelopSnapshot',
    'Adobe_imageProofSettings',
)
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS lightroom_archive (
        source_key TEXT,kind TEXT,source_id TEXT,digest TEXT,payload TEXT NOT NULL,
        PRIMARY KEY(source_key,kind,source_id,digest))''',
    '''CREATE TABLE IF NOT EXISTS lightroom_import_items (
        source_key TEXT,source_id TEXT,digest TEXT,photo_id INTEGER NOT NULL,
        PRIMARY KEY(source_key,source_id,digest))''',
    '''CREATE TABLE IF NOT EXISTS lightroom_import_sets (
        source_key TEXT,digest TEXT,collection_id INTEGER NOT NULL,
        PRIMARY KEY(source_key,digest))''',
    '''CREATE TABLE IF NOT EXISTS lightroom_import_runs (
        id TEXT PRIMARY KEY,created TEXT,source_path TEXT,source_key TEXT,summary TEXT)''',
    '''CREATE TRIGGER IF NOT EXISTS lightroom_photo_removed AFTER DELETE ON photos BEGIN
        DELETE FROM lightroom_import_items WHERE photo_id=OLD.id; END''',
    '''CREATE TRIGGER IF NOT EXISTS lightroom_collection_removed AFTER DELETE ON collections BEGIN
        DELETE FROM lightroom_import_sets WHERE collection_id=OLD.id; END''',
)


class ImportCancelled(Exception):
    pass


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise ImportCancelled()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=lambda data: {'base64': base64.b64encode(data).decode('ascii')})


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Source:
    def __init__(self, path, cancel=None):
        self.path = Path(path).resolve()
        self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.cancel = cancel
        try:
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA trusted_schema=OFF')
            self.db.set_progress_handler(lambda: bool(cancel and cancel.is_set()), 1000)
            self.db.execute('BEGIN')  # One consistent source snapshot, including committed WAL.
            self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, wanted in REQUIRED.items():
                if table not in self.tables or not wanted <= self.columns(table):
                    raise ValueError('이 Lightroom 카탈로그 구조는 지원하지 않습니다: ' + table)
            self.key = 'path:' + str(self.path).casefold()
            for table in ('Adobe_variablesTable', 'Adobe_variables'):
                if table in self.tables and {'name', 'value'} <= self.columns(table):
                    row = self.db.execute(f'SELECT value FROM "{table}" WHERE name=?', ('Adobe_storeProviderID',)).fetchone()
                    if row and row[0]:
                        self.key = 'catalog:' + str(row[0])
                        break
        except BaseException:
            self.db.close()
            raise

    def columns(self, table):
        return {r[1] for r in self.db.execute(f'PRAGMA table_info("{table}")')}

    def counts(self):
        result = {}
        for table in ARCHIVE_TABLES:
            _check(self.cancel)
            if table in self.tables:
                result[table] = self.db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        return result

    def close(self):
        self.db.close()


def inspect_catalog(path, cancel=None):
    with closing(Source(path, cancel)) as source:
        return {'path': str(source.path), 'source_key': source.key, 'counts': source.counts()}


def _stage(source, db, cancel, progress, warnings):
    # The temporary index avoids repeated full source scans for each image and
    # bounds Python memory even when a catalog has millions of history rows.
    db.execute('PRAGMA temp_store=FILE')
    db.execute('CREATE TEMP TABLE lr_stage(kind TEXT,id TEXT,image TEXT,payload TEXT,digest TEXT)')
    counts = source.counts()
    total, done = sum(counts.values()), 0
    for table in ARCHIVE_TABLES:
        if table not in counts:
            continue
        for ordinal, row in enumerate(source.db.execute(f'SELECT * FROM "{table}"')):
            _check(cancel)
            data = dict(row)
            key = str(data.get('id_local', ordinal))
            payload = _json(data)
            digest = _digest(payload)
            db.execute('INSERT OR IGNORE INTO lightroom_archive VALUES(?,?,?,?,?)',
                       (source.key, table, key, digest, payload))
            db.execute('INSERT INTO lr_stage VALUES(?,?,?,?,?)',
                       (table, key, str(data.get('image', '')), payload, digest))
            done += 1
            if done % 250 == 0:
                progress({'stage': 'Lightroom 기록 읽는 중', 'done': done, 'total': total})
    db.execute('CREATE INDEX lr_stage_id ON lr_stage(kind,id)')
    db.execute('CREATE INDEX lr_stage_image ON lr_stage(image,kind)')
    for table in ('Adobe_imageDevelopSettings', 'Adobe_libraryImageDevelopHistoryStep',
                  'Adobe_libraryImageDevelopSnapshot', 'AgLibraryCollectionStackImage', 'AgLibraryKeywordSynonym'):
        if counts.get(table):
            warnings[table] += counts[table]
    return counts


def _rows(db, kind, image=None):
    if image is None:
        cursor = db.execute('SELECT payload FROM lr_stage WHERE kind=? ORDER BY id,digest', (kind,))
    else:
        cursor = db.execute('SELECT payload FROM lr_stage WHERE kind=? AND image=? ORDER BY id,digest', (kind, str(image)))
    for row in cursor:
        yield json.loads(row[0])


def _record(db, kind, ident):
    row = db.execute('SELECT payload FROM lr_stage WHERE kind=? AND id=? LIMIT 1', (kind, str(ident))).fetchone()
    return json.loads(row[0]) if row else {}


def _keyword_paths(db, warnings, cancel):
    keywords = {str(r['id_local']): r for r in _rows(db, 'AgLibraryKeyword')}
    result = {}
    for ident in keywords:
        _check(cancel)
        seen, parts, current = set(), [], ident
        while current in keywords:
            if current in seen or len(seen) > 256:
                warnings['keyword_cycle'] += 1
                parts = []
                break
            seen.add(current)
            data = keywords[current]
            name = str(data.get('name') or '').strip()
            # Luma uses comma and | as separators; retain ambiguous names in
            # the archive instead of silently splitting one keyword into two.
            if ',' in name or '|' in name:
                warnings['keyword_separator'] += 1
                parts = []
                break
            if name:
                parts.append(name)
            parent = data.get('parent')
            if parent is not None and str(parent) not in keywords:
                warnings['keyword_parent'] += 1
            current = str(parent)
        if parts:
            result[ident] = '|'.join(reversed(parts))
    return result


def _path(db, row):
    file = _record(db, 'AgLibraryFile', row.get('rootFile'))
    folder = _record(db, 'AgLibraryFolder', file.get('folder'))
    root = _record(db, 'AgLibraryRootFolder', folder.get('rootFolder'))
    if not root.get('absolutePath') or not file.get('baseName'):
        return None
    root_path = Path(root['absolutePath'])
    # A macOS catalog needs explicit relinking on Windows. Never reinterpret a
    # foreign or relative path as a file underneath Luma's working directory.
    if not root_path.is_absolute():
        return None
    relative = Path(folder.get('pathFromRoot') or '')
    name = str(file['baseName'])
    extension = str(file.get('extension') or '').lstrip('.')
    if extension and not name.casefold().endswith('.' + extension.casefold()):
        name += '.' + extension
    if relative.is_absolute() or relative.drive or '..' in relative.parts or Path(name).name != name:
        return None
    return root_path / relative / name


def _metadata(db, row):
    from .adobe_history import adobe_time
    meta = {}
    if row.get('copyName'):
        meta['_copy_name'] = str(row['copyName'])
    edited = adobe_time(row.get('touchTime'))
    if edited:
        meta['_last_edit_time'] = edited
    stamp = row.get('captureTime')
    if stamp:
        try:
            meta['DateTimeOriginal'] = datetime.fromisoformat(stamp).strftime('%Y:%m:%d %H:%M:%S')
        except (TypeError, ValueError):
            pass
    for data in _rows(db, 'AgLibraryIPTC', row['id_local']):
        for key in ('caption', 'copyright'):
            if data.get(key) is not None:
                meta[key] = data[key]
    for data in _rows(db, 'AgHarvestedIptcMetadata', row['id_local']):
        for key in ('creator', 'city', 'country', 'state', 'location', 'jobIdentifier'):
            value = _record(db, 'AgInternedIptc' + key[0].upper() + key[1:], data.get(key + 'Ref')).get('value')
            if value is not None:
                meta['job' if key == 'jobIdentifier' else key] = value
    for data in _rows(db, 'AgHarvestedExifMetadata', row['id_local']):
        for source, table, key in (('cameraModelRef','AgInternedExifCameraModel','camera'),
                                   ('cameraSNRef','AgInternedExifCameraSN','serial'),
                                   ('lensRef','AgInternedExifLens','lens')):
            value = _record(db, table, data.get(source)).get('value')
            if value is not None:
                meta[key] = value
        if isinstance(data.get('isoSpeedRating'), (int,float)) and data['isoSpeedRating'] > 0:
            meta['iso'] = data['isoSpeedRating']
        if data.get('hasGPS'):
            for key, source, limit in (('latitude', 'gpsLatitude', 90), ('longitude', 'gpsLongitude', 180)):
                value = data.get(source)
                if isinstance(value, (int, float)) and -limit <= value <= limit:
                    meta[key] = value
    return meta


def _number(value, low, high):
    try:
        return max(low, min(high, int(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _photos(source, db, cancel, progress, warnings, count, convert_edits=False):
    from .engine import IMAGE_EXTENSIONS, RAW_EXTENSIONS, defaults
    from . import adobe_history, adobe_develop
    payload = _json(defaults())
    keywords = _keyword_paths(db, warnings, cancel)
    db.execute('CREATE TEMP TABLE lr_map(source_id TEXT PRIMARY KEY,photo_id INTEGER,created INTEGER)')
    imported = reused = skipped = copies = converted_photos = histories = snapshots = 0
    # Lightroom's masterImage points to the original; originals must be first.
    cursor = db.execute("SELECT payload FROM lr_stage WHERE kind='Adobe_images' ORDER BY "
                        "CASE WHEN json_extract(payload,'$.masterImage') IS NULL THEN 0 ELSE 1 END,id")
    for index, packed in enumerate(cursor):
        _check(cancel)
        row = json.loads(packed[0])
        ident = str(row['id_local'])
        path = _path(db, row)
        if path is None or path.suffix.lower() not in IMAGE_EXTENSIONS:
            skipped += 1
            warnings['unsupported_path_or_format'] += 1
            continue
        master = row.get('masterImage')
        source_master = None
        if master is not None:
            source_master = db.execute('SELECT photo_id FROM lr_map WHERE source_id=?', (str(master),)).fetchone()
            if not source_master or db.execute('SELECT path FROM photos WHERE id=?', source_master).fetchone()[0].casefold() != str(path).casefold():
                skipped += 1
                warnings['virtual_master'] += 1
                continue
        tags = sorted({keywords[str(k['tag'])] for k in _rows(db, 'AgLibraryKeywordImage', ident)
                       if str(k.get('tag')) in keywords})
        meta = _metadata(db, row)
        state = {'version': 1, 'row': row, 'path': str(path), 'keywords': tags, 'metadata': meta}
        if convert_edits:
            state['develop_translation'] = adobe_develop.VERSION
        sha = hashlib.sha256(_json(state).encode('utf-8'))
        # Include archived develop/history/snapshots without loading them all.
        for item in db.execute('SELECT kind,id,digest FROM lr_stage WHERE image=? ORDER BY kind,id,digest', (ident,)):
            sha.update(_json(tuple(item)).encode('utf-8'))
        digest = sha.hexdigest()
        previous = db.execute('''SELECT i.photo_id FROM lightroom_import_items i JOIN photos p ON p.id=i.photo_id
            WHERE source_key=? AND source_id=? AND digest=?''', (source.key, ident, digest)).fetchone()
        if previous:
            photo_id = previous[0]
            reused += 1
            created = 0
        else:
            translated = None
            photo_payload = payload
            if convert_edits:
                current_rows = list(_rows(db,'Adobe_imageDevelopSettings',ident))
                if len(current_rows) == 1:
                    translated = adobe_history.decode_row(current_rows[0],path.suffix.lower() in RAW_EXTENSIONS,row.get('orientation'),warnings)
                    if translated:
                        photo_payload = _json(translated['settings'])
                        converted_photos += 1
                elif current_rows:
                    warnings['develop_ambiguous'] += 1
            existing = db.execute('SELECT id,virtual_source FROM photos WHERE path=? COLLATE NOCASE ORDER BY virtual_source IS NOT NULL,id LIMIT 1', (str(path),)).fetchone()
            original = (source_master[0] if source_master else (existing[1] or existing[0]) if existing else None)
            if original:
                source_row = db.execute('SELECT virtual_source FROM photos WHERE id=?', (original,)).fetchone()
                if source_row and source_row[0]:
                    original = source_row[0]
            name = path.name
            if row.get('copyName'):
                name += ' · ' + str(row['copyName'])
            elif original:
                name += ' · Lightroom 사본'
            label = str(row.get('colorLabels') or '')
            label = {'red': '빨강', 'yellow': '노랑', 'green': '초록', 'blue': '파랑', 'purple': '보라'}.get(label.casefold(), label)
            insert = db.execute('''INSERT INTO photos(path,name,rating,flag,keywords,edits,metadata,imported,label,virtual_source,extras)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (str(path), name, _number(row.get('rating'), 0, 5),
                _number(row.get('pick'), -1, 1), ', '.join(tags), photo_payload, '{}', datetime.now(timezone.utc).isoformat(),
                label, original, _json(meta)))
            photo_id = insert.lastrowid
            db.execute('INSERT OR REPLACE INTO lightroom_import_items VALUES(?,?,?,?)', (source.key, ident, digest, photo_id))
            db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)', (str(path.parent),))
            imported += 1
            copies += int(original is not None)
            created = 1
            if convert_edits:
                transferred = adobe_history.transfer(db,ident,photo_id,translated['settings'] if translated else None,
                    is_raw=path.suffix.lower() in RAW_EXTENSIONS,orientation=row.get('orientation'),warnings=warnings,
                    cancel_check=lambda:_check(cancel))
                histories += transferred['history'];snapshots += transferred['snapshots']
        db.execute('INSERT INTO lr_map VALUES(?,?,?)', (ident, photo_id, created))
        if index % 100 == 0:
            progress({'stage': '사진 분류 가져오는 중', 'done': index + 1, 'total': count})
    return {'imported': imported, 'reused': reused, 'skipped': skipped, 'copies': copies,
            'converted_photos':converted_photos,'converted_history':histories,'converted_snapshots':snapshots}


def _collections(source, db, cancel, warnings):
    from . import smart_rules
    collections = {str(r['id_local']): r for r in _rows(db, 'AgLibraryCollection') if not r.get('systemOnly')}
    sha = hashlib.sha256(f'smart-translation:{smart_rules.VERSION}'.encode())
    for kind in ('AgLibraryCollection', 'AgLibraryCollectionImage', 'AgLibraryCollectionContent'):
        for row in db.execute('SELECT id,digest FROM lr_stage WHERE kind=? ORDER BY id,digest', (kind,)):
            _check(cancel)
            sha.update(_json(tuple(row)).encode('utf-8'))
    for row in db.execute('SELECT source_id,photo_id FROM lr_map ORDER BY source_id'):
        sha.update(_json(tuple(row)).encode('utf-8'))
    digest = sha.hexdigest()
    old = db.execute('''SELECT i.collection_id FROM lightroom_import_sets i JOIN collections c ON c.id=i.collection_id
        WHERE source_key=? AND digest=?''', (source.key, digest)).fetchone()
    if old:
        return {'collections': 0, 'collection_root': old[0], 'converted_smart': 0}
    root = db.execute('INSERT INTO collections(name) VALUES(?)',
                      ('Lightroom · ' + source.path.stem + ' · ' + datetime.now().strftime('%m/%d %H:%M'),)).lastrowid
    db.execute('INSERT INTO collection_members SELECT ?,photo_id FROM lr_map', (root,))
    mapped, smart_content = {}, {}
    for row in _rows(db, 'AgLibraryCollectionContent'):
        if row.get('owningModule') == 'ag.library.smart_collection':
            smart_content.setdefault(str(row.get('collection')), []).append(row.get('content'))
    converted = 0
    for key, row in collections.items():
        _check(cancel)
        name = str(row.get('name') or '이름 없는 컬렉션')
        rules = None
        if 'smart' in str(row.get('creationId') or '').lower():
            try:
                texts = smart_content.get(key, [])
                if len(texts) != 1:
                    raise ValueError('스마트 규칙 원문이 없거나 여러 개입니다.')
                rules = _json(smart_rules.translate(texts[0]))
                converted += 1
            except (ValueError,TypeError,OverflowError) as error:
                name += ' · 스마트 규칙 보관'
                warnings['smart_collection'] += 1
                warnings['smart_reason:' + str(error)] += 1
        mapped[key] = db.execute('INSERT INTO collections(name,parent_id,rules) VALUES(?,?,?)', (name, root, rules)).lastrowid
    for key, row in collections.items():
        _check(cancel)
        seen, parent = {key}, str(row.get('parent'))
        current = parent
        while current in collections and current not in seen:
            seen.add(current)
            current = str(collections[current].get('parent'))
        if current in seen:
            warnings['collection_cycle'] += 1
            parent = ''
        if parent in mapped:
            db.execute('UPDATE collections SET parent_id=? WHERE id=?', (mapped[parent], mapped[key]))
    for row in _rows(db, 'AgLibraryCollectionImage'):
        _check(cancel)
        target = mapped.get(str(row.get('collection')))
        if target:
            member = db.execute('SELECT photo_id FROM lr_map WHERE source_id=?', (str(row.get('image')),)).fetchone()
            if member:
                db.execute('INSERT OR IGNORE INTO collection_members VALUES(?,?)', (target, member[0]))
            else:
                warnings['collection_member'] += 1
    db.execute('INSERT OR REPLACE INTO lightroom_import_sets VALUES(?,?,?)', (source.key, digest, root))
    return {'collections': len(mapped) + 1, 'collection_root': root, 'converted_smart': converted}


def _stacks(db, cancel, warnings):
    db.execute('CREATE TEMP TABLE lr_stacks(stack TEXT,photo INTEGER,created INTEGER)')
    for row in _rows(db, 'AgLibraryFolderStackImage'):
        _check(cancel)
        if row.get('stack') is None:
            warnings['stack_reference'] += 1
            continue
        db.execute('INSERT INTO lr_stacks SELECT ?,photo_id,created FROM lr_map WHERE source_id=?',
                   (str(row.get('stack')), str(row.get('image'))))
    for stack, count, created in db.execute('SELECT stack,COUNT(*),SUM(created) FROM lr_stacks GROUP BY stack'):
        _check(cancel)
        if count != created:
            if created:
                warnings['stack_existing'] += 1
            continue
        if count > 1:
            group = db.execute('SELECT MIN(photo) FROM lr_stacks WHERE stack=?', (stack,)).fetchone()[0]
            db.execute('UPDATE photos SET stack_id=? WHERE id IN (SELECT photo FROM lr_stacks WHERE stack=?)', (group, stack))


def import_catalog(path, directory, cancel=None, progress=lambda status: None, *, convert_edits=False):
    """Worker-only entry point. Does not access the UI's SQLite connection."""
    directory = Path(directory).resolve()
    target = directory / 'catalog.sqlite'
    if Path(path).resolve() == target:
        raise ValueError('Luma 카탈로그 자신은 가져올 수 없습니다.')
    _check(cancel)
    warnings = Counter()
    run_id = uuid.uuid4().hex
    backup = directory / 'backups' / ('before-lightroom-' + run_id + '.sqlite')
    pending_backup = backup.with_suffix('.pending')
    result = {'cancelled': False, 'source_path': str(Path(path).resolve()), 'backup': ''}
    with closing(Source(path, cancel)) as source, closing(sqlite3.connect(target.as_uri() + '?mode=rw', uri=True, timeout=5)) as db:
        try:
            progress({'stage': '안전 백업 만드는 중', 'done': 0, 'total': 0})
            backup.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(pending_backup)) as copy:
                db.backup(copy, pages=256, progress=lambda *args: _check(cancel))
            _check(cancel)
            pending_backup.rename(backup)
            result['backup'] = str(backup)
            db.execute('BEGIN IMMEDIATE')
            for statement in SCHEMA:
                db.execute(statement)
            counts = _stage(source, db, cancel, progress, warnings)
            if convert_edits:
                for kind in ('Adobe_imageDevelopSettings','Adobe_libraryImageDevelopHistoryStep','Adobe_libraryImageDevelopSnapshot'):
                    warnings.pop(kind,None)
            result['convert_edits'] = bool(convert_edits)
            result.update(_photos(source, db, cancel, progress, warnings, counts['Adobe_images'],convert_edits))
            progress({'stage': '컬렉션과 스택 정리 중', 'done': 0, 'total': 0})
            result.update(_collections(source, db, cancel, warnings))
            _stacks(db, cancel, warnings)
            _check(cancel)
            result.update(source_key=source.key, source_counts=counts, warnings=dict(warnings), run_id=run_id)
            db.execute('INSERT INTO lightroom_import_runs VALUES(?,?,?,?,?)',
                       (run_id, datetime.now(timezone.utc).isoformat(), str(source.path), source.key, _json(result)))
            db.commit()
            return result
        except (ImportCancelled, sqlite3.OperationalError):
            db.rollback()
            if cancel is not None and cancel.is_set():
                return {**result, 'cancelled': True, 'imported': 0, 'reused': 0, 'skipped': 0, 'collections': 0}
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            # An interrupted SQLite backup must never appear as a restorable
            # .sqlite backup. Only this run's uniquely named partial is removed.
            if pending_backup.exists():
                pending_backup.unlink()


WARNING_LABELS = {
    'Adobe_imageDevelopSettings': 'Adobe 현상값 보관 (Luma 보정으로 변환하지 않음)',
    'Adobe_libraryImageDevelopHistoryStep': 'Adobe 보정 이력 보관 (실행 취소 이력으로 변환하지 않음)',
    'Adobe_libraryImageDevelopSnapshot': 'Adobe 스냅샷 보관 (Luma 스냅샷으로 변환하지 않음)',
    'AgLibraryCollectionStackImage': '컬렉션별 스택 정보 보관 (전역 스택으로 변환하지 않음)',
    'AgLibraryKeywordSynonym': '키워드 동의어 보관 (검색 키워드로 변환하지 않음)',
    'smart_collection': '스마트 컬렉션의 규칙 보관 (동적 검색은 적용하지 않음)',
    'keyword_cycle': '순환 키워드 계층 표시 제외',
    'keyword_separator': '구분 문자가 포함된 키워드 표시 제외',
    'keyword_parent': '부모를 찾지 못한 키워드',
    'unsupported_path_or_format': '경로 또는 파일 형식 미지원 사진 제외',
    'virtual_master': '원본 연결을 확인할 수 없는 가상 사본 제외',
    'collection_cycle': '순환 컬렉션 계층을 최상위에 배치',
    'collection_member': '가져올 수 없는 컬렉션 사진 참조',
    'stack_existing': '기존 Luma 사진이 포함된 스택 유지',
    'stack_reference': '연결이 없는 스택 참조 제외',
    'develop_unreadable': '읽을 수 없는 보정 상태 (원문만 보관)',
    'develop_empty': '변환 가능한 보정 항목이 없는 상태 (원문만 보관)',
    'develop_ambiguous': '현재 보정 상태가 여러 개인 사진 (현재 보정은 원문만 보관)',
}


def report_text(result):
    if result['cancelled']:
        return '가져오기를 취소했습니다. 이번에 추가한 기록은 모두 되돌렸습니다.'
    lines = [f'추가 {result["imported"]:,}장 · 이미 가져온 사진 {result["reused"]:,}장 · 제외 {result["skipped"]:,}장',
             f'추가한 컬렉션 {result["collections"]:,}개 · 동적 스마트 컬렉션 {result.get("converted_smart",0):,}개',
             '기존 Luma 사진의 보정과 분류는 유지했습니다.',
             '사진 원본과 Lightroom 카탈로그는 수정하지 않았습니다.']
    if result.get('convert_edits'):
        lines.extend([f'보정 변환 {result.get("converted_photos",0):,}장 · 이력 {result.get("converted_history",0):,}개 · 스냅샷 {result.get("converted_snapshots",0):,}개',
                      '보정값은 Luma의 현상 방식으로 적용하므로 Adobe 결과와 색·명암·효과 강도가 다를 수 있습니다.'])
    if result.get('warnings'):
        lines.append('\n확인이 필요한 항목:')
        for key,count in result['warnings'].items():
            label = ('미변환 보정: ' + key.split(':',1)[1] if key.startswith('develop_field:') else
                     '스마트 규칙 보관 이유: ' + key.split(':',1)[1] if key.startswith('smart_reason:') else WARNING_LABELS.get(key,key))
            lines.append(f'• {label}: {count:,}개')
    lines.append('\n안전 백업: ' + result['backup'])
    return '\n'.join(lines)
