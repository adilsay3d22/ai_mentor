"""OpenRouter, behind the same ``LlmClient`` protocol as everything else.

OpenRouter speaks the OpenAI chat-completions format, not Anthropic's Messages
format, so this cannot be the ``anthropic`` SDK with a different base URL -- it
is its own client. That is exactly what the protocol in ``client.py`` is for:
the planner, the validation, and invariant 3 are all unchanged, and only the
transport differs.

``urllib`` from the standard library rather than ``requests`` or ``httpx``: this
is one POST with a JSON body, the dependency list in ``architecture.md`` is
meant to stay short, and hand-rolling ten lines beats adding a package for them.

Why this exists at all: OpenRouter carries both genuinely free models (useful
for exercising the loop without spending anything) and ``anthropic/claude-opus-5``
at the same price as going direct, behind one key.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

import structlog

from mentor import config
from mentor.planner.client import PlannerError

log = structlog.get_logger(__name__)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

#: Sent so OpenRouter can attribute traffic. Neither is required, and neither
#: carries anything about the user or what is on their screen.
_ATTRIBUTION = {
    "HTTP-Referer": "https://github.com/mentor",
    "X-Title": "Mentor",
}

#: Free models that advertise structured-output support, ordered by measured
#: round trip on a four-element Notepad prompt. Every one of these produced a
#: step that survived validation when it answered at all.
#:
#: Scored on four goals against a five-element Notepad list, counting how often
#: the chosen element was one a competent guide could defensibly point at, and
#: how often the instruction broke design.md's under-12-words rule:
#:
#:     model                                   correct  median  too long
#:     nex-agi/nex-n2.5-mini:free                 4/4     2.0s      0
#:     liquid/lfm-2.5-2.6b:free                   4/4     3.0s      0
#:     nex-agi/nex-n2.5-pro:free                  4/4     3.2s      0
#:     dots-studio/dots-3-note-preview:free       4/4     3.9s      0
#:     nvidia/nemotron-3-super-120b-a12b:free     3/4     4.2s      0   (1 empty)
#:     openrouter/free                            2/4     6.7s      0   (2 non-JSON)
#:     qwen/qwen3.8-27b:free                      0/4      --       -   (pool limited)
#:
#: Free models share an upstream pool across everyone using them, so
#: availability moves hour to hour and these numbers will not hold. The ordering
#: is a starting point for picking a fallback, not a promise.
FREE_MODELS_WITH_STRUCTURED_OUTPUT = (
    "nex-agi/nex-n2.5-mini:free",
    "liquid/lfm-2.5-2.6b:free",
    "nex-agi/nex-n2.5-pro:free",
    "dots-studio/dots-3-note-preview:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
    "qwen/qwen3.8-27b:free",
)

#: Retried with backoff. Everything else fails immediately, because retrying a
#: 400 just spends the user's time.
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _strip_code_fence(text: str) -> str:
    """Some models wrap JSON in a markdown fence even under structured output."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


class OpenRouterClient:
    """One request, one JSON answer, logged verbatim -- same contract as the rest."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = config.OPENROUTER_MODEL,
        max_tokens: int = config.PLANNER_MAX_TOKENS,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        key = api_key or config.openrouter_api_key()
        if not key:
            raise PlannerError(
                "No OpenRouter API key. Set OPENROUTER_API_KEY, or put it in a .env "
                "file in the project root (.env is gitignored)."
            )
        self._key = key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries

    def complete_json(self, system: str, user: str, schema: dict) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # OpenAI's structured-output shape, which OpenRouter forwards to any
            # model that supports it. Models that do not will ignore it, which
            # is why the parse below is still defensive.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "mentor_step", "strict": True, "schema": schema},
            },
        }

        log.info(
            "planner_request", provider="openrouter", model=self.model, system=system, user=user
        )
        started = time.perf_counter()
        body = self._post(json.dumps(payload).encode("utf-8"))
        elapsed = time.perf_counter() - started

        try:
            choice = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            log.error("openrouter_unexpected_shape", body=body)
            raise PlannerError("OpenRouter returned a response with no message.") from exc

        if not choice or not choice.strip():
            # Usually means the model hit a content filter or produced only a
            # reasoning block. Either way there is no answer.
            log.error("openrouter_empty_content", body=body)
            raise PlannerError("The model returned an empty answer.")

        usage = body.get("usage") or {}
        log.info(
            "planner_response",
            provider="openrouter",
            model=body.get("model", self.model),
            seconds=round(elapsed, 3),
            finish_reason=(body["choices"][0] or {}).get("finish_reason"),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
            # OpenRouter reports actual spend per request when it has it, which
            # beats guessing from a price table that may be stale.
            cost_usd=usage.get("cost"),
            raw=choice,
        )

        try:
            parsed = json.loads(_strip_code_fence(choice))
        except json.JSONDecodeError as exc:
            log.error("planner_returned_invalid_json", raw=choice, error=str(exc))
            raise PlannerError(
                f"{self.model} did not return valid JSON. Try a model that supports "
                "structured outputs."
            ) from exc

        if not isinstance(parsed, dict):
            raise PlannerError("The model returned JSON that was not an object.")
        return parsed

    def _post(self, data: bytes) -> dict[str, Any]:
        """POST with backoff on the statuses worth retrying."""
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            # The URL is a module constant, never user input.
            request = urllib.request.Request(
                ENDPOINT,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    **_ATTRIBUTION,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                if exc.code == 401:
                    raise PlannerError("The OpenRouter API key was rejected.") from exc
                if exc.code == 402:
                    raise PlannerError(
                        "OpenRouter says this request needs credit. Free models end in ':free'."
                    ) from exc
                if exc.code == 429 and "rate-limited upstream" in detail:
                    # The free pool for this model is saturated across every user
                    # of it, not something this key did. Retrying will not help
                    # within a session, so say what will.
                    alternatives = ", ".join(
                        m for m in FREE_MODELS_WITH_STRUCTURED_OUTPUT if m != self.model
                    )
                    log.warning("openrouter_free_pool_saturated", model=self.model)
                    raise PlannerError(
                        f"{self.model} is rate-limited upstream — its free pool is busy, "
                        f"which is not about your key. Try another free model with "
                        f"--model, for example: {alternatives.split(', ')[0]}"
                    ) from exc
                if exc.code not in _RETRYABLE_STATUS:
                    log.error("openrouter_http_error", status=exc.code, detail=detail)
                    raise PlannerError(f"OpenRouter error {exc.code}: {detail}") from exc
                last_error = exc
                log.warning("openrouter_retrying", status=exc.code, attempt=attempt, detail=detail)
            except urllib.error.URLError as exc:
                last_error = exc
                log.warning("openrouter_connection_retrying", attempt=attempt, error=str(exc))
            except TimeoutError as exc:
                last_error = exc
                log.warning("openrouter_timeout_retrying", attempt=attempt)

            if attempt < self.max_retries:
                time.sleep(2**attempt)

        raise PlannerError(f"Could not reach OpenRouter: {last_error}")
