"""Optional features that are switched off in the public build."""

# None: read the user's own settings file; tests set True or False.
CODEX = None


def codex():
    """The ChatGPT/Codex command window. Hidden unless settings.json has "codex": true, because it
    needs a separately installed Codex program and account that most people do not have."""
    if CODEX is not None:
        return CODEX
    from .preferences import Preferences
    return Preferences().values.get('codex') is True
