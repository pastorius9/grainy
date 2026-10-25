"""Find CLI tools after installation, including GUI apps with an older PATH."""
import os
from pathlib import Path
import shutil
import sys


def tool_folders(platform=None):
    platform = sys.platform if platform is None else platform
    home = Path.home()
    folders = [home / '.local' / 'bin']
    if platform == 'win32':
        local = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local')
        roaming = Path(os.environ.get('APPDATA') or home / 'AppData' / 'Roaming')
        folders += [local / 'Programs' / 'OpenAI' / 'Codex' / 'bin', roaming / 'npm',
                    Path(os.environ.get('ProgramFiles') or 'C:/Program Files') / 'nodejs',
                    local / 'Programs' / 'nodejs', local / 'Microsoft' / 'WindowsApps']
    else:
        folders += [Path('/opt/homebrew/bin'), Path('/usr/local/bin'), home / '.npm-global' / 'bin']
    if os.environ.get('CODEX_INSTALL_DIR'):
        folders.append(Path(os.environ['CODEX_INSTALL_DIR']))
    return folders


def find_tool(name, *, platform=None):
    found = shutil.which(name)
    if found:
        return found
    platform = sys.platform if platform is None else platform
    suffixes = ('.exe', '.cmd', '.bat', '') if platform == 'win32' and not Path(name).suffix else ('',)
    for folder in tool_folders(platform):
        for suffix in suffixes:
            path = folder / (name + suffix)
            if path.is_file() and (platform == 'win32' or os.access(path, os.X_OK)):
                return str(path)
    return None


def install_environment(platform):
    """Only the child PATH changes; no shell profiles or CLI settings are read."""
    env = os.environ.copy()
    separator = ';' if platform == 'win32' else ':'
    paths = [env.get('PATH', ''), *(str(p) for p in tool_folders(platform))]
    env['PATH'] = separator.join(p for p in paths if p)
    return env
