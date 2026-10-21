"""The only module in this codebase allowed to import `openai`.

Every LLM/AI call in DeepDive routes through MeshClient.complete_json, which
enforces, in one place:
  - Mesh as the sole gateway (base_url="https://api.meshapi.ai/v1")
  - a Pydantic schema on every response — no freeform text generation
  - a disk cache keyed on (model, prompt_version, prompt hash)
  - retry with backoff on transient errors
  - a per-model circuit breaker with role-level fallback
  - a concurrency cap so parallel callers don't trigger 429 storms
  - one repair retry when a response fails schema validation

tests/unit/test_architecture.py enforces that nothing else in the codebase
imports `openai` or calls `.chat.completions.create(` directly. See
BLUEPRINT.md §1.2 and §8, and DESIGN.md §1 for why this exists.
"""

from __future__ import annotations

import re
import time
from asyncio import Semaphore
from pathlib import Path
from typing import TypeVar

import yaml
from openai import (
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.errors import LLMError, LLMOutputError
from llm.cache import DiskCache
from llm.circuit_breaker import CircuitBreaker

T = TypeVar("T", bound=BaseModel)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MODELS_PATH = Path(__file__).resolve().parent / "models.yaml"
PRICING_PATH = Path(__file__).resolve().parent / "pricing.yaml"

RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIStatusError, APIError)


def _load_pricing() -> dict:
    with open(PRICING_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int, pricing: dict | None = None) -> float:
    """USD estimate from llm/pricing.yaml (per 1K tokens). Falls back to
    the `default` entry for a model not listed there — this is an
    estimate for the self-eval cost metrics (BLUEPRINT.md §15), not a
    billing-accurate figure; confirm against Mesh's real pricing before
    trusting it for anything load-bearing."""
    pricing = pricing if pricing is not None else _load_pricing()
    rates = pricing.get(model, pricing["default"])
    return round((input_tokens / 1000) * rates["input"] + (output_tokens / 1000) * rates["output"], 6)


def _load_roles() -> dict:
    with open(MODELS_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["roles"]


def _prompt_version(prompt_name: str) -> str:
    match = re.search(r"\.(v\d+)$", prompt_name)
    return match.group(1) if match else "v0"


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_markdown_fences(text: str) -> str:
    """Found on the first live run (DESIGN.md §16): some models — notably
    Claude via this gateway — wrap JSON output in markdown code fences
    despite `response_format={"type": "json_object"}` and an explicit
    "no markdown fences" instruction. Strip them before parsing rather
    than treating well-formed-but-fenced JSON as a schema failure."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text


class MeshClient:
    """Async wrapper around the OpenAI SDK pointed at Mesh. Construct via
    `from_settings()` in the app; construct directly (as tests do) to inject
    a fake api_key / disabled cache / isolated breaker."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.meshapi.ai/v1",
        cache: DiskCache | None = None,
        breaker: CircuitBreaker | None = None,
        max_concurrency: int = 4,
        repo: object | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._roles = _load_roles()
        self._pricing = _load_pricing()
        self._cache = cache if cache is not None else DiskCache(directory="./.cache/llm", enabled=True)
        self._breaker = breaker if breaker is not None else CircuitBreaker()
        self._semaphore = Semaphore(max_concurrency)
        self._repo = repo

    @classmethod
    def from_settings(cls, settings, repo: object | None = None) -> "MeshClient":
        return cls(
            api_key=settings.mesh_api_key,
            base_url=settings.mesh_base_url,
            cache=DiskCache(directory=settings.llm_cache_dir, enabled=settings.llm_cache_enabled),
            breaker=CircuitBreaker(
                failure_threshold=settings.cb_failure_threshold,
                cooldown_seconds=settings.cb_cooldown_seconds,
            ),
            max_concurrency=settings.mesh_max_concurrency,
            repo=repo,
        )

    def _render_prompt(self, prompt_name: str, variables: dict) -> str:
        path = PROMPTS_DIR / f"{prompt_name}.md"
        template = path.read_text(encoding="utf-8")
        try:
            return template.format(**variables)
        except KeyError as exc:
            raise LLMError(f"prompt '{prompt_name}' missing variable {exc}") from exc

    async def complete_json(
        self,
        node: str,
        prompt_name: str,
        variables: dict,
        schema: type[T],
        model_role: str,
        run_id: str | None = None,
        temperature: float = 0.0,
    ) -> T:
        """The sole entry point for every LLM call in this codebase. Always
        returns an instance of `schema`, or raises LLMError/LLMOutputError —
        it never returns raw text."""
        if model_role not in self._roles:
            raise LLMError(f"unknown model role: {model_role}")

        role_cfg = self._roles[model_role]
        primary = role_cfg["model"]
        fallback = role_cfg.get("fallback")
        prompt_version = _prompt_version(prompt_name)
        rendered = self._render_prompt(prompt_name, variables)

        candidates = [primary]
        if fallback and fallback != primary:
            candidates.append(fallback)
        if fallback and self._breaker.is_open(primary):
            candidates = [c for c in reversed(candidates)]  # try fallback first

        last_exc: Exception | None = None
        for model in candidates:
            if self._breaker.is_open(model) and model != candidates[-1]:
                continue
            try:
                return await self._complete_with_model(
                    node, model, prompt_version, rendered, schema, run_id, temperature
                )
            except Exception as exc:  # noqa: BLE001 — deliberate: try next candidate, including on LLMOutputError.
                # Found on the first live run (DESIGN.md §16): a model
                # consistently failing schema validation (e.g. wrapping
                # JSON in markdown fences) is a model-specific formatting
                # quirk, not necessarily a universal prompt/schema
                # problem — the fallback model may simply not have that
                # quirk. Giving up immediately here silently broke
                # verification entirely; `last_exc` still chains the
                # original LLMOutputError for callers that inspect it.
                last_exc = exc
                continue

        raise LLMError(f"Mesh call failed for role '{model_role}' across all candidates: {last_exc}") from last_exc

    async def _complete_with_model(
        self,
        node: str,
        model: str,
        prompt_version: str,
        rendered: str,
        schema: type[T],
        run_id: str | None,
        temperature: float,
    ) -> T:
        cache_key = DiskCache.key(model, prompt_version, rendered)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._record_call(run_id, node, model, prompt_version, 0, 0, 0.0, 0, cached=True, error=None)
            return schema.model_validate_json(_strip_markdown_fences(cached))

        start = time.monotonic()
        try:
            raw, usage = await self._call_with_retry(model, rendered, schema, temperature)
        except Exception as exc:
            self._breaker.record_failure(model)
            latency_ms = int((time.monotonic() - start) * 1000)
            self._record_call(run_id, node, model, prompt_version, 0, 0, 0.0, latency_ms, cached=False, error=str(exc))
            raise LLMError(f"Mesh call failed for model '{model}': {exc}") from exc

        self._breaker.record_success(model)
        latency_ms = int((time.monotonic() - start) * 1000)
        cleaned = _strip_markdown_fences(raw)
        self._cache.set(cache_key, cleaned)
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cost_usd = estimate_cost_usd(model, input_tokens, output_tokens, self._pricing)
        self._record_call(
            run_id, node, model, prompt_version,
            input_tokens, output_tokens,
            cost_usd, latency_ms, cached=False, error=None,
        )

        try:
            return schema.model_validate_json(cleaned)
        except ValidationError as exc:
            repaired = await self._repair(model, rendered, cleaned, exc, schema)
            if repaired is not None:
                return repaired
            raise LLMOutputError(f"model output failed schema '{schema.__name__}': {exc}") from exc

    async def _call_with_retry(
        self, model: str, rendered_prompt: str, schema: type[T], temperature: float
    ) -> tuple[str, dict]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(RETRYABLE_ERRORS),
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            reraise=True,
        ):
            with attempt:
                async with self._semaphore:
                    response = await self._client.chat.completions.create(
                        model=model,
                        temperature=temperature,
                        response_format={"type": "json_object"},
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Respond ONLY with a single JSON object matching this schema "
                                    f"(no prose, no markdown fences): {schema.model_json_schema()}"
                                ),
                            },
                            {"role": "user", "content": rendered_prompt},
                        ],
                    )
                content = response.choices[0].message.content or "{}"
                usage = {
                    "input_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                    "output_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                }
                return content, usage
        raise LLMError(f"retry loop exhausted without a response for model '{model}'")

    async def _repair(
        self, model: str, rendered_prompt: str, bad_raw: str, validation_error: ValidationError, schema: type[T]
    ) -> T | None:
        try:
            async with self._semaphore:
                response = await self._client.chat.completions.create(
                    model=model,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Your previous output failed schema validation. Return a corrected "
                                f"single JSON object matching this schema exactly: {schema.model_json_schema()}"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Original request:\n{rendered_prompt}\n\n"
                                f"Your invalid output:\n{bad_raw}\n\n"
                                f"Validation error:\n{validation_error}"
                            ),
                        },
                    ],
                )
            content = _strip_markdown_fences(response.choices[0].message.content or "{}")
            return schema.model_validate_json(content)
        except Exception:
            return None

    def _record_call(
        self,
        run_id: str | None,
        node: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        cached: bool,
        error: str | None,
    ) -> None:
        if self._repo is None:
            return
        self._repo.record_llm_call(
            run_id=run_id,
            node=node,
            model=model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            cached=cached,
            error=error,
        )
