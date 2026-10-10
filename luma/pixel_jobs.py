"""Bounded row workers for independent pixel math, without approximation.

Only callers whose operations have no spatial neighbourhood use this helper.
Nested calls stay on the current worker; four concurrent image requests cannot
deadlock by filling the executor and waiting for more executor jobs.
"""
from concurrent.futures import ThreadPoolExecutor, wait
from threading import local
import os
import numpy as np

# Measured 0.5.45 on 24 threads: 8 workers cut full-size TIFF export 23%; 12+ gave no further gain.
WORKERS = min(8, max(1, os.cpu_count() or 1))
MIN_PIXELS = 128 * 1024
TILE_PIXELS = 64 * 1024
_pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='LumaPixels')
_context = local()


def rows(height, width, function, *, channels=3, dtype=np.float32):
    """Evaluate function(first, stop) into disjoint, bounded temporary tiles.

    At most WORKERS futures per caller are queued, irrespective of image size.
    Errors wait for sibling jobs before returning; none can outlive the buffers.
    The caller's NumPy floating-point error policy also applies on workers.
    """
    if _serial(height, width):
        return function(0, height)
    result = np.empty((height, width, channels), dtype=dtype)

    def tile(first, stop):
        result[first:stop] = function(first, stop)
    _run(height, width, tile)
    return result


def all_finite(array):
    """np.isfinite(array).all() on the row workers for images."""
    if array.ndim != 3:
        return bool(np.isfinite(array).all())
    finite = []
    each(array.shape[0], array.shape[1], lambda first, stop: finite.append(bool(np.isfinite(array[first:stop]).all())))
    return all(finite)


def run_all(functions):
    """Run independent callables on the workers and wait for all; serial inside a worker."""
    functions = list(functions)
    if WORKERS == 1 or len(functions) < 2 or getattr(_context, 'active', False):
        for function in functions:
            function()
        return
    policy = np.geterr()

    def work(function):
        _context.active = True
        try:
            with np.errstate(**policy):
                function()
        finally:
            _context.active = False
    futures = []
    try:
        futures = [_pool.submit(work, function) for function in functions]
        for future in futures:
            future.result()
    finally:
        wait(futures)


def each(height, width, function):
    """Call function(first, stop) over disjoint row ranges, for in-place work on caller-owned arrays."""
    if _serial(height, width):
        function(0, height)
    else:
        _run(height, width, function)


def _serial(height, width):
    return WORKERS == 1 or height * width < MIN_PIXELS or getattr(_context, 'active', False)


def _run(height, width, function):
    tile_rows = max(1, TILE_PIXELS // max(1, width))
    policy = np.geterr()

    def work(index):
        _context.active = True
        try:
            with np.errstate(**policy):
                for first in range(index * tile_rows, height, WORKERS * tile_rows):
                    function(first, min(first + tile_rows, height))
        finally:
            _context.active = False

    futures = []
    try:
        for index in range(min(WORKERS, (height + tile_rows - 1) // tile_rows)):
            futures.append(_pool.submit(work, index))
        for future in futures:
            future.result()
    finally:
        wait(futures)


def transform(array, function):
    """Float32 image result; scalar, vector and small inputs retain serial math."""
    if array.ndim != 3:
        return function(array)
    return rows(*array.shape[:2], lambda start, stop: function(array[start:stop]),
                channels=array.shape[2])
