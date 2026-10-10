"""Translate app-owned presentation text, never catalog keys or user content."""
import json
import re
import sys
from pathlib import Path

LANGUAGES = ('ko', 'en')
_language = 'ko'
_english = None


def language():
    return _language


def set_language(value):
    global _language
    if value not in LANGUAGES:
        raise ValueError('Unsupported interface language')
    _language = value


def keys(text):
    """Key names in a sentence as this system writes them: Qt's Ctrl is the Command key on macOS."""
    return re.sub(r'Ctrl\+?', '⌘', text) if sys.platform == 'darwin' else text


def tr(source, *values):
    global _english
    result = source
    if _language == 'en':
        if _english is None:
            path = Path(__file__).resolve().parents[1] / 'assets' / 'i18n' / 'en.json'
            _english = json.loads(path.read_text(encoding='utf-8'))
        result = _english.get(source, source)
    return result.format(*values) if values else result
