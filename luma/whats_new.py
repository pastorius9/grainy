"""What changed, shown once when a newer version is opened for the first time.

The text is the change log that ships with the app: CHANGELOG.md (Korean) and assets/whats-new-en.md
(English, shorter; a version without an English entry falls back to the Korean one). Nothing is fetched.
The preference 'version_seen' remembers the last version that was opened.

assets/feedback-done.json maps a release to the feedback references it addressed; a reference sent from
this computer (feedback.SENT) is thanked for once, above the notes (preference 'feedback_thanked')."""
import json
import re
from pathlib import Path
from . import __version__
from .feedback import SENT
from .i18n import language, tr

ROOT = Path(__file__).resolve().parents[1]
HEADING = re.compile(r'^## (\d+(?:\.\d+)+)[ \t]*(?:—[ \t]*(.*?))?[ \t]*$', re.M)
LIMIT = 5                                    # versions listed at most, newest first
KEY = 'version_seen'
THANKED = 'feedback_thanked'


def version_key(text):
    return tuple(int(part) for part in re.findall(r'\d+', str(text or ''))[:4])


def sections(text):
    """[(version, title, body)] of a change log, in the order written."""
    found = list(HEADING.finditer(text)); result = []
    for index, match in enumerate(found):
        end = found[index+1].start() if index+1 < len(found) else len(text)
        result.append((match.group(1), match.group(2) or '', text[match.end():end].strip()))
    return result


def _read(path):
    try:return sections(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):return []


def notes(previous, current=__version__, lang=None, root=ROOT):
    """Sections newer than `previous` up to `current`, newest first; only `current` when `previous` is unknown."""
    korean = _read(root/'CHANGELOG.md')
    english = {version: (version, title, body) for version, title, body in _read(root/'assets'/'whats-new-en.md')}
    low, high = version_key(previous), version_key(current)
    chosen = [entry for entry in korean if version_key(entry[0]) <= high and (version_key(entry[0]) > low if low else version_key(entry[0]) == high)]
    chosen.sort(key=lambda entry: version_key(entry[0]), reverse=True)
    if (lang or language()) == 'en':chosen = [english.get(entry[0], entry) for entry in chosen]
    return chosen[:LIMIT]


def markdown(entries):
    return '\n\n'.join(f'## {version}' + (f' — {title}' if title else '') + f'\n\n{body}' for version, title, body in entries)


def pending(preferences, first_run=False, current=__version__):
    """The entries to show now, or []. A first run (nothing to compare with) and a downgrade show nothing."""
    seen = preferences.values.get(KEY)
    if first_run or seen == current or (seen and version_key(seen) >= version_key(current)):return []
    return notes(seen, current)


def remember(preferences, current=__version__):
    if preferences.values.get(KEY) == current:return
    try:preferences.save(KEY, current)
    except OSError:pass


def done(root=ROOT):
    """{version: [references]}: the feedback each release addressed."""
    try:data = json.loads((root/'assets'/'feedback-done.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):return {}
    return data if isinstance(data, dict) else {}


def _listed(preferences, key):
    value = preferences.values.get(key); return value if isinstance(value, list) else []


def _sent(preferences):
    return [entry['ref'] for entry in _listed(preferences, SENT) if isinstance(entry, dict) and isinstance(entry.get('ref'), str)]


def shipped(preferences, previous, current=__version__, root=ROOT):
    """References sent from this computer that the versions after `previous` up to `current` addressed, not thanked
    for yet. The range of notes(); the same version or a downgrade gives []."""
    low, high = version_key(previous), version_key(current)
    if low and low >= high:return []
    addressed = {str(ref).lstrip('#').upper() for version, refs in done(root).items() if isinstance(refs, list)
                 and (low < version_key(version) <= high if low else version_key(version) == high) for ref in refs}
    thanked = set(_listed(preferences, THANKED))
    return [ref for ref in dict.fromkeys(_sent(preferences)) if ref in addressed and ref not in thanked]


def thank(preferences, refs):
    """Remember the references thanked for, so the line never repeats; those no longer recorded as sent are dropped."""
    if not refs:return
    sent = set(_sent(preferences)); kept = [ref for ref in _listed(preferences, THANKED) if ref in sent and ref not in refs]
    try:preferences.save(THANKED, kept+list(refs))
    except OSError:pass


def thanks(refs):
    return tr('보내 주신 피드백({0})이 이번 업데이트에 반영되었습니다. 고맙습니다.', ', '.join(f'#{ref}' for ref in refs))


def show(parent, entries=None, refs=()):
    """The dialog; without entries, this version's own notes (Settings menu). `refs`: shipped feedback, thanked for above the notes."""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTextBrowser, QVBoxLayout
    entries = notes(None) if entries is None else entries
    dialog = QDialog(parent); dialog.setObjectName('whatsNew'); dialog.setWindowTitle(tr('Grainy {0}의 달라진 점', __version__))
    layout = QVBoxLayout(dialog)
    heading = QLabel(tr('Grainy가 {0} 버전으로 업데이트되었습니다.', __version__)); heading.setWordWrap(True); layout.addWidget(heading)
    if refs:
        line = QLabel(thanks(refs)); line.setObjectName('shipped'); line.setWordWrap(True); layout.addWidget(line)
    view = QTextBrowser(); view.setOpenLinks(False)
    view.setMarkdown(markdown(entries) if entries else tr('이 버전의 변경 기록을 찾지 못했습니다.'))
    layout.addWidget(view, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); buttons.rejected.connect(dialog.reject); buttons.accepted.connect(dialog.accept)
    buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr('닫기'))
    layout.addWidget(buttons); dialog.resize(640, 540)
    dialog.exec()
    return dialog
