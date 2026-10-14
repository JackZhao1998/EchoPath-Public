"""Ask a decision model which on-screen element a replay step meant.

When a recorded pointer step can no longer be matched by picture, and the screen
has no unambiguous structure to address by index, something still has to choose
between the candidates. A full vision model can, at several seconds and a few
cents per step. Jev answers the same question as a typed choice with a
calibrated probability, which is both far cheaper and -- more usefully -- gives
a number worth thresholding on.

That matters because the alternative is a pixel-correlation score. A template
match of 0.74 says nothing about how often 0.74 is right. A calibrated
probability does, so the decision to act or escalate stops being a guess.

    from echopath.jev_client import JevClient
    client = JevClient.from_credentials()
    answer = client.choose(
        instructions="Which element is the first product listing?",
        options={"elem_3": "AXLink - Scott Paper Towels...", ...},
        state={"page": "Amazon search results"},
    )
    if answer.confidence >= 0.9:
        click(answer.choice)
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

def https_context() -> ssl.SSLContext:
    """Default trust store; python.org builds on macOS ship none until certifi is linked."""
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        try:
            import certifi
            context.load_verify_locations(certifi.where())
        except (ImportError, OSError):
            pass
    return context


DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_TIMEOUT_SECONDS = 30
# Jev accepts at most 255 options; keep headroom and keep the prompt small.
MAX_OPTIONS = 200


class JevError(RuntimeError):
    pass


@dataclass
class Decision:
    """One typed answer with the probability mass behind it."""

    choice: Optional[str] = None
    confidence: float = 0.0
    probabilities: Dict[str, float] = field(default_factory=dict)
    noul: Optional[float] = None
    score: Optional[float] = None
    latency_ms: int = 0
    cost: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def certain(self) -> bool:
        """True when the answer is in the band the model is reliably right in."""
        return self.confidence >= 0.9

    def runner_up_gap(self) -> float:
        """Distance to the second choice: a thin gap means a coin flip."""
        ranked = sorted(self.probabilities.values(), reverse=True)
        return ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0] if ranked else 0.0


def _load_key(path: Path) -> str:
    """Pull the OpenRouter key out of the credentials file.

    Deliberately a regex rather than a YAML dependency: the file holds one value
    we need, and an auth loader should not be able to fail on someone else's
    indentation.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise JevError(f"could not read credentials at {path}: {error}") from error
    match = re.search(r"^\s*openrouter\s*:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        raise JevError(f"no 'openrouter:' key found in {path}")
    return match.group(1).strip().strip("\"'")


@dataclass
class JevClient:
    api_key: str
    model: str = DEFAULT_MODEL
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    total_cost: float = 0.0
    call_count: int = 0

    @classmethod
    def from_credentials(
        cls, path: Optional[Path] = None, **kwargs: Any
    ) -> "JevClient":
        credentials = path or Path(__file__).resolve().parents[2] / "credentials" / "api_keys.yaml"
        return cls(api_key=_load_key(Path(credentials)), **kwargs)

    # -- questions -------------------------------------------------------

    def choose(
        self,
        *,
        instructions: str,
        options: Mapping[str, str],
        state: Any,
        question_key: str = "selection",
    ) -> Decision:
        """Pick one option. Options map a key you can act on to its description."""
        if not options:
            raise JevError("choose() needs at least one option")
        trimmed = dict(list(options.items())[:MAX_OPTIONS])
        return self._ask(
            question_key,
            {"type": "choice", "instructions": instructions, "criteria": trimmed},
            state,
        )

    def confirm(
        self,
        *,
        instructions: str,
        when_true: str,
        when_false: str,
        state: Any,
        question_key: str = "holds",
    ) -> Decision:
        """Yes/no with a probability -- used to check a replay actually worked."""
        return self._ask(
            question_key,
            {
                "type": "noul",
                "instructions": instructions,
                "criteria": {"true": when_true, "false": when_false},
            },
            state,
        )

    # -- transport -------------------------------------------------------

    def _ask(self, question_key: str, question: Dict[str, Any], state: Any) -> Decision:
        payload = {
            "model": self.model,
            "state": state,
            "questions": {question_key: question},
        }
        request = urllib.request.Request(
            DECISIONS_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds, context=https_context()) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # Surface the server's own message; it names the bad field.
            detail = error.read().decode("utf-8", "replace")[:300]
            raise JevError(f"Jev request failed with status {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise JevError(f"Jev request failed: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise JevError("Jev returned invalid JSON.") from error

        latency_ms = int((time.perf_counter() - started) * 1000)
        answer = (body.get("answers") or {}).get(question_key)
        if not isinstance(answer, Mapping):
            raise JevError(f"Jev returned no answer for {question_key!r}.")

        cost = float((body.get("usage") or {}).get("cost") or 0.0)
        self.total_cost += cost
        self.call_count += 1

        probabilities = {
            str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()
        }
        confidence = answer.get("confidence")
        if confidence is None and answer.get("type") == "noul":
            # A noul carries its probability directly; express it as distance
            # from an even split so one threshold works for every question type.
            probability = float(answer.get("noul") or 0.0)
            confidence = abs(probability - 0.5) * 2.0
        return Decision(
            choice=answer.get("choice"),
            confidence=float(confidence or 0.0),
            probabilities=probabilities,
            noul=answer.get("noul"),
            score=answer.get("score"),
            latency_ms=latency_ms,
            cost=cost,
            raw=dict(body),
        )
