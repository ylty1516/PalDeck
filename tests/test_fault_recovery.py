from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.fault_recovery import (
    FaultRecoveryService,
    RecoveryPlanStale,
    RecoveryUnavailable,
)


def _service(
    tmp_path: Path,
    mods: list[dict[str, object]],
    running: dict[str, bool],
) -> FaultRecoveryService:
    ticks = iter(f"2026-07-24T00:00:{second:02d}+00:00" for second in range(60))
    return FaultRecoveryService(
        tmp_path / "recovery.json",
        snapshot_provider=lambda: list(mods),
        game_running=lambda: running["value"],
        clock=lambda: next(ticks),
    )


def _enabled_mod(item_id: str, *, recoverable: bool = True) -> dict[str, object]:
    return {
        "source": "managed",
        "id": item_id,
        "name": f"Mod {item_id}",
        "enabled": True,
        "recoverable": recoverable,
    }


def test_failed_session_rolls_back_only_newly_enabled_mods(tmp_path):
    mods: list[dict[str, object]] = []
    running = {"value": False}
    service = _service(tmp_path, mods, running)

    assert service.observe()["last_stable_at"] is not None
    mods.append(_enabled_mod("new-mod"))
    running["value"] = True
    assert service.observe()["active_session"] is not None
    running["value"] = False
    pending = service.observe()["pending_assessment"]
    assert pending["change_count"] == 1
    assert pending["outcome"] == "pending"

    assessed = service.assess("fault")
    plan = assessed["recovery_plan"]
    assert plan["available"] is True
    assert [item["id"] for item in plan["actions"]] == ["new-mod"]

    def disable(actions):
        mods[0]["enabled"] = False
        return actions

    result = service.rollback(plan["revision"], disable)
    assert result["executed_count"] == 1
    assert result["status"]["pending_assessment"]["outcome"] == "rolled_back"
    assert mods[0]["enabled"] is False


def test_stable_assessment_promotes_exact_session_snapshot(tmp_path):
    mods: list[dict[str, object]] = []
    running = {"value": False}
    service = _service(tmp_path, mods, running)
    service.observe()
    mods.append(_enabled_mod("known-good"))
    running["value"] = True
    service.observe()
    running["value"] = False
    service.observe()

    stable = service.assess("stable")
    assert stable["pending_assessment"] is None

    running["value"] = True
    service.observe()
    running["value"] = False
    service.observe()
    assessed = service.assess("fault")
    assert assessed["recovery_plan"]["actions"] == []
    assert assessed["recovery_plan"]["available"] is False


def test_recovery_revision_rejects_changed_mod_state(tmp_path):
    mods: list[dict[str, object]] = []
    running = {"value": False}
    service = _service(tmp_path, mods, running)
    service.observe()
    mods.append(_enabled_mod("first"))
    running["value"] = True
    service.observe()
    running["value"] = False
    service.observe()
    revision = service.assess("fault")["recovery_plan"]["revision"]

    mods.append(_enabled_mod("second"))
    with pytest.raises(RecoveryPlanStale) as error:
        service.rollback(revision, lambda actions: actions)
    assert error.value.details["action_count"] == 2


def test_unrecoverable_enabled_mod_is_reported_but_not_executed(tmp_path):
    mods: list[dict[str, object]] = []
    running = {"value": False}
    service = _service(tmp_path, mods, running)
    service.observe()
    mods.append(_enabled_mod("damaged", recoverable=False))
    running["value"] = True
    service.observe()
    running["value"] = False
    service.observe()

    plan = service.assess("fault")["recovery_plan"]
    assert plan["actions"] == []
    assert plan["blocked_count"] == 1
    assert plan["blocked"][0]["reason"] == "state_not_toggleable"


def test_assessment_and_rollback_require_stopped_game_and_pending_fault(tmp_path):
    mods: list[dict[str, object]] = []
    running = {"value": False}
    service = _service(tmp_path, mods, running)
    service.observe()

    with pytest.raises(RecoveryUnavailable, match="等待评估"):
        service.assess("fault")
    with pytest.raises(RecoveryUnavailable, match="标记为故障"):
        service.rollback("sha256:" + "0" * 64, lambda actions: actions)

    running["value"] = True
    service.observe()
    with pytest.raises(RecoveryUnavailable, match="仍在运行"):
        service.assess("fault")


def test_damaged_persisted_baseline_fails_closed_to_current_snapshot(tmp_path):
    state_path = tmp_path / "recovery.json"
    state_path.write_text(
        json.dumps({
            "version": 1,
            "last_stable": {
                "recorded_at": "bad",
                "snapshot": [{"source": "managed", "id": "unsafe"}],
            },
            "active_session": None,
            "pending_session": None,
        }),
        encoding="utf-8",
    )
    mods = [_enabled_mod("already-enabled")]
    running = {"value": False}
    service = _service(tmp_path, mods, running)

    status = service.observe()
    assert status["last_stable_at"] is not None
    running["value"] = True
    service.observe()
    running["value"] = False
    service.observe()
    plan = service.assess("fault")["recovery_plan"]
    assert plan["actions"] == []
