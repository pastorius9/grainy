"""Bounded waits for read/compute work which an OS driver or decoder may not cancel.

Timed-out workers are detached, never write a catalog, and occupy their worker permit until
they actually stop. The request releases its preview lock immediately. This bounds stuck
workers without keeping HTTP handlers (or shutdown) waiting for a network driver.
"""
from concurrent.futures import Future,TimeoutError
import threading
import time

from .photo_service import Busy,ServiceError


def check_deadline(deadline):
    if time.monotonic()>=deadline:
        raise ServiceError('the time limit was reached; ask for fewer photos or try again')


def run(service,fn,deadline):
    check_deadline(deadline)
    if not service.io_slots.acquire(blocking=False):
        raise Busy('earlier file reads are still finishing')
    future=Future()
    def work():
        try:
            check_deadline(deadline)
            future.set_result(fn())
        except BaseException as error:future.set_exception(error)
        finally:service.io_slots.release()
    threading.Thread(target=work,name='GrainyMcpRead',daemon=True).start()
    try:return future.result(max(0.,deadline-time.monotonic()))
    except TimeoutError:
        raise ServiceError('the time limit was reached; ask for fewer photos or try again') from None
