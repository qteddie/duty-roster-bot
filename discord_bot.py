#!/usr/bin/env python3
"""Discord entry point for the daily roster reader.

Start it with ``python discord_bot.py`` after setting DISCORD_BOT_TOKEN and
OPENAI_API_KEY in .env.  The bot only handles images in DISCORD_CHANNEL_ID when
that optional setting is present.
"""

from __future__ import annotations

import os
import asyncio
from io import BytesIO

import discord
from dotenv import load_dotenv

from backend.schedule_service import ScheduleUploadError, analyze_schedule_image

load_dotenv()

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_DISCORD_MESSAGE = 1_900
MAX_DISCORD_EMBED_DESCRIPTION = 1_450
DISPLAY_CONFIDENCE_THRESHOLD = 0.5


def _is_visible(item: dict) -> bool:
    """Keep very-low-confidence hours blank, but retain plausible rest slots."""
    confidence = item.get("confidence", 1)
    return isinstance(confidence, (int, float)) and confidence >= DISPLAY_CONFIDENCE_THRESHOLD


def _needs_confirmation(item: dict) -> bool:
    confidence = item.get("confidence", 1)
    return not isinstance(confidence, (int, float)) or confidence < 0.8 or bool(item.get("note"))


def format_discord_report(result: dict, max_length: int = MAX_DISCORD_MESSAGE) -> str:
    """Create a compact, readable Discord response from the API result."""
    date = result.get("document_date") or "日期待確認"
    name = result.get("employee_name", "蕭志明")
    code = result.get("employee_code", "己")
    audit = result.get("audit") or {}
    is_day_off = result.get("is_day_off", not result.get("assignments"))
    schedule_type = result.get("schedule_type") or (
        "off" if is_day_off
        else "domestic" if result.get("roster_profile") == "domestic_control"
        else "international"
    )
    schedule_label = {"off": "休假", "international": "國際線", "domestic": "國內線"}[schedule_type]
    audit_label = (
        "當日休假" if is_day_off
        else "已辨識 8 個時段" if audit.get("passed")
        else "時段尚未完整"
    )
    ranges = result.get("working_ranges") or []
    range_text = "、".join(ranges) if ranges else "尚未辨識到工作時段"
    rest_ranges = result.get("resting_ranges") or []
    rest_text = "、".join(rest_ranges) if rest_ranges else "尚未辨識到休息時段"

    lines = [f"**{date}｜{name}（{code}）**", f"**班別：** {schedule_label}", audit_label]
    if is_day_off:
        lines.append("該員工當天沒有排入任何時段。")
    else:
        lines.extend([
            f"**需要工作的時段：** {range_text}",
            f"**休息的時段：** {rest_text}",
            "",
            "**逐時班表（固定 8 小時；空白＝未確認）**",
        ])
        assignments_by_time: dict[str, list[dict]] = {}
        for item in result.get("assignments", []):
            assignments_by_time.setdefault(item.get("time", "--"), []).append(item)
        slots = result.get("shift_slots") or sorted(assignments_by_time)
        for slot in slots:
            visible = [item for item in assignments_by_time.get(slot, []) if _is_visible(item)]
            if not visible:
                lines.append(f"• `{slot}`")
                continue
            duties = "／".join(
                f"{item.get('shift', '未知')}｜{'上班' if item.get('needs_work') else '休息'}"
                f"{'（待確認）' if _needs_confirmation(item) else ''}"
                for item in visible
            )
            lines.append(f"• `{slot}`　{duties}")

    report = "\n".join(lines).replace("⚠️", "").replace("⚠", "")
    return report[:max_length - 1] + "…" if len(report) > max_length else report


def format_usage_report(result: dict) -> str:
    usage = result["usage"]
    cost = usage["estimated_usd"]
    amount = f"US${cost:.6f}" if cost is not None else "費率或用量不足，無法估算"
    lifetime = result["usage_lifetime"]
    total = lifetime["known_estimated_usd"]
    incomplete = "（部分呼叫無法計價）" if lifetime["unpriced_calls"] else ""
    return (f"本次辨識統計｜{'、'.join(usage['models'])}\n"
            f"耗時 {result['timing']['total_seconds']:.1f} 秒（前處理 {result['timing']['preprocessing_seconds']:.1f} 秒）\n"
            f"API {usage['api_calls']} 次｜輸入 {usage['input_tokens']:,}／輸出 {usage['output_tokens']:,} tokens\n"
            f"本次估計費用：{amount}\n"
            f"開始記錄以來累計：US${total:.6f}{incomplete}\n"
            "依API用量與標準費率估算，實際金額以OpenAI帳單為準；不含記錄前的呼叫。")


def crop_attachment(result: dict) -> discord.File | None:
    """Convert an in-memory prepared crop into a Discord attachment."""
    crop = result.get("crop_image")
    if not crop:
        return None
    return discord.File(BytesIO(crop["content"]), filename=crop["filename"])


class RosterBot(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.analysis_lock = asyncio.Lock()

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"已登入 Discord：{self.user}（{self.user.id}）")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.attachments:
            return

        channel_setting = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if channel_setting and str(message.channel.id) != channel_setting:
            return

        image = next((item for item in message.attachments if item.content_type in IMAGE_TYPES), None)
        if image is None:
            await message.reply("請上傳 JPG、PNG 或 WEBP 格式的班表照片。", mention_author=False)
            return
        if image.size > 15 * 1024 * 1024:
            await message.reply("圖片不可超過 15 MB。", mention_author=False)
            return

        await message.reply(
            "已收到班表，正在排隊。" if self.analysis_lock.locked() else "已收到班表，開始辨識四人班表，處理時間大約六分鐘。",
            mention_author=False,
        )
        async with self.analysis_lock, message.channel.typing():
            try:
                payload = await image.read(use_cached=True)
                result = await analyze_schedule_image(
                    payload, filename=image.filename, content_type=image.content_type,
                    all_employees=True, include_crop_images=True, use_final_markdown=True,
                )
            except ScheduleUploadError as exc:
                await message.reply(str(exc), mention_author=False)
                return
            except Exception as exc:
                # Keep credentials and internal error details out of the Discord channel.
                await message.reply("班表辨識失敗，請確認照片清楚後再試一次。", mention_author=False)
                print(f"班表辨識失敗（message={message.id}）：{exc}")
                return

        # A single Discord message can contain several embeds and attachments.
        # This keeps the four employees together instead of sending four replies.
        embeds = [
            discord.Embed(
                description=format_discord_report(
                    employee, max_length=MAX_DISCORD_EMBED_DESCRIPTION
                )
            )
            for employee in result["employees"]
        ]
        attachments = [
            attachment
            for employee in result["employees"]
            if (attachment := crop_attachment(employee)) is not None
        ]
        await message.reply(
            format_usage_report(result),
            embeds=embeds,
            files=attachments,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def main() -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("尚未設定 DISCORD_BOT_TOKEN，請填入 .env 後重試。")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("尚未設定 OPENAI_API_KEY，請填入 .env 後重試。")

    intents = discord.Intents.default()
    intents.message_content = True
    RosterBot(intents=intents).run(token, log_handler=None)


if __name__ == "__main__":
    main()
