"""人員名冊 that 幹部 edit from LINE: staff names and fixed Chinese codes, kept as the files everybody reads.

Every reader already takes these two files (``load_directory`` / ``load_fixed_codes``): the bot, the
name-table reader and the leave-table reader. In production they live next to the bot's database
(``data/line_v2``), copied once from ``configs/`` (``live_copy``): an edit made in LINE survives rebuilding
the image, and ``configs/`` stays the list a new installation starts from.

Edits keep the file as it was (comments, order) and only touch the lines concerned; a name added from LINE
carries a comment saying when and by whom. Each write replaces the whole file at once, so a reader never sees
half of it.

A fixed code set from LINE also records the day it was set (``fixed_staff_codes.since.json``, beside the
codes): rosters from before that day keep reading that code off their own name table, so giving a code to
someone new (or fixing the English codes, which used to change daily) does not rewrite older rosters.
Codes without a day, such as the ones configs/ starts with, apply to every roster.

A 役男 replaced on his Chinese code has left and comes off the names; the day is kept
(``fixed_staff_codes.former.json``) so that those older rosters, which read his code off their own name table,
still find his name to match (``former``).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from .name_table import load_directory, load_fixed_codes
from .schema import CHINESE_CODES, FIXED_CODES

STAFF_FILE, FIXED_FILE = "staff_names.txt", "fixed_staff_codes.json"


def live_copy(source: Path, target: Path) -> Path:
    """``target``, first copied from ``source`` when it does not exist yet (nothing to copy: left missing)."""
    if not target.exists() and source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return target


def live_files(configs: Path, folder: Path) -> tuple[Path, Path]:
    """The staff list and fixed codes a bot keeping its data in ``folder`` reads and edits."""
    return (live_copy(configs / STAFF_FILE, folder / STAFF_FILE),
            live_copy(configs / FIXED_FILE, folder / FIXED_FILE))


def split_names(value: str) -> list[str]:
    """「王小明 陳大華、李四」 → the names, in order, each once."""
    return list(dict.fromkeys(item for item in re.split(r"[\s,，、;；/]+", value or "") if item))


def _replace(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _name_of(line: str) -> str:
    return line.split("#", 1)[0].strip()


class StaffBook:
    """The staff names (one per line, ``#`` starts a comment) and the fixed code → name mappings."""

    def __init__(self, names: Path | None, fixed: Path | None, lock: threading.Lock | None = None):
        self.names_path, self.fixed_path = names, fixed
        self.lock = lock or threading.Lock()   # share one between books on the same files

    @property
    def editable(self) -> bool:
        return bool(self.names_path and self.fixed_path)

    def names(self) -> list[str]:
        return list(dict.fromkeys(load_directory(self.names_path)))

    def fixed(self) -> dict[str, str]:
        return load_fixed_codes(self.fixed_path)

    @property
    def since_path(self) -> Path | None:
        return self.fixed_path.with_name(f"{self.fixed_path.stem}.since.json") if self.fixed_path else None

    def since(self) -> dict[str, str]:
        """Fixed code → the day (ISO) it was set from LINE; codes set before that was recorded have none."""
        path = self.since_path
        if not path or not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return {code: day for code, day in data.items() if code in FIXED_CODES and isinstance(day, str)} \
            if isinstance(data, dict) else {}

    def fixed_on(self, day) -> dict[str, str]:
        """The fixed codes that apply to a roster of ``day`` (a ``date``; ``None``: an undated roster, all of them)."""
        fixed, since = self.fixed(), self.since()
        if day is None:
            return fixed
        return {code: name for code, name in fixed.items() if code not in since or since[code] <= day.isoformat()}

    def set_after(self, day) -> set[str]:
        """The fixed codes set now but only from after ``day`` (``fixed_on`` leaves them out): on a roster of that day
        they were daily codes, read off its name table like a number. None for ``day``: none of them."""
        return set() if day is None else set(self.fixed()) - set(self.fixed_on(day))

    @property
    def former_path(self) -> Path | None:
        return self.fixed_path.with_name(f"{self.fixed_path.stem}.former.json") if self.fixed_path else None

    def _former(self) -> dict[str, str]:
        """役男 replaced on their code → the day (ISO) they were replaced."""
        path = self.former_path
        if not path or not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        return {name: day for name, day in data.items() if isinstance(day, str)} if isinstance(data, dict) else {}

    def former(self, day) -> list[str]:
        """The 役男 who left their code after ``day`` (a ``date``): off the 名冊 now, but still who a roster of
        that day names. None (an undated roster): nobody, it is read with today's 名冊."""
        if day is None:
            return []
        return [name for name, until in self._former().items() if day.isoformat() < until]

    def departed(self, day) -> set[str]:
        """The 役男 who had left by ``day`` (None: by now) and are not back on the 名冊: a roster of that day does
        not name them, whatever a LINE account still registered under their name says."""
        listed = set(self.names()) | set(self.fixed().values())
        return {name for name, until in self._former().items()
                if name not in listed and (day is None or until <= day.isoformat())}

    def _left(self, names, day: str | None):
        """Keep the day these 役男 left their Chinese code (call with the lock held)."""
        if day and names:
            _replace(self.former_path, json.dumps({**self._former(), **{name: day for name in names}},
                                                  ensure_ascii=False, indent=2) + "\n")

    def _write_since(self, since: dict[str, str]):
        _replace(self.since_path, json.dumps({c: since[c] for c in FIXED_CODES if c in since}, indent=2) + "\n")

    def _lines(self) -> list[str]:
        if not self.names_path or not self.names_path.is_file():
            return []
        return self.names_path.read_text(encoding="utf-8").splitlines()

    def _write_names(self, lines: list[str]):
        _replace(self.names_path, "\n".join(lines).rstrip("\n") + "\n")

    def _write_fixed(self, fixed: dict[str, str]):
        ordered = {code: fixed[code] for code in FIXED_CODES if code in fixed}
        _replace(self.fixed_path, json.dumps(ordered, ensure_ascii=False, indent=2) + "\n")

    def _append(self, lines: list[str], names: list[str], note: str):
        lines.extend(f"{name}  # {note}" if note else name for name in names)

    def add(self, names: list[str], note: str = "") -> tuple[list[str], list[str]]:
        """(added, already listed). ``note`` goes in a comment after each new name."""
        names = list(dict.fromkeys(names))
        with self.lock:
            lines, known = self._lines(), set(self.names())
            added = [name for name in names if name not in known]
            if added:
                self._append(lines, added, note)
                self._write_names(lines)
            return added, [name for name in names if name in known]

    def remove(self, names: list[str], day: str | None = None) -> tuple[list[str], list[str], dict[str, str]]:
        """(removed, not listed, the fixed codes those people had → name). Their fixed codes are dropped too; a
        役男 leaving this way on ``day`` (ISO) keeps his older rosters, as one replaced on his code does."""
        names = list(dict.fromkeys(names))
        with self.lock:
            known, fixed = set(self.names()), self.fixed()
            removed = [name for name in names if name in known or name in fixed.values()]
            freed = {code: name for code, name in fixed.items() if name in removed}
            if removed:
                self._write_names([line for line in self._lines() if _name_of(line) not in removed])
            if freed:
                self._write_fixed({code: name for code, name in fixed.items() if code not in freed})
                since = self.since()
                if any(code in since for code in freed):
                    self._write_since({code: set_on for code, set_on in since.items() if code not in freed})
                self._left([name for code, name in freed.items() if code in CHINESE_CODES], day)
            return removed, [name for name in names if name not in removed], freed

    def set_code(self, code: str, name: str, note: str = "", day: str | None = None) -> dict:
        """Make ``name`` the person with fixed code ``code`` from ``day`` (ISO) on.

        Returns what changed: ``before`` (who had the code), ``moved`` (the code ``name`` had until now),
        ``listed`` (whether ``name`` had to be added to the names) and ``removed`` (``before`` again when that
        was a 役男 on a Chinese code: replaced there he has left, so he comes off the names as well, rather
        than staying among the people whose numeric code changes daily).
        """
        with self.lock:
            fixed, since = self.fixed(), self.since()
            before = fixed.get(code)
            moved = next((c for c, n in fixed.items() if n == name and c != code), None)
            if moved:
                del fixed[moved]
                since.pop(moved, None)
            if before != name and day:
                since[code] = day
            fixed[code] = name
            self._write_fixed(fixed)
            self._write_since(since)
            listed = name not in self.names()
            if listed:
                lines = self._lines()
                self._append(lines, [name], note)
                self._write_names(lines)
            removed = None
            if before and before != name and code in CHINESE_CODES:
                removed = before
                self._write_names([line for line in self._lines() if _name_of(line) != before])
                self._left([before], day)   # rosters from before ``day`` still name him on this code (``former``)
            elif before and before != name:   # an English code changed hands: they stay on the 名冊 (it decides
                self._keep_listed(before, note)   # who may use the bot)
            return {"before": before if before != name else None, "moved": moved, "listed": listed,
                    "removed": removed}

    def clear_code(self, code: str, day: str | None = None) -> str | None:
        """Nobody has fixed code ``code`` any more; who had it (they stay on the list of names, written there if
        the code was all that listed them, since the 名冊 decides who may use the bot). For a 役男's code the
        ``day`` (ISO) is kept: should he be taken off the 名冊 next, rosters from before still name him."""
        with self.lock:
            fixed = self.fixed()
            name = fixed.pop(code, None)
            if name:
                self._write_fixed(fixed)
                since = self.since()
                if since.pop(code, None):
                    self._write_since(since)
                self._keep_listed(name, "")
                if code in CHINESE_CODES:
                    self._left([name], day)
            return name

    def _keep_listed(self, name: str, note: str):
        """Write ``name`` into the names file unless it is there (call with the lock held)."""
        if name not in self.names():
            lines = self._lines()
            self._append(lines, [name], note)
            self._write_names(lines)


USER_ID = re.compile(r"U[0-9a-f]{32}")   # a LINE user id (not the LINE ID people add friends with)


def load_officers(path: Path | None) -> list[tuple[str, str | None]]:
    """(LINE user id, title) of the 幹部 a new database starts with (``configs/officers.json``)."""
    if not path or not Path(path).is_file():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    found = []
    for item in data.get("officers", []) if isinstance(data, dict) else []:
        user_id = str(item.get("user_id", "")).strip() if isinstance(item, dict) else ""
        if not USER_ID.fullmatch(user_id):
            raise ValueError(f"幹部名單格式錯誤：「{user_id}」不是 LINE userId（U 開頭加 32 個十六進位字元）")
        found.append((user_id, (item.get("title") or "").strip() or None))
    return found
