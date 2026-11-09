"""Passenger-flow forecast (人流預測): how busy the 國際／國內 security lines will be, hour by hour.

Every day a text message in the team's group lists how many flights and passengers
arrive and depart in each hour::

    2026年10月06日
    【國內 – 離站】
    6-7時：2架74人
    …
    【國際 – 出境】
    7-8時：2架368人

Passengers do not reach the security line in the hour their flight leaves: most
international passengers pass it one to two hours earlier; domestic ones are counted in the
hour before their flight. ``checkpoint_load`` therefore spreads each departure hour's passengers over the
hours before it with fixed weights (a show-up profile). ``forecast`` divides that by the
number of first-line staff the day's roster has in that hour and grades each hour
超忙／忙／正常／閒／超閒 against the busiest hours of the same day.

The weights are starting assumptions, not measurements: nobody records how many people
pass the line each hour, and they have not been calibrated against anything yet. The
planned check is to pair each day's message with that day's roster once a few weeks of
both are stored. Because the absolute numbers cannot be verified, callers should show the
grades, not head counts.

Standard library only and independent of the bot, so it can be tested and tuned alone.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

AREAS = ("intl", "dom")
# Share of one departure hour's passengers passing security in that hour, one hour earlier, two hours…
# 國際: 0–3 h before departure, 91 min on average, with the flights spread evenly over their hour.
# 國內: all in the hour before departure, the team's own rule (it replaced a 15 min–2 h spread).
WEIGHTS = {"intl": (0.06, 0.44, 0.43, 0.07), "dom": (0.0, 1.0)}
# The people actually working each line: duty names on the roster, and the columns to fall back on
# when a header was not read (the duty is then "c05"). 二線 and 備勤 are resting, so they do not count.
FIRST_LINE = {"intl": (("國際線",), range(2, 6)), "dom": (("國內線一線",), range(17, 20))}
FIRST_HOUR, LAST_HOUR = 5, 23           # roster rows 05–06 … 23–24
VERY_BUSY, BUSY, NORMAL, QUIET, VERY_QUIET = "超忙", "忙", "正常", "閒", "超閒"
LEVELS = (VERY_QUIET, QUIET, NORMAL, BUSY, VERY_BUSY)     # quietest first
# An hour's share of the day's peak (the mean of its three busiest hours) decides its grade:
# 超忙 and 忙 from their ratio up, 閒 and 超閒 below theirs, 正常 in between.
VERY_BUSY_RATIO, BUSY_RATIO, QUIET_RATIO, VERY_QUIET_RATIO = 0.95, 0.75, 0.40, 0.20
QUIET_BELOW = 60                        # fewer passengers an hour than this is 閒 at most, whatever the ratio

# 2026年10月06日, 2026/10/6, 10月6日, 10/6 and the ROC year (115年10月6日, 115/10/06)
_DATE = re.compile(r"(?<!\d)(?:(\d{3,4})\s*[年/.\-]\s*)?(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?")
# 【國內 – 離站】, [國際-出境], 國內線離站 …; anything after it on the line (a total, say) is ignored
_SECTION = re.compile(r"[【\[(〔「]?\s*(國內|國際)\s*線?\s*[-–—~・:|/]?\s*(到站|抵達|到達|入境|離站|出發|出境)")
# 6-7時：2架74人, 6~7點 2架次 1,274人, 06:00-07:00 2架74人
_ROW = re.compile(r"(\d{1,2})(?::?00)?\s*[時點]?\s*[-–—~到至]\s*(\d{1,2})(?::?00)?\s*[時點]?\s*:?\s*"
                  r"(\d+)\s*架次?\s*[,、]?\s*(\d[\d,]*)\s*人")
_DEPARTING = ("離站", "出發", "出境")


@dataclass
class Flow:
    """One day's message: ``series[(area, "dep" | "arr")][hour] = (flights, passengers)``."""
    day: date | None
    series: dict[tuple[str, str], dict[int, tuple[int, int]]] = field(default_factory=dict)

    def total(self, area: str, direction: str = "dep") -> tuple[int, int]:
        rows = self.series.get((area, direction), {}).values()
        return sum(f for f, _ in rows), sum(p for _, p in rows)


def _day(match: re.Match, near: date) -> date | None:
    year, month, day = match.groups()
    if year and len(year) == 3:     # 民國
        year = str(int(year) + 1911)
    options = []
    for candidate in ([int(year)] if year else [near.year - 1, near.year, near.year + 1]):
        try:
            options.append(date(candidate, int(month), int(day)))
        except ValueError:
            continue
    return min(options, key=lambda d: abs((d - near).days)) if options else None


def parse(text: str, near: date | None = None) -> Flow | None:
    """Read the daily message; ``None`` for anything that is not one (ordinary chat, a single quoted line).

    ``day`` is the date printed above the first section, or ``None`` when there is none. A date
    with its year is preferred to a bare 10/6 on an earlier line; without a year it is the date
    closest to ``near`` (today when omitted).
    """
    if not text or len(text) > 6000 or "架" not in text:
        return None
    near = near or date.today()
    flow, section, rows = Flow(None), None, 0
    dated = False      # whether flow.day came with its year
    for line in unicodedata.normalize("NFKC", text).splitlines():
        line = line.strip()
        if not line:
            continue
        header = _SECTION.match(line)
        if header:
            section = ("intl" if header.group(1) == "國際" else "dom",
                       "dep" if header.group(2) in _DEPARTING else "arr")
            flow.series.setdefault(section, {})
            continue
        if section is None:
            for found in _DATE.finditer(line):
                day = _day(found, near)
                if day and (flow.day is None or (found.group(1) and not dated)):
                    flow.day, dated = day, bool(found.group(1))
            continue
        row = _ROW.match(line)
        if not row:
            continue
        start, end, flights, passengers = (int(v.replace(",", "")) for v in row.groups())
        if 0 <= start <= 23 and end in (start + 1, (start + 1) % 24):
            flow.series[section][start] = (flights, passengers)
            rows += 1
    return flow if rows >= 2 else None


def checkpoint_load(flow: Flow, area: str) -> dict[int, float]:
    """Passengers expected to pass the area's security line in each hour (0–23)."""
    load = dict.fromkeys(range(24), 0.0)
    for hour, (_, passengers) in flow.series.get((area, "dep"), {}).items():
        for back, share in enumerate(WEIGHTS[area]):
            if hour - back >= 0:
                load[hour - back] += passengers * share
    return load


def first_line_staff(result: dict) -> dict[str, dict[int, int]]:
    """Head count working each first line per hour (hour 5 = roster row 4), from a recognised roster.

    An area the roster shows nobody on is left out, so the forecast falls back to passengers alone.
    """
    seen: dict[str, dict[int, set]] = {area: {} for area in AREAS}
    for person in result.get("people", []):
        for slot in person.get("slots", []):
            duty = (slot.get("duty") or "").strip()
            unread = not duty or re.fullmatch(r"c\d+", duty, re.I)
            for area, (names, columns) in FIRST_LINE.items():
                if duty in names or (unread and slot.get("column") in columns):
                    seen[area].setdefault(int(slot["row"]) + 1, set()).add(person["code"])
    return {area: {hour: len(codes) for hour, codes in hours.items()} for area, hours in seen.items() if hours}


@dataclass
class Hour:
    load: float           # passengers expected at the line in this hour
    staff: int | None     # first-line head count; None when the roster is unknown
    level: str | None     # one of LEVELS; None when the area has no departures that day


def forecast(flow: Flow, staff: dict[str, dict[int, int]] | None = None) -> dict[str, dict[int, Hour]]:
    """Grade every roster hour of both lines. ``staff`` comes from ``first_line_staff``.

    The measure is passengers per first-line person, or passengers alone when the area has no
    staffing. It is compared with the day's peak, the mean of the three highest hours so one odd
    hour cannot bend the scale: 超忙 from 95% of it, 忙 from 75%, 正常 from 40%, 閒 from 20% and
    超閒 below that. An hour with fewer than ``QUIET_BELOW`` passengers is 閒 at most. The grades
    compare the hours of one day with each other, so an ordinary day has 超忙 hours too. An hour
    whose head count was not read borrows the day's median so that it stays comparable.
    """
    hours = range(FIRST_HOUR, LAST_HOUR + 1)
    result = {}
    for area in AREAS:
        load = checkpoint_load(flow, area)
        people = (staff or {}).get(area) or None
        counts = sorted(v for v in (people or {}).values() if v > 0)
        typical = counts[len(counts) // 2] if counts else 1
        measure = {h: load[h] / ((people.get(h) or typical) if people else 1) for h in hours}
        top = sorted((v for v in measure.values() if v > 0), reverse=True)[:3]
        peak = sum(top) / len(top) if top else 0.0
        graded = {}
        for h in hours:
            if not peak:
                level = None
            elif measure[h] < VERY_QUIET_RATIO * peak:
                level = VERY_QUIET
            elif load[h] < QUIET_BELOW or measure[h] < QUIET_RATIO * peak:
                level = QUIET
            elif measure[h] < BUSY_RATIO * peak:
                level = NORMAL
            else:
                level = VERY_BUSY if measure[h] >= VERY_BUSY_RATIO * peak else BUSY
            graded[h] = Hour(load[h], people.get(h, 0) if people else None, level)
        result[area] = graded
    return result


def shown_hours(graded: dict[str, dict[int, Hour]]) -> list[int]:
    """The roster hours from the first to the last one with any passenger at either line."""
    active = [h for h in range(FIRST_HOUR, LAST_HOUR + 1)
              if any(graded[area][h].load >= 1 for area in AREAS)]
    return list(range(active[0], active[-1] + 1)) if active else []


def busy_ranges(hours: dict[int, Hour]) -> list[tuple[int, int]]:
    """Consecutive 忙 and 超忙 hours merged: [(11, 12), (14, 16)] reads 11–12、14–16."""
    ranges: list[tuple[int, int]] = []
    for hour in sorted(h for h, item in hours.items() if item.level in (BUSY, VERY_BUSY)):
        if ranges and ranges[-1][1] == hour:
            ranges[-1] = (ranges[-1][0], hour + 1)
        else:
            ranges.append((hour, hour + 1))
    return ranges
