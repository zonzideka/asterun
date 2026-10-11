"""ControlRuntime finalizers must not raise off the thread that opened SQLite."""
from __future__ import annotations

import gc
import sqlite3
import sys
import threading
import types
import weakref

import pytest

from asterun.control import ControlRuntime


def _install_unraisable_hook(monkeypatch):
    gc.collect()
    recorded = []
    previous = sys.unraisablehook

    def hook(unraisable):
        recorded.append(unraisable)
        previous(unraisable)

    monkeypatch.setattr(sys, "unraisablehook", hook)
    return recorded


def _unraisable_texts(recorded):
    return [f"{getattr(item.exc_type, '__name__', item.exc_type)}: {item.exc_value}" for item in recorded]


def _release(bucket, errors):
    try:
        bucket.clear()
        gc.collect()
    except Exception as exc:
        errors.append(exc)


def _private_runtime():
    app = types.SimpleNamespace(store=types.SimpleNamespace())
    runtime = ControlRuntime(app)
    assert runtime.own_connection is True
    app.runtime = runtime
    return runtime


def _finalize_on_other_thread(build):
    """Drop the only strong ref on another thread and collect the cycle there."""
    runtime = build()
    watched = weakref.ref(runtime)
    holder = [runtime]
    errors = []
    thread = threading.Thread(target=_release, args=(holder, errors))
    assert watched() is runtime
    del runtime
    del holder
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert watched() is None


def test_closed_private_runtime_finalized_on_another_thread_is_silent(monkeypatch):
    recorded = _install_unraisable_hook(monkeypatch)

    def build():
        runtime = _private_runtime()
        runtime.close()
        return runtime

    _finalize_on_other_thread(build)
    assert _unraisable_texts(recorded) == []


def test_unclosed_private_runtime_finalized_on_another_thread_is_silent(monkeypatch):
    recorded = _install_unraisable_hook(monkeypatch)
    _finalize_on_other_thread(_private_runtime)
    assert _unraisable_texts(recorded) == []


def test_close_is_idempotent_and_closes_private_connection():
    runtime = _private_runtime()
    connection = runtime.store._conn
    runtime.close()
    runtime.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_shared_connection_survives_close_and_finalization(monkeypatch):
    recorded = _install_unraisable_hook(monkeypatch)
    connection = sqlite3.connect(":memory:", isolation_level=None)
    store = types.SimpleNamespace(_conn=connection)

    def build():
        app = types.SimpleNamespace(store=store)
        runtime = ControlRuntime(app)
        assert runtime.own_connection is False
        app.runtime = runtime
        runtime.close()
        runtime.close()
        assert connection.execute("SELECT 1").fetchone() == (1,)
        return runtime

    _finalize_on_other_thread(build)
    assert connection.execute("SELECT 1").fetchone() == (1,)
    assert _unraisable_texts(recorded) == []
    connection.close()
