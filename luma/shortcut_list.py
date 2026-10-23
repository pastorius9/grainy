"""The window's keyboard shortcuts as one table. MainWindow.shortcuts() binds every key of it and the shortcut list
(Shift+/ or Settings) shows every key of it, so the list cannot differ from what the keys do."""
from collections import namedtuple
from PySide6.QtGui import QShortcut,QKeySequence
from PySide6.QtWidgets import QDialog,QDialogButtonBox,QGridLayout,QHBoxLayout,QLabel,QVBoxLayout
from .i18n import tr,keys as shown

# keys: the QKeySequences bound to the row; label: how the list writes them when that is not the keys themselves.
# run(window,key) is called with the key that was pressed, for the rows that cover several (rating, brush size).
# repeat: whether holding the key repeats it; not for the keys that switch a tool on or off or make a mask.
Shortcut=namedtuple('Shortcut','group keys text run label repeat')
GROUPS=('화면 보기','현상','마스크','분류','파일')


def row(group,sequences,text,run,label='',repeat=True):
    return Shortcut(group,tuple(sequences.split()),text,run,label,repeat)


# New Mask > kind of the Masks tab: its key and the text of the list. The tab writes the same keys beside its kinds.
MASKS=(('brush','K','새 브러시 마스크'),('linear','M','새 선형 그레이디언트 마스크'),('radial','Shift+M','새 방사형 그레이디언트 마스크'))

TABLE=(
    row('화면 보기','\\','원본 비교',lambda w,k:w.before_button.click()),
    row('화면 보기','C','비교 보기',lambda w,k:w.compare_button.click()),
    row('화면 보기','D','현상으로 이동',lambda w,k:w.set_mode(0)),
    row('화면 보기','G','라이브러리로 이동',lambda w,k:w.set_mode(1)),
    row('화면 보기','Shift+/','단축키 목록',lambda w,k:w.show_shortcuts(),'?',False),
    row('현상','R','크롭',lambda w,k:w.crop_button.click()),
    row('현상','W','화이트 밸런스 스포이드 · 드래그하면 영역 평균',lambda w,k:w.wb_shortcut(),repeat=False),
    row('현상','Q','힐링 브러시',lambda w,k:w.studio.heal_shortcut(),repeat=False),
    row('현상','Ctrl+Z','실행 취소',lambda w,k:w.undo()),
    row('현상','Ctrl+Shift+Z Ctrl+Y','다시 실행',lambda w,k:w.redo()),
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
    row('분류','B','빠른 컬렉션에 추가·제외',lambda w,k:w.manager.run(w.workflow.quick_toggle)),
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
            shortcut.activated.connect(lambda run=entry.run,key=key:window.safe_shortcut(lambda:run(window,key)))


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
