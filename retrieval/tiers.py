"""Rule-based source tier classification (BLUEPRINT.md §7): 1 = highest
trust (government/regulators/standards bodies/peer-reviewed/primary
filings), 4 = lowest (forums/unknown blogs/content farms). Logic lives
here, in code, auditable — not a prompt, and it costs no Mesh call."""

from __future__ import annotations

TIER_1_SUFFIXES = (".gov", ".mil", ".int")
TIER_1_DOMAINS = frozenset(
    {
        "sec.gov", "who.int", "imf.org", "worldbank.org", "federalreserve.gov",
        "nature.com", "science.org", "nejm.org", "ncbi.nlm.nih.gov", "oecd.org",
    }
)
TIER_2_DOMAINS = frozenset(
    {
        "reuters.com", "apnews.com", "bloomberg.com", "wsj.com", "ft.com",
        "nytimes.com", "bbc.com", "bbc.co.uk", "economist.com", "npr.org",
        "washingtonpost.com", "theguardian.com",
    }
)
TIER_4_HINTS = ("reddit.com", "quora.com", "blogspot", "medium.com", "substack.com", "forum")


def classify_tier(domain: str | None) -> int:
    if not domain:
        return 4
    domain = domain.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    if domain in TIER_1_DOMAINS or domain.endswith(TIER_1_SUFFIXES):
        return 1
    if domain in TIER_2_DOMAINS:
        return 2
    if any(hint in domain for hint in TIER_4_HINTS):
        return 4
    return 3
