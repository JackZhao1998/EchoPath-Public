"""Close the record -> consolidate -> replay loop for the MCP server.

The offline pipeline (gui_wrapper -> memory_consolidation -> memory_repo ->
memory_replay) shipped complete but unwired: nothing recorded wrapper runs,
nothing called the consolidator, and nothing constructed the memory-backed
replay store. This module is the missing glue, sized for the MCP server where
there is no outer coding agent to drive those pieces as a skill.

Three responsibilities:

- ``consolidate_completed_run``: one successful GUI run in a wrapper run
  directory -> a consolidated, platform-stamped memory path with component
  crops, promoted when the run's own goal verification vouches for it.
- ``CompositeReplayStore``: arbitration between the learned memory repo
  (fuzzy intent match, flexible-input rebinding, visual re-aiming) and the
  existing ``InteractionReplayStore`` cascade.
- ``prune_gui_runs``: keeps ``local_data/gui_runs`` from growing without
  bound; safe because promoted evidence is copied into the repo's
  content-addressed blob store.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .memory_consolidation import (
    TrajectoryConsolidationError,
    consolidate_wrapper_trajectory,
)
from .memory_repo import (
    MemoryPromotionGateError,
    MemoryRepository,
    MemoryRepositoryError,
)

GOAL_VERIFICATION_EVALUATOR = "echopath-goal-verification"
RUN_MANIFEST_EVALUATOR = "echopath-run-manifest"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_artifact_validation(
    goal_verification: Optional[Mapping[str, Any]],
    *,
    evaluator: str = GOAL_VERIFICATION_EVALUATOR,
) -> Dict[str, Any]:
    """Turn a GUI run's goal verification into a promotion-gate artifact.

    The promotion gate requires an artifact validation with a truthy ``passed``
    and a named evaluator, but the repository ships no producer for it. The
    run's own model-verified goal completion is the honest evidence we have.
    """
    verification = dict(goal_verification or {})
    return {
        "passed": bool(verification.get("goal_completed") or verification.get("passed")),
        "evaluator": evaluator,
        "assessment": str(verification.get("assessment") or "").strip(),
        "blocker": verification.get("blocker"),
        "verified_at": _utc_iso(),
        "source": str(verification.get("source") or "gui_agent.goal_verification"),
    }


async def consolidate_completed_run(
    router: Any,
    run_directory: Path,
    *,
    task_intent: str,
    memory_repository: MemoryRepository,
    goal_verification: Optional[Mapping[str, Any]] = None,
    artifact_validation: Optional[Mapping[str, Any]] = None,
    application: str = "",
    model: str = "default",
    reasoning_effort: str = "medium",
    metadata: Optional[Mapping[str, Any]] = None,
    platform: str = "",
) -> Dict[str, Any]:
    """Consolidate one wrapper run into the memory repo and try to promote it.

    Best-effort by contract: every failure mode returns a status dict rather
    than raising, because learning must never fail the run that produced it.
    ``model`` defaults to the ``default`` sentinel so the configured bridge
    resolves it (Claude Opus 5 here) instead of the offline harness default.
    """
    run_directory = Path(run_directory).resolve()
    summary: Dict[str, Any] = {
        "status": "error",
        "run_directory": str(run_directory),
        "path_id": None,
        "promoted": False,
    }

    manifest_path = run_directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("metadata", {}).get("segmented_replay"):
            return {**summary, "status": "skipped", "reason": "Native segments must retain their contracts; this recording is incomplete"}
        if goal_verification is None:
            recorded_verification = manifest.get("metadata", {}).get("goal_verification")
            if isinstance(recorded_verification, Mapping):
                goal_verification = recorded_verification

    try:
        result = await consolidate_wrapper_trajectory(
            router,
            run_directory,
            task_intent=task_intent,
            application=application,
            model=model,
            reasoning_effort=reasoning_effort,
            goal_verification=goal_verification,
        )
    except (TrajectoryConsolidationError, OSError, ValueError) as error:
        summary["error"] = f"consolidation failed: {error}"
        return summary

    reasoning_report = dict(result.reasoning_report)
    summary.update(
        {
            "model": result.model,
            "selected_step_count": len(result.selected_step_ids),
            "dropped_step_count": len(result.dropped_steps),
            "flexible_input_count": len(result.flexible_action_inputs),
            "segment_count": len(result.segments),
            "native_segment_count": sum(s["execution_policy"] == "native" for s in result.segments),
            "query_keys": list(reasoning_report.get("query_keys") or []),
            "blockers": list(reasoning_report.get("blockers") or []),
        }
    )

    try:
        candidate = memory_repository.add_candidate_from_wrapper_run(
            run_directory,
            task_intent=task_intent,
            application=application,
            selected_step_ids=result.selected_step_ids,
            consolidation=result.to_dict(),
            metadata=metadata,
            platform=platform or sys.platform,
        )
    except (MemoryRepositoryError, OSError, ValueError) as error:
        summary["error"] = f"candidate storage failed: {error}"
        return summary

    path_id = str(candidate.get("path_id") or "")
    summary["path_id"] = path_id
    summary["status"] = "candidate"
    summary.pop("error", None)

    if str(candidate.get("status") or "") == "active":
        # An identical action sequence was already learned and promoted; the
        # repository refreshed that path instead of duplicating it.
        summary["status"] = "promoted"
        summary["promoted"] = True
        summary["refreshed_existing"] = True
        return summary

    recommendation = str(reasoning_report.get("promotion_recommendation") or "").strip()
    if recommendation != "promote" or not reasoning_report.get("viable"):
        summary["gate_reasons"] = [
            f"consolidator recommendation was '{recommendation or 'missing'}'"
        ] + summary["blockers"]
        return summary

    validation = dict(
        artifact_validation
        if artifact_validation is not None
        else build_artifact_validation(goal_verification)
    )
    if not validation.get("passed"):
        summary["gate_reasons"] = [
            "goal verification did not confirm completion; kept as candidate"
        ]
        return summary

    try:
        memory_repository.promote_path(
            path_id,
            artifact_validation=validation,
            reasoning_report=reasoning_report,
        )
    except MemoryPromotionGateError as error:
        summary["gate_reasons"] = [part.strip() for part in str(error).split(";") if part.strip()]
        return summary
    except (MemoryRepositoryError, OSError) as error:
        summary["gate_reasons"] = [f"promotion failed: {error}"]
        return summary

    summary["status"] = "promoted"
    summary["promoted"] = True
    return summary


class CompositeReplayStore:
    """Consult the learned memory repo first, then the existing replay cascade.

    The orchestrator probes for ``find_latest_successful_match_for_task`` and
    prefers it; that async method is where flexible-input rebinding happens.
    Everything not defined here delegates to the fallback store, so the
    interaction logger, history clearing, and short-term memory snapshots keep
    working against the same object the runtime always registered.
    """

    def __init__(self, memory_store: Any, fallback: Any) -> None:
        # Assign via __dict__ so __getattr__ never sees a half-built instance.
        self.__dict__["memory_store"] = memory_store
        self.__dict__["fallback"] = fallback

    def find_latest_successful_match(self, user_query: str):
        match = None
        memory_store = self.memory_store
        if memory_store is not None:
            try:
                match = memory_store.find_latest_successful_match(user_query)
            except Exception:
                match = None
        if match is not None:
            return match
        return self.fallback.find_latest_successful_match(user_query)

    async def find_latest_successful_match_for_task(
        self,
        user_query: str,
        *,
        current_task: Optional[Mapping[str, Any]] = None,
    ):
        memory_store = self.memory_store
        if memory_store is not None:
            try:
                match = await memory_store.find_latest_successful_match_for_task(
                    user_query,
                    current_task=current_task,
                )
            except Exception:
                match = None
            if match is not None:
                return match
        return self.fallback.find_latest_successful_match(user_query)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["fallback"], name)


def prune_gui_runs(
    root_directory: Path,
    *,
    keep_recent: int = 20,
    keep_failed: int = 5,
) -> List[str]:
    """Delete old wrapper run directories, newest-first retention.

    Promoted evidence is content-addressed copies inside the memory repo's blob
    store, and replay never reads ``gui_runs`` screenshots, so pruning is safe.
    Failed runs get a smaller budget; unreadable directories are left alone.
    """
    root = Path(root_directory)
    if not root.is_dir():
        return []

    completed: List[Path] = []
    failed: List[Path] = []
    for run_dir in root.iterdir():
        manifest = run_dir / "manifest.json"
        if not run_dir.is_dir() or not manifest.is_file():
            continue
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            status = str(payload.get("status") or "").strip().lower()
        except (OSError, json.JSONDecodeError):
            continue
        (completed if status == "completed" else failed).append(run_dir)

    def newest_first(paths: List[Path]) -> List[Path]:
        return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)

    removed: List[str] = []
    for bucket, budget in ((newest_first(completed), keep_recent), (newest_first(failed), keep_failed)):
        for run_dir in bucket[max(0, budget):]:
            try:
                shutil.rmtree(run_dir)
                removed.append(run_dir.name)
            except OSError:
                continue
    return removed
