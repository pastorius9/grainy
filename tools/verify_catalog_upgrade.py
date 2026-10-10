"""Read-only upgrade audit against an explicit SQLite backup."""
import argparse,sqlite3,json,hashlib,os,sys
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('version')
parser.add_argument('--allow-folder-recovery',action='store_true')
# The user's library since 2026-10-09 (the installed app's default); pass the project's data folder for the old one.
parser.add_argument('--library',type=Path,default=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'Luma'/'Library')
args=parser.parse_args();args.library=args.library.resolve()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
backup=args.library/'backups'/f'before-release-{args.version}.sqlite'
current=args.library/'catalog.sqlite'
a=sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)
b=sqlite3.connect(current.as_uri()+'?mode=ro',uri=True)
a.row_factory=b.row_factory=sqlite3.Row
a.execute('BEGIN');b.execute('BEGIN')
report={'version':args.version,'integrity':b.execute('PRAGMA integrity_check').fetchone()[0],'tables':{}}
assert report['integrity']=='ok'
from luma.profile_store import verify_references
report['referenced_profiles']=len(verify_references(b))
report['profile_references_valid']=True
report['stored_profiles']=b.execute('SELECT COUNT(*) FROM profile_assets').fetchone()[0]
recovery=None
if args.allow_folder_recovery:
    row=b.execute("SELECT value FROM preferences WHERE key='folder_recovery_last'").fetchone()
    assert row,'No recorded folder recovery'
    recovery=json.loads(row[0]);assert recovery['moves']
    from luma.folder_locations import rebase
    def restored_path(path):
        for move in recovery['moves']:path=rebase(path,move['source'],move['destination'])
        return path
    report['folder_recovery']=recovery
for table in ('photos','history','presets','folder_roots','collections','collection_members','snapshots','dust_sessions','dust_entries'):
    order='1,2' if table=='dust_entries' else '1'
    before=[dict(r) for r in a.execute(f'SELECT * FROM {table} ORDER BY {order}')] if a.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() else []
    after=[dict(r) for r in b.execute(f'SELECT * FROM {table} ORDER BY {order}')] if b.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone() else []
    report['tables'][table]={'before':len(before),'after':len(after),'identical':before==after}
    if table=='folder_roots' and recovery:
        assert {restored_path(r['path']) for r in before}=={r['path'] for r in after}
        report['tables'][table]['verified_location_changes_only']=True
        continue
    assert len(before)==len(after),table
    if table=='photos':
        changes={key for x,y in zip(before,after) for key in x if x[key]!=y[key]}
        report['changed_photo_columns']=sorted(changes)
        assert changes<= ({'metadata','path'} if recovery else {'metadata'}),changes
        if recovery:
            changed=0
            for x,y in zip(before,after):
                expected=restored_path(x['path']);assert y['path']==expected
                changed+=x['path']!=y['path']
            assert changed==recovery['photos'],(changed,recovery['photos'])
            report['photo_locations_recovered']=changed
        report['photo_paths_edits_and_classification_preserved']=True
    else:assert before==after,table
before=a.execute("SELECT key,value FROM preferences WHERE key LIKE 'undo:%' ORDER BY key").fetchall()
after=b.execute("SELECT key,value FROM preferences WHERE key LIKE 'undo:%' ORDER BY key").fetchall()
assert before==after
report['undo_preferences_preserved']=True
installed=Path(os.environ['LOCALAPPDATA'])/'Programs'/'Grainy'/'Grainy.exe'
release=root/'release'/'Grainy'/'Grainy.exe'
for key,path in [('installed_sha256',installed),('release_sha256',release)]:
    report[key]=hashlib.sha256(path.read_bytes()).hexdigest()
assert report['installed_sha256']==report['release_sha256']
for library,key in [('luma_lcms2','native_color_sha256'),('luma_dcp','native_dcp_sha256'),('grainy_hdr','native_hdr_sha256')]:
    native_relative=Path(f'_internal/assets/native/{library}.dll')
    if (release.parent/native_relative).is_file():
        native_release=hashlib.sha256((release.parent/native_relative).read_bytes()).hexdigest()
        native_installed=hashlib.sha256((installed.parent/native_relative).read_bytes()).hexdigest()
        native_manifest=json.loads((root/f'assets/native/{library}.json').read_text(encoding='utf-8'))
        assert native_release==native_installed==native_manifest['sha256']
        report[key]=native_installed
(root/'validation'/f'catalog-after-{args.version}.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
