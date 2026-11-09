"""Local LINE simulator for the V2 roster bot.

``python -m all_staff.cli line-sim`` opens a LINE-like chat page (default
http://127.0.0.1:8779) with a few virtual users, a group and 1:1 chats. Every
action on the page becomes a real LINE Messaging API webhook event (``follow``,
``join``, ``memberJoined``, text / image ``message``) that is fed to
``RosterBot``; the bot's ``reply`` / ``push`` calls land back in the chats. The
developer panel shows the raw events and API calls, so what works here works
the same once the webhook is connected to LINE.

The full-table viewer of each roster is served at ``/roster?run=<id>`` (the same
page as ``demo``) and linked from the bot's messages.

The rich menu under the 1:1 chat is drawn by the page. The three menus of ``rich_menu`` count as uploaded: the
bot links 幹部 and administrators (``--admin``) to theirs as it would on LINE, and the page adds their 「管理」 row.

Like the server by default (``LINE_V2_PUSH``), the bot pushes nothing (``Settings.push``): what it would push lands in
that user's 收件匣 and shows up with their next reply. ``--push`` lets it push, as ``LINE_V2_PUSH=1`` does.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from . import rich_menu
from .bot import RosterBot, Settings, Store
from .demo import MAX_UPLOAD, make_handler
from .staff_book import live_files

PAGE = Path(__file__).with_name("line_sim_page.html")
DEFAULT_USERS = [("小明", "U_ming"), ("小華", "U_hua"), ("阿傑", "U_jie")]
MENU_ROLES = {name: role for role, name in rich_menu.NAMES.items() if role}   # the menus a user can be linked to


def menu_rows() -> dict:
    """The 「管理」 row of each role's menu, for the page: its category and buttons (a message, or text to type)."""
    return {role: {"category": rich_menu.MANAGE[0], "buttons": [
        {"label": label, **({"text": value} if kind == "message" else {"fill": value})}
        for label, _, (kind, value) in buttons]} for role, buttons in rich_menu.ROLE_BUTTONS.items()}


class SimWorld:
    """Users, chats and message logs of the simulated LINE, plus the bot's transport."""

    def __init__(self, folder: Path):
        self.folder = folder; folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "state.json"
        self.lock = threading.RLock()
        self.tokens: dict[str, str] = {}          # replyToken -> chat key
        self.uploads: dict[str, bytes] = {}       # message id -> image bytes
        self.pending = 0
        self.load()

    # -- state ---------------------------------------------------------------
    def load(self):
        if self.path.is_file():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            for name in (self.folder / "uploads").glob("*"):
                self.uploads[name.stem] = name.read_bytes()
        else:
            self.state = {"users": [], "chats": [], "messages": [], "log": []}

    def save(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.path)

    def user(self, user_id: str) -> dict:
        return next(u for u in self.state["users"] if u["id"] == user_id)

    def chat(self, key: str) -> dict:
        return next(c for c in self.state["chats"] if c["key"] == key)

    def add_message(self, chat_key: str, sender: str, message: dict) -> dict:
        with self.lock:
            item = {"id": len(self.state["messages"]) + 1, "chat": chat_key, "sender": sender,
                    "ts": time.time(), **message}
            self.state["messages"].append(item); self.save()
            return item

    def log(self, kind: str, payload):
        with self.lock:
            self.state["log"].append({"ts": time.time(), "kind": kind, "payload": payload})
            self.state["log"] = self.state["log"][-200:]

    # -- LINE transport (what the bot sees) ---------------------------------------
    def reply(self, reply_token: str, messages: list[dict]):
        self.log("reply", {"replyToken": reply_token, "messages": messages})
        chat_key = self.tokens.pop(reply_token, None)
        if chat_key is None:
            raise RuntimeError("LINE API 400: Invalid reply token")
        for message in messages:
            self.add_message(chat_key, "bot", message)

    def push(self, to: str, messages: list[dict]):
        self.log("push", {"to": to, "messages": messages})
        with self.lock:
            chat = next((c for c in self.state["chats"] if c.get("line_id") == to), None)
            if chat is None:  # a user id: their 1:1 chat, only if they added the bot
                user = next((u for u in self.state["users"] if u["id"] == to), None)
                if not user or not user.get("friend"):
                    raise RuntimeError("LINE API 400: The user hasn't added the LINE Official Account as a friend")
                chat = self.chat(f"dm:{to}")
        for message in messages:
            self.add_message(chat["key"], "bot", message)

    def loading(self, chat_id: str, seconds: int = 60):
        self.log("loading", {"chatId": chat_id, "loadingSeconds": seconds})

    def content(self, message_id: str) -> bytes:
        return self.uploads[message_id]

    def display_name(self, user_id: str, chat_id: str | None) -> str | None:
        try:
            return self.user(user_id)["name"]
        except StopIteration:
            return None

    # rich menus (bot.MenuTransport): the 幹部 and administrator menus count as uploaded; ``state["menus"]`` keeps
    # which role's menu each user is linked to, as LINE would (nobody there has the default)
    def menu_id(self, name: str, refresh: bool = False) -> str | None:
        return f"richmenu-sim-{name}" if name in MENU_ROLES else None

    def link_menu(self, user_id: str, name: str) -> str:
        menu = self.menu_id(name)
        if menu is None:
            raise LookupError(f"沒有名為 {name} 的圖文選單")
        self.log("menu", {"userId": user_id, "richMenu": name})
        with self.lock:
            self.state.setdefault("menus", {})[user_id] = MENU_ROLES[name]
        return menu

    def unlink_menu(self, user_id: str):
        self.log("menu", {"userId": user_id, "richMenu": None})
        with self.lock:
            self.state.setdefault("menus", {}).pop(user_id, None)

    # -- building webhook events ----------------------------------------------------
    def event(self, kind: str, chat_key: str, user_id: str | None, **extra) -> dict:
        chat = self.chat(chat_key)
        source = ({"type": "group", "groupId": chat["line_id"], "userId": user_id}
                  if chat["type"] == "group" else {"type": "user", "userId": chat["line_id"]})
        if user_id is None:
            source.pop("userId", None)
        token = uuid4().hex
        self.tokens[token] = chat_key
        event = {"type": kind, "mode": "active", "timestamp": int(time.time() * 1000), "source": source,
                 "webhookEventId": uuid4().hex, "replyToken": token, **extra}
        self.log("event", event)
        return event


class Simulator:
    def __init__(self, analyzer, folder: Path, public_base: str, admins=(), notify=False, push=False):
        self.world = SimWorld(folder)
        self.store = Store(folder / "bot.sqlite3")
        # 「人員管理」 edits copies in the simulator's folder, never configs/ itself
        staff_names, fixed_codes = live_files(Path(__file__).resolve().parents[1] / "configs", folder)
        if hasattr(analyzer, "staff_names"):
            analyzer.staff_names, analyzer.fixed_codes = staff_names, fixed_codes
        self.bot = RosterBot(analyzer, self.store, self.world, Settings(
            public_base=public_base, staff_names=staff_names, fixed_staff_codes=fixed_codes,
            admins=tuple(admins), notify=bool(notify) and bool(push), push=bool(push)))
        self.pool = ThreadPoolExecutor(max_workers=4)
        if not self.world.state["users"]:
            self.seed()
        with self.world.lock:   # as a server does at start-up (--admin may differ from the last run), from scratch,
            self.world.state["menus"] = {}   # so no menu outlives the records that put it there
        self.bot.sync_menus(force=True)
        self.world.save()

    def seed(self):
        for name, user_id in DEFAULT_USERS:
            self.add_user(name, user_id, friend=True)
        self.create_group("安檢隊群組", [u for _, u in DEFAULT_USERS])

    def dispatch(self, event: dict):
        def run():
            try:
                self.bot.handle(event)
            except Exception as exc:
                traceback.print_exc()
                self.world.log("error", f"{type(exc).__name__}: {exc}")
            finally:
                with self.world.lock:
                    self.world.pending -= 1
                self.world.save()
        with self.world.lock:
            self.world.pending += 1
        self.pool.submit(run)

    # -- actions from the page ----------------------------------------------------------
    def add_user(self, name: str, user_id: str | None = None, friend=False) -> dict:
        user = {"id": user_id or "U_" + uuid4().hex[:10], "name": name.strip()[:20] or "使用者", "friend": False}
        with self.world.lock:
            self.world.state["users"].append(user)
            self.world.state["chats"].append({"key": f"dm:{user['id']}", "type": "user", "line_id": user["id"],
                                              "name": user["name"], "members": [user["id"]], "active": False})
        if friend:
            self.follow(user["id"])
        self.world.save()
        return user

    def follow(self, user_id: str):
        user = self.world.user(user_id)
        if user.get("friend"):
            return
        user["friend"] = True
        self.world.chat(f"dm:{user_id}")["active"] = True
        self.dispatch(self.world.event("follow", f"dm:{user_id}", user_id))

    def create_group(self, name: str, members: list[str]) -> dict:
        chat = {"key": "grp:" + uuid4().hex[:8], "type": "group", "line_id": "C" + uuid4().hex,
                "name": name.strip()[:30] or "群組", "members": list(dict.fromkeys(members)), "active": True}
        with self.world.lock:
            self.world.state["chats"].append(chat)
        self.dispatch(self.world.event("join", chat["key"], None))
        self.world.save()
        return chat

    def invite(self, chat_key: str, user_id: str):
        chat = self.world.chat(chat_key)
        if user_id in chat["members"]:
            return
        chat["members"].append(user_id)
        self.dispatch(self.world.event("memberJoined", chat_key, None,
                                       joined={"members": [{"type": "user", "userId": user_id}]}))

    def send_text(self, chat_key: str, user_id: str, value: str):
        value = value.strip()[:2000]
        if not value:
            return
        self.world.add_message(chat_key, user_id, {"type": "text", "text": value})
        self.dispatch(self.world.event("message", chat_key, user_id,
                                       message={"type": "text", "id": uuid4().hex[:16], "text": value}))

    def send_postback(self, chat_key: str, user_id: str, data: str, display_text: str | None = None,
                      params: dict | None = None):
        """A tapped postback button (flex / quick reply / rich menu); LINE shows ``displayText`` as sent.
        ``params``: what a date picker adds (``{"date": "2026-10-01"}``)."""
        if display_text:
            self.world.add_message(chat_key, user_id, {"type": "text", "text": display_text})
        postback = {"data": data, **({"params": params} if params else {})}
        self.dispatch(self.world.event("postback", chat_key, user_id, postback=postback))

    def send_image(self, chat_key: str, user_id: str, payload: bytes):
        message_id = uuid4().hex[:16]
        uploads = self.world.folder / "uploads"; uploads.mkdir(exist_ok=True)
        (uploads / f"{message_id}.img").write_bytes(payload)
        self.world.uploads[message_id] = payload
        self.world.add_message(chat_key, user_id, {"type": "image", "url": f"/sim/media/{message_id}"})
        self.dispatch(self.world.event("message", chat_key, user_id,
                                       message={"type": "image", "id": message_id,
                                                "contentProvider": {"type": "line"}}))

    def reset(self):
        with self.world.lock:
            self.world.state = {"users": [], "chats": [], "messages": [], "log": []}
            self.world.tokens.clear()
        with self.store.lock, self.store.db:
            for table in ("subscriptions", "units", "users", "members", "rosters", "menu_links", "mailbox"):
                self.store.db.execute(f"delete from {table}")
        self.seed()


def make_sim_handler(sim: Simulator, analyzer):
    Base = make_handler(analyzer)   # serves /roster data: /api/runs, /runs/…, /data.js
    roster_page = Path(__file__).with_name("viewer_page.html")

    class Handler(Base):
        def body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD:
                raise ValueError("檔案超過 40 MB")
            return self.rfile.read(length)

        def do_GET(self):
            parsed = urlparse(self.path); route = parsed.path
            if route in ("/", "/index.html"):
                return self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
            if route == "/roster":
                return self.send(roster_page.read_bytes(), "text/html; charset=utf-8")
            if route == "/sim/api/state":
                since = int(parse_qs(parsed.query).get("since", ["0"])[0] or 0)
                with sim.world.lock:
                    state = sim.world.state
                    payload = {"users": state["users"], "chats": state["chats"],
                               "messages": [m for m in state["messages"] if m["id"] > since],
                               "log": state["log"][-80:], "pending": sim.world.pending,
                               "bindings": {u["id"]: (sim.store.user(u["id"]) or {}).get("staff_name")
                                            for u in state["users"]},
                               "menus": state.get("menus", {}), "menuRows": menu_rows()}
                return self.send_json(payload)
            if route.startswith("/sim/media/"):
                data = sim.world.uploads.get(route.rsplit("/", 1)[-1])
                if data is None:
                    return self.send_error(HTTPStatus.NOT_FOUND)
                return self.send(data, "image/jpeg")
            return super().do_GET()

        def do_POST(self):
            parsed = urlparse(self.path); route = parsed.path
            if not route.startswith("/sim/api/"):
                return super().do_POST()
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                if route == "/sim/api/image":
                    sim.send_image(query["chat"], query["user"], self.body())
                    return self.send_json({"ok": True})
                data = json.loads(self.body() or b"{}")
                if route == "/sim/api/send":
                    sim.send_text(data["chat"], data["user"], data.get("text", ""))
                elif route == "/sim/api/postback":
                    sim.send_postback(data["chat"], data["user"], data.get("data", ""), data.get("display"),
                                      data.get("params") if isinstance(data.get("params"), dict) else None)
                elif route == "/sim/api/user":
                    return self.send_json(sim.add_user(data.get("name", ""), friend=bool(data.get("friend"))))
                elif route == "/sim/api/follow":
                    sim.follow(data["user"])
                elif route == "/sim/api/group":
                    return self.send_json(sim.create_group(data.get("name", ""), data.get("members", [])))
                elif route == "/sim/api/invite":
                    sim.invite(data["chat"], data["user"])
                elif route == "/sim/api/reset":
                    sim.reset()
                else:
                    return self.send_error(HTTPStatus.NOT_FOUND)
                sim.world.save()
                return self.send_json({"ok": True})
            except (KeyError, StopIteration) as exc:
                return self.send_json({"error": f"找不到：{exc}"}, 400)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)

    return Handler


def serve(analyzer, folder: Path, host="127.0.0.1", port=8779, open_browser=True, admins=(), notify=False,
          push=False):
    public_base = f"http://{host}:{port}"
    sim = Simulator(analyzer, folder, public_base, admins, notify, push)
    server = ThreadingHTTPServer((host, port), make_sim_handler(sim, analyzer))
    print(f"LINE 模擬器：{public_base}（Ctrl+C 結束；資料在 {folder}）", flush=True)
    print("推播：開（--push）" if push else
          "推播：關，和正式環境的預設一樣：要給某人的訊息放進他的收件匣，下次回覆時一起送出；加 --push 可模擬推播", flush=True)
    print("自動通知：開（--notify）" if notify and push else
          "自動通知：關（--notify 要配合 --push）" if notify else
          "自動通知：關，和正式環境的預設一樣；加 --push --notify 可模擬開啟", flush=True)
    if admins:
        print(f"管理員：{'、'.join(admins)}（群組每張照片的辨識結果都會私訊回報；日期讀不到的群組班表會先問管理員，"
              "確認後才通知）" if push else
              f"管理員：{'、'.join(admins)}（群組每張照片的辨識回報放進管理員的收件匣；日期讀不到的群組班表直接發布）",
              flush=True)
    if open_browser:
        webbrowser.open(public_base)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
