"""One computation per key, which every other request for the key waits for: ``science.cache.DatasetCache``'s loads
and derived tables, and ``science.recon.ProductCache``'s products.

A cache puts a ``Pending`` in its table of computations for each key it claims (under its own lock), then runs
``Pending.settle`` outside the lock. Whatever fails once the computation starts (the computation, or the cache's
bookkeeping after it), the keys are released and every waiter is woken, so no key is left computing. A waiter gets
the value, or its own copy of the computation's error raised from it (``Pending.wait``), never the object the
computing thread raised: one object raised in many threads would grow a traceback in each.

A coroutine waits with ``Pending.wait_async`` (an ``async`` route waiting for a dataset load): in its
event loop, holding no thread; the computing thread wakes it through the loop's token.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Collection, Hashable, MutableMapping
from typing import Any, TypeVar

import anyio
import anyio.from_thread
import anyio.lowlevel

T = TypeVar("T")


class Pending:
    """A value one thread computes (``settle``) for others to wait on (``wait``)."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        self._wakers: list[Callable[[], None]] = []
        self._wakers_lock = threading.Lock()

    def wait(self) -> Any:
        """The computation's value, or a copy of its error (``own_copy``) raised from it."""
        self._done.wait()
        return self._outcome()

    async def wait_async(self) -> Any:
        """``wait`` in an event loop: the coroutine waits there, holding no thread, and the computing thread wakes
        it (``anyio.from_thread.run_sync`` with the loop's token; directly if it computed in the loop's own
        thread). A waiter cancelled meanwhile leaves no waker behind."""
        if not self._done.is_set():
            woken = anyio.Event()
            token = anyio.lowlevel.current_token()
            loop_thread = threading.get_ident()

            def wake() -> None:
                if threading.get_ident() == loop_thread:
                    woken.set()
                    return
                try:
                    anyio.from_thread.run_sync(woken.set, token=token)
                except Exception:  # noqa: S110  # the loop has ended: nothing waits there any more
                    pass

            with self._wakers_lock:
                waiting = not self._done.is_set()
                if waiting:
                    self._wakers.append(wake)
            if waiting:
                try:
                    await woken.wait()
                finally:
                    with self._wakers_lock:
                        if wake in self._wakers:
                            self._wakers.remove(wake)
        return self._outcome()

    def _outcome(self) -> Any:
        if self._error is not None:
            raise own_copy(self._error) from self._error
        return self._value

    def settle(
        self,
        compute: Callable[[], T],
        *,
        keep: Callable[[T], None],
        lock: threading.Lock,
        table: MutableMapping[Any, Pending],
        keys: Collection[Hashable],
    ) -> T:
        """``compute()`` for this computation's waiters, then ``keep(value)`` (the cache's bookkeeping, under its own
        lock). In every case ``keys`` are then released from ``table`` (each one this computation still holds, under
        ``lock``) and the waiters are woken, with the value or with the error, which also propagates here."""
        try:
            value = compute()
            keep(value)
        except BaseException as error:
            self._error = error
            raise
        else:
            self._value = value
            return value
        finally:
            try:
                with lock:
                    for key in keys:
                        if table.get(key) is self:
                            del table[key]
            finally:
                with self._wakers_lock:
                    self._done.set()
                    wakers, self._wakers = self._wakers, []
                for wake in wakers:
                    wake()


def own_copy(error: BaseException) -> BaseException:
    """``error`` for one more thread to raise: a new exception of its type, with its arguments and attributes. A
    built-in exception is copied by its own reduction (an ``OSError`` keeps its file name); one whose ``__init__`` does
    not take its ``args`` back (``ProblemError``) is made without running it, with the original's attributes."""
    cls = type(error)
    try:
        clone: BaseException | None = copy.copy(error)
    except Exception:  # its __init__ does not take its args back
        clone = None
    if clone is not None and type(clone) is cls and _same(clone.args, error.args):
        return clone
    try:
        clone = cls.__new__(cls, *error.args)
        clone.args = error.args
        clone.__dict__.update(vars(error))
    except Exception:  # an exception type that cannot be made so: the original (its traceback is then shared)
        return error
    return clone


def _same(made: tuple[object, ...], given: tuple[object, ...]) -> bool:
    return len(made) == len(given) and all(a is b for a, b in zip(made, given, strict=True))
