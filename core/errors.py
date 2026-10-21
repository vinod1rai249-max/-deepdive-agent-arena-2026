class DeepDiveError(Exception):
    """Base class for all DeepDive errors. Nodes catch this (and subclasses),
    record the failure in the run trail, and keep going — see BLUEPRINT.md §10."""


class RetrievalError(DeepDiveError):
    """Fetching or scraping a source failed."""


class LLMError(DeepDiveError):
    """A Mesh call failed after retries/fallback were exhausted."""


class LLMOutputError(LLMError):
    """A model's output could not be made to satisfy its Pydantic schema,
    even after one repair retry."""


class VerificationError(DeepDiveError):
    """A verification stage could not be completed (not the same as a claim
    failing verification, which is a normal Verdict, not an error)."""


class BudgetExceeded(DeepDiveError):
    """A run hit its USD/token/time ceiling."""


class GuardrailViolation(DeepDiveError):
    """An input or output violated a safety/shape guardrail
    (e.g. SSRF-blocked URL, oversized question, untrusted-content leak)."""
