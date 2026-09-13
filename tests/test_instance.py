from __future__ import annotations

from asterun.application import Application
from asterun.errors import INSTANCE_LOCKED, AsterunError
from asterun.policy import Principal


def test_second_instance_cannot_preempt(config_path, isolated_env):
    state = isolated_env / "state"
    first = Application.from_paths(
        config_path,
        state,
        principal=Principal("test:user", "test"),
    )
    try:
        submitted = first.handle("task.submit", {"workspace": "demo", "script": "success"})
        assert submitted.ok
        try:
            Application.from_paths(config_path, state, principal=Principal("test:user", "test"))
            raise AssertionError("第二实例不应取得锁")
        except AsterunError as error:
            assert error.code == INSTANCE_LOCKED
        first.close()
        second = Application.from_paths(config_path, state, principal=Principal("test:user", "test"))
        fetched = second.handle("task.get", {"task_id": submitted.ids["task_id"]})
        assert fetched.ok
        assert fetched.data["task"]["id"] == submitted.ids["task_id"]
        second.close()
    finally:
        if first.instance_lock is not None:
            first.close()
