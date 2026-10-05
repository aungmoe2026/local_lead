"""Rough wall-clock ETA for a lead job (wider Maps discovery + optional enrich).

Not a promise — Apify crawl time varies a lot by place density and phrase count.
Tuned from observed city runs (e.g. Edinburgh garages ~80–120s per Apify pass).
"""

from typing import List, Tuple

from config import ENRICHMENT_WORKERS, SATELLITE_RING_POINTS
from apify_maps import build_search_phrases


def estimate_job_seconds(
    categories: List[str],
    name_terms: List[str],
    target_leads: int,
    enrich: bool,
    include_county: bool = True,
) -> Tuple[int, int, str]:
    """Return (low_seconds, high_seconds, human_summary).

    Low/high is a band so the UI can say "~8–15 min" rather than a false precision.
    """
    phrases = build_search_phrases(categories, name_terms)
    n_phrases = max(1, len(phrases))
    target = max(1, min(1000, int(target_leads or 100)))

    # One Maps crawl cost grows with phrase count (Apify runs them together,
    # but scrolling more searches takes longer).
    seconds_per_crawl = 55 + 10 * n_phrases

    # Wider discovery: small targets often finish in 1 crawl; larger ones
    # typically expand radius and may hit the satellite ring.
    expected_crawls = 1.0
    if target >= 120:
        expected_crawls += 1.2  # likely one radius widen
    if target >= 220:
        expected_crawls += float(SATELLITE_RING_POINTS) * 0.85
    if include_county and target >= 120:
        expected_crawls += 1.0  # parent county / wider-area pass when still short

    discovery = expected_crawls * seconds_per_crawl
    geocode = 5.0  # may include a second Nominatim lookup for parent area

    enrich_secs = 0.0
    if enrich:
        # Parallel website + CH lookups; ~6–10s wall per batch of workers.
        batches = max(1.0, target / max(1, ENRICHMENT_WORKERS))
        enrich_secs = batches * 8.0

    mid = geocode + discovery + enrich_secs
    low = int(max(60, mid * 0.7))
    high = int(max(low + 30, mid * 1.35))
    summary = format_eta_band(low, high)
    return low, high, summary


def format_eta_band(low_seconds: int, high_seconds: int) -> str:
    """Human band like 'about 8-15 minutes'."""
    def _fmt(secs: int) -> str:
        if secs < 90:
            return f"{max(1, secs // 15 * 15)} sec"
        mins = max(1, round(secs / 60))
        if mins < 60:
            return f"{mins} min"
        hours = mins // 60
        rem = mins % 60
        if rem == 0:
            return f"{hours} hr"
        return f"{hours} hr {rem} min"

    lo, hi = _fmt(low_seconds), _fmt(high_seconds)
    if lo == hi:
        return f"about {lo}"
    return f"about {lo}-{hi}"
