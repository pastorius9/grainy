"""Self-contained, content-addressed DCP assets inside the SQLite catalog.

Only storage uses references. Public settings, XMP and preset JSON carry the
original bytes, so they remain portable without an installed profile file.
"""
from collections import OrderedDict
from functools import lru_cache
import base64
import hashlib
import json
import re
import sqlite3

MAX_BYTES = 4 * 1024 * 1024
REF = 'data_ref'


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS profile_assets (
        sha256 TEXT PRIMARY KEY, data BLOB NOT NULL, size INTEGER NOT NULL)''')


def digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('카메라 프로파일의 체크섬이 올바르지 않습니다.')
    return value


@lru_cache(maxsize=4)
def decoded(encoded, sha):
    digest(sha)
    if not isinstance(encoded, str) or len(encoded) > ((MAX_BYTES + 2) // 3) * 4:
        raise ValueError('카메라 프로파일의 크기가 올바르지 않습니다.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('카메라 프로파일을 읽을 수 없습니다.') from error
    if not data or len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError('카메라 프로파일의 원본과 체크섬이 일치하지 않습니다.')
    # Preserve round trips exactly, including the encoded representation.
    if base64.b64encode(data).decode('ascii') != encoded:
        raise ValueError('카메라 프로파일의 인코딩이 올바르지 않습니다.')
    return data


def walk(value, transform):
    if isinstance(value, list):
        return [walk(item, transform) for item in value]
    if isinstance(value, dict):
        return {key: transform(item) if key == 'dcp_profile' and isinstance(item, dict)
                else walk(item, transform) for key, item in value.items()}
    return value


class ProfileCodec:
    def __init__(self, db):
        self.db = db
        self.clear()

    def clear(self):
        self._cache = OrderedDict()
        self._bytes = 0
        self._version = None

    def _asset_row(self, sha):
        # Reject corrupt oversized blobs in SQLite before copying them into Python.
        return self.db.execute('''SELECT CASE WHEN typeof(data)='blob' AND
            size BETWEEN 1 AND ? AND length(data)=size THEN data END,size
            FROM profile_assets WHERE sha256=?''', (MAX_BYTES, sha)).fetchone()

    def _read(self, sha, local):
        sha = digest(sha)
        if sha in local:
            return local[sha]
        version = (self.db.execute('PRAGMA data_version').fetchone()[0], self.db.total_changes)
        # Never retain uncommitted data: a rollback does not change total_changes.
        cacheable = not self.db.in_transaction
        if not cacheable or version != self._version:
            self.clear()
            self._version = version
        if sha in self._cache:
            self._cache.move_to_end(sha)
            local[sha] = self._cache[sha]
            return local[sha]
        try:
            row = self._asset_row(sha)
        except sqlite3.OperationalError as error:
            raise ValueError('카탈로그의 카메라 프로파일 보관함이 없습니다.') from error
        if row is None:
            raise ValueError(f'카탈로그의 카메라 프로파일 원본이 누락되었습니다: {sha[:12]}')
        data, size = row
        if (not isinstance(data, bytes) or not data or len(data) > MAX_BYTES or len(data) != size
                or hashlib.sha256(data).hexdigest() != sha):
            raise ValueError(f'카탈로그의 카메라 프로파일 원본이 손상되었습니다: {sha[:12]}')
        encoded = base64.b64encode(data).decode('ascii')
        local[sha] = encoded
        if cacheable:
            self._cache[sha] = encoded
            self._bytes += len(encoded)
            while len(self._cache) > 8 or self._bytes > 32 * 1024 * 1024:
                _, removed = self._cache.popitem(last=False)
                self._bytes -= len(removed)
        return encoded

    def expand(self, value):
        local = {}

        def profile(record):
            if REF not in record:
                return dict(record)
            sha = digest(record.get('sha256'))
            if record[REF] != sha or 'data' in record:
                raise ValueError('카메라 프로파일 참조가 올바르지 않습니다.')
            return {**{k: v for k, v in record.items() if k != REF}, 'data': self._read(sha, local)}

        return walk(value, profile)

    def pack(self, value, *, internal=False):
        assets = {}

        def profile(record):
            if REF in record:
                if not internal:
                    raise ValueError('외부 보정에는 카메라 프로파일 원본이 포함되어야 합니다.')
                # Validate existing references even in a mixed legacy payload.
                self.expand({'dcp_profile': record})
                return dict(record)
            sha = digest(record.get('sha256'))
            data = decoded(record.get('data'), sha)
            assets[sha] = data
            return {**{k: v for k, v in record.items() if k != 'data'}, REF: sha}

        packed = walk(value, profile)
        for sha, data in assets.items():
            row = self._asset_row(sha)
            if row is None:
                self.db.execute('INSERT INTO profile_assets VALUES(?,?,?)', (sha, data, len(data)))
            elif row[0] != data or row[1] != len(data):
                raise ValueError(f'기존 카메라 프로파일 원본이 손상되었습니다: {sha[:12]}')
        return packed

    def dumps(self, value):
        return json.dumps(self.pack(value))

    def loads(self, text):
        return self.expand(json.loads(text))


def payload_columns(db, *, all_preferences=False):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('photos', 'history', 'presets', 'snapshots'):
        if table in tables:
            yield table, 'edits', ''
    if 'preferences' in tables:
        yield 'preferences', 'value', ('' if all_preferences else
            " WHERE key LIKE 'undo:%' OR key='raw_defaults_v1'")


def references(value):
    found = set()

    def profile(record):
        if REF in record:
            sha = digest(record.get('sha256'))
            if sha != record[REF] or 'data' in record:
                raise ValueError('카메라 프로파일 참조가 올바르지 않습니다.')
            found.add(sha)
        return record

    walk(value, profile)
    return found


def verify_references(db, check_cancel=lambda: None):
    """Validate all reachable internal assets, including unknown preferences."""
    found = set()
    for table, column, where in payload_columns(db, all_preferences=True):
        for row in db.execute(f'SELECT {column} FROM {table}{where}'):
            check_cancel()
            found.update(references(json.loads(row[0])))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dust_entries'").fetchone():
        from .dust_store import unpack
        import zlib
        for row in db.execute('SELECT payload FROM dust_entries'):
            check_cancel()
            try:value=json.loads(unpack(row[0]))
            except (ValueError,UnicodeError,zlib.error):
                # A damaged checkpoint can conceal references. Keep all assets
                # until that photo is redetected instead of collecting them.
                found.update(r[0] for r in db.execute('SELECT sha256 FROM profile_assets'))
                continue
            found.update(references(value))
    codec = ProfileCodec(db)
    for sha in found:
        check_cancel()
        codec._read(sha, {})
    return found
