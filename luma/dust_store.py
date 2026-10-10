"""Transactional, per-photo dust review checkpoints in the catalog backup."""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import hashlib,json,zlib,math
from uuid import uuid4

from .dust import Options,checkpoint

VERSION=1
MAX_ENTRY_BYTES=16*1024*1024
STATES={'pending','ready','reviewed','failed','applied'}


def unpack(payload):
    if len(payload)>MAX_ENTRY_BYTES:raise ValueError('저장된 검토 크기가 올바르지 않습니다.')
    decoder=zlib.decompressobj();raw=decoder.decompress(payload,MAX_ENTRY_BYTES+1)
    if len(raw)>MAX_ENTRY_BYTES or not decoder.eof or decoder.unused_data:raise ValueError('저장된 검토 파일이 손상됐습니다.')
    return raw.decode('utf-8')


def migrate(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS dust_sessions (
            id TEXT PRIMARY KEY,version INTEGER NOT NULL,target_key TEXT NOT NULL,
            scope TEXT NOT NULL,snapshot TEXT NOT NULL,options TEXT NOT NULL,
            created TEXT NOT NULL,updated TEXT NOT NULL,current_index INTEGER NOT NULL,
            revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS dust_entries (
            session_id TEXT NOT NULL,ordinal INTEGER NOT NULL,photo_id INTEGER NOT NULL,
            path TEXT NOT NULL,state TEXT NOT NULL,payload BLOB NOT NULL,
            PRIMARY KEY(session_id,ordinal));
    ''')


def target_key(snapshot,scope):
    target=str(Path(snapshot['folder']).resolve()).casefold() if scope=='folder' and snapshot['folder'] else sorted(set(snapshot['scopes'][scope]))
    return hashlib.sha256(json.dumps([scope,target],ensure_ascii=False).encode()).hexdigest()


def read_work(directory,ident,cancel,progress):
    from .catalog import Catalog
    with Catalog.open_reader(directory) as catalog:
        return DustReviewStore(catalog).load(ident,cancel=cancel,progress=progress,verify_sources=True)


class DustReviewStore:
    def __init__(self,catalog):self.catalog=catalog;self.db=catalog.db

    @contextmanager
    def transaction(self,nested=False):
        if nested:
            if not self.db.in_transaction:raise RuntimeError('먼지 검토 저장 트랜잭션이 없습니다.')
            yield
        else:
            if self.db.in_transaction:raise RuntimeError('다른 저장 작업이 진행 중입니다.')
            with self.db:
                self.db.execute('BEGIN IMMEDIATE');yield

    def encode(self,entry):
        value={**entry,'state':'pending' if entry['state']=='scanning' else entry['state']}
        self.validate(value)
        raw=self.catalog.profiles.dumps(value).encode('utf-8')
        if len(raw)>MAX_ENTRY_BYTES:raise ValueError('이 사진의 검토 내용이 저장 한도를 넘었습니다.')
        return zlib.compress(raw,3),value['state']

    @staticmethod
    def validate(value):
        if not isinstance(value,dict) or value.get('state') not in STATES:raise ValueError('저장된 검토 상태가 올바르지 않습니다.')
        record=value['record']
        if not isinstance(record['id'],int) or not isinstance(record['path'],str) or not isinstance(record['settings'],dict):raise ValueError('저장된 사진 정보가 올바르지 않습니다.')
        if value['state'] in ('ready','reviewed','applied'):
            Options(**value['options']).validate()
            result=value['result']
            if not isinstance(result['spots'],list) or len(result['spots'])>5000 or min(result['width'],result['height'])<1:raise ValueError('저장된 먼지 후보가 올바르지 않습니다.')
            DustReviewStore.validate_spots(result['spots'])
        if value['state'] in ('reviewed','applied'):
            operation=value['operation'];review=value['review']
            if operation['type']!='dust' or not isinstance(operation['spots'],list) or len(operation['spots'])>6000:raise ValueError('저장된 제거 선택이 올바르지 않습니다.')
            if not isinstance(review,dict) or len(review['spots'])+len(review['pending'])>6000:raise ValueError('저장된 검토 목록이 올바르지 않습니다.')
            indices=review['checked']
            if any(type(i) is not int or not 0<=i<len(review['spots']) for i in indices):raise ValueError('저장된 후보 선택이 올바르지 않습니다.')
            if len(set(indices))!=len(indices):raise ValueError('저장된 후보 선택이 중복됐습니다.')
            DustReviewStore.validate_spots(review['spots']);DustReviewStore.validate_spots(review['pending'])
            if operation['spots']!=[review['spots'][i] for i in indices]:raise ValueError('검토한 후보와 제거 선택이 일치하지 않습니다.')
            method=operation.get('repair_method','smooth')
            if method not in ('smooth','texture') or review.get('repair_method','smooth')!=method:raise ValueError('저장된 복구 방식이 올바르지 않습니다.')

    @staticmethod
    def validate_spots(spots):
        from .dust_shapes import ellipses
        for spot in spots:
            for key in ('x','y','rx','ry'):
                value=spot[key]
                if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1 or key in ('rx','ry') and value==0:
                    raise ValueError('저장된 먼지 좌표가 올바르지 않습니다.')
            if spot['polarity'] not in ('dark','bright','manual') or not 25<=spot.get('scale',100)<=250:raise ValueError('저장된 먼지 선택이 올바르지 않습니다.')
            if 'parts' in spot:ellipses(spot,(100,100))

    def decode(self,payload):
        value=self.catalog.profiles.loads(unpack(payload));self.validate(value);return value

    def sessions(self):
        return [dict(row) for row in self.db.execute('''SELECT s.id,s.scope,s.snapshot,s.target_key,s.created,s.updated,
            COUNT(e.ordinal) AS total,SUM(e.state='reviewed') AS reviewed,SUM(e.state='applied') AS applied
            FROM dust_sessions s LEFT JOIN dust_entries e ON e.session_id=s.id
            GROUP BY s.id ORDER BY s.updated DESC,s.id''')]

    def create(self,snapshot,scope,options,entries,current=-1):
        Options(**options).validate();ident=uuid4().hex;now=datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self.db.execute('INSERT INTO dust_sessions VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident,VERSION,target_key(snapshot,scope),scope,json.dumps(snapshot,ensure_ascii=False),json.dumps(options),now,now,current,0))
            for index,entry in enumerate(entries):
                payload,state=self.encode(entry);record=entry['record']
                self.db.execute('INSERT INTO dust_entries VALUES(?,?,?,?,?,?)',(ident,index,record['id'],record['path'],state,payload))
        return ident,0

    def save(self,ident,revision,changes,current,*,nested=False):
        with self.transaction(nested):
            changed=self.db.execute('UPDATE dust_sessions SET revision=revision+1,updated=?,current_index=? WHERE id=? AND revision=? AND version=?',
                (datetime.now(timezone.utc).isoformat(),current,ident,revision,VERSION)).rowcount
            if changed!=1:raise ValueError('다른 창에서 검토 작업이 바뀌었습니다. 저장한 작업을 다시 열어 주세요.')
            for index,entry in changes:
                payload,state=self.encode(entry);record=entry['record']
                changed=self.db.execute('UPDATE dust_entries SET photo_id=?,path=?,state=?,payload=? WHERE session_id=? AND ordinal=?',
                    (record['id'],record['path'],state,payload,ident,index)).rowcount
                if changed!=1:raise ValueError('저장된 검토 목록이 바뀌었습니다. 다시 열어 주세요.')
        return revision+1

    def load(self,ident,*,cancel=None,progress=None,verify_sources=False):
        checkpoint(cancel)
        header=self.db.execute('SELECT * FROM dust_sessions WHERE id=?',(ident,)).fetchone()
        if not header or header['version']!=VERSION:raise ValueError('이 버전에서 읽을 수 없는 검토 작업입니다.')
        header=dict(header);header['snapshot']=json.loads(header['snapshot']);header['options']=json.loads(header['options'])
        snapshot=header['snapshot']
        if header['scope'] not in ('folder','selection') or not isinstance(snapshot,dict) or not isinstance(snapshot.get('scopes'),dict):raise ValueError('저장된 작업 대상이 올바르지 않습니다.')
        if snapshot.get('folder') is not None and not isinstance(snapshot['folder'],str):raise ValueError('저장된 폴더가 올바르지 않습니다.')
        for scope in ('folder','selection'):
            if not isinstance(snapshot['scopes'].get(scope),list) or any(type(i) is not int for i in snapshot['scopes'][scope]):raise ValueError('저장된 사진 목록이 올바르지 않습니다.')
        if header['target_key']!=target_key(snapshot,header['scope']):raise ValueError('저장된 작업 대상이 일치하지 않습니다.')
        Options(**header['options']).validate();entries=[];damaged=[]
        total=self.db.execute('SELECT COUNT(*) FROM dust_entries WHERE session_id=?',(ident,)).fetchone()[0]
        for row in self.db.execute('SELECT * FROM dust_entries WHERE session_id=? ORDER BY ordinal',(ident,)):
            checkpoint(cancel)
            if row['ordinal']!=len(entries):raise ValueError('저장된 검토 목록 순서가 올바르지 않습니다.')
            try:
                entry=self.decode(row['payload'])
                if entry['record']['id']!=row['photo_id'] or entry['record']['path']!=row['path'] or entry['state']!=row['state']:raise ValueError('검토 정보가 일치하지 않습니다.')
            except (ValueError,TypeError,KeyError,UnicodeError,zlib.error,OverflowError):
                from .dust_batch import capture_record
                from .engine import defaults
                try:record=capture_record(self.catalog,row['photo_id'])
                except ValueError:record=dict(id=row['photo_id'],path=row['path'],settings=defaults(),fingerprint=None)
                entry=dict(record=record,state='failed',result=None,options=None,operation=None,review=None,
                    error='저장된 검토 내용이 손상됐습니다. 이 사진을 다시 검출해 주세요.')
                damaged.append(row['ordinal'])
            if verify_sources and entry['state'] in ('pending','ready','reviewed'):
                from .dust_batch import verify_source
                try:verify_source(self.catalog,entry['record'])
                except ValueError as error:
                    entry.update(state='failed',error=str(error),operation=None,review=None);damaged.append(row['ordinal'])
            entries.append(entry)
            if progress:progress(len(entries),total)
        checkpoint(cancel)
        return header,entries,damaged

    def delete(self,ident,revision):
        with self.transaction():
            if self.db.execute('DELETE FROM dust_sessions WHERE id=? AND revision=?',(ident,revision)).rowcount!=1:
                raise ValueError('다른 창에서 검토 작업이 바뀌었습니다. 목록을 다시 열어 주세요.')
            self.db.execute('DELETE FROM dust_entries WHERE session_id=?',(ident,))
