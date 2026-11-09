"""One person's month, added up from the rosters that were recognised (standard library only).

A month is a list of days. Each day says what that day's roster shows for the person:

* ``DUTY``        on duty: how many hours (one roster row is one hour) and where they were spent
* ``OFF``         on the roster's name table, but without duties (輪休, 請假 or simply nothing that day)
* ``UNKNOWN``     there is a roster, but the name was not found on its name table — the hours of that
                  day, if any, are missing from the total until the person claims a code (「我是 72 10/5」)
* ``MISSING``     no roster of that day has been recognised
* ``PENDING``     the saved roster was made by an older reader and must be read again first; next time
* ``UNREADABLE``  the saved roster could not be opened

A day can also be ``partial``: its roster was read before the last four duty columns were (and could not
be read again), so hours spent there are missing.

These are the hours *planned* on the photographed roster, not attendance: a swap that never made it
onto a posted roster is not in here. An hour with two duties counts once, half to each place.

Overtime (加班) is the team's own rule, not the labour law's: on a day on duty, every hour past
``OVERTIME_AFTER`` (8) is overtime; a day of eight hours or less has none, and its spare hours do not make up
for another day's.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

DUTY, OFF, UNKNOWN, MISSING, PENDING, UNREADABLE = "duty", "off", "unknown", "missing", "pending", "unreadable"
OVERTIME_AFTER = 8   # hours of a day on duty before overtime starts

THIS_MONTH = ("本月統計", "這個月統計", "月統計", "我的統計", "統計", "本月時數", "時數統計", "工時統計")
LAST_MONTH = ("上月統計", "上個月統計", "上月時數", "上個月時數")
_KIND = r"(?:統計|時數統計|工時統計|時數)"
_NAMED = (re.compile(rf"(?:(\d{{4}})[年/])?(\d{{1,2}})月份?的?{_KIND}"),      # 9月統計、2026年9月統計
          re.compile(rf"(?:本月)?{_KIND}(?:(\d{{4}})[年/])?(\d{{1,2}})月份?"))   # 統計 9月


@dataclass
class Day:
    day: date
    status: str
    hours: int = 0
    places: dict[str, float] = field(default_factory=dict)   # place → hours
    unsure: int = 0          # hours with a low-confidence reading
    note: str = ""           # OFF: 輪休, or the kind of leave, when the roster says so
    code: str | None = None  # the person's code on that roster
    partial: bool = False    # read by an older reader that left out the last four duty columns

    @property
    def overtime(self) -> float:
        """The hours past ``OVERTIME_AFTER`` on a day on duty: 10 → 2, 8 or 7.5 → 0, and none on any other day."""
        return max(0, self.hours - OVERTIME_AFTER) if self.status == DUTY else 0


@dataclass
class Month:
    year: int
    month: int
    days: list[Day]

    def of(self, status: str) -> list[Day]:
        return [day for day in self.days if day.status == status]

    @property
    def hours(self) -> int:
        return sum(day.hours for day in self.days)

    @property
    def unsure(self) -> int:
        return sum(day.unsure for day in self.days)

    @property
    def overtime(self) -> float:
        """Each day's overtime added up (a short day does not take any off a long one)."""
        return sum(day.overtime for day in self.days)

    def places(self) -> list[tuple[str, float]]:
        """Hours per place over the month, the longest first (ties in name order)."""
        total: dict[str, float] = {}
        for day in self.days:
            for name, hours in day.places.items():
                total[name] = total.get(name, 0.0) + hours
        return sorted(total.items(), key=lambda item: (-round(item[1], 6), item[0]))


def previous(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def which_month(command: str, today: date) -> tuple[int, int] | None:
    """「本月統計」「上月統計」「9月統計」「統計 9月」「2026年9月統計」 → (year, month).

    ``None`` when the text is not a statistics command. A month without a year is the latest one that has
    begun: in October, 「11月統計」 is last November.
    """
    compact = re.sub(r"\s+", "", command)
    if compact in THIS_MONTH:
        return today.year, today.month
    if compact in LAST_MONTH:
        return previous(today.year, today.month)
    match = next((m for m in (pattern.fullmatch(compact) for pattern in _NAMED) if m), None)
    if not match:
        return None
    year, month = match.group(1), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    if year:
        return int(year), month
    return (today.year if month <= today.month else today.year - 1), month


def days_of(year: int, month: int, through: date) -> list[date]:
    """The days of a month up to and including ``through`` (none for a month that has not begun)."""
    last = calendar.monthrange(year, month)[1]
    return [day for day in (date(year, month, number) for number in range(1, last + 1)) if day <= through]


def person_day(person: dict, *, place=lambda slot: slot.get("duty") or "", threshold: float = .75
               ) -> tuple[int, dict[str, float], int]:
    """(hours, hours per place, hours with a low-confidence reading) of one person on one roster.

    Every roster row the person appears in is one hour, however many duties share it; the places of a
    shared hour each get an equal part, so the places always add up to the hours.
    """
    by_row: dict[int, list[dict]] = {}
    for slot in person.get("slots", []):
        by_row.setdefault(slot["row"], []).append(slot)
    places: dict[str, float] = {}
    unsure = 0
    for slots in by_row.values():
        names = list(dict.fromkeys(place(slot) for slot in slots))
        for name in names:
            places[name] = places.get(name, 0.0) + 1 / len(names)
        unsure += any(slot.get("confidence", 1.0) < threshold for slot in slots)
    return len(by_row), places, unsure


def hours_text(value: float) -> str:
    """24 → 「24」, 6.5 → 「6.5」: whole hours without a decimal."""
    rounded = round(value, 1)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"


def share(part: float, whole: float) -> int:
    """Percentage of the month's hours, rounded."""
    return round(100 * part / whole) if whole else 0
