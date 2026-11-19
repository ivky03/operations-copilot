"""A small client for language-model providers. No framework: about 150 lines we fully own.

- Groq and Gemini both expose an OpenAI-compatible /chat/completions API, so one class serves
  both. Providers are tried in order: Groq first, then Gemini only if its key is configured.
- Temporary failures (timeouts, 5xx) get one retry, then the next provider. Rate limits (429)
  get up to two retries, waiting as long as the provider says (Retry-After or the token-reset
  header), capped at MAX_RATE_LIMIT_WAIT seconds.
- Replies must be JSON matching a Pydantic model. If one doesn't, the model is asked once more
  with the validation error ("repair"); a second failure raises LLMOutputError.
- Every attempt is recorded as a CallRecord (provider, model, tokens, latency, outcome) for
  the llm_calls table. API keys are SecretStr and never logged.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings

GROQ_URL = "https://api.groq.com/openai/v1"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
MAX_RATE_LIMIT_WAIT = 30.0

T = TypeVar("T", bound=BaseModel)


class LLMUnavailableError(RuntimeError):
    """No provider could answer (all failed or none configured)."""


class LLMOutputError(RuntimeError):
    """The model's reply didn't match the required structure, even after one repair attempt."""


@dataclass
class CallRecord:
    purpose: str
    provider: str
    model: str
    status: str  # ok | error | invalid_output
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass
class Completion:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None


class Provider(Protocol):
    name: str
    model: str

    def complete(self, messages: list[dict], timeout: float) -> Completion: ...


@dataclass
class OpenAICompatibleProvider:
    name: str
    base_url: str
    api_key: str
    model: str
    extra: dict = field(default_factory=dict)  # provider-specific options
    transport: httpx.BaseTransport | None = None  # tests inject a fake network here

    def complete(self, messages: list[dict], timeout: float) -> Completion:
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,  # as repeatable as the provider allows
            "response_format": {"type": "json_object"},
            **self.extra,
        }
        with httpx.Client(timeout=timeout, transport=self.transport) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage") or {}
        return Completion(
            text=data["choices"][0]["message"]["content"] or "",
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )


def _is_transient(error: Exception) -> bool:
    if isinstance(error, httpx.TimeoutException | httpx.TransportError):
        return True
    return (
        isinstance(error, httpx.HTTPStatusError) and error.response.status_code in TRANSIENT_STATUS
    )


def _parse_duration(value: str) -> float | None:
    """'2', '1.14s', '2m3.5s' or '450ms' → seconds."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    total, number = 0.0, ""
    for part in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value):
        number, unit = part
        total += float(number) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if number else None


def _rate_limit_wait(error: Exception) -> float | None:
    if not (isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429):
        return None
    headers = error.response.headers
    wait = _parse_duration(headers.get("retry-after", "")) or _parse_duration(
        headers.get("x-ratelimit-reset-tokens", "")
    )
    return min(wait if wait is not None else 5.0, MAX_RATE_LIMIT_WAIT)


def _describe(error: Exception) -> str:
    # Never include request headers (they carry the API key).
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


class LLMClient:
    def __init__(self, providers: list[Provider], timeout: float = 45.0, retry_delay: float = 1.0):
        self.providers = providers
        self.timeout = timeout
        self.retry_delay = retry_delay

    def complete_json(
        self, purpose: str, messages: list[dict], schema: type[T], calls: list[CallRecord]
    ) -> T:
        """Return the reply parsed as `schema`. Appends a CallRecord per attempt to `calls`."""
        if not self.providers:
            raise LLMUnavailableError("No language model provider is configured.")
        last_error: Exception | None = None
        for provider in self.providers:
            try:
                return self._with_repair(provider, purpose, messages, schema, calls)
            except LLMOutputError:
                raise  # the provider answered; switching providers won't fix our prompt
            except Exception as e:  # provider failed after retry: try the next one
                last_error = e
        raise LLMUnavailableError(
            f"The language model service is unavailable ({_describe(last_error)})."
        ) from last_error

    def _with_repair(self, provider, purpose, messages, schema, calls):
        text = self._call(provider, purpose, messages, calls)
        try:
            return schema.model_validate_json(_extract_json(text))
        except (ValidationError, ValueError) as first:
            calls[-1].status = "invalid_output"
            repair = [
                *messages,
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": "Your reply did not match the required JSON structure: "
                    f"{_short(first)}. Reply again with only the corrected JSON object.",
                },
            ]
            text = self._call(provider, f"{purpose}_repair", repair, calls)
            try:
                return schema.model_validate_json(_extract_json(text))
            except (ValidationError, ValueError) as second:
                calls[-1].status = "invalid_output"
                raise LLMOutputError(f"Model reply was not valid: {_short(second)}") from second

    def _call(self, provider: Provider, purpose: str, messages: list[dict], calls) -> str:
        transient_retries, rate_limit_retries = 1, 2
        while True:
            start = time.monotonic()
            try:
                completion = provider.complete(messages, self.timeout)
            except Exception as e:
                calls.append(
                    CallRecord(
                        purpose,
                        provider.name,
                        provider.model,
                        "error",
                        int((time.monotonic() - start) * 1000),
                        error=_describe(e),
                    )
                )
                wait = _rate_limit_wait(e)
                if wait is not None and rate_limit_retries > 0:
                    rate_limit_retries -= 1
                    time.sleep(wait + self.retry_delay)
                    continue
                if wait is None and _is_transient(e) and transient_retries > 0:
                    transient_retries -= 1
                    time.sleep(self.retry_delay)
                    continue
                raise
            calls.append(
                CallRecord(
                    purpose,
                    provider.name,
                    provider.model,
                    "ok",
                    int((time.monotonic() - start) * 1000),
                    completion.prompt_tokens,
                    completion.completion_tokens,
                )
            )
            return completion.text


def _extract_json(text: str) -> str:
    """Accept a bare JSON object, or one wrapped in a ```json fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").removeprefix("json").strip()
    json.loads(stripped)  # raises ValueError with a useful message if it isn't JSON
    return stripped


def _short(error: Exception) -> str:
    return str(error).replace("\n", " ")[:400]


def build_client(settings: Settings | None = None, purpose_model: str | None = None) -> LLMClient:
    """Groq first; Gemini as fallback if configured."""
    s = settings or get_settings()
    providers: list[Provider] = []
    if s.groq_api_key:
        providers.append(
            OpenAICompatibleProvider(
                "groq",
                GROQ_URL,
                s.groq_api_key.get_secret_value(),
                purpose_model or s.llm_planner_model,
                extra={"reasoning_effort": "low"},  # gpt-oss reasoning models: fast, sufficient
            )
        )
    if s.gemini_api_key:
        providers.append(
            OpenAICompatibleProvider(
                "gemini", GEMINI_URL, s.gemini_api_key.get_secret_value(), s.llm_fallback_model
            )
        )
    return LLMClient(providers, timeout=s.llm_timeout_seconds)
