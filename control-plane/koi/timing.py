"""Opt-in CLI stage durations; never include command payloads."""

from contextlib import contextmanager
import os
import sys
import time


@contextmanager
def stage(name):
    if os.environ.get("KOI_PROFILE") != "1":
        yield
        return
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed = (time.monotonic() - start) * 1000
        print(f"[koi profile] {name}: {elapsed:.3f} ms", file=sys.stderr, flush=True)
