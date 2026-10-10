"""Per-user folders. Windows keeps its %LOCALAPPDATA% layout; macOS uses Application Support/Grainy."""
import os
import sys
from pathlib import Path

MAC=sys.platform=='darwin'
# Bundled native libraries: assets/native/<name>.dll on Windows, <name>.dylib on macOS, none elsewhere.
NATIVE_SUFFIX='.dll' if os.name=='nt' else '.dylib' if MAC else None


def local_data(windows_folder,fallback=None):
    """The app's own folder: %LOCALAPPDATA%/<windows_folder>, or the one Grainy folder on macOS."""
    if MAC:return Path.home()/'Library'/'Application Support'/'Grainy'
    return Path(os.environ.get('LOCALAPPDATA',str(fallback or Path.home())))/windows_folder
