"""Serialize Kopf's independent timer/change threads for one ManagedCluster.

The operator deployment is intentionally single-replica with Recreate. This is
a process lock, not distributed leader election. Waiting callers retain their
lock references; unused identities disappear from the weak map automatically.
"""

from _thread import LockType
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from weakref import WeakValueDictionary

_guard = Lock()
_locks: WeakValueDictionary[tuple[str, str, str], LockType] = WeakValueDictionary()


@contextmanager
def reconciliation_lock(namespace: str, name: str, uid: str) -> Iterator[None]:
    identity = namespace, name, uid
    with _guard:
        lock = _locks.get(identity)
        if lock is None:
            lock = Lock()
            _locks[identity] = lock
    with lock:
        yield
