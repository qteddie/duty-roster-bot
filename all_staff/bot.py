"""LINE roster bot (V2): anyone can add it, register their name and get their duties.

The bot logic is transport-agnostic: it consumes LINE Messaging API webhook
events and talks through a small ``Transport`` interface (reply / push / image
content / display name). ``all_staff.line_webhook`` plugs in the real LINE API;
``all_staff.line_sim`` plugs in a local chat simulator.

Conversation model
------------------
* The bot serves one unit (安檢隊). Any user may add it as a friend; no join code.
* Numeric codes rotate every day; Chinese codes (役男) and English codes are fixed (``fixed_staff_codes.json``).
  Users register their *name* (「設定 蕭志明」).
  Each roster's 「姓名代碼對照表」 is read (``name_table``) and snapped to known staff
  names (``Settings.staff_names``, which 幹部 edit, + registered names) to find today's code.
  When a name is not found, the user can claim a code for that roster: 「我是 72」.
* A registered name is locked, so nobody takes a colleague's name to read their month: another name, or one
  another LINE account already has, is a request that the administrators answer on a card
  (``name_request_card``, 「姓名申請」); the first answer decides. Without administrators, names change at once.
* 名冊守門員: in private chats, only someone whose registered name is on the 名冊 (``Settings.staff_names`` and the
  fixed codes' names), a 幹部 or an administrator sees anything of the team. Anyone else can register a name on
  the 名冊, read the help, find their user id, send 「意見」 and open 我的設定; everything else answers with one card
  that says so (``gate_card``). With no 名冊 at all the gate is open.
* Every group the bot is in is a roster source. Group events are handled silently:
  no greeting on join, no replies, no error messages in the group.
* 不推播 (``Settings.push`` off; the default in production, ``LINE_V2_PUSH``): LINE's free plan has 200 pushed messages
  a month, each push request counting once per recipient, and a reply (with the token of the person's own event) is
  free. So the bot then never pushes: whatever it would have pushed to someone (a report, a name request, its answer, a
  notice to an uploader, a private photo's result past the reply window) is filed in their 收件匣 (``Store.file_mail``)
  and comes in front of the next reply they get (``RosterBot._inbox``), or when they ask (「收件匣」, the administrators'
  menu button 「辨識回報」). Automatic notifications and the daily feedback push are off then, and an undated group
  photo is published at once instead of held (below). With pushes on, everything below that says "pushed" is.
* A roster photo posted in a group becomes the team roster. Telling people about it is off
  by default (``Settings.notify``): every push counts against the LINE plan's monthly quota
  and replies do not, so users look their roster up with the menu. With it on, users with
  notifications on receive their own duties privately. Of a group's text only the 航班人數 message
  with departures is taken. Problems with a group roster (unreadable page, unknown date) go privately
  to the uploader, and every group photo, read or not, is reported to the administrators.
* With administrators (``Settings.admins``: LINE user ids, not LINE IDs) and pushes on, a group photo whose
  date cannot be read is held instead: nobody is notified and only the administrators are
  asked which day it is. The usual notifications go out once one of them has answered, and
  the date of a group roster is then theirs alone to correct. Without pushes nobody could be asked in time: the
  photo is published at once as 日期未辨識, and the report in the administrators' 收件匣 has its date picker.
* 「查看時段」 picks a time slot and 國際／國內 and lists who is on duty there.
* The daily 航班人數 text message is recorded when it is posted in a group (silently, like
  rosters) or pasted privately; 「人流預測」 grades each hour of the 國際／國內 security lines from it.
* 「本月統計」 adds up one person's planned hours and places this month, up to today (``monthly_stats``),
  and draws them: a column per day, a bar per place. Other months cannot be seen. The card names the days
  its total does not cover.
* 「加班」／「月底結算」 lists the same month date by date with the hours past eight a day (加班), and with the
  hourly wage the user typed (「時薪 200」) estimates the overtime pay: overtime hours × wage, nothing more.
* 幹部 (``Store.officers``, first imported from ``configs/officers.json``) and administrators keep the staff
  list from LINE (「人員管理」): names, fixed Chinese codes and the 幹部 themselves (``staff_book``).
* 「意見 …」 is feedback for the administrators: kept with its sender's name, pushed to them at most
  once a day (never without pushes), and otherwise shown with the next reply the bot sends one of them (``_inbox``).
* How each 航班人數 message posted in a group was read (kept, or skipped and why) is pushed to every administrator
  at once, a card each, like the photo reports (``flow_report``; without pushes the cards go to their 收件匣). Only
  when that reached none of them does it wait for the next reply one of them gets, which carries it for free
  (``flow_digest``).
* Private queries use the latest team roster, or the user's own private uploads
  while no group has posted a roster yet. Private uploads never change the team roster.
"""
from __future__ import annotations

import calendar
import json
import re
import sqlite3
import threading
import time
import unicodedata
from hashlib import sha256
from dataclasses import dataclass
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from typing import Any, Protocol

from . import monthly_stats, passenger_flow
from .dataset import GRID_VERSION
from .name_table import COLUMNS as NAME_COLUMNS, NAMES_VERSION, load_directory, load_fixed_codes, match_names
from .leave_table import LEAVE_VERSION
from .lines import DUTY_VERSION
from .rich_menu import NAMES as MENU_NAMES
from .schema import ALLOWED_CODE_SET, ALLOWED_CODES, CHINESE_CODES, ENGLISH_CODES, FIXED_CODES
from .staff_book import USER_ID, StaffBook, split_names

TEXT_LIMIT = 4900
EXAMPLE_NAME = "王小明"   # the name the examples type: not one on the 名冊, which the 名冊守門員 never suggests
MY_ROSTER = ("我的班表", "班表", "我的", "查班表")  # alone: the card of MY_DAY; with a date: that day's duty card
HELP = "📋 用法說明｜下方選單分成班表、查詢、人員、更多四排（幹部和管理員多一排「管理」）；左右滑動卡片查看每個按鈕的用法。"
HELP_SECTIONS = (
    ("班表", "看自己現在、下個小時與明天的勤務", "#173F68", (
        ("我的班表", "一張卡片看完：最上面是下個小時要做什麼，接著是今天每一段勤務（正在做的框起來、做完的變灰），"
                     "最下面是明天的班表，還沒上傳也會說。"),
    ), "想看其他日期，直接輸入日期，例如 9/30。左右滑動，繼續看「查詢」。"),
    ("查詢", "找時間、工作或某位同仁", "#147D87", (
        ("查看時段", "點一格就看那個小時誰在哪裡；國際、國內各一區，顏色越深人越多。"),
        ("查看工作", "選一項工作，看它整天各時段的上班名單。"),
        ("查詢人員", "鍵盤會帶入「查 」，再輸入代號或完整姓名。"),
    ), "今天與明天都有班表時，可在查詢結果下方切換。"),
    ("人員", "核對當天有哪些人、哪些時段忙", "#405879", (
        ("勤務人員", "一張卡片看當天全部代號與姓名，也能開原始班表核對。"),
        ("休假人員", "一張卡片看輪休與請假名單和假別。"),
        ("人流預測", "看各時段國際、國內安檢線預估忙不忙。"),
    ), "人員清單也能切換今天／明天。"),
    ("更多", "統計、設定與意見回饋", "#52606D", (
        ("本月統計", "看這個月的時數、上班天數與各地點排行；月曆上點日期就能看當天的班表，「加班結算」還能算加班費。"),
        ("我的設定", "{my_settings}"),
        ("意見回饋", "鍵盤會帶入「意見 」，寫下問題或建議送出，會連同姓名轉給管理員。"),
    ), "第一次使用請先登記自己的姓名：輸入「設定 你的姓名」，只需一次。"),
)
HELP_NOTIFY = "想收到新班表私訊，輸入「通知 開」；停止則輸入「通知 關」。"   # only while Settings.notify is on
HELP_AGAIN = "想再看這四張說明卡，隨時傳「說明」。"   # the last card's last words: the menu has no button for the help
MORE_HELP = (
    "📋 進階說明\n"
    "\n"
    "【第一次使用】\n"
    "設定 姓名：登記自己的姓名\n"
    "請將「姓名」換成自己的姓名。\n"
    "數字代號每天會換，英文與中文代號固定；請一律用姓名設定。\n"
    "{roster}\n"
    "{rename}\n"
    "\n"
    "【查班表】\n"
    "我的班表：下個小時與現在的勤務、今天的時間軸和明天的班表\n"
    "今天／明天：只看今天或明天的詳細班表與標示圖\n"
    "9/30：直接輸入日期，查看那天的班表\n"
    "查 姓名／查 代號：查看其他人的勤務\n"
    "\n"
    "「9/30」可換成其他日期，也可直接傳日期查自己的班表。\n"
    "查其他人的指定日期：查 姓名 9/30 或查 代號 9/30\n"
    "\n"
    "【查看時段】\n"
    "查看時段：點一格就看那個小時誰在哪裡（國際、國內各一區，顏色越深人越多）\n"
    "查看工作：先選工作，再看整天各時段的上班名單\n"
    "10-11 國際：直接查看指定時段與區域\n"
    "也可加日期，例如：10-11 國內 9/30\n"
    "勤務人員：查看最新班表的代號、姓名清單及原圖\n"
    "勤務人員 9/30：查看指定日期的姓名／代號對照\n"
    "休假人員：查看最新班表的輪休與請假人員\n"
    "休假人員 9/30：查看指定日期的休假名單\n"
    "人流預測：查看各時段國際、國內安檢線預估忙不忙\n"
    "人流預測 10/7：查看指定日期的預估\n"
    "\n"
    "【本月統計】\n"
    "本月統計：這個月的時數、上班天數與各地點時數，加上班表月曆\n"
    "在月曆上點有顏色的日期，就能看那天的原圖班表和自己的勤務，看完會再附上月曆，接著點別天；"
    "從 11 月起，月曆下方也能打開上個月的月曆。\n"
    "加班：這個月每一天的時數、加班時數與加班費試算（月底結算）\n"
    "時薪 200：設定自己的時薪，用來算加班費\n"
    "請將「200」換成自己的時薪。每天超過 8 小時的部分算加班，加班費＝加班時數 × 時薪，只是試算。\n"
    "統計只看得到這個月。只算已上傳而且日期正確的班表，是排定的時數，不是出勤紀錄。\n"
    "\n"
    "【意見回饋】\n"
    "意見 內容：把問題或建議轉給管理員\n"
    "請將「內容」換成想說的話；會附上你登記的姓名。\n"
    "\n"
    "【找不到自己時】\n"
    "我是 代號：補上自己在這張班表的代號\n"
    "姓名沒辨識出來時，請將「代號」換成當天的代號。\n"
    "\n"
    "{settings}"
    "{mailbox}"
    "\n"
    "【更正日期】\n"
    "日期 月/日：更正你最後上傳的班表日期\n"
    "「月/日」請換成班表上印的日期。卡片上寫「這張的日期是後來設定的」時，表示日期和照片上讀到的不一樣。\n"
    "群組照片的日期讀不到時，上傳的人會收到「📅 選這張的日期」，直接選那一張的日期；有管理員時，由管理員在那天的班表卡片點「📅 改這張的日期」。\n"
    "\n"
    "【幹部與管理員】\n"
    "人員管理：幹部增刪人員名冊、固定代號與幹部；管理員的卡片上還有姓名申請和意見列表\n"
    "姓名申請：管理員查看等待確認的改名申請，直接點同意或不同意\n"
    "意見列表：管理員查看最近的意見回饋\n"
    "{reports}"
    "\n"
    "【上傳班表】\n"
    "班表照片可傳到隊上群組，或私訊給我辨識。\n"
    "航班人數訊息貼在群組會自動記下，也可以私訊貼給我。\n"
    "機器人不會在群組回覆訊息。\n"
    "\n"
    "所有指令請在私人聊天室使用，也可點下方選單。"
)
MORE_HELP_SETTINGS = {   # the 【設定】 part of MORE_HELP, by Settings.notify
    True: ("【通知與設定】\n"
           "通知 開：群組有新班表時，私訊你的勤務\n"
           "通知 關：停止自動私訊\n"
           "我的設定：查看姓名與通知狀態，以及改名申請\n"
           "解除：取消姓名登記並關閉通知\n"),
    False: ("【設定】\n"
            "我的設定：查看登記的姓名與改名申請\n"
            "解除：取消姓名登記\n"),
}
# Without pushes (``Settings.push`` off): the 【收件匣】 part of MORE_HELP, and the administrators' menu button
MORE_HELP_MAILBOX = ("\n【收件匣】\n"
                     "收件匣：還沒看到的訊息，例如改名申請的結果、群組照片的提醒（也可以傳「我的訊息」）\n"
                     "機器人不主動推播，給你的訊息會在你下次傳訊息或按選單時，跟著回覆一起送來。\n")
MORE_HELP_REPORTS = "辨識回報：管理員選單上的按鈕，查看群組班表與航班人數訊息的辨識回報（和「收件匣」相同）\n"
SHORT_DUTY = {
    "國際線（二）": "國際二線", "國際線備勤": "國際備勤", "國際線帶勤": "國際帶勤",
    "國內線一線": "國內一線", "國內線二線": "國內二線", "國內線備勤": "國內備勤",
    "國內線帶勤": "國內帶勤", "國內工作管制門": "國內管制門", "國內貨物安檢": "國內貨安",
}


class Transport(Protocol):
    """What the bot talks through. Some methods are optional and looked up with ``getattr``, so a transport without
    them (a test fake) still works: ``loading`` (the typing indicator) and the rich menus of ``MenuTransport``."""
    def reply(self, reply_token: str, messages: list[dict]) -> None: ...
    def push(self, to: str, messages: list[dict]) -> None: ...
    def content(self, message_id: str) -> bytes: ...
    def display_name(self, user_id: str, chat_id: str | None) -> str | None: ...


class MenuTransport(Transport, Protocol):
    """A transport that can give one user a rich menu of their own (``RosterBot.sync_menu``): a per-user link wins over
    the default menu, and deleting a menu on LINE drops its links."""
    def menu_id(self, name: str, refresh: bool = False) -> str | None: ...   # the current id of ``rich_menu.NAMES[role]``; None: not uploaded
    def link_menu(self, user_id: str, name: str) -> str: ...   # the menu called ``name`` for one user; the id linked; raises on failure
    def unlink_menu(self, user_id: str) -> None: ...   # back to the default; a user with nothing linked is not an error


def text(value: str) -> dict:
    return {"type": "text", "text": value[:TEXT_LIMIT]}


def fill_action(label: str, prefix: str) -> dict:
    """A quick-reply button that opens the keyboard with ``prefix`` typed in (the bot ignores its postback)."""
    return {"type": "action", "action": {"type": "postback", "label": label, "data": f"fill:{prefix.strip()}",
                                         "inputOption": "openKeyboard", "fillInText": prefix}}


# What the help says about changing one's name: locked while an administrator can approve a change (姓名申請),
# free otherwise
MY_SETTINGS_HELP = {True: "看登記的姓名；姓名登記後不能自己改，要改按回覆下方的「申請改姓名」，管理員同意後才會改。",
                    False: "看登記的姓名；回覆下方的按鈕可設定或更改姓名。"}
RENAME_HELP = {True: "登記後不能自己改姓名，再傳「設定 新姓名」會送出申請，管理員同意後才會改。",
               False: "要改姓名，再傳一次「設定 新姓名」即可。"}
ROSTER_HELP = "姓名要和隊上名冊上的一模一樣；新進同仁請先請幹部加入名冊（人員管理 → 新增人員），再登記。"   # 名冊守門員


def help_card(advanced: bool = False, *, notify: bool = False, locked: bool = True, gated: bool = False,
              mailbox: bool = False) -> dict:
    """Four menu-matched cards; advanced text commands remain on a separate card.

    ``notify`` (``Settings.notify``) says whether 「通知 開」 exists: it is only explained while it does.
    ``locked``: whether a registered name needs an approval to change (an administrator exists to approve it).
    ``gated``: whether only names on the 名冊 can register (the 名冊守門員 is on).
    ``mailbox``: whether the bot files what it would push (``Settings.push`` off): 「收件匣」 and the administrators'
    「辨識回報」 button are then explained.
    """
    if not advanced:
        bubbles = []
        for index, (section, intro, color, commands, note) in enumerate(HELP_SECTIONS, 1):
            body = []
            for command, explanation in commands:
                if body:
                    body.append({"type": "separator", "margin": "md", "color": "#E2E8F0"})
                body.append({"type": "box", "layout": "vertical", "spacing": "xs",
                             "margin": "md", "contents": [
                    {"type": "text", "text": command, "weight": "bold", "decoration": "underline",
                     "size": "md", "color": color},
                    {"type": "text", "text": explanation.replace("{my_settings}", MY_SETTINGS_HELP[locked]),
                     "size": "sm", "wrap": True, "color": "#1B2430"}]})
            if gated and index == len(HELP_SECTIONS):
                note += "姓名要和隊上名冊上的一樣。"
            if notify and index == len(HELP_SECTIONS):
                note += HELP_NOTIFY
            if index == len(HELP_SECTIONS):
                note += HELP_AGAIN
            body.append({"type": "text", "text": note, "size": "xs", "wrap": True,
                         "margin": "lg", "color": "#666666"})
            bubble = {"type": "bubble", "size": "mega", "header": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": color,
                "contents": [
                    {"type": "text", "text": f"{index:02d} / 04  {section}", "weight": "bold",
                     "size": "lg", "color": "#FFFFFF"},
                    {"type": "text", "text": intro, "size": "sm", "wrap": True,
                     "margin": "sm", "color": "#FFFFFF"}]},
                "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                         "contents": body}}
            if index == len(HELP_SECTIONS):
                bubble["footer"] = {"type": "box", "layout": "vertical", "paddingAll": "12px",
                                    "contents": [{"type": "button", "style": "link", "height": "sm",
                                                  "action": {"type": "message", "label": "進階說明",
                                                             "text": "進階說明"}}]}
            bubbles.append(bubble)
        return {"type": "flex", "altText": HELP, "contents": {"type": "carousel", "contents": bubbles}}
    source = (MORE_HELP.replace("{settings}", MORE_HELP_SETTINGS[bool(notify)]).replace("{rename}", RENAME_HELP[locked])
              .replace("{roster}\n", f"{ROSTER_HELP}\n" if gated else "")
              .replace("{mailbox}", MORE_HELP_MAILBOX if mailbox else "")
              .replace("{reports}", MORE_HELP_REPORTS if mailbox else ""))
    lines = source.splitlines()
    contents = [{"type": "text", "text": lines[0], "weight": "bold", "size": "lg", "wrap": True,
                 "color": "#244F87"}]
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("【") and line.endswith("】"):
            contents.append({"type": "text", "text": line[1:-1], "weight": "bold", "decoration": "underline",
                             "size": "md", "wrap": True, "margin": "lg", "color": "#244F87"})
        elif "：" in line:
            command, explanation = line.split("：", 1)
            contents.append({"type": "box", "layout": "vertical", "margin": "md", "spacing": "xs", "contents": [
                {"type": "text", "text": command, "weight": "bold", "decoration": "underline", "size": "sm",
                 "wrap": True, "color": "#244F87"},
                {"type": "text", "text": explanation, "size": "sm", "wrap": True, "color": "#1B2430"}]})
        else:
            contents.append({"type": "text", "text": line, "size": "xs", "wrap": True,
                             "margin": "sm", "color": "#666666"})
    bubble = {"type": "bubble", "size": "mega", "body": {
        "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": contents}}
    return {"type": "flex", "altText": source[:400], "contents": bubble}


def normalise(value: str) -> str:
    """Full-width → half-width, trimmed, upper-case Latin letters."""
    value = unicodedata.normalize("NFKC", value or "").strip()
    return re.sub(r"\s+", " ", value)


def parse_code(value: str) -> str | None:
    value = normalise(value).upper().lstrip("#")
    if value.isdigit():
        value = str(int(value))
    return value if value in ALLOWED_CODE_SET else None


# ---------------------------------------------------------------- storage
class NameTaken(Exception):
    """Another LINE account has this staff name: a name belongs to one account (``Store.take_name`` moves it)."""


LOOK_AGAIN = object()   # ``Store.approve_name_request``: someone registered the name since the card was built


class Store:
    """SQLite: bindings, rosters, daily 航班人數 messages and group membership.

    The units/subscriptions tables are legacy and unused.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript("""
            create table if not exists users(user_id text primary key, code text, name text,
                notify integer default 0, updated_at real);
            create table if not exists members(chat_id text, user_id text, last_seen real,
                primary key(chat_id, user_id));
            create table if not exists rosters(id integer primary key autoincrement, chat_id text,
                run_id text, uploaded_by text, created_at real, codes integer, roster_date text);
            create table if not exists units(id integer primary key autoincrement,
                name text not null, admin_user_id text not null unique,
                join_code text not null unique, connect_code text not null unique,
                chat_id text unique);
            create table if not exists subscriptions(user_id text primary key, unit_id integer not null);
            create table if not exists flows(id integer primary key autoincrement, chat_id text,
                flow_date text, raw text, uploaded_by text, sent_at real, created_at real);
            """)
            self.db.execute("""create table if not exists claims(run_id text, user_id text, code text,
                               primary key(run_id, user_id))""")
            self.db.execute("""create table if not exists viewed_rosters(user_id text primary key,
                               run_id text not null, viewed_at real not null)""")
            # 「意見 …」: ``delivered_at`` is when an administrator was first shown it (a push, or with a
            # reply to them); ``pushed_at`` is set when that was a push, which happens at most once a day
            self.db.execute("""create table if not exists feedback(id integer primary key autoincrement,
                               user_id text not null, staff_name text, display_name text, body text not null,
                               context text, created_at real not null, delivered_at real, pushed_at real)""")
            # 人流資料: one row per group message read as 航班人數 (``RosterBot._capture_flow``): kept (``status`` "ok",
            # ``detail`` what ``add_flow`` answered) or not ("skipped", ``detail`` why). It is pushed to the
            # administrators at once; ``delivered_at`` is set when that reached one of them, or else when the next
            # reply the bot sends one of them showed it.
            self.db.execute("""create table if not exists flow_notices(id integer primary key autoincrement,
                               chat_id text, user_id text, poster text, sent_at real, status text not null,
                               flow_date text, detail text, summary text, created_at real not null,
                               delivered_at real)""")
            # 幹部: may edit the staff list from LINE (「人員管理」). ``meta`` remembers that the first ones were
            # imported (``seed_officers``), so an officer removed later is not brought back at the next start.
            self.db.execute("""create table if not exists officers(user_id text primary key, title text,
                               added_by text, added_at real not null)""")
            self.db.execute("create table if not exists meta(key text primary key, value text)")
            # 姓名申請: a registered name is locked; changing it, or taking a name another account has, waits here
            # for an administrator. ``status``: pending, approved, refused, or replaced / cancelled by
            # the requester (a newer request, 「解除」, or typing the name they already have).
            self.db.execute("""create table if not exists name_requests(id integer primary key autoincrement,
                               user_id text not null, current_name text, requested_name text not null,
                               display_name text, status text not null default 'pending', created_at real not null,
                               decided_by text, decided_at real, released_name text, pushed_at real)""")
            # 圖文選單: the menu the bot linked to a 幹部 or an administrator (``RosterBot.sync_menu``; ``role`` is
            # rich_menu.ROLES, ``menu_id`` LINE's id of it), so it is not linked again while nothing changed and is
            # unlinked once they are neither. Nobody here has the default menu.
            self.db.execute("""create table if not exists menu_links(user_id text primary key, role text not null,
                               menu_id text not null, linked_at real not null)""")
            # 收件匣: with pushes off (``Settings.push``), what the bot would have pushed to one person, a group of
            # messages at a time (``messages``: LINE message objects, JSON), kept until a reply to them that went out
            # carried it (``delivered_at``; ``RosterBot._inbox``) or they asked for it (「收件匣」)
            self.db.execute("""create table if not exists mailbox(id integer primary key autoincrement,
                               user_id text not null, messages text not null, created_at real not null,
                               delivered_at real, attempts integer not null default 0)""")
            if "attempts" not in {row[1] for row in self.db.execute("pragma table_info(mailbox)")}:
                self.db.execute("alter table mailbox add column attempts integer not null default 0")
            self.db.execute("create index if not exists mailbox_waiting on mailbox(user_id, delivered_at)")
            columns = {row[1] for row in self.db.execute("pragma table_info(rosters)")}
            if "roster_date" not in columns:  # databases created before date reading
                self.db.execute("alter table rosters add column roster_date text")
            if "read_date" not in columns:    # the date read off the photo, kept when 「日期」 changes it
                self.db.execute("alter table rosters add column read_date text")
            if "sent_at" not in columns:      # when LINE says the photo was sent: the newest photo of a day is chosen by this
                self.db.execute("alter table rosters add column sent_at real")
            if "held" not in columns:         # 1 while a group photo waits for an administrator to confirm its date
                self.db.execute("alter table rosters add column held integer default 0")
            columns = {row[1] for row in self.db.execute("pragma table_info(users)")}
            if "staff_name" not in columns:   # databases from when users bound a (rotating) code
                self.db.execute("alter table users add column staff_name text")
            if "hourly_wage" not in columns:  # 「時薪 200」: what 加班結算 multiplies the overtime hours by
                self.db.execute("alter table users add column hourly_wage real")
            if "released_name" not in columns:   # the name 「解除」 cleared: the name lock outlives it
                self.db.execute("alter table users add column released_name text")
            if "name_at" not in columns:      # when LINE was last asked for the account's display name (``name``)
                self.db.execute("alter table users add column name_at real")
            # The first start with 「one name, one LINE account」: a 姓名申請 card pushed before this moment still says
            # a yes leaves the other account alone (``one_name_since``). Kept once, whatever else the database has
            self.started = time.time()
            self.db.execute("insert or ignore into meta(key, value) values('one_name_since', ?)", (str(self.started),))
            try:   # one staff name, one LINE account
                self.db.execute("create unique index if not exists users_staff_name on users(staff_name) "
                                "where staff_name is not null")
            except sqlite3.IntegrityError:
                # Accounts that shared a name before the rule keep it until a 幹部 or an administrator releases
                # them; ``set_staff_name`` lets nobody else in meanwhile, and the index is made at a later start.
                print("⚠ 有姓名同時登記在好幾個 LINE 帳號上：請幹部或管理員私訊「綁定清單」查看，"
                      "用「解除登記 姓名」留下一個", flush=True)

    def _one(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def _run(self, sql, *args):
        with self.lock, self.db:
            self.db.execute(sql, args)

    USER_KEYS = ("user_id", "code", "name", "notify", "staff_name")

    def user(self, user_id: str) -> dict | None:
        row = self._one(f"select {', '.join(self.USER_KEYS)} from users where user_id=?", user_id)
        return dict(zip(self.USER_KEYS, row)) if row else None

    def set_staff_name(self, user_id: str, staff_name: str | None, display: str | None = None):
        """Register (or clear, with None) the user's real name; also drops any legacy code binding.

        Clearing it (「解除」) keeps it as ``released_name``, so that registering another name afterwards is still
        a change of name (``locked_name``), not a first registration that skips the approval.

        A name belongs to one LINE account: ``NameTaken`` when another one has it, and nothing changes
        (``take_name`` moves a name from one account to another).
        """
        with self.lock, self.db:
            if staff_name is not None and self.db.execute(
                    "select 1 from users where staff_name=? and user_id<>?", (staff_name, user_id)).fetchone():
                raise NameTaken(staff_name)
            self._set_name(user_id, staff_name, display)

    def _set_name(self, user_id: str, staff_name: str | None, display: str | None):
        """``set_staff_name`` without the check, inside the caller's transaction."""
        previous = self.db.execute("select staff_name from users where user_id=?", (user_id,)).fetchone()
        if previous and previous[0] != staff_name:
            self.db.execute("delete from claims where user_id=?", (user_id,))
        if staff_name is None:   # a request they still have waiting was filed under the name they no longer have
            self.db.execute("update name_requests set released_name=coalesce(current_name, released_name), "
                            "current_name=null where user_id=? and status='pending'", (user_id,))
        now = time.time()
        self.db.execute("""insert into users(user_id, staff_name, name, code, updated_at, notify, name_at)
                          values(?,?,?,null,?,0,?)
                          on conflict(user_id) do update set staff_name=excluded.staff_name, code=null,
                          name=coalesce(excluded.name, users.name), updated_at=excluded.updated_at,
                          name_at=coalesce(excluded.name_at, users.name_at),
                          released_name=case when excluded.staff_name is null
                                             then coalesce(users.staff_name, users.released_name) end""",
                        (user_id, staff_name, display, now, now if display else None))

    def take_name(self, user_id: str, staff_name: str, display: str | None = None) -> list[dict]:
        """Give ``staff_name`` to ``user_id`` and release every other account that had it (their notifications
        go off too); those accounts, as they were. One transaction: the name is never without exactly one owner
        in between."""
        with self.lock, self.db:
            return self._take(user_id, staff_name, display)

    def _take(self, user_id: str, staff_name: str, display: str | None) -> list[dict]:
        """``take_name`` inside the caller's transaction."""
        rows = self.db.execute(f"select {', '.join(self.USER_KEYS)} from users where staff_name=? and user_id<>? "
                               "order by updated_at, user_id", (staff_name, user_id)).fetchall()
        for row in rows:
            self._set_name(row[0], None, None)
            self.db.execute("update users set notify=0 where user_id=?", (row[0],))
        self._set_name(user_id, staff_name, display)
        return [dict(zip(self.USER_KEYS, row)) for row in rows]

    def one_name_since(self) -> float:
        """When this database first ran with 「one name, one LINE account」 (unreadable: since this start, so a card
        from before it is still looked at twice and a fresh one never is)."""
        row = self._one("select value from meta where key='one_name_since'")
        try:
            since = float(row[0])
        except (TypeError, ValueError, IndexError):
            return self.started
        return since if 0 < since <= time.time() else self.started

    def approve_name_request(self, number: int, by: str, shown: float):
        """✅ on a request, as one step: ``None`` when it is no longer pending; ``LOOK_AGAIN`` (nothing changes)
        when another account registered the name after ``shown``, the moment the tapped card was built, so the
        approver has not seen whom a yes would release; otherwise the request is approved, the name moves and
        the accounts it was taken from are returned (as ``take_name``)."""
        with self.lock, self.db:
            row = self.db.execute("select user_id, requested_name, display_name from name_requests "
                                  "where id=? and status='pending'", (number,)).fetchone()
            if not row:
                return None
            user_id, name, display = row
            if self.db.execute("select 1 from users where staff_name=? and user_id<>? and updated_at>?",
                               (name, user_id, shown)).fetchone():
                return LOOK_AGAIN
            self.db.execute("update name_requests set status='approved', decided_by=?, decided_at=? where id=?",
                            (by, time.time(), number))
            known = self.db.execute("select name from users where user_id=?", (user_id,)).fetchone()
            # the LINE name filed with the request only fills a blank: one read from LINE since is newer
            return self._take(user_id, name, None if known and known[0] else display)

    def release(self, user_id: str) -> bool:
        """Take the staff name off ``user_id`` (a 幹部 did, or 「解除」): as ``set_staff_name`` with None, and
        notifications off. ``False`` when the account had none."""
        with self.lock, self.db:
            row = self.db.execute("select staff_name from users where user_id=?", (user_id,)).fetchone()
            if not row or not row[0]:
                return False
            self._set_name(user_id, None, None)
            self.db.execute("update users set notify=0 where user_id=?", (user_id,))
            return True

    BINDING_KEYS = ("user_id", "staff_name", "name", "updated_at")

    def bindings(self) -> list[dict]:
        """Every LINE account with a staff name (``name``: its LINE display name), the earliest first."""
        rows = self._all(f"select {', '.join(self.BINDING_KEYS)} from users where staff_name is not null "
                         "order by updated_at, user_id")
        return [dict(zip(self.BINDING_KEYS, row)) for row in rows]

    def display_due(self, user_id: str, max_age: float) -> bool:
        """Is it time to ask LINE again what this account, which has a staff name, is called?"""
        row = self._one("select name_at from users where user_id=? and staff_name is not null", user_id)
        return bool(row) and (row[0] is None or row[0] < time.time() - max_age)

    def set_display(self, user_id: str, display: str | None):
        """The LINE display name just read (None: LINE gave nothing, what is known stays), and when."""
        self._run("update users set name=coalesce(?, name), name_at=? where user_id=?", display, time.time(), user_id)

    def locked_name(self, user_id: str) -> str | None:
        """The name this LINE account has, or had until 「解除」: another one needs an approval."""
        row = self._one("select coalesce(staff_name, released_name) from users where user_id=?", user_id)
        return row[0] if row else None

    def staff_names(self) -> list[str]:
        return [r[0] for r in self._all("select distinct staff_name from users where staff_name is not null")]

    def claim(self, run_id: str, user_id: str, code: str):
        """「我是 72」: this user is code 72 on that roster (when their name was not recognised)."""
        with self.lock, self.db:
            self.db.execute("delete from claims where run_id=? and code=? and user_id<>?", (run_id, code, user_id))
            self.db.execute("insert into claims values(?,?,?) on conflict(run_id, user_id) do update "
                            "set code=excluded.code", (run_id, user_id, code))

    def claims(self, run_id: str) -> dict[str, str]:
        """Code → registered name claimed on this roster."""
        rows = self._all("select c.code, u.staff_name from claims c join users u on u.user_id=c.user_id "
                         "where c.run_id=? and u.staff_name is not null", run_id)
        return {code: name for code, name in rows}

    def bind(self, user_id: str, code: str | None, name: str | None):
        self._run("""insert into users(user_id, code, name, updated_at, notify) values(?,?,?,?,0)
                     on conflict(user_id) do update set code=excluded.code,
                     name=coalesce(excluded.name, users.name), updated_at=excluded.updated_at""",
                  user_id, code, name, time.time())

    def set_notify(self, user_id: str, on: bool):
        self._run("""insert into users(user_id, notify, updated_at) values(?,?,?)
                     on conflict(user_id) do update set notify=excluded.notify""", user_id, int(on), time.time())

    def set_hourly_wage(self, user_id: str, wage: float | None):
        """「時薪 200」 (None forgets it). Kept apart from the name, so changing the name keeps the wage."""
        self._run("""insert into users(user_id, hourly_wage, updated_at, notify) values(?,?,?,0)
                     on conflict(user_id) do update set hourly_wage=excluded.hourly_wage""", user_id, wage, time.time())

    def hourly_wage(self, user_id: str) -> float | None:
        row = self._one("select hourly_wage from users where user_id=?", user_id)
        return row[0] if row else None

    def seen(self, chat_id: str, user_id: str):
        self._run("""insert into members values(?,?,?) on conflict(chat_id, user_id)
                     do update set last_seen=excluded.last_seen""", chat_id, user_id, time.time())

    def members(self, chat_id: str) -> list[dict]:
        rows = self._all("""select u.user_id, u.code, u.name, u.notify from members m
                            join users u on u.user_id=m.user_id where m.chat_id=? and u.code is not null""",
                         chat_id)
        return [dict(zip(("user_id", "code", "name", "notify"), r)) for r in rows]

    def subscribers(self) -> list[dict]:
        """Users who registered their name and turned notifications on."""
        rows = self._all(f"select {', '.join(self.USER_KEYS)} from users where staff_name is not null and notify=1")
        return [dict(zip(self.USER_KEYS, r)) for r in rows]

    def legacy_subscribers(self) -> list[dict]:
        """Notification users still bound to a code from before names (codes rotate daily)."""
        rows = self._all(f"select {', '.join(self.USER_KEYS)} from users "
                         "where staff_name is null and code is not null and notify=1")
        return [dict(zip(self.USER_KEYS, r)) for r in rows]

    def drop_code(self, user_id: str):
        self._run("update users set code=null where user_id=?", user_id)

    def add_roster(self, chat_id: str, run_id: str, uploaded_by: str | None, codes: int,
                   roster_date: str | None = None, sent_at: float | None = None, *, held: bool = False):
        """``roster_date`` is what was read off the photo; it is also kept as ``read_date``.

        ``sent_at`` is when the photo was sent (LINE's event time, in seconds). Which photo is the newest of a
        day follows it, not the order in which recognition happened to finish. ``held`` marks a group photo
        whose notifications wait for an administrator to say which day it is (``confirm_date`` / ``unhold``).
        """
        now = time.time()
        self._run("insert into rosters(chat_id, run_id, uploaded_by, created_at, codes, roster_date, read_date, "
                  "sent_at, held) values(?,?,?,?,?,?,?,?,?)",
                  chat_id, run_id, uploaded_by, now, codes, roster_date, roster_date,
                  now if sent_at is None else sent_at, int(held))

    def has_uploaded(self, user_id: str) -> bool:
        """Has this user ever sent a roster that was recognised? (Used to tell a bad shot from a stray photo.)"""
        return self._one("select 1 from rosters where uploaded_by=? limit 1", user_id) is not None

    def set_read_date(self, run_id: str, read_date: str | None):
        self._run("update rosters set read_date=? where run_id=?", read_date, run_id)

    def set_roster_date(self, run_id: str, roster_date: str | None):
        self._run("update rosters set roster_date=? where run_id=?", roster_date, run_id)

    ROSTER_KEYS = ("chat_id", "run_id", "uploaded_by", "created_at", "codes", "roster_date", "read_date",
                   "sent_at", "id", "held")
    _COLUMNS = ", ".join(ROSTER_KEYS)
    # what one user may look up: rosters posted in any group (C…/R… ids) and their own private uploads
    _VISIBLE = "(substr(chat_id, 1, 1) in ('C', 'R') or chat_id=?)"
    _GROUPS = "substr(chat_id, 1, 1) in ('C', 'R')"
    # "Newer" means sent later (LINE's message time); rows from before that column fall back to the save time.
    _SENT = "coalesce(sent_at, created_at)"
    _LATEST = f"order by {_SENT} desc, id desc"
    # newest *roster day* first (the printed date, or the Taiwan send day when it is unknown), then newest photo
    _NEWEST = (f"order by coalesce(roster_date, date({_SENT}, 'unixepoch', '+8 hours')) desc, "
               f"{_SENT} desc, id desc")

    def _roster(self, row) -> dict | None:
        return dict(zip(self.ROSTER_KEYS, row)) if row else None

    def latest_roster(self, chat_id: str) -> dict | None:
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where chat_id=? "
                                      f"{self._LATEST} limit 1", chat_id))

    def roster_by_run(self, run_id: str) -> dict | None:
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where run_id=? "
                                      "order by id desc limit 1", run_id))

    def remember_roster(self, user_id: str, run_id: str):
        """Keep a short-lived reading context for a plain 「查 代號／姓名」 query."""
        self._run("""insert into viewed_rosters(user_id, run_id, viewed_at) values(?,?,?)
                     on conflict(user_id) do update set run_id=excluded.run_id,
                     viewed_at=excluded.viewed_at""", user_id, run_id, time.time())

    def recently_viewed_roster(self, user_id: str, *, max_age: float = 12 * 3600) -> dict | None:
        row = self._one("select run_id from viewed_rosters where user_id=? and viewed_at>=?",
                        user_id, time.time() - max_age)
        roster = self.roster_by_run(row[0]) if row else None
        if roster and (str(roster["chat_id"]).startswith(("C", "R")) or roster["chat_id"] == user_id):
            return roster
        return None

    def team_roster(self) -> dict | None:
        """The latest roster posted in any group (group IDs start with C, room IDs with R)."""
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where {self._GROUPS} "
                                      f"{self._LATEST} limit 1"))

    def roster_for_user(self, user_id: str, *, dated: bool = False) -> dict | None:
        """The roster a user sees by default: the newest roster day among group posts and their own uploads.

        An old roster re-posted later, or a group roster older than a photo the user sent
        privately, does not hide the newer day. ``dated``: not a photo still waiting for an administrator to
        confirm its date.
        """
        settled = "and coalesce(held, 0)=0 " if dated else ""
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where {self._VISIBLE} {settled}"
                                      f"{self._NEWEST} limit 1", user_id))

    def own_upload(self, user_id: str) -> dict | None:
        """The photo this user sent last, to a group or privately."""
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where {self._VISIBLE} "
                                      f"and uploaded_by=? {self._LATEST} limit 1", user_id, user_id))

    def newest_visible(self, user_id: str) -> dict | None:
        """The photo sent last among those of every group and this user's private ones."""
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where {self._VISIBLE} "
                                      f"{self._LATEST} limit 1", user_id))

    def last_upload_for(self, user_id: str) -> dict | None:
        """Correct the user's last upload; users without uploads can correct the latest visible roster."""
        return self.own_upload(user_id) or self.newest_visible(user_id)

    def held_rosters(self) -> list[dict]:
        """Group photos still waiting for an administrator to say which day they are, newest first."""
        return [self._roster(row) for row in self._all(
            f"select {self._COLUMNS} from rosters where held=1 and {self._GROUPS} {self._LATEST}")]

    def confirm_date(self, run_id: str, roster_date: str) -> bool:
        """Date a held photo and stop holding it. Only one caller gets ``True``: that one notifies everybody."""
        with self.lock, self.db:
            return self.db.execute("update rosters set roster_date=?, held=0 where run_id=? and held=1",
                                   (roster_date, run_id)).rowcount > 0

    def unhold(self, run_id: str) -> bool:
        """Stop holding a photo without dating it (dismissed, or there was nobody to ask)."""
        with self.lock, self.db:
            return self.db.execute("update rosters set held=0 where run_id=? and held=1", (run_id,)).rowcount > 0

    def roster_on(self, user_id: str, day: date) -> dict | None:
        """The newest photo whose roster date is ``day``: a photo of the same day sent later replaces it."""
        return self._roster(self._one(f"select {self._COLUMNS} from rosters where {self._VISIBLE} "
                                      f"and roster_date=? {self._LATEST} limit 1", user_id, day.isoformat()))

    def same_day(self, roster: dict, *, later: bool, user_id: str | None = None) -> dict | None:
        """The nearest other photo of the same printed day sent after (``later``) or before ``roster``.

        Group photos are compared with other group photos; pass ``user_id`` to include that user's private
        uploads. ``None`` when the day is unknown or no such photo exists. ``later=True`` means this photo has
        already been replaced by a newer one; ``later=False`` returns the version it replaces.
        """
        if not roster.get("roster_date"):
            return None
        sent = roster.get("sent_at") if roster.get("sent_at") is not None else roster.get("created_at")
        scope, scope_args = (self._VISIBLE, [user_id]) if user_id else (self._GROUPS, [])
        compare, order = ((">", "asc") if later else ("<", "desc"))
        return self._roster(self._one(
            f"select {self._COLUMNS} from rosters where {scope} and roster_date=? and run_id<>? "
            f"and ({self._SENT} {compare} ? or ({self._SENT} = ? and id {compare} ?)) "
            f"order by {self._SENT} {order}, id {order} limit 1",
            *scope_args, roster["roster_date"], roster["run_id"], sent, sent, roster["id"]))

    def roster_dates(self, user_id: str, limit: int = 5) -> list[date]:
        """Most recent roster dates the user can look up, newest date first."""
        rows = self._all(f"select distinct roster_date from rosters where {self._VISIBLE} "
                         "and roster_date is not null order by roster_date desc limit ?", user_id, limit)
        return [date.fromisoformat(r[0]) for r in rows]

    def all_rosters(self) -> list[dict]:
        return [self._roster(r) for r in self._all(f"select {self._COLUMNS} from rosters order by id")]

    def claims_for_run(self, run_id: str) -> list[tuple[str, str]]:
        return self._all("select user_id, code from claims where run_id=?", run_id)

    FLOW_KEYS = ("chat_id", "flow_date", "raw", "uploaded_by", "sent_at", "id")

    def add_flow(self, chat_id: str, flow_date: str, raw: str, uploaded_by: str | None,
                 sent_at: float | None = None) -> str:
        """Keep the daily 航班人數 message for ``flow_date`` (the raw text, so it can be read again later).

        The same text from the same chat is kept once; posting it again only makes it the newest. Returns how it
        compares with the messages of that date its readers already had (every group's; for a private paste, the
        groups' and the sender's own): "new" (the first), "replaced" (a different one was the newest until now),
        "again" (the newest already had this text), "older" (one sent later stays the newest) or "repeat" (this
        text from this chat was kept already as sent this late: a redelivered webhook, nothing changes).
        """
        now = time.time()
        sent = now if sent_at is None else sent_at
        scope, scope_args = ((self._GROUPS, ()) if str(chat_id).startswith(("C", "R"))
                             else (self._VISIBLE, (chat_id,)))
        with self.lock, self.db:
            known = self.db.execute("select id, sent_at from flows where chat_id=? and flow_date=? and raw=? limit 1",
                                    (chat_id, flow_date, raw)).fetchone()
            if known and (known[1] or 0) >= sent:
                return "repeat"
            newest = self.db.execute(f"select raw, sent_at from flows where {scope} and flow_date=? "
                                     "order by sent_at desc, id desc limit 1", (*scope_args, flow_date)).fetchone()
            if known:
                self.db.execute("update flows set sent_at=? where id=?", (sent, known[0]))
            else:
                self.db.execute("insert into flows(chat_id, flow_date, raw, uploaded_by, sent_at, created_at) "
                                "values(?,?,?,?,?,?)", (chat_id, flow_date, raw, uploaded_by, sent, now))
        if not newest:
            return "new"
        if (newest[1] or 0) > sent:
            return "older"
        return "again" if newest[0] == raw else "replaced"

    def flow_on(self, user_id: str, day: date) -> dict | None:
        """The newest message for ``day`` among group posts and this user's own private pastes."""
        row = self._one(f"select {', '.join(self.FLOW_KEYS)} from flows where {self._VISIBLE} and flow_date=? "
                        "order by sent_at desc, id desc limit 1", user_id, day.isoformat())
        return dict(zip(self.FLOW_KEYS, row)) if row else None

    def flow_dates(self, user_id: str, limit: int = 5) -> list[date]:
        """Most recent days with a 航班人數 message this user can look up, newest first."""
        rows = self._all(f"select distinct flow_date from flows where {self._VISIBLE} "
                         "order by flow_date desc limit ?", user_id, limit)
        return [date.fromisoformat(r[0]) for r in rows]

    FLOW_NOTICE_KEYS = ("id", "chat_id", "user_id", "poster", "sent_at", "status", "flow_date", "detail", "summary",
                        "created_at", "delivered_at")

    def add_flow_notice(self, chat_id: str, user_id: str | None, poster: str | None, sent_at: float, status: str,
                        flow_date: str | None, detail: str, summary: str | None, *, claimed: bool = False) -> int | None:
        """Queue one 人流資料 notice for the administrators; its number, or None when that very message has one
        already (a redelivered webhook keeps the time it was sent). ``claimed``: saved as delivered, for a push about
        to carry it (``unmark_flow_notices`` puts it back in the queue when the push reached nobody), so a reply sent
        meanwhile does not carry it as well."""
        with self.lock, self.db:
            if self.db.execute("select 1 from flow_notices where chat_id=? and coalesce(user_id, '')=? and sent_at=? "
                               "and status=? and coalesce(summary, '')=? limit 1",
                               (chat_id, user_id or "", sent_at, status, summary or "")).fetchone():
                return None
            return self.db.execute(
                "insert into flow_notices(chat_id, user_id, poster, sent_at, status, flow_date, detail, summary, "
                "created_at, delivered_at) values(?,?,?,?,?,?,?,?,?,?)",
                (chat_id, user_id, poster, sent_at, status, flow_date, detail, summary, time.time(),
                 time.time() if claimed else None)).lastrowid

    def undelivered_flow_notices(self) -> list[dict]:
        """The 人流資料 notices no administrator has been shown yet, oldest first."""
        rows = self._all(f"select {', '.join(self.FLOW_NOTICE_KEYS)} from flow_notices where delivered_at is null "
                         "order by id")
        return [dict(zip(self.FLOW_NOTICE_KEYS, row)) for row in rows]

    def unmark_flow_notices(self, ids):
        """Back in the queue: the push that claimed these reached nobody (``add_flow_notice``)."""
        with self.lock, self.db:
            for number in ids:
                self.db.execute("update flow_notices set delivered_at=null where id=?", (number,))

    def mark_flow_notices(self, ids):
        """These have been shown to an administrator, pushed or with a reply."""
        now = time.time()
        with self.lock, self.db:
            for number in ids:
                self.db.execute("update flow_notices set delivered_at=coalesce(delivered_at, ?) where id=?",
                                (now, number))

    FEEDBACK_KEYS = ("id", "user_id", "staff_name", "display_name", "body", "context", "created_at",
                     "delivered_at", "pushed_at")

    def add_feedback(self, user_id: str, staff_name: str | None, display_name: str | None, body: str,
                     context: str | None = None) -> int:
        """Keep one 「意見 …」 message with who sent it; returns its number."""
        with self.lock, self.db:
            return self.db.execute(
                "insert into feedback(user_id, staff_name, display_name, body, context, created_at) "
                "values(?,?,?,?,?,?)", (user_id, staff_name, display_name, body, context, time.time())).lastrowid

    def _feedback(self, where: str, *args) -> list[dict]:
        rows = self._all(f"select {', '.join(self.FEEDBACK_KEYS)} from feedback {where}", *args)
        return [dict(zip(self.FEEDBACK_KEYS, row)) for row in rows]

    def feedback_count(self, user_id: str, since: float) -> int:
        """How many messages this user has sent since ``since`` (so that one person cannot flood the list)."""
        return self._one("select count(*) from feedback where user_id=? and created_at>=?", user_id, since)[0]

    def undelivered_feedback(self) -> list[dict]:
        """What no administrator has been shown yet, oldest first."""
        return self._feedback("where delivered_at is null order by id")

    def recent_feedback(self, limit: int = 10) -> list[dict]:
        """The newest messages, newest first."""
        return self._feedback("order by id desc limit ?", limit)

    def mark_feedback(self, ids, *, pushed: bool = False):
        """These have been shown to an administrator (``pushed``: by a push, not with a reply)."""
        now = time.time()
        with self.lock, self.db:
            for number in ids:
                self.db.execute("update feedback set delivered_at=coalesce(delivered_at, ?), "
                                "pushed_at=coalesce(pushed_at, ?) where id=?", (now, now if pushed else None, number))

    def last_feedback_push(self) -> float | None:
        """When feedback was last pushed to the administrators."""
        return self._one("select max(pushed_at) from feedback")[0]

    OFFICER_KEYS = ("user_id", "title", "added_by", "added_at", "staff_name", "name")

    def officers(self) -> list[dict]:
        """The 幹部, the earliest first, with the name each registered (``staff_name``) and their LINE name."""
        rows = self._all("select o.user_id, o.title, o.added_by, o.added_at, u.staff_name, u.name from officers o "
                         "left join users u on u.user_id=o.user_id order by o.added_at, o.rowid")
        return [dict(zip(self.OFFICER_KEYS, row)) for row in rows]

    def is_officer(self, user_id: str | None) -> bool:
        return bool(user_id) and self._one("select 1 from officers where user_id=?", user_id) is not None

    def add_officer(self, user_id: str, title: str | None = None, added_by: str | None = None) -> bool:
        """``False`` when they already were one (a new title still replaces the old one)."""
        with self.lock, self.db:
            if self.db.execute("select 1 from officers where user_id=?", (user_id,)).fetchone():
                if title:
                    self.db.execute("update officers set title=? where user_id=?", (title, user_id))
                return False
            self.db.execute("insert into officers(user_id, title, added_by, added_at) values(?,?,?,?)",
                            (user_id, title, added_by, time.time()))
            return True

    def remove_officer(self, user_id: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("delete from officers where user_id=?", (user_id,)).rowcount > 0

    def seed_officers(self, officers) -> int:
        """Import the first 幹部 ((user id, title) pairs) once per database; how many were added this time.

        Nothing to import leaves the database unmarked, so a list that turns up later is still imported.
        """
        if not officers:
            return 0
        with self.lock, self.db:
            if self.db.execute("select 1 from meta where key='officers_seeded'").fetchone():
                return 0
            added = 0
            for user_id, title in officers:
                added += self.db.execute("insert or ignore into officers(user_id, title, added_by, added_at) "
                                         "values(?,?,?,?)", (user_id, title, "seed", time.time())).rowcount
            self.db.execute("insert into meta(key, value) values('officers_seeded', ?)", (str(time.time()),))
            return added

    def users_named(self, staff_name: str) -> list[dict]:
        """Everyone who registered this name (usually one person)."""
        rows = self._all(f"select {', '.join(self.USER_KEYS)} from users where staff_name=? "
                         "order by updated_at, user_id", staff_name)
        return [dict(zip(self.USER_KEYS, row)) for row in rows]

    def registered(self) -> dict[str, int]:
        """Registered name → how many LINE accounts registered it."""
        return {name: count for name, count in self._all(
            "select staff_name, count(*) from users where staff_name is not null group by staff_name")}

    NAME_REQUEST_KEYS = ("id", "user_id", "current_name", "requested_name", "display_name", "status", "created_at",
                         "decided_by", "decided_at", "released_name", "pushed_at")

    def _name_requests(self, where: str, *args) -> list[dict]:
        rows = self._all(f"select {', '.join(self.NAME_REQUEST_KEYS)} from name_requests {where}", *args)
        return [dict(zip(self.NAME_REQUEST_KEYS, row)) for row in rows]

    def add_name_request(self, user_id: str, current_name: str | None, requested_name: str,
                         display_name: str | None, released_name: str | None = None) -> tuple[int, dict | None]:
        """File a request for a name; (its number, the pending one it replaces). One pending request a person.

        ``current_name``: the name the account has now; ``released_name``: the one it gave up with 「解除」, shown to
        the approvers for context."""
        now = time.time()
        with self.lock, self.db:
            row = self.db.execute(f"select {', '.join(self.NAME_REQUEST_KEYS)} from name_requests "
                                  "where user_id=? and status='pending' order by id desc limit 1", (user_id,)).fetchone()
            self.db.execute("update name_requests set status='replaced', decided_by=?, decided_at=? "
                            "where user_id=? and status='pending'", (user_id, now, user_id))
            number = self.db.execute("insert into name_requests(user_id, current_name, requested_name, display_name, "
                                     "created_at, released_name) values(?,?,?,?,?,?)",
                                     (user_id, current_name, requested_name, display_name, now, released_name)).lastrowid
        return number, dict(zip(self.NAME_REQUEST_KEYS, row)) if row else None

    def recently_pushed(self, user_id: str, since: float) -> bool:
        """Did a request of this person reach an approver after ``since`` and still wait for an answer (pending, or
        replaced / withdrawn by the person since)? Once an approver has answered, a new request is news again."""
        return self._one("select 1 from name_requests where user_id=? and pushed_at>? "
                         "and status not in ('approved', 'refused') limit 1", user_id, since) is not None

    def mark_name_request_pushed(self, number: int):
        with self.lock, self.db:
            self.db.execute("update name_requests set pushed_at=? where id=?", (time.time(), number))

    def registered_since(self, staff_name: str, since: float, besides: str) -> list[str]:
        """LINE accounts other than ``besides`` that registered ``staff_name`` after ``since``."""
        return [row[0] for row in self._all("select user_id from users where staff_name=? and user_id<>? "
                                            "and updated_at>?", staff_name, besides, since)]

    def name_request(self, number: int) -> dict | None:
        found = self._name_requests("where id=?", number)
        return found[0] if found else None

    def pending_name_request(self, user_id: str) -> dict | None:
        found = self._name_requests("where user_id=? and status='pending' order by id desc limit 1", user_id)
        return found[0] if found else None

    def pending_name_requests(self) -> list[dict]:
        """Every request still waiting for an answer, the oldest first."""
        return self._name_requests("where status='pending' order by id")

    def decide_name_request(self, number: int, status: str, by: str) -> bool:
        """Answer a pending request (approved / refused / cancelled). Only one caller gets ``True``: the first
        approver to tap decides, and a later tap, or another approver's, finds it answered."""
        with self.lock, self.db:
            return self.db.execute("update name_requests set status=?, decided_by=?, decided_at=? "
                                   "where id=? and status='pending'", (status, by, time.time(), number)).rowcount > 0

    MENU_LINK_KEYS = ("role", "menu_id", "linked_at")

    def menu_link(self, user_id: str) -> dict | None:
        """The rich menu the bot linked to ``user_id`` (role, menu_id, linked_at); None: they have the default."""
        row = self._one(f"select {', '.join(self.MENU_LINK_KEYS)} from menu_links where user_id=?", user_id)
        return dict(zip(self.MENU_LINK_KEYS, row)) if row else None

    def set_menu_link(self, user_id: str, role: str, menu_id: str):
        self._run("insert or replace into menu_links(user_id, role, menu_id, linked_at) values(?,?,?,?)",
                  user_id, role, menu_id, time.time())

    def drop_menu_link(self, user_id: str):
        self._run("delete from menu_links where user_id=?", user_id)

    def menu_linked_users(self) -> list[str]:
        """Everyone the bot linked a menu to, the earliest first."""
        return [row[0] for row in self._all("select user_id from menu_links order by linked_at")]

    MAIL_KEPT = 7 * 86400   # seconds a delivered group stays in the table

    def file_mail(self, user_id: str, messages: list[dict], keep: int) -> tuple[int, int]:
        """File one group of messages for ``user_id``; (its number, how many of their oldest waiting groups were
        dropped to keep ``keep`` of them). Groups delivered more than ``MAIL_KEPT`` ago are cleared on the way."""
        now = time.time()
        with self.lock, self.db:
            number = self.db.execute("insert into mailbox(user_id, messages, created_at) values(?,?,?)",
                                     (user_id, json.dumps(messages, ensure_ascii=False), now)).lastrowid
            dropped = self.db.execute(
                "delete from mailbox where user_id=? and delivered_at is null and id not in (select id from mailbox "
                "where user_id=? and delivered_at is null order by id desc limit ?)", (user_id, user_id, keep)).rowcount
            self.db.execute("delete from mailbox where delivered_at<?", (now - self.MAIL_KEPT,))
        return number, dropped

    def undelivered_mail(self, user_id: str) -> list[dict]:
        """The groups waiting for ``user_id``, oldest first: id, messages (the list filed), created_at."""
        rows = self._all("select id, messages, created_at from mailbox where user_id=? and delivered_at is null "
                         "order by id", user_id)
        return [{"id": number, "messages": json.loads(messages), "created_at": created}
                for number, messages, created in rows]

    MAIL_TRIES = 2   # replies LINE may refuse with a group in them before the group is set aside

    def claim_mail(self, ids) -> list[int]:
        """Take these waiting groups for one reply, before it goes out: those of them no other reply took meanwhile
        (two events of one person handled at once must not both carry them). ``release_mail`` gives them back."""
        now, taken = time.time(), []
        with self.lock, self.db:
            for number in ids:
                if self.db.execute("update mailbox set delivered_at=? where id=? and delivered_at is null",
                                   (now, number)).rowcount:
                    taken.append(number)
        return taken

    def release_mail(self, ids, *, refused: bool = False) -> list[int]:
        """Put groups a reply did not show back in the queue. ``refused``: LINE turned that reply down for what it held
        (not for its token): each counts a try, and a group refused ``MAIL_TRIES`` times stays set aside, so one bad
        message cannot hold up every reply to the person. The groups set aside."""
        dropped = []
        with self.lock, self.db:
            for number in ids:
                if refused:
                    self.db.execute("update mailbox set attempts=attempts+1 where id=?", (number,))
                    row = self.db.execute("select attempts from mailbox where id=?", (number,)).fetchone()
                    if row and row[0] >= self.MAIL_TRIES:
                        dropped.append(number)
                        continue
                self.db.execute("update mailbox set delivered_at=null where id=?", (number,))
        return dropped

    def mark_mail(self, ids):
        """These groups went out with a reply: they are not shown again."""
        now = time.time()
        with self.lock, self.db:
            for number in ids:
                self.db.execute("update mailbox set delivered_at=coalesce(delivered_at, ?) where id=?", (now, number))


# ---------------------------------------------------------------- formatting
WEEKDAYS = "一二三四五六日"
TAIPEI = timezone(timedelta(hours=8))   # the server runs in UTC; rosters are Taiwan dates


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def today() -> date:
    return taipei_now().date()


def roster_day(roster: dict) -> date | None:
    """The roster date in use (read from the header, or set later with 「日期」 or a date picker), if known."""
    value = roster.get("roster_date")
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def day_label(day: date) -> str:
    return f"{day.month}/{day.day}（{WEEKDAYS[day.weekday()]}）"


def parse_day(value: str, near: date) -> date | None:
    """「10/1」「10月1日」「2026/10/1」 → a date; without a year, the one closest to ``near``."""
    match = re.fullmatch(r"(?:(\d{4})[/\-.年])?(\d{1,2})[/\-.月](\d{1,2})日?", value.strip())
    if not match:
        return None
    year, month, day = match.groups()
    years = [int(year)] if year else [near.year - 1, near.year, near.year + 1]
    options = []
    for candidate in years:
        try:
            options.append(date(candidate, int(month), int(day)))
        except ValueError:
            continue
    return min(options, key=lambda d: abs((d - near).days)) if options else None


def iso_day(value) -> date | None:
    """「2026-10-05」 → that date; None for anything else. For what comes back from a card: a postback's data, or the
    day LINE's date picker puts in its params."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


RELATIVE_DAYS = {"前天": -2, "昨天": -1, "昨日": -1, "今天": 0, "今日": 0, "明天": 1, "明日": 1, "後天": 2}
DATE_TOKEN = r"(?:\d{4}[/\-.年])?\d{1,2}[/\-.月]\d{1,2}日?|" + "|".join(RELATIVE_DAYS)


def split_day(value: str, near: date) -> tuple[str, date | None]:
    """「我的班表 9/30」「明天班表」「9/30 查 72」 → (the rest of the command, the date)."""
    match = re.fullmatch(rf"(.*?)\s*({DATE_TOKEN})\s*(?:的)?\s*(.*)", value)
    if not match:
        return value, None
    before, token, after = match.groups()
    day = (near + timedelta(days=RELATIVE_DAYS[token])) if token in RELATIVE_DAYS else parse_day(token, near)
    return (f"{before} {after}".strip(), day) if day else (value, None)


def _hours(label: str) -> tuple[str, str]:
    start, _, end = label.partition("-")
    return start, end


def format_duties(code: str, name: str | None, roster: dict, result: dict, *,
                  threshold: float = .75, heading: str | None = None) -> str:
    """One person's duties, consecutive hours on the same duty merged."""
    person = next((p for p in result.get("people", []) if p["code"] == code), None)
    day = roster_day(roster)
    title = f"{heading or (f'{name}（{code}）' if name else f'代號 {code}')}　{day_label(day) if day else '（日期未辨識）'}"
    if person is None:
        note = absence_note(code, result) or f"這張班表上沒有辨識到代號 {code}（休假，或照片中看不清楚）。"
        return f"{title}\n\n{note}"
    blocks, hours = duty_blocks(person, threshold=threshold)
    flagged = sum(b["low"] for b in blocks)
    lines = [title, f"共 {hours} 小時" + (f"｜{flagged} 段待確認" if flagged else "")]
    lines += [f"{b['start']}–{b['end']}　{b['duties']}{' ？' if b['low'] else ''}" for b in blocks]
    if flagged:
        lines.append("？＝辨識信心較低，請對照原圖")
    return "\n".join(lines)


def absence_note(code, result) -> str | None:
    leave = result.get('leave_table', {})
    if not leave.get('available'):
        return None
    if code in leave.get('rest', []):
        return '班表右下角記載：輪休。'
    entry = next((e for e in leave.get('leave', []) if e.get('code') == code), None)
    if entry:
        return f"班表右下角記載：{entry.get('kind') or '請假（假別未辨識）'}。"
    return None


def duty_blocks(person: dict, *, threshold: float = .75) -> tuple[list[dict], int]:
    """The shared calculation for plain-text summaries and personal Flex cards."""
    by_row: dict[int, list[dict]] = {}
    for slot in person["slots"]:
        by_row.setdefault(slot["row"], []).append(slot)
    blocks: list[dict] = []
    for row in sorted(by_row):
        slots = by_row[row]
        duties = "／".join(dict.fromkeys(SHORT_DUTY.get(s["duty"], s["duty"]) for s in slots))
        low = any(s["confidence"] < threshold for s in slots)
        if blocks and blocks[-1]["duties"] == duties and blocks[-1]["end_row"] == row - 1:
            blocks[-1].update(end_row=row, end=_hours(slots[0]["time"])[1], low=blocks[-1]["low"] or low)
        else:
            start, end = _hours(slots[0]["time"])
            blocks.append({"duties": duties, "start": start, "end": end, "end_row": row, "low": low})
    return blocks, len(by_row)


def duty_hours(person: dict, *, threshold: float = .75) -> list[dict]:
    """Hourly assignments for navigation; consecutive identical jobs stay separate."""
    by_row: dict[int, list[dict]] = {}
    for slot in person["slots"]:
        by_row.setdefault(slot["row"], []).append(slot)
    return [{"start": _hours(slots[0]["time"])[0], "end": _hours(slots[0]["time"])[1],
             "duties": "／".join(dict.fromkeys(SHORT_DUTY.get(s["duty"], s["duty"]) for s in slots)),
             "low": any(s["confidence"] < threshold for s in slots)}
            for _, slots in sorted(by_row.items())]


def duty_signature(result: dict, code: str | None) -> list[tuple[str, str, str]] | None:
    """What a code does on a roster as (start, end, duties) blocks, for comparing two versions of one day.

    ``None`` means the code is not on that roster at all (leave, or not read).
    """
    person = next((p for p in result.get("people", []) if p["code"] == code), None) if code else None
    if person is None:
        return None
    return [(b["start"], b["end"], b["duties"]) for b in duty_blocks(person)[0]]


def duty_change(before: list | None, after: list | None) -> str:
    """One phrase on how a person's day differs from the version an update replaces; empty when on neither."""
    if before is None and after is None:
        return ""
    if before is None:
        return "上一版沒有你的勤務"
    if after is None:
        return "上一版有你的勤務，這一版沒有"
    return "你的勤務與上一版相同" if before == after else "你的勤務與上一版不同"


def roster_summary(result: dict, roster: dict | None = None) -> str:
    sheet = result["sheets"][0]
    codes = sum(len(c["pred"]) for c in sheet["cells"])
    people = len(result.get("people", []))
    review = sum(p["conf"] < .75 for c in sheet["cells"] for p in c["pred"])
    day = roster_day(roster or {})
    lines = [f"✅ {day_label(day) + ' ' if day else ''}班表辨識完成：{people} 人、{codes} 個勤務格"
             + (f"（{review} 格信心較低）" if review else "")]
    if roster is not None and day is None:
        lines.append("⚠ 讀不到班表上的日期，請輸入「日期 月/日」（換成班表上印的日期）")
    for warning in sheet.get("warnings") or []:
        lines.append(f"⚠ {warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------- who is on duty in one hour
AREAS = {"intl": ("國際", range(2, 17)), "dom": ("國內", range(17, 31))}   # c02–c16 | c17–c30; A1 and right are out
FIRST_ROW, LAST_ROW = 4, 22                                                # 05-06 … 23-24
AREA_EMOJI = {"intl": "✈️", "dom": "🏠"}
SLOT_COLUMNS = 5   # hour chips per row on 「查看時段」: 05–06 … 09–10, 10–11 … 14–15, …
SLOT_UI = {   # one hue per area, lighter → darker as more people are on duty; words stay dark on every shade
    "intl": ("#EAF2FD", "#CFE0F8", "#AAC8F0", "#82ACE6"),
    "dom": ("#E4F5F2", "#C2E9E2", "#97D7CC", "#66C1B3"),
    "now": "#C2410C",   # the ring and 「現在」: warm, so it stands apart from both hues
    "past": "#475569",  # the words of an hour already over, on its shade faded to "fade"
    "fade": "B3",
}
NAME_PATTERN = re.compile(r"[一-鿿]{2,5}")
SLOT_PATTERN = re.compile(r"(?:(?:查看時段|同時段)\s*)?(\d{1,2})(?:\s*[-–~～到至]\s*(\d{1,2}))?\s*(?:點|時)?\s*(國際|國內)線?(?:\s+(.+))?")


def source_label(roster: dict, user_id: str | None = None) -> str:
    """「群組 9/30 21:05 上傳的 10/1（四）班表」: which photo a reply is about."""
    sent = roster.get("sent_at") if roster.get("sent_at") is not None else roster.get("created_at")
    when = datetime.fromtimestamp(sent or 0, TAIPEI)
    where = "你私訊" if roster.get("chat_id") == user_id else "群組"
    day = roster_day(roster)
    return f"{where} {when.month}/{when.day} {when:%H:%M} 上傳的{day_label(day) if day else '（日期未辨識）'}班表"


def sent_time(roster: dict) -> float:
    """When the photo was sent (rows from before ``sent_at`` existed: when it was saved)."""
    sent = roster.get("sent_at") if roster.get("sent_at") is not None else roster.get("created_at")
    return sent or 0


HELD_DAYS = (0, 1, 2)   # the dates offered for a held photo, counted from the day it was sent
# The days a photo can be given, counted from the day it was sent: an old roster posted again up to two months
# later, a roster posted up to three weeks ahead (as header_date.WINDOW allows). A year typo on a date picker
# (2027-10-01) would otherwise make the photo everyone's newest roster.
PICK_WINDOW = (-60, 21)


def pick_range(roster: dict) -> tuple[date, date]:
    sent = datetime.fromtimestamp(sent_time(roster), TAIPEI).date()
    return sent + timedelta(days=PICK_WINDOW[0]), sent + timedelta(days=PICK_WINDOW[1])


def picked_day(value, roster: dict) -> date | None:
    """A day a card sent back for ``roster`` (a date picker's params, a button's data), or None when it is not a
    date or lies outside ``pick_range``: ignored like any malformed tap."""
    day = iso_day(value)
    low, high = pick_range(roster)
    return day if day and low <= day <= high else None


def redate_picker(roster: dict, label: str = "改這張的日期", *, data: str | None = None,
                  near: date | None = None) -> dict:
    """LINE's date picker bound to this very photo (``redate|run``, or ``data``): the day picked comes back with the
    postback (``params["date"]``), so it cannot land on another photo as a typed 「日期」 can. It opens on the photo's
    date, or without one on ``near`` (by default the day it was sent)."""
    low, high = pick_range(roster)
    initial = roster_day(roster) or near or datetime.fromtimestamp(sent_time(roster), TAIPEI).date()
    initial = min(max(initial, low), high)   # LINE refuses an initial day outside min..max
    return {"type": "datetimepicker", "label": label, "data": data or f"redate|{roster['run_id']}", "mode": "date",
            "initial": initial.isoformat(), "min": low.isoformat(), "max": high.isoformat()}


def date_request_card(roster: dict, audience: int, *, typed: date | None = None) -> dict:
    """Ask an administrator which day a held group photo is; nobody has been notified about it yet.

    Every button carries that photo's run id, so the answer cannot land on another photo. Under the three days from
    the one it was sent, 「📅 其他日期」 opens LINE's date picker on that photo (``date|run|pick``, the day in the
    postback's params) for any other day: an old roster posted again later is older than all three. ``typed`` is the
    date the administrator typed (「日期 月/日」 names no photo): the card then asks to confirm it with a button.
    ``audience`` is how many people have notifications on: with nobody, the date only makes the roster
    findable by day, and the card does not speak of notifying.
    """
    when = datetime.fromtimestamp(sent_time(roster), TAIPEI)
    source = f"群組 {when.month}/{when.day} {when:%H:%M} 上傳的班表"
    days = [typed] if typed else [when.date() + timedelta(days=offset) for offset in HELD_DAYS]
    reason = "這張班表還在等日期" if typed else "表頭的日期沒有讀出來"
    if audience:
        question, confirm = "要用這個日期通知嗎？", "，通知大家"
        effect = f"還沒有通知任何人。按下日期後才會通知已開通知的 {audience} 人。"
    else:
        question, confirm = "要設成這個日期嗎？", ""
        effect = "確認日期之前，「我的班表」和日期都查不到這張。"
    contents = [
        {"type": "text", "text": question if typed else "⚠ 班表日期讀不到", "weight": "bold",
         "size": "lg", "wrap": True, "color": "#B45309"},
        {"type": "text", "text": source, "weight": "bold", "decoration": "underline", "size": "md",
         "wrap": True, "margin": "sm", "color": "#244F87"},
        {"type": "text", "text": f"{reason}，{effect}", "size": "sm", "wrap": True, "margin": "md",
         "color": "#1B2430"}]
    for day in days:
        contents.append({"type": "button", "style": "primary", "height": "sm", "margin": "md", "color": "#244F87",
                         "action": {"type": "postback", "label": day_label(day) + (confirm if typed else ""),
                                    "data": f"date|{roster['run_id']}|{day.isoformat()}",
                                    "displayText": f"這張是 {day_label(day)}的班表"}})
    if not typed:   # a box, not a button: LINE's buttons cut a label that does not fit a small phone
        contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "8px",
                         "cornerRadius": "8px", "borderWidth": "1px", "borderColor": "#244F87",
                         "action": redate_picker(roster, "其他日期", data=f"date|{roster['run_id']}|pick",
                                                 near=when.date()), "contents": [
                             {"type": "text", "text": "📅 其他日期", "size": "sm", "weight": "bold", "align": "center",
                              "color": "#244F87"},
                             {"type": "text", "text": "舊班表重貼時選表頭印的那天" + ("，選好就通知" if audience else ""),
                              "size": "xxs", "wrap": True, "align": "center", "color": "#666666"}]})
    contents.append({"type": "button", "style": "link", "height": "sm", "margin": "sm",
                     "action": {"type": "postback", "label": "略過這張",
                                "data": f"date|{roster['run_id']}|skip", "displayText": "略過這張"}})
    return {"type": "flex", "altText": f"⚠ {source}：{reason}，請確認是哪一天。"[:400],
            "contents": {"type": "bubble", "size": "mega", "body": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": contents}}}


def undated_notice(roster: dict, *, inline: bool = False) -> dict:
    """What the uploader of a group photo whose date could not be read is told while there is no administrator to
    ask: a short text with LINE's date picker on that very photo (``redate|run``) under it. A typed 「日期」 would
    change whichever photo they sent last.

    ``inline``: the picker is a row of a card instead of a quick reply. A notice filed in the 收件匣 comes in front of
    another reply, whose own last message has the only quick replies LINE shows."""
    when = datetime.fromtimestamp(sent_time(roster), TAIPEI)
    words = (f"⚠ 你在群組 {when.month}/{when.day} {when:%H:%M} 傳的班表，日期未辨識：表頭的日期沒有讀出來。\n"
             "請點下方「📅 選這張的日期」，選班表上印的那一天；只會改這一張。\n"
             "（按鈕不見了，也可以私訊「日期 月/日」）")
    picker = redate_picker(roster, "📅 選這張的日期")
    if not inline:
        return {**text(words), "quickReply": {"items": [{"type": "action", "action": picker}]}}
    row = {"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "10px", "cornerRadius": "12px",
           "backgroundColor": STAFF_UI["chip"], "action": picker, "contents": [
               {"type": "text", "text": "📅 選這張的日期", "size": "sm", "weight": "bold", "align": "center",
                "wrap": True, "color": STAFF_UI["navy"]}]}
    body = {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
        {"type": "text", "text": words, "size": "sm", "wrap": True, "color": "#1B2430"}, row]}
    return {"type": "flex", "altText": words.splitlines()[0][:400],
            "contents": {"type": "bubble", "size": "mega", "body": body}}


REPORT_UI = {   # the band of an upload report, by outcome; the title says it in words as well
    "ok": ("#1E7B3A", "#2A8C6A"), "failed": ("#B45309", "#C2410C"), "skipped": ("#5F6B7A", "#7A8696")}
REPORT_TITLES = {"ok": "✅ 群組班表辨識成功", "failed": "⚠️ 群組班表辨識失敗", "skipped": "🚫 不像班表，已略過"}
REPORT_UNNAMED = 12   # codes listed in one row of a report, and unread codes on its 「🔍」 lines; the rest only counted
REPORT_BLANK = 30     # numeric codes listed on the 「⬜ 當天空白的代號」 row: a sheet leaves about ten empty
REPORT_NOBODY = 6     # codes on the 「⚠️」 lines, each with its first REPORT_STRETCHES stretches of duty
REPORT_STRETCHES = 3
REPORT_UNSURE = 10    # low-confidence cells on the 「❓」 lines
REPORT_READING = 8    # characters of what the photo read for a name, on the 「🔍」 lines
NAME_TABLE_CODES = frozenset(code for column in NAME_COLUMNS for code in column)   # 辰, 巳 and 36–53 are not printed
# Why nobody has a code with duties: its numeric name cell is empty, the settings give that fixed code to nobody for
# the day, or the sheet's name table does not print it at all; in this order wherever they are grouped
NOBODY_REASONS = ("姓名欄空白", "固定代號沒有設定", "班表上沒有這個代號")
NOBODY_CELLS = dict(zip(NOBODY_REASONS, (" 空白", " 沒設定", " 表上沒有")))   # on 「勤務人員」, after the code
CODE_ORDER = {code: index for index, code in enumerate(ALLOWED_CODES)}


def code_key(code: str) -> tuple[int, str]:
    return CODE_ORDER.get(code, 999), code


def unnamed_codes(result: dict, names: dict[str, str] | None, fixed=None, *,
                  codes=None, later=()) -> tuple[list[str], dict[str, str]]:
    """The codes with duties (or ``codes``) but no name, in code order, as (unread, nobody).

    ``nobody`` (code → one of ``NOBODY_REASONS``) are codes nobody has that day, so there is no name to find: a
    numeric code whose name cell is empty on the sheet (``names_blank``), a fixed code (Chinese or English) the
    settings give nobody for the roster's day, whatever its cell on the sheet holds (fixed codes follow the settings,
    not the photo), and a code the sheet's name table does not print at all (辰). A duty on one of them is mostly a
    misread code in the grid, or a 兼服 mark (「#26」). ``unread``: somebody has the code and the name table could not
    put a name to them. ``fixed``: the fixed codes in force for that day (``StaffBook.fixed_on``); None takes those
    ``names`` names, as ``names_for`` and ``match_names`` always name every one in force. Results read before blank
    cells were told apart have no ``names_blank``: a numeric code without a name is unread there, as before.
    ``later``: fixed codes set now but only from after the roster's day (``StaffBook.set_after``): on that roster they
    were read off the sheet like a numeric code, so a blank cell is 「姓名欄空白」 and anything else is unread.
    """
    named = (names or {}).keys()
    fixed = set(named if fixed is None else fixed)
    blank = set(result.get("names_blank") or ())
    if codes is None:
        codes = {person["code"] for person in result.get("people", [])}
    unread, nobody = [], {}
    for code in sorted(set(codes) - named, key=code_key):
        if code in fixed:   # someone has it today, by the settings
            unread.append(code)
        elif code not in NAME_TABLE_CODES:
            nobody[code] = NOBODY_REASONS[2]
        elif code in FIXED_CODES and code not in later:
            nobody[code] = NOBODY_REASONS[1]
        elif code in blank:
            nobody[code] = NOBODY_REASONS[0]
        else:
            unread.append(code)
    return unread, nobody


def nobody_words(nobody: dict[str, str], cap: int | None = None) -> str:
    """「76、77（姓名欄空白）、未（固定代號沒有設定）」: the codes nobody has, grouped by why; past ``cap`` codes
    only counted (「… 等 52 個」)."""
    groups, shown = [], 0
    for reason in NOBODY_REASONS:
        codes = [code for code in nobody if nobody[code] == reason]
        codes = codes if cap is None else codes[:cap - shown]
        if codes:
            groups.append("、".join(codes) + f"（{reason}）")
            shown += len(codes)
    return "、".join(groups) + (f"等 {len(nobody)} 個" if shown < len(nobody) else "")


def nobody_note(codes, result: dict, names: dict[str, str], later=()) -> tuple[dict[str, str], str | None]:
    """Of ``codes``, those nobody has that day (``unnamed_codes``: code → why) and a note naming them, for the cards
    that list people: such a code is not 「姓名未辨識」, and its duty may be a misread code. ``later``: as there."""
    _, nobody = unnamed_codes(result, names, codes=codes, later=later)
    return nobody, (f"⬜ {nobody_words(nobody, REPORT_UNNAMED)}：當天沒有這個人，請對照原圖" if nobody else None)


def _listed(codes: list[str], cap: int) -> str:
    return "、".join(codes[:cap]) + (" 等" if len(codes) > cap else "")


def _stretches(slots: list[dict]) -> list[str]:
    """「14–16 國際線」 for each run of consecutive hours on one work, in time order (works in roster column order)."""
    rows: dict[str, set[int]] = {}
    columns: dict[str, int] = {}
    for slot in slots:
        work = work_name(slot)
        rows.setdefault(work, set()).add(slot["row"])
        columns[work] = min(columns.get(work, slot["column"]), slot["column"])
    runs = []
    for work, hours in rows.items():
        hours = sorted(hours)
        start = hours[0]
        for previous, row in zip(hours, hours[1:] + [None]):
            if row != previous + 1:
                runs.append((start, columns[work], f"{start + 1:02d}–{previous + 2:02d} {work}"))
                start = row
    return [words for *_, words in sorted(runs)]


def _reading(raw) -> str | None:
    """What the photo most often read in one name cell (``result["names"]``: one string or a list, a reading per
    variant); None when nothing was read there."""
    readings = [re.sub(r"\s+", "", r) for r in (raw if isinstance(raw, list) else [raw]) if isinstance(r, str)]
    counted = Counter(r for r in readings if r).most_common(1)
    if not counted:
        return None
    word = counted[0][0]
    return word if len(word) <= REPORT_READING else word[:REPORT_READING] + "…"


def _code_line(code: str, before: str = "", after: str = "") -> dict:
    """One line of a report's detail: the code in bold between plain words."""
    spans = ([{"type": "span", "text": before}] if before else []) + [
        {"type": "span", "text": code, "weight": "bold", "color": STAFF_UI["navy"]}] + (
        [{"type": "span", "text": after}] if after else [])
    return {"type": "text", "text": before + code + after, "size": "xs", "wrap": True, "margin": "sm",
            "color": STAFF_UI["ink"], "contents": spans}


def _report_detail(title: str, lines: list[dict], hints: list[str], color: str) -> dict:
    """A small white panel under the report's rows: a bold title, a line per code or cell, muted hints."""
    contents = [{"type": "text", "text": title, "size": "sm", "weight": "bold", "wrap": True, "color": color}, *lines]
    contents += [{"type": "text", "text": hint, "size": "xxs", "wrap": True, "margin": "md",
                  "color": STAFF_UI["muted"]} for hint in hints]
    return {"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "12px", "cornerRadius": "12px",
            "backgroundColor": STAFF_UI["white"], "contents": contents}


def _more(count: int, word: str) -> dict:
    return {"type": "text", "text": f"…還有 {count} {word}", "size": "xs", "margin": "sm", "color": STAFF_UI["muted"]}


def _report_details(result: dict, names: dict[str, str], unread: list[str], nobody: dict[str, str],
                    unsure: list[tuple[dict, dict]], day: date | None = None) -> list[dict]:
    """The panels under a recognised roster's rows: where the codes nobody has got duties, every low-confidence cell
    with who is on it, and what the photo read for each name it could not match."""
    panels = []
    if nobody:
        slots = {person["code"]: person.get("slots", []) for person in result.get("people", [])}
        lines = []
        for code in list(nobody)[:REPORT_NOBODY]:
            runs = _stretches(slots.get(code, []))
            hours = "、".join(runs[:REPORT_STRETCHES])
            if len(runs) > REPORT_STRETCHES:
                hours += f" 等 {len(slots[code])} 格"
            lines.append(_code_line(code, after=f"（{nobody[code]}）"))
            if hours:   # on a line of its own: where to look on the photo
                lines.append({"type": "text", "text": hours, "size": "xs", "wrap": True, "color": STAFF_UI["ink"]})
        if len(nobody) > REPORT_NOBODY:
            lines.append(_more(len(nobody) - REPORT_NOBODY, "個代號"))
        hints = ["多半是勤務格的代號讀錯了，請對照原圖；兼服（#）的格子除外。"]
        unset = [code for code in nobody if nobody[code] == NOBODY_REASONS[1]]
        if unset:
            hints.append(f"固定代號其實有人的話，傳「代號 {unset[0]} 姓名」設定。")
        panels.append(_report_detail("⚠️ 沒有這個人，卻讀到勤務", lines, hints, "#B45309"))
    if unsure:
        lines = []
        for person, slot in unsure[:REPORT_UNSURE]:
            code = person["code"]
            lines.append(_code_line(code, f"{hour_label(slot['row'])} {work_name(slot)}　",
                                    (f" {names[code]}" if code in names else "")
                                    + f" {round(slot.get('confidence', 0) * 100)}%"))
        if len(unsure) > REPORT_UNSURE:
            lines.append(_more(len(unsure) - REPORT_UNSURE, "格"))
        panels.append(_report_detail("❓ 信心較低的格子", lines, ["請對照原圖確認這幾格的代號。"], STAFF_UI["navy"]))
    if unread:
        raw = result.get("names") or {}
        lines = []
        for code in unread[:REPORT_UNNAMED]:
            reading = _reading(raw.get(code, []))
            lines.append(_code_line(code, after=f"：照片上讀到「{reading}」" if reading else "：照片上的字讀不出來"))
        if len(unread) > REPORT_UNNAMED:
            lines.append(_more(len(unread) - REPORT_UNNAMED, "個代號"))
        when = f" {day.month}/{day.day}" if day else " 月/日"   # a claim with its date: never another day's roster
        hints = [f"名冊上沒有這個姓名的話，傳「新增人員 姓名」加進名冊，之後就會配對；本人也可以傳「我是 {unread[0]}{when}」。"]
        panels.append(_report_detail("🔍 姓名沒讀出來", lines, hints, STAFF_UI["navy"]))
    return panels


def upload_report(status: str, who: str, sent: float, *, roster: dict | None = None, result: dict | None = None,
                  names: dict[str, str] | None = None, fixed=None, later=(), reason: str = "", told: bool | str = False,
                  replaced: bool = False, superseded: bool = False, threshold: float = .75, link: bool = False,
                  has_image: bool = False) -> dict:
    """What an administrator is told about one photo posted in a group: ``status`` "ok" (recognised: its date,
    how many codes have duties, how many of them have a name), "failed" (a roster that could not be read) or
    "skipped" (it did not look like a roster at all). Nothing posted in a group goes unseen by them: a roster
    shot too badly to pass for one looks, to the bot, like any other photo.

    ``told``: the uploader was told privately as well (or words saying how: it waits in their 收件匣). ``replaced``:
    an earlier photo of the same day exists, so this is the day's update; ``superseded``: a photo of that day sent
    later exists, so this one is only filed.
    ``link``: a footer opens the whole photo; ``has_image``: the 勤務人員 crop follows the card.

    A recognised roster's codes without a name are told apart (``unnamed_codes``, with ``fixed``, the fixed codes
    in force): those nobody has that day are not matchable, so 「姓名配對」 leaves them out and 「⚠️ 沒有這個人」
    counts them; 「⬜ 當天空白的代號」 lists every numeric code whose name cell is empty on the sheet, duties or not,
    so the administrator can check the empty cells were read right. Panels under the rows then say where the
    codes nobody has got duties, which cells are unsure and who is on them, and what the photo read for each name
    that was not matched.
    """
    when = datetime.fromtimestamp(sent or 0, TAIPEI)
    rows: list[tuple[str, str]] = []
    listed: dict[str, str] = {}   # row label → the codes it counts, on a line of their own under it
    notes: list[str] = []
    panels: list[dict] = []
    if status == "ok":
        day = roster_day(roster)
        rows.append(("📅 班表日期", day_label(day) if day else "日期未辨識"))
        if superseded:
            rows.append(("🗂️ 版本", "同一天已有更晚的，只存檔"))
        elif replaced:
            rows.append(("🔄 版本", "同一天的更新版"))
        names = names or {}
        people = result.get("people", [])
        on_duty = {person["code"] for person in people}
        unread, nobody = unnamed_codes(result, names, fixed, later=later)
        matchable = len(on_duty) - len(nobody)   # nobody has these today: no name to match
        rows.append(("👥 有勤務", f"{len(on_duty)} 個代號"))
        rows.append(("👤 姓名配對", f"{matchable - len(unread)}／{matchable}"
                                    + (f"（{len(unread)} 個沒讀出來）" if unread else "，全部對到" if matchable else "")))
        if nobody:
            rows.append(("⚠️ 沒有這個人", f"{len(nobody)} 個"))
            listed["⚠️ 沒有這個人"] = _listed(list(nobody), REPORT_UNNAMED)
        unsure = sorted(((person, slot) for person in people for slot in person.get("slots", [])
                         if slot.get("confidence", 0) < threshold),
                        key=lambda pair: (pair[1]["row"], pair[1]["column"], code_key(pair[0]["code"])))
        if unsure:
            rows.append(("❓ 信心較低", f"{len(unsure)} 格"))
        blank = sorted((code for code in set(result.get("names_blank") or ()) if code.isdigit()), key=code_key)
        if blank:   # fixed codes follow the settings, whatever their cells hold
            rows.append(("⬜ 當天空白的代號", f"{len(blank)} 個"))
            listed["⬜ 當天空白的代號"] = _listed(blank, REPORT_BLANK)
        elif "names_blank" in result and result.get("names"):   # the table was read, and no cell came out empty
            rows.append(("⬜ 當天空白的代號", "沒有"))
        panels = _report_details(result, names, unread, nobody, unsure, roster_day(roster) if roster else None)
        if has_image:
            notes.append("📷 下方是勤務人員（姓名代碼對照表）的原圖，可點開放大")
    else:
        rows.append(("原因", reason or "沒有說明"))
        rows.append(("上傳者", told if isinstance(told, str) else "已私訊告知" if told else "沒有私訊"))
        notes.append("如果這是班表：請把整張主表格（四個角都要入鏡）拍清楚後重傳到群組。"
                     + ("一般照片可以忽略這則。" if status == "skipped" else "原有班表沒有更新。"))
    start, end = REPORT_UI[status]
    header = {"type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "120deg", "startColor": start, "endColor": end},
              "contents": [{"type": "text", "text": REPORT_TITLES[status], "weight": "bold", "size": "lg",
                            "wrap": True, "color": STAFF_UI["white"]},
                           {"type": "text", "text": f"{who}｜{when.month}/{when.day} {when:%H:%M} 傳到群組",
                            "size": "xs", "wrap": True, "color": "#F1F5F9"}]}
    lines = [{"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "sm", "color": STAFF_UI["muted"], "flex": 0},
        {"type": "text", "text": value, "size": "sm", "weight": "bold", "wrap": True, "align": "end",
         "color": STAFF_UI["ink"]}]} for label, value in rows]
    for line in lines:   # a list of codes reads better on a line of its own than wrapped at the right edge
        label = line["contents"][0]["text"]
        if label in listed:
            line.update(layout="vertical", spacing="xs", contents=[
                {"type": "box", "layout": "horizontal", "spacing": "md", "contents": line["contents"]},
                {"type": "text", "text": listed[label], "size": "xs", "wrap": True, "color": STAFF_UI["ink"]}])
    body = [{"type": "box", "layout": "vertical", "paddingAll": "12px", "cornerRadius": "12px",
             "backgroundColor": STAFF_UI["white"], "contents": lines}]
    if status == "ok" and roster:
        # LINE's date picker, bound to this very photo: a typed 「日期」 changes whichever was posted last
        body.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "10px", "cornerRadius": "12px",
                     "backgroundColor": STAFF_UI["chip"], "action": redate_picker(roster, near=when.date()), "contents": [
                         {"type": "text", "text": "📅 日期不對？點這裡改這張的日期", "size": "sm", "weight": "bold",
                          "align": "center", "wrap": True, "color": STAFF_UI["navy"]}]})
    body += panels
    if notes:
        body.append(_notes(notes))
    bubble = {"type": "bubble", "size": "mega", "header": header,
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                       "contents": body},
              "styles": {"header": {"backgroundColor": start}}}
    if link and roster:
        bubble["footer"] = original_roster_link(roster)
        bubble["styles"]["footer"] = {"backgroundColor": STAFF_UI["page"]}
    alt = f"{REPORT_TITLES[status]}｜{who} {when.month}/{when.day} {when:%H:%M}｜" + "，".join(
        f"{label.split(' ')[-1]} {value}" + (f"（{listed[label]}）" if label in listed else "") for label, value in rows)
    return {"type": "flex", "altText": alt[:400], "contents": bubble}


def hour_label(row: int) -> str:
    return f"{row + 1:02d}–{row + 2:02d}"


def slot_counts(result: dict, area: str) -> dict[int, int]:
    """How many people are on 國際 or 國內 duty in each hour row: someone on two duties that hour counts once,
    as in the 「共 N 人」 of ``slot_card``."""
    columns = AREAS[area][1]
    counts: dict[int, int] = {}
    for person in result.get("people", []):
        for row in {s["row"] for s in person["slots"] if s["column"] in columns}:
            counts[row] = counts.get(row, 0) + 1
    return counts


def who_is_on(result: dict, row: int, area: str, names: dict[str, str], roster: dict, *,
              threshold: float = .75, later=()) -> str:
    """Everyone on 國際 or 國內 duty in one hour, grouped by duty in roster column order."""
    groups = slot_groups(result, row, area, threshold=threshold)
    label = AREAS[area][0]
    day = roster_day(roster)
    title = f"{day_label(day) + '班表' if day else '（日期未辨識）'}\n{hour_label(row)} {label}"
    if not groups:
        return f"{title}\n\n這個時段{label}沒有辨識到人員。"
    lines, total, flagged = [title], set(), False
    for duty, people in groups:
        entries = []
        for code, low in people:
            entries.append(f"{code} {names[code]}" if code in names else code)
            entries[-1] += " ？" if low else ""
            total.add(code); flagged |= low
        lines.append(f"{duty}：{'、'.join(entries)}")
    lines.append(f"共 {len(total)} 人")
    if flagged:
        lines.append("？＝辨識信心較低，請對照原圖")
    nobody, note = nobody_note(total, result, names, later)
    if total - names.keys() - nobody.keys():
        lines.append("只有代號的人：姓名對照表沒辨識出來")
    if note:
        lines.append(note)
    return "\n".join(lines)


def slot_groups(result: dict, row: int, area: str, *, threshold: float = .75) -> list:
    """Ordered duty groups, with each person's code and confidence flag."""
    label, columns = AREAS[area]
    order = {code: n for n, code in enumerate(ALLOWED_CODES)}
    groups: dict[str, list[tuple[int, str, bool]]] = {}
    first_column: dict[str, int] = {}
    for person in result.get("people", []):
        for slot in person["slots"]:
            if slot["row"] != row or slot["column"] not in columns:
                continue
            duty = SHORT_DUTY.get(slot["duty"], slot["duty"])
            first_column[duty] = min(first_column.get(duty, 99), slot["column"])
            groups.setdefault(duty, []).append((slot["column"], person["code"], slot["confidence"] < threshold))
    return [(duty, [(code, low) for _, code, low in
                    sorted(groups[duty], key=lambda e: (e[0], order.get(e[1], 999)))])
            for duty in sorted(groups, key=first_column.get)]


def _glass_pill(value: str) -> dict:
    """A short fact on the gradient band, in white on dark glass so it reads on navy and on teal alike."""
    return {"type": "box", "layout": "vertical", "flex": 0, "paddingTop": "4px", "paddingBottom": "4px",
            "paddingStart": "10px", "paddingEnd": "10px", "cornerRadius": "20px", "backgroundColor": STAFF_UI["glass"],
            "contents": [{"type": "text", "text": value, "size": "xs", "weight": "bold", "color": STAFF_UI["white"]}]}


def slot_card(result: dict, row: int, area: str, names: dict[str, str], roster: dict, *,
              threshold: float = .75, has_image: bool = False, later=()) -> dict:
    """Who is on duty in one hour of 國際 or 國內: a rounded row per duty, in roster column order, with the duty
    as a pill and its people one per line (code in bold, then the name; 「？」 where the cell is unsure)."""
    groups = slot_groups(result, row, area, threshold=threshold)
    codes = {code for _, people in groups for code, _ in people}
    day = roster_day(roster)
    date_text = day_label(day) + "班表" if day else "日期未辨識"
    label = AREAS[area][0]
    heading = f"{hour_label(row)} {label}"
    pills = [_glass_pill(f"👥 共 {len(codes)} 人")] + ([_glass_pill(f"📋 {len(groups)} 項勤務")] if groups else [])
    header = _hero(f"{AREA_EMOJI[area]} {heading}線", date_text,
                   [{"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": pills}])

    def person(code: str, low: bool) -> dict:
        shown = f"{code} {names[code]}" if code in names else code
        spans = [{"type": "span", "text": code, "weight": "bold", "color": STAFF_UI["navy"]}]
        if code in names:
            spans.append({"type": "span", "text": f" {names[code]}"})
        if low:
            spans.append({"type": "span", "text": " ？", "weight": "bold", "color": SLOT_UI["now"]})
        return {"type": "text", "text": shown + (" ？" if low else ""), "size": "sm", "wrap": True,
                "color": STAFF_UI["ink"], "contents": spans}

    contents, alt = [], [f"{date_text}｜{heading}｜共 {len(codes)} 人"]
    for duty, people in groups:
        lines = [person(code, low) for code, low in people]
        contents.append({"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm", "paddingAll": "10px",
                         "cornerRadius": "14px", "backgroundColor": STAFF_UI["white"], "borderWidth": "1px",
                         "borderColor": STAFF_UI["line"], "contents": [
            {"type": "box", "layout": "vertical", "flex": 0, "width": "96px", "contents": [   # keeps the pill short
                {"type": "box", "layout": "vertical", "paddingAll": "6px", "cornerRadius": "10px",
                 "backgroundColor": SLOT_UI[area][1], "contents": [
                     {"type": "text", "text": duty, "weight": "bold", "size": "sm", "wrap": True, "align": "center",
                      "color": STAFF_UI["ink"]},
                     {"type": "text", "text": f"{len(people)} 人", "size": "xxs", "align": "center",
                      "color": STAFF_UI["ink"]}]}]},
            {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "contents": lines}]})
        alt.append(f"{duty}：" + "、".join(line["text"] for line in lines))
    if not groups:
        contents.append({"type": "box", "layout": "vertical", "paddingAll": "18px", "cornerRadius": "14px",
                         "backgroundColor": STAFF_UI["white"], "contents": [
            {"type": "text", "text": "🍃", "size": "xxl", "align": "center"},
            {"type": "text", "text": "這個時段沒有辨識到人員。", "size": "sm", "wrap": True, "align": "center",
             "color": STAFF_UI["ink"], "margin": "sm"}]})
    notes = [f"⚠️ {result['duty_recheck_error']}"] if result.get('duty_recheck_error') else []
    if any(low for _, people in groups for _, low in people):
        notes.append("❓ 標「？」的勤務待確認，請對照原圖")
    nobody, note = nobody_note(codes, result, names, later)
    if codes - names.keys() - nobody.keys():
        notes.append("🔤 只有代號：姓名未辨識")
    if note:
        notes.append(note)
    if has_image:
        notes.append("📷 下方為本時段原圖，可點開放大")
    if notes:
        contents.append({"type": "text", "text": "\n".join(notes), "size": "xs", "wrap": True,
                         "color": STAFF_UI["muted"], "margin": "lg"})
    contents[0]["margin"] = "none"
    return {"type": "flex", "altText": "｜".join(alt)[:400], "contents": {
        "type": "bubble", "size": "giga", "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                 "contents": contents},
        "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}}


def work_name(slot: dict) -> str:
    """One display name per work, including a clear fallback for unreadable headers."""
    raw = (slot.get("duty") or "").strip()
    column = int(slot["column"])
    if not raw or re.fullmatch(r"c\d+", raw, re.I):
        return f"第{column}欄（未辨識）"
    return SHORT_DUTY.get(raw, raw)


def work_options(result: dict) -> list[tuple[str, int, int]]:
    """(work name, first column, distinct staff count), in printed column order."""
    columns: dict[str, int] = {}
    workers: dict[str, set[str]] = {}
    for person in result.get("people", []):
        for slot in person.get("slots", []):
            name = work_name(slot)
            columns[name] = min(columns.get(name, slot["column"]), slot["column"])
            workers.setdefault(name, set()).add(person["code"])
    return [(name, columns[name], len(workers[name])) for name in sorted(columns, key=lambda n: (columns[n], n))]


def work_key(name: str) -> str:
    """Short, stable postback key; OCR text can be too long for LINE's data field."""
    return sha256(name.encode("utf-8")).hexdigest()[:20]


WORK_COLUMNS = 3     # work tiles per row on 「查看工作」
WORK_PEOPLE = 2      # people per line beside an hour on one work's card
WORK_GROUPS = {      # 「查看工作」 sections, by the column a work starts in; the tint of its tiles and hour pills
    "intl": ("✈️ 國際", "#E3EEFC"), "dom": ("🏠 國內", "#DFF3EF"), "other": ("🧩 其他", "#EEEAFB")}


def work_area(column: int) -> str:
    """``intl`` or ``dom`` for the columns 查看時段 counts as 國際／國內 (``AREAS``), ``other`` for A1 and right."""
    return next((area for area, (_, columns) in AREAS.items() if column in columns), "other")


def _relative_day(day: date | None, now: datetime) -> str:
    """「・今天」, 「・明天」 or 「・昨天」 after a roster date; nothing for other days."""
    return {-1: "・昨天", 0: "・今天", 1: "・明天"}.get((day - now.date()).days, "") if day else ""


def _day_pill(day: date | None, now: datetime) -> dict:
    """「📅 10/8（四）・今天」 for the gradient band: the date, and how it stands to today."""
    return _glass_pill(f"📅 {day_label(day) + _relative_day(day, now) if day else '班表日期未辨識'}")


def _pill_row(pills: list[dict]) -> dict:
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": pills}


def _notes(lines: list[str]) -> dict:
    """The muted lines at the foot of a card: what a mark means, what was not read, where the photo is."""
    return {"type": "text", "text": "\n".join(lines), "size": "xs", "wrap": True, "color": STAFF_UI["muted"],
            "margin": "lg"}


def _list_bubble(header: dict, body: list[dict], footer: dict | None = None) -> dict:
    """The giga bubble of 查看工作／勤務人員／休假人員: the band, then ``body`` on the pale page."""
    body[0] = {**body[0], "margin": "none"}
    bubble = {"type": "bubble", "size": "giga", "header": header,
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                       "contents": body},
              "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}
    if footer:
        bubble["footer"] = footer
        bubble["styles"]["footer"] = {"backgroundColor": STAFF_UI["page"]}
    return bubble


def original_roster_link(roster: dict) -> dict:
    """「查看整張班表」 as a tappable footer row (``roster|run``: the bot answers with the whole photo): a LINE button
    there cuts its label on a small phone."""
    return {"type": "box", "layout": "vertical", "paddingTop": "0px", "paddingBottom": "16px", "paddingStart": "16px",
            "paddingEnd": "16px", "contents": [
                {"type": "box", "layout": "vertical", "paddingAll": "10px", "cornerRadius": "12px",
                 "backgroundColor": STAFF_UI["white"], "borderWidth": "1px", "borderColor": STAFF_UI["line"],
                 "action": {"type": "postback", "label": "查看整張班表", "data": f"roster|{roster['run_id']}",
                            "displayText": "查看整張班表"},
                 "contents": [{"type": "text", "text": "🖼️ 查看整張班表", "size": "sm", "weight": "bold",
                               "align": "center", "color": STAFF_UI["navy"]}]}]}


def _photo_hint(has_image: bool, show_original: bool) -> str:
    """Where to check what was not read: the crop sent under the card, the whole photo, or the paper."""
    if has_image:
        return "請看下方原圖"
    return "請看整張班表" if show_original else "請對照班表照片"


WORK_BREAKS = re.compile(r"(?<=[號站線欄])|(?=（)")   # where a long work name may go on to a second line


def tile_name(name: str) -> str:
    """A work's name for its tile: as it is when it fits a third of a small phone, else on two lines where it reads
    naturally (「國際1、2號／託運」, 「防爆／（清艙）」, 「第15欄／（未辨識）」), the most even such break; in the
    middle when there is none. Left to LINE, the line would break wherever the width ran out (「…託／運」)."""
    def width(text):   # in Chinese characters; Latin letters and digits are about half as wide
        return sum(.55 if char.isascii() else 1 for char in text)

    if width(name) <= 5:
        return name
    breaks = [match.start() for match in WORK_BREAKS.finditer(name) if 0 < match.start() < len(name)]
    cut = min(breaks or [len(name) // 2], key=lambda index: abs(width(name[:index]) - width(name[index:])))
    return f"{name[:cut]}\n{name[cut:]}"


def _work_tile(roster: dict, name: str, count: int, tint: str) -> dict:
    """One work: its name (``tile_name``; a short one shrinks rather than being cut on a narrow phone) and its head
    count; a tap lists its day. Every tile has room for two lines, so the grid stays even."""
    shown = tile_name(name)
    label = {"type": "text", "text": shown, "size": "sm", "weight": "bold", "align": "center", "color": STAFF_UI["ink"]}
    if "\n" in shown:
        label["wrap"] = True
    else:
        label["adjustMode"] = "shrink-to-fit"
    return {"type": "box", "layout": "vertical", "flex": 1, "height": "58px", "justifyContent": "center",
            "paddingStart": "4px", "paddingEnd": "4px", "cornerRadius": "12px", "backgroundColor": tint,
            "action": {"type": "postback", "label": name[:20], "data": f"work|{roster['run_id']}|{work_key(name)}",
                       "displayText": f"查看工作 {name}"},
            "contents": [label, {"type": "text", "text": f"{count} 人", "size": "xxs", "align": "center",
                          "color": STAFF_UI["muted"]}]}


def work_picker(roster: dict, result: dict, *, now: datetime | None = None) -> dict:
    """「查看工作」 on one card, nothing to swipe: the works read off this roster as tiles under ✈️ 國際, 🏠 國內 and
    🧩 其他 (by the column each starts in, as 查看時段 divides them), with how many people work each during the day.
    A tap lists them hour by hour (``work|run|key``, the postback of the old 查看 buttons, so cards sent before
    keep working).

    A roster has at most 35 work columns; all of them come to about 17 KB, well under LINE's limit.
    """
    now = now or taipei_now()
    options = work_options(result)
    day = roster_day(roster)
    date_text = f"{day_label(day)}班表" if day else "日期未辨識"
    groups: dict[str, list[tuple[str, int]]] = {area: [] for area in WORK_GROUPS}
    for name, column, count in options:
        groups[work_area(column)].append((name, count))
    body = []
    for area, works in groups.items():
        if works:
            label, tint = WORK_GROUPS[area]
            body += [_section(label, f"{len(works)} 項"),
                     *_rows([_work_tile(roster, name, count, tint) for name, count in works], WORK_COLUMNS)]
    notes = ["👥 人數＝整天輪到這項工作的人"]
    if any(name.endswith("欄（未辨識）") for name, _, _ in options):
        notes.append("🔤 「第 N 欄」：表頭沒讀出來，用欄位代替")
    body = body or [{"type": "text", "text": "這張班表還沒有辨識到可查的工作。", "size": "sm", "wrap": True,
                     "color": STAFF_UI["ink"]}]
    header = _hero("🧭 查看工作", "點一項工作，看它整天的上班名單",
                   [_pill_row([_day_pill(day, now), _glass_pill(f"📋 {len(options)} 項工作")])])
    alt = f"{date_text}｜選擇工作查看整天名單｜" + "、".join(name for name, _, _ in options)
    return {"type": "flex", "altText": alt[:400], "contents": _list_bubble(header, [*body, _notes(notes)])}


def _work_person(code: str, names: dict[str, str], low: bool, plain: bool = False) -> dict:
    """「72 林小明」 with the code in bold (only the code when the name table gave none), 「？」 where unsure.

    ``plain``: the same words as one plain text, about half the bytes, for a work too busy to fit otherwise.
    """
    spans = [{"type": "span", "text": code, "weight": "bold", "color": STAFF_UI["navy"]}]
    if code in names:
        spans.append({"type": "span", "text": f" {names[code]}"})
    if low:
        spans.append({"type": "span", "text": " ？", "weight": "bold", "color": SLOT_UI["now"]})
    shown = "".join(span["text"] for span in spans)
    person = {"type": "text", "text": shown, "size": "sm"}
    if not plain:
        person["contents"] = spans
    if len(shown) > 7:
        person["adjustMode"] = "shrink-to-fit"
    return person


def work_card(roster: dict, result: dict, names: dict[str, str], work: str, *, threshold: float = .75,
              has_image: bool = False, now: datetime | None = None, later=()) -> dict:
    """Every staffed hour of one work on one saved roster: a row per hour, the hour as a pill and its people two to
    a line (code in bold, then the name; 「？」 where the cell is unsure). On today's roster the current hour
    (``now``, Taiwan time) is framed and says 「現在」. ``has_image``: the work's crop of the photo follows.

    A row's people sit in cells rather than one wrapped line, which could break a name in two.
    """
    now = now or taipei_now()
    rows: dict[int, dict[str, bool]] = {}
    first = None
    for person in result.get("people", []):
        for slot in person.get("slots", []):
            if work_name(slot) == work:
                code = person["code"]
                people = rows.setdefault(slot["row"], {})
                people[code] = people.get(code, False) or slot.get("confidence", 0) < threshold
                first = slot["column"] if first is None else min(first, slot["column"])
    day = roster_day(roster)
    date_text = f"{day_label(day)}班表" if day else "日期未辨識"
    total = len({code for people in rows.values() for code in people})
    area = work_area(first) if first is not None else "other"
    tint = WORK_GROUPS[area][1]
    current = now.hour - 1 if day == now.date() else None   # row r is (r + 1)–(r + 2) o'clock
    order = {code: index for index, code in enumerate(ALLOWED_CODES)}

    def hour_lines(plain=False):
        lines = []
        for row in sorted(rows):
            people = rows[row]
            cells = [_work_person(code, names, people[code], plain)
                     for code in sorted(people, key=lambda c: order.get(c, 999))]
            hour = [{"type": "text", "text": hour_label(row), "size": "sm", "weight": "bold", "align": "center",
                     "color": STAFF_UI["ink"]}]
            crew = [{"type": "box", "layout": "horizontal",   # a filler keeps a lone last one at its width
                     "contents": cells[start:start + WORK_PEOPLE]
                     + [{"type": "filler"}] * (start + WORK_PEOPLE - len(cells))}
                    for start in range(0, len(cells), WORK_PEOPLE)]
            line = {"type": "box", "layout": "horizontal", "spacing": "md", "paddingAll": "6px",
                    "alignItems": "center",
                    "contents": [{"type": "box", "layout": "vertical", "flex": 0, "width": "58px", "paddingAll": "5px",
                                  "cornerRadius": "8px", "backgroundColor": tint, "contents": hour},
                                 {"type": "box", "layout": "vertical", "contents": crew}]}
            if row == current:
                hour.insert(0, {"type": "text", "text": "現在", "size": "xxs", "weight": "bold", "align": "center",
                                "color": SLOT_UI["now"]})
                line.update(paddingAll="4px", borderWidth="2px", borderColor=SLOT_UI["now"], cornerRadius="10px")
            lines.append(line)
        return lines

    alt = [f"{date_text}｜{work}｜整天共 {total} 人"]
    for row in sorted(rows):
        people = rows[row]
        alt.append(f"{hour_label(row)} " + "、".join(_work_person(code, names, people[code], True)["text"]
                                                    for code in sorted(people, key=lambda c: order.get(c, 999))))
    notes = [f"⚠️ {result['duty_recheck_error']}"] if result.get("duty_recheck_error") else []
    if current in rows:
        notes.append("⏰ 框起來的是現在這個小時")
    if any(low for people in rows.values() for low in people.values()):
        notes.append("❓ 標「？」的格子辨識信心較低，請對照原圖")
    working = {code for people in rows.values() for code in people}
    nobody, note = nobody_note(working, result, names, later)
    if working - names.keys() - nobody.keys():
        notes.append("🔤 只有代號：姓名未辨識")
    if note:
        notes.append(note)
    if has_image:
        notes.append("📷 下方為這項工作的原圖，可點開放大")
    pills = [_day_pill(day, now), _glass_pill(f"👥 整天 {total} 人")]

    def bubble(hours, part=""):
        header = _hero(f"{AREA_EMOJI.get(area, '🧩')} {work}{part}", "整天的上班名單", [_pill_row(pills)])
        if hours:
            body = [_ruled_panel(hours)]
        else:
            body = [{"type": "box", "layout": "vertical", "paddingAll": "18px", "cornerRadius": "14px",
                     "backgroundColor": STAFF_UI["white"], "contents": [
                {"type": "text", "text": "🍃", "size": "xxl", "align": "center"},
                {"type": "text", "text": "這張班表沒有辨識到這項工作；請換一項工作或日期。", "size": "sm", "wrap": True,
                 "align": "center", "color": STAFF_UI["ink"], "margin": "sm"}]}]
        return _list_bubble(header, [*body, _notes(notes)] if notes else body)

    lines = hour_lines()
    contents = bubble(lines)
    if flex_size(contents) > FLEX_BUBBLE_LIMIT:   # a wide work busy all day: the codes give up their bold to fit
        lines = hour_lines(plain=True)
        contents = bubble(lines)
    if flex_size(contents) > FLEX_BUBBLE_LIMIT and len(lines) > 1:   # still too big: the hours over a few bubbles
        for count in range(2, len(lines) + 1):                         # the hours over as few bubbles as fit, evenly
            size = -(-len(lines) // count)
            pages = [lines[start:start + size] for start in range(0, len(lines), size)]
            bubbles = [bubble(page, f" {number}/{len(pages)}") for number, page in enumerate(pages, 1)]
            if all(flex_size(part) <= FLEX_BUBBLE_LIMIT for part in bubbles):
                break
        contents = {"type": "carousel", "contents": bubbles}
    return {"type": "flex", "altText": "｜".join(alt)[:400], "contents": contents}


def work_span(result: dict, work: str) -> tuple[list[int], range] | None:
    """The roster columns one work is printed in, and its hours from the first to the last staffed one.

    What the photo crop under 查看工作 shows: hours outside that span would only add empty rows to a
    picture that is already tall and narrow.
    """
    slots = [slot for person in result.get("people", []) for slot in person.get("slots", [])
             if work_name(slot) == work and FIRST_ROW <= slot["row"] <= LAST_ROW]
    if not slots:
        return None
    rows = [slot["row"] for slot in slots]
    return sorted({int(slot["column"]) for slot in slots}), range(min(rows), max(rows) + 1)


STAFF_COLUMNS = 3    # codes per row on 「勤務人員」
LEAVE_TAGS = {"rest": "💤", "leave": "📝"}   # after the name of someone the leave table lists: 輪休, 請假
STAFF_KINDS = (("🔢 數字代號", str.isdigit), ("🔤 英文代號", lambda c: c.isascii() and c.isalpha()),
               ("🎖️ 中文代號", lambda c: not c.isascii()))


def leave_tags(leave: dict) -> dict[str, str]:
    """Code → 💤 for 輪休 or 📝 for 請假, from what the roster's leave table printed (nothing when not read)."""
    if not leave.get("available"):
        return {}
    tags = {e.get("code"): LEAVE_TAGS["leave"] for e in leave.get("leave", []) if e.get("code")}
    tags.update((code, LEAVE_TAGS["rest"]) for code in leave.get("rest", []))
    return tags


def _code_cell(code: str, name: str | None, tag: str | None = None, *, missing: str = " —") -> dict:
    """「72 林小明」 with the code in bold; without a name, ``missing`` in faint grey (a quiet 「—」 rather than a
    loud phrase repeated down the list). ``tag``: 💤 or 📝 after the name.

    Lean on purpose: up to 118 codes share one bubble, so a cell is one text, without a box or a colour of its own.
    """
    spans = [{"type": "span", "text": code, "weight": "bold", "color": STAFF_UI["navy"]},
             {"type": "span", "text": f" {name}"} if name else {"type": "span", "text": missing, "color": STAFF_UI["faint"]}]
    if not name and len(missing) > 2:   # words, not a dash: a size smaller, so they stay in the cell
        spans[1]["size"] = "xs"
    if tag:
        spans.append({"type": "span", "text": f" {tag}", "size": "xxs"})
    shown = "".join(span["text"] for span in spans)
    cell = {"type": "text", "text": shown, "size": "sm", "contents": spans}
    if len(name or missing.strip()) + 2 * bool(tag) > 4:   # wider than a third of a small phone
        cell["adjustMode"] = "shrink-to-fit"
    return cell


def _code_panel(cells: list[dict], columns: int = 3) -> dict:
    """``cells`` in rows of ``columns`` on a white panel, every other row shaded so the eye keeps to its line."""
    rows = _rows(cells, columns, "xs")
    for index, row in enumerate(rows):
        del row["margin"]
        row.update(paddingAll="5px", cornerRadius="8px")
        if index % 2 == 0:
            row["backgroundColor"] = STAFF_UI["chip"]
    return {"type": "box", "layout": "vertical", "margin": "sm", "paddingAll": "6px", "cornerRadius": "12px",
            "backgroundColor": STAFF_UI["white"], "contents": rows}


def staff_card(roster: dict, result: dict, names: dict[str, str], *, has_image=False, show_original=False,
               now: datetime | None = None, later=()) -> dict:
    """「勤務人員」 on one card, nothing to swipe: every code on this roster under 數字／英文／中文代號, three to a row,
    the code in bold and then its name (a faint 「—」 when the name table gave none; 「空白」, 「沒設定」 or 「表上沒有」
    when nobody has that code that day, ``unnamed_codes``). Those on 輪休 or 請假 are listed too, tagged 💤 or 📝.
    ``has_image``: the crop of the name table follows the card.

    Never treat the directory's order as a daily numeric code assignment. Only
    matched names, observed duty codes and explicit leave entries form the list.

    A day of the team (about 80 codes, some on leave) comes to about 20 KB, and all 118 codes with four-character
    names still fit one bubble (28 KB). Only beyond that, with long names and everyone tagged, do the 英文／中文
    sections move to a second bubble.
    """
    now = now or taipei_now()
    present = set(names) | {p['code'] for p in result.get('people', [])}
    leave = result.get('leave_table', {})
    if leave.get('available'):
        present.update(leave.get('rest', []))
        present.update(e.get('code') for e in leave.get('leave', []))
    ordered = [code for code in ALLOWED_CODES if code in present]
    tags = leave_tags(leave)
    day = roster_day(roster)
    date_text = f'{day_label(day)}班表' if day else '日期未辨識'
    matched = sum(c in names for c in ordered)
    nobody, nobody_text = nobody_note(ordered, result, names, later)
    sections = []
    for label, kind in STAFF_KINDS:
        codes = [code for code in ordered if kind(code)]
        if codes:
            missing = sum(code not in names and code not in nobody for code in codes)
            side = f"{len(codes)} 人" + (f"・{missing} 人未配對" if missing else "")
            cells = [_code_cell(code, names.get(code), tags.get(code),
                                missing=NOBODY_CELLS.get(nobody.get(code), " —")) for code in codes]
            sections.append([_section(label, side), _code_panel(cells, STAFF_COLUMNS)])
    if not sections:
        sections = [[{"type": "box", "layout": "vertical", "paddingAll": "18px", "cornerRadius": "14px",
                      "backgroundColor": STAFF_UI["white"], "contents": [
            {"type": "text", "text": "🔍", "size": "xxl", "align": "center"},
            {"type": "text", "text": "姓名對照尚未辨識，" + _photo_hint(has_image, show_original), "size": "sm",
             "wrap": True, "align": "center", "color": STAFF_UI["ink"], "margin": "sm"}]}]]
    notes = []
    legend = [f"{LEAVE_TAGS[kind]} {word}" for kind, word in (("rest", "輪休"), ("leave", "請假"))
              if LEAVE_TAGS[kind] in tags.values()]
    if legend:
        notes.append("　".join(legend) + "（班表右下角的記載）")
    unmatched = len(ordered) - matched - len(nobody)
    if nobody_text:
        notes.append(nobody_text)
    if unmatched:
        notes.append(f"— 姓名未辨識 {unmatched} 人，" + _photo_hint(has_image, show_original))
    elif has_image:
        notes.append("📷 下方為姓名／代號對照原圖，可點開放大")
    footer = original_roster_link(roster) if show_original else None
    pills = [_day_pill(day, now), _glass_pill(f"👥 共 {len(ordered)} 人")]   # two short pills fit a small phone

    def bubble(parts, title="👥 勤務人員"):
        body = [part for section in parts for part in section]
        return _list_bubble(_hero(title, f"代號與姓名對照｜姓名已配對 {matched} 人", [_pill_row(pills)]),
                            [*body, _notes(notes)] if notes else body, footer)

    bubbles = [bubble(sections)]
    if len(sections) > 1 and flex_size(bubbles[0]) > FLEX_BUBBLE_LIMIT:
        bubbles = [bubble(sections[:1], "👥 勤務人員 1/2"), bubble(sections[1:], "👥 勤務人員 2/2")]
    alt = f'{date_text}｜勤務人員姓名／代號對照｜姓名已配對 {matched} 人'
    return {'type': 'flex', 'altText': alt,
            'contents': bubbles[0] if len(bubbles) == 1 else {'type': 'carousel', 'contents': bubbles}}


def _hour_chip(roster: dict, row: int, area: str, count: int, top: int, *, now: bool = False,
               past: bool = False) -> dict:
    """One hour of one area: the hour and how many people are on duty, a shade darker per quarter of the area's
    busiest hour (``top``). Tapping it lists them, with the postback of the old 國際／國內 buttons.

    Every chip has the same height, so the current hour's extra 「現在」 line does not stretch its row. An hour
    already over keeps its shade, faded, and its words turn slate: still readable, no longer in the way.
    """
    label = f"{hour_label(row)} {AREAS[area][0]}"
    ink = SLOT_UI["past"] if past else STAFF_UI["ink"]
    contents = [{"type": "text", "text": hour_label(row), "size": "sm", "weight": "bold", "align": "center",
                 "color": ink, "adjustMode": "shrink-to-fit"},
                {"type": "text", "text": f"{count} 人", "size": "xxs", "align": "center",
                 "color": ink if count else STAFF_UI["faint"]}]
    if now:
        contents.insert(0, {"type": "text", "text": "現在", "size": "xxs", "weight": "bold", "align": "center",
                            "color": SLOT_UI["now"]})
    chip = {"type": "box", "layout": "vertical", "flex": 1, "height": "54px", "justifyContent": "center",
            "cornerRadius": "10px", "contents": contents, "action": {
                "type": "postback", "label": label, "data": f"area|{roster['run_id']}|{row}|{area}",
                "displayText": label}}
    shades = SLOT_UI[area]
    if count:
        chip["backgroundColor"] = shades[-(-len(shades) * count // top) - 1] + (SLOT_UI["fade"] if past else "")
    else:
        chip.update(backgroundColor=STAFF_UI["white"], borderWidth="1px", borderColor=STAFF_UI["line"])
    if now:
        chip.update(borderWidth="2px", borderColor=SLOT_UI["now"])
    return chip


def _slot_legend(*, ringed: bool, greyed: bool) -> dict:
    """What the shades, the ring and the slate hours mean, so none of them rests on colour alone."""
    def key(marks: list, words: str) -> dict:
        return {"type": "box", "layout": "horizontal", "spacing": "xs", "alignItems": "center", "flex": 0,
                "contents": [*marks, {"type": "text", "text": words, "size": "xxs", "color": STAFF_UI["muted"],
                                      "flex": 0}]}

    def ramp(area: str) -> dict:   # the area's shades as one strip, light (few) to dark (many)
        shades = SLOT_UI[area]
        return _bar(10, shades[1], width="56px", background={
            "type": "linearGradient", "angle": "90deg", "startColor": shades[0], "endColor": shades[-1]})

    def swatch(**border) -> dict:
        return _bar(12, STAFF_UI["white"], width="12px", **border)

    marks = [key([swatch(borderWidth="1px", borderColor=STAFF_UI["line"])], "0 人")]
    if ringed:
        marks.append(key([swatch(borderWidth="2px", borderColor=SLOT_UI["now"])], "現在"))
    if greyed:
        marks.append(key([], "灰字＝已經過了"))
    return {"type": "box", "layout": "vertical", "margin": "xl", "paddingAll": "12px", "spacing": "sm",
            "cornerRadius": "12px", "backgroundColor": STAFF_UI["white"], "contents": [
                {"type": "text", "text": "🎨 顏色越深，那個小時人越多", "size": "xs", "weight": "bold",
                 "color": STAFF_UI["ink"]},
                {"type": "box", "layout": "horizontal", "spacing": "lg", "contents": [
                    key([ramp(area)], f"{AREA_EMOJI[area]} {AREAS[area][0]}") for area in AREAS]},
                {"type": "box", "layout": "horizontal", "spacing": "lg", "contents": marks}]}


def slot_picker(roster: dict, result: dict, *, now: datetime | None = None) -> dict:
    """「查看時段」 on one card, nothing to swipe: a chip for every hour of 國際 and of 國內 with how many people
    are on duty, darker the more there are. One tap lists them (``area|run|row|area``, the postback of the old
    國際／國內 buttons, so cards sent before keep working).

    ``now`` (Taiwan time) rings the current hour and greys the hours already over when the roster is that day's.
    """
    now = now or taipei_now()
    day = roster_day(roster)
    date_text = f"{day_label(day)}班表" if day else "班表日期未辨識"
    current = now.hour - 1 if day == now.date() else None   # row r is (r + 1)–(r + 2) o'clock
    rows = range(FIRST_ROW, LAST_ROW + 1)
    ringed = current in rows
    body, peaks, at_now = [], [], []
    for area, (label, _) in AREAS.items():
        counts = slot_counts(result, area)
        top = max(counts.get(row, 0) for row in rows)
        peak = next(row for row in rows if counts.get(row, 0) == top)
        chips = [_hour_chip(roster, row, area, counts.get(row, 0), top, now=row == current,
                            past=current is not None and row < current) for row in rows]
        side = f"最多 {top} 人・{hour_label(peak)}" if top else "沒有辨識到人員"
        body += [_section(f"{AREA_EMOJI[area]} {label}線", side), *_rows(chips, SLOT_COLUMNS)]
        peaks.append(f"{label}最多 {top} 人（{hour_label(peak)}）" if top else f"{label}沒有辨識到人員")
        at_now.append(f"{label} {counts.get(current, 0)} 人")
    body[0] = {**body[0], "margin": "none"}
    body += [_slot_legend(ringed=ringed, greyed=ringed and current > FIRST_ROW),
             {"type": "text", "text": "💡 也可以直接輸入「10-11 國際」或「7 國內」。", "size": "xxs", "wrap": True,
              "color": STAFF_UI["muted"], "margin": "md"}]
    relative = {-1: "・昨天", 0: "・今天", 1: "・明天"}.get((day - now.date()).days, "") if day else ""
    pills = [_glass_pill(f"📅 {day_label(day) + relative if day else '班表日期未辨識'}")]
    if ringed:
        pills.append(_glass_pill(f"⏰ 現在 {hour_label(current)}"))
    header = _hero("🕐 查看時段", "點一格，看那個小時誰在哪裡",
                   [{"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": pills}])
    alt = [date_text, "點一格看那個小時誰在哪裡", *peaks]
    if ringed:
        alt.append(f"現在 {hour_label(current)}：" + "、".join(at_now))
    return {"type": "flex", "altText": "｜".join(alt)[:400], "contents": {
        "type": "bubble", "size": "giga", "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}}


def area_question(roster: dict, row: int) -> dict:
    """Quick-reply buttons 國際／國內 for one hour."""
    items = [{"type": "action", "action": {"type": "postback", "label": label,
                                           "data": f"area|{roster['run_id']}|{row}|{key}",
                                           "displayText": f"{hour_label(row)} {label}"}}
             for key, (label, _) in AREAS.items()]
    day = roster_day(roster)
    return {**text(f"{day_label(day) + '班表' if day else '（日期未辨識）'}\n"
                  f"{hour_label(row)}：要看國際還是國內？"), "quickReply": {"items": items}}


LEAVE_SECTIONS = {"rest": "💤 輪休人員", "leave": "📝 請假人員"}
LEAVE_KINDS = (   # the tint of a kind's pill on 「休假人員」, by what kind of absence it is; the word says it all
    (("休假", "補休", "補半", "特休", "年休", "榮譽假"), "#E9F6EC"),
    (("公假", "受訓", "災防假"), "#E6EFFB"),
    (("退伍", "停職", "調職"), "#F0ECFB"))
LEAVE_KIND_OTHER = "#FDECEE"   # 事假, 病假, 喪假 and the rest


def _unread_chip(text: str) -> dict:
    """One muted line for everything the photo kept from the reader, in place of a 「代號？」 row each."""
    return {"type": "box", "layout": "horizontal", "margin": "sm", "paddingAll": "8px", "cornerRadius": "10px",
            "borderWidth": "1px", "borderColor": STAFF_UI["line"], "backgroundColor": STAFF_UI["white"], "contents": [
                {"type": "text", "text": text, "size": "xs", "wrap": True, "color": STAFF_UI["muted"]}]}


def _leave_row(code: str | None, name: str | None, kind: str | None, reason: str | None) -> dict:
    """One 請假 entry, for ``_ruled_panel``: the code as a pill, the name (and the reason under it), the kind of
    leave as a pill. A code or a kind not read is a faint 「—」／「假別未辨識」, a name not read 「（未辨識）」."""
    who = {"type": "text", "text": name or "（未辨識）", "size": "sm", "weight": "bold" if name else "regular",
           "color": STAFF_UI["ink"] if name else STAFF_UI["faint"]}
    if reason:
        who = {"type": "box", "layout": "vertical", "contents": [who, {"type": "text", "text": reason, "size": "xxs",
                                                                      "wrap": True, "color": STAFF_UI["muted"]}]}
    if kind:
        tint = next((tint for kinds, tint in LEAVE_KINDS if kind in kinds), LEAVE_KIND_OTHER)
        what = {"type": "box", "layout": "vertical", "flex": 0, "paddingAll": "3px", "paddingStart": "10px",
                "paddingEnd": "10px", "cornerRadius": "20px", "backgroundColor": tint,
                "contents": [{"type": "text", "text": kind, "size": "xs", "weight": "bold", "color": STAFF_UI["ink"]}]}
    else:
        what = {"type": "text", "text": "假別未辨識", "size": "xxs", "flex": 0, "color": STAFF_UI["faint"]}
    return {"type": "box", "layout": "horizontal", "spacing": "md", "paddingAll": "6px", "alignItems": "center",
            "contents": [
                {"type": "box", "layout": "vertical", "flex": 0, "width": "36px", "paddingAll": "4px",
                 "cornerRadius": "8px", "backgroundColor": STAFF_UI["code"] if code else STAFF_UI["chip"], "contents": [
                     {"type": "text", "text": code or "—", "size": "sm", "weight": "bold", "align": "center",
                      "color": STAFF_UI["navy"] if code else STAFF_UI["faint"]}]},
                who, what]}


def _ruled_panel(lines: list[dict]) -> dict:
    """``lines`` on one white panel with a thin rule between them: lighter than a card each, and as easy to read."""
    ruled = lines[:1]
    for line in lines[1:]:
        ruled += [{"type": "separator", "color": STAFF_UI["chip"]}, line]
    return {"type": "box", "layout": "vertical", "margin": "sm", "paddingAll": "6px", "cornerRadius": "14px",
            "backgroundColor": STAFF_UI["white"], "contents": ruled}


def leave_card(roster: dict, data: dict, names: dict[str, str], directory, *, section=None,
               show_original=False, has_image=False, now: datetime | None = None) -> dict:
    """「休假人員」 on one card, nothing to swipe: 💤 輪休人員 three to a row, sorted by code, and 📝 請假人員 a row
    each with the kind of leave (and the reason, when the table gives one). ``section`` (「輪休人員」「請假人員」,
    ``leave|run|rest`` / ``leave|run|leave``) shows that part alone. ``has_image``: the leave table's crop follows.

    Only what the leave table says: nobody is taken for on leave because they have no duty. What it could not read
    stays out of the way: a name not read is a muted 「（未辨識）」 after its code, and cells whose code was not read
    are counted in one line that points to the photo, instead of a 「代號？」 row each. The printed left／middle／
    right columns are not kept: the grid is by code.
    """
    now = now or taipei_now()
    day = roster_day(roster)
    title = f"{day_label(day)}班表" if day else "（日期未辨識）"
    shown = ("rest", "leave") if section is None else (section,)
    hint = _photo_hint(has_image, show_original)
    order = {code: index for index, code in enumerate(ALLOWED_CODES)}
    body, pills, alt = [], [], []
    if not data.get("available"):
        body.append({"type": "box", "layout": "vertical", "paddingAll": "18px", "cornerRadius": "14px",
                     "backgroundColor": STAFF_UI["white"], "contents": [
            {"type": "text", "text": "🍃", "size": "xxl", "align": "center"},
            {"type": "text", "text": "休假區塊尚未辨識", "size": "md", "weight": "bold", "align": "center",
             "color": STAFF_UI["ink"], "margin": "sm"},
            {"type": "text", "text": f"這張班表的輪休、請假記載沒有讀出來，{hint}。", "size": "sm", "wrap": True,
             "align": "center", "color": STAFF_UI["muted"], "margin": "sm"}]})
        alt.append("休假區塊尚未辨識")
    if data.get("available") and "rest" in shown:
        rest = sorted(dict.fromkeys(data.get("rest", [])), key=lambda c: order.get(c, 999))
        unread = data.get("unreadable_rest")
        if unread is None:   # older readings only kept the 「?」 cells in place
            unread = sum(cell == "?" for row in data.get("rest_rows") or [] for cell in row)
        cells = [_code_cell(code, names.get(code), missing="（未辨識）") for code in rest]
        if section is None:   # alone, the band already says what this is and how many
            body.append(_section(LEAVE_SECTIONS["rest"], f"{len(rest)} 人"))
        body.append(_code_panel(cells, STAFF_COLUMNS) if rest else _unread_chip("沒有讀到輪休的代號。"))
        if unread:
            body.append(_unread_chip(f"🔍 另有 {unread} 格看不清楚，{hint}"))
        pills.append(_glass_pill(f"💤 輪休 {len(rest)} 人"))
        alt.append(f"輪休 {len(rest)} 人：" + "、".join(cell["text"] for cell in cells))
    if data.get("available") and "leave" in shown:
        rows, unread = [], 0
        for entry in data.get("leave", []):
            code = entry.get("code")
            fallback = match_names({code or "?": entry.get("name", [])}, directory).get(code or "?")
            name = names.get(code) or fallback
            if not code and not name:
                unread += 1
                continue
            rows.append((order.get(code, 999), code, name, entry.get("kind"), entry.get("reason")))
        rows.sort(key=lambda item: item[0])
        if section is None:
            body.append(_section(LEAVE_SECTIONS["leave"], f"{len(rows)} 人"))
        if rows:
            body.append(_ruled_panel([_leave_row(*row[1:]) for row in rows]))
        elif not unread:
            body.append(_unread_chip("沒有讀到請假的記載。"))
        if unread:
            body.append(_unread_chip(f"🔍 另有 {unread} 筆看不清楚，{hint}"))
        pills.append(_glass_pill(f"📝 請假 {len(rows)} 人"))
        alt.append(f"請假 {len(rows)} 人：" + "、".join(
            " ".join(part for part in (code, name or "（未辨識）", kind) if part) for _, code, name, kind, _ in rows))
    notes = []
    if data.get("available"):
        notes.append("📋 依班表右下角的記載；沒寫的人不算休假")
        if has_image:
            notes.append("📷 下方為休假區塊原圖，可點開放大")
    heading = LEAVE_SECTIONS.get(section, "🌴 休假人員")
    header = _hero(heading, title + _relative_day(day, now), [_pill_row(pills)] if pills else None)
    footer = original_roster_link(roster) if show_original else None
    label = {"rest": "輪休人員", "leave": "請假人員"}.get(section, "輪休與請假人員")
    contents = _list_bubble(header, [*body, _notes(notes)] if notes else body, footer)
    if section is None and flex_size(contents) > FLEX_BUBBLE_LIMIT:   # both tables full of long words: one each
        contents = {"type": "carousel", "contents": [
            leave_card(roster, data, names, directory, section=part, show_original=show_original,
                       has_image=has_image, now=now)["contents"] for part in shown]}
    return {"type": "flex", "altText": "｜".join([title, label, *alt])[:400], "contents": contents}


# ---------------------------------------------------------------- 人流預測
FLOW_COMMANDS = ("人流預測", "人流", "人潮預測", "旅客預測", "忙不忙")
# Pill background and text. Only 超忙 is solid, so the day's peak is what stands out on the card.
FLOW_COLORS = {passenger_flow.VERY_BUSY: ("#C62828", "#FFFFFF"),
               passenger_flow.BUSY: ("#FDBA74", "#7C2D12"),
               passenger_flow.NORMAL: ("#EEF2F6", "#334155"),
               passenger_flow.QUIET: ("#E3F4E8", "#1E6B3A"),
               passenger_flow.VERY_QUIET: ("#BDE3C8", "#14532D")}
NO_FLOW_HINT = "群組有人貼出當天的航班人數訊息時，我會自動記下來；也可以把那則訊息直接貼給我。"


def flow_source_label(record: dict, user_id: str | None = None) -> str:
    """「群組 10/6 08:12 貼的航班人數」: which message a forecast was made from."""
    when = datetime.fromtimestamp(record.get("sent_at") or 0, TAIPEI)
    where = "你私訊" if record.get("chat_id") == user_id else "群組"
    return f"{where} {when.month}/{when.day} {when:%H:%M} 貼的航班人數"


FLOW_WAYS = {"dep": {"intl": "出境", "dom": "離站"}, "arr": {"intl": "入境", "dom": "到站"}}


def flow_totals(flow, direction: str = "dep") -> list[str]:
    """「國際出境 3,924 人」, 「國內離站 3,412 人」: the passengers a 航班人數 message lists, one entry per line listed."""
    return [f"{label}{FLOW_WAYS[direction][area]} {flow.total(area, direction)[1]:,} 人"
            for area, (label, _) in AREAS.items() if flow.series.get((area, direction))]


def flow_card(day: date, flow, graded: dict, *, source: str = "", has_roster: bool = False,
              now: datetime | None = None) -> dict:
    """Every hour of one day with a 超忙／忙／正常／閒／超閒 pill for 國際 and 國內.

    Only grades are shown: the estimated head counts behind them cannot be verified.
    ``now`` (Taiwan time) frames the current hour when ``day`` is today.
    """
    date_text = day_label(day)
    hours = passenger_flow.shown_hours(graded)
    current = now.hour if now is not None and now.date() == day else None
    staffed = {area: any(item.staff is not None for item in graded[area].values()) for area in AREAS}
    busiest = {}       # area → (label, the busiest hours) for the summary rows and the notification text
    for area, (label, _) in AREAS.items():
        if all(item.level is None for item in graded[area].values()):
            busiest[area] = (label, "沒有出境資料" if area == "intl" else "沒有離站資料")
        else:
            busiest[area] = (f"{label}最忙", "、".join(
                f"{start:02d}–{end:02d}" for start, end in passenger_flow.busy_ranges(graded[area]))
                or "沒有特別忙的時段")
    totals = flow_totals(flow)
    contents = [
        {"type": "text", "text": "人流預測", "weight": "bold", "size": "sm", "color": "#244F87"},
        {"type": "text", "text": date_text, "weight": "bold", "size": "xl", "color": "#244F87", "margin": "xs"}]
    if totals:
        contents.append({"type": "text", "text": "表定：" + "、".join(totals), "size": "xs",
                         "color": "#555555", "wrap": True, "margin": "xs"})
    alt = f"{date_text}人流預測"
    if not hours:
        contents.append({"type": "text", "text": "這則訊息沒有離站或出境的航班，無法預估安檢線的人流。",
                         "size": "sm", "wrap": True, "margin": "lg"})
    else:
        alt += "｜" + "｜".join(" ".join(busiest[area]) for area in AREAS)
        contents.append({"type": "separator", "margin": "md"})
        for area, color in (("intl", "#285D9F"), ("dom", "#006D66")):
            contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm", "contents": [
                {"type": "text", "text": busiest[area][0], "size": "sm", "weight": "bold",
                 "color": color, "flex": 3},
                {"type": "text", "text": busiest[area][1], "size": "sm", "wrap": True, "flex": 7,
                 "color": "#1B2430"}]})
        contents += [
            {"type": "separator", "margin": "md"},
            {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "md", "paddingAll": "3px",
             "contents": [
                 {"type": "text", "text": "時間", "flex": 2, "size": "sm", "weight": "bold",
                  "align": "center", "color": "#244F87"},
                 {"type": "text", "text": "國際", "flex": 3, "size": "sm", "weight": "bold",
                  "align": "center", "color": "#285D9F"},
                 {"type": "text", "text": "國內", "flex": 3, "size": "sm", "weight": "bold",
                  "align": "center", "color": "#006D66"}]}]

        def pill(item) -> dict:
            box = {"type": "box", "layout": "vertical", "flex": 3, "paddingTop": "5px", "paddingBottom": "5px"}
            if item.level is None:
                return {**box, "contents": [{"type": "text", "text": "—", "size": "sm", "align": "center",
                                             "color": "#AAAAAA"}]}
            background, colour = FLOW_COLORS[item.level]
            return {**box, "cornerRadius": "6px", "backgroundColor": background, "contents": [
                {"type": "text", "text": item.level, "size": "sm", "weight": "bold", "align": "center",
                 "color": colour}]}

        for hour in hours:
            row = {"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "xs", "paddingAll": "3px",
                   "contents": [{"type": "text", "text": hour_label(hour - 1), "size": "sm", "weight": "bold",
                                 "color": "#244F87", "flex": 2, "align": "center", "gravity": "center"},
                                pill(graded["intl"][hour]), pill(graded["dom"][hour])]}
            if hour == current:   # same footprint as the other rows: 2px frame + 1px padding
                row.update(paddingAll="1px", borderWidth="2px", borderColor="#244F87", cornerRadius="8px")
            contents.append(row)
        measure = "忙碌程度＝預估過安檢人數 ÷ 當時一線人數，再和當天最忙的時段比較"
        if has_roster and all(staffed.values()):
            notes = [measure + "。"]
        elif has_roster and any(staffed.values()):
            unread = "、".join(AREAS[area][0] for area in AREAS if not staffed[area])
            notes = [f"{measure}；{unread}的一線人數沒讀到，只看人流。"]
        elif has_roster:
            notes = ["這天班表的一線人數沒讀到，先只看人流，和當天最忙的時段比較。"]
        else:
            notes = ["這天的班表還沒上線，先只看人流，和當天最忙的時段比較；班表上線後會算進一線人數。"]
        notes.append("過安檢的時間是推估的（國際約提前 1–2 小時、國內算在起飛前一小時），沒有實測校正，僅供參考。")
        if current in hours:
            notes.append("框起來的是現在的時段。")
        if source:
            notes.append(f"資料：{source}")
        contents += [{"type": "separator", "margin": "md"},
                     {"type": "text", "text": "\n".join(notes), "size": "xs", "wrap": True,
                      "color": "#666666", "margin": "md"}]
    return {"type": "flex", "altText": alt[:400],
            "contents": {"type": "bubble", "size": "mega", "body": {
                "type": "box", "layout": "vertical", "paddingAll": "16px", "contents": contents}}}


# ---------------------------------------------------------------- feedback for the administrators
# 「意見 …」 (the menu's 意見回饋 button types 「意見 」): everything after the word is the message, as typed
FEEDBACK_PATTERN = re.compile(r"(?:意見回饋|意見|回饋|建議|問題回報|回報問題|回報)[\s:：,，]*(.*)", re.S)
FEEDBACK_LIST = ("意見列表", "意見清單", "回饋列表", "回饋清單")   # administrators only
FEEDBACK_LENGTH = 1000   # characters kept of one message
FEEDBACK_A_DAY = 10      # messages one user may send in 24 hours
FEEDBACK_SHOWN = 10      # messages in 「意見列表」
FEEDBACK_ROOM = 4500     # characters of entries in one digest: under TEXT_LIMIT with its heading and last line
FEEDBACK_HOW = ("想說的話請接在「意見」後面一起送出，例如：\n意見 10/8 的班表日期沒有讀到\n\n"
                "會連同你的姓名交給管理員。")


def feedback_digest(items: list[dict], heading: str = "📮 新的意見回饋", *, limit: int = FEEDBACK_ROOM
                    ) -> tuple[dict | None, list[dict]]:
    """One text message with as many of ``items`` as fit, and which those are (in the order given).

    Each entry says who sent it and when, then the message as it was typed, then what the sender was looking
    at. What does not fit is left out and counted in a last line.
    """
    entries, shown = [], []
    for item in items:
        when = datetime.fromtimestamp(item["created_at"], TAIPEI)
        who = item.get("staff_name") or "未登記姓名"
        if item.get("display_name") and item["display_name"] != item.get("staff_name"):
            who += f"（LINE：{item['display_name']}）"
        entry = f"#{item['id']}・{when.month}/{when.day} {when:%H:%M}・{who}\n{item['body']}"
        if item.get("context"):
            entry += f"\n（當時在看：{item['context']}）"
        if shown and sum(len(e) + 2 for e in entries) + len(entry) > limit:
            break
        entries.append(entry[:limit]); shown.append(item)
    if not shown:
        return None, []
    lines = [f"{heading}（{len(shown)} 則）", *entries]
    if len(shown) < len(items):
        lines.append(f"…還有 {len(items) - len(shown)} 則，輸入「意見列表」查看。")
    return text("\n\n".join(lines)), shown


# How the 航班人數 messages posted in a group were read, for the administrators: a card pushed to each of them
# (``flow_report``); what no push reached comes free with the next reply one of them gets (``flow_digest``)
FLOW_NOTICES_SHOWN = 8   # notices listed one by one in a message; older ones are only counted
FLOW_KEPT = {   # add_flow's answer, as a report's 版本 row and an ✅ entry say it ("new" and "repeat" need no words)
    "replaced": ("🔄", "取代了同一天較早貼的那則"),
    "again": ("🔁", "和同一天原本的資料相同"),
    "older": ("🗂️", "同一天已有更晚貼的一則，預測仍用那則"),
}
FLOW_SKIPPED = {   # why a ⚠️ entry was not kept, and what to do about it
    "arrivals": ("只有到站／入境",
                 "人流預測只看離站／出境：含「離站」或「出境」段落的那則貼到群組就會記下；只有到站的訊息不用理會。"),
    "undated": ("訊息裡沒有日期", "請連同日期那一行（例如 2026年10月06日）重貼到群組。"),
}
FLOW_UNREAD = ("讀不出可用的資料", "請確認訊息完整後重貼到群組。")   # a skip this version does not know
FLOW_REPORT_TITLES = {"ok": "✅ 群組人流資料讀取成功", "skipped": "⚠️ 群組人流資料沒有記下"}


def flow_lines(summary: str | None) -> list[tuple[str, str]]:
    """A notice's ``summary`` (``flow_totals`` joined: 「國際出境 3,924 人、國內離站 3,412 人」) as a report's rows:
    ("✈️ 國際出境", "3,924 人"), ("🏠 國內離站", "3,412 人")."""
    rows = []
    for entry in (summary or "").split("、"):
        label, _, count = entry.partition(" ")
        if count:
            rows.append((f"{AREA_EMOJI['intl' if label.startswith('國際') else 'dom']} {label}", count))
    return rows


def flow_report(item: dict) -> dict:
    """The card pushed to an administrator about one 航班人數 message posted in a group: a notice as
    ``Store.add_flow_notice`` keeps it, banded like ``upload_report`` (green kept, amber not).

    Who posted it and when (Taiwan time) under the title; then the date of the data and the passengers read per line;
    for a kept one whether it replaced an earlier message of that date, repeated it or came after a later one
    (``FLOW_KEPT``), and a footer that opens that date's 人流預測 (``flow|YYYY-MM-DD``, as its 今天／明天 do); for
    one not kept why, and what to do (``FLOW_SKIPPED``).
    """
    kept = item["status"] == "ok"
    when = datetime.fromtimestamp(item.get("sent_at") or item.get("created_at") or 0, TAIPEI)
    who = item.get("poster") or "不明的上傳者"
    day = iso_day(item.get("flow_date"))
    rows: list[tuple[str, str]] = []
    notes: list[str] = []
    if not kept:
        reason, advice = FLOW_SKIPPED.get(item.get("detail"), FLOW_UNREAD)
        rows.append(("原因", reason))
        notes.append(f"👉 {advice}")
    if kept or day:
        rows.append(("📅 資料日期", day_label(day) if day else "日期不明"))
    rows += flow_lines(item.get("summary"))
    if kept and item.get("detail") in FLOW_KEPT:
        icon, words = FLOW_KEPT[item["detail"]]
        rows.append((f"{icon} 版本", words))
    start, end = REPORT_UI["ok" if kept else "failed"]
    title = FLOW_REPORT_TITLES["ok" if kept else "skipped"]
    header = {"type": "box", "layout": "vertical", "paddingAll": "16px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "120deg", "startColor": start, "endColor": end},
              "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "wrap": True,
                            "color": STAFF_UI["white"]},
                           {"type": "text", "text": f"{who}｜{when.month}/{when.day} {when:%H:%M} 傳到群組",
                            "size": "xs", "wrap": True, "color": "#F1F5F9"}]}
    lines = [{"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "sm", "color": STAFF_UI["muted"], "flex": 0},
        {"type": "text", "text": value, "size": "sm", "weight": "bold", "wrap": True, "align": "end",
         "color": STAFF_UI["ink"]}]} for label, value in rows]
    body = [{"type": "box", "layout": "vertical", "paddingAll": "12px", "cornerRadius": "12px",
             "backgroundColor": STAFF_UI["white"], "contents": lines}]
    if notes:
        body.append(_notes(notes))
    bubble = {"type": "bubble", "size": "mega", "header": header,
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                       "contents": body},
              "styles": {"header": {"backgroundColor": start}}}
    if kept and day:   # a row to tap, not a LINE button: those cut a label that does not fit a small phone
        short = f"{day.month}/{day.day}"
        bubble["footer"] = {"type": "box", "layout": "vertical", "paddingTop": "0px", "paddingBottom": "16px",
                            "paddingStart": "16px", "paddingEnd": "16px", "contents": [
                                {"type": "box", "layout": "vertical", "paddingAll": "10px", "cornerRadius": "12px",
                                 "backgroundColor": STAFF_UI["white"], "borderWidth": "1px",
                                 "borderColor": STAFF_UI["line"],
                                 "action": {"type": "postback", "label": f"人流預測 {short}",
                                            "data": f"flow|{day.isoformat()}", "displayText": f"人流預測 {short}"},
                                 "contents": [{"type": "text", "text": f"📊 看 {day_label(day)}的人流預測",
                                               "size": "sm", "weight": "bold", "align": "center", "wrap": True,
                                               "color": STAFF_UI["navy"]}]}]}
        bubble["styles"]["footer"] = {"backgroundColor": STAFF_UI["page"]}
    alt = f"{title}｜{who} {when.month}/{when.day} {when:%H:%M}｜" + "，".join(
        f"{label.split(' ')[-1]} {value}" for label, value in rows)
    return {"type": "flex", "altText": alt[:400], "contents": bubble}


def flow_digest(items: list[dict]) -> tuple[dict | None, list[dict]]:
    """One text message on how the group's 航班人數 messages were read (``Store.undelivered_flow_notices``), and
    the notices it accounts for: all of them. The newest ``FLOW_NOTICES_SHOWN`` are listed oldest first, so the
    newest is last; the older ones are counted in a line above them.

    An entry is ✅ kept, with the date of the data, or ⚠️ not kept and why; then who posted it and when (Taiwan
    time) and what was read (the passengers per line); then whether it replaced an earlier message of that date,
    or what to do about a skipped one.
    """
    if not items:
        return None, []
    shown, older = items[-FLOW_NOTICES_SHOWN:], items[:-FLOW_NOTICES_SHOWN]
    parts = [f"📊 人流資料讀取結果（{len(items)} 則）"]
    if older:
        skipped = sum(1 for item in older if item["status"] != "ok")
        parts.append(f"另有 {len(older)} 則較早的：" + (f"✅ {len(older) - skipped} 則已記下、⚠️ {skipped} 則沒有記下。"
                                                    if skipped else "都已記下 ✅。"))
    for item in shown:
        when = datetime.fromtimestamp(item.get("sent_at") or item["created_at"], TAIPEI)
        day = iso_day(item.get("flow_date"))
        posted = f"{item.get('poster') or '不明的上傳者'}・{when.month}/{when.day} {when:%H:%M} 貼到群組"
        if item["status"] == "ok":
            version = FLOW_KEPT.get(item.get("detail"))
            lines = [f"✅ {day_label(day) if day else '（日期不明）'}的航班人數已記下", posted, item.get("summary"),
                     " ".join(version) if version else None]
        else:
            reason, advice = FLOW_SKIPPED.get(item.get("detail"), FLOW_UNREAD)
            read = "・".join(part for part in (day_label(day) if day else None, item.get("summary")) if part)
            lines = [f"⚠️ 沒有記下：{reason}", posted, read, f"👉 {advice}"]
        parts.append("\n".join(line for line in lines if line))
    return text("\n\n".join(parts)), list(items)


# ---------------------------------------------------------------- 收件匣 (without pushes)
# LINE's free plan has 200 pushed messages a month, a push request counting once per recipient; a reply, with the token
# of the person's own event, is free. With ``Settings.push`` off the bot pushes nothing: what it would have pushed to
# someone is filed for them, a group of messages at a time (``RosterBot._file``), and comes in front of the next reply
# they get (``RosterBot._inbox``), or when they ask (「收件匣」; the administrators' menu button 「辨識回報」 sends it).
MAILBOX = ("收件匣", "辨識回報", "我的訊息", "收件夾")
MAILBOX_KEEP = 30    # waiting groups kept per person; past it the oldest is dropped, with a log line
REPLY_LIMIT = 5      # messages LINE takes in one reply (and in one push request)
STILL_READING = "⏳ 還在辨識，好了之後傳任何字給我（或按「我的班表」）就會看到結果。"
INTERIM_LEAD = 5.0   # seconds: a private photo still being read this long before the reply window closes is answered
#                      with STILL_READING (the reply has to reach LINE in time), and its result goes to the 收件匣
NEWS = {True: "有結果會私訊通知你", False: "有結果之後，你下次傳訊息給我就會看到"}   # what a requester hears, by push
TELL = {True: "會私訊通知對方", False: "對方下次傳訊息給我時就會看到"}          # and the approver
AGAIN = {True: "這次沒有再私訊管理員（剛剛才通知過），管理員傳「姓名申請」就會看到這次的申請。",   # within NAME_PUSH_GAP
         False: "管理員傳「姓名申請」就會看到這次的申請。"}


def refused_reply(exc: Exception) -> bool:
    """Did LINE turn a reply down for what it held (a 400 not about the token), rather than for its reply token (used,
    expired) or a failure on the way? Only then is the same token worth a second try with less in it."""
    words = str(exc)
    return (getattr(exc, "status", None) == 400 or "LINE API 400" in words) and "reply token" not in words.lower()


def mail_left(count: int, *, button: bool = False) -> str:
    """The line under what a reply carried from the 收件匣 when ``count`` groups still wait. ``button``: the person has
    the administrators' menu, whose 「辨識回報」 sends 「收件匣」."""
    return f"📬 還有 {count} 則訊息，傳「收件匣」{'或按選單「辨識回報」' if button else ''}查看。"


# ---------------------------------------------------------------- 人員管理 (幹部)
# 幹部 (``Store.officers``) and administrators edit the staff list from LINE instead of the files in configs/.
# Fixed codes: every Chinese code belongs to a 役男 (alternative-service conscript), so the cards call them
# 役男代號; the English codes A–N are fixed too. Only the numeric codes change from day to day.
STAFF_MENU = ("人員管理", "管理人員", "幹部功能", "幹部選單")
STAFF_LIST = ("人員名冊", "人員列表", "人員清單", "名冊")
STAFF_HELP = ("人員管理說明", "人員管理指令")   # 「所有指令」 is everybody's: the 進階說明 card
BINDING_LIST = ("綁定清單", "綁定名單", "綁定列表", "登記清單", "登記名單", "登記列表")   # which LINE account has which name
# 「解除登記 王小明」: a 幹部 releases the account that has a name. The older words for letting go of one's own name
# (解除, 取消登記…) stay what they were when nothing follows them; 「解除登記」 alone only explains itself.
RELEASE = re.compile(r"(?:解除登記|取消登記|解除綁定|取消綁定)[:：]?\s*(\S+)(?:\s+(\S+))?")
RELEASE_ALONE = ("解除登記",)
RELEASE_ALL = ("全部", "all")
RELEASE_ONLY = "只有幹部和管理員可以解除別人的登記。\n要取消自己的登記，請傳「解除」。"
NAME_REFRESH = 24 * 3600   # seconds between two questions to LINE about one account's display name
OFFICER_LIST = ("幹部名單", "幹部列表", "幹部清單")
FIXED_LIST = ("固定代號", "固定代號列表", "固定代號名單", "固定代號清單", "役男代號", "役男代號列表", "役男代號名單",
              "役男代號清單", "役男", "役男名單", "役男列表", "役男清單", "役男名冊", "英文代號", "代號表")
LOOSE_WORDS = ("役男", "役男名單", "役男列表", "役男清單", "役男名冊", "英文代號", "代號表")   # others fall through
LIST_WORDS = ("列表", "名單", "清單", "名冊", "一覽", "全部")
CODE_SEPARATORS = re.compile(r"[\s,，、;；/:：]+")
QUESTION = re.compile(r"[誰嗎呢哪？?]|什麼|多少")   # 「代號A是誰」 asks who has A; it names nobody
ADD_STAFF = re.compile(r"(?:新增|加入|增加)人員[:：]?\s*(.*)")
REMOVE_STAFF = re.compile(r"(?:刪除|移除|刪掉)人員[:：]?\s*(.*)")
# 「役男 丁 王小明」, 「代號 A 王小明」, also name first; bare 「代號 …」 only when a fixed code follows (_staff_command)
FIXED_CODE = re.compile(r"(役男代號|固定代號|英文代號|役男|代號)[:：]?\s*(.+)")
ADD_OFFICER = re.compile(r"(?:新增|加入|增加)幹部[:：]?\s*(\S*)(?:\s+(\S+))?")
REMOVE_OFFICER = re.compile(r"(?:刪除|移除|取消)幹部[:：]?\s*(\S*)")
CLEAR_WORDS = ("取消", "刪除", "清除", "清空", "移除", "無", "沒有", "空")
OFFICERS_ONLY = ("「人員管理」只有幹部可以使用。\n"
                 "如果你是幹部，請隊上的幹部傳「新增幹部 你的姓名」把你加進來（你要先登記姓名）。")
STAFF_COMMANDS = (
    ("人員管理", "打開人員管理面板：人數一覽和七個方塊；管理員還有「姓名申請」和「意見列表」兩個。"),
    ("人員名冊", "全部同仁（綠色 ✓＝已登記）、固定代號，和自己登記但不在名冊的人。"),
    ("新增人員 王小明 陳大華", "把新同仁加進名冊，一次可以加好幾位，用空白隔開；加進名冊後對方才能登記姓名。"),
    ("刪除人員 王小明", "離隊或調走的同仁從名冊移除；對方有固定代號的話一起清空，之後也看不到班表。"),
    ("固定代號", "役男的中文代號（甲到酉）和英文代號（A 到 N）一覽；點格子就能設定或換人。"),
    ("役男 丁 王小明", "把中文代號「丁」給王小明（不在名冊會自動加入）；原本那位役男會移出名冊。「役男 丁 取消」清空。"),
    ("代號 A 王小明", "英文代號一樣這樣設定；「代號 A 取消」清空。"),
    ("役男 王小明", "只打姓名：點一個空白的格子就把那個代號給他。"),
    ("幹部名單", "目前的幹部和職稱。"),
    ("新增幹部 王小明 副隊長", "給對方幹部權限（對方要先登記姓名），職稱可省略。"),
    ("移除幹部 學姊", "取消幹部權限，用姓名、職稱或 userId 都可以。"),
    ("綁定清單", "每個已登記的 LINE 帳號（LINE 名稱）用的是哪個姓名；同一個姓名有好幾個帳號、或姓名不在名冊的會另外列出。"),
    ("解除登記 王小明", "解除登記這個姓名的 LINE 帳號（換了帳號、被別人登記走）；對方之後要重新登記。"),
    ("姓名申請", "同仁改姓名、或登記別人已登記的姓名，要管理員同意；管理員傳這個會列出等待確認的申請，點同意或不同意。"
                 "一個姓名只能登記在一個 LINE 帳號：同意別人已登記的姓名，就是把姓名轉給申請人，原本的帳號會被解除。"),
    ("意見列表", f"管理員傳這個會看到最近 {FEEDBACK_SHOWN} 則意見回饋（同仁傳「意見 …」送來的）。"),
)
STAFF_UI = {   # navy and teal, as the rest of the bot; tints for the tiles, ink for every word
    "navy": "#1F4E8C", "teal": "#2A8CA0", "ink": "#1B2430", "muted": "#5F6B7A", "faint": "#9AA5B5",
    "line": "#D7DEE8", "page": "#F6F8FB", "white": "#FFFFFF", "glass": "#0B254540",   # dark glass: white stays legible
    "chip": "#EEF2F7", "ok": "#E3F4E8", "check": "#1E7B3A", "warn": "#FFF1DC", "code": "#E6EFFB",
    "tints": ("#E6EFFB", "#E3F4F1", "#E9F6EC", "#FDECEE", "#F0ECFB", "#FFF1DC"),
    # a tile with something waiting (人員管理's 姓名申請): an amber none of the tints is, ringed, its words bold
    "alert": "#FFE0B2", "alert_edge": "#E8892B", "alert_ink": "#9A4A00",
    "letter": "#E9ECFB",   # filled English code cells, a shade apart from the Chinese ones
}
ROSTER_COLUMNS = 4     # names per row on 「人員名冊」
CODE_COLUMNS = 5       # 甲乙丙丁戊 / 己庚辛壬癸 / 子丑寅卯辰 / 巳午未申酉
FLEX_BUBBLE_LIMIT = 28_000     # bytes of compact JSON (what LineTransport sends); LINE refuses a bubble over 30 KB
FLEX_CAROUSEL_LIMIT = 48_000   # and a carousel over 50 KB


def flex_size(node) -> int:
    return len(json.dumps(node, ensure_ascii=False, separators=(",", ":")).encode())


def short_id(user_id: str) -> str:
    return f"{user_id[:5]}…{user_id[-4:]}" if user_id and len(user_id) > 12 else (user_id or "")


def officer_label(officer: dict) -> str:
    """「隊長 林大明」, 「學姊」, 「林大明」, or failing those their LINE name or a shortened user id."""
    parts = [part for part in (officer.get("title"), officer.get("staff_name")) if part]
    if parts:
        return " ".join(parts)
    return f"LINE：{officer['name']}" if officer.get("name") else short_id(officer["user_id"])


def keyboard_action(label: str, data: str, prefix: str) -> dict:
    """Tap → the keyboard opens with ``prefix`` typed in, and the bot answers ``data`` with how to go on (for a
    LINE too old to open the keyboard from a card)."""
    return {"type": "postback", "label": label[:20], "data": data, "inputOption": "openKeyboard", "fillInText": prefix}


def message_action(label: str, value: str | None = None) -> dict:
    return {"type": "message", "label": label[:20], "text": value or label}


def welcome_card(locked: bool = True, gated: bool = False) -> dict:
    """What a newcomer sees on adding the bot: one step, registering their name, and a big tile that does it.

    ``gated``: the 名冊守門員 is on, so the name has to be one on the 名冊 (and a newcomer is added there first)."""
    body = [
        {"type": "text", "text": "第一步：登記你的姓名", "weight": "bold", "size": "xl", "color": "#1B2430"},
        {"type": "text", "text": "只要一次。登記好之後，點下方選單就能看到自己的班表、下個小時和本月統計。",
         "size": "sm", "wrap": True, "color": "#5F6B7A", "margin": "sm"},
        {"type": "box", "layout": "vertical", "margin": "xl", "paddingAll": "16px", "cornerRadius": "14px",
         "backgroundColor": "#1F4E8C", "action": keyboard_action("輸入姓名", "guide|設定姓名", "設定 "), "contents": [
             {"type": "text", "text": "✏️ 點這裡，輸入我的姓名", "weight": "bold", "size": "lg", "align": "center",
              "color": "#FFFFFF"}]},
        {"type": "text", "text": f"會打開鍵盤並帶入「設定 」，接著打你的姓名送出，例如：設定 {EXAMPLE_NAME}",
         "size": "xs", "wrap": True, "color": "#5F6B7A", "margin": "md"},
        {"type": "text", "text": "數字代號每天會換，所以請用姓名登記，不要用代號。", "size": "xs", "wrap": True,
         "color": "#5F6B7A", "margin": "sm"}]
    if gated:
        body.append({"type": "text", "text": f"🗂️ {ROSTER_HELP}", "size": "xs", "wrap": True, "color": "#5F6B7A",
                     "margin": "sm"})
    if locked:   # only while an administrator can approve a change (姓名申請); otherwise a name changes at once
        body.append({"type": "text", "text": "🔒 請登記自己的姓名：登記後要改，需要管理員同意。", "size": "xs",
                     "wrap": True, "color": "#5F6B7A", "margin": "sm"})
    return {"type": "flex", "altText": "歡迎！第一步：輸入「設定 你的姓名」登記姓名（只需一次）",
            "contents": {"type": "bubble", "size": "giga",
                         "header": _hero("👋 歡迎加入", "安檢隊班表小幫手｜從班表照片找出你的勤務"),
                         "body": {"type": "box", "layout": "vertical", "paddingAll": "18px",
                                  "backgroundColor": "#F6F8FB", "contents": body},
                         "styles": {"header": {"backgroundColor": "#1F4E8C"}}}}


# ---------------------------------------------------------------- 名冊守門員 (the roster gate)
# Only someone whose registered name is on the 名冊, a 幹部 or an administrator sees anything of the team in a
# private chat (``RosterBot._admitted``). Anyone else may only do what registering needs (``RosterBot._open_door``):
# the rest is answered with ``gate_card``. No answer to them ever lists or suggests a name on the 名冊.
GATE_POSTBACKS = ("guide|設定姓名", "fill:設定", "fill:意見")   # the tiles and buttons that open 「設定 」／「意見 」
GATE_ALT = "🔒 請先登記名冊上的姓名：輸入「設定 你的姓名」，姓名在隊上的名冊上，就能查看班表和所有功能。"


def gate_card(removed: str | None = None, pending: str | None = None) -> dict:
    """The 名冊守門員's answer to someone not admitted: why there is nothing to see yet, and a big tile that opens the
    keyboard on 「設定 」 (``guide|設定姓名``, the welcome card's own postback).

    ``removed``: the name they registered, no longer on the 名冊 (left the team, or never added); ``pending``: the
    name they asked for, still waiting for an administrator (姓名申請). Nothing else is named on it.
    """
    body = [
        {"type": "text", "text": "班表只給名冊上的同仁看", "weight": "bold", "size": "xl", "wrap": True,
         "color": STAFF_UI["ink"]},
        {"type": "text", "text": "為了保護同仁的班表和個人資料，登記的姓名要在隊上的名冊上，才能查看班表、時段、人員和統計。",
         "size": "sm", "wrap": True, "color": STAFF_UI["muted"], "margin": "sm"}]
    notices = []
    if removed:
        notices.append((f"⚠️ 你登記的姓名「{removed}」目前不在名冊上（可能已經離隊，或名冊上不是這樣寫）。"
                        "如果你還在隊上，請聯絡幹部。", STAFF_UI["warn"]))
    if pending:
        notices.append((f"⏳ 你申請的「{pending}」還在等管理員確認，通過後就能使用。", STAFF_UI["chip"]))
    for words, tint in notices:
        body.append({"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "12px", "cornerRadius": "12px",
                     "backgroundColor": tint, "contents": [
                         {"type": "text", "text": words, "size": "sm", "wrap": True, "color": STAFF_UI["ink"]}]})
    body += [
        {"type": "box", "layout": "vertical", "margin": "xl", "paddingAll": "16px", "cornerRadius": "14px",
         "backgroundColor": STAFF_UI["navy"], "action": keyboard_action("登記名冊上的姓名", "guide|設定姓名", "設定 "),
         "contents": [{"type": "text", "text": "✏️ 登記名冊上的姓名", "weight": "bold", "size": "lg", "align": "center",
                       "color": STAFF_UI["white"]}]},
        {"type": "text", "text": f"會打開鍵盤並帶入「設定 」，接著打你的姓名送出，例如：設定 {EXAMPLE_NAME}",
         "size": "xs", "wrap": True, "color": STAFF_UI["muted"], "margin": "md"},
        {"type": "text", "text": "🆕 新進同仁：請先請幹部把你加入名冊（人員管理 → 新增人員），再登記一次。",
         "size": "xs", "wrap": True, "color": STAFF_UI["muted"], "margin": "sm"}]
    title = "🔒 姓名不在名冊上" if removed else "🔒 請先登記姓名"
    alt = f"🔒 你登記的「{removed}」目前不在名冊上，暫時看不到班表；如果你還在隊上，請聯絡幹部。" if removed else GATE_ALT
    return {"type": "flex", "altText": alt[:400], "contents": {
        "type": "bubble", "size": "giga", "header": _hero(title, "安檢隊班表小幫手｜只給名冊上的同仁使用"),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "18px", "backgroundColor": STAFF_UI["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}}


def _hero(title: str, subtitle: str, extra: list | None = None) -> dict:
    """The navy → teal band at the top of every 人員管理 card."""
    return {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs",
            "background": {"type": "linearGradient", "angle": "120deg", "startColor": STAFF_UI["navy"],
                           "endColor": STAFF_UI["teal"]},
            "contents": [{"type": "text", "text": title, "weight": "bold", "size": "xl", "color": STAFF_UI["white"]},
                         {"type": "text", "text": subtitle, "size": "xs", "wrap": True, "color": "#E4EEF8"},
                         *(extra or [])]}


def _rows(cells: list[dict], columns: int, spacing: str = "sm") -> list[dict]:
    """Cells in rows of ``columns``; the last row is padded with empty space so every cell keeps its width."""
    rows = []
    for start in range(0, len(cells), columns):
        row = cells[start:start + columns]
        row += [{"type": "filler"}] * (columns - len(row))
        rows.append({"type": "box", "layout": "horizontal", "spacing": spacing, "margin": spacing, "contents": row})
    return rows


def _section(title: str, side: str = "") -> dict:
    return {"type": "box", "layout": "horizontal", "margin": "xl", "contents": [
        {"type": "text", "text": title, "weight": "bold", "size": "sm", "color": STAFF_UI["navy"], "flex": 0},
        {"type": "text", "text": side or " ", "size": "xs", "color": STAFF_UI["muted"], "align": "end",
         "gravity": "center"}]}


def action_tile(emoji: str, title: str, hint: str, action: dict, tint: str) -> dict:
    """A tappable tile: an emoji, a short title that always fits, and a hint underneath."""
    return {"type": "box", "layout": "vertical", "flex": 1, "paddingAll": "12px", "spacing": "xs",
            "cornerRadius": "14px", "backgroundColor": tint, "action": action, "contents": [
                {"type": "text", "text": emoji, "size": "lg"},
                {"type": "text", "text": title, "weight": "bold", "size": "md", "color": STAFF_UI["ink"]},
                {"type": "text", "text": hint, "size": "xxs", "color": STAFF_UI["muted"], "wrap": True}]}


def waiting_tile(tile: dict, waiting: bool) -> dict:
    """``tile`` (``action_tile``) marked when something waits behind it: an amber no other tile has, a ring, and its
    hint in bold, so it stands out from the tiles around it and not only by its words."""
    if waiting:
        tile.update(backgroundColor=STAFF_UI["alert"], borderWidth="2px", borderColor=STAFF_UI["alert_edge"])
        tile["contents"][2].update(weight="bold", color=STAFF_UI["alert_ink"])
    return tile


def _stat(value: str, label: str) -> dict:
    return {"type": "box", "layout": "vertical", "flex": 1, "paddingAll": "8px", "cornerRadius": "10px",
            "backgroundColor": STAFF_UI["glass"], "contents": [
                {"type": "text", "text": value, "weight": "bold", "size": "lg", "align": "center",
                 "color": STAFF_UI["white"], "adjustMode": "shrink-to-fit"},
                {"type": "text", "text": label, "size": "xxs", "align": "center", "color": "#E4EEF8",
                 "adjustMode": "shrink-to-fit"}]}


def staff_menu_card(*, names: int, fixed: int, registered: int, unlisted: int, officers: int,
                    admin: bool = False, requests: int = 0, feedback: int = 0) -> dict:
    """「人員管理」: the counts in the band, then a tile for everything a 幹部 does, two in a row.

    ``admin``: for an administrator, two more tiles for what only they do, 「姓名申請」 and 「意見列表」 (their menu
    has no buttons for them), with ``requests`` (name requests waiting for their answer) and ``feedback`` (messages no
    administrator has been shown yet) as their hints. They go in a row of their own, before 「綁定清單」, which ends
    both cards alone."""
    stats = {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "lg", "contents": [
        _stat(str(names), "名冊"), _stat(f"{fixed}/{len(FIXED_CODES)}", "代號"),   # 固定代號: too wide on a small phone
        _stat(str(registered), "已登記"), _stat(str(officers), "幹部")]}
    tints = STAFF_UI["tints"]
    tiles = [
        action_tile("📋", "人員名冊", "看全部姓名與登記狀況", message_action("人員名冊"), tints[0]),
        action_tile("📌", "固定代號", "役男與英文代號", message_action("固定代號"), tints[1]),
        action_tile("➕", "新增人員", "輸入姓名就好", keyboard_action("新增人員", "guide|新增人員", "新增人員 "), tints[2]),
        action_tile("➖", "刪除人員", "離隊或調走的同仁", keyboard_action("刪除人員", "guide|刪除人員", "刪除人員 "), tints[3]),
        action_tile("👑", "幹部名單", "誰可以管理人員", message_action("幹部名單"), tints[4]),
        action_tile("🙋", "新增幹部", "對方要先登記姓名", keyboard_action("新增幹部", "guide|新增幹部", "新增幹部 "), tints[5]),
    ]
    if admin:   # a request waiting turns its tile the warm colour of a notice
        tiles += [
            waiting_tile(action_tile("📮", "姓名申請", f"{requests} 筆等你確認" if requests else "審核改名與登記",
                                     message_action("姓名申請"), tints[0]), bool(requests)),
            action_tile("💬", "意見列表", f"{feedback} 則新的意見" if feedback else "大家的意見回饋",
                        message_action("意見列表"), tints[2]),
        ]
    tiles.append(action_tile("🔗", "綁定清單", "誰用哪個 LINE 登記", message_action("綁定清單"), tints[1]))
    tip = ("💡 名冊用來比對班表上的姓名，也決定誰能登記使用（幹部和管理員除外），改完馬上生效；"
           "已上傳的班表也會照新名冊顯示。")
    if unlisted:
        tip += f"\n🙋 有 {unlisted} 位自己登記、但不在名冊，打開「人員名冊」可以一鍵加入。"
    alt = f"人員管理｜名冊 {names} 位、固定代號 {fixed}/{len(FIXED_CODES)}、已登記 {registered} 位、幹部 {officers} 位"
    if admin and requests:
        alt += f"｜姓名申請 {requests} 筆等你確認"
    return {"type": "flex", "altText": alt, "contents": {"type": "bubble", "size": "giga",
        "header": _hero("👥 人員管理", f"{'管理員' if admin else '幹部'}專用｜點下面的方塊開始", [stats]),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                 "contents": [*_rows(tiles, 2), {"type": "text", "text": tip, "size": "xs", "wrap": True,
                                                  "color": STAFF_UI["muted"], "margin": "lg"},
                              {"type": "text", "text": "📖 人員管理指令", "size": "sm", "weight": "bold", "align": "center",
                               "color": STAFF_UI["navy"], "margin": "lg", "action": message_action("人員管理指令")}]},
        "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}}


def staff_help_text() -> str:
    return "\n".join(["📖 人員管理指令", *(f"\n{command}\n　{explanation}" for command, explanation in STAFF_COMMANDS)])


def code_kind(code: str) -> tuple[str, str, str]:
    """(emoji, what the cards call it, the word that sets it): 役男 for Chinese codes, 代號 for English ones."""
    return ("🎖️", "役男代號", "役男") if code in CHINESE_CODES else ("🔤", "英文代號", "代號")


def claim_action(code: str, name: str) -> dict:
    """One tap gives ``name`` a blank ``code``; should someone have taken it since the card was sent, the bot
    asks before replacing them (``RosterBot._set_fixed_code`` with ``expect_empty``)."""
    word = code_kind(code)[2]
    return {"type": "postback", "label": f"{word} {code}"[:20], "data": f"setcode|{code}|{name}|empty",
            "displayText": f"{word} {code} {name}"}


def code_cell(code: str, name: str | None, *, changed: bool = False, for_name: str | None = None,
              registered=()) -> dict:
    """One fixed code: the code, and the name (blank while nobody has it). Tapping it sets or changes it.

    With ``for_name`` (「役男 王小明」 was typed) a blank cell gives that person the code in one tap; a filled
    cell still only opens the keyboard, so nobody is replaced by a stray tap. A name someone registered with
    the bot is green with ✓.
    """
    word = code_kind(code)[2]
    action = (claim_action(code, for_name) if for_name and not name else
              keyboard_action(f"{word} {code}", f"fixed|{code}", f"{word} {code} "))
    cell = {"type": "box", "layout": "vertical", "flex": 1, "paddingTop": "8px", "paddingBottom": "8px",
            "cornerRadius": "12px", "action": action,
            "contents": [
                {"type": "text", "text": code, "weight": "bold", "size": "lg", "align": "center",
                 "color": STAFF_UI["navy"] if name else STAFF_UI["faint"]},
                {"type": "text", "text": name or "　", "size": "xs", "align": "center", "color": STAFF_UI["ink"],
                 "adjustMode": "shrink-to-fit"}]}
    if name and name in registered:
        cell["contents"][1].update(text=f"{name}✓", weight="bold", color=STAFF_UI["check"])
        cell["backgroundColor"] = STAFF_UI["ok"]
    elif name:
        cell["backgroundColor"] = STAFF_UI["code"] if code in CHINESE_CODES else STAFF_UI["letter"]
    else:
        cell.update(backgroundColor=STAFF_UI["white"], borderWidth="1px", borderColor=STAFF_UI["line"])
    if changed:
        cell.update(borderWidth="2px", borderColor=STAFF_UI["teal"])
    return cell


def code_grid(fixed: dict[str, str], codes=CHINESE_CODES, *, changed: str | None = None,
              for_name: str | None = None, registered=()) -> list[dict]:
    """``codes`` five to a row: 甲–戊, 己–癸, 子–辰, 巳–酉 for the Chinese ones; A–E, F–J, K–N for English."""
    return _rows([code_cell(code, fixed.get(code), changed=code == changed, for_name=for_name, registered=registered)
                  for code in codes], CODE_COLUMNS)


def small_code_grid(fixed: dict[str, str], codes=CHINESE_CODES, registered=()) -> list[dict]:
    """The same codes, small and without taps (the 人員名冊 card has to stay under LINE's size limit); a name
    someone registered with the bot is green with ✓, as in the list above."""
    cells = []
    for code in codes:
        name = fixed.get(code)
        filled = STAFF_UI["code"] if code in CHINESE_CODES else STAFF_UI["letter"]
        label = {"type": "text", "text": name or "　", "size": "xxs", "align": "center", "adjustMode": "shrink-to-fit"}
        if name and name in registered:
            label.update(text=f"{name}✓", weight="bold", color=STAFF_UI["check"])
            filled = STAFF_UI["ok"]
        cells.append({"type": "box", "layout": "vertical", "flex": 1, "paddingAll": "4px", "cornerRadius": "8px",
                      **({"backgroundColor": filled} if name else
                         {"backgroundColor": STAFF_UI["white"], "borderWidth": "1px", "borderColor": STAFF_UI["line"]}),
                      "contents": [
                          {"type": "text", "text": code, "weight": "bold", "size": "sm", "align": "center",
                           "color": STAFF_UI["navy"] if name else STAFF_UI["faint"]}, label]})
    return _rows(cells, CODE_COLUMNS, "xs")


def taken(fixed: dict[str, str], codes) -> str:
    return f"{sum(1 for code in codes if code in fixed)} / {len(codes)}"


def fixed_codes_card(fixed: dict[str, str], *, changed: str | None = None, for_name: str | None = None,
                     registered=()) -> dict:
    """「固定代號」: every Chinese (役男) and English code, filled or blank; tapping one opens the keyboard on
    「役男 丁 」 or 「代號 A 」 (or, with ``for_name``, gives that person a blank code in one tap). Holders who
    registered with the bot are green with ✓."""
    chinese = code_grid(fixed, CHINESE_CODES, changed=changed, for_name=for_name, registered=registered)
    english = code_grid(fixed, ENGLISH_CODES, changed=changed, for_name=for_name, registered=registered)
    body = [{**_section("🎖️ 役男（中文代號）", taken(fixed, CHINESE_CODES)), "margin": "none"}, *chinese,
            _section("🔤 英文代號", taken(fixed, ENGLISH_CODES)), *english,
            {"type": "text", "text": "👆 點格子就能設定或換人；空白的代號目前沒有人，綠色 ✓＝已在機器人登記。\n"
                                     "也可以直接打「役男 丁 王小明」或「代號 A 王小明」，清空打「代號 A 取消」。",
             "size": "xs", "wrap": True, "color": STAFF_UI["muted"], "margin": "lg"}]
    alt = f"固定代號 {sum(1 for code in FIXED_CODES if code in fixed)}/{len(FIXED_CODES)}｜" + "、".join(
        f"{code}{fixed[code]}" for code in FIXED_CODES if code in fixed)
    return {"type": "flex", "altText": alt[:400], "contents": {"type": "bubble", "size": "giga",
        "header": _hero("📌 固定代號", f"每天都一樣的代號｜已設定 {taken(fixed, FIXED_CODES).replace(' ', '')}"),
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}}


def name_rows(names: list[str], registered, *, tint: str | None = None) -> list[dict]:
    """Names four to a row, every other row shaded; a registered name is green and bold with a ✓.

    Plain text cells: there can be a hundred names and the card has to stay under LINE's size limit.
    """
    rows = []
    for start in range(0, len(names), ROSTER_COLUMNS):
        cells = []
        for name in names[start:start + ROSTER_COLUMNS]:
            cell = {"type": "text", "text": name, "size": "sm", "align": "center"}
            if name in registered:
                cell.update(text=f"{name}✓", weight="bold", color=STAFF_UI["check"])
            if len(cell["text"]) > 4:
                cell["adjustMode"] = "shrink-to-fit"
            cells.append(cell)
        cells += [{"type": "text", "text": "　", "size": "sm"}] * (ROSTER_COLUMNS - len(cells))   # keeps the columns
        row = {"type": "box", "layout": "horizontal", "spacing": "sm", "paddingAll": "7px", "cornerRadius": "8px",
               "contents": cells}
        if tint or (start // ROSTER_COLUMNS) % 2 == 0:
            row["backgroundColor"] = tint or STAFF_UI["chip"]
        if tint and start:
            row["margin"] = "xs"
        rows.append(row)
    return rows


def staff_roster_bubbles(names: list[str], fixed: dict[str, str], registered: dict[str, int]) -> list[dict]:
    """「人員名冊」 as bubbles: the names without a code in a grid (registered ones green with ✓), all twenty
    役男代號 (blank where nobody has one), and who registered but is not on the list, with one tap to add them.

    One bubble while it fits under LINE's size limit; beyond that the names go on pages of their own.
    """
    coded = set(fixed.values())
    plain = [name for name in names if name not in coded]
    unlisted = [name for name in registered if name not in names and name not in coded]
    everyone = list(dict.fromkeys([*names, *coded]))
    signed = sum(1 for name in everyone if name in registered)
    head = _hero("📋 人員名冊", f"共 {len(everyone)} 位｜✓ 已在機器人登記 {signed} 位")
    tints = STAFF_UI["tints"]
    codes = [_section("🎖️ 役男（中文代號）", taken(fixed, CHINESE_CODES)),
             *small_code_grid(fixed, CHINESE_CODES, registered),
             _section("🔤 英文代號", taken(fixed, ENGLISH_CODES)), *small_code_grid(fixed, ENGLISH_CODES, registered)]
    others = []
    if unlisted:
        shown = unlisted[:20]
        others += [_section("🙋 自己登記、不在名冊", f"{len(unlisted)} 位"),
                   *name_rows(shown, registered, tint=STAFF_UI["warn"]),
                   {"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "10px", "cornerRadius": "12px",
                    "backgroundColor": STAFF_UI["navy"],
                    "action": message_action("全部加入名冊", "新增人員 " + " ".join(shown)),
                    "contents": [{"type": "text", "text": f"➕ 這 {len(shown)} 位全部加入名冊", "size": "sm",
                                  "weight": "bold", "align": "center", "color": STAFF_UI["white"]}]}]
    tiles = _rows([
        action_tile("➕", "新增人員", "輸入姓名就好", keyboard_action("新增人員", "guide|新增人員", "新增人員 "), tints[2]),
        action_tile("📌", "固定代號", "點格子設定", message_action("固定代號"), tints[1])], 2)
    tiles[0]["margin"] = "xl"
    legend = {"type": "text", "text": "數字代號每天換，看當天的姓名對照表。綠色 ✓＝已在機器人登記；"
                                      "有固定代號的人依代號排在下面（剛清空代號的人也在上面這裡）。", "size": "xxs",
              "color": STAFF_UI["muted"], "margin": "md", "wrap": True}

    def bubble(header, contents):
        return {"type": "bubble", "size": "giga", "header": header,
                "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}},
                "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                         "contents": contents}}

    empty = [{"type": "text", "text": "名冊是空的。", "size": "sm", "color": STAFF_UI["muted"], "margin": "md"}]
    names_part = [_section("👥 沒有固定代號", f"{len(plain)} 位"), *(name_rows(plain, registered) or empty), legend]
    single = bubble(head, [*names_part, *codes, *others, *tiles])
    if flex_size(single) <= FLEX_BUBBLE_LIMIT:
        return [single]
    # Too many names for one bubble: the fixed codes and the buttons first, then pages of names. Should that
    # still be too much for one carousel, the small code grids give way (the 固定代號 tile shows them), and
    # then the pages that do not fit, which the first bubble owns up to.
    per_page = ROSTER_COLUMNS * 15
    slices = [plain[start:start + per_page] for start in range(0, len(plain), per_page)][:11]

    def pages_of(slices):
        return [bubble(_hero(f"📋 名冊 {number}/{len(slices)}", f"沒有固定代號，共 {len(plain)} 位｜綠色 ✓＝已登記"),
                       name_rows(names, registered)) for number, names in enumerate(slices, 1)]

    def swipe(slices, *, with_codes=True):
        hint = f"👉 往左滑看沒有固定代號的 {len(plain)} 位（{len(slices)} 頁）。"
        if not with_codes:
            hint += "\n固定代號請按下面的「固定代號」看。"
        shown = sum(map(len, slices))
        if shown < len(plain):
            hint += f"\n⚠️ 名冊太長，最後 {len(plain) - shown} 位沒有列出。"
        return {"type": "text", "text": hint, "size": "xs", "weight": "bold", "wrap": True, "color": STAFF_UI["navy"]}

    def first_of(slices, with_codes):
        return bubble(head, [swipe(slices, with_codes=with_codes), *(codes if with_codes else []), *others, *tiles])

    with_codes = flex_size(first_of(slices, True)) + sum(map(flex_size, pages_of(slices))) <= FLEX_CAROUSEL_LIMIT
    while slices and flex_size(first_of(slices, with_codes)) + sum(map(flex_size, pages_of(slices))) > FLEX_CAROUSEL_LIMIT:
        slices = slices[:-1]
    return [first_of(slices, with_codes), *pages_of(slices)]


def staff_roster_card(names: list[str], fixed: dict[str, str], registered: dict[str, int]) -> dict:
    bubbles = staff_roster_bubbles(names, fixed, registered)
    everyone = set(names) | set(fixed.values())
    alt = (f"人員名冊｜共 {len(everyone)} 位｜固定代號 {sum(1 for c in FIXED_CODES if c in fixed)} 位｜"
           + "、".join(name for name in names if name not in fixed.values()))
    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return {"type": "flex", "altText": alt[:400], "contents": contents}


def line_label(account: dict) -> str:
    """The LINE display name of an account, or the end of its user id while LINE has not told us."""
    return (account.get("name") or "").strip() or f"LINE 名稱未知 …{account['user_id'][-4:]}"


def _registered_on(account: dict) -> str:
    """「10/8」: the day an account took its name (Taiwan time); 「日期不明」 for rows older than that was kept."""
    if not account.get("updated_at"):
        return "日期不明"
    when = datetime.fromtimestamp(account["updated_at"], TAIPEI)
    return f"{when.month}/{when.day}"


def account_label(account: dict) -> str:
    """「明明（…c8f2）」: the LINE name and the end of the user id, for accounts that must be told apart (two under
    one name may well carry the same LINE name)."""
    return f"{(account.get('name') or '').strip() or 'LINE 名稱未知'}（…{account['user_id'][-4:]}）"


def _binding_row(name: str, display: str, badge: str = "") -> dict:
    """姓名｜LINE 名稱｜職稱. Plain text cells: a hundred of them have to stay under LINE's size limit."""
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm", "contents": [
        {"type": "text", "text": name, "size": "sm", "weight": "bold", "flex": 3},
        {"type": "text", "text": display, "size": "sm", "flex": 5, "wrap": True},
        # always there, so the LINE names line up whether or not a row has a badge (LINE takes no empty text)
        {"type": "text", "text": badge or " ", "size": "xxs", "flex": 2, "align": "end", "gravity": "center",
         "color": STAFF_UI["navy"]}]}


def binding_card(bindings: list[dict], names: list[str], fixed: dict[str, str], *, officers: dict[str, str | None],
                 admins=()) -> dict:
    """「綁定清單」: which LINE account (by its LINE name) registered which staff name — who can see the team.

    First what needs a look: names several accounts share (one tap asks which to release). Then the accounts
    under a name on the 名冊, in its order, and the ones under a name that is not on it (the 名冊守門員 keeps those
    out unless they are 幹部 or administrators). ``officers``: user id → title; ``admins``: user ids.

    One bubble while it fits LINE's size limit, then pages; accounts that fit on no page are owned up to.
    """
    roster = list(dict.fromkeys([*names, *fixed.values()]))
    by_name: dict[str, list[dict]] = {}
    for account in bindings:
        by_name.setdefault(account["staff_name"], []).append(account)
    order = [name for name in roster if name in by_name] + [name for name in by_name if name not in roster]
    shared = [name for name in order if len(by_name[name]) > 1]
    single = [name for name in order if len(by_name[name]) == 1]
    listed = [name for name in single if not roster or name in roster]
    outside = [name for name in single if roster and name not in roster]

    def badge(account):
        parts = (["管理員"] if account["user_id"] in admins else []) + \
                ([officers[account["user_id"]] or "幹部"] if account["user_id"] in officers else [])
        return "・".join(parts)

    def rows(group):
        return [(_binding_row(name, line_label(by_name[name][0]), badge(by_name[name][0])), 1) for name in group]

    blocks = []   # (node, how many accounts it shows)
    if shared:
        blocks.append((_section("⚠️ 同一個姓名有好幾個帳號", f"{len(shared)} 個姓名"), 0))
        for name in shared:
            who = "、".join(account_label(account) for account in by_name[name])
            blocks.append(({"type": "box", "layout": "vertical", "margin": "sm", "paddingAll": "10px",
                            "cornerRadius": "12px", "backgroundColor": STAFF_UI["warn"],
                            "action": message_action(f"解除登記 {name}"), "contents": [
                                {"type": "text", "text": name, "size": "sm", "weight": "bold"},
                                {"type": "text", "text": who, "size": "xs", "wrap": True},
                                {"type": "text", "text": "點這裡選要解除哪一個 ›", "size": "xxs", "color": STAFF_UI["navy"]}]},
                           len(by_name[name])))
    if listed:
        blocks += [(_section("✅ 已登記", f"{len(listed)} 個帳號"), 0), *rows(listed)]
    if outside:
        blocks += [(_section("🚫 不在名冊", f"{len(outside)} 個帳號"), 0),
                   ({"type": "text", "text": "姓名不在名冊上，看不到班表（幹部和管理員除外）。", "size": "xxs", "wrap": True,
                     "color": STAFF_UI["muted"]}, 0),
                   *rows(outside)]
    if not bindings:
        blocks.append(({"type": "text", "text": "還沒有人登記姓名。", "size": "sm", "color": STAFF_UI["muted"],
                        "margin": "md"}, 0))
    foot = {"type": "text", "size": "xxs", "wrap": True, "color": STAFF_UI["muted"], "margin": "lg",
            "text": "LINE 名稱是對方在 LINE 上的顯示名稱，對方每次傳訊息時最多每天更新一次。\n"
                    "一個姓名只能登記在一個 LINE 帳號。要解除某個帳號：傳「解除登記 姓名」。\n"
                    "還沒登記的同仁請看「人員名冊」（沒有 ✓ 的）。"}

    def bubble(page, of, contents):
        head = _hero("🔗 綁定清單" + (f" {page}/{of}" if of > 1 else ""),
                     f"{len(bindings)} 個 LINE 帳號已登記｜名冊 {len(roster)} 位")
        return {"type": "bubble", "size": "giga", "header": head,
                "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}},
                "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                         "contents": contents}}

    def paged(blocks, note=None):
        """The blocks cut into bubbles that each fit; the last one carries the foot (and ``note`` in front)."""
        pages, current = [], [note] if note else []
        for node, _ in blocks:
            if current and flex_size(bubble(9, 9, [*current, node, foot])) > FLEX_BUBBLE_LIMIT:
                pages.append(current); current = []
            current.append(node if current else {**node, "margin": "none"})
        pages.append([*current, foot])
        return [bubble(number, len(pages), contents) for number, contents in enumerate(pages, 1)]

    def fits(pages):
        return len(pages) <= 12 and sum(map(flex_size, pages)) <= FLEX_CAROUSEL_LIMIT

    def cut(size):
        """The first ``size`` blocks, owning up to the accounts left out (from the end: what needs a look is first)."""
        missing = len(bindings) - sum(count for _, count in blocks[:size])
        return paged(blocks[:size], {"type": "text", "text": f"⚠️ 名單太長，最後 {missing} 個帳號沒有列出。", "size": "xs",
                                     "weight": "bold", "wrap": True, "color": STAFF_UI["navy"]})

    bubbles = paged(blocks)
    if not fits(bubbles):
        low, high = 0, len(blocks) - 1   # the most blocks that fit: found by halving, not by dropping one at a time
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if fits(cut(middle)) else (low, middle - 1)
        bubbles = cut(low)
    pairs = "、".join(f"{name}＝{'／'.join(line_label(account) for account in by_name[name])}" for name in order)
    alt = f"綁定清單｜{len(bindings)} 個 LINE 帳號已登記｜{pairs}"
    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return {"type": "flex", "altText": alt[:400], "contents": contents}


def officers_card(officers: list[dict], admins: int) -> dict:
    """「幹部名單」: a row per 幹部 with their title as a badge, and the two things to do next."""
    rows = []
    for officer in officers:
        name = officer.get("staff_name")
        sub = "已登記" if name else ("LINE：" + officer["name"] + "｜" if officer.get("name") else "") + short_id(officer["user_id"])
        rows.append({"type": "box", "layout": "horizontal", "spacing": "md", "margin": "md", "paddingAll": "12px",
                     "cornerRadius": "12px", "backgroundColor": STAFF_UI["white"], "alignItems": "center", "contents": [
            {"type": "box", "layout": "vertical", "flex": 0, "paddingAll": "6px", "paddingStart": "10px",
             "paddingEnd": "10px", "cornerRadius": "20px", "backgroundColor": STAFF_UI["code"], "contents": [
                 {"type": "text", "text": officer.get("title") or "幹部", "size": "xs", "weight": "bold",
                  "color": STAFF_UI["navy"]}]},
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": name or "尚未登記姓名", "size": "md", "weight": "bold",
                 "color": STAFF_UI["ink"] if name else STAFF_UI["faint"]},
                {"type": "text", "text": sub, "size": "xxs", "color": STAFF_UI["muted"]}]}]})
    if not rows:
        rows.append({"type": "text", "text": "目前沒有幹部。", "size": "sm", "color": STAFF_UI["muted"]})
    note = "幹部可以用「人員管理」增刪名冊、固定代號與幹部。"
    if admins:
        note += f"\n另有管理員 {admins} 位（設定檔），一樣可以管理。"
    tiles = {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "xl", "contents": [
        action_tile("🙋", "新增幹部", "對方要先登記姓名", keyboard_action("新增幹部", "guide|新增幹部", "新增幹部 "),
                    STAFF_UI["tints"][1]),
        action_tile("🙅", "移除幹部", "用姓名或職稱", keyboard_action("移除幹部", "guide|移除幹部", "移除幹部 "),
                    STAFF_UI["tints"][3])]}

    def bubble(shown):
        more = ([{"type": "text", "text": f"…還有 {len(rows) - shown} 位，打「移除幹部 職稱」或「新增幹部」照常可用。",
                  "size": "xs", "wrap": True, "color": STAFF_UI["muted"], "margin": "md"}] if shown < len(rows) else [])
        return {"type": "bubble", "size": "giga", "header": _hero("👑 幹部名單", f"{len(officers)} 位｜可以使用人員管理"),
                "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": STAFF_UI["page"],
                         "contents": [*rows[:shown], *more, {"type": "text", "text": note, "size": "xs", "wrap": True,
                                                             "color": STAFF_UI["muted"], "margin": "lg"}, tiles]},
                "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}

    shown = len(rows)
    while shown > 1 and flex_size(bubble(shown)) > FLEX_BUBBLE_LIMIT:   # a long list: as many rows as fit
        shown -= 1
    alt = f"幹部名單｜{len(officers)} 位：" + "、".join(officer_label(o) for o in officers)
    return {"type": "flex", "altText": alt[:400], "contents": bubble(shown)}


# ---------------------------------------------------------------- 姓名申請: the name lock
# A registered name is locked, so that nobody takes a colleague's name to read their month: 「設定 another name」
# files a request instead, and so does registering a name another LINE account already has. Every administrator
# gets it on a card (``name_request_card``); the first of them to tap ✅ or ❌ decides. The 幹部 keep 人員管理, but
# answering these is the administrators' alone.
NAME_REQUESTS = ("姓名申請", "改名申請", "姓名審核", "改名審核", "姓名申請列表")
NAME_SHOWN = 8   # requests on one 「姓名申請」 card, the oldest first; the rest wait until these are answered
NAME_PUSH_GAP = 12 * 3600   # seconds: a person's next request within this is filed but not pushed again
RENAME_UI = {    # pastel like 本月統計, ink for every word; ✅ 同意／❌ 不同意 say what they do in words as well
    "band": ("#E3E8FF", "#FFE4EF"), "title": "#4C3F91", "ink": "#2E2A3D", "muted": "#6E6880", "page": "#FAF8FF",
    "panel": "#FFFFFF", "edge": "#E6E1F5", "from": "#EEF1F6", "to": "#E3F4E8", "warn": "#FFF1DC",
    "yes": "#DDF4E4", "yes_edge": "#2F8F55", "no": "#FDE8EC", "no_edge": "#C2415B",
}


def request_time(stamp: float | None) -> str:
    """「10/10（六）14:05」, Taiwan time."""
    when = datetime.fromtimestamp(stamp or 0, TAIPEI)
    return f"{day_label(when.date())}{when:%H:%M}"


def requester_label(request: dict) -> str:
    """「林小明（LINE：明明）」: the name the requester has and their LINE name, as much of it as is known."""
    name, display = request.get("current_name"), request.get("display_name")
    if name and display and display != name:
        return f"{name}（LINE：{display}）"
    return name or (f"LINE：{display}" if display else short_id(request["user_id"]))


def request_words(request: dict) -> str:
    """「林小明（LINE：明明） 改名為「王小明」」: what a request asks, for the approvers' replies."""
    return f"{requester_label(request)} {'改名為' if request.get('current_name') else '登記為'}「{request['requested_name']}」"


def _name_chip(caption: str, name: str | None, tint: str) -> dict:
    """A name in a tinted box under a small caption; no name (a first registration) is a quiet 「還沒登記」."""
    shown = ({"type": "text", "text": name, "size": "lg", "weight": "bold", "color": RENAME_UI["ink"]} if name else
             {"type": "text", "text": "還沒登記", "size": "sm", "color": RENAME_UI["muted"]})
    return {"type": "box", "layout": "vertical", "flex": 1, "paddingAll": "10px", "cornerRadius": "12px",
            "backgroundColor": tint, "justifyContent": "center", "contents": [
                {"type": "text", "text": caption, "size": "xxs", "align": "center", "color": RENAME_UI["muted"]},
                {**shown, "align": "center", "wrap": True}]}


def _decision(request: dict, approve: bool, built: int) -> dict:
    """✅ 同意 or ❌ 不同意 as a tappable box bound to this one request, never to another: ``rename|<id>|yes|<built>``,
    ``built`` being when the card was made (microseconds; cards from before that was needed carry milliseconds), so
    an approval can tell what that card could show."""
    new, old = request["requested_name"], request.get("current_name")
    who = old or request.get("display_name") or "申請人"
    if approve:
        mark, answer, hint, tint = "✅", "同意", f"改成「{new}」" if old else f"登記為「{new}」", "yes"
    else:
        mark, answer, hint, tint = "❌", "不同意", f"維持「{old}」" if old else "不登記", "no"
    return {"type": "box", "layout": "vertical", "flex": 1, "paddingAll": "12px", "cornerRadius": "14px",
            "backgroundColor": RENAME_UI[tint], "borderWidth": "2px", "borderColor": RENAME_UI[f"{tint}_edge"],
            "action": {"type": "postback", "label": answer, "data": f"rename|{request['id']}|{tint}|{built}",
                       "displayText": f"{answer}：{who} {'改名為' if old else '登記為'} {new}"},
            "contents": [{"type": "text", "text": f"{mark} {answer}", "size": "md", "weight": "bold", "align": "center",
                          "color": RENAME_UI["ink"]},
                         {"type": "text", "text": hint, "size": "xxs", "align": "center", "wrap": True,
                          "color": RENAME_UI["muted"]}]}


def _request_panel(request: dict, holders: list[dict], *, heading: bool, built: int) -> dict:
    """One request on a white panel: the name now ➜ the name asked for, a warning when other LINE accounts already
    have that name (``holders``), and the two answers. ``heading``: who asks and when, above it all (on the list;
    the card of a single request says that in its band)."""
    new, old = request["requested_name"], request.get("current_name")
    contents = []
    if heading:
        contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "none", "contents": [
            {"type": "text", "text": f"🙋 {requester_label(request)}", "size": "sm", "weight": "bold", "wrap": True,
             "flex": 1, "color": RENAME_UI["ink"]},
            {"type": "text", "text": request_time(request["created_at"]), "size": "xxs", "flex": 0, "gravity": "center",
             "color": RENAME_UI["muted"]}]})
    contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md" if heading else "none",
                     "contents": [
        _name_chip("現在的姓名", old, RENAME_UI["from"]),
        {"type": "text", "text": "➜", "size": "lg", "flex": 0, "gravity": "center", "color": RENAME_UI["muted"]},
        _name_chip("想改成" if old else "想登記", new, RENAME_UI["to"])]})
    if request.get("released_name") and not old:
        contents.append({"type": "text", "text": f"（之前登記過「{request['released_name']}」，後來解除了）",
                         "size": "xxs", "wrap": True, "margin": "sm", "color": RENAME_UI["muted"]})
    if holders:
        lines = [display for display in (holder.get("name") for holder in holders) if display]
        contents.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "10px",
                         "cornerRadius": "12px", "backgroundColor": RENAME_UI["warn"], "contents": [
            {"type": "text", "wrap": True, "size": "xs", "color": RENAME_UI["ink"],
             "text": f"⚠️「{new}」已經有 {len(holders)} 個 LINE 帳號登記" + (f"（LINE：{'、'.join(lines[:3])}）" if lines else "")
                     + "。一個姓名只能登記在一個 LINE 帳號：同意後姓名轉給申請人，原本的帳號會被解除"
                     + ("、看不到班表" if any(holder.get("kept_out") for holder in holders) else "") + "。"
                     + "".join(f"\n{holder.get('name') or short_id(holder['user_id'])} 是{holder['role']}，解除後照樣看得到班表、"
                               "可以管理人員。" for holder in holders if holder.get("role"))}]})
    contents.append({"type": "box", "layout": "horizontal", "spacing": "md", "margin": "lg",
                     "contents": [_decision(request, True, built), _decision(request, False, built)]})
    return {"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "14px", "cornerRadius": "16px",
            "backgroundColor": RENAME_UI["panel"], "borderWidth": "1px", "borderColor": RENAME_UI["edge"],
            "contents": contents}


def name_request_card(requests: list[tuple[dict, list[dict]]], *, listing: bool = False, more: int = 0,
                      built: int | None = None) -> dict:
    """What the administrators answer: (request, other accounts with that name) pairs, each with its own
    ✅ 同意／❌ 不同意.

    One request (``listing`` False) is the card pushed to each of them, its band saying who asks for what and when.
    「姓名申請」 lists the pending ones, the oldest first (``more``: how many wait past ``NAME_SHOWN``). Either
    way it is one giga bubble: eight requests, long names and warnings on all of them, come to about 22 KB.
    """
    start, end = RENAME_UI["band"]
    if listing:
        total = len(requests) + more
        top = [{"type": "text", "text": "📮 姓名申請", "weight": "bold", "size": "sm", "color": RENAME_UI["title"]},
               {"type": "text", "text": f"{total} 筆等你確認", "weight": "bold", "size": "xl", "wrap": True,
                "color": RENAME_UI["ink"]},
               {"type": "text", "text": "先按的人決定；處理過的就不會再列出來", "size": "xs", "wrap": True,
                "color": RENAME_UI["muted"]}]
        alt = f"📮 姓名申請 {total} 筆｜" + "、".join(request_words(request) for request, _ in requests)
    else:
        (request, _), = requests
        old, who = request.get("current_name"), requester_label(request)
        caller = old or request.get("display_name") or "一位同仁"
        top = [{"type": "text", "text": "🙋 姓名變更申請" if old else "🙋 姓名登記申請", "weight": "bold", "size": "sm",
                "color": RENAME_UI["title"]},
               {"type": "text", "text": f"{caller} 想{'改名' if old else '登記姓名'}", "weight": "bold", "size": "xl",
                "wrap": True, "color": RENAME_UI["ink"]},
               {"type": "text", "text": f"申請人：{who}\n{request_time(request['created_at'])} 送出，等你確認",
                "size": "xs", "wrap": True, "color": RENAME_UI["muted"]}]
        alt = f"🙋 姓名申請｜{request_words(request)}｜請點同意或不同意"
    header = {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "135deg", "startColor": start, "endColor": end},
              "contents": top}
    # What the card shows is as of now, to the microsecond: a millisecond is long enough for someone to register
    # the name just before or just after, and a yes must know which
    built = int(time.time() * 1_000_000) if built is None else built
    body = [_request_panel(request, holders, heading=listing, built=built) for request, holders in requests]
    body[0] = {**body[0], "margin": "none"}
    notes = ["🔒 姓名登記後不能自己改，免得有人用別人的姓名查看班表與統計。",
             f"👥 每位管理員都{'看得到這些申請' if listing else '收到這張'}，先按的人決定；之後再按只會告訴你結果。"]
    if more:
        notes.append(f"📮 還有 {more} 筆，處理完這些再傳一次「姓名申請」。")
    body.append({"type": "text", "text": "\n".join(notes), "size": "xxs", "wrap": True, "color": RENAME_UI["muted"],
                 "margin": "lg"})
    return {"type": "flex", "altText": alt[:400], "contents": {
        "type": "bubble", "size": "giga", "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": RENAME_UI["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": start}}}}


# ---------------------------------------------------------------- one person's month
STATS_PLACES = 6    # places listed on the card; the rest are added up in one last line
STATS_DATES = 8     # dates spelled out in a note before 「…等 N 天」


def absence_kind(code, result) -> str:
    """「輪休」, or the kind of leave the roster's leave table gives this code; empty when it says nothing."""
    leave = result.get('leave_table', {})
    if not leave.get('available'):
        return ""
    if code in leave.get('rest', []):
        return "輪休"
    entry = next((e for e in leave.get('leave', []) if e.get('code') == code), None)
    return (entry.get('kind') or "請假") if entry else ""


def _stat_dates(days) -> str:
    """「10/3、10/5（輪休）」 for a note; a long list ends in 「…等 12 天」."""
    shown = "、".join(f"{d.day.month}/{d.day.day}" + (f"（{d.note}）" if d.note else "") for d in days[:STATS_DATES])
    return shown + (f"…等 {len(days)} 天" if len(days) > STATS_DATES else "")


def stats_notes(month) -> list[str]:
    """What a month's total rests on: the days without a roster, without a match, or not counted this time."""
    kinds = monthly_stats
    missing, unknown, off = month.of(kinds.MISSING), month.of(kinds.UNKNOWN), month.of(kinds.OFF)
    pending, unreadable = month.of(kinds.PENDING), month.of(kinds.UNREADABLE)
    notes = [f"資料：這 {len(month.days)} 天裡，有 {len(month.days) - len(missing)} 天的班表。"]
    if missing:
        notes.append(f"沒有班表：{_stat_dates(missing)}。")
    if unknown:
        first = unknown[0].day
        notes.append(f"有班表但沒找到你：{_stat_dates(unknown)}。那天有上班的話，"
                     f"傳「我是 代號 {first.month}/{first.day}」補登。")
    if off:
        notes.append(f"班表上沒有你的勤務：{_stat_dates(off)}。")
    if pending:
        notes.append(f"舊班表還在重新讀取，這次沒算：{_stat_dates(pending)}。請稍後再查一次。")
    if unreadable:
        notes.append(f"班表資料讀不出來：{_stat_dates(unreadable)}。")
    partial = [day for day in month.days if day.partial]
    if partial:
        notes.append(f"舊版讀法的班表，最右四欄（A1A3站、內機、內部管理、全區帶勤）沒有讀到，"
                     f"時數可能偏少：{_stat_dates(partial)}。")
    if month.unsure:
        notes.append(f"其中 {month.unsure} 小時的辨識信心較低，請對照原圖。")
    notes.append("這是班表上排定的時數，不是正式出勤紀錄；臨時換班沒有重貼班表就不會反映。")
    return notes


# The card's colours: pastel surfaces, ink for every word. A date's tint on the calendar says what kind of day it
# was, its mark says it in words and the legend under it names both, so colour is never the only cue.
CUTE = {
    "page": "#FFF8FB", "head": "#FFE3EC", "title": "#9C2F5A", "ink": "#3F2533", "muted": "#7D6470",
    "panel": "#FFFFFF", "edge": "#F6DCE6", "track": "#FDEAF1", "duty": "#F06A98", "today": "#C93B6E",
}
STATS_TILES = (("⏰", "排定時數", "#FFEDF3"), ("📅", "上班天數", "#FFF3DF"), ("🍙", "平均一天", "#E8F4FF"))


def _bar(height: int, color: str, **extra) -> dict:
    """A plain rounded block (a column, a pill, a swatch): a box holding only a filler."""
    return {"type": "box", "layout": "vertical", "contents": [{"type": "filler"}], "height": f"{height}px",
            "backgroundColor": color, "cornerRadius": f"{min(height, 8) // 2}px", **extra}


def _tile(emoji: str, label: str, value: str, unit: str, background: str) -> dict:
    return {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "paddingAll": "10px",
            "cornerRadius": "14px", "backgroundColor": background, "contents": [
                {"type": "text", "text": emoji, "size": "lg", "align": "center"},
                {"type": "text", "text": f"{value} {unit}", "align": "center", "color": CUTE["ink"], "contents": [
                    {"type": "span", "text": value, "size": "xl", "weight": "bold"},
                    {"type": "span", "text": f" {unit}", "size": "xs"}]},
                {"type": "text", "text": label, "size": "xxs", "align": "center", "color": CUTE["muted"]}]}


def _panel(title: str, side: str | None, contents: list) -> dict:
    """A white rounded panel with a title (and a note at its right) above ``contents``."""
    heading = [{"type": "text", "text": title, "weight": "bold", "size": "sm", "color": CUTE["title"], "flex": 0}]
    if side:
        heading.append({"type": "text", "text": side, "size": "xxs", "color": CUTE["muted"], "align": "end",
                        "gravity": "center", "wrap": True})
    return {"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "12px", "cornerRadius": "16px",
            "backgroundColor": CUTE["panel"], "borderWidth": "1px", "borderColor": CUTE["edge"], "contents": [
                {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": heading}, *contents]}


def places_chart(places: list, total: float) -> dict:
    """Hours per place as bars, the longest first; the rest beyond ``STATS_PLACES`` add up to one last bar."""
    hours = monthly_stats.hours_text
    rows = places[:STATS_PLACES]
    if places[STATS_PLACES:]:
        rows.append((f"其他 {len(places) - STATS_PLACES} 個地點", sum(value for _, value in places[STATS_PLACES:])))
    top = max(value for _, value in rows)
    contents = []
    for index, (name, value) in enumerate(rows):
        weight = "bold" if index == 0 else "regular"
        width = max(4, round(100 * value / top))
        contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "alignItems": "center",
                         "contents": [
            {"type": "text", "text": name, "size": "sm", "weight": weight, "wrap": True, "flex": 4,
             "color": CUTE["ink"]},
            {"type": "box", "layout": "vertical", "flex": 4, "height": "10px", "cornerRadius": "5px",
             "backgroundColor": CUTE["track"], "contents": [_bar(10, CUTE["duty"], width=f"{width}%")]},
            {"type": "text", "text": f"{hours(value)} 小時", "size": "xs", "weight": weight, "align": "end",
             "flex": 3, "color": CUTE["ink"]},
            {"type": "text", "text": f"{monthly_stats.share(value, total)}%", "size": "xxs", "align": "end",
             "flex": 2, "color": CUTE["muted"]}]})
    return _panel("🍰 各地點時數", "長條越長，時數越多", contents)


# ---------------------------------------------------------------- 班表月曆: a day's roster a tap away
# The days of 「本月統計」 laid out as a calendar: a date with a roster opens that day's whole photo and one's own
# duty card on it. The team asked for it to begin with October 2026, so no month or date before that is offered.
CALENDAR_START = date(2026, 10, 1)
CALENDAR_WEEK = "日一二三四五六"   # Taiwan calendars start on Sunday
CALENDAR_UI = {   # with CUTE: a date's tint says what kind of day it was, and the mark under its number says it again
    "duty": "#FFE3EC", "long": "#FFC6D9", "off": "#ECE5FB", "uncounted": "#FFEFD2", "missing": "#EEE9EC",
    "missing_ink": "#9C8C93", "future_ink": "#CDBFC6", "earlier": "#FFF3DF", "band": ("#FFD3E2", "#E7DEFF"),
}
CALENDAR_KEYS = (   # the legend: what a date may show, in words, on the tint of such a date
    ("h", "8h 排定時數", "duty"), ("long", f"超過 {monthly_stats.OVERTIME_AFTER} 小時", "long"), ("💤", "💤 輪休", "off"),
    ("🌴", "🌴 請假", "off"), ("🍃", "🍃 沒有勤務", "off"), ("❓", "❓ 沒找到你", "uncounted"),
    ("⏳", "⏳ 讀取中", "uncounted"), ("⚠️", "⚠️ 讀不出來", "missing"), ("missing", "沒有班表", "missing"))
CALENDAR_LEGEND_WIDTH = 230   # px of legend pills on a line: a narrow phone's giga bubble has about 240


def earlier_month(year: int, month: int) -> tuple[int, int] | None:
    """The month before, when 「班表月曆」 has it: only from ``CALENDAR_START``'s month on."""
    before = monthly_stats.previous(year, month)
    return before if date(*before, 1) >= CALENDAR_START.replace(day=1) else None


def calendar_mark(day) -> str:
    """What a date says under its number: the hours planned (「8h」), or what kind of day it was — 💤 輪休, 🌴 another
    leave, 🍃 nothing on the roster, ❓ the name not on its name table, ⏳ or ⚠️ a roster that was not counted."""
    kinds = monthly_stats
    if day.status == kinds.DUTY:
        return f"{kinds.hours_text(day.hours)}h"
    if day.status == kinds.OFF:
        return "💤" if day.note == "輪休" else "🌴" if day.note else "🍃"
    return {kinds.UNKNOWN: "❓", kinds.PENDING: "⏳", kinds.UNREADABLE: "⚠️"}.get(day.status, "　")


def _calendar_kind(day) -> str:
    """The tint of a date (a key of ``CALENDAR_UI``). An unreadable roster is grey: there is nothing to show of it."""
    kinds = monthly_stats
    if day.status == kinds.DUTY:
        return "long" if day.overtime else "duty"
    return {kinds.OFF: "off", kinds.MISSING: "missing", kinds.UNREADABLE: "missing"}.get(day.status, "uncounted")


CALENDAR_CELL = "13.5%"   # every day's width: seven of them leave 5.5% (about 3px a gap) for space-between


def _calendar_frame(number: str) -> dict:
    """The box of a day on the calendar: its number over a second line (a mark, or a blank one), as wide as every
    other day (``CALENDAR_CELL``, see ``month_calendar``). Padded above and below only: padding at the sides could
    count on top of the width and push the week past the panel; the words are centred anyway. Lean on purpose, as a
    month has up to 42 of these: no fixed height (every cell has the two lines)."""
    return {"type": "box", "layout": "vertical", "flex": 0, "width": CALENDAR_CELL, "paddingTop": "3px",
            "paddingBottom": "3px", "cornerRadius": "8px", "contents": [
        {"type": "text", "text": number, "size": "sm", "align": "center", "color": CUTE["ink"]},
        {"type": "text", "text": "　", "size": "xxs", "align": "center", "color": CUTE["muted"]}]}


def _calendar_blank() -> dict:
    """A day of the month before or after, filling a week: an empty box as wide as a date; no tint, no tap."""
    return {"type": "box", "layout": "vertical", "flex": 0, "width": CALENDAR_CELL, "contents": [{"type": "filler"}]}


def _calendar_cell(when: date, day, *, today: bool) -> dict:
    """One date. With a roster to show it is tinted, marked and tappable (``cal|YYYY-MM-DD``); without one it is grey;
    a date still to come (``day`` is None) or before ``CALENDAR_START`` is only a faint number. Today is ringed."""
    cell = _calendar_frame(str(when.day))
    number, mark = cell["contents"]
    if day is None or when < CALENDAR_START:
        number["color"] = CALENDAR_UI["future_ink"]
    else:
        kind = _calendar_kind(day)
        cell["backgroundColor"] = CALENDAR_UI[kind]
        mark["text"] = calendar_mark(day)
        if kind == "missing":
            number["color"] = CALENDAR_UI["missing_ink"]
        else:
            words = f"{when.month}/{when.day} 的班表"
            cell["action"] = {"type": "postback", "label": words, "data": f"cal|{when.isoformat()}",
                              "displayText": words}
    if today:
        number.update(weight="bold", color=CUTE["today"])
        cell.update(borderWidth="2px", borderColor=CUTE["today"])
    return cell


def _calendar_legend(keys: set[str], today: bool) -> list[dict]:
    """The legend: a little pill like a date for each tint and mark on the calendar (and today's ring), named in ink.
    Only those that are there, on as many lines as they need (``CALENDAR_LEGEND_WIDTH``, from a rough width of their
    words: a Chinese character or an emoji is about 11px at this size, a letter or a digit about 6px)."""
    def pill(words, background, **ring):
        return {"type": "box", "layout": "vertical", "flex": 0, "paddingStart": "6px", "paddingEnd": "6px",
                "paddingTop": "1px", "paddingBottom": "1px", "cornerRadius": "6px", "backgroundColor": background,
                **ring, "contents": [{"type": "text", "text": words, "size": "xxs", "color": CUTE["ink"]}]}

    pills = [(words, pill(words, CALENDAR_UI[tint])) for key, words, tint in CALENDAR_KEYS if key in keys]
    if today:
        pills.append(("今天", pill("今天", CUTE["panel"], borderWidth="2px", borderColor=CUTE["today"])))
    lines, used = [], CALENDAR_LEGEND_WIDTH
    for words, shown in pills:
        width = 16 + sum(6 if character.isascii() else 11 for character in words)
        if used + width > CALENDAR_LEGEND_WIDTH:
            lines.append([])
            used = 0
        lines[-1].append(shown)
        used += width
    return [{"type": "box", "layout": "horizontal", "spacing": "xs", "margin": "xs" if index else "md",
             "contents": line} for index, line in enumerate(lines)]


def month_calendar(month, *, today: date | None = None, earlier: tuple[int, int] | None = None,
                   title: str = "🗓️ 班表月曆") -> dict:
    """「🗓️ 班表月曆」: a ``monthly_stats.Month`` as a calendar, Sunday first, one cell per date (``_calendar_cell``);
    a tap on a date with a roster shows that day's roster. ``today`` is ringed and the dates after it are faint
    (the month's days stop at today). ``earlier`` is the month a tile under the calendar opens (``earlier_month``).

    Every cell, the weekday names and the blank days that fill the first and last weeks included, has one fixed
    width (``CALENDAR_CELL``, flex 0) and each row spreads them out (space-between). With flex alone LINE's app sizes
    the cells of a row from what they hold and then shares out the room left over, so a week with empty days, or
    with a wider mark such as 「10h」, gave its dates more or less of it than the weeks around it (in October 2026,
    1–3 came out about 7px wider than the dates under them) and the columns did not line up.
    """
    length = calendar.monthrange(month.year, month.month)[1]
    found = {day.day: day for day in month.days}
    first = date(month.year, month.month, 1)
    dates = [first + timedelta(days=offset) for offset in range(length)]
    cells = [_calendar_blank() for _ in range((first.weekday() + 1) % 7)]
    cells += [_calendar_cell(when, found.get(when), today=when == today) for when in dates]
    cells += [_calendar_blank() for _ in range(-len(cells) % 7)]

    def week(contents, margin):
        return {"type": "box", "layout": "horizontal", "justifyContent": "space-between", "margin": margin,
                "contents": contents}

    names = [{"type": "box", "layout": "vertical", "flex": 0, "width": CALENDAR_CELL, "contents": [
        {"type": "text", "text": name, "size": "xxs", "weight": "bold", "align": "center",
         "color": CUTE["title"] if name in "日六" else CUTE["muted"]}]} for name in CALENDAR_WEEK]
    shown = [found[when] for when in dates if when in found and when >= CALENDAR_START]
    keys = ({"h" if day.status == monthly_stats.DUTY else calendar_mark(day) for day in shown}
            | {"long" for day in shown if day.overtime}
            | {"missing" for day in shown if day.status == monthly_stats.MISSING})
    contents = [week(names, "md"), *(week(cells[start:start + 7], "3px") for start in range(0, len(cells), 7)),
                *_calendar_legend(keys, today in dates)]
    if earlier:
        year, number = earlier
        words = f"{number} 月的班表"
        contents.append({"type": "box", "layout": "horizontal", "margin": "md", "paddingAll": "10px",
                         "cornerRadius": "12px", "backgroundColor": CALENDAR_UI["earlier"], "action": {
                             "type": "postback", "label": words, "data": f"cal|{year}-{number:02d}",
                             "displayText": words}, "contents": [
                             {"type": "text", "text": f"◀ {words}", "size": "sm", "weight": "bold",
                              "color": CUTE["ink"], "flex": 0},
                             {"type": "text", "text": "上個月的月曆", "size": "xxs", "color": CUTE["muted"],
                              "align": "end", "gravity": "center"}]})
    return _panel(title, "點有顏色的日期看當天班表", contents)


def stats_card(staff_name: str, month) -> dict:
    """This month for one person (a ``monthly_stats.Month``, up to today): three figures, the month as a calendar
    whose dates open that day's roster (``month_calendar``, with last month a tap away once it has one) and a bar
    per place.

    The notes name the days the total does not cover, so a missing roster or an unmatched name shows
    instead of silently lowering the total.
    """
    worked = month.of(monthly_stats.DUTY)
    total, places = month.hours, month.places()
    hours = monthly_stats.hours_text
    last = month.days[-1].day
    head = [
        {"type": "text", "text": "🐰 本月統計", "weight": "bold", "size": "sm", "color": CUTE["title"]},
        {"type": "text", "text": f"{staff_name} 的 {month.month} 月", "weight": "bold", "size": "xl", "wrap": True,
         "color": CUTE["ink"], "margin": "xs"},
        {"type": "text", "text": f"{day_label(month.days[0].day)}–{day_label(last)}，班表上排定的勤務",
         "size": "xs", "color": CUTE["muted"], "wrap": True, "margin": "xs"}]
    alt = f"{month.month} 月統計｜{staff_name}"
    contents = []
    if worked:
        top, top_hours = places[0]
        head.append({"type": "text", "text": f"到 {last.month}/{last.day} 排了 {total} 小時，最常在{top}，辛苦了！🍵",
                     "size": "sm", "wrap": True, "color": CUTE["ink"], "margin": "md"})
        if month.overtime:   # the hours past eight a day: a tap away from what they pay (「加班結算」)
            head.append({"type": "text", "text": f"🌙 其中加班 {hours(month.overtime)} 小時，點這裡看加班費 👉",
                         "size": "xs", "wrap": True, "weight": "bold", "color": CUTE["title"], "margin": "sm",
                         "action": {"type": "message", "label": "加班結算", "text": "加班結算"}})
        contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "lg", "contents": [
            _tile(emoji, label, value, unit, background) for (emoji, label, background), (value, unit) in zip(
                STATS_TILES, ((str(total), "小時"), (str(len(worked)), "天"), (hours(total / len(worked)), "小時")))]})
        alt += f"｜排定 {total} 小時、上班 {len(worked)} 天｜最久：{top} {hours(top_hours)} 小時"
    else:
        head.append({"type": "text", "text": "🌱 這段期間的班表上沒有找到你的勤務。", "size": "sm", "wrap": True,
                     "color": CUTE["ink"], "margin": "md"})
        alt += "｜沒有找到勤務"
    if date(month.year, month.month, 1) >= CALENDAR_START.replace(day=1):
        contents.append(month_calendar(month, today=last, earlier=earlier_month(month.year, month.month)))
    if worked:
        contents.append(places_chart(places, total))
    contents.append({"type": "text", "text": "\n".join(stats_notes(month)), "size": "xxs", "wrap": True,
                     "color": CUTE["muted"], "margin": "lg"})
    return {"type": "flex", "altText": alt[:400], "contents": {"type": "bubble", "size": "giga", "body": {
        "type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": CUTE["page"], "contents": [
            {"type": "box", "layout": "vertical", "paddingAll": "14px", "cornerRadius": "16px",
             "backgroundColor": CUTE["head"], "contents": head}, *contents]}}}


def calendar_card(staff_name: str, month, *, today: date | None = None, earlier: tuple[int, int] | None = None,
                  seen: date | None = None) -> dict:
    """「◀ 9 月的班表」: a past month's 班表月曆 on a card of its own (a whole ``monthly_stats.Month``), to look
    up that month's rosters a day at a time. After a date tapped on a calendar (``seen``) it is that month's calendar
    again, to tap the next date straight away: this month's runs up to ``today``, ringed, and offers last month
    (``earlier``) as 「本月統計」's does.

    Only the calendar: the statistics stay this month's, in 「本月統計」, which the tile under it goes back to.
    """
    found = [day for day in month.days if day.status != monthly_stats.MISSING]
    start, end = CALENDAR_UI["band"]
    seen_words = f"剛看完 {seen.month}/{seen.day} 的班表，點其他日期繼續看" if seen else ""
    header = {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "135deg", "startColor": start, "endColor": end},
              "contents": [
                  {"type": "text", "text": "🗓️ 班表月曆", "weight": "bold", "size": "sm", "color": CUTE["title"]},
                  {"type": "text", "text": f"{staff_name} 的 {month.month} 月", "weight": "bold", "size": "xl",
                   "wrap": True, "color": CUTE["ink"]},
                  {"type": "text", "text": f"{seen_words} 🐰" if seen else
                   f"{month.month} 月有 {len(found)} 天的班表。點日期，看當天的原圖和你的勤務 🐰" if found else
                   f"{month.month} 月沒有班表可以看。", "size": "xs", "wrap": True, "color": CUTE["muted"]}]}
    body = [{**month_calendar(month, today=today, earlier=earlier, title=f"🗓️ {month.year} 年 {month.month} 月"),
             "margin": "none"},
            {"type": "box", "layout": "horizontal", "margin": "md", "paddingAll": "10px", "cornerRadius": "12px",
             "backgroundColor": CUTE["head"], "action": message_action("本月統計"), "contents": [
                 {"type": "text", "text": "📊 回到本月統計", "size": "sm", "weight": "bold", "color": CUTE["ink"],
                  "flex": 0},
                 {"type": "text", "text": "時數只算這個月", "size": "xxs", "color": CUTE["muted"], "align": "end",
                  "gravity": "center"}]}]
    alt = f"🗓️ {month.month} 月的班表月曆｜{staff_name}｜{seen_words or f'{len(found)} 天有班表，點日期看當天的班表'}"
    return {"type": "flex", "altText": alt[:400], "contents": {"type": "bubble", "size": "giga", "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": CUTE["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": start}}}}


# ---------------------------------------------------------------- 加班結算
# The team's rule (``monthly_stats.OVERTIME_AFTER``): on a day on duty every hour past eight is overtime. The pay
# is a plain estimate, the overtime hours × the hourly wage the user typed, without the legal 1.34 / 1.67 rates.
OVERTIME_COMMANDS = ("加班", "加班費", "加班時數", "加班結算", "加班費試算", "試算加班費", "我的加班", "本月加班",
                     "本月加班費", "月底結算", "結算", "本月結算")
OVERTIME_LAST_MONTH = ("上月結算", "上個月結算", "上月加班", "上個月加班", "上月加班費", "上個月加班費")
WAGE_COMMAND = re.compile(r"(?:設定\s*)?時薪[:：]?\s*(.*)")
WAGE_NUMBER = re.compile(r"(?:NT\$?|NTD|\$)?\s*(-?\d[\d,]*(?:\.\d+)?)\s*(?:元|塊)?(?:\s*/\s*(?:小時|hr|h))?", re.I)
WAGE_CLEAR = ("取消", "刪除", "清除", "清空")
WAGE_LIMIT = 10_000   # dollars an hour; more is surely a slip (a monthly salary, an extra zero)
WAGE_HOW = "輸入「時薪 200」（換成自己的時薪，可以有小數，例如 196.5），就能算出這個月的加班費。"
OVERTIME_UI = {   # with CUTE: a day's bar is pink up to eight hours and amber past them; the words stay in ink
    "regular": CUTE["duty"], "over": "#F59E0B", "band": ("#FFD3E2", "#FFEBC4"), "pay": ("#FFF0C2", "#FFD9E6"),
    "days": "#FFF3DF", "hours": "#FFEDF3", "extra": "#FFF6D9", "faint": "#CBB9C2",
}
OVERTIME_SCALE = 12   # hours of a full bar (a longer day widens every bar)
OVERTIME_COLUMNS = (3, 9, 2, 2)   # widths of a day's row: date, bar, hours, overtime
OVERTIME_DAY = {monthly_stats.UNKNOWN: "❓ 有班表，沒找到你", monthly_stats.PENDING: "⏳ 舊班表還在重新讀取",
                monthly_stats.UNREADABLE: "⚠️ 班表讀不出來", monthly_stats.MISSING: "沒有班表"}


def parse_wage(value: str) -> float | None:
    """「200」「200元」「NT$200」「196.5」「1,000」 → the number (a negative one too, for the caller to refuse);
    ``None`` when it is not a number."""
    match = WAGE_NUMBER.fullmatch(normalise(value))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def money(value: float) -> str:
    """1234.5 → 「$1,235」: whole dollars, half up, with thousands separators."""
    return f"${int(value + .5):,}"


def wage_text(wage: float) -> str:
    """200 → 「$200」, 196.5 → 「$196.5」: the wage as typed, without needless decimals."""
    return f"${wage:,.2f}".rstrip("0").rstrip(".")


def overtime_row(when: date, day, scale: int, *, now: bool, soon: bool = False) -> dict:
    """One date of the month: a day on duty is a bar (the first eight hours pink, the rest amber) with its hours
    and 「+2」; any other day says in a few words why it has no bar. A day still to come is only its faint date;
    the first of them (``soon``) also says 「還沒到」, which explains the blank rows under it.

    Lean on purpose: a month has 31 of these, and the card has to stay well under LINE's size limit. So the bar's
    pieces sit straight in the row instead of in a box of their own: the columns (``OVERTIME_COLUMNS``) are
    weighted ``scale`` times, and an hour of the bar weighs the bar's share, so every bar keeps the same scale.
    """
    kinds, hours = monthly_stats, monthly_stats.hours_text
    date_part, bar_part, hours_part, over_part = OVERTIME_COLUMNS
    label = {"type": "text", "text": "今天" if now else f"{when.day} {WEEKDAYS[when.weekday()]}", "size": "xs",
             "color": CUTE["ink"]}
    if now:
        label.update(weight="bold", color=CUTE["today"])
    monday = {"margin": "lg"} if when.weekday() == 0 else {}   # a little air before each Monday groups the weeks
    if day is None and not soon:
        return {**label, "color": OVERTIME_UI["faint"], **monday}
    if day is None:
        cells = [{**label, "color": OVERTIME_UI["faint"], "flex": date_part},
                 {"type": "text", "text": "還沒到", "size": "xxs", "color": OVERTIME_UI["faint"],
                  "flex": bar_part + hours_part + over_part}]
    elif day.status == kinds.DUTY and day.hours:
        regular, over = min(day.hours, kinds.OVERTIME_AFTER), day.overtime
        cells = [{**label, "flex": date_part * scale},
                 _bar(10, OVERTIME_UI["regular"], flex=max(1, round(bar_part * regular)))]
        if over:   # 2px apart, so the two parts never read as one
            cells.append(_bar(10, OVERTIME_UI["over"], flex=max(1, round(bar_part * over)), margin="2px"))
        if scale > day.hours:
            cells.append({"type": "filler", "flex": round(bar_part * (scale - day.hours))})
        cells += [{"type": "text", "text": hours(day.hours), "size": "xs", "flex": hours_part * scale, "align": "end",
                   "color": CUTE["ink"]},
                  {"type": "text", "text": f"+{hours(over)}", "size": "xs", "flex": over_part * scale, "align": "end",
                   "weight": "bold", "color": CUTE["ink"]} if over else {"type": "filler", "flex": over_part * scale}]
    else:
        word = f"💤 {day.note or '休假'}" if day.status == kinds.OFF else OVERTIME_DAY.get(day.status, "沒有勤務")
        cells = [{**label, "flex": date_part}, {"type": "text", "text": word, "size": "xxs", "color": CUTE["muted"],
                                                "flex": bar_part + hours_part + over_part}]
    return {"type": "box", "layout": "horizontal", "alignItems": "center", "contents": cells, **monday}


def overtime_days(month) -> dict:
    """Every date of the month, one row each (``overtime_row``), under a scale that marks where eight hours end."""
    after = monthly_stats.OVERTIME_AFTER
    length = calendar.monthrange(month.year, month.month)[1]
    found = {day.day.day: day for day in month.days}
    last = month.days[-1].day.day
    scale = max([OVERTIME_SCALE] + [round(day.hours) for day in month.days])
    small = {"size": "xxs", "color": CUTE["muted"]}
    date_part, bar_part, hours_part, over_part = OVERTIME_COLUMNS
    scale_row = {"type": "box", "layout": "horizontal", "margin": "md", "contents": [
        {"type": "text", "text": "日期", "flex": date_part, **small},
        {"type": "box", "layout": "horizontal", "flex": bar_part, "contents": [
            {"type": "text", "text": "0", "flex": after, **small},
            {"type": "text", "text": str(after), "flex": scale - after, **small}]},
        {"type": "text", "text": "時數", "flex": hours_part, "align": "end", **small},
        {"type": "text", "text": "加班", "flex": over_part, "align": "end", **small}]}
    rows = {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "sm", "contents": [
        overtime_row(date(month.year, month.month, number), found.get(number), scale, now=number == last,
                     soon=number == last + 1) for number in range(1, length + 1)]}
    keys = (("regular", f"{after} 小時內　　"), ("over", f"超過 {after} 小時＝加班"))   # one line of spans: it is small
    legend = {"type": "text", "text": "".join(f"● {label}" for _, label in keys), "margin": "lg", **small,
              "contents": [span for kind, label in keys for span in (
                  {"type": "span", "text": "● ", "color": OVERTIME_UI[kind], "size": "sm"},
                  {"type": "span", "text": label})]}
    return _panel("🗓️ 每天的時數", f"{month.month} 月 1–{length} 日", [
        scale_row, {"type": "separator", "margin": "xs", "color": CUTE["edge"]}, rows, legend])


def _pay_tile(overtime: float, wage: float | None) -> dict:
    """加班費: tapping it opens the keyboard on 「時薪 」; without a wage the tile is that invitation."""
    tile = {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "paddingAll": "10px",
            "cornerRadius": "14px", "action": keyboard_action("改時薪" if wage else "設定時薪", "wage", "時薪 ")}
    if wage is None:
        return {**tile, "backgroundColor": CUTE["panel"], "borderWidth": "2px", "borderColor": OVERTIME_UI["over"],
                "contents": [
                    {"type": "text", "text": "✏️", "size": "lg", "align": "center"},
                    {"type": "text", "text": "設定時薪", "size": "lg", "weight": "bold", "align": "center",
                     "color": CUTE["title"]},
                    {"type": "text", "text": "點我，算出加班費", "size": "xxs", "align": "center",
                     "color": CUTE["muted"]}]}
    start, end = OVERTIME_UI["pay"]
    return {**tile, "background": {"type": "linearGradient", "angle": "135deg", "startColor": start, "endColor": end},
            "backgroundColor": start, "contents": [
                {"type": "text", "text": "💰", "size": "lg", "align": "center"},
                {"type": "text", "text": money(overtime * wage), "size": "xl", "weight": "bold", "align": "center",
                 "color": CUTE["ink"], "adjustMode": "shrink-to-fit"},
                {"type": "text", "text": "加班費（試算）", "size": "xxs", "align": "center", "color": CUTE["muted"]}]}


def overtime_notes(month) -> list[str]:
    return [f"🧮 加班＝每天超過 {monthly_stats.OVERTIME_AFTER} 小時的部分；加班費＝加班時數 × 時薪，只是試算："
            "沒有乘上 1.34／1.67 倍的加班費率，實際以單位核發為準。", *stats_notes(month)]


def overtime_card(staff_name: str, month, wage: float | None) -> dict:
    """「加班」「月底結算」: this month (a ``monthly_stats.Month``, up to today) in four tiles — days on duty, hours,
    overtime and what it pays at ``wage`` — and then every date of the month with its hours.

    It is the month so far; on the month's last day it is the whole month (月底結算). The notes say what the
    total rests on, as 「本月統計」 does, and that the pay is an estimate.
    """
    kinds, hours = monthly_stats, monthly_stats.hours_text
    worked = month.of(kinds.DUTY)
    total, overtime = month.hours, month.overtime
    first, last = month.days[0].day, month.days[-1].day
    whole = last.day == calendar.monthrange(month.year, month.month)[1]
    if not worked:
        cheer = "🌱 這段期間的班表上沒有找到你的勤務。"
    elif overtime:
        cheer = f"到 {last.month}/{last.day} 加班 {hours(overtime)} 小時，辛苦了！🍵"
    else:
        cheer = f"到 {last.month}/{last.day} 每天都沒有超過 {kinds.OVERTIME_AFTER} 小時 👍"
    start, end = OVERTIME_UI["band"]
    header = {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "135deg", "startColor": start, "endColor": end},
              "contents": [
                  {"type": "text", "text": "💰 本月結算", "weight": "bold", "size": "sm", "color": CUTE["title"]},
                  {"type": "text", "text": f"{staff_name} 的 {month.month} 月", "weight": "bold", "size": "xl",
                   "wrap": True, "color": CUTE["ink"]},
                  {"type": "text", "text": f"{day_label(first)}–{day_label(last)}｜"
                                           + ("整個月都算好了 🎉" if whole else "算到今天，月底就完整了"),
                   "size": "xs", "wrap": True, "color": CUTE["muted"]},
                  {"type": "text", "text": cheer, "size": "sm", "wrap": True, "color": CUTE["ink"], "margin": "md"}]}
    tiles = [_tile("📅", "上班天數", str(len(worked)), "天", OVERTIME_UI["days"]),
             _tile("⏰", "總時數", hours(total), "小時", OVERTIME_UI["hours"]),
             _tile("🌙", "加班時數", hours(overtime), "小時", OVERTIME_UI["extra"]),
             _pay_tile(overtime, wage)]
    if wage is None:
        sum_up = "🧮 加班費＝加班時數 × 時薪。點「設定時薪」輸入一次，之後每次都會幫你算。"
    else:
        sum_up = f"🧮 加班 {hours(overtime)} 小時 × 時薪 {wage_text(wage)}＝{money(overtime * wage)}"
    body = [*({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "sm" if index else "none",
               "contents": tiles[index:index + 2]} for index in (0, 2)),
            {"type": "text", "text": sum_up, "size": "xs", "wrap": True, "color": CUTE["ink"], "margin": "md"},
            overtime_days(month),
            {"type": "text", "text": "\n".join(overtime_notes(month)), "size": "xxs", "wrap": True,
             "color": CUTE["muted"], "margin": "lg"}]
    alt = (f"{month.month} 月結算｜{staff_name}｜上班 {len(worked)} 天、{hours(total)} 小時、加班 {hours(overtime)} 小時｜"
           + (f"加班費試算 {money(overtime * wage)}（時薪 {wage_text(wage)}）" if wage is not None else "還沒設定時薪"))
    return {"type": "flex", "altText": alt[:400], "contents": {"type": "bubble", "size": "giga", "header": header,
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "backgroundColor": CUTE["page"],
                 "contents": body},
        "styles": {"header": {"backgroundColor": start}}}}


# ---------------------------------------------------------------- 我的班表: the next hour, today and tomorrow
# One card instead of the three old buttons 今天班表／明天班表／下個時段, whose words still lead to it. With a date
# (「9/30」「今天」「我的班表 9/30」) the words are still that day's ``duty_card``.
MY_DAY = MY_ROSTER + ("今天班表", "今日班表", "明天班表", "明日班表", "下個時段", "下一個時段", "下個小時", "下一個小時")
MY_DAY_UI = {   # with STAFF_UI and SLOT_UI (the warm ring of 「現在」); every state also says itself in words
    "time": "#E6EFFB", "past": "#EEF1F5", "off": "#F0ECFB", "unmatched": "#FFF1DC", "missing": "#E9EEF4",
}
MY_DAY_BUDGET = 25_000   # bytes of the card past which today's tiles become lines (LINE refuses a bubble over 30 KB)


@dataclass
class MyDay:
    """One day of 「我的班表」: what that day's roster says about the registered user.

    No ``roster``: that day's has not been uploaded yet. No ``code``: their name is not on its name table. A
    ``person`` without slots, or none: they are off that day (the leave table may say 輪休 or which leave).
    """
    day: date
    roster: dict | None = None
    result: dict | None = None
    code: str | None = None
    person: dict | None = None

    @property
    def state(self) -> str:
        if not self.roster:
            return "missing"
        if not self.code:
            return "unmatched"
        return "duty" if self.person and self.person.get("slots") else "off"

    def blocks(self, threshold: float = .75) -> list[dict]:
        return duty_blocks(self.person, threshold=threshold)[0] if self.state == "duty" else []

    def hours(self, threshold: float = .75) -> dict[int, dict]:
        """Starting hour → that hour's duty (``duty_hours``)."""
        return ({int(hour["start"]): hour for hour in duty_hours(self.person, threshold=threshold)}
                if self.state == "duty" else {})

    @property
    def absence(self) -> str:
        return absence_kind(self.code, self.result or {})

    @property
    def leave_read(self) -> bool:
        return bool((self.result or {}).get("leave_table", {}).get("available"))


def _duty_words(entry: dict) -> str:
    return entry["duties"] + (" ？" if entry["low"] else "")


def _after_today(info: MyDay, threshold: float) -> str:
    """What tomorrow holds, in a line for the 「下個小時」 panel once today has nothing more."""
    state = info.state
    if state == "duty":
        first = info.blocks(threshold)[0]
        return f"👉 明天第一段 {first['start']}–{first['end']}：{_duty_words(first)}"
    if state == "off":
        absence = info.absence
        return f"💤 明天{absence}" if absence == "輪休" else f"🌴 明天{absence}" if absence else "🍃 明天沒有排勤務"
    return "🔍 明天的班表上找不到你" if state == "unmatched" else "🌙 明天的班表還沒上傳"


def next_hour(now: datetime, today_info: MyDay, tomorrow_info: MyDay, *, threshold: float = .75) -> dict:
    """The 「下個小時」 of 「我的班表」 at the Taiwan time ``now``: the next hour's duty, or that it has none and what
    comes after; and the duty of the hour under way. From 23:00 the next hour is tomorrow's 00–01, which no roster
    has, so what comes after is tomorrow's first duty.

    Words for the card and its altText: ``label`` (「⏭️ 下個小時 11–12」), ``value`` (the duty, or 「休息 ☕」,
    「下班 🎉」, 「沒有勤務」, or why it is not known), ``duty`` (the value is a duty), ``now`` and ``then`` (or None).
    """
    hour, upcoming = now.hour, now.hour + 1
    late = upcoming > 23
    label = f"⏭️ 下個小時 {upcoming % 24:02d}–{upcoming % 24 + 1:02d}" + ("（明天）" if late else "")
    hours = today_info.hours(threshold)
    current, following = hours.get(hour), None if late else hours.get(upcoming)
    later = [] if late else [b for b in today_info.blocks(threshold) if int(b["start"]) > upcoming]
    words = {"label": label, "duty": bool(following), "value": "", "then": None,
             "now": f"▶️ 現在 {hour:02d}–{hour + 1:02d}：{_duty_words(current)}" if current else None}
    if following:
        words["value"] = _duty_words(following)
        return words
    if late or today_info.state in ("duty", "off"):
        words["value"] = ("休息 ☕" if later else "下班 🎉") if current else "沒有勤務"
    else:
        words["value"] = "今天的班表還沒上傳" if today_info.state == "missing" else "今天的班表上找不到你"
    first = later[0] if later else None
    words["then"] = (f"👉 下一段 {first['start']}–{first['end']}：{_duty_words(first)}" if first
                     else _after_today(tomorrow_info, threshold))
    return words


def _duty_text(entry: dict, *, size: str, color: str) -> dict:
    """A duty in bold, with a warm 「？」 when one of its cells is unsure (the notes say what it means)."""
    duty = {"type": "text", "text": _duty_words(entry), "size": size, "weight": "bold", "wrap": True, "color": color}
    if entry["low"]:
        duty["contents"] = [{"type": "span", "text": entry["duties"]},
                            {"type": "span", "text": " ？", "color": SLOT_UI["now"]}]
    return duty


def _block_tile(block: dict, *, past: bool, current: bool, tag: str | None) -> dict:
    """One stretch of today's duty: its hours in a pill, the duty, and how long. The hour under way has the warm
    ring and 「▶ 現在」; a stretch already over is greyed and ticked; the first one still to come says 「下一段」."""
    ink = SLOT_UI["past"] if past else STAFF_UI["ink"]
    duty = {**_duty_text(block, size="md", color=ink), "flex": 1}
    if tag:
        duty = {"type": "box", "layout": "vertical", "flex": 1, "contents": [duty, {
            "type": "text", "text": tag, "size": "xxs", "weight": "bold",
            "color": SLOT_UI["now"] if current else STAFF_UI["navy"]}]}
    tile = {"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm", "paddingAll": "10px",
            "cornerRadius": "12px", "alignItems": "center",
            "backgroundColor": MY_DAY_UI["past"] if past else STAFF_UI["white"], "contents": [
                {"type": "box", "layout": "vertical", "flex": 0, "width": "74px", "paddingTop": "6px",
                 "paddingBottom": "6px", "cornerRadius": "8px",
                 "backgroundColor": STAFF_UI["white"] if past else MY_DAY_UI["time"], "contents": [
                     {"type": "text", "text": ("✓ " if past else "") + f"{block['start']}–{block['end']}", "size": "sm",
                      "weight": "bold", "align": "center", "color": ink}]},
                duty,
                {"type": "text", "text": f"{int(block['end']) - int(block['start'])} 小時", "size": "xs", "flex": 0,
                 "color": STAFF_UI["muted"]}]}
    if current:
        tile.update(borderWidth="2px", borderColor=SLOT_UI["now"])
    elif not past:
        tile.update(borderWidth="1px", borderColor=STAFF_UI["line"])
    return tile


def _rest_row(start: int, end: int, *, now: bool = False) -> dict:
    """A break between two stretches of duty; the one under way says so, in the warm colour of 「現在」."""
    row = {"type": "text", "text": f"☕ {start:02d}–{end:02d} 休息 {end - start} 小時", "size": "xxs", "align": "center",
           "color": STAFF_UI["muted"], "margin": "sm"}
    if now:
        row.update(text=f"▶ 現在 {row['text']}", weight="bold", color=SLOT_UI["now"])
    return row


def _timeline(blocks: list[dict], hour: int | None = None) -> tuple[list[dict], set[str]]:
    """Stretches of duty in order as tiles, with the breaks between them; and which marks (▶ ✓ ？) were used.

    ``hour`` is the Taiwan hour now when the day is today: the stretch (or break) under way then says so in the warm
    colour of 「現在」, those over are ticked and greyed, and the first one still to come says 「下一段」."""
    rows, marks, previous, tagged = [], set(), None, False
    today = hour is not None
    for block in blocks:
        start, end = int(block["start"]), int(block["end"])
        if previous is not None and start > previous:
            resting = today and previous <= hour < start
            rows.append(_rest_row(previous, start, now=resting))
            marks |= {"now"} if resting else set()
        past, current = today and end <= hour, today and start <= hour < end
        tag = "▶ 現在" if current else None
        if today and not past and not current and not tagged:
            tag, tagged = "⏭️ 下一段", True
        rows.append(_block_tile(block, past=past, current=current, tag=tag))
        marks |= {"now"} if current else {"past"} if past else set()
        marks |= {"low"} if block["low"] else set()
        previous = end
    return rows, marks


def _compact_day(blocks: list[dict], hour: int | None = None) -> tuple[dict, set[str]]:
    """Stretches of duty one line each in a single panel: tomorrow's on 「我的班表」, a glance (today has the detail).

    With ``hour`` they are today's, when their tiles would not fit LINE's size limit: the stretch under way then says
    「▶」 in the warm colour of 「現在」 and those already over are ticked and greyed, as their tiles would be.
    """
    rows, marks = [], set()
    for index, block in enumerate(blocks):   # lean: up to 19 of these, and the card has a size limit
        start, end = int(block["start"]), int(block["end"])
        past = hour is not None and end <= hour
        current = hour is not None and start <= hour < end
        mark, color = ("▶ ", SLOT_UI["now"]) if current else ("✓ ", SLOT_UI["past"]) if past else ("", STAFF_UI["navy"])
        if index:
            rows.append({"type": "separator", "margin": "sm", "color": STAFF_UI["line"]})
        rows.append({"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm" if index else "none",
                     "contents": [
                         {"type": "text", "text": f"{mark}{block['start']}–{block['end']}", "size": "sm",
                          "weight": "bold", "color": color, "flex": 3 if hour is None else 4},
                         {**_duty_text(block, size="sm", color=SLOT_UI["past"] if past else STAFF_UI["ink"]), "flex": 9},
                         {"type": "text", "text": f"{end - start} 小時", "size": "xxs", "flex": 2, "align": "end",
                          "gravity": "center", "color": STAFF_UI["muted"]}]})
        marks |= {"now"} if current else {"past"} if past else set()
        marks |= {"low"} if block["low"] else set()
    return {"type": "box", "layout": "vertical", "margin": "sm", "paddingAll": "12px", "cornerRadius": "12px",
            "backgroundColor": STAFF_UI["white"], "borderWidth": "1px", "borderColor": STAFF_UI["line"],
            "contents": rows}, marks


def _status_tile(emoji: str, title: str, note: str, background: str, *, extra: list | None = None,
                 action: dict | None = None) -> dict:
    tile = {"type": "box", "layout": "horizontal", "spacing": "md", "margin": "sm", "paddingAll": "14px",
            "cornerRadius": "14px", "backgroundColor": background, "contents": [
                {"type": "text", "text": emoji, "size": "xl", "flex": 0, "gravity": "center"},
                {"type": "box", "layout": "vertical", "flex": 1, "spacing": "xs", "contents": [
                    {"type": "text", "text": title, "weight": "bold", "size": "md", "wrap": True,
                     "color": STAFF_UI["ink"]},
                    {"type": "text", "text": note, "size": "xs", "wrap": True, "color": STAFF_UI["muted"]},
                    *(extra or [])]}]}
    if action:
        tile["action"] = action
    return tile


def _day_status(info: MyDay, which: str, staff_name: str, latest: dict | None = None) -> dict:
    """A day without duties to list: not uploaded yet, the name not on its name table, or off (輪休, a leave, or
    nothing the leave table says: then it says only that, the leave table may well not have been read)."""
    state, when = info.state, f"{info.day.month}/{info.day.day}"
    if state == "missing":
        extra = []
        if latest:   # nothing for today or tomorrow: the newest roster is a tap away, as 「我的班表」 used to show
            day = roster_day(latest)
            shown = f"{day_label(day)}的班表" if day else "日期未辨識的那張"
            extra.append({"type": "text", "text": f"👉 先看最新的一張：{shown}", "size": "xs", "weight": "bold",
                          "wrap": True, "color": STAFF_UI["navy"], "margin": "sm", "action": {
                              "type": "postback", "label": "看最新的班表", "data": f"mine|{latest['run_id']}",
                              "displayText": f"我的班表 {day.month}/{day.day}" if day else "我的班表（最新的一張）"}})
        return _status_tile("☀️" if which == "今天" else "🌙", f"{which}的班表還沒上傳",
                            "上傳後再點「我的班表」就看得到。", MY_DAY_UI["missing"], extra=extra)
    if state == "unmatched":   # a tap opens the keyboard on 「我是 10/8 」: the date keeps the claim on this day's photo
        return _status_tile("🔍", f"{which}的班表上找不到你",
                            f"姓名對照表上沒有「{staff_name}」（可能休假，或字沒辨識清楚）。", MY_DAY_UI["unmatched"],
                            extra=[{"type": "text", "text": f"✏️ 有上班就傳「我是 代號 {when}」", "size": "xs",
                                    "weight": "bold", "wrap": True, "color": STAFF_UI["navy"], "margin": "sm"}],
                            action={"type": "postback", "label": "我是 代號", "data": "fill:我是",
                                    "inputOption": "openKeyboard", "fillInText": f"我是 {when} "})
    return _off_status(info.absence, info.leave_read, which)


def _off_status(absence: str, leave_read: bool, which: str, who: str = "你") -> dict:
    """A day off (``absence_kind``): 輪休, a leave (its kind, or only that it is one), or nothing the leave table says,
    when it says only that: the leave table may well not have been read. ``who``: 你, or the name looked up."""
    if absence == "輪休":
        return _status_tile("💤", f"{which}輪休", "班表右下角的休假區記載：輪休。", MY_DAY_UI["off"])
    if absence == "請假":
        return _status_tile("🌴", f"{which}請假", "班表右下角記載請假，假別沒有辨識出來。", MY_DAY_UI["off"])
    if absence:
        return _status_tile("🌴", f"{which}{absence}", f"班表右下角的休假區記載：{absence}。", MY_DAY_UI["off"])
    return _status_tile("🍃", f"{which}沒有排勤務", f"班表上沒有{who}的勤務，休假區也沒有寫到{who}。" if leave_read
                        else f"班表上沒有{who}的勤務；休假區沒有辨識出來，是輪休還是請假請對照原圖。", MY_DAY_UI["off"])


def _day_side(info: MyDay, threshold: float) -> str:
    if info.state == "duty":
        return f"代號 {info.code}・共 {len(info.hours(threshold))} 小時"
    return f"代號 {info.code}" if info.code else ""


def _day_summary(info: MyDay, threshold: float) -> str:
    """A day in a few words for the altText."""
    if info.state == "duty":
        return "、".join(f"{b['start']}–{b['end']} {_duty_words(b)}" for b in info.blocks(threshold))
    return {"missing": "班表還沒上傳", "unmatched": "班表上找不到你"}.get(info.state) or info.absence or "沒有排勤務"


def my_day_card(staff_name: str, now: datetime, today_info: MyDay, tomorrow_info: MyDay, *, threshold: float = .75,
                photo: date | None = None, latest: dict | None = None) -> dict:
    """「我的班表」 on one card: the next hour in the band at the top (``next_hour``), today as a timeline of duty
    stretches (the one under way ringed, those over greyed, the breaks between them), then tomorrow in a few lines,
    or why there is nothing to list (not uploaded yet, the name not found, off).

    ``now`` is Taiwan time (only its hour and minute are used: the days are the infos'). ``photo`` is the day of the
    roster whose blue-outlined photo follows the card. ``latest`` is the newest roster the user can see when it is
    neither today's nor tomorrow's: today's empty state offers it.

    A day has at most 19 hours on a roster (05–24). Should every one of them be another duty on both days, today's
    tiles would take the card past what LINE accepts: today is then listed in lines like tomorrow (``MY_DAY_BUDGET``).
    """
    words = next_hour(now, today_info, tomorrow_info, threshold=threshold)
    panel = [{"type": "text", "text": words["label"], "size": "sm", "weight": "bold", "wrap": True, "color": "#E4EEF8"},
             {"type": "text", "text": words["value"], "size": "xxl" if words["duty"] else "xl", "weight": "bold",
              "wrap": True, "color": STAFF_UI["white"]}]
    panel += [{"type": "text", "text": words[key], "size": "xs", "wrap": True, "color": STAFF_UI["white"],
               "margin": "sm"} for key in ("now", "then") if words[key]]
    header = _hero(f"📅 {staff_name}的班表", f"{day_label(today_info.day)}・現在 {now:%H:%M}", [
        {"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "14px", "cornerRadius": "14px",
         "spacing": "xs", "backgroundColor": STAFF_UI["glass"], "contents": panel}])
    notes = [f"⚠️ {error}" for error in dict.fromkeys(
        info.result.get("duty_recheck_error") for info in (today_info, tomorrow_info)
        if info.result and info.result.get("duty_recheck_error"))]
    if photo:
        notes.append(f"📷 下方是 {day_label(photo)}的標示圖：藍框是你的勤務")
    example = today_info.day - timedelta(days=1)
    notes.append(f"💡 其他日期直接輸入日期，例如 {example.month}/{example.day}")
    for dense in (False, True):
        body, marks = [], set()
        for info, which, emoji in ((today_info, "今天", "☀️"), (tomorrow_info, "明天", "🌙")):
            body.append(_section(f"{emoji} {which} {day_label(info.day)}", _day_side(info, threshold)))
            if info.state != "duty":
                body.append(_day_status(info, which, staff_name, latest if which == "今天" else None))
                continue
            if which == "今天" and not dense:
                rows, used = _timeline(info.blocks(threshold), now.hour)
            else:
                box, used = _compact_day(info.blocks(threshold), now.hour if which == "今天" else None)
                rows = [box]
            body += rows
            marks |= used
        body[0] = {**body[0], "margin": "none"}
        legend = [key for mark, key in (("now", "▶ 現在"), ("past", "✓ 已結束"), ("low", "？ 待確認，請對照原圖"))
                  if mark in marks]
        body.append({"type": "text", "text": "\n".join((["　".join(legend)] if legend else []) + notes), "size": "xxs",
                     "wrap": True, "color": STAFF_UI["muted"], "margin": "lg"})
        bubble = {"type": "bubble", "size": "giga", "header": header,
                  "body": {"type": "box", "layout": "vertical", "paddingAll": "16px",
                           "backgroundColor": STAFF_UI["page"], "contents": body},
                  "styles": {"header": {"backgroundColor": STAFF_UI["navy"]}}}
        if flex_size(bubble) <= MY_DAY_BUDGET:
            break
    alt = [f"📅 {staff_name}｜{day_label(today_info.day)}", f"{words['label']}：{words['value']}",
           *(words[key] for key in ("now", "then") if words[key]),
           f"今天：{_day_summary(today_info, threshold)}", f"明天：{_day_summary(tomorrow_info, threshold)}"]
    return {"type": "flex", "altText": "｜".join(alt)[:400], "contents": bubble}


# ---------------------------------------------------------------- one day's duty card
# One person on one roster: a typed date (「10/1」「今天」), 查 72／查 姓名, the 今天／明天 of 「我的班表」, a date tapped on
# 「班表月曆」 and the pushes of a new roster. In the house style of 「我的班表」: the band says who and which day, the
# day's stretches of duty are tiles with the breaks between them, and a day without any is one calm tile that says why.
# A date that is not the one read off the photo says so on the card, where a wrong one is seen.
DUTY_DATES = 6          # other dates offered as chips under the card: the newest ones
DUTY_DATE_COLUMNS = 3   # chips per row


def photo_date(roster: dict, result: dict | None = None) -> date | None:
    """The date read off the photo's header: ``read_date``, or for a row saved before that column existed, the reading
    kept with the result. None when the header could not be read."""
    return iso_day(roster.get("read_date") or ((result or {}).get("run") or {}).get("roster_date"))


def date_note(roster: dict, result: dict | None = None) -> str:
    """「📅 這張的日期是後來設定的（照片上讀到 9/29）」 when the date in use is not the one read off the photo: someone
    set it later (「日期」, a date picker, an administrator's button). Empty when it is the one read, or there is none."""
    day, read = roster_day(roster), photo_date(roster, result)
    if not day or read == day:
        return ""
    return f"📅 這張的日期是後來設定的（{f'照片上讀到 {read.month}/{read.day}' if read else '照片上沒有讀到日期'}）"


def other_dates_text(others, ask: str = "") -> str:
    """The other dates in words, the newest three, for an answer that is text (a name not on the name table).
    ``ask``: what goes before the date to see them (「查 王小明 」 for a colleague; nothing for one's own)."""
    newest = sorted(others, reverse=True)[:3]
    if not newest:
        return ""
    example = f"{ask}{newest[0].month}/{newest[0].day}"
    return (f"\n\n其他日期：{'、'.join(day_label(day) for day in newest)}"
            f"\n（直接輸入「{example}」，就能查看）" if ask else
            f"\n\n其他日期：{'、'.join(day_label(day) for day in newest)}"
            f"\n（直接輸入日期，例如 {example}，就能查看）")


def _date_chip(day: date, now: datetime, value: str) -> dict:
    """Another date as a chip; a tap sends ``value`` (「10/11」, 「查 王小明 10/11」) as if it were typed. Today's is
    tinted like the hours of a duty tile."""
    return {"type": "box", "layout": "vertical", "flex": 1, "paddingTop": "8px", "paddingBottom": "8px",
            "cornerRadius": "12px", "backgroundColor": MY_DAY_UI["time"] if day == now.date() else STAFF_UI["white"],
            "borderWidth": "1px", "borderColor": STAFF_UI["line"], "action": message_action(f"{day.month}/{day.day}", value),
            "contents": [
                {"type": "text", "text": f"{day.month}/{day.day}", "size": "md", "weight": "bold", "align": "center",
                 "color": STAFF_UI["navy"]},
                {"type": "text", "text": f"週{WEEKDAYS[day.weekday()]}{_relative_day(day, now)}", "size": "xxs",
                 "align": "center", "color": STAFF_UI["muted"]}]}


def duty_card(code: str, name: str | None, roster: dict, result: dict, *, threshold: float = .75,
              now: datetime | None = None, mine: bool = False, news: str | None = None, change: str = "",
              others=(), redate: bool = False, photo: bool = False, show_original: bool = False) -> dict:
    """One person's duties on one roster, on a giga bubble.

    The band: who (name and code, under 「📋 我的班表」, 「🔎 同仁班表」 or, on a push, ``news`` such as 「📌 新班表」, with
    ``change``: how the day differs from the version it replaces), the day (今天／明天／昨天 when it is one, in Taiwan
    time ``now``) and, on a day on duty, when it starts and ends and the hours. Then the stretches of duty as tiles,
    the breaks between them (``_timeline``): on today's roster the one under way is ringed in the warm colour of
    「現在」, those over are greyed and the next one is tagged. A day without duties is one calm tile: 輪休, a leave, on
    the roster with nothing to do, or the code not on it at all (``mine``: worded to 你).

    A date not read off the photo says so in the band (``date_note``), and ``redate`` (an administrator is looking)
    adds 「📅 改這張的日期」: LINE's date picker on this very photo. ``others`` are other dates with a roster, as chips
    that send the date (one's own card; someone else's follows their name: codes change every day, so a code alone
    gets none). ``photo``: the blue-outlined photo follows the card; ``show_original``: 「查看整張班表」 at its foot.

    The altText is ``format_duties``, 「林小明（72）　10/1（四）」 and the day, so the card reads as before in a
    notification and to a search. Should every hour be another duty, the tiles become lines (``MY_DAY_BUDGET``).
    """
    now = now or taipei_now()
    day = roster_day(roster)
    title = f"{name}（{code}）" if name else f"代號 {code}"
    person = next((p for p in result.get("people", []) if p["code"] == code), None)
    blocks, hours = duty_blocks(person, threshold=threshold) if person else ([], 0)
    hour = now.hour if day == now.date() else None
    band = [{"type": "text", "text": news or ("📋 我的班表" if mine else "🔎 同仁班表"), "size": "xs", "weight": "bold",
             "color": "#E4EEF8"},
            {"type": "text", "text": title, "size": "xl", "weight": "bold", "wrap": True, "color": STAFF_UI["white"]},
            _pill_row([_day_pill(day, now)])]
    if blocks:
        band.append({"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": [
            _stat(f"{blocks[0]['start']}:00", "上班"), _stat(f"{blocks[-1]['end']}:00", "下班"),
            _stat(f"{hours} 小時", "排定時數")]})
    if change:
        band.append({"type": "box", "layout": "vertical", "margin": "md", "paddingAll": "8px", "cornerRadius": "10px",
                     "backgroundColor": STAFF_UI["glass"], "contents": [
                         {"type": "text", "text": f"🔁 {change}", "size": "xs", "weight": "bold", "wrap": True,
                          "color": STAFF_UI["white"]}]})
    note = date_note(roster, result)
    if note:
        band.append({"type": "text", "text": note, "size": "xxs", "wrap": True, "margin": "md", "color": "#E4EEF8"})
    header = {"type": "box", "layout": "vertical", "paddingAll": "18px", "spacing": "xs",
              "background": {"type": "linearGradient", "angle": "120deg", "startColor": STAFF_UI["navy"],
                             "endColor": STAFF_UI["teal"]}, "contents": band}
    if blocks:
        status = None
    else:
        which = {-1: "昨天", 0: "今天", 1: "明天"}.get((day - now.date()).days, "這天") if day else "這天"
        absence = absence_kind(code, result)
        status = (_off_status(absence, bool(result.get("leave_table", {}).get("available")), which,
                              "你" if mine else name) if name or absence else
                  _status_tile("🔍", f"這張班表上沒有辨識到代號 {code}", "可能休假，或照片中看不清楚；請對照整張班表。",
                               MY_DAY_UI["missing"]))
    ask = "" if mine else f"查 {name} " if name else None
    chips = [] if ask is None else [_date_chip(other, now, f"{ask}{other.month}/{other.day}") for other in others]
    for dense in (False, True):
        if status:
            body, marks = [status], set()
        elif dense:
            box, marks = _compact_day(blocks, hour)
            body = [box]
        else:
            body, marks = _timeline(blocks, hour)
        legend = [key for mark, key in (("now", "▶ 現在"), ("past", "✓ 已結束"), ("low", "？ 待確認，請對照原圖"))
                  if mark in marks]
        notes = (["　".join(legend)] if legend else []) + (["📷 下方是標示圖：藍框是勤務格"] if photo else [])
        if result.get("duty_recheck_error"):
            body.append({"type": "box", "layout": "vertical", "margin": "lg", "paddingAll": "10px",
                         "cornerRadius": "12px", "backgroundColor": STAFF_UI["warn"], "contents": [
                             {"type": "text", "text": f"⚠️ {result['duty_recheck_error']}", "size": "xs", "wrap": True,
                              "color": STAFF_UI["ink"]}]})
        if notes:
            body.append(_notes(notes))
        if chips:
            body += [_section("📆 其他日期", "點一下就看那天"), *_rows(chips, DUTY_DATE_COLUMNS)]
        if redate:   # an administrator's: the same picker as on the report of an upload, on this very photo
            body.append({"type": "box", "layout": "horizontal", "spacing": "md", "margin": "xl", "paddingAll": "12px",
                         "cornerRadius": "12px", "backgroundColor": STAFF_UI["chip"], "action": redate_picker(roster),
                         "contents": [
                             {"type": "text", "text": "📅 改這張的日期", "size": "sm", "weight": "bold", "flex": 0,
                              "color": STAFF_UI["navy"]},
                             {"type": "text", "text": "只改這一張｜管理員才看得到", "size": "xxs", "wrap": True,
                              "align": "end", "gravity": "center", "color": STAFF_UI["muted"]}]})
        bubble = _list_bubble(header, body, original_roster_link(roster) if show_original else None)
        if flex_size(bubble) <= MY_DAY_BUDGET:
            break
    heading = f"{news}｜{title}" + (f"・{change}" if change else "") if news else None
    return {"type": "flex", "altText": format_duties(code, name, roster, result, threshold=threshold,
                                                     heading=heading)[:400], "contents": bubble}


# ---------------------------------------------------------------- the bot
@dataclass
class Settings:
    public_base: str | None = None   # https URL serving saved roster images
    threshold: float = .75
    reply_window: float = 50.0       # seconds; LINE reply tokens are valid for one minute
    loading_refresh: float = 3.0     # seconds between typing-indicator re-sends while recognising
    images: bool = True              # attach the blue-outlined roster photo (needs public_base)
    staff_names: Path | None = None  # one staff name per line; snaps the name-table OCR
    fixed_staff_codes: Path | None = None  # authoritative Chinese code → name mappings
    admins: tuple[str, ...] = ()     # LINE user ids (``U`` + 32 hex, not LINE IDs) of the administrators
    notify: bool = False             # push each subscriber their duties when a group posts a roster; every
    #                                  such push counts against the LINE plan's monthly quota, so it is off
    push: bool = True                # False: never push (the free plan's 200 a month): what would be pushed to
    #                                  someone waits in their 收件匣 for their next reply, and ``notify`` is off
    #                                  whatever it says. Production starts with it off (LINE_V2_PUSH)

    def __post_init__(self):
        self.admins = (self.admins,) if isinstance(self.admins, str) else tuple(self.admins or ())


NO_ROSTER = "目前還沒有班表。等群組上傳班表後就能查；也可以私訊傳班表照片給我。"
NEED_NAME = f"你還沒登記姓名。請輸入「設定 你的姓名」（例如 設定 {EXAMPLE_NAME}）。"
NOTIFY_OFF = "自動通知目前停用，不會私訊新班表。\n有新班表時，請按下方選單的「我的班表」查看。"
NOTIFY_NO_PUSH = ("自動通知要用每月的推播額度，機器人目前設定成不推播（省下額度），所以自動通知是關的。\n"
                  "有新班表時，請按下方選單的「我的班表」查看。")
CODE_NOT_NAME = (f"請改用姓名登記，例如「設定 {EXAMPLE_NAME}」。\n"
                 "如果這是你在最新班表上的代號，請傳「我是 {code}」。")
NOT_ON_ROSTER = ("請確認姓名有沒有打錯字：要和隊上名冊上的姓名一模一樣。\n"
                 "新進同仁請先請幹部把你加入名冊（人員管理 → 新增人員），加好之後再傳一次「設定 你的姓名」。")
ADMIN_DATES = ("群組班表的日期由管理員更正；發現日期不對，請告訴管理員。\n"
               "「日期 月/日」只用來更正你自己私訊傳給我的班表照片。")
# Words that ``_handle_text`` answers, and that the 名冊守門員 lets through (``RosterBot._open_text``)
MY_SETTINGS = "我的設定"
MY_ID = ("我的id", "我的userid", "userid")   # compared case-folded: the value LINE_V2_ADMIN_USER_IDS takes
REGISTER = re.compile(r"(?:設定|綁定|登記|姓名|我是)[:：]?\s*(\S+)(?:\s+(\S+))?")   # 「我是 72」 claims a code instead
HELP_WORDS = ("用法說明", "說明", "使用說明", "用法", "help", "?", "？")
MORE_HELP_WORDS = ("進階說明", "完整說明", "其他指令", "所有指令")


def departures(flow: passenger_flow.Flow) -> bool:
    """Whether a 航班人數 message lists any departing flights: the forecast reads nothing else."""
    return any(rows for (_, way), rows in flow.series.items() if way == "dep")


class RosterBot:
    def __init__(self, analyzer, store: Store, transport: Transport, settings: Settings | None = None):
        self.analyzer, self.store, self.transport = analyzer, store, transport
        self.settings = settings or Settings()
        self.analysis_lock = threading.Lock()
        self._staff_lock = threading.Lock()   # one edit of the staff files at a time
        self._turn = threading.local()   # this thread's event: ``admin``, ``listed``, ``user`` (see ``_inbox``)
        self._gate_warned = False        # said once that the 名冊 is empty, so the 名冊守門員 lets everyone in
        self._menu_lock = threading.Lock()   # one rich menu change at a time (``sync_menu``)
        self._menus_warned = False       # said once that the 幹部／管理員 menus are not on LINE yet

    # -- entry point --------------------------------------------------------
    def handle(self, event: dict) -> None:
        """Handle one webhook event. Image analysis runs synchronously; call from a worker."""
        self._turn.admin = False   # worker threads are reused: nothing is carried over from their last event
        self._turn.listed = False
        self._turn.user = None     # whose 收件匣 a reply to this event carries (``_inbox``): a private event's sender
        source = event.get("source", {})
        user_id = source.get("userId")
        chat_id = source.get("groupId") or source.get("roomId") or user_id
        is_group = bool(source.get("groupId") or source.get("roomId"))
        if user_id and chat_id:
            self.store.seen(chat_id, user_id)
        kind = event.get("type")
        token = event.get("replyToken")
        if is_group:
            # Every group path is silent: no greeting on join, no replies, no errors in the group.
            message = event.get("message", {})
            if kind == "message" and message.get("type") == "image":
                return self._handle_image(event, chat_id, user_id, True)
            if kind == "message" and message.get("type") == "text":
                self._capture_flow(message.get("text", ""), chat_id, user_id, self._sent_at(event))
            return
        if not user_id:
            return
        self._turn.admin = self._is_admin(user_id)   # their reply also carries feedback and notices they have not seen
        self._turn.user = user_id                    # and what waits in their 收件匣
        if kind == "unfollow":
            self.store.set_notify(user_id, False)
            return
        try:
            return self._handle_private(event, kind, token, chat_id, user_id)
        finally:   # after the answer: asking LINE for a name must not stand between an event and its reply
            self._refresh_display(user_id)

    def _handle_private(self, event, kind, token, chat_id, user_id):
        """One event of a private chat (not an unfollow)."""
        if kind == "follow":
            try:
                return self._welcome(token, user_id)
            finally:   # after the reply: a 幹部 or an administrator who added the bot (again) gets their menu
                self.sync_menu(user_id, force=True)
        if not self._open_door(event) and not self._admitted(user_id):
            return self._reply(token, [self._gate(user_id)])   # 名冊守門員: nothing of the team before that
        if kind == "postback":
            postback = event.get("postback", {})
            return self._handle_postback(postback.get("data", ""), token, user_id, postback.get("params") or {})
        if kind != "message":
            return
        message = event.get("message", {})
        if message.get("type") == "image":
            return self._handle_image(event, chat_id, user_id, False)
        if message.get("type") == "text":
            return self._handle_text(message.get("text", ""), token, chat_id, user_id, False)

    def _refresh_display(self, user_id) -> None:
        """Keep the LINE display name of an account that has a staff name current (「綁定清單」 shows it): LINE is
        asked when the account writes, once a day at most. Whatever goes wrong, the event is still answered."""
        try:
            if not self.store.display_due(user_id, NAME_REFRESH):
                return
            self.store.set_display(user_id, self.transport.display_name(user_id, None))
        except Exception as exc:
            print(f"讀取 LINE 顯示名稱失敗（{user_id}）：{type(exc).__name__}: {exc}", flush=True)
            try:
                self.store.set_display(user_id, None)   # asked today: not again with every message
            except Exception:
                pass

    def _welcome(self, token, user_id):
        """A follow: 「歡迎回來」 for someone registered and admitted, the gate for someone whose name left the 名冊,
        and for a newcomer the card that asks for their name."""
        name = (self.store.user(user_id) or {}).get("staff_name")
        if name and self._admitted(user_id):   # back after blocking: already registered
            return self._reply(token, [{**text(f"歡迎回來，{name}！點下方選單的「我的班表」就能查看。"),
                                        "quickReply": {"items": [{"type": "action", "action": message_action("我的班表")}]}}])
        if name:   # back, but their name has left the 名冊 meanwhile
            return self._reply(token, [self._gate(user_id)])
        # A newcomer needs one thing first, their name: a card that says so, not the whole help
        return self._reply(token, [{**welcome_card(locked=bool(self._approvers(besides=user_id)),
                                                   gated=bool(self._roster_names())),
                                    "quickReply": {"items": [fill_action("✏️ 設定姓名", "設定 ")]}}])

    def _is_admin(self, user_id) -> bool:
        return bool(user_id) and user_id in self.settings.admins

    @property
    def _notifying(self) -> bool:
        """Whether automatic notifications are on: ``Settings.notify``, and never without pushes (``Settings.push``),
        which would fill everybody's 收件匣 instead."""
        return self.settings.notify and self.settings.push

    # -- 名冊守門員 -------------------------------------------------------------------
    def _roster_names(self) -> set[str]:
        """The 名冊: the names the 幹部 keep in 人員管理 (``Settings.staff_names``) and those of the fixed codes.

        Empty (no files, or nothing in them) turns the 名冊守門員 off, or nobody could ever register; a bot that was
        given the files says so once."""
        book = self._book()
        names = set(book.names()) | set(book.fixed().values())
        if not names and not self._gate_warned and (self.settings.staff_names or self.settings.fixed_staff_codes):
            self._gate_warned = True
            print(f"⚠ 名冊是空的（{self.settings.staff_names}）：名冊守門員暫停，任何人登記任何姓名都能查看班表。"
                  "請幹部在 LINE 用「人員管理」新增人員。", flush=True)
        return names

    def _admitted(self, user_id, roster: set[str] | None = None) -> bool:
        """Whether the 名冊守門員 lets ``user_id`` see the team: an administrator or a 幹部 always, anyone else once the
        name they registered is on the 名冊 (``roster``, read when not given), and everybody while there is none."""
        if self._can_manage(user_id):
            return True
        roster = self._roster_names() if roster is None else roster
        return not roster or (self.store.user(user_id) or {}).get("staff_name") in roster

    def _may_register(self, user_id, name: str, roster: set[str] | None = None) -> bool:
        """Whether ``name`` may become ``user_id``'s: a name on the 名冊 (any name while there is none). 幹部 and
        administrators are let in whatever their name, but they register names on the 名冊 like everyone: a typo kept
        on their account would lock them out the day they no longer hold the role."""
        roster = self._roster_names() if roster is None else roster
        return not roster or name in roster

    def _open_door(self, event) -> bool:
        """What the 名冊守門員 lets through before someone is admitted: the words registering needs (``_open_text``),
        the tiles and buttons that open the keyboard on 「設定 」／「意見 」, and what is never answered anyway (a
        sticker, a video…). A photo, every other postback (old cards included) and every other word show the team."""
        kind, message = event.get("type"), event.get("message", {})
        if kind == "postback":
            return event.get("postback", {}).get("data", "") in GATE_POSTBACKS
        if kind != "message":
            return True
        if message.get("type") == "text":
            return self._open_text(message.get("text", ""))
        return message.get("type") != "image"

    @staticmethod
    def _open_text(raw: str) -> bool:
        """The words open to everybody, read as ``_handle_text`` reads them: 「意見 …」, 我的設定, 收件匣 (what was filed
        for them: an uploader's notice, the answer to a name request), 我的ID, registering a name (not 「我是 72」, which
        claims a code on a roster, nor 姓名申請 or 設定時薪, answered before names are) and the help."""
        if re.sub(r"\s+", "", raw) not in FEEDBACK_LIST and FEEDBACK_PATTERN.fullmatch(raw.strip()):
            return True
        value = normalise(raw)
        compact = value.replace(" ", "")
        if compact in (MY_SETTINGS, *MAILBOX, *HELP_WORDS, *MORE_HELP_WORDS) or compact.casefold() in MY_ID:
            return True
        match = REGISTER.fullmatch(value)
        if not match or compact in NAME_REQUESTS + BINDING_LIST or WAGE_COMMAND.fullmatch(value):
            return False
        target = match.group(1)
        return not (compact.startswith("我是") and (parse_code(target) or re.fullmatch(DATE_TOKEN, normalise(target))))

    def _gate(self, user_id) -> dict:
        """``gate_card`` for someone not admitted. It names their own name only: the one they registered (which is
        then off the 名冊) and the one they are waiting for."""
        user, pending = self.store.user(user_id) or {}, self.store.pending_name_request(user_id)
        waiting = pending["requested_name"] if pending else None
        if waiting and (waiting not in self._roster_names() or not self._approvers(besides=user_id)):
            waiting = None   # nobody could approve it any more: the card asks them to register again instead
        return {**gate_card(removed=user.get("staff_name"), pending=waiting),
                "quickReply": {"items": [fill_action("✏️ 設定姓名", "設定 "),
                                         {"type": "action", "action": message_action("用法說明")},
                                         fill_action("意見回饋", "意見 ")]}}

    # -- names ------------------------------------------------------------------
    def names_for(self, roster: dict, result: dict) -> dict[str, str]:
        """Code → staff name: match daily OCR, retain fixed codes, then apply non-conflicting claims.

        Rosters uploaded before the name table was read, or read by an older (mis-aligning)
        version, are re-read once from their saved photo (``analyzer.ensure_names``).
        """
        ensure = getattr(self.analyzer, "ensure_names", None)
        if ensure and result.get("names_version") != NAMES_VERSION:
            try:
                result.update(ensure(roster["run_id"]))
            except Exception as exc:
                print(f"補讀姓名對照表失敗（{roster['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
                result["names"] = {}  # old versions may have put the correct name on the wrong code
                result.pop("names_blank", None)   # or a blank cell on the wrong code
        directory = self._directory(roster)
        fixed = self._book().fixed_on(roster_day(roster))   # a code set later does not rewrite older rosters
        names = match_names(result.get("names") or {}, directory, fixed_codes=fixed)
        for code, name in self.store.claims(roster["run_id"]).items():
            if name in fixed.values() and fixed.get(code) != name:
                continue  # an old daily claim must not move someone away from their fixed code
            if code in names and names[code] != name:
                continue  # retain the old claim for auditing, but do not overwrite a conflicting table entry
            names = {c: n for c, n in names.items() if n != name and c != code}
            names[code] = name
        return names

    def _directory(self, roster: dict) -> list[str]:
        """The names a reading on this roster may be matched to: the 名冊, whoever registered, and the 役男 who
        were replaced on their code after the roster's day (off the 名冊 since, but it is them it names)."""
        book, day = self._book(), roster_day(roster)
        gone = book.departed(day)   # a LINE account may still carry the name of a 役男 who had left by then
        return [name for name in load_directory(self.settings.staff_names) + self.store.staff_names()
                if name not in gone] + book.former(day)

    def code_for(self, staff_name: str, roster: dict, result: dict) -> str | None:
        return next((c for c, n in self.names_for(roster, result).items() if n == staff_name), None)

    def _roster(self, user_id, day=None) -> dict | None:
        return self.store.roster_on(user_id, day) if day else self.store.roster_for_user(user_id)

    def _person_roster(self, user_id, day=None) -> dict | None:
        """An undated person lookup follows the roster the user just viewed."""
        if day is not None:
            return self.store.roster_on(user_id, day)
        return self.store.recently_viewed_roster(user_id) or self._browse_roster(user_id)

    def _browse_roster(self, user_id, day=None) -> dict | None:
        """For staff and hour browsing, prefer today over an already-uploaded tomorrow."""
        if day is not None:
            return self.store.roster_on(user_id, day)
        current = today()
        for candidate in (current, current + timedelta(days=1)):
            roster = self.store.roster_on(user_id, candidate)
            if roster:
                return roster
        return self.store.roster_for_user(user_id)

    def _date_switch_actions(self, user_id, roster, kind, *, row=None, area=None, section=None) -> list[dict]:
        """Exact-run buttons let a reader switch today/tomorrow without losing the chosen view."""
        current = today()
        days = (("今天", current), ("明天", current + timedelta(days=1)))
        available = [(label, day, self.store.roster_on(user_id, day)) for label, day in days]
        available = [(label, day, target) for label, day, target in available if target]
        if len(available) < 2:
            return []
        actions = []
        for label, day, target in available:
            run = target["run_id"]
            command = {"slots": "查看時段", "staff": "勤務人員", "leave": "休假人員",
                       "area": "查看時段", "works": "查看工作", "work": "查看工作"}[kind]
            if kind == "area":
                data = f"area|{run}|{row}|{area}"
            elif kind == "work":
                data = f"work|{run}|{work_key(section)}"
            elif kind == "leave" and section:
                data = f"leave|{run}|{section}"
                command = "輪休人員" if section == "rest" else "請假人員"
            else:
                data = f"{kind}|{run}"
            actions.append({"type": "action", "action": {
                "type": "postback", "label": f"{'✓' if run == roster['run_id'] else '看'}{label} {day.month}/{day.day}",
                "data": data, "displayText": f"{command} {day.month}/{day.day}"}})
        return actions

    def _result(self, run_id):
        result = self.analyzer.result(run_id)
        ensure = getattr(self.analyzer, 'ensure_duties', None)
        if ensure and result.get('duty_version') != DUTY_VERSION:
            try:
                return ensure(run_id)
            except Exception as exc:
                print(f"補讀勤務代號失敗（{run_id}）：{type(exc).__name__}: {exc}", flush=True)
                result = {**result, 'duty_recheck_error': '這張舊班表的代號尚未複核，1／11、2／22、3／33 請對照原圖。'}
        return result

    def _missing_roster(self, token, user_id, day):
        dates = self.store.roster_dates(user_id)
        if not day or not dates:
            return self._reply(token, [text(NO_ROSTER)])
        return self._reply(token, [text(f"沒有 {day_label(day)}的班表。\n"
                                        f"可查的日期：{'、'.join(day_label(d) for d in dates)}")])

    def _help(self, advanced: bool = False, user_id=None) -> dict:
        return help_card(advanced, notify=self._notifying, locked=bool(self._approvers(besides=user_id)),
                         gated=bool(self._roster_names()), mailbox=not self.settings.push)

    def _audience(self) -> list[dict]:
        """Who is told about a group roster: the subscribers the 名冊守門員 lets in, and nobody while automatic
        notifications are off (``_notifying``: always without pushes)."""
        if not self._notifying:
            return []
        roster = self._roster_names()
        return [member for member in self.store.subscribers() if self._admitted(member["user_id"], roster)]

    def _need_name(self) -> dict:
        """「你還沒登記姓名」 with a button that opens the keyboard on 「設定 」 (the menu has no such button)."""
        return {**text(NEED_NAME), "quickReply": {"items": [fill_action("設定姓名", "設定 ")]}}

    # -- text commands --------------------------------------------------------
    def _handle_text(self, raw: str, token, chat_id, user_id, is_group):
        if is_group:
            return
        # Before everything else, and as typed: a message for the administrators may hold dates, names of
        # commands or several lines, none of which are meant for the bot.
        if re.sub(r"\s+", "", raw) in FEEDBACK_LIST:
            if self._is_admin(user_id):
                return self._list_feedback(token)
        else:
            feedback = FEEDBACK_PATTERN.fullmatch(raw.strip())
            if feedback:
                return self._receive_feedback(feedback.group(1), token, chat_id, user_id)
        flow = passenger_flow.parse(raw, near=today())   # before normalising: the message is many lines
        if flow:
            return self._receive_flow(flow, raw, token, user_id)
        value = normalise(raw)
        compact = value.replace(" ", "")
        if compact == MY_SETTINGS:
            user = self.store.user(user_id) or {}
            roster = self._roster_names()
            admitted = self._admitted(user_id, roster)   # 名冊守門員: open to all, so it also says how to get in
            lines = [f"姓名：{user.get('staff_name') or '尚未登記'}"]
            if user.get("staff_name") and not admitted:
                lines.append(f"⚠️「{user['staff_name']}」目前不在名冊上，暫時看不到班表；如果你還在隊上，請聯絡幹部。")
            pending = self.store.pending_name_request(user_id)
            if pending:
                lines.append(f"申請中：{'改成' if pending['current_name'] else '登記'}「{pending['requested_name']}」"
                             f"（{request_time(pending['created_at'])} 送出，等待確認）")
            wage = self.store.hourly_wage(user_id)
            if wage:   # for 加班結算
                lines.append(f"時薪：{wage_text(wage)}（輸入「時薪 200」可以改）")
            if self._notifying and admitted:
                lines.append(f"通知：{'開' if user.get('notify') else '關'}")
            if self._is_admin(user_id):
                waiting = len(self.store.held_rosters())
                lines.append("管理員：是" + (f"（{waiting} 張群組班表在等你確認日期）" if waiting else ""))
                if not self.settings.push:   # nothing is pushed: where the reports are
                    lines.append("辨識回報：放在收件匣，跟著你下一次的回覆送來；按選單「辨識回報」或傳「收件匣」可以直接看")
            if self.store.is_officer(user_id):
                lines.append("幹部：是（可以用「人員管理」增刪名冊、固定代號與幹部）")
            if self._linked_menu(user_id):
                lines.append(f"選單：{'管理員' if self.menu_role(user_id) == 'admin' else '幹部'}版（最下面一排「管理」）")
            asks = (sum(1 for request in self.store.pending_name_requests() if request["user_id"] != user_id)
                    if self._is_admin(user_id) else 0)   # 姓名申請 is the administrators' to answer
            if asks:
                lines.append(f"姓名申請：{asks} 筆等你確認（傳「姓名申請」）")
            locked = bool(user.get("staff_name")) and bool(self._approvers(besides=user_id))
            if not user.get("staff_name"):
                lines.append(f"\n輸入「設定 你的姓名」（例如 設定 {EXAMPLE_NAME}）登記姓名。")
                if roster:
                    lines.append(f"🗂️ {ROSTER_HELP}")
            elif self._notifying and admitted and not user.get("notify"):
                lines.append("\n輸入「通知 開」，群組有新班表時會私訊你的勤務。")
            if locked and not pending:
                lines.append("\n🔒 姓名登記後不能自己改；要改請按下方「申請改姓名」送出申請，同意後才會改。")
            label = "申請改姓名" if locked else "改姓名" if user.get("staff_name") else "設定姓名"
            actions = [fill_action(label, "設定 ")]
            if self._notifying and user.get("staff_name") and admitted:   # as buttons: the menu has none for them
                switch = "通知 關" if user.get("notify") else "通知 開"
                actions.append({"type": "action", "action": {"type": "message", "label": switch, "text": switch}})
            if asks:
                actions.append({"type": "action", "action": message_action("📮 姓名申請", "姓名申請")})
            if self._can_manage(user_id):   # everyone's menu has no button for it (theirs has, once linked)
                actions.append({"type": "action", "action": {"type": "message", "label": "人員管理", "text": "人員管理"}})
            return self._reply(token, [{**text("\n".join(lines)), "quickReply": {"items": actions}}])
        if compact in MAILBOX:
            return self._show_mailbox(token, user_id)
        if compact in NAME_REQUESTS:   # before 「姓名 …」 below would register 「申請」 as a name
            return self._show_name_requests(token, user_id)
        if compact.casefold() in MY_ID:
            return self._reply(token, [text(f"你的 LINE userId：\n{user_id}\n\n"
                                            "這不是加好友用的 LINE ID。設定管理員時要填的是這一串。")])
        if self._staff_command(value, compact, token, user_id):
            return
        if compact in MY_DAY:   # before dates are read: 「今天班表」「明天班表」 were menu buttons, not date queries
            return self._show_my_day(token, user_id)
        which = monthly_stats.which_month(value, today())
        if which:
            return self._show_stats(token, user_id, *which)
        if compact in OVERTIME_COMMANDS + OVERTIME_LAST_MONTH:
            return self._show_overtime(token, user_id, other=compact in OVERTIME_LAST_MONTH)
        wage = WAGE_COMMAND.fullmatch(value)   # before 「設定 …」, which would take 「設定時薪 200」 for a name
        if wage:
            return self._set_wage(token, user_id, wage.group(1).strip())
        match = REGISTER.fullmatch(value)
        if match:
            return self._register_or_claim(match.group(1), match.group(2), compact.startswith("我是"),
                                           token, chat_id, user_id)
        if compact in ("查看時段", "同時段", "同時段人員", "同時段同事", "誰一起上班") or compact.startswith(("查看時段", "同時段")) and \
                not SLOT_PATTERN.fullmatch(value):
            rest, day = split_day(value, today())
            roster = self._browse_roster(user_id, day)
            if not roster:
                return self._missing_roster(token, user_id, day)
            return self._show_slot_picker(token, user_id, roster)
        slot = SLOT_PATTERN.fullmatch(value)
        if slot:
            start, end, area, when = slot.groups()
            row = int(start) - 1
            if end and int(end) != int(start) + 1 or not FIRST_ROW <= row <= LAST_ROW:
                return self._reply(token, [text("時段請用一個小時，例如「10-11 國際」或「10 國內」（05–24 點）。")])
            day = parse_day(when, today()) if when else None
            if when and not day:
                day = split_day(when, today())[1]
            roster = self._browse_roster(user_id, day)
            if not roster:
                return self._missing_roster(token, user_id, day)
            return self._answer_slot(token, user_id, roster, row, "intl" if area == "國際" else "dom")
        rest, day = split_day(value, today())
        leave_command = rest.replace(" ", "")
        if leave_command == "查看工作":
            return self._show_work_picker(token, user_id, day=day)
        if leave_command in ("休假人員", "休假名單", "輪休人員", "請假人員"):
            section = {"輪休人員": "rest", "請假人員": "leave"}.get(leave_command)
            return self._show_leave(token, user_id, day=day, section=section)
        if rest.replace(" ", "") in ("勤務人員", "當日勤務人員", "人員對照", "姓名代號對照"):
            return self._show_staff_table(token, user_id, day=day)
        if rest.replace(" ", "") in FLOW_COMMANDS:
            return self._show_flow(token, user_id, day=day)
        if day and rest.replace(" ", "") in MY_ROSTER + ("",):
            return self._show_mine(token, user_id, day)
        if rest.replace(" ", "") in ("查詢人員", "查代號", "查名字", "查姓名"):
            example = "查 72" if "代號" in rest else "查 蕭志明"
            return self._reply(token, [text(f"請輸入要查的代號或完整姓名，例如「{example}」。\n也可以加日期，例如「{example} 10/3」。")])
        query = re.fullmatch(r"(?:查詢?|找)[:：]?\s*(\S+)", rest if day else value)
        if query or parse_code(rest if day else value):
            target = query.group(1) if query else (rest if day else value)
            code = parse_code(target)
            if code:
                return self._show(token, user_id, code=code, day=day)
            if NAME_PATTERN.fullmatch(target):
                return self._show(token, user_id, staff_name=target, day=day)
            return self._reply(token, [text("找不到這個代號或姓名。代號是 1–84、A–N 或 甲乙丙丁…")])
        if compact in ("解除", "取消綁定", "解除綁定", "取消登記"):
            had, pending = (self.store.user(user_id) or {}).get("staff_name"), self.store.pending_name_request(user_id)
            self.store.set_staff_name(user_id, None)
            self.store.set_notify(user_id, False)
            lines = ["已取消姓名登記，也關閉了通知。" if self._notifying else "已取消姓名登記。"]
            if pending and self.store.decide_name_request(pending["id"], "cancelled", user_id):
                lines.append(f"申請中的「{pending['requested_name']}」也一併取消了。")
            if had and self._approvers(besides=user_id):   # the lock stays (``Store.locked_name``)
                lines.append(f"之後再登記「{had}」以外的姓名，要先經管理員同意。")
            return self._reply(token, [text("\n".join(lines))])
        match = re.fullmatch(r"通知\s*(開|關|on|off)", value, flags=re.I)
        if match:
            on = match.group(1).lower() in ("開", "on")
            if on and not self._notifying:   # nothing is recorded: who had it on before stays as it was
                return self._reply(token, [text(NOTIFY_OFF if self.settings.push else NOTIFY_NO_PUSH)])
            if on and not (self.store.user(user_id) or {}).get("staff_name"):
                return self._reply(token, [{**text(f"請先輸入「設定 你的姓名」（例如 設定 {EXAMPLE_NAME}），再開啟通知。"),
                                            "quickReply": {"items": [fill_action("設定姓名", "設定 ")]}}])
            self.store.set_notify(user_id, on)
            return self._reply(token, [text("好的，之後有新班表時會私訊你的勤務。" if on
                                            else "好的，不再私訊你新班表。")])
        match = re.fullmatch(r"(?:日期|班表日期)[:：]?\s*(\S+)", value)
        if match:
            return self._set_date(token, user_id, match.group(1))
        if compact in HELP_WORDS:
            return self._reply(token, [self._help(user_id=user_id)])
        if compact in MORE_HELP_WORDS:
            return self._reply(token, [self._help(advanced=True, user_id=user_id)])
        return self._reply(token, [text("我看不懂這句話 🙏"),
                                   {**self._help(user_id=user_id), "quickReply": {"items": [fill_action("意見回饋", "意見 ")]}}])

    def _set_date(self, token, user_id, value):
        """「日期 月/日」: correct a roster photo's date and warn when the header read another date.

        Without administrators: the user's most recent upload or, having none, the latest roster they can see.
        With administrators the date of a group roster is theirs to set (``_admin_date_target``); everybody
        else can only correct a photo they sent privately.
        """
        if not self.settings.admins:
            roster = self.store.last_upload_for(user_id)
        elif self._is_admin(user_id):
            roster = self._admin_date_target(user_id)
        else:
            roster = self.store.own_upload(user_id)
            if not roster or str(roster["chat_id"])[:1] in "CR":
                return self._reply(token, [text(ADMIN_DATES)])
        if not roster:
            return self._reply(token, [text("目前還沒有班表。請先傳一張班表照片。")])
        read = date.fromisoformat(roster["read_date"]) if roster.get("read_date") else None
        day = parse_day(value, roster_day(roster) or today())
        if not day:
            return self._reply(token, [text("日期格式看不懂，請輸入「日期 月/日」（換成班表上印的日期）。")])
        if roster.get("held"):
            if self._is_admin(user_id):
                # Typing names no photo, and dating this one notifies everybody: confirm with a button that does.
                return self._reply(token, [date_request_card(roster, len(self._audience()), typed=day)])
            self.store.unhold(roster["run_id"])   # held for administrators who are no longer configured
        return self._redate(token, user_id, roster, day, undo=f"傳「日期 {read.month}/{read.day}」" if read else None)

    def _redate(self, token, user_id, roster, day, *, undo=None):
        """Give a photo that is not waiting for its date another one; nobody is notified again. ``undo``: how to
        change it back, said when the date read off the photo differs from the new one."""
        read = date.fromisoformat(roster["read_date"]) if roster.get("read_date") else None
        self.store.set_roster_date(roster["run_id"], day.isoformat())
        print(f"{user_id} 把班表 {roster['run_id']} 的日期改為 {day.isoformat()}", flush=True)
        lines = [f"好的，{source_label(roster, user_id)}的日期已改為 {day_label(day)}。"]
        if read and read != day:
            lines.append(f"注意：照片上辨識到的日期是 {day_label(read)}。" + (f"如果改錯了，{undo}改回來。" if undo else ""))
        return self._reply(token, [text("\n".join(lines))])

    def _admin_date_target(self, user_id) -> dict | None:
        """Which photo an administrator's typed 「日期 月/日」 is about.

        The newest group photo still waiting for its date (the request card says to type other dates), and
        with nothing waiting the photo sent last, whoever posted it: a group's, or their own private one.
        """
        waiting = self.store.held_rosters()
        return waiting[0] if waiting else self.store.newest_visible(user_id)

    def _may_redate(self, user_id, roster) -> bool:
        """Whose date picker (``redate|run``) re-dates ``roster``: an administrator's, for a group photo or one they sent
        privately; and only while there is no administrator, its uploader's (the picker of their notice), once the
        名冊守門員 lets them in. Anyone else's tap is ignored, as a stale button is."""
        visible = roster["chat_id"] == user_id or str(roster["chat_id"])[:1] in "CR"
        if self._is_admin(user_id):
            return visible
        return (not self.settings.admins and visible and bool(user_id) and roster.get("uploaded_by") == user_id
                and self._admitted(user_id))

    def _confirm_date(self, token, user_id, roster, day):
        """An administrator named the day of a held group photo: date it, then notify as for any group photo."""
        run_id = roster["run_id"]
        try:
            result = self._result(run_id)
        except Exception as exc:   # nothing has changed: the photo is still held and the button still works
            print(f"讀不到待確認的班表（{run_id}）：{type(exc).__name__}: {exc}", flush=True)
            return self._reply(token, [text("⚠ 這張班表的辨識結果讀不出來，所以日期沒有設定，也沒有通知任何人。\n"
                                            "請再按一次；還是不行的話，請把照片重傳到群組。")])
        before = source_label(roster, user_id)
        if not self.store.confirm_date(run_id, day.isoformat()):   # another tap, or another administrator, was first
            return self._resolved(token, self.store.roster_by_run(run_id) or roster)
        print(f"管理員 {user_id} 確認班表 {run_id} 的日期：{day.isoformat()}", flush=True)
        roster = self.store.roster_by_run(run_id)
        lines = [f"好的，{before}的日期已設為 {day_label(day)}。"]
        newer = self.store.same_day(roster, later=True)
        audience = len(self._audience())
        if newer:
            lines.append(f"不過同一天已經有更晚傳送的班表（{source_label(newer, user_id)}），"
                         "這張只存檔，沒有另外通知。")
        elif audience:
            update = "這是同一天的更新版，" if self.store.same_day(roster, later=False) else ""
            lines.append(f"{update}現在通知已開通知的 {audience} 人。")
        elif self._notifying:
            lines.append("目前沒有人開通知，所以沒有發出通知。")
        else:
            lines.append("現在大家可以用「我的班表」或日期查到這張。")
        self._reply_quietly(token, [text("\n".join(lines))])   # first: the reply token does not outlast a long round
        try:
            self._announce(roster, result)
        except Exception as exc:
            print(f"通知失敗（{run_id}）：{type(exc).__name__}: {exc}", flush=True)
            self._push_quietly(user_id, [text("⚠ 通知大家的時候發生錯誤，可能有人沒收到。請把這張班表重傳到群組一次。")])

    def _resolved(self, token, roster):
        """A button on a request that was already answered (another tap, or another administrator, was first)."""
        day = roster_day(roster)
        return self._reply(token, [text(f"這張班表已經確認過日期：{day_label(day)}。沒有再通知一次。" if day
                                        else "這張班表先前已經略過，這次沒有變更。")])

    def _register_or_claim(self, target, when, claiming, token, chat_id, user_id):
        """「設定 蕭志明」 registers a name; 「我是 72 (9/30)」 claims a code on one roster."""
        if claiming and not parse_code(target) and re.fullmatch(DATE_TOKEN, normalise(target)):
            # 「我是 10/8 72」: how the 我的班表 card fills it in, the code typed last. A date word is never a name,
            # so without a code after it (「我是 10/8」, 「我是 明天 90」) the answer is how to write it.
            if not when or not parse_code(when):
                return self._reply(token, [text(f"請在日期後面加上你在那張班表的代號，例如「我是 {target} 72」"
                                                + (f"；「{when}」不是班表上的代號。" if when else "。"))])
            target, when = when, target
        code = parse_code(target)
        if code:
            user = self.store.user(user_id) or {}
            if not claiming:   # 「我是 72」 is no way in past the 名冊守門員: it is only offered to those let in
                words = CODE_NOT_NAME.format(code=code)
                return self._reply(token, [text(words if self._admitted(user_id) else words.splitlines()[0])])
            if not user.get("staff_name"):
                return self._reply(token, [self._need_name()])
            fixed = load_fixed_codes(self.settings.fixed_staff_codes)
            fixed_code = next((c for c, n in fixed.items() if n == user["staff_name"]), None)
            if fixed_code and code != fixed_code:
                return self._reply(token, [text(f"{user['staff_name']}的固定代號是「{fixed_code}」，不需要改成「{code}」。\n"
                                                "直接點「我的班表」或輸入日期，就能查自己的勤務。")])
            day = (parse_day(when, today()) or split_day(when, today())[1]) if when else None
            if when and not day:   # without it the claim would land on the newest roster, maybe another day's
                return self._reply(token, [text(f"「{when}」不是看得懂的日期，請寫成像「我是 {code} 10/8」這樣。")])
            roster = self._roster(user_id, day)
            if not roster:
                return self._missing_roster(token, user_id, day)
            result = self._result(roster["run_id"])
            read = self.names_for(roster, result).get(code)
            if read and read != user["staff_name"]:
                return self._reply(token, [text(
                    f"{source_label(roster, user_id)}的姓名對照表上，代號 {code} 是「{read}」，沒有改。\n"
                    "請確認是不是看錯日期的班表；要指定日期請傳「我是 代號 9/30」。")])
            self.store.claim(roster["run_id"], user_id, code)
            note = f"好的，{source_label(roster, user_id)}上你是代號 {code}。"
            if not any(p["code"] == code for p in result.get("people", [])):
                note += f"\n這張班表上沒有辨識到代號 {code} 的勤務（休假，或看錯了日期的班表？）。"
            return self._reply(token, [text(note), *self._duties(code, user["staff_name"], roster, result,
                                                                 viewer=user_id, mine=True)])
        if not NAME_PATTERN.fullmatch(target):
            return self._reply(token, [text(f"姓名請輸入 2–5 個中文字，例如「設定 {EXAMPLE_NAME}」。")])
        return self._register_name(target, token, chat_id, user_id)

    def _register_name(self, target, token, chat_id, user_id):
        """「設定 蕭志明」: a first name nobody else has registers at once and answers with 「我的班表」.

        Only a name on the 名冊 (``_may_register``, the 名冊守門員): any other is refused before anything else, and
        nothing changes (``_off_roster``). A registered name is locked: another one, or a name another LINE account
        already has, goes to the administrators for approval (``_request_name``). Without one to ask (none set up,
        or the only one is asking) it changes at once, as it always did.
        """
        started = time.monotonic()
        roster = self._roster_names()
        if not self._may_register(user_id, target, roster):
            return self._off_roster(token, user_id, target)
        user = self.store.user(user_id) or {}
        pending = self.store.pending_name_request(user_id)
        if user.get("staff_name") == target:
            note = f"「{target}」本來就是你登記的姓名，不用再設定。"
            if pending and self.store.decide_name_request(pending["id"], "cancelled", user_id):
                note += f"\n申請中的「{pending['requested_name']}」已經取消。"
            return self._reply(token, [{**text(note), "quickReply": {"items": [
                {"type": "action", "action": message_action("我的班表")}]}}])
        held = self.store.locked_name(user_id)   # theirs, or theirs until 「解除」
        changing = bool(held) and held != target
        taken = any(person["user_id"] != user_id for person in self.store.users_named(target))
        approvers = self._approvers(besides=user_id)
        if (changing or taken) and approvers:
            current = user.get("staff_name")   # none after 「解除」: the card then says so, and shows the old one
            return self._request_name(token, chat_id, user_id, current, target, approvers, pending,
                                      released=None if current else held)
        if taken and not self._is_admin(user_id):
            # A name belongs to one LINE account, and there is nobody to ask: it stays with the account that has it
            print(f"{user_id} 想登記別人已登記的「{target}」，沒有管理員可以確認，沒有登記", flush=True)
            kept = user.get("staff_name")
            return self._reply(token, [{**text(
                f"🙏「{target}」已經有其他 LINE 帳號登記了。一個姓名只能登記在一個 LINE 帳號，所以"
                + (f"沒有改名，你的姓名還是「{kept}」。" if kept else "這次沒有登記。")
                + f"\n如果這是你的姓名，請聯絡幹部解除原本的登記，再傳一次「設定 {target}」。"),
                "quickReply": {"items": [fill_action("意見回饋", "意見 ")]}}])
        if changing or taken:   # the only administrator: nobody to ask, and theirs to decide
            print(f"⚠ 沒有管理員可以確認姓名（LINE_V2_ADMIN_USER_IDS）：{user_id} 直接"
                  + (f"從「{held}」改名為「{target}」" if changing else "")
                  + (f"接手別人已登記的「{target}」，原本的帳號解除登記" if taken else ""), flush=True)
        if pending:   # the name they now have settles what they were asking for
            self.store.decide_name_request(pending["id"], "cancelled", user_id)
        display = self.transport.display_name(user_id, chat_id) if user_id else None
        try:
            released = self.store.take_name(user_id, target, display) if taken else \
                self.store.set_staff_name(user_id, target, display) or []
        except NameTaken:   # another account registered it this very moment: it is a taken name now
            return self._register_name(target, token, chat_id, user_id)
        reply = f"已登記：{target}。之後點「我的班表」就能查看" + (
            "；想自動收到新班表，請輸入「通知 開」。" if self._notifying else "。")
        if released:
            reply += "\n" + self._released_note(target, released)
        if approvers:
            reply += "\n🔒 登記後就不能自己改了；打錯字的話再傳「設定 正確的姓名」，管理員同意後就會改。"
        messages = self._my_day_messages(user_id, target)   # today first, though tomorrow's may already be up
        if not messages:
            return self._reply(token, [text(reply + "\n" + NO_ROSTER)])
        return self._answer(token, user_id, started, [text(reply), *messages])

    def _role(self, user_id) -> str | None:
        """管理員 or 幹部 for an account the 名冊守門員 lets in whatever its name; None for everyone else."""
        return "管理員" if self._is_admin(user_id) else "幹部" if self.store.is_officer(user_id) else None

    def _still_in(self, accounts) -> list[str]:
        """What to say about released accounts that are 幹部 or administrators: the role goes with the LINE account,
        not with the name, so they see and manage as before. For a 幹部, the command that ends it: with the whole
        user id, the one handle left on an account that has no name any more."""
        lines = []
        for account in accounts:
            role = self._role(account["user_id"])
            if role:
                lines.append(f"⚠️ {account.get('name') or short_id(account['user_id'])} 仍是{role}，照樣看得到班表、可以管理人員"
                             f"（{role}身分跟著 LINE 帳號，不跟著姓名）。")
            if self.store.is_officer(account["user_id"]):
                lines.append(f"不該再是幹部的話，請傳「移除幹部 {account['user_id']}」。")
        return lines

    def _kept_out(self, accounts) -> list[dict]:
        """The released accounts the 名冊守門員 now keeps out: none while there is no 名冊, never a 幹部 or an
        administrator."""
        return [account for account in accounts if not self._admitted(account["user_id"])]

    def _released_note(self, name, released) -> str:
        """What an administrator is told about the accounts a name was taken from."""
        who = "、".join(account.get("name") or short_id(account["user_id"]) for account in released)
        out = "，看不到班表了" if len(self._kept_out(released)) == len(released) else ""
        lines = [f"原本登記「{name}」的 LINE 帳號（{who}）已經解除{out}；一個姓名只能登記在一個 LINE 帳號。",
                 *self._still_in(released)]
        if any(self.store.is_officer(account["user_id"]) for account in released):
            lines.append(f"新帳號要當幹部，請傳「新增幹部 {name}」。")
        return "\n".join(lines)

    def _off_roster(self, token, user_id, target):
        """A name that is not on the 名冊: nothing changes and nothing is filed. The answer names no one on the 名冊:
        it goes to someone the 名冊守門員 keeps out, who is told to check the spelling or to be added first."""
        held = (self.store.user(user_id) or {}).get("staff_name")
        print(f"名冊守門員：{user_id} 想登記名冊上沒有的「{target}」，沒有登記", flush=True)
        if held == target:   # their own name, since removed from the 名冊 (or registered before there was one)
            words = f"🙏「{target}」目前不在名冊上，所以暫時看不到班表。\n如果你還在隊上，請聯絡幹部。"
        else:
            changed = "沒有送出改名申請" if self._approvers(besides=user_id) else "沒有改名"
            words = (f"🙏 名冊上沒有「{target}」，所以"
                     + (f"{changed}，你的姓名還是「{held}」。" if held else "沒有登記。") + f"\n{NOT_ON_ROSTER}")
        return self._reply(token, [{**text(words), "quickReply": {"items": [fill_action("✏️ 重新輸入姓名", "設定 ")]}}])

    def _not_found(self, staff_name, roster) -> str:
        day = roster_day(roster)
        # The date keeps the claim on this roster: a bare 「我是 72」 goes to the newest one, tomorrow's once it is up
        when = f" {day.month}/{day.day}" if day else ""
        return (f"在 {day_label(day) if day else '最新'}的班表上沒有找到「{staff_name}」"
                "（可能休假，或姓名對照表的字辨識不清）。\n"
                f"如果有上班，請傳「我是 你的代號{when}」（例如 我是 72{when}），之後這張班表就會用這個代號。")

    def _show_mine(self, token, user_id, day=None, roster=None):
        """One's own duty card for ``day``, or for that photo (``roster``: the 今天／明天 buttons of 「我的班表」)."""
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        return self._show(token, user_id, staff_name=user["staff_name"], day=day, mine=True, roster=roster)

    def _show_my_day(self, token, user_id):
        """「我的班表」 (and the old 「今天班表」「明天班表」「下個時段」): the next hour, today and tomorrow on one card
        (``my_day_card``), then the blue-outlined photo of the roster the next duty is on.

        Today's roster comes first even when tomorrow's is already up. With neither of them, the card offers the
        newest roster the user can see; with no roster at all there is nothing to show but how rosters arrive.
        """
        started = time.monotonic()
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        messages = self._my_day_messages(user_id, user["staff_name"])
        if not messages:
            return self._reply(token, [text(NO_ROSTER)])
        # No quick replies: the card is the whole answer (its 「我是 代號」 tile and 👉 link are tappable themselves)
        return self._answer(token, user_id, started, messages)

    def _my_day_messages(self, user_id, name) -> list[dict] | None:
        """The card of 「我的班表」 for ``name`` and the photo under it, without sending them (a registration, and a
        name an approver let through, answer with them too); ``None`` when the user can see no roster at all."""
        threshold = self.settings.threshold
        now = taipei_now()
        current = now.date()   # read once: two reads could straddle midnight
        mine, ahead = (self._my_day(user_id, name, current + timedelta(days=offset)) for offset in (0, 1))
        latest = None
        if not mine.roster:
            newest = self.store.roster_for_user(user_id, dated=True)   # a held photo stays out of sight
            if not newest and not ahead.roster:
                return None
            if newest and roster_day(newest) not in (mine.day, ahead.day):
                latest = newest
        # The photo is of the roster the next duty is on: today's while a duty is left today, else tomorrow's.
        left = any(int(block["end"]) > now.hour for block in mine.blocks(threshold))
        focus = mine if left else ahead if ahead.state == "duty" else mine if mine.state == "duty" else None
        image = self._person_image(focus.roster["run_id"], focus.code) if focus else None
        seen = (focus or (mine if mine.roster else ahead)).roster or latest
        if seen:   # a plain 「查 72」 afterwards looks in the same roster
            self.store.remember_roster(user_id, seen["run_id"])
        card = my_day_card(name, now, mine, ahead, threshold=threshold, photo=focus.day if image else None,
                           latest=latest)
        return [card, image] if image else [card]

    def _my_day(self, user_id, staff_name, day) -> MyDay:
        """What ``day``'s roster (the newest photo of that day) says about ``staff_name``."""
        roster = self.store.roster_on(user_id, day)
        if not roster:
            return MyDay(day)
        result = self._result(roster["run_id"])
        code = self.code_for(staff_name, roster, result)
        person = next((p for p in result.get("people", []) if p["code"] == code), None) if code else None
        if code and not (person and person.get("slots")):
            result = self._with_leave(user_id, roster, result)
        return MyDay(day, roster, result, code, person)

    def _with_leave(self, user_id, roster, result) -> dict:
        """``result`` with the leave table read, for a code without duties: it may say 輪休 or which leave.

        Rosters saved before the table was read are read once (slowly, with the typing indicator meanwhile)."""
        ensure_leave = getattr(self.analyzer, 'ensure_leave', None)
        if ensure_leave and result.get('leave_table_version') != LEAVE_VERSION:
            self._loading(user_id)
            try:
                return {**result, 'leave_table': ensure_leave(roster['run_id'])}
            except Exception as exc:
                print(f"讀取休假人員失敗（{roster['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
        return result

    def _show(self, token, user_id, *, code=None, staff_name=None, day=None, mine=False, roster=None, tapped=None,
              started=None):
        """One person's duties (by today's code or by name): the latest roster, or the one for ``day``.

        ``tapped``: the date tapped on 「班表月曆」 (``_show_calendar_day``, which ``started`` the reply). The whole
        roster photo goes first, so the card needs no button for it, and that month's calendar last
        (``_calendar_after``) instead of the other dates, carrying the quick replies."""
        started = time.monotonic() if started is None else started
        roster = roster or (self._roster(user_id, day) if mine else self._person_roster(user_id, day))
        if not roster:
            return self._missing_roster(token, user_id, day)
        self.store.remember_roster(user_id, roster["run_id"])
        result = self._result(roster["run_id"])
        names = self.names_for(roster, result)
        whole = self._roster_image(roster["run_id"]) if tapped else None
        before = [whole] if whole else []
        others = () if tapped else self._other_dates(user_id, roster)
        if staff_name:
            code = next((c for c, n in names.items() if n == staff_name), None)
            if not code:
                message = self._not_found(staff_name, roster) if mine else \
                    f"在這張班表的姓名對照表沒有找到「{staff_name}」。可以改用「查 代號」。"
                note = date_note(roster, result)   # a re-dated photo shows it here too: the name may be missing for that
                if note:
                    message = f"{note}\n{message}"
                actions = self._person_actions(roster, result, names)
                if self._is_admin(user_id) and self._may_redate(user_id, roster):
                    actions.append({"type": "action", "action": redate_picker(roster, "📅 改這張的日期")})
                ask = "" if mine else f"查 {staff_name} "
                messages = [*before, text(message + other_dates_text(others, ask))]
                if tapped:
                    messages += self._calendar_after(user_id, staff_name, tapped, started)
                messages[-1]["quickReply"] = {"items": actions}
                return self._reply(token, messages)
        if not any(p['code'] == code for p in result.get('people', [])):
            result = self._with_leave(user_id, roster, result)
        messages = before + self._duties(code, staff_name or names.get(code), roster, result, viewer=user_id,
                                         mine=mine, others=others,
                                         show_original=bool(self.settings.public_base and self.settings.images
                                                            and not whole))
        if tapped:
            messages += self._calendar_after(user_id, staff_name, tapped, started)
        actions = self._person_actions(roster, result, names, code)
        if mine:   # from the day's hours to the month's
            actions.append({"type": "action", "action": {"type": "message", "label": "本月統計", "text": "本月統計"}})
        messages[-1]["quickReply"] = {"items": actions}
        return self._answer(token, user_id, started, messages)

    def _person_actions(self, roster, result, names, code=None) -> list[dict]:
        items = [self._lookup_action(roster, label='查其他人')]
        present = set(names) | {p['code'] for p in result.get('people', [])}
        ordered = [c for c in ALLOWED_CODES if c in present]
        if code in ordered:
            index = ordered.index(code)
            for position, label in ((index - 1, "前一人"), (index + 1, "後一人")):
                if 0 <= position < len(ordered):
                    target = ordered[position]
                    caption = f"{label}：{target}" + (f" {names[target]}" if target in names else "")
                    items.append({"type": "action", "action": {"type": "postback", "label": caption,
                                  "data": f"person|{roster['run_id']}|{target}", "displayText": f"查 {target}"}})
        return items

    def _other_dates(self, user_id, roster) -> list[date]:
        """The newest days other than this roster's with a roster the user can look up (``DUTY_DATES``), oldest first:
        the chips of the duty card."""
        shown = roster_day(roster)
        return sorted(day for day in self.store.roster_dates(user_id, DUTY_DATES + 1) if day != shown)[-DUTY_DATES:]

    # -- 查看時段 -------------------------------------------------------------------
    def _handle_postback(self, data: str, token, user_id, params: dict | None = None):
        """``params``: what LINE adds to a postback, such as the day picked on a date picker (``date``)."""
        parts = data.split("|")
        if parts[0] == "person" and len(parts) == 3 and parse_code(parts[2]):
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show(token, user_id, code=parse_code(parts[2]), roster=roster)
        if parts[0] == "mine" and len(parts) == 2:   # 「我的班表」's 今天／明天: one's duty card on that very photo
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_mine(token, user_id, roster=roster)
        if parts[0] == "leave" and len(parts) in (2, 3) and (len(parts) == 2 or parts[2] in ("rest", "leave")):
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_leave(token, user_id, roster=roster,
                                        section=parts[2] if len(parts) == 3 else None)
        if parts[0] == "staff" and len(parts) == 2:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_staff_table(token, user_id, roster=roster)
        if parts[0] == "works" and len(parts) == 2:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_work_picker(token, user_id, roster=roster)
        if parts[0] == "work" and len(parts) == 3 and re.fullmatch(r"[0-9a-f]{20}", parts[2]):
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_work(token, user_id, roster, parts[2])
        if parts[0] == "roster" and len(parts) == 2:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                self.store.remember_roster(user_id, roster["run_id"])
                image = self._roster_image(roster["run_id"])
                return self._reply(token, [image or text("這張班表的原始照片目前無法取得。")])
        if parts[0] == "slots" and len(parts) == 2:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id):
                return self._show_slot_picker(token, user_id, roster)
        if parts[0] == "slot" and len(parts) == 3:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id) and \
                    parts[2].isdigit() and FIRST_ROW <= int(parts[2]) <= LAST_ROW:
                self.store.remember_roster(user_id, roster["run_id"])
                return self._reply(token, [area_question(roster, int(parts[2]))])
        if parts[0] == "area" and len(parts) == 4 and parts[3] in AREAS:
            roster = self.store.roster_by_run(parts[1])
            if roster and (str(roster["chat_id"])[:1] in "CR" or roster["chat_id"] == user_id) and \
                    parts[2].isdigit() and FIRST_ROW <= int(parts[2]) <= LAST_ROW:
                return self._answer_slot(token, user_id, roster, int(parts[2]), parts[3])
        if parts[0] in ("guide", "fixed", "setcode") and self._staff_postback(parts, token, user_id):
            return
        if parts[0] == "stats" and len(parts) == 2 and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", parts[1]):
            return self._show_stats(token, user_id, int(parts[1][:4]), int(parts[1][5:]))
        if parts[0] == "cal" and len(parts) == 2:   # 「班表月曆」: a date on it, or the month of 「◀ 9 月的班表」
            if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", parts[1]):
                return self._show_calendar(token, user_id, int(parts[1][:4]), int(parts[1][5:]))
            day = iso_day(parts[1])
            if day:
                return self._show_calendar_day(token, user_id, day)
        if parts[0] == "wage" and len(parts) == 1:   # the 加班費 tile: the keyboard opens on 「時薪 」, this says how
            return self._set_wage(token, user_id, "")
        if parts[0] == "flow" and len(parts) == 2:
            try:
                day = date.fromisoformat(parts[1])
            except ValueError:
                return
            return self._show_flow(token, user_id, day=day)
        if parts[0] == "redate" and len(parts) == 2:
            # LINE's date picker on one photo (``redate_picker``): the 📅 of an upload report or of a duty card (an
            # administrator's), or of the notice to the uploader of an undated group photo (without administrators)
            roster = self.store.roster_by_run(parts[1])
            day = picked_day((params or {}).get("date"), roster) if roster else None
            if not roster or not day or not self._may_redate(user_id, roster):
                return
            if roster.get("held"):
                if self._is_admin(user_id):   # dating a held photo releases it: the same as a button of its question
                    return self._confirm_date(token, user_id, roster, day)
                self.store.unhold(roster["run_id"])   # held for administrators who are no longer configured
            read = photo_date(roster)
            undo = "再點一次「📅 改這張的日期」" if self._is_admin(user_id) else \
                f"傳「日期 {read.month}/{read.day}」" if read else None
            return self._redate(token, user_id, roster, day, undo=undo)
        if parts[0] == "date" and len(parts) == 3 and self._is_admin(user_id):
            # A button of ``date_request_card``, or the day picked on its 「📅 其他日期」 (``pick``). Only these, never
            # typed text, release a held group photo.
            roster = self.store.roster_by_run(parts[1])
            if not roster or str(roster["chat_id"])[:1] not in "CR":
                return
            if parts[2] == "skip":
                if not self.store.unhold(roster["run_id"]):
                    return self._resolved(token, roster)
                print(f"管理員 {user_id} 選擇不通知班表 {roster['run_id']}", flush=True)
                return self._reply(token, [text(f"好的，已略過{source_label(roster, user_id)}：不會通知，日期維持未辨識。")])
            day = picked_day((params or {}).get("date") if parts[2] == "pick" else parts[2], roster)
            if not day:
                return
            if not roster.get("held"):
                return self._resolved(token, roster)
            return self._confirm_date(token, user_id, roster, day)
        if parts[0] == "rename" and len(parts) in (3, 4) and parts[1].isdigit() and parts[2] in ("yes", "no") \
                and (len(parts) == 3 or parts[3].isdigit()):
            # ✅／❌ of ``name_request_card``. Only an administrator's tap counts; anyone else's, a 幹部's too (an old card
            # from when they could answer), is ignored as a stale button is.
            if self._is_admin(user_id):
                stamp = int(parts[3]) if len(parts) == 4 else None   # microseconds; milliseconds on older cards
                built = stamp / (1_000_000 if stamp > 10 ** 14 else 1000) if stamp else None
                return self._decide_name(token, user_id, int(parts[1]), parts[2] == "yes", built=built)
            return
        # 「fill:…」 postbacks only open the keyboard; anything else is stale — stay quiet

    def _show_slot_picker(self, token, user_id, roster):
        self.store.remember_roster(user_id, roster["run_id"])
        picker = slot_picker(roster, self._result(roster["run_id"]))   # head counts as the listing will show
        actions = self._date_switch_actions(user_id, roster, "slots")
        if actions:
            picker["quickReply"] = {"items": actions}
        return self._reply(token, [picker])

    def _show_work_picker(self, token, user_id, *, day=None, roster=None):
        roster = roster or self._browse_roster(user_id, day)
        if not roster:
            return self._missing_roster(token, user_id, day)
        self.store.remember_roster(user_id, roster["run_id"])
        result = self._result(roster["run_id"])
        if not work_options(result):
            return self._reply(token, [text("這張班表還沒有辨識到可查的工作，請對照原始照片。")])
        picker = work_picker(roster, result)
        actions = self._date_switch_actions(user_id, roster, "works")
        if actions:
            picker["quickReply"] = {"items": actions}
        return self._reply(token, [picker])

    def _show_work(self, token, user_id, roster, key):
        self.store.remember_roster(user_id, roster["run_id"])
        result = self._result(roster["run_id"])
        work = next((name for name, _, _ in work_options(result) if work_key(name) == key), None)
        if work is None:
            return self._reply(token, [{**text("這張班表沒有這項工作，請重新選擇。"), "quickReply": {"items": [
                {"type": "action", "action": {"type": "postback", "label": "換工作",
                    "data": f"works|{roster['run_id']}", "displayText": "查看工作"}}]}}])
        names = self.names_for(roster, result)
        span = work_span(result, work)
        image = self._work_image(roster["run_id"], *span) if span else None
        messages = [work_card(roster, result, names, work, threshold=self.settings.threshold,
                              has_image=bool(image), later=self._set_after(roster))]
        if image:
            messages.append(image)
        messages[-1]["quickReply"] = {"items": [
            {"type": "action", "action": {"type": "postback", "label": "換工作",
                "data": f"works|{roster['run_id']}", "displayText": "查看工作"}},
            *self._date_switch_actions(user_id, roster, "work", section=work)]}
        return self._reply(token, messages)

    def _work_image(self, run_id, columns, rows) -> dict | None:
        """The work's columns cut from the roster photo, for 查看工作 (as ``_slot_image`` is for 查看時段)."""
        render = getattr(self.analyzer, "work_image", None)
        if not (self.settings.images and self.settings.public_base and render):
            return None
        try:
            paths = render(run_id, columns, rows)
        except Exception as exc:
            print(f"工作原圖裁切失敗（{run_id} / {columns} / {rows}）：{type(exc).__name__}: {exc}", flush=True)
            return None
        if not paths:
            return None
        base = f"{self.settings.public_base}/runs/"
        return {"type": "image", "originalContentUrl": base + quote(paths[0]),
                "previewImageUrl": base + quote(paths[1])}

    def _answer_slot(self, token, user_id, roster, row, area):
        self.store.remember_roster(user_id, roster["run_id"])
        result = self._result(roster["run_id"])
        names = self.names_for(roster, result)
        other = "dom" if area == "intl" else "intl"
        label = AREAS[other][0]
        quick_reply = {"items": [
            {"type": "action", "action": {"type": "postback", "label": f"改看{label}",
                                          "data": f"area|{roster['run_id']}|{row}|{other}",
                                          "displayText": f"{hour_label(row)} {label}"}},
            {"type": "action", "action": {"type": "postback", "label": "換時段",
                                          "data": f"slots|{roster['run_id']}", "displayText": "換時段"}},
            self._staff_table_action(roster), self._leave_action(roster),
            *self._date_switch_actions(user_id, roster, "area", row=row, area=area)]}
        image = self._slot_image(roster["run_id"], row, area)
        messages = [slot_card(result, row, area, names, roster, threshold=self.settings.threshold,
                              has_image=bool(image), later=self._set_after(roster))]
        if image:
            messages.append(image)
        messages[-1]["quickReply"] = quick_reply
        return self._reply(token, messages)

    def _slot_image(self, run_id, row, area) -> dict | None:
        render = getattr(self.analyzer, "slot_image", None)
        if not (self.settings.images and self.settings.public_base and render):
            return None
        try:
            paths = render(run_id, row, area)
        except Exception as exc:
            print(f"時段原圖裁切失敗（{run_id} / {row} / {area}）：{type(exc).__name__}: {exc}", flush=True)
            return None
        if not paths:
            return None
        base = f"{self.settings.public_base}/runs/"
        return {"type": "image", "originalContentUrl": base + quote(paths[0]),
                "previewImageUrl": base + quote(paths[1])}

    def _staff_table_action(self, roster) -> dict:
        return {"type": "action", "action": {"type": "postback", "label": "勤務人員",
                                               "data": f"staff|{roster['run_id']}",
                                               "displayText": "勤務人員"}}

    def _leave_action(self, roster) -> dict:
        return {"type": "action", "action": {"type": "postback", "label": "休假人員",
                                               "data": f"leave|{roster['run_id']}", "displayText": "休假人員"}}

    def _lookup_action(self, roster, *, label='查詢人員') -> dict:
        day = roster_day(roster)
        prefix = f'查 {day.year}/{day.month}/{day.day} ' if day else '查 '
        return {'type': 'action', 'action': {'type': 'postback', 'label': label, 'data': 'fill:查',
                                           'inputOption': 'openKeyboard', 'fillInText': prefix}}

    def _slots_action(self, roster) -> dict:
        return {'type': 'action', 'action': {'type': 'postback', 'label': '查看時段',
                'data': f"slots|{roster['run_id']}", 'displayText': '查看時段'}}

    def _show_leave(self, token, user_id, *, day=None, roster=None, section=None):
        started = time.monotonic()
        roster = roster or self._browse_roster(user_id, day)
        if not roster:
            return self._missing_roster(token, user_id, day)
        self.store.remember_roster(user_id, roster["run_id"])
        run = roster["run_id"]
        result = self.analyzer.result(run)
        if result.get("leave_table_version") != LEAVE_VERSION:
            self._loading(user_id)
        names = self.names_for(roster, result)
        leave = result.get("leave_table", {"available": False})
        ensure = getattr(self.analyzer, "ensure_leave", None)
        if ensure and result.get("leave_table_version") != LEAVE_VERSION:
            try:
                leave = ensure(run)
            except Exception as exc:
                print(f"讀取休假人員失敗（{run}）：{type(exc).__name__}: {exc}", flush=True)
                leave = {"available": False}
        directory = self._directory(roster)
        messages = []
        render = getattr(self.analyzer, "leave_table_image", None)
        if self.settings.images and self.settings.public_base and render:
            try:
                paths = render(run)
                if paths:
                    base = f"{self.settings.public_base}/runs/"
                    messages.append({"type": "image", "originalContentUrl": base + quote(paths[0]),
                                     "previewImageUrl": base + quote(paths[1])})
            except Exception as exc:
                print(f"休假人員原圖失敗（{run}）：{type(exc).__name__}: {exc}", flush=True)
        messages.insert(0, leave_card(roster, leave, names, directory, section=section,
                                     show_original=bool(self.settings.public_base and self.settings.images),
                                     has_image=bool(messages)))
        messages[-1]["quickReply"] = {"items": [self._staff_table_action(roster), self._leave_action(roster),
                                               self._lookup_action(roster), self._slots_action(roster),
                                               *self._date_switch_actions(user_id, roster, "leave", section=section)]}
        return self._answer(token, user_id, started, messages)

    def _show_staff_table(self, token, user_id, *, day=None, roster=None):
        started = time.monotonic()
        roster = roster or self._browse_roster(user_id, day)
        if not roster:
            return self._missing_roster(token, user_id, day)
        self.store.remember_roster(user_id, roster["run_id"])
        result = self.analyzer.result(roster['run_id'])
        names = self.names_for(roster, result)
        image = self._staff_table_image(roster["run_id"])
        messages = [staff_card(roster, result, names, has_image=bool(image), later=self._set_after(roster),
                               show_original=bool(self.settings.public_base and self.settings.images))]
        if image:
            messages.append(image)
        messages[-1]['quickReply'] = {'items': [self._lookup_action(roster), self._slots_action(roster),
                                               self._leave_action(roster),
                                               *self._date_switch_actions(user_id, roster, "staff")]}
        return self._answer(token, user_id, started, messages)

    # -- 人流預測 -------------------------------------------------------------------
    def _capture_flow(self, raw, chat_id, user_id, sent_at) -> None:
        """A 航班人數 message posted in a group becomes the team's data for its date; silently, like rosters.

        Only a message with departures (離站／出境) and a date is kept: the forecast reads nothing else, and every
        other text in the group is left alone. Each message read as a 航班人數 one, kept or not, is reported to the
        administrators at once (``_flow_notice``).
        """
        flow = passenger_flow.parse(raw, near=today())
        if not flow:
            return
        if not departures(flow):
            print(f"略過沒有離站／出境的航班人數訊息（{chat_id}）", flush=True)
            return self._flow_notice(flow, "skipped", "arrivals", chat_id, user_id, sent_at)
        if not flow.day:
            print(f"略過沒有日期的航班人數訊息（{chat_id}）", flush=True)
            return self._flow_notice(flow, "skipped", "undated", chat_id, user_id, sent_at)
        outcome = self.store.add_flow(chat_id, flow.day.isoformat(), raw, user_id, sent_at)
        print(f"已記下 {flow.day.isoformat()} 的航班人數訊息（{chat_id}）", flush=True)
        if outcome != "repeat":   # a redelivered webhook: nothing new to tell
            self._flow_notice(flow, "ok", outcome, chat_id, user_id, sent_at)

    def _flow_notice(self, flow, status, detail, chat_id, user_id, sent_at) -> None:
        """Tell the administrators at once how one group 航班人數 message was read: a card pushed to each of them
        (``flow_report``), one push each, as photos are reported (``_report_upload``); only with administrators.

        The notice is saved as delivered (claimed) before the pushes go out, so a reply sent to one of them meanwhile
        does not carry it too. A push counts against the monthly quota; when it reached nobody (not a friend, the
        quota used up) the notice goes back in the queue and the next reply the bot sends one of them carries it for
        free (``_inbox``, ``flow_digest``). A redelivered webhook finds its notice saved already (``add_flow_notice``
        answers None) and pushes nothing. Without pushes (``Settings.push``) each card is filed in that
        administrator's 收件匣 instead (``_push_quietly``), which counts as reaching them: the claim stands, and the
        digest does not repeat what the cards say.
        """
        if not self.settings.admins:
            return
        notice = {"chat_id": chat_id, "user_id": user_id, "poster": self._uploader(user_id, chat_id),
                  "sent_at": sent_at, "status": status, "flow_date": flow.day.isoformat() if flow.day else None,
                  "detail": detail,
                  "summary": "、".join(flow_totals(flow, "dep" if departures(flow) else "arr")) or None}
        number = self.store.add_flow_notice(**notice, claimed=True)
        if number is None:
            return
        message = flow_report(notice)
        if not sum(1 for admin in self.settings.admins if self._push_quietly(admin, [message])):
            self.store.unmark_flow_notices([number])
            print(f"人流資料讀取結果沒有推播到任何管理員（#{number}）：等管理員下一次收到回覆時一起送出", flush=True)

    def _receive_flow(self, flow, raw, token, user_id):
        """The same message pasted privately: kept for this user only and answered with its forecast."""
        if not departures(flow):   # kept, it would hide the group's message of that day from this user
            return self._reply(token, [text("這則航班人數只有到站／入境，人流預測只看離站／出境的人數，所以沒有記下。\n"
                                            "請貼含有「離站」或「出境」段落的訊息。")])
        day = flow.day or today()
        self.store.add_flow(user_id, day.isoformat(), raw, user_id)
        note = f"已收到 {day_label(day)}的航班人數。"
        if not flow.day:
            note += "\n訊息裡沒有日期，先當成今天；日期不對的話，請連同日期那一行重貼一次。"
        note += "\n私訊貼的資料只有你自己查得到；貼在群組的訊息我會自動記下，大家都能查。"
        return self._show_flow(token, user_id, day=day, note=note)

    def _flow_actions(self, user_id, day) -> list[dict]:
        """今天／明天 buttons, when both days have a message."""
        current = today()
        days = [(label, d) for label, d in (("今天", current), ("明天", current + timedelta(days=1)))
                if self.store.flow_on(user_id, d)]
        if len(days) < 2:
            return []
        return [{"type": "action", "action": {
            "type": "postback", "label": f"{'✓' if d == day else '看'}{label} {d.month}/{d.day}",
            "data": f"flow|{d.isoformat()}", "displayText": f"人流預測 {d.month}/{d.day}"}} for label, d in days]

    def _show_flow(self, token, user_id, *, day=None, note=None):
        """Grades for ``day``; without a date, today's, or tomorrow's while that is the only one posted."""
        current = today()
        candidates = (day,) if day else (current, current + timedelta(days=1))
        record = next((r for r in (self.store.flow_on(user_id, d) for d in candidates) if r), None)
        if not record:
            wanted = f" {day_label(day)}" if day else f"今天（{current.month}/{current.day}）"
            message = text(f"還沒有{wanted}的航班人數資料。\n{NO_FLOW_HINT}")
            others = self.store.flow_dates(user_id, 3)
            if others:
                message["text"] += f"\n\n可查的日期：{'、'.join(day_label(d) for d in others)}"
                message["quickReply"] = {"items": [{"type": "action", "action": {
                    "type": "postback", "label": f"看 {d.month}/{d.day}", "data": f"flow|{d.isoformat()}",
                    "displayText": f"人流預測 {d.month}/{d.day}"}} for d in others]}
            return self._reply(token, [message])
        day = date.fromisoformat(record["flow_date"])
        flow = passenger_flow.parse(record["raw"], near=day)
        if not flow:
            return self._reply(token, [text(f"{day_label(day)}的航班人數訊息讀不出來，請重新貼一次。")])
        roster = self.store.roster_on(user_id, day)
        staff = None
        if roster:
            try:
                staff = passenger_flow.first_line_staff(self.analyzer.result(roster["run_id"]))
            except Exception as exc:   # the forecast still works from the passengers alone
                print(f"人流預測讀不到班表人力（{roster['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
        card = flow_card(day, flow, passenger_flow.forecast(flow, staff),
                         source=flow_source_label(record, user_id), has_roster=bool(roster), now=taipei_now())
        actions = ([self._slots_action(roster)] if roster else []) + self._flow_actions(user_id, day)
        if actions:
            card["quickReply"] = {"items": actions}
        return self._reply(token, ([text(note)] if note else []) + [card])

    # -- 意見回饋 -------------------------------------------------------------------
    def _receive_feedback(self, body, token, chat_id, user_id):
        """「意見 …」: keep the message with its sender's name and pass it on to the administrators."""
        body = body.strip()
        if not body:
            return self._reply(token, [{**text(FEEDBACK_HOW),
                                        "quickReply": {"items": [fill_action("意見回饋", "意見 ")]}}])
        if self.store.feedback_count(user_id, time.time() - 86400) >= FEEDBACK_A_DAY:
            return self._reply(token, [text("今天已經收到你很多則意見了，謝謝！其餘的請明天再傳，或直接告訴管理員。")])
        user = self.store.user(user_id) or {}
        display = user.get("name") or self.transport.display_name(user_id, chat_id)
        viewed = self.store.recently_viewed_roster(user_id)
        context = source_label(viewed, user_id).replace("你私訊", "私訊") if viewed else None
        number = self.store.add_feedback(user_id, user.get("staff_name"), display, body[:FEEDBACK_LENGTH], context)
        print(f"收到意見 #{number}（{user.get('staff_name') or display or user_id}）", flush=True)
        self._push_feedback()
        who = f"你的姓名（{user['staff_name']}）" if user.get("staff_name") else "你的 LINE 名稱"
        note = f"收到了，謝謝！這則意見會連同{who}交給管理員。"
        if len(body) > FEEDBACK_LENGTH:
            note += f"\n內容比較長，只留下前 {FEEDBACK_LENGTH} 個字。"
        return self._reply(token, [text(note)])

    def _push_feedback(self) -> bool:
        """Push the administrators what they have not been shown — at most once a day (Taiwan time).

        A push counts against the monthly quota, so one a day tells them something has arrived; whatever
        comes later that day waits for their next message to the bot, where a reply carries it for free
        (``_inbox``). A push that reaches nobody (no quota left, not a friend) does not use up the day.
        Without pushes (``Settings.push``) there is none: the feedback only ever rides on their replies, which also
        keeps it out of their 收件匣.
        """
        if not self.settings.admins:
            print("有新的意見回饋，但沒有設定管理員（LINE_V2_ADMIN_USER_IDS），只存進資料庫", flush=True)
            return False
        if not self.settings.push:
            return False
        midnight = datetime.combine(today(), datetime.min.time(), TAIPEI).timestamp()
        if (self.store.last_feedback_push() or 0) >= midnight:
            return False
        message, shown = feedback_digest(self.store.undelivered_feedback())
        if not shown or not sum(1 for admin in self.settings.admins if self._push_quietly(admin, [message])):
            return False
        self.store.mark_feedback([item["id"] for item in shown], pushed=True)
        return True

    def _inbox(self, messages) -> tuple[list[dict], dict[str, list[int]]]:
        """(messages to put in front of a reply of ``messages``, what they show) for the person this event is from
        (``_turn``): an administrator's unseen feedback and 人流資料 notices (``feedback_digest``, ``flow_digest``), then
        the groups waiting in their 收件匣 (``_file``). Nothing for anyone else, nor in another thread (an interim
        reply, ``_interim``). The 收件匣 groups are taken (``Store.claim_mail``) before the reply goes out, so another
        reply to the same person sent meanwhile does not carry them too; ``_delivered`` marks the rest seen once the
        reply went out, and ``_undelivered`` puts the groups back when it did not.

        Once per event, and only in the room the reply leaves: LINE takes five messages and shows the quick replies of
        the last one, so these go first and the reply keeps all of its own, in order. The digests come first: a
        message each however much they hold (with room for one only they share it, the feedback cut to what still
        fits). Then the 收件匣, oldest first, a whole group at a time (a roster report and its 勤務人員 picture stay
        together): the first group that does not fit waits, and so does every one after it. With some waiting and room
        left, one line says how many and how to see them (``mail_left``). Whatever is not shown waits for the next
        reply.
        """
        room, first, shown = REPLY_LIMIT - len(messages), [], {}
        if room < 1:
            return first, shown
        if getattr(self._turn, "admin", False):
            self._turn.admin = False
            notice, notices = flow_digest(self.store.undelivered_flow_notices())
            pending = [] if getattr(self._turn, "listed", False) else self.store.undelivered_feedback()
            if notice and pending and room == 1:   # one text for both: notices are short, feedback takes the rest
                digest, feedback = feedback_digest(pending, limit=FEEDBACK_ROOM - len(notice["text"]) - 2)
                first = [text(digest["text"] + "\n\n" + notice["text"])]
            else:
                digest, feedback = feedback_digest(pending)
                first = [message for message in (digest, notice) if message]
            shown.update(feedback=[item["id"] for item in feedback], notices=[item["id"] for item in notices])
        user, room = getattr(self._turn, "user", None), room - len(first)
        if user and room > 0:
            self._turn.user = None
            groups = self.store.undelivered_mail(user)
            taken = []
            for group in groups:
                if len(group["messages"]) > room:
                    break
                taken.append(group)
                room -= len(group["messages"])
            left = len(groups) - len(taken)
            claimed = set(self.store.claim_mail([group["id"] for group in taken]))   # another reply may have them
            taken = [group for group in taken if group["id"] in claimed]
            first += [message for group in taken for message in group["messages"]]
            if left and room > 0:
                first.append(text(mail_left(left, button=self._is_admin(user))))
            shown["mail"] = [group["id"] for group in taken]
        return first, shown

    def _show_mailbox(self, token, user_id):
        """「收件匣」 (「辨識回報」 on the administrators' menu, 「我的訊息」): only what waits in it, oldest first, as many
        whole groups as one reply holds; with more left, the last message says how many and that sending it again
        shows them, and a quick reply does. Nothing else rides on this reply (``_inbox``): the digests wait for the
        next one. Open to everybody (the 名冊守門員 lets it through): an uploader's notices land here too."""
        self._turn.admin, self._turn.user = False, None
        if not token:   # nothing can show them (a redelivered event): they wait
            return
        groups = self.store.undelivered_mail(user_id)
        if not groups:
            words = "📭 沒有新的訊息。"
            if self._is_admin(user_id):
                words += ("\n群組有新的班表照片或航班人數訊息時，辨識回報會放在這裡。" if not self.settings.push
                          else "\n群組班表與人流資料的辨識回報會直接私訊給你。")
            return self._reply(token, [text(words)])
        taken, used = [], 0
        for group in groups:
            if used + len(group["messages"]) > REPLY_LIMIT:
                break
            taken.append(group)
            used += len(group["messages"])
        if len(taken) < len(groups) and used == REPLY_LIMIT and len(taken) > 1:
            used -= len(taken.pop()["messages"])   # room for the line that says how many are left
        left = len(groups) - len(taken)
        claimed = set(self.store.claim_mail([group["id"] for group in taken]))   # a reply sent meanwhile may have them
        taken = [group for group in taken if group["id"] in claimed]
        if not taken:   # all of them went out with another reply just now
            return self._reply(token, [text("📭 剛剛的回覆已經帶上收件匣的訊息了。")])
        messages = [dict(message) for group in taken for message in group["messages"]]
        if left:   # 則 counts groups: a report and its picture are one, and a reply holds five messages
            if used < REPLY_LIMIT:
                messages.append(text(f"📬 還有 {left} 則訊息，再傳「收件匣」繼續看。"))
            more = {"type": "action", "action": message_action("📬 繼續看", "收件匣")}
            items = messages[-1].get("quickReply", {}).get("items", [])
            messages[-1]["quickReply"] = {"items": [*items, more][-13:]}   # LINE: 13 at most
        ids = [group["id"] for group in taken]
        try:
            self.transport.reply(token, messages)
        except Exception as exc:
            dropped = self.store.release_mail(ids, refused=refused_reply(exc))
            if dropped:
                print(f"LINE 一再拒收 {user_id} 收件匣裡的訊息 #{'、#'.join(map(str, dropped))}，已略過：{exc}", flush=True)
            raise

    def _list_feedback(self, token):
        """「意見列表」 (administrators): the newest messages, oldest of them first."""
        self._turn.listed = True   # the list holds them all: no separate feedback in front of it
        items = self.store.recent_feedback(FEEDBACK_SHOWN)
        if not items:
            return self._reply(token, [text("目前沒有任何意見回饋。")])
        _, newest = feedback_digest(items)   # as many of the newest as one message holds…
        message, shown = feedback_digest(newest[::-1], "📮 最近的意見回饋")   # …read from the oldest down
        self.store.mark_feedback([item["id"] for item in shown])
        return self._reply(token, [message])

    # -- 姓名申請 (the name lock) -------------------------------------------------------
    def _approvers(self, besides=None) -> list[str]:
        """Who answers a name request: the administrators (``Settings.admins``, not the 幹部), each once, and never
        the one asking. None of them: the name lock is off and names change at once."""
        return [user for user in dict.fromkeys(self.settings.admins) if user != besides]

    def _holders(self, request) -> list[dict]:
        """The other LINE accounts that already registered the name a request asks for."""
        gated = bool(self._roster_names())
        return [{**person, "role": self._role(person["user_id"]),
                 "kept_out": gated and not self._can_manage(person["user_id"])}   # without the name: no班表 for them
                for person in self.store.users_named(request["requested_name"])
                if person["user_id"] != request["user_id"]]

    def _request_name(self, token, chat_id, user_id, current, target, approvers, pending, released=None):
        """File a request for ``target`` and push it to every approver, one card each. ``current``: the name the
        account has now (None for a first one, or after 「解除」, whose name is ``released``). The requester hears
        back once one of them answers (``_decide_name``); a new request replaces the one still pending.

        Only one request per person every ``NAME_PUSH_GAP`` seconds is pushed: switching names over and over would
        otherwise spend the monthly push quota. A later one waits under 「姓名申請」. Without pushes the cards are filed
        in the approvers' 收件匣 instead (``_push_quietly``), under the same gap: one person switching names must not
        fill it, where ``MAILBOX_KEEP`` would drop the reports waiting with them."""
        recent = self.store.recently_pushed(user_id, time.time() - NAME_PUSH_GAP)
        news = NEWS[self.settings.push]   # how the requester hears the answer
        if pending and pending["requested_name"] == target and (pending.get("pushed_at") or recent):
            # asked again while waiting: nobody is pushed twice
            return self._reply(token, [text(f"⏳「{target}」的申請已經送出了，還在等管理員確認，{news}。")])
        user = self.store.user(user_id) or {}
        if pending and pending["requested_name"] == target:   # filed while pushes were held back: push it now
            request, replaced = pending, None
        else:
            display = self.transport.display_name(user_id, chat_id) or user.get("name")
            number, replaced = self.store.add_name_request(user_id, current, target, display, released)
            request = self.store.name_request(number)
        number, before = request["id"], current or released or "未登記"
        if recent:
            reached = None
            print(f"姓名申請 #{number}：{user_id}「{before}」→「{target}」，"
                  f"{'剛剛才私訊過，這次不再私訊' if self.settings.push else '剛剛才放進收件匣，這次不再放'}", flush=True)
        else:
            card = name_request_card([(request, self._holders(request))])
            reached = sum(1 for approver in approvers if self._push_quietly(approver, [card]))
            if reached:
                self.store.mark_name_request_pushed(number)
            print(f"姓名申請 #{number}：{user_id}「{before}」→「{target}」，"
                  f"已{'私訊' if self.settings.push else '放進收件匣給'} {reached}/{len(approvers)} 位管理員", flush=True)
        if current:
            lines = [f"📮 改名申請送出了：{current} → {target}",
                     f"姓名登記後不能自己改（免得有人用別人的姓名查看資料），已經交給管理員確認，{news}。",
                     f"在那之前，你的姓名還是「{current}」。"]
        elif released and released != target:
            lines = [f"📮 你之前登記過「{released}」，換成「{target}」要先交給管理員確認，{news}。",
                     "（姓名登記後不能自己改，免得有人用別人的姓名查看資料。）"]
        else:
            lines = [f"📮「{target}」已經有其他 LINE 帳號登記了，所以這次登記要先交給管理員確認，{news}。",
                     "（免得有人用別人的姓名查看資料。如果這是你的姓名，請放心等一下。）"]
        if replaced:
            lines.append(f"之前申請的「{replaced['requested_name']}」已經取消，以這次為準。")
        if reached is None:
            lines.append(AGAIN[self.settings.push])
        elif not reached:
            print(f"⚠ 姓名申請 #{number} 沒有送到任何管理員（沒加好友、封鎖，或本月額度用完？），"
                  "管理員可以私訊「姓名申請」查看", flush=True)
            lines.append("⚠ 目前私訊不到管理員，請直接跟管理員說一聲你送出了申請。")
        return self._reply(token, [{**text("\n".join(lines)), "quickReply": {"items": [
            {"type": "action", "action": message_action("我的設定")}]}}])

    def _decide_name(self, token, user_id, number, approve, built=None):
        """✅ or ❌ on a name request, from an approver: the first answer counts and the requester is told either
        way. A request that was answered, replaced or withdrawn meanwhile only says so (``_name_resolved``).

        A ✅ for a name that is not on the 名冊 (it has left since the request was filed) is turned down (the 名冊守門員):
        nothing changes, the request still waits, and the approver is told how to go on."""
        request = self.store.name_request(number)
        if not request:
            return
        if request["user_id"] == user_id:
            return self._reply(token, [text("自己的申請要由其他管理員確認喔。")])
        new = request["requested_name"]
        if approve and request["status"] == "pending" and not self._may_register(request["user_id"], new):
            return self._reply(token, [{**text(
                f"⚠️「{new}」目前不在名冊上，所以這次沒有同意，{requester_label(request)}的姓名沒有變更，申請還在等。\n"
                f"要讓對方用這個姓名，請先把「{new}」加進名冊（傳「新增人員 {new}」），再按一次同意；不然請按不同意。"),
                "quickReply": {"items": [{"type": "action", "action": message_action("➕ 加入名冊", f"新增人員 {new}")}]}}])
        shown = built or request["created_at"]   # what the tapped card could show (older cards: when it was filed)
        released = []
        if approve and request["status"] == "pending":
            since, holders = self.store.one_name_since(), self._holders(request)
            if holders and since and shown + 1 < since:
                # A card from before a yes moved the name: it says 「原本的帳號不受影響」. Nothing changes on its word
                return self._reply(token, [text("⚠️ 這張卡片是舊的。現在一個姓名只能登記在一個 LINE 帳號："
                                                f"同意會把「{new}」轉給申請人，原本登記的帳號會被解除。請看過下面這張再決定一次。"),
                                           name_request_card([(request, holders)])])
            # A name belongs to one LINE account: a yes moves it, and whoever had it is released. One step in the
            # database, so nobody who registered meanwhile is released without the approver having seen them
            released = self.store.approve_name_request(number, user_id, shown)
            if released is LOOK_AGAIN:   # the card the approver tapped did not show them: nothing changes yet
                return self._reply(token, [text(f"⚠️ 申請送出後，「{new}」已經有其他 LINE 帳號登記了。"
                                                "請看過下面這張再決定一次。"),
                                           name_request_card([(request, self._holders(request))])])
            if released is None:
                return self._name_resolved(token, user_id, self.store.name_request(number))
        elif not self.store.decide_name_request(number, "approved" if approve else "refused", user_id):
            return self._name_resolved(token, user_id, self.store.name_request(number))
        old = request["current_name"]
        print(f"{self._who(user_id)}（{user_id}）{'同意' if approve else '不同意'}姓名申請 #{number}："
              f"{request['user_id']}「{old or '未登記'}」→「{new}」"
              + (f"，解除原本的帳號 {'、'.join(account['user_id'] for account in released)}" if released else ""),
              flush=True)
        lines = [f"✅ 已同意：{request_words(request)}，{TELL[self.settings.push]}。" if approve
                 else f"❌ 已婉拒：{request_words(request)}的申請，{TELL[self.settings.push]}。"]
        if released:
            lines.append(self._released_note(new, released))
        waiting = sum(1 for other in self.store.pending_name_requests() if other["user_id"] != user_id)
        answer = text("\n".join(lines + ([f"還有 {waiting} 筆姓名申請等你確認。"] if waiting else [])))
        if waiting:
            answer["quickReply"] = {"items": [{"type": "action", "action": message_action("📮 姓名申請", "姓名申請")}]}
        self._reply_quietly(token, [answer])   # first: building the requester's 「我的班表」 can take a while
        if not self._push_quietly(request["user_id"], self._name_answer(request, approve)):
            self._push_quietly(user_id, [text(f"⚠ 私訊沒有送到 {requester_label(request)}（可能封鎖了機器人），"
                                              "結果請另外告訴對方。")])

    def _name_answer(self, request, approve) -> list[dict]:
        """What the requester is pushed: the answer, and with a yes their 「我的班表」 under the new name. Without pushes
        only the answer, which waits in their 收件匣: read later, a card of the moment it was made would show an hour
        long gone, and the 「我的班表」 it points to (which carries it) shows the one of the moment."""
        new, user_id = request["requested_name"], request["user_id"]
        if not approve:
            if not request["current_name"]:
                return [text(f"🙏「{new}」的登記申請沒有通過，目前還沒有登記姓名。\n如果這是你的姓名，請直接聯絡管理員。")]
            now = (self.store.user(user_id) or {}).get("staff_name")   # none when they asked after 「解除」
            return [text("🙏 改名申請沒有通過，" + (f"姓名維持「{now}」。" if now else "目前沒有登記姓名。")
                         + "\n有問題請直接聯絡管理員。")]
        words = (f"✅ 改名申請通過了！你的姓名現在是「{new}」，「我的班表」和本月統計都會用新的姓名。"
                 if request["current_name"] else f"✅ 姓名登記通過了：{new}。之後點「我的班表」就能查看。")
        try:
            day = (self._my_day_messages(user_id, new) or []) if self.settings.push else []
        except Exception as exc:   # the answer matters more than the card
            print(f"姓名申請通過後讀不到 {user_id} 的班表：{type(exc).__name__}: {exc}", flush=True)
            day = []
        return [text(words), *day]

    def _name_resolved(self, token, user_id, request):
        """A ✅／❌ on a request that is no longer pending: who answered it and how, or that its requester
        withdrew it. Nothing changes."""
        status, asked = request["status"], request_words(request)
        if status == "replaced":
            said = f"申請人後來送出了新的申請，這筆作廢（{asked}）。"
        elif status == "cancelled":
            said = f"申請人自己取消了（{asked}）。"
        else:
            decider = request["decided_by"]
            who = self._who(decider)
            by = "你" if decider == user_id else who if who != decider else \
                ("管理員 " if self._is_admin(decider) else "") + short_id(decider)
            said = f"{by}在 {request_time(request['decided_at'])} {'同意' if status == 'approved' else '不同意'}了（{asked}）。"
        answer = text(f"這筆姓名申請已經處理過了：{said}這次沒有變更。")
        if status in ("replaced", "cancelled") and self.store.pending_name_request(request["user_id"]):
            # the newer one may not have been pushed: it is on the list
            answer["quickReply"] = {"items": [{"type": "action", "action": message_action("📮 姓名申請", "姓名申請")}]}
        return self._reply(token, [answer])

    def _show_name_requests(self, token, user_id):
        """「姓名申請」: an approver (an administrator) gets the pending requests (others', not their own) on one card,
        each with its ✅／❌; anyone else, a 幹部 too, how to ask and where their own request stands."""
        if not self._is_admin(user_id):
            user, pending = self.store.user(user_id) or {}, self.store.pending_name_request(user_id)
            locked = bool(self._approvers(besides=user_id))
            if pending:
                words = (f"⏳ 你申請的「{pending['requested_name']}」在 {request_time(pending['created_at'])} 送出，"
                         f"還在等管理員確認，{NEWS[self.settings.push]}。")
            elif user.get("staff_name") and locked:
                words = f"你的姓名是「{user['staff_name']}」。要改姓名，請輸入「設定 新的姓名」，會交給管理員確認。"
            elif user.get("staff_name"):
                words = f"你的姓名是「{user['staff_name']}」。要改姓名，再傳一次「設定 新的姓名」即可。"
            else:
                words = "請先輸入「設定 你的姓名」登記姓名。"
            label = ("申請改姓名" if locked else "改姓名") if user.get("staff_name") else "設定姓名"
            return self._reply(token, [{**text(words), "quickReply": {"items": [fill_action(label, "設定 ")]}}])
        waiting = [request for request in self.store.pending_name_requests() if request["user_id"] != user_id]
        if not waiting:
            return self._reply(token, [text("目前沒有等待確認的姓名申請 👍")])
        shown = waiting[:NAME_SHOWN]
        return self._reply(token, [name_request_card([(request, self._holders(request)) for request in shown],
                                                     listing=True, more=len(waiting) - len(shown))])

    # -- 人員管理 (幹部) ---------------------------------------------------------------
    def _can_manage(self, user_id) -> bool:
        """幹部, and administrators, edit the staff list and the 幹部."""
        return self._is_admin(user_id) or self.store.is_officer(user_id)

    def _book(self) -> StaffBook:
        return StaffBook(self.settings.staff_names, self.settings.fixed_staff_codes, self._staff_lock)

    def _set_after(self, roster) -> set[str]:
        """The fixed codes set only after ``roster``'s day (``StaffBook.set_after``): daily codes on it."""
        return self._book().set_after(roster_day(roster))

    def _who(self, user_id) -> str:
        """How a change made by ``user_id`` is signed in the staff file and the log."""
        user = self.store.user(user_id) or {}
        title = next((o["title"] for o in self.store.officers() if o["user_id"] == user_id and o["title"]), None)
        return " ".join(part for part in (title, user.get("staff_name") or user.get("name")) if part) or user_id

    def _staff_command(self, value, compact, token, user_id) -> bool:
        """The 幹部 commands of 「人員管理」; ``False`` when ``value`` is none of them."""
        add, remove = ADD_STAFF.fullmatch(value), REMOVE_STAFF.fullmatch(value)
        code = None if compact in FIXED_LIST else FIXED_CODE.fullmatch(value)
        if code and code.group(1) == "代號" and not self._names_a_code(code.group(2)):
            code = None   # 「代號 …」 without a fixed code in it is not ours (「代號 72」 stays as it was)
        add_officer, remove_officer = ADD_OFFICER.fullmatch(value), REMOVE_OFFICER.fullmatch(value)
        release = RELEASE.fullmatch(value)
        if not (compact in STAFF_MENU + STAFF_LIST + STAFF_HELP + OFFICER_LIST + FIXED_LIST + BINDING_LIST + RELEASE_ALONE
                or add or remove or code or add_officer or remove_officer or release):
            return False
        if (release or compact in RELEASE_ALONE) and not self._can_manage(user_id):
            self._reply(token, [text(RELEASE_ONLY)])
        elif compact in RELEASE_ALONE:
            self._reply(token, [{**text("✏️ 在「解除登記 」後面輸入姓名，例如：解除登記 王小明\n"
                                        "那個姓名的 LINE 帳號會被解除、看不到班表，本人之後要重新登記。"
                                        "誰登記了哪個姓名，看「綁定清單」。"),
                                 "quickReply": {"items": [fill_action("✏️ 解除登記", "解除登記 "),
                                                          {"type": "action", "action": message_action("🔗 綁定清單", "綁定清單")}]}}])
        elif release:
            self._release(token, user_id, release.group(1), release.group(2))
        elif not self._can_manage(user_id):
            # Loose words (a bare 「役男」…) and code phrases that set nothing are not commands for them: they
            # get the usual answer, as before. Only what a 幹部 would do is refused.
            parsed = self._code_and_name(code.group(2)) if code else None
            if compact in LOOSE_WORDS or (code and (not parsed or not parsed[1] or QUESTION.search(parsed[1]))):
                return False
            self._reply(token, [text(OFFICERS_ONLY)])
        elif compact in STAFF_MENU:
            self._staff_menu(token, user_id)
        elif compact in STAFF_HELP:
            self._reply(token, [{**text(staff_help_text()), "quickReply": {"items": self._staff_actions()}}])
        elif compact in STAFF_LIST:
            book = self._book()
            card = staff_roster_card(book.names(), book.fixed(), self.store.registered())
            self._reply(token, [{**card, "quickReply": {"items": self._staff_actions()}}])
        elif compact in FIXED_LIST:
            self._show_fixed_codes(token)
        elif compact in OFFICER_LIST:
            self._list_officers(token)
        elif compact in BINDING_LIST:
            self._show_bindings(token)
        elif not self._book().editable:
            self._reply(token, [text("這台機器人沒有設定名冊檔，無法在 LINE 修改名冊。")])
        elif add or remove:
            self._edit_staff(token, user_id, split_names((add or remove).group(1)), adding=bool(add))
        elif code:
            self._set_fixed_code(token, user_id, code.group(2))
        elif add_officer:
            self._add_officer(token, user_id, add_officer.group(1), add_officer.group(2))
        else:
            self._remove_officer(token, user_id, remove_officer.group(1))
        return True

    def _staff_postback(self, parts, token, user_id) -> bool:
        """The tiles and code cells of the 人員管理 cards. They also open the keyboard on the command (LINE
        12.6+); the answer says how to go on, for a LINE that does not."""
        if parts == ["guide", "設定姓名"]:   # the welcome card's tile: anyone
            self._reply(token, [{**text(f"✏️ 在「設定 」後面打你的姓名送出就好，例如：設定 {EXAMPLE_NAME}"
                                        + ("\n姓名要和隊上名冊上的一模一樣。" if self._roster_names() else "")),
                                 "quickReply": {"items": [fill_action("✏️ 設定姓名", "設定 ")]}}])
            return True
        if parts[0] == "guide" and len(parts) == 2 and parts[1] in ("新增人員", "刪除人員", "新增幹部", "移除幹部"):
            return self._staff_command(parts[1], parts[1], token, user_id)
        if parts[0] == "setcode" and len(parts) == 4 and parts[1] in FIXED_CODES and NAME_PATTERN.fullmatch(parts[2]) \
                and parts[3] in ("new", "empty"):
            if not self._can_manage(user_id):
                self._reply(token, [text(OFFICERS_ONLY)])
            elif self._book().editable:
                self._set_fixed_code(token, user_id, f"{parts[1]} {parts[2]}", confirmed=parts[3] == "new",
                                     expect_empty=parts[3] == "empty")
            return True
        if parts[0] == "fixed" and len(parts) == 2 and parts[1] in FIXED_CODES:
            if not self._can_manage(user_id):
                self._reply(token, [text(OFFICERS_ONLY)])
            else:
                self._ask_fixed_code(token, parts[1])
            return True
        return False

    def _staff_actions(self) -> list[dict]:
        return [{"type": "action", "action": message_action("📋 人員名冊", "人員名冊")},
                fill_action("➕ 新增人員", "新增人員 "), fill_action("➖ 刪除人員", "刪除人員 "),
                {"type": "action", "action": message_action("📌 固定代號", "固定代號")},
                {"type": "action", "action": message_action("👥 人員管理", "人員管理")}]

    def _staff_menu(self, token, user_id=None):
        book = self._book()
        names, fixed, registered = book.names(), book.fixed(), self.store.registered()
        everyone = set(names) | set(fixed.values())
        admin = self._is_admin(user_id)   # 姓名申請 and 意見列表 are the administrators' alone: tiles only for them
        asks = (sum(1 for request in self.store.pending_name_requests() if request["user_id"] != user_id)
                if admin else 0)
        unseen = len(self.store.undelivered_feedback()) if admin else 0   # this reply carries them too (``_inbox``)
        card = staff_menu_card(names=len(everyone), fixed=len(fixed),
                               registered=sum(1 for name in registered if name in everyone),
                               unlisted=sum(1 for name in registered if name not in everyone),
                               officers=len(self.store.officers()), admin=admin, requests=asks, feedback=unseen)
        card["quickReply"] = {"items": [*self._staff_actions()[1:3], fill_action("🎖️ 設定役男", "役男 "),
                                        fill_action("🙋 新增幹部", "新增幹部 ")]
                              + ([{"type": "action", "action": message_action("📮 姓名申請", "姓名申請")}] if asks else [])}
        return self._reply(token, [card])

    def _edit_staff(self, token, user_id, names, *, adding):
        """「新增人員 王小明 陳大華」 / 「刪除人員 王小明」."""
        verb = "新增人員" if adding else "刪除人員"
        if not names:
            example = "新增人員 王小明 陳大華" if adding else "刪除人員 王小明"
            return self._reply(token, [{**text(f"✏️ 在「{verb} 」後面輸入姓名就好，好幾位用空白隔開。\n例如：{example}"),
                                        "quickReply": {"items": [fill_action(f"✏️ {verb}", f"{verb} "),
                                                                 *self._staff_actions()[4:]]}}])
        valid = [name for name in names if NAME_PATTERN.fullmatch(name)]
        lines = []
        book = self._book()
        if adding:
            stamp = f"{today().isoformat()} {self._who(user_id)} 新增"
            added, known = book.add(valid, stamp) if valid else ([], [])
            if added:
                lines.append(f"✅ 已加入名冊：{'、'.join(added)}（現在共 {len(book.names())} 位）")
                print(f"{self._who(user_id)}（{user_id}）新增人員：{'、'.join(added)}", flush=True)
            if known:
                lines.append(f"👌 已經在名冊裡：{'、'.join(known)}")
            if added:
                lines.append("有固定代號（役男的中文代號或英文代號）的話，按下面的「📌 給 … 代號」，點空白的格子就好。")
        else:
            removed, missing, freed = book.remove(valid, today().isoformat()) if valid else ([], [], {})
            if removed:
                lines.append(f"✅ 已從名冊移除：{'、'.join(removed)}（現在共 {len(book.names())} 位）")
                print(f"{self._who(user_id)}（{user_id}）刪除人員：{'、'.join(removed)}", flush=True)
            if freed:
                lines.append("📌 也清空了固定代號：" + "、".join(f"{code}（{name}）" for code, name in freed.items()))
            still = [name for name in removed if self.store.users_named(name)]
            if still:
                lines.append(f"ℹ️ {'、'.join(still)} 仍有 LINE 帳號登記這個姓名，班表上看到這個姓名時還是會認得；"
                             "對方傳「解除」才會取消登記，你也可以傳「" + "」「".join(f"解除登記 {name}" for name in still)
                             + "」。")
            if missing:
                lines.append(f"❓ 名冊裡沒有：{'、'.join(missing)}")
        invalid = [name for name in names if name not in valid]
        if invalid:
            lines.append(f"⚠️ 姓名要 2–5 個中文字，沒有處理：{'、'.join(invalid)}")
        items = self._staff_actions()
        if adding and added:   # the newest first: a code is usually what comes next
            items = [{"type": "action", "action": message_action(f"📌 給{name}代號", f"役男 {name}")}
                     for name in added[:4]] + items[:1] + items[3:]
        return self._reply(token, [{**text("\n".join(lines)), "quickReply": {"items": items}}])

    def _show_fixed_codes(self, token, note: str | None = None, changed: str | None = None, first=()):
        """The 固定代號 card under ``note``; ``first``: quick replies for what was just done, before the usual ones."""
        card = fixed_codes_card(self._book().fixed(), changed=changed, registered=self.store.registered())
        card["quickReply"] = {"items": [*first, fill_action("✏️ 設定役男", "役男 "), fill_action("✏️ 英文代號", "代號 "),
                                        *self._staff_actions()[:1], *self._staff_actions()[4:]]}
        return self._reply(token, ([text(note)] if note else []) + [card])

    def _known_names(self) -> set[str]:
        """Everyone the bot knows by name: the list, the code holders and who registered."""
        book = self._book()
        return set(book.names()) | set(book.fixed().values()) | set(self.store.registered())

    def _ask_code_for(self, token, name):
        """「役男 王小明」: the grid with every blank cell giving 王小明 that code in one tap.

        Only for somebody the bot knows: anything else typed after 役男 (「役男 你好」, a new person not yet on
        the list) is offered 新增人員 first, so a stray phrase never ends up as a person with a code.
        """
        if name not in self._known_names():
            note = f"📌 名冊裡還沒有「{name}」。是新同仁的話，先按下面的「➕ 新增{name}」，再給他代號。"
            if name[0] in CHINESE_CODES and len(name) == 3:   # 丁小明: or 小明 on code 丁?
                note += f"\n如果是要把「{name[0]}」給 {name[1:]}，請打「役男 {name[0]} {name[1:]}」。"
            return self._reply(token, [{**text(note), "quickReply": {"items": [
                {"type": "action", "action": message_action(f"➕ 新增{name}", f"新增人員 {name}")},
                *self._staff_actions()[3:5]]}}])
        fixed = self._book().fixed()
        held = next((code for code, holder in fixed.items() if holder == name), None)
        note = (f"📌 {name} 目前是「{held}」。點一個空白的格子就會換過去。" if held else
                f"📌 要給 {name} 哪個代號？點一個空白的格子就好。")
        card = fixed_codes_card(fixed, changed=held, for_name=name, registered=self.store.registered())
        free = [code for code in FIXED_CODES if code not in fixed][:8]
        card["quickReply"] = {"items": [
            *({"type": "action", "action": {**claim_action(code, name), "label": f"{code_kind(code)[0]} {code}"}}
              for code in free),
            *self._staff_actions()[4:]]}
        return self._reply(token, [text(note), card])

    def _ask_fixed_code(self, token, code):
        """A code cell was tapped (or 「役男 丁」 typed): who has it, and how to set, change or clear it."""
        emoji, kind, word = code_kind(code)
        holder = self._book().fixed().get(code)
        if holder:
            message = (f"{emoji} {kind}「{code}」目前是 {holder}。\n"
                       f"換人：在「{word} {code} 」後面輸入新的姓名"
                       + ("（原本那位會移出名冊）" if code in CHINESE_CODES else "")
                       + f"。\n清空：按下面的「清空 {code}」。")
        else:
            message = f"{emoji} {kind}「{code}」目前空著。\n在「{word} {code} 」後面輸入姓名就好，例如：{word} {code} 王小明"
        items = [fill_action(f"✏️ 填寫 {code}", f"{word} {code} ")]
        if holder:
            items.append({"type": "action", "action": message_action(f"🧹 清空 {code}", f"{word} {code} 取消")})
        items.append({"type": "action", "action": message_action("📌 固定代號", "固定代號")})
        return self._reply(token, [{**text(message), "quickReply": {"items": items}}])

    @staticmethod
    def _as_code(value: str) -> str | None:
        """「丁」, 「A」, 「a」 → the fixed code; anything else → None."""
        value = value.upper() if value.isascii() else value
        return value if value in FIXED_CODES else None

    def _names_a_code(self, rest: str) -> bool:
        """Does 「代號 …」 name a fixed code (「代號 A 王小明」, 「代號 王小明 A」, 「代號A王小明」), or try to with
        some other letter (「代號 O 王小明」: answered with how to write it)? 「代號 72」 does not."""
        tokens = [t for t in CODE_SEPARATORS.split(rest) if t]

        def letter(value):
            return len(value) == 1 and value.isascii() and value.isalpha()
        return bool(tokens) and (any(self._as_code(t) or letter(t) for t in tokens)
                                 or bool(self._as_code(tokens[0][0])) or letter(tokens[0][0]))

    def _code_and_name(self, rest: str) -> tuple[str, str] | None:
        """「丁 王小明」, 「王小明 丁」, 「丁、王小明」, 「丁王大同」, 「A 王小明」, 「a王小明」, 「丁」 → (code, name or "").

        Without a space a leading Chinese code is only split off when what follows is a known name or a full
        three-character name, and the whole is nobody's name: 「王志明」 and 「丁小明」 are people (丁 is also a
        surname), so they are asked about rather than saved as 芬玲 or 小明 on code 丁. Names are Chinese, so a
        leading English letter is always the code.
        """
        tokens = [t for t in CODE_SEPARATORS.split(rest) if t]
        if len(tokens) == 1:
            token = tokens[0]
            if self._as_code(token):
                return self._as_code(token), ""
            head, name = self._as_code(token[0]), token[1:]
            if head in ENGLISH_CODES:
                return head, name
            known = set(self._book().names()) | set(self._book().fixed().values()) | set(self.store.registered())
            if head and token not in known and (name in known or len(name) >= 3):
                return head, name
            return None
        if len(tokens) == 2:
            first, second = tokens
            if self._as_code(first):
                return self._as_code(first), second
            if self._as_code(second):
                return self._as_code(second), first
        return None

    def _set_fixed_code(self, token, user_id, rest, *, confirmed=False, expect_empty=False):
        """「役男 丁 簡志明」／「代號 A 簡志明」 sets a code, 「… 取消」 clears it, 「役男 丁」 tells who has it.

        A code set here applies from today's roster on; older rosters keep reading it off their own name
        table (``StaffBook.fixed_on``). Nothing is written for a name the bot does not know until the 幹部
        confirms with one tap (``confirmed``): 「代號 A 換人」 or a cut-off 「役男丁大同學」 must not become a
        person. ``expect_empty`` (a one-tap cell) asks before replacing someone who took the code since.
        """
        parsed = self._code_and_name(rest)
        alone = rest.strip()
        if not parsed and alone in LIST_WORDS:
            return self._show_fixed_codes(token)
        if not parsed and alone in CLEAR_WORDS:
            return self._show_fixed_codes(token, "🧹 要清空哪個代號？點有人的格子再按「清空」，或打「役男 丁 取消」。")
        if not parsed and NAME_PATTERN.fullmatch(alone):   # a name alone: which code? one tap on a blank cell
            return self._ask_code_for(token, alone)
        if not parsed:
            return self._reply(token, [{**text("📌 固定代號請這樣打：役男 丁 王小明，或 代號 A 王小明\n"
                                               "中文代號用甲乙丙丁戊己庚辛壬癸、子丑寅卯辰巳午未申酉，英文代號用 A 到 N；"
                                               "清空打「代號 A 取消」。"),
                                        "quickReply": {"items": [fill_action("✏️ 設定役男", "役男 "),
                                                                 fill_action("✏️ 英文代號", "代號 "),
                                                                 *self._staff_actions()[3:4]]}}])
        code, name = parsed
        if not name:
            return self._ask_fixed_code(token, code)
        _, kind, word = code_kind(code)
        book = self._book()
        if name in CLEAR_WORDS:
            before = book.clear_code(code, today().isoformat())
            if before:
                print(f"{self._who(user_id)}（{user_id}）清空{kind} {code}（{before}）", flush=True)
            note = (f"🧹 清空了{kind}「{code}」（原本是 {before}，他仍在名冊上）。" if before
                    else f"{kind}「{code}」本來就空著。")
            gone = []
            if before and code in CHINESE_CODES:   # a 役男 without a code: usually he has left
                note += "\n退伍或離隊的話，按下面的「移出」就會從名冊拿掉。"
                gone = [{"type": "action", "action": message_action(f"➖ 移出{before}", f"刪除人員 {before}")}]
            return self._show_fixed_codes(token, note, code, gone)
        if QUESTION.search(name):   # 「代號A是誰」, 「役男 丁 是誰」: who has it
            return self._ask_fixed_code(token, code)
        if not NAME_PATTERN.fullmatch(name):
            return self._reply(token, [text(f"⚠️ 姓名要 2–5 個中文字，例如「{word} {code} 王小明」。")])
        holder = book.fixed().get(code)
        leaves = f"{holder} 會移出名冊（還在隊上的話可以再加回）。" if holder and code in CHINESE_CODES else ""
        if expect_empty and holder and holder != name:
            return self._reply(token, [{**text(f"📌 「{code}」剛剛已經給了 {holder}。要換成 {name} 嗎？"
                                               + (f"\n換了以後 {leaves}" if leaves else "")), "quickReply": {"items": [
                {"type": "action", "action": message_action(f"🔁 換成{name}", f"{word} {code} {name}")},
                *self._staff_actions()[3:4]]}}])
        if not confirmed and name not in self._known_names():
            lines = [f"📌 名冊裡還沒有「{name}」。要把{kind}「{code}」給他，並把他加進名冊嗎？"]
            if holder:
                lines.append(f"「{code}」現在是 {holder}，會換成 {name}。{leaves}")
            if len([t for t in CODE_SEPARATORS.split(rest) if t]) == 1:   # typed without a space: a guess
                lines.append(f"如果「{code}{name}」是一個人的姓名，請按「固定代號」，再點空白的格子。")
            return self._reply(token, [{**text("\n".join(lines)), "quickReply": {"items": [
                {"type": "action", "action": {"type": "postback", "label": f"✅ 新增並給{code}",
                                              "data": f"setcode|{code}|{name}|new", "displayText": f"{word} {code} {name}"}},
                *self._staff_actions()[3:5]]}}])
        day = today()
        change = book.set_code(code, name, f"{day.isoformat()} {self._who(user_id)} 新增", day.isoformat())
        print(f"{self._who(user_id)}（{user_id}）設定{kind} {code}：{name}", flush=True)
        lines, back = [f"✅ {kind}「{code}」：{name}"], []
        if change["removed"]:   # a 役男 replaced on his code has left: not kept among the daily numeric codes
            gone = change["removed"]
            lines.append(f"原本是 {gone}：役男換人，已經把他移出名冊。")
            if any(not self._admitted(account["user_id"]) for account in self.store.users_named(gone)):
                lines.append(f"{gone} 登記的 LINE 帳號從現在起看不到班表。")   # not a 幹部 or an administrator: they stay in
            lines.append(f"如果他還在隊上，按下面的「加回{gone}」。")
            back = [{"type": "action", "action": message_action(f"➕ 加回{gone}", f"新增人員 {gone}")}]
            print(f"{self._who(user_id)}（{user_id}）役男 {code} 換人：{gone} 移出名冊", flush=True)
        elif change["before"]:
            lines.append(f"原本是 {change['before']}，他現在沒有代號。")
        if change["moved"]:
            lines.append(f"{name} 原本的代號「{change['moved']}」已清空。")
        if change["listed"]:
            lines.append(f"也把 {name} 加進了名冊。")
        lines.append(f"從 {day.month}/{day.day} 的班表開始套用；更早的班表照當天的姓名對照表。")
        return self._show_fixed_codes(token, "\n".join(lines), code, back)

    def _show_bindings(self, token):
        """「綁定清單」: every LINE account with a staff name, and what about them needs a look."""
        book = self._book()
        officers = {officer["user_id"]: officer["title"] for officer in self.store.officers()}
        card = binding_card(self.store.bindings(), book.names(), book.fixed(), officers=officers,
                            admins=self.settings.admins)
        card["quickReply"] = {"items": [fill_action("✏️ 解除登記", "解除登記 "), *self._staff_actions()[:1],
                                        *self._staff_actions()[4:]]}
        return self._reply(token, [card])

    def _release(self, token, user_id, name, which=None):
        """「解除登記 王小明」: take the name off the LINE account that has it (it changed hands, or someone took a
        name that is not theirs). With several accounts under it (from before a name belonged to one): which one."""
        actions = [{"type": "action", "action": message_action("🔗 綁定清單", "綁定清單")}, *self._staff_actions()[4:]]
        if not NAME_PATTERN.fullmatch(name):
            return self._reply(token, [{**text("⚠️ 姓名要 2–5 個中文字，例如：解除登記 王小明"),
                                        "quickReply": {"items": actions}}])
        accounts = [account for account in self.store.bindings() if account["staff_name"] == name]
        if not accounts:
            return self._reply(token, [{**text(f"沒有 LINE 帳號登記「{name}」。"), "quickReply": {"items": actions}}])

        def listing(accounts):
            """The accounts numbered, each told apart by its LINE name, the day it registered and the end of its
            user id, with one quick reply per number. The numbers are those of this list, the earliest first."""
            rows = "\n".join(f"{number}. {(account.get('name') or '').strip() or 'LINE 名稱未知'}｜{_registered_on(account)} 登記"
                             f"｜…{account['user_id'][-4:]}" for number, account in enumerate(accounts, 1))
            taps = [{"type": "action", "action": message_action(f"解除第 {number} 個", f"解除登記 {name} {number}")}
                    for number in range(1, min(len(accounts), 8) + 1)]
            return rows, taps

        if which is None and len(accounts) == 1 or (which or "").casefold() in RELEASE_ALL:
            chosen = accounts
        elif which and which.isdecimal() and 1 <= int(which) <= len(accounts):
            chosen = [accounts[int(which) - 1]]
        elif len(accounts) == 1:   # a number that fits nothing, for a name one account has
            rows, _ = listing(accounts)
            return self._reply(token, [{**text(
                f"「{name}」只登記在 1 個 LINE 帳號上：\n{rows}\n\n傳「解除登記 {name}」就會解除它。"),
                "quickReply": {"items": [{"type": "action", "action": message_action(f"解除登記 {name}")}, *actions]}}])
        else:
            rows, taps = listing(accounts)
            return self._reply(token, [{**text(
                f"「{name}」登記在 {len(accounts)} 個 LINE 帳號上，要解除哪一個？（依登記先後）\n{rows}\n\n"
                f"傳「解除登記 {name} 2」解除其中一個（數字是上面的編號），或「解除登記 {name} 全部」。"),
                "quickReply": {"items": [*taps, *actions]}}])
        for account in chosen:
            self.store.release(account["user_id"])
            print(f"{self._who(user_id)}（{user_id}）解除了「{name}」的登記：{account['user_id']}", flush=True)
        who = "、".join(line_label(account) for account in chosen)
        left = [account for account in accounts if account not in chosen]
        lines = [f"✅ 已解除「{name}」的登記（LINE：{who}）。" if len(chosen) == 1
                 else f"✅ 已解除「{name}」的 {len(chosen)} 個帳號（LINE：{who}）。"]
        out = self._kept_out(chosen)   # the 名冊守門員 keeps these out now (none of them while there is no 名冊)
        if out:
            lines.append("對方現在沒有登記姓名，看不到班表。" if len(chosen) == 1
                         else "這些帳號現在沒有登記姓名，看不到班表。" if len(out) == len(chosen)
                         else "其餘被解除的帳號現在沒有登記姓名，看不到班表。")
        lines += self._still_in(chosen)
        taps = []
        if left:   # the numbers have changed: show them again, so the next one is read off this list
            rows, taps = listing(left)
            lines.append(f"\n「{name}」還登記在 {len(left)} 個 LINE 帳號上（編號重新排過）：\n{rows}")
        elif self._roster_names() and name not in self._roster_names():
            lines.append(f"「{name}」不在名冊上：要再登記，得先傳「新增人員 {name}」。")
        else:
            lines.append(f"「{name}」現在沒有人登記：先傳「設定 {name}」的 LINE 帳號就會登記到"
                         "（已經登記過別的姓名的帳號，仍要管理員同意）。")
        return self._reply(token, [{**text("\n".join(lines)), "quickReply": {"items": [*taps, *actions]}}])

    def _list_officers(self, token):
        card = officers_card(self.store.officers(), len(self.settings.admins))
        card["quickReply"] = {"items": [fill_action("🙋 新增幹部", "新增幹部 "), fill_action("🙅 移除幹部", "移除幹部 "),
                                        *self._staff_actions()[4:]]}
        return self._reply(token, [card])

    def _add_officer(self, token, user_id, target, title):
        """「新增幹部 王小明 (副隊長)」: by the name they registered, or by their LINE user id."""
        if not target:
            return self._reply(token, [{**text("✏️ 在「新增幹部 」後面輸入對方登記的姓名，職稱可省略。\n"
                                               "例如：新增幹部 王小明 副隊長\n對方要先私訊我「設定 他的姓名」登記。"),
                                        "quickReply": {"items": [fill_action("🙋 新增幹部", "新增幹部 "),
                                                                 *self._staff_actions()[4:]]}}])
        if USER_ID.fullmatch(target):
            found, label = target, (self.store.user(target) or {}).get("staff_name") or short_id(target)
        elif NAME_PATTERN.fullmatch(target):
            people = self.store.users_named(target)
            if not people:
                return self._reply(token, [text(f"找不到登記「{target}」的 LINE 帳號。\n"
                                                f"請他先私訊我「設定 {target}」，再傳一次「新增幹部 {target}」。\n"
                                                "也可以請他傳「我的ID」給你，改用「新增幹部 U…」。")])
            if len(people) > 1:
                ids = "\n".join(f"・{p['user_id']}" + (f"（LINE：{p['name']}）" if p.get("name") else "") for p in people)
                return self._reply(token, [text(f"有 {len(people)} 個 LINE 帳號登記「{target}」，請改用 userId：\n{ids}\n"
                                                "例如「新增幹部 U…」。")])
            found, label = people[0]["user_id"], target
        else:
            return self._reply(token, [text("請輸入對方登記的姓名（2–5 個中文字），或 U 開頭的 LINE userId。")])
        title = title if title and len(title) <= 10 else None
        if not self.store.add_officer(found, title, user_id):
            return self._reply(token, [text(f"{label} 本來就是幹部" + (f"，職稱改為「{title}」。" if title else "。"))])
        print(f"{self._who(user_id)}（{user_id}）新增幹部：{label}（{found}）", flush=True)
        lines = [f"好的，{label} 現在是幹部了" + (f"（{title}）" if title else "") + "。", "請他私訊我「人員管理」就能開始使用。"]
        if self._menu_change(found) == "linked" and self.menu_role(found) == "officer":   # an administrator keeps theirs
            lines.append("他重新打開聊天室後，選單最下面會多一排「管理」。")
        return self._reply(token, [{**text("\n".join(lines)),
                                    "quickReply": {"items": [{"type": "action", "action": {
                                        "type": "message", "label": "幹部名單", "text": "幹部名單"}}]}}])

    def _remove_officer(self, token, user_id, target):
        """「移除幹部 王小明」: by registered name, title or LINE user id."""
        if not target:
            return self._reply(token, [{**text("✏️ 在「移除幹部 」後面輸入姓名或職稱就好。\n例如：移除幹部 學姊"),
                                        "quickReply": {"items": [fill_action("🙅 移除幹部", "移除幹部 "),
                                                                 *self._staff_actions()[4:]]}}])
        officers = self.store.officers()
        matches = [o for o in officers if target in (o["user_id"], o.get("staff_name"), o.get("title"))]
        if not matches:
            return self._reply(token, [text(f"幹部名單裡沒有「{target}」。傳「幹部名單」可以看目前有誰。")])
        if len(matches) > 1:
            return self._reply(token, [text(f"有 {len(matches)} 位幹部符合「{target}」：" +
                                            "、".join(f"{officer_label(o)}（{o['user_id']}）" for o in matches) +
                                            "\n請改用 userId，例如「移除幹部 U…」。")])
        officer = matches[0]
        if len(officers) == 1 and not self.settings.admins:
            return self._reply(token, [text("這是最後一位幹部，移除後就沒有人能管理人員了。請先新增另一位幹部。")])
        self.store.remove_officer(officer["user_id"])
        print(f"{self._who(user_id)}（{user_id}）移除幹部：{officer_label(officer)}（{officer['user_id']}）", flush=True)
        who = "你" if officer["user_id"] == user_id else "他"
        lines = ["好的，你已經不是幹部了。" if officer["user_id"] == user_id else
                 f"好的，{officer_label(officer)} 已經不是幹部了。"]
        menu = self._menu_change(officer["user_id"])   # an administrator keeps the administrator menu
        if self._is_admin(officer["user_id"]):
            lines.append(f"不過{who}是管理員（設定檔），一樣可以管理人員。")
        elif menu == "unlinked":
            lines.append(f"{who}重新打開聊天室後，選單的「管理」那排會收起來。")
        return self._reply(token, [text("\n".join(lines))])

    # -- 圖文選單: the 幹部 and administrator menus ----------------------------------------------------
    MENU_WAIT = 3.0   # seconds an answer waits for the menu change it would mention (``_menu_change``)
    MENU_MISSING = "管理員／幹部選單還沒上傳，請執行 python -m all_staff.cli line-menu"

    def menu_role(self, user_id) -> str | None:
        """Which menu ``user_id`` gets (rich_menu.ROLES): "admin" for an administrator, else "officer" for a 幹部,
        else None, everyone's (the default)."""
        if self._is_admin(user_id):
            return "admin"
        return "officer" if self.store.is_officer(user_id) else None

    def _menu_methods(self):
        """The transport's rich menu methods (``MenuTransport``), or None for a transport without them."""
        methods = tuple(getattr(self.transport, name, None) for name in ("menu_id", "link_menu", "unlink_menu"))
        return methods if all(methods) else None

    def sync_menu(self, user_id, *, force=False) -> str | None:
        """Bring ``user_id``'s rich menu in line with ``menu_role``: link their role's menu, or unlink the one the bot
        linked once they have none, so LINE shows them the default again. What happened: "linked", "unlinked",
        "same" (``Store.menu_link`` already names the current menu: no call, unless ``force``), "missing" (their
        role's menu is not on LINE yet: nothing failed), or None (nothing to do, or it could not be done). Never
        raises: failures are logged. Someone with neither a role nor a recorded link needs nothing and does not wait
        for the lock, which a slow LINE can hold for a while.

        The menus are uploaded by ``line-menu``; until they are on LINE everybody keeps the default (said once in the
        log). Uploading them again gives them new ids, which ``menu_id`` finds, so the next sync links those."""
        methods = self._menu_methods()
        if not methods:
            return None
        menu_id, link, unlink = methods
        try:
            if not self.menu_role(user_id) and not self.store.menu_link(user_id):
                return None
            with self._menu_lock:
                role, record = self.menu_role(user_id), self.store.menu_link(user_id)
                current = menu_id(MENU_NAMES[role]) if role else None
                if role and not current and not self._menus_warned:
                    self._menus_warned = True
                    print(f"⚠ {self.MENU_MISSING}", flush=True)
                if not current:   # no role, or their menu is not on LINE: the default
                    if not record:
                        return "missing" if role else None
                    unlink(user_id)
                    self.store.drop_menu_link(user_id)
                    return "unlinked"
                if not force and record and (record["role"], record["menu_id"]) == (role, current):
                    return "same"
                self.store.set_menu_link(user_id, role, link(user_id, MENU_NAMES[role]))
                return "linked"
        except Exception as exc:
            print(f"圖文選單切換失敗（{user_id}）：{type(exc).__name__}: {exc}", flush=True)
            return None

    def sync_menus(self, *, force=False) -> dict[str, int]:
        """``sync_menu`` for every administrator, 幹部 and user the bot linked a menu to (who may be neither any more,
        and goes back to the default): at start-up, and after ``line-menu`` uploaded the menus again. How many now
        have the administrator menu, the 幹部 menu, went back to the default, or could not be changed. Never raises."""
        summary = {"admin": 0, "officer": 0, "default": 0, "failed": 0}
        if not self._menu_methods():
            return summary
        try:
            users = dict.fromkeys([*self.settings.admins, *(officer["user_id"] for officer in self.store.officers()),
                                   *self.store.menu_linked_users()])
            for user_id in users:
                role = self.menu_role(user_id)
                outcome = self.sync_menu(user_id, force=force)
                if outcome in ("linked", "same") and role:
                    summary[role] += 1
                elif outcome == "unlinked":
                    summary["default"] += 1
                elif outcome != "missing" and (role or self.store.menu_link(user_id)):   # should have changed, did not
                    summary["failed"] += 1
        except Exception as exc:
            print(f"圖文選單同步失敗：{type(exc).__name__}: {exc}", flush=True)
        return summary

    def _linked_menu(self, user_id) -> bool:
        """Does ``user_id`` have their role's menu, as far as the bot knows: it linked one (``Store.menu_link``) for the
        role they still have, and that menu is still on LINE (``line-menu`` deletes the copies it replaces, and with
        them every link to them, also of someone it could not move to the new copy)."""
        record, methods = self.store.menu_link(user_id), self._menu_methods()
        if not record or not methods or record["role"] != self.menu_role(user_id):
            return False
        try:
            return methods[0](MENU_NAMES[record["role"]]) == record["menu_id"]
        except Exception as exc:
            print(f"圖文選單查詢失敗（{user_id}）：{type(exc).__name__}: {exc}", flush=True)
            return False

    def _menu_change(self, user_id) -> str | None:
        """``sync_menu`` for an answer that says what changed: it runs in a thread and is waited for ``MENU_WAIT``
        seconds at most, so a slow LINE cannot hold the reply; a change that takes longer finishes by itself and is
        not mentioned (None)."""
        outcome = []
        worker = threading.Thread(target=lambda: outcome.append(self.sync_menu(user_id)), name="menu", daemon=True)
        worker.start()
        worker.join(self.MENU_WAIT)
        return outcome[0] if outcome else None

    # -- 本月統計 -------------------------------------------------------------------
    STATS_BUDGET = 20.0   # seconds after which one reply starts no more re-reading of an old roster: a reply
    #                       token lasts a minute and one roster can take half of that

    def _stale(self, result) -> bool:
        """Would using this saved roster make the analyzer read its photo again (slow)?"""
        return bool((getattr(self.analyzer, "ensure_duties", None) and result.get("duty_version") != DUTY_VERSION)
                    or (getattr(self.analyzer, "ensure_names", None) and result.get("names_version") != NAMES_VERSION))

    def _month(self, user_id, staff_name, year, month, days, *, reread=True):
        """What each day's roster shows for ``staff_name``: the newest photo of the day, as in every other query.

        Rosters saved by an older reader are read again on the way, one by one and slowly. Only what fits in
        this reply is done (``reread`` False: none at all); the rest are reported as not counted yet (what was
        read stays saved, so asking again goes further).
        """
        kinds = monthly_stats
        deadline, busy, found = time.monotonic() + self.STATS_BUDGET, None, []
        try:
            for day in days:
                roster = self.store.roster_on(user_id, day)
                if not roster:
                    found.append(kinds.Day(day, kinds.MISSING)); continue
                try:
                    result = self.analyzer.result(roster["run_id"])
                    if self._stale(result):
                        if not reread or time.monotonic() > deadline:
                            found.append(kinds.Day(day, kinds.PENDING)); continue
                        busy = busy or self._keep_loading(user_id)
                    result = self._result(roster["run_id"])
                    names = self.names_for(roster, result)
                except Exception as exc:
                    print(f"統計時讀不到班表（{roster['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
                    found.append(kinds.Day(day, kinds.UNREADABLE)); continue
                code = next((c for c, n in names.items() if n == staff_name), None)
                if not code:
                    found.append(kinds.Day(day, kinds.UNKNOWN)); continue
                # an old grid that the start-up re-reading could not replace: the last four columns are missing
                partial = bool(getattr(self.analyzer, "ensure_grid", None)) and result.get("grid_version") != GRID_VERSION
                person = next((p for p in result.get("people", []) if p["code"] == code), None)
                if not person or not person.get("slots"):
                    found.append(kinds.Day(day, kinds.OFF, note=absence_kind(code, result), code=code,
                                           partial=partial)); continue
                hours, places, unsure = kinds.person_day(person, place=work_name, threshold=self.settings.threshold)
                found.append(kinds.Day(day, kinds.DUTY, hours, places, unsure, code=code, partial=partial))
        finally:
            if busy:
                busy.set()
        return kinds.Month(year, month, found)

    def _show_stats(self, token, user_id, year, month):
        """「本月統計」: the registered user's planned hours and places this month, up to today, as charts.

        Only this month can be seen: asking for another one (「上月統計」, 「9月統計」, an old card's button)
        says so and shows this month.
        """
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        current = today()
        days = monthly_stats.days_of(current.year, current.month, current)
        card = stats_card(user["staff_name"], self._month(user_id, user["staff_name"], current.year, current.month,
                                                          days))
        card["quickReply"] = {"items": [
            {"type": "action", "action": {"type": "message", "label": label, "text": label}}
            for label in ("我的班表", "查看工作")] + [
            {"type": "action", "action": {"type": "message", "label": "💰 加班結算", "text": "加班結算"}}]}
        other = (year, month) != (current.year, current.month)
        return self._reply(token, ([text(f"統計只看得到這個月的喔！以下是 {current.month} 月 🐰")] if other else [])
                           + [card])

    # -- 班表月曆 -------------------------------------------------------------------
    def _show_calendar(self, token, user_id, year, month):
        """「◀ 9 月的班表」: last month's 班表月曆 on a card of its own, each day as the statistics would read it.

        This month's calendar is the one in 「本月統計」, which a button for this month shows. Older months and those
        before ``CALENDAR_START`` are not offered: an old button for one says so.
        """
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        current = today()
        if (year, month) == (current.year, current.month):
            return self._show_stats(token, user_id, year, month)
        back = {"quickReply": {"items": [{"type": "action", "action": message_action("📊 本月統計", "本月統計")}]}}
        if (year, month) != earlier_month(current.year, current.month):
            start = CALENDAR_START
            words = (f"班表月曆從 {start.year} 年 {start.month} 月開始，更早的月份沒有月曆喔！"
                     if (year, month) < (start.year, start.month) else "班表月曆只看得到這個月和上個月喔！")
            return self._reply(token, [{**text(words + "\n其他日期的班表可以直接輸入日期查，例如 9/30。"), **back}])
        name = user["staff_name"]
        days = monthly_stats.days_of(year, month, current)   # all of it: the month is over
        card = calendar_card(name, self._month(user_id, name, year, month, days))
        card["quickReply"] = {"items": [{"type": "action", "action": message_action(label, value)}
                                        for label, value in (("📊 本月統計", "本月統計"), ("📅 我的班表", "我的班表"))]}
        return self._reply(token, [card])

    def _show_calendar_day(self, token, user_id, day):
        """A date tapped on 「班表月曆」: that day's whole roster photo, then one's own duty card on that roster with
        the blue-outlined photo — or, with the name not on its name table, the dated 「我是 代號」 hint — and then
        that month's calendar again (``_calendar_after``), so the next date is a tap away: no other dates under the
        card, the calendar is for that.

        The roster is that day's newest the user may see (``Store.roster_on``), the one the statistics count. A date
        still to come or before ``CALENDAR_START`` (an old card, a forged postback) is answered in words.
        """
        started = time.monotonic()
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        if day > today():
            return self._reply(token, [text(f"{day.month}/{day.day} 還沒到喔！明天的班表點「我的班表」就看得到 📅")])
        if day < CALENDAR_START:
            start = CALENDAR_START
            return self._reply(token, [text(f"班表月曆從 {start.month}/{start.day} 開始喔！更早的班表可以直接輸入日期查，"
                                            f"例如 {day.month}/{day.day}。")])
        roster = self.store.roster_on(user_id, day)
        if not roster:
            return self._missing_roster(token, user_id, day)
        try:
            self._result(roster["run_id"])
        except Exception as exc:   # an old card: the month drew this day grey since
            print(f"班表月曆讀不到 {day.isoformat()} 的班表（{roster['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
            image = self._roster_image(roster["run_id"])
            return self._reply(token, [*([image] if image else []),
                                       text(f"{day_label(day)}的班表資料讀不出來，請對照原圖，或請管理員重傳那張班表。"),
                                       *self._calendar_after(user_id, user["staff_name"], day, started)])
        return self._show(token, user_id, staff_name=user["staff_name"], mine=True, roster=roster, tapped=day,
                          started=started)

    CALENDAR_MARGIN = 10.0   # seconds of the reply window the calendar after a day's roster needs left: a month of
    #                          saved rosters takes a moment to look through, and past the window the reply is a push

    def _calendar_after(self, user_id, staff_name, day, started) -> list[dict]:
        """The calendar to send after a date tapped on 「班表月曆」 (``day``): that month's again (``calendar_card``),
        this month's up to today or the whole of last month, headed with the date just seen.

        It starts no re-reading of an old roster (such a day is ⏳, as when 「本月統計」 runs out of time, which goes
        on with it) and is left out once the reply window since ``started`` is nearly spent: the day's roster has to
        go out as a reply, which is free, not as a push, which is not. Nor is there one for an older month (an old
        card): 班表月曆 only offers this month and last.
        """
        current = today()
        this = (day.year, day.month) == (current.year, current.month)
        if not (this or (day.year, day.month) == earlier_month(current.year, current.month)) or \
                time.monotonic() - started > self.settings.reply_window - self.CALENDAR_MARGIN:
            return []
        month = self._month(user_id, staff_name, day.year, day.month,
                            monthly_stats.days_of(day.year, day.month, current), reread=False)
        return [calendar_card(staff_name, month, today=current, seen=day,
                              earlier=earlier_month(day.year, day.month) if this else None)]

    # -- 加班結算 -------------------------------------------------------------------
    def _show_overtime(self, token, user_id, *, before=(), other=False):
        """「加班」「月底結算」: this month day by day, the hours past eight and what they pay at the user's wage.

        Like 「本月統計」 it is this month up to today (``other``: another month was asked for), so on the
        month's last day it is the whole month. ``before`` goes ahead of the card (「時薪 200」's answer).
        """
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        current = today()
        days = monthly_stats.days_of(current.year, current.month, current)
        wage = self.store.hourly_wage(user_id)
        card = overtime_card(user["staff_name"], self._month(user_id, user["staff_name"], current.year,
                                                             current.month, days), wage)
        card["quickReply"] = {"items": [fill_action("✏️ 改時薪" if wage else "✏️ 設定時薪", "時薪 ")] + [
            {"type": "action", "action": {"type": "message", "label": label, "text": value}}
            for label, value in (("📊 本月統計", "本月統計"), ("📅 我的班表", "我的班表"))]}
        note = [text(f"結算只看得到這個月的喔！以下是 {current.month} 月 💰")] if other else []
        return self._reply(token, [*before, *note, card])

    def _set_wage(self, token, user_id, value):
        """「時薪 200」 keeps the user's hourly wage and answers with the settlement it gives; 「時薪」 alone (or
        「時薪多少」: no number at all) says what it is and how to set it. A number that is not above zero or is
        above ``WAGE_LIMIT`` is refused with how to type it, and the wage kept so far stays."""
        user = self.store.user(user_id) if user_id else None
        if not user or not user.get("staff_name"):
            return self._reply(token, [self._need_name()])
        again = {"quickReply": {"items": [fill_action("✏️ 輸入時薪", "時薪 ")]}}
        if value not in WAGE_CLEAR and not re.search(r"\d", normalise(value)):
            wage = self.store.hourly_wage(user_id)
            message = (f"💰 你的時薪：{wage_text(wage)}（只用來試算加班費）\n要改就輸入新的數字，例如「時薪 220」；"
                       "不想留可以傳「時薪 取消」。") if wage else "💰 還沒設定時薪。\n" + WAGE_HOW
            return self._reply(token, [{**text(message), "quickReply": {"items": [
                fill_action("✏️ 改時薪" if wage else "✏️ 設定時薪", "時薪 "),
                {"type": "action", "action": {"type": "message", "label": "💰 加班結算", "text": "加班結算"}}]}}])
        if value in WAGE_CLEAR:
            self.store.set_hourly_wage(user_id, None)
            return self._reply(token, [{**text("好的，時薪已經刪掉了。加班時數照樣查得到，加班費要再設定時薪才會算。"),
                                        **again}])
        wage = parse_wage(value)
        if wage is None:
            return self._reply(token, [{**text("時薪請只打數字喔！\n" + WAGE_HOW), **again}])
        if round(wage, 2) <= 0:
            return self._reply(token, [{**text("時薪要大於 0 喔！\n" + WAGE_HOW), **again}])
        if wage > WAGE_LIMIT:
            return self._reply(token, [{**text(f"時薪 {wage_text(wage)} 好像太多了 😮 是不是多打了一個 0？\n"
                                               f"時薪最多 {WAGE_LIMIT:,} 元，例如「時薪 200」。"), **again}])
        wage = round(wage, 2)
        self.store.set_hourly_wage(user_id, wage)
        return self._show_overtime(token, user_id, before=[text(f"✅ 時薪記好了：{wage_text(wage)}，加班費算好了 👇")])

    # -- duty messages -----------------------------------------------------------------
    def _duties(self, code, name, roster, result, *, viewer=None, mine=False, news=None, change="", others=(),
                show_original=False) -> list[dict]:
        """The duty card (``duty_card``), followed by the blue-outlined roster photo when available. ``viewer``: whom
        it goes to; an administrator's card can change the photo's date."""
        image = self._person_image(roster["run_id"], code)
        card = duty_card(code, name, roster, result, threshold=self.settings.threshold, now=taipei_now(), mine=mine,
                         news=news, change=change, others=others, redate=self._is_admin(viewer), photo=bool(image),
                         show_original=show_original)
        return [card] + ([image] if image else [])

    def _person_image(self, run_id, code) -> dict | None:
        render = getattr(self.analyzer, "person_image", None)
        if not (self.settings.images and self.settings.public_base and render):
            return None
        try:
            paths = render(run_id, code)
        except Exception as exc:  # the text reply matters more than the photo
            print(f"班表標示圖失敗（{run_id} / {code}）：{type(exc).__name__}: {exc}", flush=True)
            return None
        if not paths:
            return None
        base = f"{self.settings.public_base}/runs/"
        return {"type": "image", "originalContentUrl": base + quote(paths[0]),
                "previewImageUrl": base + quote(paths[1])}

    def _roster_image(self, run_id) -> dict | None:
        render = getattr(self.analyzer, "roster_image", None)
        if not (self.settings.images and self.settings.public_base and render):
            return None
        try:
            paths = render(run_id)
        except Exception as exc:
            print(f"整張班表照片失敗（{run_id}）：{type(exc).__name__}: {exc}", flush=True)
            return None
        if not paths:
            return None
        base = f"{self.settings.public_base}/runs/"
        return {"type": "image", "originalContentUrl": base + quote(paths[0]),
                "previewImageUrl": base + quote(paths[1])}

    def _staff_table_image(self, run_id) -> dict | None:
        """The crop of the name table that 「勤務人員」 sends, as an image message; None without one."""
        render = getattr(self.analyzer, "staff_table_image", None)
        if not (self.settings.images and self.settings.public_base and render):
            return None
        try:
            paths = render(run_id)
        except Exception as exc:
            print(f"勤務人員對照圖失敗（{run_id}）：{type(exc).__name__}: {exc}", flush=True)
            return None
        if not paths:
            return None
        base = f"{self.settings.public_base}/runs/"
        return {"type": "image", "originalContentUrl": base + quote(paths[0]),
                "previewImageUrl": base + quote(paths[1])}

    # -- roster photo ------------------------------------------------------------
    @staticmethod
    def _sent_at(event) -> float:
        """When LINE says the photo was sent (``timestamp`` is in milliseconds); now when it is missing or absurd.

        This, not the moment recognition finishes, decides which of two photos of one day is the newer.
        A redelivered webhook keeps its original timestamp, so it cannot jump the queue either.
        """
        now = time.time()
        try:
            stamp = float(event.get("timestamp")) / 1000
        except (TypeError, ValueError):
            return now
        return stamp if 0 < stamp <= now + 300 else now   # a time in the future must not win every comparison

    def _log_unnamed(self, roster, result, names) -> None:
        """For whoever runs the server: duties on a code the name table could not put a name to, and apart from
        them those on a code nobody has that day, with why (``unnamed_codes``)."""
        run_id = roster["run_id"]
        unread, nobody = unnamed_codes(result, names, later=self._set_after(roster))
        if unread:
            print(f"班表 {run_id}：{len(unread)} 個有勤務的代號沒有對到姓名：{'、'.join(unread)}", flush=True)
        if nobody:
            print(f"班表 {run_id}：{len(nobody)} 個有勤務的代號當天沒有這個人（勤務格的代號可能讀錯）："
                  f"{nobody_words(nobody)}", flush=True)

    def _earlier_version(self, replaced: dict | None) -> tuple[dict, dict] | None:
        """(code → name, result) of the photo an update replaces; None without one or when it cannot be read."""
        if not replaced:
            return None
        try:
            result = self._result(replaced["run_id"])
            return self.names_for(replaced, result), result
        except Exception as exc:   # the comparison is a courtesy: never lose the notification over it
            print(f"讀不到被取代的班表（{replaced['run_id']}）：{type(exc).__name__}: {exc}", flush=True)
            return None

    def _change_note(self, name, code, earlier, result) -> str:
        """How one subscriber's day differs from the version that was just replaced (empty if unknown)."""
        if earlier is None:
            return ""
        names, before = earlier
        old_code = next((c for c, n in names.items() if n == name), None)
        return duty_change(duty_signature(before, old_code), duty_signature(result, code))

    def _report_upload(self, status, chat_id, user_id, sent_at, **details):
        """Tell every administrator how a photo posted in a group went (``upload_report``): one push each.

        A recognised roster's report is judged against the fixed codes in force for its day (the mapping
        ``names_for`` applies) and carries the 「勤務人員」 crop of the name table under the card, in the same push:
        a push request counts once per recipient however many messages it holds. Without that crop (images off, or
        it could not be drawn) the card goes alone. Without pushes the two are one group of each administrator's
        收件匣 (``_push_quietly``): they come together or not at all.
        """
        if not self.settings.admins:
            return
        roster, image = details.get("roster"), None
        if status == "ok" and roster:
            book, day = self._book(), roster_day(roster)
            details.update(fixed=set(book.fixed_on(day)), later=book.set_after(day))
            image = self._staff_table_image(roster["run_id"])
        message = upload_report(status, self._uploader(user_id, chat_id), sent_at,
                                threshold=self.settings.threshold, has_image=bool(image), **details)
        for admin in self.settings.admins:
            self._push_quietly(admin, [message, image] if image else [message])

    def _uploader(self, user_id, chat_id) -> str:
        """Who posted a photo, for the administrators: the registered name, else their LINE name in that group."""
        if not user_id:
            return "不明的上傳者"
        name = (self.store.user(user_id) or {}).get("staff_name")
        if not name:
            try:
                name = self.transport.display_name(user_id, chat_id)
            except Exception as exc:   # a report without the name beats no report
                print(f"讀不到 {user_id} 的 LINE 名稱：{exc}", flush=True)
        return name or short_id(user_id)

    def _ask_admins(self, roster) -> int:
        """Privately ask every administrator which day a held photo is; how many of them were reached. Only with
        pushes: without them no photo is held (``_handle_image``)."""
        image = self._roster_image(roster["run_id"])   # the photo itself: the date is printed at its top
        messages = ([image] if image else []) + [date_request_card(roster, len(self._audience()))]
        return sum(1 for admin in self.settings.admins if self._push_quietly(admin, messages))

    def _announce(self, roster, result):
        """Privately tell every subscriber their duties on a group photo: a new roster, or a day's update.

        While automatic notifications are off (``_notifying``: ``Settings.notify``, never without pushes) nobody is
        told; the rest still happens. Only
        those the 名冊守門員 lets in are told (``_audience``), the old code subscribers included: the push carries
        the whole photo.
        """
        run_id = roster["run_id"]
        if self.store.same_day(roster, later=True):
            # Recognition of a photo of the same day that was sent after this one has already finished:
            # that one is the day's roster. Keep this photo on file, but tell nobody about it.
            print(f"略過較舊的班表（{run_id}）：同一天已有更晚傳送的版本", flush=True)
            return
        # A photo of the same printed day sent earlier means this one is the updated version.
        replaced = self.store.same_day(roster, later=False)
        label = "🔄 班表更新" if replaced else "📌 新班表"
        names = self.names_for(roster, result)
        self._log_unnamed(roster, result, names)
        audience = self._audience()
        roster_names = self._roster_names()
        legacy = [member for member in self.store.legacy_subscribers()
                  if self._admitted(member["user_id"], roster_names)] if self._notifying else []
        if not audience and not legacy:   # nobody to tell: no need to read the earlier version or draw the photo
            return
        earlier = self._earlier_version(replaced)
        roster_image = self._roster_image(run_id)
        for member in audience:
            if self.store.same_day(roster, later=True):
                # A newer photo of this day finished recognition while we were notifying: stop, so the
                # last thing anyone is told is the newest roster (that photo notifies everybody itself).
                print(f"通知到一半停止（{run_id}）：同一天已有更晚傳送的版本", flush=True)
                return
            name = member["staff_name"]
            code = next((c for c, n in names.items() if n == name), None)
            change = self._change_note(name, code, earlier, result)
            first = [roster_image] if roster_image else []
            if code:
                self._push_quietly(member["user_id"], first + self._duties(
                    code, name, roster, result, viewer=member["user_id"], mine=True, news=label, change=change))
            else:
                self._push_quietly(member["user_id"], first + [text(
                    f"{label}\n" + (f"⚠ {change}\n" if change else "") + self._not_found(name, roster))])
        for member in legacy:   # once: codes rotate, so ask for the name
            self._push_quietly(member["user_id"], ([roster_image] if roster_image else []) + [text(
                "📌 群組有新班表。\n班表機器人已更新：請改用姓名登記，"
                f"例如「設定 {EXAMPLE_NAME}」，之後新班表會自動找出你當天的代號。")])
            self.store.drop_code(member["user_id"])

    def _handle_image(self, event, chat_id, user_id, is_group):
        """Group photos notify subscribers privately; private photos receive a reply.

        Only private uploads fall back to a push when their reply token expires; without pushes they get an interim
        reply instead, and the result waits in the 收件匣 (``_interim``). Group uploads never call the Reply API or push
        to the source group. With administrators and pushes, a group photo whose date could not be read notifies
        nobody until one of them has said which day it is (``_ask_admins`` → the buttons of ``date_request_card`` →
        ``_confirm_date``); without pushes nobody could be asked in time, so it is published at once as 日期未辨識 and
        the report in their 收件匣 has the date picker (``upload_report``).
        """
        started = time.monotonic()
        busy = self._keep_loading(chat_id) if not is_group else None
        # the time the event waited for a worker counts against its reply token too (``_waited``)
        token = self._interim(event.get("replyToken"), busy, self._waited(event)) if not is_group else lambda: None
        try:
            return self._read_photo(event, chat_id, user_id, is_group, started, busy, token)
        finally:
            token()   # answered, or failed on the way: no interim reply can go out any more

    def _waited(self, event) -> float:
        """How long ago LINE sent ``event`` (its ``timestamp``, milliseconds), within the reply window: time spent
        waiting for a worker counts against the reply token too. 0 without a usable timestamp."""
        try:
            waited = time.time() - float(event.get("timestamp")) / 1000
        except (TypeError, ValueError):
            return 0.0
        return min(max(0.0, waited), self.settings.reply_window)

    def _interim(self, token, busy, waited=0.0):
        """What a private photo's answer calls to get the reply token (``_handle_image``).

        With pushes that is just the token: past the reply window ``_answer`` pushes the result. Without them a photo
        still being read ``INTERIM_LEAD`` seconds before the window closes gets a free interim reply instead
        (``STILL_READING``; the typing indicator stops), and its result, getting None, waits in the 收件匣 for the
        person's next message (``_answer``). The token is used once whatever the timing: the first to take the lock
        has it. The interim reply runs in a thread of its own, so it carries nothing else (``_inbox``). ``waited``: how
        long the event waited before this (``_waited``), off the window."""
        if self.settings.push or not token:
            return lambda: token
        claim, window = threading.Lock(), self.settings.reply_window

        def interim():
            if not claim.acquire(blocking=False):
                return
            if busy:
                busy.set()
            print("私訊的照片還在辨識：先回覆請對方稍等，結果之後放進收件匣", flush=True)
            self._reply_quietly(token, [text(STILL_READING)])

        remaining = window - waited
        timer = threading.Timer(remaining - INTERIM_LEAD if remaining > 2 * INTERIM_LEAD else remaining / 2, interim)
        timer.daemon = True
        timer.start()

        def take():
            timer.cancel()
            return token if claim.acquire(blocking=False) else None
        return take

    def _read_photo(self, event, chat_id, user_id, is_group, started, busy, token):
        """``_handle_image`` once the typing indicator is on: ``token()`` gives the reply token, or None once an
        interim reply has used it."""
        sent_at = self._sent_at(event)   # taken now: recognition takes a while and may finish out of order
        try:
            payload = self.transport.content(event["message"]["id"])
            with self.analysis_lock:
                result = self.analyzer.analyze(payload, f"line-{event['message']['id']}.jpg")
        except Exception as exc:  # DemoError carries a user-facing reason
            kind = exc.__class__.__name__
            reason = str(exc) if kind in ("DemoError", "NotARoster") else "處理時發生錯誤，請稍後再試"
            if busy:
                busy.set()
            if is_group:
                # A group has all sorts of photos. The uploader hears about a roster that could not be read, and
                # about any photo from someone who posts rosters (it may be their bad shot); a push costs quota,
                # so others' holiday photos pass in silence. The administrators hear about every one of them.
                told = False
                if user_id and not self._is_admin(user_id) and (kind != "NotARoster"
                                                                 or self.store.has_uploaded(user_id)):
                    when = datetime.fromtimestamp(sent_at, TAIPEI)   # it may be read days later, from the 收件匣
                    told = self._push_quietly(user_id, [text(
                        f"⚠ 你在群組 {when.month}/{when.day} {when:%H:%M} 傳的班表無法辨識：{reason}\n"
                        "請確認拍到完整的主表格後重傳。這張沒有更新班表。")])
                    if told and not self.settings.push:   # filed: they see it with their next reply
                        told = "已放進他的收件匣"
                print(f"群組圖片辨識失敗（{user_id}）：{kind}: {exc}", flush=True)
                detail = reason if kind in ("DemoError", "NotARoster") else f"處理時發生錯誤（{kind}: {exc}）"[:300]
                self._report_upload("skipped" if kind == "NotARoster" else "failed", chat_id, user_id, sent_at,
                                    reason=detail, told=told)
                return
            return self._answer(token(), chat_id, started,
                                [text(f"⚠ 這張照片無法辨識：{reason}\n請確認拍到完整的主表格後重傳。")])
        if busy:
            busy.set()
        run = result["run"]
        undated = roster_day({"roster_date": run.get("roster_date")}) is None
        # Ask the administrators before anyone hears; without pushes they could not be asked in time (the next time
        # they write to the bot): the photo is published at once, and their report has the date picker
        hold = is_group and undated and bool(self.settings.admins) and self.settings.push
        self.store.add_roster(chat_id, run["id"], user_id, run["codes"], run.get("roster_date"), sent_at, held=hold)
        # This very upload (as stored), even if another upload finishes concurrently.
        roster = self.store.roster_by_run(run["id"])
        if is_group:
            if hold:
                if self._ask_admins(roster):
                    print(f"班表 {run['id']} 的日期讀不到：已私訊管理員，確認日期前不通知", flush=True)
                    return
                # Nobody could be asked (a wrong id, not a friend, blocked, the monthly push quota used up):
                # do not sit on the roster forever.
                self.store.unhold(run["id"])
                roster = self.store.roster_by_run(run["id"])
                print(f"⚠ 班表 {run['id']} 的日期讀不到，而且沒有一位管理員收得到私訊（LINE_V2_ADMIN_USER_IDS "
                      "是否正確？管理員是否加了好友？本月推播額度是否用完？）。改為直接通知，日期未辨識。", flush=True)
            if undated and self.settings.admins and not self.settings.push:
                print(f"班表 {run['id']} 的日期讀不到：不推播，所以不等管理員，直接發布（日期未辨識）；"
                      "管理員可在辨識回報上點「📅 日期不對？」補上日期", flush=True)
            # A held photo's administrators already have its date question, with the photo; the others hear now
            self._report_upload("ok", chat_id, user_id, sent_at, roster=roster, result=result,
                                names=self.names_for(roster, result),
                                replaced=self.store.same_day(roster, later=False) is not None,
                                superseded=self.store.same_day(roster, later=True) is not None,
                                link=bool(self.settings.public_base and self.settings.images))
            self._announce(roster, result)
            if undated and user_id and not self.settings.admins and self._admitted(user_id):
                # with administrators the date is theirs to set; someone the 名冊守門員 keeps out could not use the picker
                self._push_quietly(user_id, [undated_notice(roster, inline=not self.settings.push)])
            return
        names = self.names_for(roster, result)
        self._log_unnamed(roster, result, names)
        summary = text(roster_summary(result, roster))
        if self.settings.public_base and self.settings.images:
            summary["text"] += "\n可在班表卡片點「查看整張班表」，直接取得原始照片。"
        messages = [summary]
        user = self.store.user(user_id) if user_id else None
        if user and user.get("staff_name"):
            code = next((c for c, n in names.items() if n == user["staff_name"]), None)
            if code:
                messages += self._duties(code, user["staff_name"], roster, result, viewer=user_id, mine=True,
                                         show_original=bool(self.settings.public_base and self.settings.images))
            else:
                messages.append(text(self._not_found(user["staff_name"], roster)))
        else:
            summary["text"] += "\n\n輸入「設定 你的姓名」，之後點「我的班表」查自己的勤務。"
        summary["text"] += "\n\n私訊的照片只有你自己查得到，不會通知其他人，也不會改變群組的班表。"
        self._answer(token(), chat_id, started, messages)

    def _answer(self, token, chat_id, started, messages):
        """Reply while the token is fresh (free); otherwise push (counts against the quota). Like ``_reply``, the reply
        carries what waits for the person (``_inbox``); the push never does, and what the failed reply would have shown
        waits for the next one.

        Without pushes (``Settings.push``) the reply is tried however late (the token may still be good), and with no
        token, or the reply refused, the messages wait in the 收件匣 for the next reply instead (``_file``)."""
        fresh = time.monotonic() - started < self.settings.reply_window
        if token and (fresh or not self.settings.push):
            first, shown = self._inbox(messages)
            try:
                self.transport.reply(token, (first + list(messages))[:REPLY_LIMIT])
            except Exception as exc:
                refused = bool(first) and refused_reply(exc)   # for what it carried: try once without that
                self._undelivered(shown, refused=refused)
                try:
                    if not refused:
                        raise exc
                    self.transport.reply(token, list(messages)[:REPLY_LIMIT])
                    return None
                except Exception as again:
                    print(f"reply 失敗，{'改用 push' if self.settings.push else '放進收件匣'}：{again}", flush=True)
            else:
                return self._delivered(shown)
        if self.settings.push:
            self.transport.push(chat_id, messages)
        else:
            self._file(chat_id, messages)

    def _loading(self, chat_id, seconds=60) -> bool:
        """LINE's typing indicator (1:1 chats only); best effort."""
        loading = getattr(self.transport, "loading", None)
        if not loading:
            return False
        try:
            loading(chat_id, seconds)
        except Exception as exc:
            print(f"loading 動畫失敗：{exc}", flush=True)
        return True

    def _keep_loading(self, chat_id) -> threading.Event | None:
        """Start the typing indicator and re-send it every few seconds until the returned event is set.

        LINE only shows the indicator if the user is looking at the bot's chat when the request
        arrives. A photo forwarded from another group is sent while the user is still in that
        group, so a single call at the start is lost; re-sending makes it appear as soon as they
        switch to the bot's chat. Sending the reply also clears it on LINE's side.
        """
        if not self._loading(chat_id):
            return None
        done = threading.Event()

        def refresh():
            deadline = time.monotonic() + self.settings.reply_window + 10
            while not done.wait(self.settings.loading_refresh) and time.monotonic() < deadline:
                self._loading(chat_id, 10)  # short, so a late re-send cannot outlive the reply by much

        threading.Thread(target=refresh, daemon=True).start()
        return done

    def _push_quietly(self, to, messages) -> bool:
        """Keep one failed private delivery from preventing deliveries to other users; says whether it went out.

        Every push of the bot goes through here (and through ``_answer``). Without pushes (``Settings.push``) nothing
        is pushed: the messages are filed for ``to`` instead (``_file``), and True says they will reach them, free."""
        if not self.settings.push:
            return self._file(to, messages)
        try:
            self.transport.push(to, messages)
            return True
        except Exception as exc:
            print(f"私訊 {to} 失敗：{exc}", flush=True)
            return False

    def _file(self, to, messages) -> bool:
        """Without pushes: keep ``messages`` in ``to``'s 收件匣 as one group, which the next reply they get carries in
        front of its own (``_inbox``) or 「收件匣」 shows; whether they were filed. A reply never splits a group (a
        report comes with its picture); one longer than a reply is filed in fives, as a push request would have sent
        it. Only a person (a LINE user id, ``U…``) has a 收件匣. Past ``MAILBOX_KEEP`` waiting groups the oldest go."""
        if not messages:
            return False
        if not str(to or "").startswith("U"):
            print(f"不推播：{to} 不是使用者，沒有收件匣，{len(messages)} 則訊息沒有送出", flush=True)
            return False
        numbers, dropped = [], 0
        for start in range(0, len(messages), REPLY_LIMIT):
            number, gone = self.store.file_mail(to, list(messages[start:start + REPLY_LIMIT]), MAILBOX_KEEP)
            numbers.append(f"#{number}")
            dropped += gone
        print(f"不推播：{len(messages)} 則訊息放進 {to} 的收件匣（{'、'.join(numbers)}），他下次收到回覆時一起送出",
              flush=True)
        if dropped:
            print(f"⚠ {to} 的收件匣等著的超過 {MAILBOX_KEEP} 則，刪掉了最舊的 {dropped} 則", flush=True)
        return True

    def _reply_quietly(self, token, messages):
        """A reply that must not stop what follows it (an expired token, an error from LINE)."""
        try:
            self._reply(token, messages)
        except Exception as exc:
            print(f"回覆失敗：{exc}", flush=True)

    def _reply(self, token, messages):
        """Reply with ``messages``, what waits for the person in front of them (``_inbox``). When LINE turns the reply
        down for what it carried (``refused_reply``), it is sent again without that: one bad filed message must not
        hold up every reply to them."""
        if token and messages:
            first, shown = self._inbox(messages)
            try:
                self.transport.reply(token, (first + list(messages))[:REPLY_LIMIT])
            except Exception as exc:
                refused = bool(first) and refused_reply(exc)
                self._undelivered(shown, refused=refused)
                if not refused:
                    raise
                print(f"LINE 拒收這次回覆帶上的訊息，只回覆本身：{exc}", flush=True)
                self.transport.reply(token, list(messages)[:REPLY_LIMIT])
                return False
            self._delivered(shown)   # only now: a reply that failed has shown nothing
        return False

    def _undelivered(self, shown, *, refused: bool = False) -> None:
        """A reply that carried what ``_inbox`` took did not go out: its 收件匣 groups go back in the queue (the digests
        were never marked). ``refused``: LINE turned it down for what it held (``Store.release_mail``)."""
        if shown.get("mail"):
            dropped = self.store.release_mail(shown["mail"], refused=refused)
            if dropped:
                print(f"LINE 一再拒收收件匣裡的訊息 #{'、#'.join(map(str, dropped))}，已略過", flush=True)

    def _delivered(self, shown) -> None:
        """Mark what a reply that went out showed (``_inbox``): it is not shown again."""
        if shown.get("feedback"):
            self.store.mark_feedback(shown["feedback"])
        if shown.get("notices"):
            self.store.mark_flow_notices(shown["notices"])
        if shown.get("mail"):
            self.store.mark_mail(shown["mail"])
