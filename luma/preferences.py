"""Application preferences independent of any photo library."""
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from .i18n import LANGUAGES
from .user_paths import local_data


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else local_data('Grainy') / 'settings.json'
        self.values = {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                self.values = data
        except (OSError, ValueError):
            pass

    @property
    def language(self):
        value = self.values.get('language')
        return value if value in LANGUAGES else None

    @property
    def use_gpu(self):
        return self.values.get('use_gpu') is not False

    def save_language(self, value):
        if value not in LANGUAGES:
            raise ValueError('Unsupported interface language')
        self.save('language', value)

    def save_use_gpu(self, value):
        self.save('use_gpu', bool(value))

    def save(self, key, value):
        updated = {**self.values, key: value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False, suffix='.tmp') as output:
                temporary = Path(output.name)
                json.dump(updated, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.values = updated
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
