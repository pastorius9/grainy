"""Opt-in real Ask AI check; generated photos, temporary settings/library, free port.

Uses the installed programs and their existing subscriptions. Writes only result
metadata, never prompts, transcripts, tokens or raw provider diagnostics.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('program', choices=['claude', 'codex', 'gemini'])
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=90)
    parser.add_argument('--model', default='', help='optional installed-client model override')
    args = parser.parse_args()
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from PIL import Image
    from luma.app import configure_application
    from luma.desktop_session import DesktopSession
    from luma.preferences import Preferences
    from luma import mcp_service
    from luma.ai_dialog import AiDialog
    from luma.ai_programs import PROFILES
    app = QApplication([]); configure_application(app)
    root = Path(tempfile.mkdtemp(prefix='grainy-ask-ai-')).resolve()
    preferences = Preferences(root / 'settings/settings.json')
    preferences.save_language('ko'); preferences.save('update_check', False)
    mcp_service.pictures_folder = lambda: root / 'exports'
    session = DesktopSession(app, root / 'library', preferences)
    session.apply_language('ko'); session.open_window()
    assert session.mcp.start(0), 'Temporary MCP could not start'
    window = session.window
    window.folder_sync.set_enabled(False)
    photos = root / 'photos'; photos.mkdir()
    ids = []
    for n in range(3):
        path = photos / f'test-{n}.png'
        Image.new('RGB', (320, 240), (90 + n * 20, 120, 160)).save(path)
        ids.append(window.catalog.add(path))
    window.refresh_lists()
    profile = next(p for p in PROFILES if p.key == args.program)
    dialog = AiDialog(session.mcp, window, profiles=(profile,))
    window.ai_dialog = dialog
    dialog.show(); dialog.acknowledge()
    dialog.model.setText(args.model)
    report = {'program': args.program, 'temporary_root': str(root), 'port': session.mcp.bound_port,
              'resume_mode': profile.resume, 'model': args.model or 'default', 'phases': []}
    def wait(predicate, seconds):
        loop = QEventLoop(); poll = QTimer(); poll.setInterval(20)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline = QTimer(); deadline.setSingleShot(True); deadline.timeout.connect(loop.quit)
        poll.start(); deadline.start(int(seconds * 1000))
        if not predicate(): loop.exec()
        poll.stop(); deadline.stop()
        return predicate()
    def run(label, prompt, stop_after=None):
        tools = []; outcomes = []; errors = []
        def event(e):
            if e.kind == 'tool': tools.append(e.text)
            if e.kind in ('error', 'exit', 'launch_error'):
                # Classify without retaining account data or provider response text.
                text = e.text.lower()
                errors.append('authentication' if any(t in text for t in ('login', 'sign in', 'auth', '401'))
                              else 'quota' if any(t in text for t in ('limit', 'quota', '429')) else e.kind)
        dialog.runner.event.connect(event); dialog.runner.finished.connect(outcomes.append)
        started = time.monotonic()
        dialog.input.setPlainText(prompt); dialog.send()
        if stop_after is not None:
            QTimer.singleShot(int(stop_after * 1000), dialog.stop)
        if not wait(lambda: bool(outcomes), args.timeout):
            dialog.runner.shutdown(); wait(lambda: bool(outcomes), 3)
            errors.append('deadline')
        phase = {'phase': label, 'result': outcomes[-1] if outcomes else 'not_started',
                 'seconds': round(time.monotonic() - started, 2), 'tools': tools, 'errors': errors,
                 'exposure': window.catalog.photo(ids[0])['settings']['exposure']}
        report['phases'].append(phase)
        # Local diagnostics are kept only beneath these throwaway Grainy settings.
        # They are never printed or included in the committed result report.
        if dialog.runner.diagnostic:
            (root / 'settings' / (label + '-diagnostic.txt')).write_text(dialog.runner.diagnostic, encoding='utf-8')
        print(json.dumps(phase), flush=True)
        dialog.runner.event.disconnect(event); dialog.runner.finished.disconnect(outcomes.append)
        return phase
    try:
        wait(lambda: not window.library_loading, 15)
        first = run('edit', '사진 목록을 보고 첫 사진의 노출을 +0.3 올려줘. 다른 사진은 바꾸지 마. 짧게 답해.')
        if first['result'] == 'done' and abs(first['exposure'] - .3) < 1e-6:
            followup = run('followup', '방금 바꾼 같은 사진의 노출을 다시 +0.2 올려줘. 짧게 답해.')
            report['edit_verified'] = abs(followup['exposure'] - .5) < 1e-6
        else:
            report['edit_verified'] = False
        run('stop', '사진 목록을 확인하고 각 사진의 현재 보정값을 확인해 줘.', stop_after=.5)
        report['stopped'] = not dialog.runner.running
    finally:
        dialog.shutdown(); session.mcp.stop(); session.switching = True; window.close()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    return 0 if report['edit_verified'] and report['stopped'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
