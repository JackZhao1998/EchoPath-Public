"""One readable line per desktop step, for the Control Room and the menu bar.

The session's debug projection already folds agent events into steps (what was
planned, the action, whether it verified), but it keeps only the latest eight
and is rebuilt from a capped event list. StepLog merges each projection into a
full history and adds what the projection leaves out: whether a replayed click
hit its saved image, and which fallback ran when it did not.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

STEP_FIELDS = ('source', 'action', 'expected_outcome', 'grounding', 'verification',
               'verification_status', 'blocker', 'status', 'timestamp', 'updated_at')

# Replay events worth a word on the step they happened to.
REPLAY_NOTES = {
    'agent.replay.coordinate_copy': lambda p: 'Clicked the saved position',
    'agent.replay.visual_reaimed': lambda p: 'Matched the saved image',
    'agent.replay.visual_reaiming.rejected': lambda p: "Saved image didn't match",
    'agent.replay.optional_prerequisite.skipped': lambda p: 'Skipped an optional step',
    'agent.replay.repair.rewind': lambda p: 'Dismissed a blocker and retried',
    'agent.replay.decision_model.completed': lambda p: (
        f"Chose {p.get('choice_text')!r} by its text" if p.get('located') else 'Text decision unsure'),
    'agent.replay.step_grounding_fallback.completed': lambda p: (
        'Vision model found the target' if p.get('status') == 'grounded' else 'Vision model could not find it'),
    'agent.replay.visual_reaiming.fallback': lambda p: 'Stopped replaying; planning the rest live',
    'agent.replay.goal_not_met.fallback': lambda p: 'Goal not on screen after replay; planning the rest live',
    'agent.replay.memory_enhanced_fallback.started': lambda p: 'Stopped replaying; steering from the saved steps',
}


class StepLog:
    def __init__(self) -> None:
        self.steps: Dict[int, Dict[str, Any]] = {}
        # Model calls made after one step verified and before the next is planned
        # belong to that next step (they are its planning call).
        self.pending_calls = 0

    def _step(self, number: int) -> Dict[str, Any]:
        if number not in self.steps:
            self.steps[number] = {'n': number, 'source': 'planned', 'notes': [], 'model_calls': self.pending_calls}
            self.pending_calls = 0
        return self.steps[number]

    def merge(self, projected: Optional[List[Mapping[str, Any]]], event: Any = None) -> bool:
        changed = False
        for item in projected or []:
            number = item.get('iteration')
            if not isinstance(number, int):
                continue
            step = self._step(number)
            for key in STEP_FIELDS:
                value = item.get(key)
                if value not in (None, '') and step.get(key) != value:
                    step[key] = value
                    changed = True
        name = str(getattr(event, 'name', '') or '')
        payload = getattr(event, 'payload', None)
        payload = payload if isinstance(payload, Mapping) else {}
        latest = max(self.steps) if self.steps else None
        number = payload.get('iteration') if isinstance(payload.get('iteration'), int) else latest
        stamp = getattr(event, 'timestamp', None)
        if isinstance(payload.get('iteration'), int) and stamp is not None:
            # A replay seeds every step at once, so the projection's timestamps are all
            # the same; the first event that names a step is when it actually began.
            step = self._step(number)
            if 'started' not in step:
                step['started'] = stamp.isoformat()
                changed = True
                for earlier in self.steps.values():
                    # Replay doesn't verify each step, so moving on is what completes one.
                    if earlier['n'] < number and earlier.get('source') == 'replay' and earlier.get('status') == 'running':
                        earlier['status'] = 'completed'
        next_step = payload.get('next_step')
        if name == 'agent.desktop.planned' and isinstance(next_step, Mapping) and number is not None:
            # The plan names the target; the executed action only keeps its coordinates.
            target = str(next_step.get('target_description') or next_step.get('target_text') or '').strip()
            if target and self._step(number).get('target') != target:
                self._step(number)['target'] = target[:120]
                changed = True
        if name == 'agent.desktop.goal.verified' and number is not None:
            step = self._step(number)
            note = 'Goal confirmed on screen' if payload.get('goal_completed') else 'Goal not on screen'
            if note not in step['notes']:
                step['notes'].append(note)
                changed = True
        if name in REPLAY_NOTES and number is not None:
            step = self._step(number)
            step['source'] = 'replay'
            note = REPLAY_NOTES[name](payload)
            if note not in step['notes']:
                step['notes'].append(note)
            changed = True
        elif name == 'agent.model.requested':
            open_step = self.steps.get(latest) if latest is not None else None
            if open_step is not None and not open_step.get('verification_status') and open_step.get('status') == 'running':
                open_step['model_calls'] += 1
                changed = True
            else:
                self.pending_calls += 1
        return changed

    def finish(self, status: str) -> bool:
        """Close out steps still marked running when the run ends."""
        changed = False
        for step in self.steps.values():
            if step.get('status') == 'running':
                step['status'] = 'completed' if status == 'completed' else 'stopped'
                changed = True
        return changed

    def as_list(self) -> List[Dict[str, Any]]:
        return [dict(self.steps[number]) for number in sorted(self.steps)][-200:]
