"""Photo context actions shared by the library and filmstrip."""
from .i18n import tr
import sys
from copy import deepcopy
from PySide6.QtWidgets import QMenu
from .engine import defaults


def edit_selected(window,change,label,fit=False):
    w=window;w.commit();ids=w.selected_ids()
    for ident in ids:
        values=deepcopy(w.catalog.photo(ident)['settings'])
        change(values)
        w.manager.edit_photo(ident,values,label)
        if ident==w.current_id:
            state=w.catalog.preference(f'undo:{ident}',{})
            w.undo_stack=deepcopy(state.get('undo',[]));w.redo_stack=deepcopy(state.get('redo',[]))
            w.settings=w.catalog.photo(ident)['settings'];w.last_saved=deepcopy(w.settings)
    w.end_crop();w.before_button.setChecked(False);w.load_controls();w.render_version+=1
    if fit:w.view.fit_photo()
    w.render()
    w.statusBar().showMessage(tr('{0}장의 사진에 적용했습니다. 사진별 실행 취소로 되돌릴 수 있습니다.', f'{len(ids)}'))


def transform_selected(window,turns=None,flip=False):
    def change(values):
        if turns is not None:values.update(rotation=(values['rotation']+turns)%4,crop=None)
        if flip:values['flip']=not values['flip']
    edit_selected(window,change,'사진 반전' if flip else '사진 회전',fit=True)


def reset_selected(window):
    window.studio.upright_generation+=1
    edit_selected(window,lambda values:values.update(defaults()),'보정 초기화',fit=True)


def build_menu(w):
    menu=QMenu(w);ids=w.selected_ids();photos=[w.catalog.photo(i) for i in ids];manager=w.manager;flow=w.workflow
    def add(parent,text,key,callback,enabled=True):
        action=parent.addAction(tr(text));action.triggered.connect(lambda checked=False:manager.run(callback))
        action.setData(key);action.setEnabled(bool(ids) and enabled);return action
    def choice(parent,text,key,callback,values,target):
        action=add(parent,text,key,callback);action.setCheckable(True)
        action.setChecked(bool(values) and all(v==target for v in values));return action
    add(menu,'현상에서 크게 보기','develop',lambda:w.open_library_photo(w.photo_model.index(w.photo_model.positions[w.current_id])))
    add(menu,'내보내기…','export',w.export_dialog,not w.export_running)
    from .quick_export import previous
    quick=menu.addMenu(tr('빠른 내보내기'));quick.menuAction().setData('quick-export')
    quick.setEnabled(bool(ids) and not w.export_running)
    for text,kind in [('JPG (작은 크기 · 2048px)','small'),('JPG (원본 크기)','large'),('원본 + 설정','original')]:
        action=add(quick,text,'quick-export-'+kind,lambda k=kind:w.quick_export(k),not w.export_running)
        if kind=='original':action.setToolTip(tr('원본 파일과 Grainy 보정값 XMP를 함께 복사합니다.'))
    quick.addSeparator()
    add(quick,'이전 설정으로 내보내기','export-previous',lambda:w.quick_export('previous'),
        not w.export_running and previous(w.catalog) is not None)
    add(quick,'사용자 지정 설정…','export-custom',w.export_dialog,not w.export_running)
    add(menu,'내보내기 기록…','export-history',flow.show_exports)
    menu.addSeparator()
    add(menu,'시계 방향으로 90° 회전','rotate-right',lambda:transform_selected(w,turns=1))
    add(menu,'반시계 방향으로 90° 회전','rotate-left',lambda:transform_selected(w,turns=-1))
    add(menu,'좌우 반전','flip',lambda:transform_selected(w,flip=True))
    add(menu,'상하 반전','flip-vertical',lambda:transform_selected(w,turns=2,flip=True))
    add(menu,'카메라 방향으로 복원','camera-orientation',flow.camera_orientation,
        any(p['settings']['rotation'] or p['settings']['flip'] for p in photos))
    menu.addSeparator()
    add(menu,'보정 복사','copy',w.copy_edits)
    add(menu,'보정 붙여넣기','paste',w.paste_edits,bool(w.copied))
    edits=menu.addMenu(tr('보정 설정'))
    add(edits,'복사할 보정 항목 선택…','copy-options',manager.choose_copy)
    add(edits,'현재 보정을 선택 사진에 동기화','sync',manager.sync_selected,len(ids)>1)
    add(edits,'이전 사진 보정 붙이기','previous-edits',flow.previous_edits,
        flow.previous_id is not None and any(i!=flow.previous_id for i in ids))
    add(edits,'자동 화이트 밸런스','auto-white-balance',flow.auto_white,flow.cancel is None)
    add(edits,'현재 사진에 총노출 맞추기','match-exposure',flow.match_exposure,len(ids)>1)
    for text,value,key in [('흑백으로 전환',True,'monochrome'),('컬러로 전환',False,'color')]:
        choice(edits,text,key,lambda n=value:edit_selected(w,lambda v:v.update(monochrome=n),'흑백 / 컬러 전환'),[p['settings']['monochrome'] for p in photos],value)
    add(edits,'현재 보정 스냅샷 저장…','snapshot',manager.snapshot)
    add(edits,'스냅샷 불러오기…','restore-snapshot',manager.restore_snapshot,bool(w.catalog.snapshots(w.current_id)))
    edits.addSeparator();add(edits,'선택 사진 보정 초기화','reset',lambda:reset_selected(w))
    add(menu,'가상 사본 만들기','virtual-copy',manager.virtual_copy)
    stacks=menu.addMenu(tr('스택'))
    add(stacks,'선택 사진을 스택으로 묶기','stack',manager.stack,len(ids)>1)
    add(stacks,'선택 사진 스택 해제','unstack',manager.unstack,any(p['stack_id'] is not None for p in photos))
    add(stacks,'스택 펼쳐 보기' if manager.collapsed else '스택 접어서 보기','toggle-stacks',manager.stack_action.trigger)
    current=w.catalog.photo(w.current_id) if w.current_id is not None else None
    add(stacks,'현재 사진을 스택 대표로','stack-cover',flow.stack_cover,bool(current and current['stack_id'] is not None))
    rating=menu.addMenu(tr('별점'))
    for value in range(6):choice(rating,'별점 해제' if value==0 else '★'*value,f'rating-{value}',lambda n=value:w.set_rating(n),[p['rating'] for p in photos],value)
    flag=menu.addMenu(tr('선택 표시'))
    for text,value in [('선택',1),('제외',-1),('표시 해제',0)]:choice(flag,text,f'flag-{value}',lambda n=value:w.set_flag(n),[p['flag'] for p in photos],value)
    label=menu.addMenu(tr('색 라벨'))
    for value in ('','빨강','노랑','초록','파랑','보라'):choice(label,value or '라벨 해제',f'label-{value}',lambda n=value:manager.set_label(n),[p['label'] for p in photos],value)
    collections=menu.addMenu(tr('컬렉션'))
    add(collections,'빠른 컬렉션에 추가·제외','quick-collection',flow.quick_toggle)
    add(collections,'컬렉션에 추가…','collection-add',manager.add_to_collection,any(c['rules'] is None for c in w.catalog.collections()))
    add(collections,'선택 사진으로 새 컬렉션…','collection-new',lambda:manager.create_collection(False))
    ordinary=any(c['id']==manager.collection and c['rules'] is None for c in w.catalog.collections())
    add(collections,'현재 컬렉션에서 제외','collection-remove',manager.remove_from_collection,ordinary)
    compare=menu.addMenu(tr('비교 보기'))
    add(compare,'선택 사진 비교','compare',lambda:manager.compare(2),len(ids)>1)
    add(compare,'선택 사진 모아보기','survey',lambda:manager.compare(12),len(ids)>1)
    add(compare,'현재 사진 보정 전·후','before-after',lambda:manager.compare(2,before=True))
    metadata=menu.addMenu(tr('메타데이터'))
    for text,key,fn in [('메타데이터·키워드 편집…','metadata',manager.metadata),('촬영 시각 일괄 이동…','capture-time',manager.shift_date),('XMP 저장','write-xmp',manager.write_xmp),('XMP 불러오기','read-xmp',manager.read_xmp)]:add(metadata,text,key,fn)
    metadata.addSeparator()
    add(metadata,'메타데이터 프리셋 적용…','metadata-preset',flow.apply_metadata)
    add(metadata,'바로가기 키워드 적용','shortcut-keyword',flow.apply_keyword)
    files=menu.addMenu(tr('파일 관리'))
    for text,key,fn in [('파일 이름 변경…','rename',manager.rename),('다른 폴더로 이동…','move',manager.move),('누락 원본 다시 연결…','relink',manager.relink),('오프라인 편집용 미리보기 만들기','smart-preview',manager.smart_previews),('미리보기 다시 만들기','rebuild-preview',lambda:w.preview_queue.start_batch(w.selected_ids()))]:add(files,text,key,fn)
    menu.addSeparator();add(menu,'Finder에서 폴더 열기' if sys.platform=='darwin' else '탐색기에서 폴더 열기','show-folder',w.show_file)
    remove=menu.addMenu(tr('사진 제거'))
    add(remove,'카탈로그에서만 제거…','remove',lambda:manager.remove(False))
    add(remove,'원본을 휴지통으로…','trash',lambda:manager.remove(True))
    return menu
