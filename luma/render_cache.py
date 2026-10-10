"""Bounded intermediate image cache, owned by one preview worker.

Source arrays must be immutable while bound. Switching source objects invalidates
all entries; source references prevent recycled object ids from matching old data.
Exports do not use this cache.
"""
from collections import OrderedDict
import json
import os


def physical_memory():
    """Installed RAM in bytes, or None when it cannot be read."""
    try:
        if os.name=='nt':
            import ctypes
            class Status(ctypes.Structure):
                _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),('total',ctypes.c_ulonglong),('available',ctypes.c_ulonglong),
                          ('page_total',ctypes.c_ulonglong),('page_available',ctypes.c_ulonglong),('virtual_total',ctypes.c_ulonglong),
                          ('virtual_available',ctypes.c_ulonglong),('extended',ctypes.c_ulonglong)]
            status=Status();status.length=ctypes.sizeof(status)
            return int(status.total) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
        return os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
    except (OSError,ValueError,AttributeError):
        return None


def settings_key(settings, keys=None):
    value = settings if keys is None else {key: settings[key] for key in keys}
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class DevelopmentCache:
    def __init__(self, max_bytes=128*1024*1024, per_source=0, ceiling=None):
        """per_source>0 grows the budget to that many copies of each bound source, up to ceiling.

        A full-resolution frame larger than the fixed budget otherwise disables the cache and
        every slider event recomputes the whole chain.
        """
        self.base_bytes = self.max_bytes = max(0, int(max_bytes))
        self.per_source = max(0, per_source)
        self.ceiling = self.base_bytes if ceiling is None else max(self.base_bytes, int(ceiling))
        self.bytes = 0
        self.hits = self.misses = 0
        self.source = None
        self.version = 0   # changes with the bound source; keys of results kept outside entries include it
        self.entries = OrderedDict()

    def bind(self, source):
        if source is not self.source:
            self.entries.clear()
            self.bytes = 0
            self.source = source
            self.version += 1
            if self.per_source:
                wanted = int(self.per_source * getattr(source, 'nbytes', 0))
                self.max_bytes = max(self.base_bytes, min(wanted, self.ceiling))

    def evaluate(self, stage, key, calculate):
        value = self.lookup(stage, key)
        return value if value is not None else self.store(stage, key, calculate())

    def lookup(self, stage, key):
        """The cached value, or None after dropping a stale entry for stage."""
        previous = self.entries.pop(stage, None)
        if previous is not None:
            if previous[0] == key:
                self.entries[stage] = previous
                self.hits += 1
                return previous[1]
            self.bytes -= previous[1].nbytes
        return None

    def discard(self, *stages):
        for stage in stages:
            previous = self.entries.pop(stage, None)
            if previous is not None:
                self.bytes -= previous[1].nbytes

    def store(self, stage, key, value):
        self.misses += 1
        # Keep the prefix that already fits. Evicting it to store later stages
        # causes a full chain of misses on every subsequent slider event.
        if self.bytes + value.nbytes <= self.max_bytes and len(self.entries) < 64:
            value.setflags(write=False)
            self.entries[stage] = (key, value)
            self.bytes += value.nbytes
        return value
