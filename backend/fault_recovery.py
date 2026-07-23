"""Conservative game-session tracking and newly-enabled Mod rollback plans."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .storage import JsonStore


class RecoveryUnavailable(RuntimeError):
    """No assessed failed session is available for rollback."""


class RecoveryPlanStale(RuntimeError):
    def __init__(self, plan: dict[str, object]):
        self.details = plan
        super().__init__("故障恢复状态已变化，请重新检查后再回滚")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FaultRecoveryService:
    """Persist session evidence without guessing whether a game exit was a crash."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        snapshot_provider: Callable[[], Iterable[dict[str, object]]],
        game_running: Callable[[], bool],
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.store = JsonStore(state_path)
        self.snapshot_provider = snapshot_provider
        self.game_running = game_running
        self.clock = clock
        self._lock = threading.RLock()

    @staticmethod
    def _empty_state() -> dict[str, object]:
        return {
            "version": 1,
            "last_stable": None,
            "active_session": None,
            "pending_session": None,
        }

    def _read(self) -> dict[str, object]:
        value = self.store.read(self._empty_state())
        if not isinstance(value, dict) or value.get("version") != 1:
            return self._empty_state()

        def snapshot_record(
            raw: object, *, pending: bool = False
        ) -> dict[str, object] | None:
            if not isinstance(raw, dict) or not isinstance(raw.get("snapshot"), list):
                return None
            snapshot = self._valid_snapshot(raw["snapshot"])
            if len(snapshot) != len(raw["snapshot"]):
                return None
            if pending:
                if (
                    not isinstance(raw.get("id"), str)
                    or not isinstance(raw.get("started_at"), str)
                    or not isinstance(raw.get("ended_at"), str)
                    or raw.get("outcome")
                    not in {"pending", "fault", "rolled_back"}
                ):
                    return None
            return {**raw, "snapshot": snapshot}

        stable = value.get("last_stable")
        if (
            not isinstance(stable, dict)
            or not isinstance(stable.get("recorded_at"), str)
            or not isinstance(stable.get("snapshot"), list)
        ):
            stable = None
        else:
            snapshot = self._valid_snapshot(stable["snapshot"])
            stable = (
                {**stable, "snapshot": snapshot}
                if len(snapshot) == len(stable["snapshot"])
                else None
            )
        active = snapshot_record(value.get("active_session"))
        if active is not None and (
            not isinstance(active.get("id"), str)
            or not isinstance(active.get("started_at"), str)
        ):
            active = None
        return {
            "version": 1,
            "last_stable": stable,
            "active_session": active,
            "pending_session": snapshot_record(
                value.get("pending_session"), pending=True
            ),
        }

    def _snapshot(self) -> list[dict[str, object]]:
        normalized: dict[str, dict[str, object]] = {}
        for raw in self.snapshot_provider():
            if not isinstance(raw, dict):
                continue
            source = raw.get("source")
            item_id = raw.get("id")
            enabled = raw.get("enabled")
            recoverable = raw.get("recoverable", True)
            if (
                source not in {"managed", "workshop"}
                or not isinstance(item_id, str)
                or not item_id
                or type(enabled) is not bool
                or type(recoverable) is not bool
            ):
                continue
            key = f"{source}:{item_id}"
            normalized[key] = {
                "key": key,
                "source": source,
                "id": item_id,
                "name": str(raw.get("name") or item_id)[:160],
                "enabled": enabled,
                "recoverable": recoverable,
            }
        return [normalized[key] for key in sorted(normalized)]

    @staticmethod
    def _valid_snapshot(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        normalized: dict[str, dict[str, object]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            item_id = item.get("id")
            enabled = item.get("enabled")
            recoverable = item.get("recoverable", True)
            if (
                source not in {"managed", "workshop"}
                or not isinstance(item_id, str)
                or not item_id
                or type(enabled) is not bool
                or type(recoverable) is not bool
            ):
                continue
            key = f"{source}:{item_id}"
            if item.get("key") != key:
                continue
            normalized[key] = {
                "key": key,
                "source": source,
                "id": item_id,
                "name": str(item.get("name") or item_id)[:160],
                "enabled": enabled,
                "recoverable": recoverable,
            }
        return [normalized[key] for key in sorted(normalized)]

    @staticmethod
    def _changes(
        baseline: list[dict[str, object]], current: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        before = {str(item.get("key")): item for item in baseline}
        after = {str(item.get("key")): item for item in current}
        changes: list[dict[str, object]] = []
        for key in sorted(before.keys() | after.keys()):
            old = before.get(key)
            new = after.get(key)
            old_enabled = bool(old and old.get("enabled") is True)
            new_enabled = bool(new and new.get("enabled") is True)
            if old_enabled == new_enabled:
                continue
            item = new or old or {}
            changes.append({
                "key": key,
                "source": item.get("source"),
                "id": item.get("id"),
                "name": item.get("name"),
                "before_enabled": old_enabled,
                "after_enabled": new_enabled,
            })
        return changes

    def _ensure_baseline(
        self, state: dict[str, object], snapshot: list[dict[str, object]]
    ) -> bool:
        if state["last_stable"] is not None:
            return False
        state["last_stable"] = {
            "recorded_at": self.clock(),
            "session_id": None,
            "snapshot": snapshot,
        }
        return True

    def _build_plan(
        self,
        state: dict[str, object],
        current: list[dict[str, object]],
    ) -> dict[str, object]:
        stable = state.get("last_stable")
        pending = state.get("pending_session")
        baseline = self._valid_snapshot(
            stable.get("snapshot") if isinstance(stable, dict) else None
        )
        before = {str(item.get("key")): item for item in baseline}
        actions: list[dict[str, object]] = []
        blocked: list[dict[str, object]] = []
        for item in current:
            if item.get("enabled") is not True:
                continue
            previous = before.get(str(item.get("key")))
            if previous is not None and previous.get("enabled") is True:
                continue
            action = {
                "key": item["key"],
                "source": item["source"],
                "id": item["id"],
                "name": item["name"],
                "from_enabled": True,
                "to_enabled": False,
            }
            if item.get("recoverable") is True:
                actions.append(action)
            else:
                blocked.append({**action, "reason": "state_not_toggleable"})

        payload = {
            "pending_session_id": pending.get("id")
            if isinstance(pending, dict)
            else None,
            "pending_outcome": pending.get("outcome")
            if isinstance(pending, dict)
            else None,
            "baseline": baseline,
            "current": current,
            "actions": actions,
            "blocked": blocked,
        }
        revision = "sha256:" + hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assessed_fault = (
            isinstance(pending, dict) and pending.get("outcome") == "fault"
        )
        return {
            "revision": revision,
            "available": assessed_fault and bool(actions),
            "actions": actions,
            "blocked": blocked,
            "action_count": len(actions),
            "blocked_count": len(blocked),
        }

    def _public_status(
        self,
        state: dict[str, object],
        *,
        running: bool,
        current: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        stable = state.get("last_stable")
        active = state.get("active_session")
        pending = state.get("pending_session")
        pending_public = None
        if isinstance(pending, dict):
            baseline = self._valid_snapshot(
                stable.get("snapshot") if isinstance(stable, dict) else None
            )
            session_snapshot = self._valid_snapshot(pending.get("snapshot"))
            changes = self._changes(baseline, session_snapshot)
            pending_public = {
                "id": pending.get("id"),
                "started_at": pending.get("started_at"),
                "ended_at": pending.get("ended_at"),
                "outcome": pending.get("outcome", "pending"),
                "changes": changes,
                "change_count": len(changes),
            }
        plan = None
        if isinstance(pending, dict) and pending.get("outcome") == "fault":
            plan = self._build_plan(state, current if current is not None else self._snapshot())
        return {
            "running": running,
            "monitoring": True,
            "active_session": {
                "id": active.get("id"),
                "started_at": active.get("started_at"),
            }
            if isinstance(active, dict)
            else None,
            "pending_assessment": pending_public,
            "last_stable_at": stable.get("recorded_at")
            if isinstance(stable, dict)
            else None,
            "recovery_plan": plan,
        }

    def observe(self) -> dict[str, object]:
        with self._lock:
            running = self.game_running()
            state = self._read()
            changed = False
            current: list[dict[str, object]] | None = None
            if state["last_stable"] is None:
                current = self._snapshot()
                changed = self._ensure_baseline(state, current) or changed
            active = state.get("active_session")
            if running and not isinstance(active, dict):
                current = current if current is not None else self._snapshot()
                state["active_session"] = {
                    "id": uuid.uuid4().hex,
                    "started_at": self.clock(),
                    "snapshot": current,
                }
                changed = True
            elif not running and isinstance(active, dict):
                state["pending_session"] = {
                    **active,
                    "ended_at": self.clock(),
                    "outcome": "pending",
                }
                state["active_session"] = None
                changed = True
            if changed:
                self.store.write(state)
            return self._public_status(state, running=running, current=current)

    def assess(self, outcome: str) -> dict[str, object]:
        if outcome not in {"stable", "fault"}:
            raise ValueError("outcome must be stable or fault")
        with self._lock:
            if self.game_running():
                raise RecoveryUnavailable("游戏仍在运行，不能评估本次会话")
            state = self._read()
            pending = state.get("pending_session")
            if not isinstance(pending, dict):
                raise RecoveryUnavailable("没有等待评估的游戏会话")
            if outcome == "stable":
                state["last_stable"] = {
                    "recorded_at": self.clock(),
                    "session_id": pending.get("id"),
                    "snapshot": self._valid_snapshot(pending.get("snapshot")),
                }
                state["pending_session"] = None
            else:
                pending["outcome"] = "fault"
                pending["assessed_at"] = self.clock()
            self.store.write(state)
            return self._public_status(
                state, running=False, current=self._snapshot()
            )

    def rollback(
        self,
        revision: str,
        executor: Callable[[list[dict[str, object]]], list[dict[str, object]]],
    ) -> dict[str, object]:
        with self._lock:
            if self.game_running():
                raise RecoveryUnavailable("游戏仍在运行，不能回滚 Mod")
            state = self._read()
            pending = state.get("pending_session")
            if not isinstance(pending, dict) or pending.get("outcome") != "fault":
                raise RecoveryUnavailable("请先将最近一次游戏会话标记为故障")
            current = self._snapshot()
            plan = self._build_plan(state, current)
            if revision != plan["revision"]:
                raise RecoveryPlanStale(plan)
            if not plan["actions"]:
                raise RecoveryUnavailable("没有可安全回滚的新启用 Mod")
            executed = executor(list(plan["actions"]))
            pending["outcome"] = "rolled_back"
            pending["rolled_back_at"] = self.clock()
            pending["executed"] = executed
            self.store.write(state)
            return {
                "ok": True,
                "executed": executed,
                "executed_count": len(executed),
                "status": self._public_status(
                    state, running=False, current=self._snapshot()
                ),
            }
