"""Check (and optionally repair) the LINE bot's stored rosters: dates, name tables and 「我是」 claims.

    python -m all_staff.cli line-check                  # report only
    python -m all_staff.cli line-check --reread         # re-read every name table from its photo
    python -m all_staff.cli line-check --restore-dates  # undo 「日期」 changes: back to the date read off the photo

Per roster it prints where it came from, the date in use and the date read off the photo
(``!`` when they differ), how many codes got a name, which codes with duties got none (those
nobody has that day apart, with why: ``unnamed_codes``), which name-table columns could not be
verified, and the 「我是」 claims on it (``!`` when the claimed code has no duty there). Fixed
codes are those in force on the roster's day, as the bot applies them.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .bot import TAIPEI, Store, nobody_words, roster_day, unnamed_codes
from .name_table import NAMES_VERSION, load_directory, locate, match_names, read_name_cells, warp_page
from .staff_book import StaffBook


def _photo(run_folder: Path) -> Path | None:
    return next(iter(sorted((run_folder / "upload").glob("*"))), None)


def check_rosters(folder: Path, template_path: Path, staff_names: Path | None, *, reread=False,
                  restore_dates=False, only: str | None = None, out=print) -> list[dict]:
    from backend.phase1_cell_pipeline import GridTemplate
    store = Store(folder / "bot.sqlite3")
    template = GridTemplate.from_json(template_path)
    directory = load_directory(staff_names) + store.staff_names()
    book = StaffBook(staff_names, staff_names.with_name("fixed_staff_codes.json") if staff_names else None)
    users = {u: (store.user(u) or {}).get("staff_name") for u in
             {row[0] for row in store._all("select user_id from users")}}
    report = []
    for roster in store.all_rosters():
        run = roster["run_id"]
        if only and run != only:
            continue
        run_folder = folder / "runs" / run
        path = run_folder / "result.json"
        if not path.is_file():
            out(f"{run}: 找不到 result.json"); continue
        data = json.loads(path.read_text(encoding="utf-8"))
        read = data.get("run", {}).get("roster_date")
        if roster.get("read_date") is None and read:      # rows from before read_date existed
            store.set_read_date(run, read); roster["read_date"] = read
        skipped = []
        photo = _photo(run_folder)
        if photo:
            page = warp_page(photo, template)
            if page is not None:
                skipped = [f"{r['first']}欄" for r in locate(page)[1] if not r["ok"]]
            else:
                skipped = ["姓名表無法定位"]
            if reread or data.get("names_version") != NAMES_VERSION:
                data["names"], data["names_blank"] = read_name_cells(photo, template, directory=directory)
                data["names_version"] = NAMES_VERSION
                temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
                temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                temporary.replace(path)
        else:
            skipped = ["原始照片不存在"]
        fixed = book.fixed_on(roster_day(roster))   # as ``names_for``: a code set later does not rewrite older rosters
        names = match_names(data.get("names") or {}, directory, fixed_codes=fixed)
        unread, nobody = unnamed_codes(data, names, fixed, later=book.set_after(roster_day(roster)))
        claims = store.claims_for_run(run)
        on_duty = {p["code"] for p in data.get("people", [])}
        changed = roster.get("read_date") and roster.get("roster_date") != roster["read_date"]
        if restore_dates and changed:
            store.set_roster_date(run, roster["read_date"])
            out(f"{run}: 日期 {roster['roster_date']} → {roster['read_date']}（照片上的日期）")
            roster["roster_date"], changed = roster["read_date"], False
        when = datetime.fromtimestamp(roster.get("sent_at") or roster["created_at"] or 0, TAIPEI).strftime("%m/%d %H:%M")
        source = "群組" if str(roster["chat_id"])[:1] in "CR" else "私訊"
        claim_text = "、".join(
            f"{users.get(u) or u}={c}{'' if c in on_duty else '!無勤務'}"
            f"{'!固定代號衝突' if users.get(u) in fixed.values() and fixed.get(c) != users.get(u) else ''}"
            f"{'!姓名衝突' if c in names and names[c] != users.get(u) else ''}" for u, c in claims)
        out(f"{run}  {source} {when} 上傳  日期 {roster['roster_date'] or '未知'}"
            f"{'（等管理員確認，尚未通知）' if roster.get('held') else ''}"
            f"{'！照片是 ' + roster['read_date'] if changed else ''}"
            f"  姓名 {len(names)}（固定 {len(fixed)}，原始 OCR {len(data.get('names') or {})}）"
            f"{'  有勤務沒對到：' + '、'.join(unread) if unread else ''}"
            f"{'  沒有這個人：' + nobody_words(nobody) if nobody else ''}"
            f"{'  未核對：' + '、'.join(skipped) if skipped else ''}"
            f"{'  我是：' + claim_text if claim_text else ''}")
        report.append({"run": run, **roster, "names": len(names), "unread": unread, "nobody": nobody,
                       "claims": claims, "skipped": skipped})
    return report
