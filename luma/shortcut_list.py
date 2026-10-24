"""The window's keyboard shortcuts as one table. MainWindow.shortcuts() binds every key of it and the shortcut list
(Shift+/ or Settings) shows every key of it, so the list cannot differ from what the keys do."""
from collections import namedtuple
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import QDialog,QDialogButtonBox,QGridLayout,QHBoxLayout,QLabel,QVBoxLayout
from .i18n import tr,keys as shown

# keys: the QKeySequences bound to the row; label: how the list writes them when that is not the keys themselves.
# run(window,key) is called with the key that was pressed, for the rows that cover several (rating, brush size).
# repeat: whether holding the key repeats it; not for the keys that switch a tool on or off or make a mask.
# focus: the Tab keys. Qt moves the focus with them, so in a text field they still do that (window.safe_shortcut) and do
# not run the row.
Shortcut=namedtuple('Shortcut','group keys text run label repeat focus')
GROUPS=('화면 보기','현상','마스크','분류','파일')


def row(group,sequences,text,run,label='',repeat=True,focus=False):
    return Shortcut(group,tuple(sequences.split()),text,run,label,repeat,focus)


# New Mask > kind of the Masks tab: its key and the text of the list. The tab writes the same keys beside its kinds.
MASKS=(('brush','K','새 브러시 마스크'),('linear','M','새 선형 그레이디언트 마스크'),('radial','Shift+M','새 방사형 그레이디언트 마스크'))
# 6–9: the key of each colour label, by the name the catalog stores. The label menu writes the same keys beside its colours.
LABELS={'6':'빨강','7':'노랑','8':'초록','9':'파랑'}


def rotate(window,key):
    """Ctrl+[ and Ctrl+]: the photo menu's 90° turn of the selected photos."""
    from .photo_actions import transform_selected
    if window.selected_ids():window.manager.run(lambda:transform_selected(window,turns=1 if key.endswith(']') else -1))


TABLE=(
    row('화면 보기','\\','원본 비교',lambda w,k:w.before_button.click()),
    row('화면 보기','C','비교 보기',lambda w,k:w.compare_button.click()),
    row('화면 보기','D','현상으로 이동',lambda w,k:w.set_mode(0)),
    row('화면 보기','G','라이브러리로 이동',lambda w,k:w.set_mode(1)),
    row('화면 보기','J','클리핑 표시 켜기·끄기',lambda w,k:w.clipping_button.click(),repeat=False),
    row('화면 보기','Z','1:1과 화면 맞춤 번갈아 보기',lambda w,k:w.view.fit_photo() if not w.view.fit_mode else w.view.actual_size(),repeat=False),
    row('화면 보기','F','전체 화면 켜기·끄기 (Esc로 나옴)',lambda w,k:w.screen_modes.toggle_full_screen(),repeat=False),
    row('화면 보기','Tab','양옆 패널 숨기기·보이기',lambda w,k:w.screen_modes.toggle_sides(),repeat=False,focus=True),
    row('화면 보기','Shift+Tab','모든 패널 숨기기·보이기',lambda w,k:w.screen_modes.toggle_bars(),repeat=False,focus=True),
    row('화면 보기','L','조명 끄기: 사진만 보기 (Esc로 나옴)',lambda w,k:w.screen_modes.toggle_lights(),repeat=False),
    row('화면 보기','Shift+/ Ctrl+/','단축키 목록',lambda w,k:w.show_shortcuts(),'? / Ctrl+/',False),
    row('현상','R','크롭',lambda w,k:w.crop_button.click()),
    row('현상','W','화이트 밸런스 스포이드 · 드래그하면 영역 평균',lambda w,k:w.wb_shortcut(),repeat=False),
    row('현상','Q','힐링 브러시',lambda w,k:w.studio.heal_shortcut(),repeat=False),
    row('현상','Ctrl+Z','실행 취소',lambda w,k:w.undo()),
    row('현상','Ctrl+Shift+Z Ctrl+Y','다시 실행',lambda w,k:w.redo()),
    row('현상','Ctrl+U','자동 톤',lambda w,k:w.auto_button.click(),repeat=False),
    row('현상','Ctrl+Shift+U','자동 색',lambda w,k:w.auto_color_button.click(),repeat=False),
    row('현상','Ctrl+[ Ctrl+]','90° 회전 (왼쪽 · 오른쪽)',rotate,'Ctrl+[ / Ctrl+]'),
    row('현상','Ctrl+Shift+C','보정 복사',lambda w,k:w.copy_edits()),
    row('현상','Ctrl+Shift+V','보정 붙여넣기',lambda w,k:w.paste_edits()),
    row('현상','Ctrl+Alt+V','이전 사진 보정 붙이기',lambda w,k:w.manager.run(w.workflow.previous_edits)),
    *(row('마스크',key,text,lambda w,k,kind=kind:w.studio.mask_shortcut(kind),repeat=False) for kind,key,text in MASKS),
    row('마스크','O','마스크 오버레이 켜기·끄기',lambda w,k:w.studio.toggle_overlay()),
    row('마스크','[ ]','브러시 크기 줄이기 · 키우기',lambda w,k:w.view.resize_brush(k==']'),'[ / ]'),
    row('분류','P','선택 표시',lambda w,k:w.set_flag(1)),
    row('분류','X','제외 표시',lambda w,k:w.set_flag(-1)),
    row('분류','U','표시 해제',lambda w,k:w.set_flag(0)),
    row('분류','0 1 2 3 4 5','별점 0–5',lambda w,k:w.set_rating(int(k)),'0 – 5'),
    row('분류','6 7 8 9','색 라벨: 빨강 · 노랑 · 초록 · 파랑 (같은 키를 한 번 더 누르면 해제)',lambda w,k:w.manager.run(lambda:w.manager.toggle_label(LABELS[k])),'6 – 9',False),
    row('분류','B','빠른 컬렉션에 추가·제외',lambda w,k:w.manager.run(w.workflow.quick_toggle)),
    row('분류',"Ctrl+'",'가상 사본 만들기',lambda w,k:w.manager.run(w.manager.virtual_copy),repeat=False),
    row('파일','Ctrl+O','사진 가져오기',lambda w,k:w.import_files()),
    row('파일','Ctrl+Shift+E','내보내기…',lambda w,k:w.export_dialog()),
    row('파일','Ctrl+E','이전 설정으로 내보내기',lambda w,k:w.quick_export('previous')),
)


def bind(window):
    """A QShortcut for every key of the table. All of them go through window.safe_shortcut, so none acts while a
    text field has the focus."""
    for entry in TABLE:
        for key in entry.keys:
            shortcut=QShortcut(QKeySequence(key),window);shortcut.setAutoRepeat(entry.repeat)
            forward=('Shift' not in key) if entry.focus else None
            shortcut.activated.connect(lambda run=entry.run,key=key,forward=forward:window.safe_shortcut(lambda:run(window,key),forward))


def written(entry):
    """The keys of a row as the list and the menus write them."""
    return shown(entry.label or ' / '.join(entry.keys))


def key_hint(key):
    """The key of the one-key row `key` ("K"), for a tooltip or a menu: Ctrl is the Command key on macOS."""
    return shown(next(e for e in TABLE if key in e.keys).label or key)


def columns(groups):
    """The groups split in two runs, in order, with about the same number of rows each."""
    sizes=[len(entries) for _,entries in groups];best=min(range(1,len(groups)),key=lambda i:abs(sum(sizes[:i])-sum(sizes[i:])))
    return groups[:best],groups[best:]


class ShortcutDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent)
        from .app import section
        self.setObjectName('shortcutList');self.setWindowTitle(tr('단축키'))
        layout=QVBoxLayout(self);row_box=QHBoxLayout();layout.addLayout(row_box)
        for part in columns([(group,[e for e in TABLE if e.group==group]) for group in GROUPS]):
            column=QVBoxLayout();row_box.addLayout(column,1)
            for group,entries in part:
                column.addWidget(section(group));grid=QGridLayout();grid.setColumnMinimumWidth(0,150);grid.setColumnStretch(1,1)
                for number,entry in enumerate(entries):
                    key=QLabel(written(entry));key.setObjectName('shortcutKey');grid.addWidget(key,number,0)
                    grid.addWidget(QLabel(tr(entry.text)),number,1)
                column.addLayout(grid)
            column.addStretch()
        note=QLabel(tr('글자를 입력하는 칸에서는 단축키가 동작하지 않습니다. 도구를 쓰는 중에는 Esc로 끝냅니다.'))
        note.setObjectName('muted');note.setWordWrap(True);layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Close);buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr('닫기'));layout.addWidget(buttons)
        self.resize(760,self.sizeHint().height())
