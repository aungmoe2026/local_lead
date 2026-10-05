"""Filters so leads stay focused on small independent high-street businesses.

Signals (applied in order):
  1. Explicit OSM "brand" tag — checked in overpass_search.py when present.
  2. Franchise / chain name blocklist (this module).
  3. Head-office / HQ name patterns.
  4. Retail-park / shopping-mall address or name phrases.
"""

import re
from typing import Dict, List, Optional, Tuple

from config import CHAIN_BLOCKLIST, HEAD_OFFICE_NAME_RE, RETAIL_PARK_PHRASES

_STRIP_RE = re.compile(r"[^a-z0-9 ]")


def _normalize(name: str) -> str:
    name = _STRIP_RE.sub(" ", (name or "").lower())
    return re.sub(r"\s+", " ", name).strip()


def is_chain(business_name: str) -> bool:
    normalized = _normalize(business_name)
    if not normalized:
        return False
    return any(chain in normalized for chain in CHAIN_BLOCKLIST)


def is_head_office(business_name: str) -> bool:
    return bool(HEAD_OFFICE_NAME_RE.search(business_name or ""))


def is_retail_park_location(business_name: str = "", address: str = "") -> bool:
    blob = f"{business_name or ''} {address or ''}".lower()
    return any(phrase in blob for phrase in RETAIL_PARK_PHRASES)


def reject_reason(business_name: str, address: str = "") -> Optional[str]:
    """Return a short reject reason, or None if the listing is OK to keep."""
    if is_chain(business_name):
        return "chain"
    if is_head_office(business_name):
        return "head_office"
    if is_retail_park_location(business_name, address):
        return "retail_park"
    return None


def filter_out_chains(rows: List[Dict]) -> List[Dict]:
    return [r for r in rows if not is_chain(r.get("business_name", ""))]


def filter_low_quality_leads(rows: List[Dict]) -> Tuple[List[Dict], Dict[str, int]]:
    """Drop chains, HQs, and retail-park sites. Returns (kept, reason_counts)."""
    kept: List[Dict] = []
    counts: Dict[str, int] = {"chain": 0, "head_office": 0, "retail_park": 0}
    for row in rows:
        reason = reject_reason(
            row.get("business_name", "") or "",
            row.get("address", "") or "",
        )
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
            continue
        kept.append(row)
    return kept, counts
