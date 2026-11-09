"""Real LINE Messaging API webhook for the V2 roster bot (same logic as ``line-sim``).

    python -m all_staff.cli line-bot          # uvicorn on 0.0.0.0:$LINE_V2_PORT (8081)

Environment (``.env`` is loaded; names are separate from the V1 bot's on purpose, the
two bots are different LINE channels and share one ``.env`` in Docker Compose):
    LINE_V2_CHANNEL_ACCESS_TOKEN / LINE_V2_CHANNEL_SECRET   (required)
    LINE_V2_RECOGNIZER     optional model path (default: newest models/all_staff/line-*/recognizer.pt)
    LINE_V2_REGRID_LIMIT   how many saved rosters (newest first) are read again at start-up when they were
                           made by an older grid (default 20; 0 turns it off). All of them:
                           python -m all_staff.cli reanalyze
    LINE_V2_PUBLIC_BASE_URL optional https URL of this server; when set, replies link to
                           /roster?run=<id> (the full-table viewer) and attach each person's
                           roster photo with their cells outlined in blue
                           (/runs/<id>/people/<code>.jpg). Anyone with the link can see that
                           roster, so leave it unset if that is not wanted (text-only replies).
    LINE_V2_ADMIN_USER_IDS optional LINE user ids of the administrators, comma separated (``U`` and 32 hex
                           characters each; not the LINE ID people add friends with — send the bot
                           「我的ID」 to see yours). Every group photo and 航班人數 message is reported to them, and
                           only administrators can correct the date of a group roster. With pushes on, a group
                           photo whose date cannot be read notifies nobody until one of them has confirmed the
                           date. 「意見 …」 messages go to them too.
    The staff list (staff_names.txt, fixed_staff_codes.json) is read from the data folder, copied there from
    configs/ the first time; 幹部 change it from LINE (「人員管理」). The first 幹部 come from
    configs/officers.json, imported once per database; after that 「新增幹部」「移除幹部」 manage them.
    LINE_V2_PUSH           ``1`` lets the bot push. Off by default: every push request counts once per recipient
                           against the LINE plan's monthly message quota (200 on the free plan) and replies do
                           not, so the bot never pushes. Whatever it would push someone (the administrators'
                           reports, name requests and their answers, a notice to an uploader, a private photo's
                           result past the reply window) waits in that person's 收件匣 and comes, free, in front of
                           the next reply they get; 「收件匣」 (the administrators' menu button 「辨識回報」) shows
                           it. An undated group photo is then published at once, as 日期未辨識, instead of waiting
                           for an administrator, and automatic notifications are off whatever LINE_V2_NOTIFY says.
    LINE_V2_NOTIFY         ``1`` turns automatic notifications on (with LINE_V2_PUSH=1 only): when a group posts a
                           roster, everyone who sent 「通知 開」 is pushed their duties. Off by default, because
                           every such push counts against the LINE plan's monthly message quota (replies do not):
                           users look their roster up with the menu instead, and 「通知 開」 says so.
Rich menus: ``python -m all_staff.cli line-menu`` uploads three (``rich_menu``). At start-up (in the background), when
「新增幹部」「移除幹部」 change the 幹部 and when one of them adds the bot again, each 幹部 and administrator is linked to
their own (``RosterBot.sync_menus``); until the menus are uploaded everybody keeps the default.
Webhook URL to register in the LINE console: https://<host>/line/callback
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .bot import RosterBot, Settings, Store
from .staff_book import live_files, load_officers

try:  # module level on purpose: with `from __future__ import annotations` FastAPI resolves the
    # handler's `request: FastRequest` annotation in this module's globals
    from fastapi import Request as FastRequest
except ImportError:  # fastapi is only needed by create_app
    FastRequest = None

API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
MENU_CACHE = 600   # seconds the rich menu ids (``LineTransport.menu_id``) are trusted before LINE is asked again


def _env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    raise RuntimeError(f"尚未設定 {names[0]}")


USER_ID = re.compile(r"U[0-9a-f]{32}")


def admin_ids(value: str | None) -> tuple[str, ...]:
    """``LINE_V2_ADMIN_USER_IDS`` → LINE user ids; anything else (a LINE ID, a name) is reported and skipped."""
    found = []
    for item in re.split(r"[\s,;，、]+", (value or "").strip()):
        item = item.strip("'\"")
        if USER_ID.fullmatch(item):
            found.append(item)
        elif item:
            print(f"⚠ LINE_V2_ADMIN_USER_IDS 的「{item}」不是 LINE userId（U 開頭加 32 個十六進位字元；不是 LINE ID），"
                  "已略過。私訊機器人「我的ID」可以查到自己的 userId。", flush=True)
    return tuple(dict.fromkeys(found))


def switched_on(value: str | None) -> bool:
    """``LINE_V2_NOTIFY`` and the like: 1 / true / yes / on / 開 (anything else, or nothing, is off)."""
    return (value or "").strip().strip("'\"").lower() in ("1", "true", "yes", "on", "開")


def valid_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode("ascii"), signature)


class LineError(RuntimeError):
    """An error answer from the LINE API; ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(f"LINE API {status}: {message}")
        self.status = status


class LineTransport:
    def __init__(self, token: str):
        self.token = token
        self._menus: dict[str, str] | None = None   # rich menu name → id, as LINE listed them (``menu_id``)
        self._menus_at = 0.0
        self._menus_lock = threading.Lock()
        self._pinned: dict[str, str] = {}   # name → id handed over by ``remember_menus``: kept while LINE lists the id
        self._alive: set[str] = set()       # every id LINE listed last time (two copies of one name included)

    def _call(self, method: str, url: str, payload=None) -> bytes:
        # compact: a Flex card's size counts against LINE's 30 KB per bubble
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() if payload is not None else None
        request = Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", **({"Content-Type": "application/json"} if body else {})})
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            raise LineError(exc.code, exc.read().decode('utf-8', 'replace')) from exc

    def reply(self, reply_token, messages):
        self._call("POST", f"{API}/message/reply", {"replyToken": reply_token, "messages": messages[:5]})

    def push(self, to, messages):
        for start in range(0, len(messages), 5):
            self._call("POST", f"{API}/message/push", {"to": to, "messages": messages[start:start + 5]})

    def loading(self, chat_id, seconds=60):
        """Typing indicator in a 1:1 chat while the roster is being recognised."""
        self._call("POST", f"{API}/chat/loading/start", {"chatId": chat_id, "loadingSeconds": seconds})

    def content(self, message_id):
        return self._call("GET", f"{DATA_API}/message/{message_id}/content")

    def display_name(self, user_id, chat_id):
        paths = []
        if chat_id and chat_id.startswith("C"):
            paths.append(f"{API}/group/{chat_id}/member/{user_id}")
        elif chat_id and chat_id.startswith("R"):
            paths.append(f"{API}/room/{chat_id}/member/{user_id}")
        paths.append(f"{API}/profile/{user_id}")
        for path in paths:
            try:
                return json.loads(self._call("GET", path)).get("displayName")
            except RuntimeError:
                continue
        return None

    # -- rich menus (``bot.MenuTransport``): the 幹部 and administrator menus ``line-menu`` uploads -------------------
    def remember_menus(self, ids: dict[str, str]):
        """Take these ids for these names (``line-menu`` just made them: until it deletes the old copies, LINE lists
        both under the same name)."""
        with self._menus_lock:
            self._pinned.update(ids)
            self._menus, self._menus_at = {**(self._menus or {}), **ids}, time.monotonic()

    def menu_id(self, name: str, refresh: bool = False) -> str | None:
        """The id of the rich menu called ``name``, None when there is none. LINE is asked again when told to, when
        the name is missing, and every ``MENU_CACHE`` seconds; of two with one name (an upload cut short), the one
        listed last, unless ``remember_menus`` named one of them (a refresh must not swap a new copy for an old one)."""
        with self._menus_lock:
            if refresh or self._menus is None or name not in self._menus or \
                    time.monotonic() - self._menus_at > MENU_CACHE:
                listed = json.loads(self._call("GET", f"{API}/richmenu/list")).get("richmenus", [])
                self._alive = {item["richMenuId"] for item in listed}
                self._pinned = {key: menu for key, menu in self._pinned.items() if menu in self._alive}
                self._menus = {**{item.get("name"): item["richMenuId"] for item in listed}, **self._pinned}
                self._menus_at = time.monotonic()
            return self._menus.get(name)

    def link_menu(self, user_id: str, name: str) -> str:
        """Link the rich menu called ``name`` to one user (it wins over the default); the id linked. When LINE refuses
        an id it no longer lists (the menus were uploaded again), the name is looked up afresh and tried once more; a
        refusal of a menu LINE still has (a user who blocked the bot, an id of another channel, LINE busy) is raised."""
        menu = self.menu_id(name)
        if not menu:
            raise LookupError(f"LINE 上沒有名為 {name} 的圖文選單")
        try:
            self._call("POST", f"{API}/user/{user_id}/richmenu/{menu}")
            return menu
        except LineError:
            fresh = self.menu_id(name, refresh=True)
            if not fresh or fresh == menu or menu in self._alive:
                raise
        self._call("POST", f"{API}/user/{user_id}/richmenu/{fresh}")
        return fresh

    def unlink_menu(self, user_id: str):
        """Back to the default menu. A user who has no menu of their own is fine."""
        try:
            self._call("DELETE", f"{API}/user/{user_id}/richmenu")
        except LineError as exc:
            if exc.status != 404:
                raise


def create_app(root: Path, recognizer: Path, *, roster: Path | None, template: Path, folder: Path):
    from fastapi import FastAPI, Response
    from .demo import DemoApp
    secret = _env("LINE_V2_CHANNEL_SECRET")
    token = _env("LINE_V2_CHANNEL_ACCESS_TOKEN")
    public_base = os.getenv("LINE_V2_PUBLIC_BASE_URL", "").rstrip("/") or None
    if public_base and not public_base.startswith("https://"):
        print("⚠ LINE_V2_PUBLIC_BASE_URL 不是 https：LINE 不會顯示圖片訊息（標示圖）", flush=True)
    # The staff list lives next to the database, where 幹部 edit it from LINE (「人員管理」); configs/ is
    # only the list it starts from.
    staff_names, fixed_codes = live_files(root / "configs", folder)
    analyzer = DemoApp(root, recognizer, roster=roster, template=template, runs=folder / "runs",
                       staff_names=staff_names, fixed_codes=fixed_codes)
    push = switched_on(os.getenv("LINE_V2_PUSH"))
    print("推播：開（LINE_V2_PUSH）。辨識回報、姓名申請與結果等會直接私訊，每人每次算 1 則訊息額度" if push else
          "推播：關（預設，不用每月的訊息額度）。要給某人的訊息（辨識回報、姓名申請與結果等）放進他的收件匣，"
          "等他下次傳訊息給機器人時隨回覆一起送出；LINE_V2_PUSH=1 可改回推播", flush=True)
    wanted = switched_on(os.getenv("LINE_V2_NOTIFY"))
    notify = wanted and push
    print("自動通知：開（LINE_V2_NOTIFY）。群組有新班表時私訊已開通知的人，每人每張算 1 則訊息額度" if notify else
          "自動通知：關。LINE_V2_NOTIFY=1 要推播，不推播時不會生效（LINE_V2_PUSH=1 才能開）" if wanted else
          "自動通知：關。群組的新班表不會私訊任何人，使用者自己按選單查；LINE_V2_NOTIFY=1 可開啟", flush=True)
    admins = admin_ids(os.getenv("LINE_V2_ADMIN_USER_IDS"))
    if not admins:
        print("管理員：未設定（LINE_V2_ADMIN_USER_IDS）。日期讀不到的群組班表會直接通知，並請上傳者更正", flush=True)
    elif push:
        print(f"管理員：{len(admins)} 位。群組每張照片的辨識結果都會私訊管理員；日期讀不到的群組班表會先問管理員，"
              "確認後才通知", flush=True)
    else:
        print(f"管理員：{len(admins)} 位。群組每張照片、每則航班人數的辨識回報放進管理員的收件匣（選單「辨識回報」）；"
              "日期讀不到的群組班表直接發布（日期未辨識），管理員在回報上補日期", flush=True)
    settings = Settings(public_base=public_base, staff_names=staff_names, fixed_staff_codes=fixed_codes,
                        admins=admins, notify=notify, push=push)
    store = Store(folder / "bot.sqlite3")
    seeded = store.seed_officers(load_officers(root / "configs" / "officers.json"))   # once per database
    if seeded:
        print(f"已從 configs/officers.json 匯入 {seeded} 位幹部；之後請在 LINE 用「新增幹部」「移除幹部」管理", flush=True)
    print(f"人員名冊：{staff_names}（幹部 {len(store.officers())} 位，可在 LINE 私訊「人員管理」修改）", flush=True)
    bot = RosterBot(analyzer, store, LineTransport(token), settings)
    pool = ThreadPoolExecutor(max_workers=4)

    def menus():   # in the background, so start-up does not wait on LINE: 幹部 and administrators get their menu
        done = bot.sync_menus()
        if any(done.values()):
            print(f"圖文選單：管理員版 {done['admin']} 位、幹部版 {done['officer']} 位"
                  + (f"、{done['default']} 位改回一般選單" if done["default"] else "")
                  + (f"、{done['failed']} 位沒換成" if done["failed"] else ""), flush=True)
    threading.Thread(target=menus, name="menus", daemon=True).start()
    app = FastAPI(title="Roster bot V2")

    try:
        regrid_limit = int(os.getenv("LINE_V2_REGRID_LIMIT", "20"))
    except ValueError:
        regrid_limit = 20
    if regrid_limit > 0:   # rosters saved by an older grid are read again in the background, newest first
        def regrid():
            try:
                report = analyzer.upgrade_runs(limit=regrid_limit)
                print(f"已存班表重讀完成：更新 {len(report['upgraded'])}、已是最新 {len(report['current'])}、"
                      f"失敗 {len(report['failed'])}", flush=True)
            except Exception as exc:
                print(f"重讀已存班表失敗：{type(exc).__name__}: {exc}", flush=True)
        threading.Thread(target=regrid, name="regrid", daemon=True).start()

    def run(event):
        try:
            bot.handle(event)
        except Exception as exc:  # never let one event kill the worker
            print(f"事件處理失敗：{type(exc).__name__}: {exc}", flush=True)

    @app.post("/line/callback")
    async def callback(request: FastRequest):
        body = await request.body()
        if not valid_signature(secret, body, request.headers.get("x-line-signature")):
            return Response(status_code=400, content="Invalid signature")
        for event in json.loads(body).get("events", []):
            pool.submit(run, event)
        return Response(status_code=200, content="OK")

    @app.get("/line/health")
    def health():
        return {"status": True, "public_base": public_base, "model": str(recognizer)}

    if public_base:  # full-table viewer for links in replies
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
        page = Path(__file__).with_name("viewer_page.html")

        @app.get("/roster")
        def roster_page():
            return HTMLResponse(page.read_text(encoding="utf-8"))

        @app.get("/data.js")
        def data_js():
            return PlainTextResponse("window.DEMO_MODE = true;", media_type="text/javascript")

        @app.get("/api/runs/{run_id}")
        def run_result(run_id: str):
            try:
                return JSONResponse(analyzer.result(run_id))
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=404)

        @app.get("/runs/{path:path}")
        def run_file(path: str):
            target = (analyzer.runs / path).resolve()
            if analyzer.runs.resolve() not in target.parents or not target.is_file():
                return Response(status_code=404)
            return FileResponse(target)

    return app
