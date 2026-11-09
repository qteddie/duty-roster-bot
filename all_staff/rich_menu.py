"""LINE rich menus for the V2 roster bot: the tappable menu at the bottom of 1:1 chats.

Rich menus only appear in 1:1 chats with the official account, never in groups, so the
group stays quiet. Message buttons send their text as if the user typed it; 「查詢人員」
and 「意見回饋」 open the keyboard with 「查 」／「意見 」 pre-filled instead (a postback
the bot ignores), because those commands need more text from the user.

The first row is one wide button, 「我的班表」 (now, the next hour, today and tomorrow on one
card): it replaced 今天班表／明天班表／下個時段, whose words the bot still understands, so a menu
uploaded before keeps working. The other rows hold three buttons each (``ROW_COUNTS``, ``ROW_FONTS``):
「用法說明」 left the last one, whose four buttons needed a smaller type; typing 「說明」 still shows the help.
Registering a name is a one-off, so it has no button: 「我的設定」 and every reply that needs a name offer it instead.

Three menus (``ROLES``), one per kind of user: everyone's (``None``, the default), and for 幹部 (``"officer"``)
and administrators (``"admin"``) the same four rows plus a fifth, 「管理」 (``ROLE_BUTTONS``), so they can reach
「人員管理」 and 「人員名冊」 without typing them. The administrators' third button there is 「辨識回報」: their 收件匣,
where the reports of group photos and 航班人數 messages wait while the bot does not push; their 姓名申請 and 意見列表
are tiles of the 人員管理 card (``bot.staff_menu_card``). Five rows share the height four had (LINE wants the picture
at least 1.45 times as wide as it is tall, so it cannot grow): everything in a row is drawn smaller with the row
(``_scale``). The bot links each 幹部 and administrator to their menu (``RosterBot.sync_menus``); everybody else sees
the default.

    python -m all_staff.cli line-menu            # upload all three, make everyone's the default, relink 幹部／管理員
    python -m all_staff.cli line-menu --dry-run  # only (re)draw assets/line_rich_menu*.png

Re-running replaces the previous menus of the same names. Changes take effect for users
the next time they open the chat (sometimes after reopening LINE). A new menu has to be
uploaded after the server runs the code it needs: deploying alone does not change it.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROLES = (None, "officer", "admin")   # everyone, 幹部, administrators (``RosterBot.menu_role``)
NAMES = {None: "roster-bot-v2", "officer": "roster-bot-v2-officer", "admin": "roster-bot-v2-admin"}
NAME = NAMES[None]
CHAT_BAR = {None: "選單", "officer": "幹部選單", "admin": "管理員選單"}   # LINE: 14 characters at most
WIDTH, HEIGHT = 2500, 1686
ROW_COUNTS = (1, 3, 3, 3)
ROW_FONTS = ((124, 54), (124, 54), (124, 54), (124, 54))   # label, hint (pixels)
ROLE_FONTS = {3: (124, 54), 4: (104, 50)}   # the 「管理」 row by its buttons: three like the rows above; four (or any
#                                             other count) narrower, as 更多 was with four; scaled by ``_scale``
CATEGORY_WIDTH = 320
GROUPS = (("班表", "#173F68"), ("查詢", "#147D87"),
          ("人員", "#405879"), ("更多", "#52606D"))
MANAGE = ("管理", "#7A2E3E")   # a deep wine, apart from the blues and greys of everyone's rows
ASSETS = Path(__file__).resolve().parents[1] / "assets"
IMAGES = {None: ASSETS / "line_rich_menu.png", "officer": ASSETS / "line_rich_menu_officer.png",
          "admin": ASSETS / "line_rich_menu_admin.png"}
IMAGE = IMAGES[None]

# (label, hint, action) — action is ("message", text) or ("fill", text to pre-fill in the keyboard)
BUTTONS = [
    ("我的班表", "現在、下個小時、明天", ("message", "我的班表")),
    ("查看時段", "這個時段有誰上班", ("message", "查看時段")),
    ("查看工作", "選工作看整天名單", ("message", "查看工作")),
    ("查詢人員", "輸入代號或完整姓名", ("fill", "查 ")),
    ("勤務人員", "代號與姓名清單", ("message", "勤務人員")),
    ("休假人員", "輪休與請假名單", ("message", "休假人員")),
    ("人流預測", "各時段忙不忙", ("message", "人流預測")),
    ("本月統計", "時數與地點", ("message", "本月統計")),
    ("我的設定", "登記的姓名", ("message", "我的設定")),
    ("意見回饋", "回報給管理員", ("fill", "意見 ")),
]
# The 「管理」 row of the 幹部 and administrator menus: both reach 人員管理 and the 名冊 (bot.STAFF_MENU, STAFF_LIST);
# 幹部 then the fixed codes (FIXED_LIST), administrators their 收件匣, where the group photos' and 航班人數 messages'
# reports wait when the bot does not push (bot.MAILBOX). The administrators' 姓名申請 and 意見列表 (bot.NAME_REQUESTS,
# FEEDBACK_LIST), which 幹部 cannot use, are tiles of their 人員管理 card (bot.staff_menu_card) instead of buttons here.
ROLE_BUTTONS = {
    "officer": [
        ("人員管理", "名冊、代號、幹部", ("message", "人員管理")),
        ("人員名冊", "誰在名冊上", ("message", "人員名冊")),
        ("固定代號", "役男與英文代號", ("message", "固定代號")),
    ],
    "admin": [
        ("人員管理", "名冊、代號、幹部", ("message", "人員管理")),
        ("人員名冊", "誰在名冊上", ("message", "人員名冊")),
        ("辨識回報", "班表與人流", ("message", "辨識回報")),
    ],
}

API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"


def _layout(role: str | None = None) -> tuple[tuple[int, ...], tuple, list, tuple]:
    """``role``'s menu as rows: buttons per row, (category, colour) per row, the buttons in reading order and
    (label, hint) type sizes per row. Read at call time, so a patched ``BUTTONS`` is drawn too."""
    if role not in ROLES:
        raise ValueError(f"沒有「{role}」這種選單；只有 None（所有人）、officer（幹部）、admin（管理員）")
    if role is None:
        return ROW_COUNTS, GROUPS, list(BUTTONS), ROW_FONTS
    extra = ROLE_BUTTONS[role]
    return (ROW_COUNTS + (len(extra),), GROUPS + (MANAGE,), list(BUTTONS) + list(extra),
            ROW_FONTS + (ROLE_FONTS.get(len(extra), ROLE_FONTS[4]),))


def _scale(rows: int) -> float:
    """How large a row's type, icon and spacing are drawn when ``rows`` rows share the height: 1 for everyone's four
    (the picture stays as it was), less for the five of a role menu. It shrinks more gently than the row (√ of
    it: 0.89 where the row is 0.8), since four rows left room around their words and a phone shows them small."""
    return (len(ROW_COUNTS) / rows) ** .5


def areas(role: str | None = None) -> list[dict]:
    """The tappable areas of ``role``'s menu, row by row: they tile everything right of the category column."""
    counts, _, buttons, _ = _layout(role)
    result = []
    index = 0
    for row, columns in enumerate(counts):
        for column in range(columns):
            label, _, (kind, value) = buttons[index]
            x = CATEGORY_WIDTH + (WIDTH - CATEGORY_WIDTH) * column // columns
            right = CATEGORY_WIDTH + (WIDTH - CATEGORY_WIDTH) * (column + 1) // columns
            y, bottom = HEIGHT * row // len(counts), HEIGHT * (row + 1) // len(counts)
            bounds = {"x": x, "y": y, "width": right - x,
                      "height": bottom - y}
            if kind == "message":
                action = {"type": "message", "label": label, "text": value}
            else:
                action = {"type": "postback", "label": label, "data": f"fill:{value.strip()}",
                          "inputOption": "openKeyboard", "fillInText": value}
            result.append({"bounds": bounds, "action": action})
            index += 1
    return result


def menu(role: str | None = None) -> dict:
    return {"size": {"width": WIDTH, "height": HEIGHT}, "selected": True, "name": NAMES[role],
            "chatBarText": CHAT_BAR[role], "areas": areas(role)}


def _font(size: int):
    from PIL import ImageFont
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",                                   # macOS
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",                  # Debian/Ubuntu
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msjh.ttc",                                            # Windows
    ]
    for path in candidates:
        if Path(path).is_file():
            # index 3 of the Noto CJK collection is Traditional Chinese (TC)
            return ImageFont.truetype(path, size, index=3 if "NotoSansCJK" in path else 0)
    raise SystemExit("找不到中文字型；請在有中文字型的電腦執行 --dry-run 產生圖片，再把 assets/line_rich_menu*.png 一起部署。")


def _category_icon(pen, kind, cx, cy, scale=1.0):
    """Small, consistent line symbols for the categories; no font glyph dependency. Drawn at ``scale`` (``_scale``)."""
    def s(value):
        return round(value * scale)
    color, stroke = '#FFFFFF', s(10)
    if kind == '班表':
        pen.rounded_rectangle((cx-s(68), cy-s(59), cx+s(68), cy+s(66)), radius=s(10), outline=color, width=stroke)
        pen.line((cx-s(65), cy-s(20), cx+s(65), cy-s(20)), fill=color, width=stroke)
        for x in (cx-s(34), cx+s(34)):
            pen.line((x, cy-s(75), x, cy-s(43)), fill=color, width=stroke)
        for y in (cy+s(10), cy+s(39)):
            for x in (cx-s(31), cx, cx+s(31)):
                pen.ellipse((x-s(5), y-s(5), x+s(5), y+s(5)), fill=color)
    elif kind == '查詢':
        pen.ellipse((cx-s(69), cy-s(67), cx+s(35), cy+s(37)), outline=color, width=stroke)
        pen.line((cx+s(22), cy+s(25), cx+s(70), cy+s(73)), fill=color, width=stroke+s(2))
    elif kind == '人員':
        for x, y, size in ((cx-s(43), cy-s(24), s(27)), (cx+s(43), cy-s(24), s(27))):
            pen.ellipse((x-size, y-size, x+size, y+size), outline=color, width=stroke)
            pen.arc((x-s(45), y+s(37), x+s(45), y+s(112)), 180, 360, fill=color, width=stroke)
    elif kind == '更多':
        for dx in (-52, 0, 52):
            pen.ellipse((cx+s(dx)-s(17), cy-s(17), cx+s(dx)+s(17), cy+s(17)), fill=color)
    elif kind == '管理':   # a shield with a check: looking after the team's list, as 安檢 looks after the gate
        top, shoulder, waist, tip, half = cy-s(74), cy-s(54), cy+s(8), cy+s(78), s(62)
        left = [(cx-half, shoulder)] + [   # the lower edge curves in to the tip (a quadratic Bézier)
            ((1-t)**2*(cx-half) + 2*(1-t)*t*(cx-half) + t*t*cx, (1-t)**2*waist + 2*(1-t)*t*(cy+s(58)) + t*t*tip)
            for t in (i / 8 for i in range(9))]
        right = [(2*cx - x, y) for x, y in left]
        outline = [(cx, top)] + left + right[::-1] + [(cx, top)]
        pen.line(outline, fill=color, width=stroke, joint='curve')
        pen.line((cx-s(28), cy+s(4), cx-s(6), cy+s(26), cx+s(32), cy-s(16)), fill=color, width=stroke+s(2), joint='curve')
    else:
        for dy, dx in ((-44, -22), (0, 29), (44, -8)):
            y = cy+s(dy)
            pen.line((cx-s(69), y, cx+s(69), y), fill=color, width=stroke)
            pen.ellipse((cx+s(dx)-s(14), y-s(14), cx+s(dx)+s(14), y+s(14)), fill='#52606D', outline=color, width=s(8))


def _wide_button(pen, box, label, hint, big, small, accent, scale=1.0):
    """A row's only button, across the whole band: a tinted pill with a clock, the label and its hint beside it,
    centred together, and a chevron at its end, so the wide row still reads as one thing to tap."""
    def s(value):
        return round(value * scale)
    left, top, right, bottom = box
    pill = (left + s(56), top + s(44), right - s(56), bottom - s(44))
    pen.rounded_rectangle(pill, radius=(pill[3] - pill[1]) // 2, fill='#EAF1FA', outline='#C7D8EC', width=s(4))
    cy, radius, gap = (top + bottom) / 2, s(92), s(60)
    words = max(pen.textlength(label, font=big), pen.textlength(hint, font=small))
    start = (left + right - (2 * radius + gap + words)) / 2
    if start < pill[0] + s(120):
        raise ValueError(f"選單文字「{label}」「{hint}」放不進這一排；請縮短文字，或調小 ROW_FONTS")
    cx = start + radius
    pen.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=accent)
    pen.ellipse((cx - s(54), cy - s(54), cx + s(54), cy + s(54)), outline='#FFFFFF', width=s(10))   # a clock: now, the next hour
    pen.line((cx, cy + s(4), cx, cy - s(34)), fill='#FFFFFF', width=s(10))
    pen.line((cx - s(3), cy, cx + s(28), cy + s(16)), fill='#FFFFFF', width=s(10))
    x = start + 2 * radius + gap
    pen.text((x, cy - s(42)), label, font=big, fill=accent, anchor='lm')
    pen.text((x, cy + s(79)), hint, font=small, fill='#556679', anchor='lm')
    tip = pill[2] - s(96)
    pen.line((tip - s(34), cy - s(44), tip, cy, tip - s(34), cy + s(44)), fill=accent, width=s(12), joint='curve')


def draw(path: Path | None = None, role: str | None = None) -> Path:
    """Task bands, with category labels separate from the tappable actions: ``role``'s menu (``IMAGES[role]`` by
    default). Type, icons and spacing follow the row height (``_scale``); words that do not fit raise ValueError."""
    from PIL import Image, ImageDraw
    path = IMAGES[role] if path is None else path
    counts, groups, buttons, sizes = _layout(role)
    rows, scale = len(counts), _scale(len(counts))

    def s(value):
        return round(value * scale)
    image = Image.new("RGB", (WIDTH, HEIGHT), "#FFFFFF")
    pen = ImageDraw.Draw(image)
    category = _font(s(88))
    fonts = [(_font(s(label_size)), _font(s(hint_size))) for label_size, hint_size in sizes]
    for row, (label, accent) in enumerate(groups):
        top, bottom = HEIGHT * row // rows, HEIGHT * (row + 1) // rows
        cy = (top + bottom) / 2
        pen.rectangle((0, top, CATEGORY_WIDTH-1, bottom-1), fill=accent)
        _category_icon(pen, label, CATEGORY_WIDTH/2, cy-s(61), scale)
        pen.text((CATEGORY_WIDTH/2, cy+s(88)), label, font=category, fill='#FFFFFF', anchor='mm')
        if row:
            pen.line((CATEGORY_WIDTH, top, WIDTH, top), fill='#DCE5ED', width=6)
    row_of_button = [row for row, count in enumerate(counts) for _ in range(count)]
    for index, area in enumerate(areas(role)):
        b = area["bounds"]
        label, hint, _ = buttons[index]
        box = (b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"])
        row = row_of_button[index]
        big, small = fonts[row]
        accent = groups[row][1]
        if counts[row] == 1:   # 「我的班表」: the one thing used every day has the row to itself
            _wide_button(pen, box, label, hint, big, small, accent, scale)
            continue
        if b['x'] > CATEGORY_WIDTH:
            pen.line((b['x'], b['y']+s(52), b['x'], b['y']+b['height']-s(52)), fill='#DCE5ED', width=4)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        for words, font in ((label, big), (hint, small)):   # a longer label must not run into its neighbour
            if pen.textlength(words, font=font) > b['width'] - 80:
                raise ValueError(f"選單文字「{words}」放不進這一格（{b['width']} px）；請縮短文字，或調小 ROW_FONTS 第 {row + 1} 排")
        pen.text((cx, cy-s(42)), label, font=big, fill=accent, anchor='mm')
        pen.text((cx, cy+s(79)), hint, font=small, fill='#556679', anchor='mm')
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    return path


def _call(token: str, method: str, url: str, body: bytes | None = None, content_type: str | None = None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        with urlopen(Request(url, data=body, method=method, headers=headers), timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        raise SystemExit(f"LINE API {exc.code}（{method} {url}）：{exc.read().decode('utf-8', 'replace')}") from exc


def install(token: str, relink=None) -> dict:
    """Create the three menus and upload their images, make everyone's the default, move the per-user links, and
    only then remove the older copies of each name; the new ids by role.

    ``relink`` is called with those ids once the menus exist (``line-menu`` relinks every 幹部 and administrator
    with ``RosterBot.sync_menus``). Deleting a menu drops every link to it, so the old ones go last: until then a
    幹部 keeps the old menu, never the default. Should something fail on the way, the old menus stay and the new
    ones are left over: running it again removes both, as older copies of the same names."""
    for role in ROLES:
        if not IMAGES[role].is_file():
            draw(role=role)
    old = [m["richMenuId"] for m in json.loads(_call(token, "GET", f"{API}/richmenu/list"))["richmenus"]
           if m.get("name") in NAMES.values()]
    ids = {}
    for role in ROLES:
        menu_id = json.loads(_call(token, "POST", f"{API}/richmenu",
                                   json.dumps(menu(role), ensure_ascii=False).encode(),
                                   "application/json"))["richMenuId"]
        _call(token, "POST", f"{DATA_API}/richmenu/{menu_id}/content", IMAGES[role].read_bytes(), "image/png")
        ids[role] = menu_id
    _call(token, "POST", f"{API}/user/all/richmenu/{ids[None]}")
    if relink:
        relink(dict(ids))
    for menu_old in old:
        _call(token, "DELETE", f"{API}/richmenu/{menu_old}")
    return ids
