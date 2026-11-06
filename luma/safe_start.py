"""Crash-safe start: a marker file while Grainy runs. Found at the next start, it means the run before ended without
closing (a crash, or a forced quit). Grainy then opens no photo by itself and offers a newer version first, so a fix
can reach a computer on which the old version quits as soon as it opens a photo (macOS, 0.6.6; see CHANGELOG 0.6.7).
"""
import json
import os
import time
from pathlib import Path

from . import __version__

NAME = 'running.json'


def begin(folder):
    """Leave this run's marker in Grainy's settings folder. Returns the record of the run before when that one ended
    without closing ({'pid', 'version', 'started'}), else None."""
    path = Path(folder)/NAME
    previous = None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and not _alive(data.get('pid')):
            previous = data
    except (OSError, ValueError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'pid': os.getpid(), 'version': __version__, 'started': time.time()}), encoding='utf-8')
    except OSError:
        pass
    return previous


def end(folder):
    """A normal close: remove this run's marker (not one another Grainy left)."""
    path = Path(folder)/NAME
    try:
        if json.loads(path.read_text(encoding='utf-8')).get('pid') == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _alive(pid):
    """Whether another Grainy that left the marker is still running (two windows on different libraries)."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    from .updater import wait_for_exit
    return not wait_for_exit(pid, 0)
