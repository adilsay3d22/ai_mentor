"""The LLM client: one request, one JSON answer, logged verbatim.

Behind a Protocol so the planner can be tested against a fake. CLAUDE.md is
explicit that tests never call a live API, and that every planner request and
response is logged verbatim -- when a highlight lands on the wrong thing, the
transcript is the only way to tell whether the planner chose badly or the
grounder handed it a bad list.

SR2 note: this is the one place in Mentor that sends anything off the machine,
and it sends *text* -- the element labels and the goal -- never pixels. The
screenshot never leaves the process.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol, runtime_checkable

import structlog

from mentor import config

log = structlog.get_logger(__name__)

#: Per-million-token prices for the default model, used only to log what a
#: session cost. Wrong prices produce a wrong log line and nothing else.
INPUT_COST_PER_MTOK = 5.00
OUTPUT_COST_PER_MTOK = 25.00


class PlannerError(RuntimeError):
    """The planner could not produce an answer. Carries something sayable."""


@runtime_checkable
class LlmClient(Protocol):
    """Anything that can turn a prompt into one JSON object."""

    def complete_json(self, system: str, user: str, schema: dict) -> dict[str, Any]:
        """Return the model's answer, already parsed. Raises PlannerError on failure."""
        ...


class AnthropicClient:
    """The real client.

    Structured outputs rather than free text: ``output_config.format`` with a
    JSON schema means the API itself will not return anything that does not fit
    the shape, which removes an entire class of parse failures. It does *not*
    remove the need to validate -- the schema cannot know which element ids exist
    this frame, and that check lives in ``planner.py``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = config.PLANNER_MODEL,
        effort: str = config.PLANNER_EFFORT,
        max_tokens: int = config.PLANNER_MAX_TOKENS,
    ) -> None:
        import anthropic

        key = api_key or config.api_key()
        if not key:
            raise PlannerError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY, or put it in a .env "
                "file in the project root (.env is gitignored)."
            )
        # The SDK already retries 429 and 5xx with backoff; two is its default
        # and is right for an interactive tool, where a slow answer is worse than
        # an honest failure.
        self._client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=30.0)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def complete_json(self, system: str, user: str, schema: dict) -> dict[str, Any]:
        import anthropic

        log.info("planner_request", model=self.model, effort=self.effort, system=system, user=user)
        started = time.perf_counter()

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": self.effort,
                },
            )
        except anthropic.AuthenticationError as exc:
            raise PlannerError("The Anthropic API key was rejected.") from exc
        except anthropic.RateLimitError as exc:
            raise PlannerError("Rate limited by the Anthropic API. Try again shortly.") from exc
        except anthropic.APIConnectionError as exc:
            raise PlannerError("Could not reach the Anthropic API. Check the network.") from exc
        except anthropic.APIStatusError as exc:
            raise PlannerError(f"Anthropic API error {exc.status_code}.") from exc

        elapsed = time.perf_counter() - started

        if response.stop_reason == "refusal":
            log.warning("planner_refused", stop_details=str(response.stop_details))
            raise PlannerError("The model declined to answer this request.")

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            log.error("planner_response_had_no_text", stop_reason=response.stop_reason)
            raise PlannerError("The model returned no answer.")

        usage = response.usage
        log.info(
            "planner_response",
            seconds=round(elapsed, 3),
            stop_reason=response.stop_reason,
            request_id=getattr(response, "_request_id", None),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=round(
                usage.input_tokens / 1e6 * INPUT_COST_PER_MTOK
                + usage.output_tokens / 1e6 * OUTPUT_COST_PER_MTOK,
                5,
            ),
            # Verbatim, per CLAUDE.md. This is the record that says whether the
            # planner chose badly or was handed a bad element list.
            raw=text,
        )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # output_config.format should make this impossible. If it happens,
            # the assumption is wrong and that is worth knowing loudly.
            log.error("planner_returned_invalid_json", raw=text, error=str(exc))
            raise PlannerError("The model returned malformed JSON.") from exc

        if not isinstance(parsed, dict):
            raise PlannerError("The model returned JSON that was not an object.")
        return parsed


class FakeLlmClient:
    """A scripted client for tests. Never touches the network.

    Returns each queued answer in turn, recording what it was asked. A test that
    exercises the re-request path queues two answers and then asserts on
    ``self.requests``.
    """

    def __init__(self, answers: list[dict[str, Any] | Exception]) -> None:
        self.answers = list(answers)
        self.requests: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, schema: dict) -> dict[str, Any]:
        self.requests.append((system, user))
        if not self.answers:
            raise PlannerError("FakeLlmClient ran out of scripted answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
