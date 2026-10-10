"""Resumable profile deduplication with a consistent backup and bounded batches."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from threading import Event
from uuid import uuid4
from .profile_store import ProfileCodec, migrate, payload_columns, verify_references


class Cancelled(Exception):
    pass


def optimize(directory, cancel=None, progress=lambda value: None, *, batch_size=16):
    directory = Path(directory)
    cancel = cancel if cancel is not None else Event()
    report = dict(converted=0, examined=0, total=0, removed_assets=0, profiles=0,
                  cancelled=False, vacuumed=False, warnings=[], backup=None)

    def check():
        if cancel.is_set():
            raise Cancelled()

    def publish(stage):
        progress(dict(stage=stage, examined=report['examined'], total=report['total']))

    def size(db):
        return db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]

    def warning(owner, error):
        # Bounded diagnostic output even for a badly damaged large catalog.
        report['warning_count'] = report.get('warning_count', 0) + 1
        if len(report['warnings']) < 20:
            report['warnings'].append(f'{owner}: {error}')

    with closing(sqlite3.connect(directory / 'catalog.sqlite', timeout=1)) as db:
        db.row_factory = sqlite3.Row
        report['before_bytes'] = size(db)
        try:
            check()
            publish('안전 백업 중')
            backup_dir = directory / 'backups'
            backup_dir.mkdir(exist_ok=True)
            target = backup_dir / f'before-optimize-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.sqlite'
            partial = target.with_suffix('.sqlite.partial')
            try:
                with closing(sqlite3.connect(partial)) as output:
                    db.backup(output, pages=128, progress=lambda *_: check(), sleep=.05)
                check()
                partial.replace(target)
            finally:
                # Only this job's incomplete backup; never touch prior backups.
                partial.unlink(missing_ok=True)
            report['backup'] = str(target)
            with db:
                migrate(db)
            codec = ProfileCodec(db)
            columns = list(payload_columns(db))
            report['total'] = sum(db.execute(f'SELECT COUNT(*) FROM {table}{where}').fetchone()[0]
                                  for table, _, where in columns)
            preview = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='preview_state'").fetchone())
            for table, column, where in columns:
                last = None
                while True:
                    check()
                    # Parenthesize the preference predicate before adding rowid.
                    base = ' WHERE (' + where[7:] + ')' if where else ''
                    condition = (base + (' AND ' if base else ' WHERE ') + 'rowid>?') if last is not None else base
                    query = f'SELECT rowid AS rid FROM {table}{condition} ORDER BY rowid LIMIT ?'
                    rows = db.execute(query, (last, batch_size) if last is not None else (batch_size,)).fetchall()
                    if not rows:
                        break
                    converted = 0
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        for row in rows:
                            check()
                            # Another writer may have changed this row after SELECT.
                            current = db.execute(f'SELECT {column} FROM {table} WHERE rowid=?', (row['rid'],)).fetchone()
                            if current is None:
                                continue
                            try:
                                value = json.loads(current[0])
                                db.execute('SAVEPOINT profile_row')
                                packed = codec.pack(value, internal=True)
                                if packed != value:
                                    token = db.execute('SELECT token FROM preview_state WHERE photo_id=?',
                                                       (row['rid'],)).fetchone() if table == 'photos' and preview else None
                                    db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?',
                                               (json.dumps(packed), row['rid']))
                                    if token:
                                        db.execute('UPDATE preview_state SET token=? WHERE photo_id=?', (token[0], row['rid']))
                                    converted += 1
                                db.execute('RELEASE profile_row')
                            except (ValueError, TypeError) as error:
                                if db.in_transaction:
                                    # JSON may have failed before the savepoint.
                                    try:
                                        db.execute('ROLLBACK TO profile_row')
                                        db.execute('RELEASE profile_row')
                                    except sqlite3.OperationalError:
                                        pass
                                warning(f'{table}/{row["rid"]}', error)
                            report['examined'] += 1
                        check()
                    report['converted'] += converted
                    last = rows[-1]['rid']
                    publish('중복 프로파일 정리 중')
            check()
            publish('프로파일 연결 검사 중')
            removed = 0
            with db:
                db.execute('BEGIN IMMEDIATE')
                try:
                    used = verify_references(db, check)
                    # Unreadable rows can conceal references; never collect then.
                    if not report.get('warning_count'):
                        for row in db.execute('SELECT sha256 FROM profile_assets').fetchall():
                            check()
                            if row[0] not in used:
                                db.execute('DELETE FROM profile_assets WHERE sha256=?', (row[0],))
                                removed += 1
                except (ValueError, TypeError) as error:
                    warning('프로파일 연결 검사', error)
            report['removed_assets'] = removed
            check()
            publish('빈 공간 회수 중')
            db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            try:
                db.execute('VACUUM')
                report['vacuumed'] = True
            except sqlite3.OperationalError as error:
                if cancel.is_set():
                    raise Cancelled() from error
                warning('빈 공간 회수', error)
            finally:
                db.set_progress_handler(None, 0)
            check()
        except Cancelled:
            report['cancelled'] = True
        finally:
            db.rollback()
            db.set_progress_handler(None, 0)
        publish('무결성 확인 중')
        report['integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        report['after_bytes'] = size(db)
        report['saved_bytes'] = max(0, report['before_bytes'] - report['after_bytes'])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='profile_assets'").fetchone():
            report['profiles'] = db.execute('SELECT COUNT(*) FROM profile_assets').fetchone()[0]
        report['checkpoint'] = list(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
    return report
