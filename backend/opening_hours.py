"""Parse Google Maps / Apify opening hours and match dial-window slots.

Dial windows (agent call times):
  am1  09:00–11:30
  am2  11:30–13:00
  pm1  13:00–15:00
  pm2  15:00–17:00
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Minutes from midnight
Slot = Tuple[int, int]  # start_min, end_min (end exclusive of overnight edge cases)

DIAL_SLOTS: Dict[str, Tuple[str, str, str]] = {
    # id -> (label, start HH:MM, end HH:MM)
    "am1": ("09:00–11:30", "09:00", "11:30"),
    "am2": ("11:30–13:00", "11:30", "13:00"),
    "pm1": ("13:00–15:00", "13:00", "15:00"),
    "pm2": ("15:00–17:00", "15:00", "17:00"),
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

_DAY_ALIASES = {
    "mon": "Monday",
    "monday": "Monday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "tuesday": "Tuesday",
    "wed": "Wednesday",
    "wednesday": "Wednesday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "thursday": "Thursday",
    "fri": "Friday",
    "friday": "Friday",
    "sat": "Saturday",
    "saturday": "Saturday",
    "sun": "Sunday",
    "sunday": "Sunday",
}

_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?",
    re.IGNORECASE,
)
_RANGE_SPLIT = re.compile(r"\s*(?:–|—|-|to|until)\s*", re.IGNORECASE)
_CLOSED_RE = re.compile(r"^\s*(closed|—|-)\s*$", re.IGNORECASE)
_OPEN_24_RE = re.compile(r"open\s*24|24\s*hours|all\s*day", re.IGNORECASE)


def _hhmm_to_min(hhmm: str) -> int:
    parts = hhmm.strip().split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    return h * 60 + m


def _parse_clock(token: str) -> Optional[int]:
    """Parse '9 AM', '09:00', '5:30pm' → minutes from midnight."""
    token = token.strip().lower().replace("\u202f", " ").replace("\u2009", " ")
    m = _TIME_RE.search(token)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").replace(".", "").lower()
    if ampm.startswith("p") and hour < 12:
        hour += 12
    elif ampm.startswith("a") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _parse_interval_text(hours_text: str) -> List[Tuple[int, int]]:
    """Parse a day's hours string into (start_min, end_min) intervals."""
    text = (hours_text or "").strip()
    if not text or _CLOSED_RE.match(text):
        return []
    if _OPEN_24_RE.search(text):
        return [(0, 24 * 60)]

    # Multiple ranges: "9 AM - 12 PM, 2 PM - 5 PM"
    chunks = re.split(r"[,;/]| and ", text, flags=re.IGNORECASE)
    intervals: List[Tuple[int, int]] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or _CLOSED_RE.match(chunk):
            continue
        parts = _RANGE_SPLIT.split(chunk, maxsplit=1)
        if len(parts) != 2:
            continue
        start = _parse_clock(parts[0])
        end = _parse_clock(parts[1])
        if start is None or end is None:
            continue
        if end <= start:
            # Overnight e.g. 10 PM - 2 AM → treat as open until midnight for dial windows
            end = 24 * 60
        intervals.append((start, end))
    return intervals


def _normalize_day(raw: str) -> Optional[str]:
    key = re.sub(r"[^a-z]", "", (raw or "").lower())
    return _DAY_ALIASES.get(key)


def parse_opening_hours(raw: Any) -> Dict[str, List[List[str]]]:
    """Normalize Apify/Maps openingHours into {Day: [["HH:MM","HH:MM"], ...]}.

    Accepts:
      - [{"day": "Monday", "hours": "9 AM - 5 PM"}, ...]
      - [{"day": "Monday", "hours": ["9 AM - 12 PM", "2 - 5 PM"]}, ...]
      - {"Monday": "9 AM - 5 PM", ...}
      - already-normalized dict with [["09:00","17:00"]] lists
    """
    result: Dict[str, List[List[str]]] = {}
    if not raw:
        return result

    def _add(day: str, intervals: Sequence[Tuple[int, int]]) -> None:
        if not intervals:
            return
        result[day] = [
            [f"{s // 60:02d}:{s % 60:02d}", f"{e // 60:02d}:{e % 60:02d}"]
            for s, e in intervals
        ]

    if isinstance(raw, dict):
        # Already normalized or day→string map
        for day_raw, value in raw.items():
            day = _normalize_day(str(day_raw)) or (
                day_raw if day_raw in WEEKDAYS or day_raw in ("Saturday", "Sunday") else None
            )
            if not day:
                continue
            if isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
                # [["09:00","17:00"]]
                intervals = []
                for pair in value:
                    if len(pair) >= 2:
                        try:
                            intervals.append((_hhmm_to_min(str(pair[0])), _hhmm_to_min(str(pair[1]))))
                        except (ValueError, TypeError):
                            continue
                _add(day, intervals)
            elif isinstance(value, list):
                intervals: List[Tuple[int, int]] = []
                for item in value:
                    intervals.extend(_parse_interval_text(str(item)))
                _add(day, intervals)
            else:
                _add(day, _parse_interval_text(str(value)))
        return result

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            day = _normalize_day(str(entry.get("day") or entry.get("Day") or ""))
            if not day:
                continue
            hours = entry.get("hours")
            if hours is None:
                hours = entry.get("time") or entry.get("Hours")
            if isinstance(hours, list):
                intervals = []
                for h in hours:
                    intervals.extend(_parse_interval_text(str(h)))
                _add(day, intervals)
            else:
                _add(day, _parse_interval_text(str(hours or "")))
    return result


def format_opening_hours_display(hours: Dict[str, List[List[str]]]) -> str:
    """Compact one-line summary for CSV / UI."""
    if not hours:
        return ""
    parts: List[str] = []
    for day in list(WEEKDAYS) + ["Saturday", "Sunday"]:
        intervals = hours.get(day)
        if not intervals:
            continue
        ranges = ", ".join(f"{a}–{b}" for a, b in intervals)
        parts.append(f"{day[:3]} {ranges}")
    return "; ".join(parts)


def _intervals_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def open_during_slot(
    hours: Dict[str, List[List[str]]],
    slot_start_hhmm: str,
    slot_end_hhmm: str,
    weekdays_only: bool = True,
) -> bool:
    """True if open with any overlap vs the slot on at least one weekday (default)."""
    if not hours:
        return False
    slot_start = _hhmm_to_min(slot_start_hhmm)
    slot_end = _hhmm_to_min(slot_end_hhmm)
    days = WEEKDAYS if weekdays_only else list(hours.keys())
    for day in days:
        for pair in hours.get(day) or []:
            if len(pair) < 2:
                continue
            try:
                open_m = _hhmm_to_min(pair[0])
                close_m = _hhmm_to_min(pair[1])
            except (ValueError, TypeError):
                continue
            if _intervals_overlap(open_m, close_m, slot_start, slot_end):
                return True
    return False


def matching_dial_slots(hours: Dict[str, List[List[str]]]) -> List[str]:
    """Assign the lead to at most one dial window (earliest open slot).

    Exclusive so dial-window CSVs never duplicate the same business.
    """
    for slot_id, (_label, start, end) in DIAL_SLOTS.items():
        if open_during_slot(hours, start, end):
            return [slot_id]
    return []


def primary_dial_slot(hours: Dict[str, List[List[str]]]) -> Optional[str]:
    slots = matching_dial_slots(hours)
    return slots[0] if slots else None


def lead_matches_slot(lead: Dict, slot_id: str) -> bool:
    """True if this lead's exclusive dial window is slot_id."""
    if slot_id not in DIAL_SLOTS:
        return False
    hours = lead.get("opening_hours")
    if isinstance(hours, dict) and hours:
        return primary_dial_slot(hours) == slot_id
    slots = lead.get("dial_slots")
    if isinstance(slots, list) and slots:
        # Exclusive: only the first (earliest) assignment counts
        return slots[0] == slot_id
    return False


def count_slots(leads: Sequence[Dict]) -> Dict[str, int]:
    return {
        slot_id: sum(1 for lead in leads if lead_matches_slot(lead, slot_id))
        for slot_id in DIAL_SLOTS
    }
