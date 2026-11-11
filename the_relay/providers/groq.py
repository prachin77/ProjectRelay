"""Real Groq adapter (OpenAI-compatible chat completions).

WHY DIRECT HTTP INSTEAD OF THE SDK?
-----------------------------------
The brief demands that we find the real limits from what the service sends back
and that retries are visible. An SDK may hide retries or normalise headers. A
direct ``httpx`` call gives us:

* every response header (``x-ratelimit-*``, ``retry-after``);
* the exact HTTP status and error body;
* control over retries (there are none inside this adapter);
* time-to-first-token from the streaming response.

DOCUMENTED FACTS USED HERE (verified 2026-11-10)
-----------------------------------------------
* Base URL: ``https://api.groq.com/openai/v1``.
* Chat completions: ``POST /chat/completions``, OpenAI-compatible.
* 429 on a rate limit; ``retry-after`` is only present on 429.
* ``x-ratelimit-limit-requests`` is RPD, ``x-ratelimit-limit-tokens`` is TPM.
* Free-tier limits for ``openai/gpt-oss-120b``: 30 RPM, 1,000 RPD, 8,000 TPM,
  200,000 TPD.

UNKNOWNS
--------
Whether our account exposes separate ITPM/OTPM limits, and whether every
response includes every header. The adapter records whatever is present and
never fabricates a missing value.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from the_relay.config import Settings
from the_relay.core.tokens import estimate_messages_tokens, estimate_tokens
from the_relay.errors import (
    ConfigurationError,
    PermanentProviderError,
    ProviderTimeoutError,
    RateLimitError,
    TransientProviderError,
)
from the_relay.obs.traces import utc_now_iso
from the_relay.providers.base import ModelProvider, TokenCallback
from the_relay.types import ModelRequest, ModelResponse, ModelUsage

_DURATION_RE = re.compile(
    r"(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", re.IGNORECASE
)


def parse_retry_after(value: str | None) -> float | None:
    """Parse ``retry-after`` or an ``x-ratelimit-reset-*`` value.

    The documented header is seconds (``retry-after: 2``). The documented reset
    headers look like ``7.66s`` or ``2m59.56s``. Return ``None`` when the value
    is missing or unparseable; the caller must then choose a conservative
    backoff instead of pretending it knows the reset time.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    match = _DURATION_RE.fullmatch(value)
    if not match:
        return None
    minutes = float(match.group(1) or 0)
    seconds = float(match.group(2) or 0)
    return minutes * 60.0 + seconds


def _rate_limit_headers(headers: httpx.Headers) -> dict[str, str]:
    """Keep only the documented rate-limit headers (lower-cased names)."""
    wanted = {
        "retry-after",
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
    }
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in wanted
    }


class GroqProvider(ModelProvider):
    """A deliberately thin, observable Groq client."""

    source = "real"

    def __init__(self, settings: Settings) -> None:
        if not settings.groq_api_key:
            raise ConfigurationError(
                "GROQ_API_KEY is not set. Add it to .env or switch "
                "RELAY_PROVIDER=standin. No real request was sent."
            )
        self.settings = settings
        self.model_id = settings.groq_model
        self._client = httpx.AsyncClient(
            base_url=settings.groq_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.groq_request_timeout_ms / 1000.0),
            # Explicitly disable transport-level retries. Every retry decision
            # must be visible to our retry policy and our traces.
            transport=httpx.AsyncHTTPTransport(retries=0),
            headers={
                "Authorization": f"Bearer {settings.groq_api_key}",
                "Content-Type": "application/json",
            },
        )

    async def generate(
        self,
        request: ModelRequest,
        on_token: TokenCallback | None = None,
    ) -> ModelResponse:
        """Send one streaming chat completion and capture its measurements."""
        started = time.monotonic()
        started_at = utc_now_iso()
        payload: dict[str, Any] = {
            "model": self.model_id,
            "messages": request.messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            # Ask for usage in the final SSE chunk. If the provider ignores it,
            # we fall back to the documented estimate and mark it as estimated.
            "stream_options": {"include_usage": True},
        }

        text_parts: list[str] = []
        first_token_ms: float | None = None
        usage: ModelUsage | None = None
        finish_reason: str | None = None
        response_headers: dict[str, str] = {}
        status_code: int | None = None

        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=payload
            ) as response:
                status_code = response.status_code
                response_headers = _rate_limit_headers(response.headers)
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._classify_http_error(
                        response.status_code, body, response_headers
                    )

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        # A malformed SSE frame is a provider protocol problem.
                        # Do not invent content from it.
                        continue

                    chunk_usage = chunk.get("usage")
                    if chunk_usage:
                        usage = ModelUsage(
                            input_tokens=int(chunk_usage.get("prompt_tokens", 0) or 0),
                            output_tokens=int(chunk_usage.get("completion_tokens", 0) or 0),
                            total_tokens=int(chunk_usage.get("total_tokens", 0) or 0),
                            estimated=False,
                        )

                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        if first_token_ms is None:
                            first_token_ms = (time.monotonic() - started) * 1000.0
                        text_parts.append(content)
                        if on_token is not None:
                            result = on_token(
                                content, (time.monotonic() - started) * 1000.0
                            )
                            if hasattr(result, "__await__"):
                                await result  # type: ignore[func-returns-value]
                    if choice.get("finish_reason"):
                        finish_reason = str(choice["finish_reason"])
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"Groq request timed out: {exc}") from exc
        except httpx.TransportError as exc:
            raise TransientProviderError(f"Groq connection error: {exc}") from exc

        ended = time.monotonic()
        text = "".join(text_parts).strip()
        if usage is None:
            # The provider did not return usage. Estimate and label it.
            usage = ModelUsage(
                input_tokens=estimate_messages_tokens(request.messages),
                output_tokens=estimate_tokens(text),
                estimated=True,
            )
        return ModelResponse(
            text=text,
            model_id=self.model_id,
            source="real",
            usage=usage,
            first_token_ms=first_token_ms,
            total_ms=(ended - started) * 1000.0,
            http_status=status_code,
            finish_reason=finish_reason,
            rate_limit_headers=response_headers,
            started_at=started_at,
            ended_at=utc_now_iso(),
        )

    @staticmethod
    def _classify_http_error(
        status_code: int,
        body: str,
        headers: dict[str, str],
    ) -> Exception:
        """Turn an HTTP error into a typed, retry-aware exception.

        The full body is not put into the exception message because a provider
        error body can be large; the first 400 characters are enough for a
        trace and contain no credential.
        """
        message = body[:400].replace("\n", " ")
        retry_after = parse_retry_after(headers.get("retry-after"))
        if retry_after is None:
            retry_after = parse_retry_after(headers.get("x-ratelimit-reset-tokens"))
        if status_code == 429:
            return RateLimitError(
                f"Groq 429 rate limit: {message}",
                retry_after_s=retry_after,
                http_status=429,
            )
        if status_code in {408, 409, 425, 500, 502, 503, 504}:
            return TransientProviderError(
                f"Groq {status_code} transient error: {message}",
                http_status=status_code,
            )
        return PermanentProviderError(
            f"Groq {status_code} error: {message}",
            http_status=status_code,
        )

    async def list_models(self) -> dict[str, Any]:
        """Fetch the account's model list (used by the probe)."""
        response = await self._client.get("/models")
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
