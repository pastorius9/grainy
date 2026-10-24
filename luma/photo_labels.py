"""Atomic label batches sharing the AI undo stack, without changing develop histories."""
from datetime import datetime,timezone
import secrets

from . import photo_service as photos
from .command_batch import _undo_text,text_hash

FIELDS=('rating','flag','label')


def guard(catalog,ident):
    row=catalog.db.execute('SELECT path,edits FROM photos WHERE id=?',(ident,)).fetchone()
    return None if row is None else [row['path'],text_hash(row['edits']),text_hash(_undo_text(catalog,ident))]


def apply(catalog,entries):
    if catalog.db.in_transaction:raise photos.Busy('Grainy is saving something else')
    targets=list(dict.fromkeys(i for e in entries for i in e['ids']))
    rows={}
    for ident in targets:
        row=catalog.db.execute('SELECT id,rating,flag,label FROM photos WHERE id=?',(ident,)).fetchone()
        if row:rows[ident]={key:row[key] for key in FIELDS}
    values={ident:dict(row) for ident,row in rows.items()}
    for entry in entries:
        for ident in entry['ids']:
            if ident in values:values[ident].update(entry['fields'])
    changed=[i for i in rows if rows[i]!=values[i]]
    errors=[{'id':i,'error':'not in the catalog'} for i in targets if i not in rows]
    if not changed:return None,[],len(rows),errors
    index=photos.batch_index(catalog);taken={e['id'] for e in index};bid='b'+secrets.token_hex(3)
    while bid in taken:bid='b'+secrets.token_hex(3)
    with catalog.db:
        catalog.db.execute('BEGIN IMMEDIATE')
        records=[{'id':i,'before':rows[i],'after':values[i],'guard':guard(catalog,i)} for i in changed]
        for ident in changed:
            catalog.db.execute('UPDATE photos SET rating=?,flag=?,label=? WHERE id=?',
                               (*(values[ident][key] for key in FIELDS),ident))
        catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',
            (photos.BATCH_PREFIX+bid,catalog.profiles.dumps({'id':bid,'state':'applied','kind':'labels','records':records})))
        photos.remember_batch(catalog,index,{'id':bid,'tool':'set_labels','label':'AI 분류','count':len(changed),
            'created':datetime.now(timezone.utc).isoformat(),'state':'applied'})
    return bid,changed,len(rows)-len(changed),errors


def restore(catalog,bid,redo,flip):
    group=catalog.preference(photos.BATCH_PREFIX+bid,{})
    side='before' if redo else 'after';target='after' if redo else 'before'
    if group.get('state')!=('undone' if redo else 'applied'):
        raise photos.ServiceError('there is no batch to redo' if redo else 'there is no batch to undo')
    with catalog.db:
        catalog.db.execute('BEGIN IMMEDIATE')
        for record in group['records']:
            row=catalog.db.execute('SELECT rating,flag,label FROM photos WHERE id=?',(record['id'],)).fetchone()
            if not row or dict(row)!=record[side] or guard(catalog,record['id'])!=record['guard']:
                raise photos.ServiceError(f'{bid} can no longer be undone: some photos or labels changed afterwards')
        for record in group['records']:
            catalog.db.execute('UPDATE photos SET rating=?,flag=?,label=? WHERE id=?',
                               (*(record[target][key] for key in FIELDS),record['id']))
        group['state']='applied' if redo else 'undone'
        catalog.db.execute('INSERT OR REPLACE INTO preferences VALUES(?,?)',
                           (photos.BATCH_PREFIX+bid,catalog.profiles.dumps(group)))
        ids=[r['id'] for r in group['records']];flip(ids)
    return ids
