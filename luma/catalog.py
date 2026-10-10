"""Local SQLite catalog. Never modifies an imported source file."""
from __future__ import annotations
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from datetime import datetime, timezone
from .engine import defaults, normalized
from .folders import contains_folder, in_folder, path_key, stored_path


class Catalog:
    @classmethod
    @contextmanager
    def open_reader(cls, directory):
        """Independent, read-only snapshot for a worker; never runs migrations."""
        reader=cls.__new__(cls);reader.directory=Path(directory)
        path=(reader.directory/'catalog.sqlite').resolve()
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
            db.row_factory=sqlite3.Row;reader.db=db
            from .profile_store import ProfileCodec
            reader.profiles=ProfileCodec(db)
            db.execute('BEGIN')
            yield reader

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.thumbs = self.directory / 'thumbnails'
        self.thumbs.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.directory / 'catalog.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 0, flag INTEGER NOT NULL DEFAULT 0,
                keywords TEXT NOT NULL DEFAULT '', edits TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
                imported TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL,
                label TEXT NOT NULL, edits TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS presets (name TEXT PRIMARY KEY, edits TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS folder_roots (path TEXT PRIMARY KEY COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS collections (id INTEGER PRIMARY KEY,name TEXT NOT NULL,parent_id INTEGER,rules TEXT);
            CREATE TABLE IF NOT EXISTS collection_members (collection_id INTEGER,photo_id INTEGER,PRIMARY KEY(collection_id,photo_id));
            CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY,photo_id INTEGER,name TEXT,edits TEXT,created TEXT);
        ''')
        self._migrate_photos()
        self._migrate_browser()
        from .preview_store import migrate
        migrate(self.db)
        self._migrate_profiles()
        from .dust_store import migrate as migrate_dust
        migrate_dust(self.db)
        # Old catalogs contain original paths but no explicit import roots.
        roots=self.folder_roots()
        parents={str(Path(row['path']).parent) for row in self.db.execute('SELECT path FROM photos')} if not roots else set()
        for folder in parents:
            if not any(contains_folder(root,folder) for root in roots):
                self.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(folder,))
                roots.append(folder)
        self.db.commit()

    def add(self, path,settings=None,metadata=None):
        path = stored_path(Path(path).resolve())
        previous=self.db.execute('SELECT id FROM photos WHERE path=? COLLATE NOCASE AND virtual_source IS NULL',(path,)).fetchone()
        if previous:
            return previous['id']
        folder=str(Path(path).parent)
        with self.db:
            if not any(contains_folder(root,folder) for root in self.folder_roots()):
                self.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(folder,))
            payload=self.profiles.dumps(normalized(settings or {}))
            self.db.execute('INSERT INTO photos(path,name,edits,metadata,imported) VALUES(?,?,?,?,?)',
                            (path, Path(path).name, payload,json.dumps(metadata or {}), datetime.now(timezone.utc).isoformat()))
        return self.db.execute('SELECT id FROM photos WHERE path=? AND virtual_source IS NULL', (path,)).fetchone()['id']

    def photos(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM photos ORDER BY id')]

    def paths(self):return [r[0] for r in self.db.execute('SELECT DISTINCT path FROM photos')]

    def update_many(self,ids,field,value):
        if field not in ('rating','flag','label','stack_id'):raise ValueError('지원하지 않는 일괄 항목입니다.')
        with self.db:self.db.executemany(f'UPDATE photos SET {field}=? WHERE id=?',[(value,i) for i in ids])

    def _migrate_browser(self):
        exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='browser_index'").fetchone()
        backup=self.directory/'backups'/'before-browser-v3.sqlite'
        if not exists and not backup.exists() and self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]:
            self.backup(backup)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS browser_index (
                id INTEGER PRIMARY KEY,folder TEXT NOT NULL,folder_key TEXT NOT NULL,
                name_key TEXT NOT NULL,keywords_key TEXT NOT NULL,capture_key TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS browser_folder ON browser_index(folder_key COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS browser_name ON browser_index(name_key,id);
            CREATE INDEX IF NOT EXISTS browser_capture ON browser_index(capture_key,id);
            CREATE INDEX IF NOT EXISTS photos_rating ON photos(rating,id);
            CREATE INDEX IF NOT EXISTS photos_flag ON photos(flag,id);
            CREATE TABLE IF NOT EXISTS browser_dirty (id INTEGER PRIMARY KEY);
            CREATE TRIGGER IF NOT EXISTS browser_added AFTER INSERT ON photos BEGIN
                INSERT OR IGNORE INTO browser_dirty VALUES(NEW.id); END;
            CREATE TRIGGER IF NOT EXISTS browser_changed AFTER UPDATE OF path,name,keywords,metadata,extras ON photos BEGIN
                INSERT OR IGNORE INTO browser_dirty VALUES(NEW.id); END;
            CREATE TRIGGER IF NOT EXISTS browser_removed AFTER DELETE ON photos BEGIN
                DELETE FROM browser_index WHERE id=OLD.id;
                DELETE FROM browser_dirty WHERE id=OLD.id; END;
        ''')
        if not exists:
            self.db.execute('INSERT OR IGNORE INTO browser_dirty SELECT id FROM photos');self.db.commit()

    def photo(self, photo_id):
        row = self.db.execute('SELECT * FROM photos WHERE id=?', (photo_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result['settings'] = normalized(self.profiles.loads(result['edits']))
        result['info'] = json.loads(result['metadata'])
        result['user_metadata']=json.loads(result.get('extras') or '{}')
        return result

    def update(self, photo_id, **fields):
        allowed = {'rating', 'flag', 'keywords', 'metadata','label','stack_id','extras','name'}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            self.db.execute('UPDATE photos SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(), photo_id))
            self.db.commit()

    def edit(self, photo_id, settings, label='보정'):
        previous = self.db.execute('SELECT edits,extras FROM photos WHERE id=?', (photo_id,)).fetchone()
        values=normalized(settings)
        if not previous or normalized(self.profiles.loads(previous['edits']))==values:return
        with self.db:
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            payload=self.profiles.dumps(values)
            self.db.execute('SAVEPOINT previous_profile')
            try:
                previous_payload=json.dumps(self.profiles.pack(json.loads(previous['edits']),internal=True))
            except (ValueError,TypeError):
                # Allow repairing an invalid legacy profile without losing its history.
                self.db.execute('ROLLBACK TO previous_profile')
                previous_payload=previous['edits']
            self.db.execute('RELEASE previous_profile')
            self.db.execute('INSERT INTO history(photo_id,label,edits,created) VALUES(?,?,?,?)',
                            (photo_id, label, previous_payload, datetime.now(timezone.utc).isoformat()))
            meta=json.loads(previous['extras'] or '{}')
            meta['_last_edit_time']=datetime.now(timezone.utc).isoformat()
            self.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?', (payload,json.dumps(meta,ensure_ascii=False),photo_id))

    def set_settings(self, photo_id, settings):
        previous=self.photo(photo_id);values=normalized(settings)
        if previous is None or previous['settings']==values:return
        with self.db:
            meta=previous['user_metadata'];meta['_last_edit_time']=datetime.now(timezone.utc).isoformat()
            self.db.execute('UPDATE photos SET edits=?,extras=? WHERE id=?', (self.profiles.dumps(values),json.dumps(meta,ensure_ascii=False),photo_id))

    def histories(self, photo_id):
        return [{**dict(r),'settings':self.profiles.loads(r['edits'])} for r in
                self.db.execute('SELECT * FROM history WHERE photo_id=? ORDER BY id DESC LIMIT 80', (photo_id,))]

    def save_preset(self, name, settings):
        values = normalized(settings)
        for key in ('crop', 'rotation', 'straighten', 'flip'):
            values[key] = defaults()[key]
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO presets VALUES(?,?)', (name, self.profiles.dumps(values)))

    def save_adobe_preset(self,name,preset):
        from .adobe_preset import KEY,translate
        checked=translate(preset[KEY])
        if not checked['mapped']:raise ValueError('변환 가능한 프리셋 보정이 없습니다.')
        value={**checked['settings'],KEY:preset[KEY]}
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO presets VALUES(?,?)',(name,self.profiles.dumps(value)))

    def presets(self):
        return {r['name']: self.profiles.loads(r['edits']) for r in self.db.execute('SELECT * FROM presets ORDER BY name')}

    def folder_roots(self):
        return [r['path'] for r in self.db.execute('SELECT path FROM folder_roots ORDER BY path COLLATE NOCASE')]

    def register_folder(self,path):
        path=stored_path(Path(path).resolve())
        self.db.execute('INSERT OR IGNORE INTO folder_roots VALUES(?)',(path,))
        self.db.commit()
        removed=self.removed_folders()
        if any(path_key(p)==path_key(path) for p in removed):
            self.save_preference('removed_folders',[p for p in removed if path_key(p)!=path_key(path)])
        return path

    def removed_folders(self):
        """Folders the user took out of the catalog. Inside a registered folder they are neither
        listed nor imported automatically, until one is imported again on request."""
        return self.preference('removed_folders',[]) or []

    def folder_photo_ids(self,folder):
        return [r['id'] for r in self.db.execute('SELECT id,path FROM photos') if in_folder(r['path'],folder)]

    def remove_folder(self,folder):
        """Take a folder out of the catalog in one step: the photos in it and in its sub-folders
        (with their edits), the registered folders at or below it and their automatic-import
        entries. No file is touched. Returns the ids of the removed photos."""
        folder=stored_path(Path(folder))
        ids=self.folder_photo_ids(folder)
        removed=[p for p in self.removed_folders() if not contains_folder(folder,p)]+[folder]
        watches=[p for p in self.preference('watch_folders',[]) or [] if not contains_folder(folder,p)]
        with self.db:
            self._delete_photos(ids)
            for root in self.folder_roots():
                if contains_folder(folder,root):self.db.execute('DELETE FROM folder_roots WHERE path=?',(root,))
            for key,value in [('removed_folders',removed),('watch_folders',watches)]:
                self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,json.dumps(value,ensure_ascii=False)))
        return ids

    def restore_folder(self,folder):
        """The user imports this folder: whatever was removed at or below it is wanted again."""
        removed=self.removed_folders()
        kept=[p for p in removed if not contains_folder(folder,p)]
        if len(kept)!=len(removed):self.save_preference('removed_folders',kept)

    def preference(self,key,default=None):
        row=self.db.execute('SELECT value FROM preferences WHERE key=?',(key,)).fetchone()
        return self.profiles.loads(row['value']) if row else default

    def save_preference(self,key,value):
        with self.db:
            payload=self.profiles.dumps(value) if key.startswith('undo:') or key=='raw_defaults_v1' else json.dumps(value)
            self.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',(key,payload))

    def _migrate_profiles(self):
        from .profile_store import migrate,ProfileCodec
        exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_assets'").fetchone()
        backup=self.directory/'backups'/'before-profile-assets-v1.sqlite'
        if not exists and not backup.exists() and self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]:
            self.backup(backup)
        with self.db:migrate(self.db)
        self.profiles=ProfileCodec(self.db)

    def close(self):
        self.db.close()

    def _migrate_photos(self):
        columns={r[1] for r in self.db.execute('PRAGMA table_info(photos)')}
        if 'virtual_source' in columns:return
        # Removing the old UNIQUE path constraint allows non-destructive copies.
        # Keep a consistent SQLite backup before touching an existing catalog.
        if self.db.execute('SELECT COUNT(*) FROM photos').fetchone()[0]:
            self.backup(self.directory/'backups'/'before-library-v2.sqlite')
        with self.db:
            self.db.execute('''CREATE TABLE photos_v2 (
                id INTEGER PRIMARY KEY,path TEXT NOT NULL,name TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 0,flag INTEGER NOT NULL DEFAULT 0,
                keywords TEXT NOT NULL DEFAULT '',edits TEXT NOT NULL,metadata TEXT NOT NULL DEFAULT '{}',
                imported TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',stack_id INTEGER,
                virtual_source INTEGER,extras TEXT NOT NULL DEFAULT '{}')''')
            self.db.execute('INSERT INTO photos_v2(id,path,name,rating,flag,keywords,edits,metadata,imported) SELECT id,path,name,rating,flag,keywords,edits,metadata,imported FROM photos')
            self.db.execute('DROP TABLE photos')
            self.db.execute('ALTER TABLE photos_v2 RENAME TO photos')
            self.db.execute('CREATE INDEX photos_path ON photos(path COLLATE NOCASE)')
            self.db.execute('CREATE INDEX photos_stack ON photos(stack_id)')

    def backup(self,destination):
        destination=Path(destination).resolve()
        if destination==self.directory.joinpath('catalog.sqlite').resolve():raise ValueError('카탈로그 원본에는 백업할 수 없습니다.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        self.db.commit()
        with closing(sqlite3.connect(destination)) as output:self.db.backup(output)
        return destination

    def virtual_copy(self,photo_id):
        p=self.photo(photo_id)
        with self.db:
            payload=json.dumps(self.profiles.pack(json.loads(p['edits']),internal=True))
            cursor=self.db.execute('''INSERT INTO photos(path,name,rating,flag,keywords,edits,metadata,imported,label,virtual_source,extras)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',(p['path'],p['name']+' · 사본',p['rating'],p['flag'],p['keywords'],payload,p['metadata'],
                datetime.now(timezone.utc).isoformat(),p['label'],p['virtual_source'] or p['id'],p['extras']))
        return cursor.lastrowid

    def snapshot(self,photo_id,name):
        p=self.photo(photo_id)
        with self.db:
            payload=json.dumps(self.profiles.pack(json.loads(p['edits']),internal=True))
            self.db.execute('INSERT INTO snapshots(photo_id,name,edits,created) VALUES(?,?,?,?)',
                (photo_id,name,payload,datetime.now(timezone.utc).isoformat()))

    def snapshots(self,photo_id):
        return [{**dict(r),'settings':self.profiles.loads(r['edits'])} for r in
                self.db.execute('SELECT * FROM snapshots WHERE photo_id=? ORDER BY id DESC',(photo_id,))]

    def add_collection(self,name,rules=None,parent_id=None):
        c=self.db.execute('INSERT INTO collections(name,parent_id,rules) VALUES(?,?,?)',(name,parent_id,json.dumps(rules) if rules is not None else None))
        self.db.commit();return c.lastrowid

    def collections(self):return [dict(r) for r in self.db.execute('SELECT * FROM collections ORDER BY name')]

    def collection_add(self,collection_id,ids):
        self.db.executemany('INSERT OR IGNORE INTO collection_members VALUES(?,?)',[(collection_id,i) for i in ids]);self.db.commit()

    def collection_ids(self,collection_id):
        from .library import matches
        c=self.db.execute('SELECT * FROM collections WHERE id=?',(collection_id,)).fetchone()
        if c is None:return set()
        if c['rules'] is not None:return {p['id'] for p in self.photos() if matches(p,json.loads(c['rules']))}
        return {r[0] for r in self.db.execute('SELECT photo_id FROM collection_members WHERE collection_id=?',(collection_id,))}

    def remove_collection(self,ident):
        with self.db:
            self.db.execute('UPDATE collections SET parent_id=NULL WHERE parent_id=?',(ident,))
            self.db.execute('DELETE FROM collection_members WHERE collection_id=?',(ident,))
            self.db.execute('DELETE FROM collections WHERE id=?',(ident,))

    def remove_photos(self,ids):
        with self.db:self._delete_photos(ids)

    def _delete_photos(self,ids):
        ids=list(ids)
        for start in range(0,len(ids),500):                 # a whole folder at once: few passes over each table
            part=ids[start:start+500];marks=','.join('?'*len(part))
            for table in ('history','snapshots','collection_members'):self.db.execute(f'DELETE FROM {table} WHERE photo_id IN ({marks})',part)
            self.db.execute(f'DELETE FROM preferences WHERE key IN ({marks})',[f'undo:{ident}' for ident in part])
            self.db.execute(f'DELETE FROM photos WHERE id IN ({marks})',part)

    def stack(self,ids):
        if not ids:return
        ident=min(ids)
        self.db.executemany('UPDATE photos SET stack_id=? WHERE id=?',[(ident,i) for i in ids]);self.db.commit()

    def set_user_metadata(self,photo_id,values):
        current=self.photo(photo_id)['user_metadata'];current.update(values)
        self.update(photo_id,extras=json.dumps(current,ensure_ascii=False))

    def delete_preset(self,name):
        self.db.execute('DELETE FROM presets WHERE name=?',(name,));self.db.commit()
