from __future__ import annotations

import pytest

from asterun.contracts import AcceptanceStatus, DeliveryStatus, RunStatus, Task
from asterun.ids import AccountRef, BackendId, ModelId, TaskId, require_type


def test_account_backend_model_ids_are_not_interchangeable() -> None:
    account = AccountRef("acct-1")
    backend = BackendId("fake")
    model = ModelId("unused")
    with pytest.raises(TypeError):
        require_type(account, BackendId)
    with pytest.raises(TypeError):
        require_type(backend, AccountRef)
    with pytest.raises(TypeError):
        require_type(model, BackendId)


def test_task_rejects_mixed_identifiers() -> None:
    with pytest.raises(TypeError):
        Task(
            id=TaskId("tsk_1"),
            workspace="demo",
            backend=AccountRef("acct-1"),  # type: ignore[arg-type]
            text="x",
            script="success",
            acceptance=AcceptanceStatus.PENDING,
            delivery=DeliveryStatus.NOT_CONFIGURED,
            created_at="2026-09-07T00:00:00Z",
        )


def test_run_success_is_not_acceptance() -> None:
    assert RunStatus.SUCCEEDED != AcceptanceStatus.PASSED
    assert AcceptanceStatus.PENDING.value == "pending"
