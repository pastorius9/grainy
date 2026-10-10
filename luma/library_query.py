"""Cancellable catalog queries. No source decode or edits loaded for browsing."""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from .folders import path_key
from .library import matches


class QueryCancelled(Exception):pass


def connect(path):
    db=sqlite3.connect(path,timeout=10)
    db.row_factory=sqlite3.Row
    return db


def sync_index(db,cancel):
    """Refresh small derived columns; triggers retain changes from all writers."""
    while True:
        if cancel.is_set():raise QueryCancelled()
        # One short write transaction prevents a changed photo from being
        # removed from the dirty queue while its old values are indexed.
        db.execute('BEGIN IMMEDIATE')
        try:
            rows=db.execute('''SELECT p.id,p.path,p.name,p.keywords,p.metadata,p.extras
                FROM browser_dirty d JOIN photos p ON p.id=d.id LIMIT 512''').fetchall()
            if not rows:db.commit();break
            values=[]
            for p in rows:
                folder=str(Path(p['path']).parent)
                info={**json.loads(p['metadata'] or '{}'),**json.loads(p['extras'] or '{}')}
                date=str(info.get('DateTimeOriginal',info.get('date',''))).replace(':','-',2)
                values.append((p['id'],folder,path_key(folder),p['name'].casefold(),p['keywords'].casefold(),date))
            db.executemany('INSERT OR REPLACE INTO browser_index VALUES(?,?,?,?,?,?)',values)
            db.executemany('DELETE FROM browser_dirty WHERE id=?',[(p['id'],) for p in rows])
            db.commit()
        except BaseException:db.rollback();raise


def query(path,options,cancel):
    db=connect(path)
    try:
        sync_index(db,cancel)
        db.set_progress_handler(lambda:1 if cancel.is_set() else 0,1000)
        db.execute('BEGIN')
        where=[];params=[]
        text=options.get('search','').casefold()
        if text:where.append('(instr(i.name_key,?)>0 OR instr(i.keywords_key,?)>0)');params.extend([text,text])
        folder=options.get('folder')
        if folder:
            key=path_key(folder)
            if options.get('recursive',True):
                prefix=key.rstrip('\\/')+os.sep
                escaped=prefix.replace('~','~~').replace('%','~%').replace('_','~_')+'%'
                where.append("(i.folder_key=? OR i.folder_key LIKE ? ESCAPE '~')");params.extend([key,escaped])
            else:where.append('i.folder_key=?');params.append(key)
        mode=options.get('mode','all')
        if mode=='picked':where.append('p.flag=1')
        elif mode=='rejected':where.append('p.flag=-1')
        elif mode=='rated':where.append('p.rating>=4')
        rules=[options.get('rules') or {}]
        collection=options.get('collection')
        if collection is not None:
            row=db.execute('SELECT rules FROM collections WHERE id=?',(collection,)).fetchone()
            if row is None:where.append('0')
            elif row['rules'] is not None:rules.append(json.loads(row['rules']))
            else:
                where.append('EXISTS(SELECT 1 FROM collection_members cm WHERE cm.collection_id=? AND cm.photo_id=p.id)')
                params.append(collection)
        query_time=datetime.now()
        for number,rule in enumerate(rules):
            if not rule:continue
            from .smart_rules import KEY,sql_condition
            if KEY in rule:
                compiled=sql_condition(rule[KEY])
                if compiled is not None:
                    expression,arguments=compiled
                    where.append(expression);params.extend(arguments)
                    rule={k:v for k,v in rule.items() if k!=KEY}
                    if not rule:continue
            def match(ident,path,name,keywords,rating,flag,label,virtual,metadata,extras,imported,stack,rules=rule):
                if cancel.is_set():return 0
                return int(matches(dict(id=ident,path=path,name=name,keywords=keywords,rating=rating,flag=flag,label=label,
                    virtual_source=virtual,metadata=metadata,extras=extras,imported=imported,stack_id=stack),rules,now=query_time))
            name=f'browser_match_{number}';db.create_function(name,12,match)
            where.append(f'{name}(p.id,p.path,p.name,p.keywords,p.rating,p.flag,p.label,p.virtual_source,p.metadata,p.extras,p.imported,p.stack_id)=1')
        sort={'imported':'p.id','name':'i.name_key','capture':'i.capture_key','rating':'p.rating'}.get(options.get('sort'),'p.id')
        direction='DESC' if options.get('descending') else 'ASC'
        sql='SELECT p.id,p.stack_id FROM photos p JOIN browser_index i ON i.id=p.id'
        if where:sql+=' WHERE '+' AND '.join(where)
        sql+=f' ORDER BY {sort} {direction},p.id {direction}'
        cursor=db.execute(sql,params);rows=[]
        while batch:=cursor.fetchmany(4096):
            if cancel.is_set():raise QueryCancelled()
            rows.extend(batch)
        # Saved stack orders keep a chosen cover first and members together.
        from .workflow_data import arrange_stacks
        ids=arrange_stacks(rows,options.get('stack_orders') or {},bool(options.get('collapsed')))
        folders=[{'path':str(Path(r['folder'])/'__count__'),'count':r['count']}
            for r in db.execute('SELECT MIN(folder) folder,COUNT(*) count FROM browser_index GROUP BY folder_key')]
        roots=[r[0] for r in db.execute('SELECT path FROM folder_roots ORDER BY path COLLATE NOCASE')]
        return {'ids':ids,'folders':folders,'roots':roots,'total':sum(p['count'] for p in folders)}
    except sqlite3.OperationalError:
        if cancel.is_set():raise QueryCancelled() from None
        raise
    finally:db.close()


def read_page(path,ids):
    if not ids:return {}
    db=connect(path)
    try:return {r['id']:dict(r) for r in db.execute(
        'SELECT id,path,name,rating,flag,label FROM photos WHERE id IN ('+','.join('?' for _ in ids)+')',ids)}
    finally:db.close()
