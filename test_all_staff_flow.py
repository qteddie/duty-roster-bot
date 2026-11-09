"""Passenger-flow forecast: the daily 航班人數 message, the grades made from it and 「人流預測」 in the bot, and how the
administrators hear that a group message was read."""
import time
import unittest
from datetime import date, datetime
from unittest.mock import patch

from all_staff import passenger_flow as pf
from all_staff.bot import (FLOW_COLORS, FLOW_NOTICES_SHOWN, REPORT_UI, TAIPEI, TEXT_LIMIT, Store, flow_digest,
                           flow_report)
from test_all_staff_bot import BotCase, flex_nodes, flex_texts, image_event, postback_event, slot, text_event

# The message as it was posted on 2026-10-06.
MESSAGE = """2026年10月06日
【國內 – 到站】
9-10時：7架473人
10-11時：3架145人
11-12時：2架113人
12-13時：3架210人
13-14時：3架157人
15-16時：6架331人
16-17時：5架229人
17-18時：1架61人
18-19時：4架373人
19-20時：7架669人
20-21時：2架112人
21-22時：2架140人
22-23時：3架225人
【國內 – 離站】
6-7時：2架74人
7-8時：6架529人
8-9時：2架209人
9-10時：4架281人
10-11時：6架473人
11-12時：3架274人
12-13時：2架155人
13-14時：4架198人
14-15時：1架17人
15-16時：2架190人
16-17時：8架501人
17-18時：2架113人
18-19時：1架31人
19-20時：4架329人
20-21時：1架38人
【國際 – 入境】
10-11時：2架232人
11-12時：3架234人
12-13時：2架370人
13-14時：2架304人
14-15時：1架117人
15-16時：2架514人
16-17時：2架398人
18-19時：2架199人
19-20時：1架125人
20-21時：2架288人
【國際 – 出境】
7-8時：2架368人
8-9時：1架178人
9-10時：2架430人
11-12時：1架155人
12-13時：2架458人
13-14時：2架389人
14-15時：2架329人
15-16時：1架288人
16-17時：2架561人
17-18時：1架271人
18-19時：1架205人
19-20時：1架147人
20-21時：1架145人
"""
TODAY = date(2026, 10, 6)
# Two equal waves of international passengers: at the line 10–12 and again 14–16.
TWO_WAVES = "2026年10月06日\n【國際 – 出境】\n12-13時：2架400人\n16-17時：2架400人\n"


def roster(day, staffing, run="flow-run"):
    """A recognised roster with the given first-line head counts: ``{(duty, hour): people}``."""
    people = {}
    for (duty, hour), count in staffing.items():
        for number in range(1, count + 1):
            people.setdefault(str(number), []).append(slot(hour - 1, 2 if duty == "國際線" else 17, duty))
    return {"run": {"id": run, "codes": len(people), "roster_date": day.isoformat()},
            "sheets": [{"cells": [], "warnings": []}], "names": {},
            "people": [{"code": code, "slots": slots} for code, slots in people.items()]}


def levels(graded, area):
    return {hour: item.level for hour, item in graded[area].items()}


class FlowMessageTests(unittest.TestCase):
    def test_reads_the_daily_message(self):
        flow = pf.parse(MESSAGE, near=TODAY)
        self.assertEqual(flow.day, TODAY)
        self.assertEqual(flow.total("dom", "arr"), (48, 3238))
        self.assertEqual(flow.total("dom"), (48, 3412))
        self.assertEqual(flow.total("intl", "arr"), (19, 2781))
        self.assertEqual(flow.total("intl"), (19, 3924))
        self.assertEqual(flow.series[("intl", "dep")][16], (2, 561))
        self.assertNotIn(10, flow.series[("intl", "dep")])          # an hour that is not listed has no flights

    def test_tolerates_width_spacing_dash_and_bracket_variants(self):
        flow = pf.parse("10/7 航班人數\n[國際-出境]\n7 - 8 時: 2 架 1,368 人\n８－９時：１架１７８人\n"
                        "國內線 離站\n6~7點 2架次74人\n23-24時：1架20人\n", near=TODAY)
        self.assertEqual(flow.day, date(2026, 10, 7))               # no year: the date closest to today
        self.assertEqual(flow.series[("intl", "dep")], {7: (2, 1368), 8: (1, 178)})
        self.assertEqual(flow.series[("dom", "dep")], {6: (2, 74), 23: (1, 20)})

    def test_the_date_may_be_written_the_roc_way_or_follow_another_date(self):
        body = "\n【國際 – 出境】\n7-8時：2架368人\n8-9時：1架178人\n"
        for head, expected in (("115年10月06日（二）", TODAY), ("115/10/06", TODAY), ("2026-10-06", TODAY),
                               ("10月6日", TODAY), ("1/2", date(2027, 1, 2)),
                               ("更新時間 10/5 20:30\n2026年10月07日", date(2026, 10, 7)),
                               ("臺中航空站旅客預報\n10/6 更新", TODAY), ("T1 航廈 2樓", None), ("13/40", None)):
            with self.subTest(head=head):
                self.assertEqual(pf.parse(head + body, near=TODAY).day, expected)

    def test_a_total_after_the_heading_and_clock_times_are_read(self):
        flow = pf.parse("10/6\n【國際 – 出境】共3架546人\n07:00-08:00 2架368人\n0800-0900：1架178人\n", near=TODAY)
        self.assertEqual(flow.series, {("intl", "dep"): {7: (2, 368), 8: (1, 178)}})

    def test_ordinary_chat_is_not_a_message(self):
        for value in ("", "今天好忙", "明天 10/7 班表出來了嗎", "9-10時：7架473人", "【國內 – 離站】",
                      "【國內 – 離站】\n今天沒有資料", "10/6\n【國際 – 出境】\n7-8時：2架368人", "架" * 7000,
                      "國內離站的旅客好多", "今天 2架飛機延誤 300人受影響", "10/6\n7-8時：2架368人\n8-9時：1架178人"):
            with self.subTest(value=value[:20]):
                self.assertIsNone(pf.parse(value, near=TODAY))

    def test_a_message_without_its_date_line_has_no_day(self):
        flow = pf.parse(MESSAGE.split("\n", 1)[1], near=TODAY)
        self.assertIsNone(flow.day)                                 # 「6-7時」 further down is not read as a date
        self.assertEqual(flow.total("intl"), (19, 3924))

    def test_rows_that_are_not_one_hour_are_skipped(self):
        flow = pf.parse("2026年10月06日\n【國際 – 出境】\n25-26時：1架10人\n9-11時：1架10人\n"
                        "7-8時：2架368人\n8-9時：1架178人\n", near=TODAY)
        self.assertEqual(sorted(flow.series[("intl", "dep")]), [7, 8])


class FlowForecastTests(unittest.TestCase):
    def test_passengers_are_spread_over_the_hours_before_departure(self):
        flow = pf.Flow(TODAY, {("intl", "dep"): {10: (1, 100)}, ("dom", "dep"): {10: (1, 100)}})
        load = pf.checkpoint_load(flow, "intl")
        self.assertEqual({h: round(v) for h, v in load.items() if v}, {7: 7, 8: 43, 9: 44, 10: 6})
        load = pf.checkpoint_load(flow, "dom")
        self.assertEqual({h: round(v) for h, v in load.items() if v}, {9: 100})   # all in the hour before
        for area, weights in pf.WEIGHTS.items():
            self.assertAlmostEqual(sum(weights), 1)

    def test_nobody_is_lost_or_invented(self):
        flow = pf.parse(MESSAGE, near=TODAY)
        for area in pf.AREAS:
            self.assertAlmostEqual(sum(pf.checkpoint_load(flow, area).values()), flow.total(area)[1])

    def test_the_line_peaks_before_the_timetable_does(self):
        flow = pf.parse(MESSAGE, near=TODAY)
        graded = pf.forecast(flow)
        self.assertEqual(pf.shown_hours(graded), list(range(5, 21)))
        self.assertEqual(pf.busy_ranges(graded["intl"]), [(11, 16)])
        self.assertEqual(pf.busy_ranges(graded["dom"]), [(6, 7), (9, 10), (15, 16)])
        # 16–17 has the most departing passengers (561) and 9–10 the third most (430), yet by then most of
        # them are through: the line is past its peak at 16 and at its quietest of the morning at 9.
        self.assertEqual(graded["intl"][16].level, pf.NORMAL)
        self.assertEqual(graded["intl"][9].level, pf.QUIET)
        self.assertEqual(graded["intl"][5].level, pf.NORMAL)        # nobody departs before 7, people already queue
        self.assertEqual(round(graded["intl"][14].load), 407)
        self.assertEqual(round(graded["dom"][9].load), 473)                   # the 473 who fly at 10–11

    def test_the_busy_and_the_quiet_hours_are_each_split_in_two(self):
        graded = pf.forecast(pf.parse(MESSAGE, near=TODAY))
        hours = lambda area: {level: [h for h, item in graded[area].items() if item.level == level]
                              for level in pf.LEVELS}
        self.assertEqual(hours("intl"), {pf.VERY_BUSY: [11, 14, 15], pf.BUSY: [12, 13],
                                         pf.NORMAL: [5, 6, 7, 8, 10, 16, 17], pf.QUIET: [9, 18],
                                         pf.VERY_QUIET: [19, 20, 21, 22, 23]})
        self.assertEqual(hours("dom"), {pf.VERY_BUSY: [6, 15], pf.BUSY: [9],
                                        pf.NORMAL: [7, 8, 10, 18], pf.QUIET: [11, 12, 14, 16],
                                        pf.VERY_QUIET: [5, 13, 17, 19, 20, 21, 22, 23]})

    def test_the_grade_follows_the_share_of_the_days_peak(self):
        # Three hours of 1,000 passengers are the peak; every other hour is graded against them.
        others = {13: 960, 14: 940, 15: 760, 16: 740, 17: 410, 18: 390, 19: 210, 20: 190}
        expected = {13: pf.VERY_BUSY, 14: pf.BUSY, 15: pf.BUSY, 16: pf.NORMAL, 17: pf.NORMAL,
                    18: pf.QUIET, 19: pf.QUIET, 20: pf.VERY_QUIET}
        flow = pf.Flow(TODAY, {("intl", "dep"): {h: (1, n) for h, n in {10: 1000, 11: 1000, 12: 1000,
                                                                       **others}.items()}})
        with patch.dict(pf.WEIGHTS, {"intl": (1.0,)}):      # everybody passes in the hour of departure
            graded = levels(pf.forecast(flow), "intl")
        self.assertEqual({h: graded[h] for h in others}, expected)
        self.assertEqual({graded[h] for h in (10, 11, 12)}, {pf.VERY_BUSY})
        self.assertEqual({graded[h] for h in (5, 9, 21, 23)}, {pf.VERY_QUIET})   # nobody at the line
        self.assertEqual(pf.LEVELS, ("超閒", "閒", "正常", "忙", "超忙"))

    def test_first_line_staff_counts_the_working_posts_only(self):
        result = {"people": [
            {"code": "1", "slots": [slot(4, 2, "國際線"), slot(5, 6, "國際線（二）"), slot(6, 17, "國內線一線")]},
            {"code": "2", "slots": [slot(4, 3, "國際線"), slot(4, 4, "國際線"), slot(6, 20, "國內線二線")]},
            {"code": "3", "slots": [slot(4, 9, "國際線備勤"), slot(5, 5, "c05"), slot(6, 21, "c21")]},
            {"code": "4", "slots": [slot(6, 18, ""), slot(6, 31, "A1站")]},
        ]}
        self.assertEqual(pf.first_line_staff(result), {"intl": {5: 2, 6: 1}, "dom": {7: 2}})
        self.assertEqual(pf.first_line_staff({"people": []}), {})

    def test_more_staff_makes_the_same_passengers_less_busy(self):
        flow = pf.parse(TWO_WAVES, near=TODAY)
        alone = pf.forecast(flow)
        self.assertEqual(pf.busy_ranges(alone["intl"]), [(10, 12), (14, 16)])
        self.assertIsNone(alone["intl"][10].staff)
        staffed = pf.forecast(flow, {"intl": {10: 2, 11: 2, 14: 6, 15: 6}})
        self.assertEqual(pf.busy_ranges(staffed["intl"]), [(10, 12)])
        self.assertEqual((staffed["intl"][10].staff, staffed["intl"][14].staff), (2, 6))
        self.assertEqual(set(levels(staffed, "dom").values()), {None})      # no domestic departures that day

    def test_an_hour_whose_head_count_was_not_read_borrows_the_days_median(self):
        flow = pf.parse(TWO_WAVES, near=TODAY)
        graded = pf.forecast(flow, {"intl": {10: 4, 11: 4, 14: 4}})        # 15 is missing, not empty
        self.assertEqual(graded["intl"][15].staff, 0)
        self.assertEqual(graded["intl"][15].level, graded["intl"][11].level)

    def test_a_few_passengers_are_never_busy(self):
        flow = pf.parse("2026年10月06日\n【國內 – 離站】\n19-20時：1架50人\n20-21時：1架40人\n", near=TODAY)
        graded = pf.forecast(flow)
        quiet = {hour for hour, level in levels(graded, "dom").items() if level == pf.QUIET}
        self.assertEqual(quiet, {18, 19})                       # 50 and 40 passengers, an hour early: the day's most
        self.assertEqual(set(levels(graded, "dom").values()), {pf.QUIET, pf.VERY_QUIET})
        self.assertEqual(pf.busy_ranges(graded["dom"]), [])

    def test_a_few_passengers_are_never_busy_however_few_the_staff(self):
        flow = pf.Flow(TODAY, {("intl", "dep"): {10: (1, 59), 11: (1, 60), 12: (2, 300), 13: (2, 300)}})
        with patch.dict(pf.WEIGHTS, {"intl": (1.0,)}):
            graded = levels(pf.forecast(flow, {"intl": {10: 1, 11: 1, 12: 10, 13: 10}}), "intl")
        # One person with 59 or 60 passengers has twice the work of the ten who share 300, yet under
        # 60 an hour the line is 閒 at most; from 60 on the share of the peak decides.
        self.assertEqual((graded[10], graded[11], graded[12]), (pf.QUIET, pf.VERY_BUSY, pf.NORMAL))


class FlowBotTests(BotCase):
    def card(self):
        message = self.transport.raw[-1][-1]
        self.assertEqual(message["type"], "flex")
        return message

    def rows(self, card=None):
        """Hour label → (國際 pill, 國內 pill)."""
        nodes = (card or self.card())["contents"]["body"]["contents"]
        return {node["contents"][0]["text"]: tuple(flex_texts(child)[0] for child in node["contents"][1:])
                for node in nodes if node.get("type") == "box" and len(node["contents"]) == 3
                and node["contents"][0].get("text", "")[:2].isdigit()}

    def post(self, value=MESSAGE, user="U2", group="Cgroup"):
        self.bot.handle(text_event(user, value, group))

    def test_a_group_message_is_recorded_silently_for_everyone(self):
        self.post()
        self.assertEqual(self.transport.replies, [])
        self.assertEqual(self.transport.pushes, [])
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask("人流預測", "U3")
        card = self.card()
        self.assertEqual(card["altText"], "10/6（二）人流預測｜國際最忙 11–16｜國內最忙 06–07、09–10、15–16")
        self.assertIn("表定：國際出境 3,924 人、國內離站 3,412 人", reply)
        self.assertIn("這天的班表還沒上線，先只看人流", reply)
        self.assertIn("沒有實測校正，僅供參考", reply)
        self.assertIn("資料：群組", reply)
        rows = self.rows(card)
        self.assertEqual(list(rows)[0], "05–06"); self.assertEqual(list(rows)[-1], "20–21")
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows["11–12"], ("超忙", "閒"))
        self.assertEqual(rows["12–13"], ("忙", "閒"))
        self.assertEqual(rows["09–10"], ("閒", "忙"))
        self.assertEqual(rows["05–06"], ("正常", "超閒"))

    def test_only_grades_are_shown_never_the_estimated_head_counts(self):
        self.post()
        with patch("all_staff.bot.today", return_value=TODAY):
            self.ask("人流預測")
        texts = flex_texts(self.card()["contents"])
        self.assertEqual({pill for pair in self.rows().values() for pill in pair},
                         {"超忙", "忙", "正常", "閒", "超閒"})
        self.assertEqual((texts.count("超忙"), texts.count("忙")), (5, 3))
        for estimate in ("401", "407", "402", "382"):
            self.assertFalse(any(estimate in value for value in texts), estimate)

    def test_every_grade_has_its_own_pill_colour(self):
        self.assertEqual(set(FLOW_COLORS), set(pf.LEVELS))
        self.assertEqual(len({background for background, _ in FLOW_COLORS.values()}), len(pf.LEVELS))
        self.post()
        with patch("all_staff.bot.today", return_value=TODAY):
            self.ask("人流預測")
        pills = [node["contents"][1] for node in self.card()["contents"]["body"]["contents"]
                 if node.get("type") == "box" and len(node["contents"]) == 3 and "contents" in node["contents"][1]]
        shown = {flex_texts(pill)[0]: (pill["backgroundColor"], pill["contents"][0]["color"]) for pill in pills}
        self.assertEqual(shown, FLOW_COLORS)                              # 國際 has all five on 10/6

    def test_ordinary_group_chat_is_not_recorded(self):
        for value in ("大家辛苦了", "9-10時：7架473人", "明天 10/7 班表出來了嗎"):
            self.post(value)
        self.assertEqual(self.store.flow_dates("U1"), [])
        self.assertEqual(self.transport.replies, [])
        self.post(MESSAGE.split("\n", 1)[1])                                # no date: which day is it for?
        self.assertEqual(self.store.flow_dates("U1"), [])

    def test_a_group_message_with_only_arrivals_is_not_recorded(self):
        arrivals = "2026年10月06日\n【國內 – 到站】\n6-7時：2架74人\n7-8時：3架120人\n8-9時：1架40人"
        self.post(arrivals)
        self.assertEqual(self.store.flow_dates("U1"), [])
        self.assertEqual(self.transport.replies, [])
        self.post(arrivals.replace("到站", "離站"))                         # departures: taken
        self.assertEqual(self.store.flow_dates("U1"), [date(2026, 10, 6)])

    def test_arrivals_pasted_privately_do_not_hide_the_groups_departures(self):
        self.post()
        arrivals = "2026年10月06日\n【國內 – 到站】\n6-7時：2架74人\n7-8時：3架120人"
        with patch("all_staff.bot.today", return_value=TODAY):
            self.assertIn("只有到站／入境", self.ask(arrivals, "U1"))
            self.assertIn("10/6（二）人流預測", self.ask("人流預測 10/6", "U1"))
        self.assertEqual(self.store.flow_on("U1", date(2026, 10, 6))["uploaded_by"], "U2")

    def test_without_a_message_the_reply_says_how_to_provide_one(self):
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask("人流預測")
            self.assertIn("還沒有今天（10/6）的航班人數資料", reply)
            self.assertIn("貼給我", reply)
            self.assertNotIn("quickReply", self.transport.raw[-1][0])
            self.post()
            reply = self.ask("人流預測 10/8")
            self.assertIn("還沒有 10/8（四）的航班人數資料", reply)
            self.assertIn("可查的日期：10/6（二）", reply)
            action = self.transport.raw[-1][0]["quickReply"]["items"][0]["action"]
            self.assertEqual((action["label"], action["data"]), ("看 10/6", "flow|2026-10-06"))
            self.bot.handle(postback_event("U1", action["data"]))
            self.assertIn("10/6（二）人流預測", self.card()["altText"])

    def test_a_message_pasted_privately_is_answered_and_stays_with_its_sender(self):
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask(MESSAGE, "U1")
            messages = self.transport.raw[-1]
            self.assertEqual([m["type"] for m in messages], ["text", "flex"])
            self.assertIn("已收到 10/6（二）的航班人數", messages[0]["text"])
            self.assertIn("只有你自己查得到", messages[0]["text"])
            self.assertIn("資料：你私訊", reply)
            self.assertIn("10/6（二）人流預測", self.ask("人流預測", "U1"))
            self.assertIn("還沒有今天（10/6）的航班人數資料", self.ask("人流預測", "U3"))
            self.bot.handle(postback_event("U3", "flow|2026-10-06"))
            self.assertIn("還沒有 10/6（二）的航班人數資料", self.reply())

    def test_a_pasted_message_without_a_date_counts_as_today(self):
        with patch("all_staff.bot.today", return_value=date(2026, 10, 9)):
            self.ask(MESSAGE.split("\n", 1)[1])
            self.assertIn("先當成今天", self.transport.raw[-1][0]["text"])
            self.assertIn("10/9（五）人流預測", self.card()["altText"])

    def test_the_days_roster_puts_its_first_line_staff_into_the_grade(self):
        self.post(TWO_WAVES)
        with patch("all_staff.bot.today", return_value=TODAY):
            self.ask("人流預測")
            self.assertIn("國際最忙 10–12、14–16", self.card()["altText"])
            self.assertNotIn("quickReply", self.card())
            self.analyzer.data = roster(TODAY, {("國際線", 10): 2, ("國際線", 11): 2,
                                                ("國際線", 14): 6, ("國際線", 15): 6})
            self.bot.handle(image_event("U2", "Cgroup"))
            reply = self.ask("人流預測")
        card = self.card()
        self.assertEqual(card["altText"], "10/6（二）人流預測｜國際最忙 10–12｜國內 沒有離站資料")
        self.assertIn("忙碌程度＝預估過安檢人數 ÷ 當時一線人數", reply)
        self.assertIn("國內的一線人數沒讀到，只看人流", reply)
        rows = self.rows(card)
        self.assertEqual((rows["10–11"], rows["14–15"]), (("超忙", "—"), ("正常", "—")))
        actions = [item["action"] for item in card["quickReply"]["items"]]
        self.assertEqual([(a["label"], a["data"]) for a in actions], [("查看時段", "slots|flow-run")])

    def test_a_roster_with_nobody_read_on_the_first_lines_falls_back_to_passengers(self):
        self.post()
        self.analyzer.data = {**roster(TODAY, {}), "people": [{"code": "甲", "slots": [slot(8, 9, "國際線備勤")]}]}
        self.bot.handle(image_event("U2", "Cgroup"))
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask("人流預測")
        self.assertIn("這天班表的一線人數沒讀到，先只看人流", reply)
        self.assertIn("國際最忙 11–16", self.card()["altText"])

    def test_today_and_tomorrow_switch_and_tomorrow_shows_while_today_has_none(self):
        tomorrow = MESSAGE.replace("2026年10月06日", "2026年10月07日")
        self.post(tomorrow)
        with patch("all_staff.bot.today", return_value=TODAY):
            self.ask("人流預測")
            self.assertIn("10/7（三）人流預測", self.card()["altText"])     # only tomorrow's has been posted
            self.assertNotIn("quickReply", self.card())
            self.post()
            self.ask("人流預測")
            card = self.card()
            self.assertIn("10/6（二）人流預測", card["altText"])
            actions = [item["action"] for item in card["quickReply"]["items"]]
            self.assertEqual([(a["label"], a["data"]) for a in actions],
                             [("✓今天 10/6", "flow|2026-10-06"), ("看明天 10/7", "flow|2026-10-07")])
            self.bot.handle(postback_event("U1", actions[1]["data"]))
            self.assertIn("10/7（三）人流預測", self.card()["altText"])
            self.assertIn("10/7（三）人流預測", self.ask("人流預測 明天"))

    def test_the_current_hour_is_framed_only_on_todays_card(self):
        self.post()
        self.post(MESSAGE.replace("2026年10月06日", "2026年10月07日"))
        framed = lambda: [node["contents"][0]["text"] for node in self.card()["contents"]["body"]["contents"]
                          if node.get("borderWidth")]
        with patch("all_staff.bot.taipei_now", return_value=datetime(2026, 10, 6, 11, 30, tzinfo=TAIPEI)):
            self.assertIn("框起來的是現在的時段", self.ask("人流預測"))
            self.assertEqual(framed(), ["11–12"])
            self.assertNotIn("框起來", self.ask("人流預測 10/7"))
            self.assertEqual(framed(), [])
        with patch("all_staff.bot.taipei_now", return_value=datetime(2026, 10, 6, 23, 30, tzinfo=TAIPEI)):
            self.assertNotIn("框起來", self.ask("人流預測"))             # no departures left: nothing to frame
            self.assertEqual(framed(), [])

    def test_other_wordings_and_dates_open_the_forecast(self):
        self.post()
        with patch("all_staff.bot.today", return_value=TODAY):
            for command in ("人流預測", "人流", "忙不忙", "人流預測 今天", "人流預測 10/6", "10/6 人流預測"):
                with self.subTest(command=command):
                    self.assertIn("10/6（二）人流預測", self.ask(command))

    def test_the_newest_message_of_a_day_wins_and_a_repost_is_kept_once(self):
        newer = MESSAGE.replace("7-8時：2架368人", "7-8時：2架999人")
        self.store.add_flow("Cgroup", "2026-10-06", MESSAGE, "U2", 100.0)
        self.store.add_flow("Cgroup", "2026-10-06", newer, "U2", 200.0)
        self.assertEqual(self.store.flow_on("U1", TODAY)["raw"], newer)
        self.store.add_flow("Cgroup", "2026-10-06", MESSAGE, "U2", 100.0)   # a redelivered webhook changes nothing
        self.assertEqual(self.store.flow_on("U1", TODAY)["raw"], newer)
        self.store.add_flow("Cgroup", "2026-10-06", MESSAGE, "U2", 300.0)   # posted again: the current one
        self.assertEqual(self.store.flow_on("U1", TODAY)["raw"], MESSAGE)
        self.assertEqual(self.store._one("select count(*) from flows")[0], 2)
        self.assertEqual(self.store.flow_dates("U1"), [TODAY])

    def test_keeping_a_message_says_how_it_compares_with_the_days_others(self):
        newer = MESSAGE.replace("7-8時：2架368人", "7-8時：2架999人")
        add = lambda raw, sent, chat="Cgroup": self.store.add_flow(chat, "2026-10-06", raw, "U2", sent)
        self.assertEqual(add(MESSAGE, 100.0), "new")
        self.assertEqual(add(newer, 200.0), "replaced")
        self.assertEqual(add(MESSAGE, 100.0), "repeat")                   # a redelivered webhook
        self.assertEqual(add(MESSAGE, 300.0), "replaced")                 # the first text, the newest again
        self.assertEqual(add(MESSAGE, 400.0), "again")                    # nothing changes
        self.assertEqual(add(MESSAGE, 410.0, "Cother"), "again")          # the same text from another group
        self.assertEqual(add(newer, 250.0, "Cother"), "older")            # sent before the newest one: only kept
        self.assertEqual(self.store.flow_on("U1", TODAY)["raw"], MESSAGE)
        self.assertEqual(self.store.add_flow("Cgroup", "2026-10-07", MESSAGE, "U2", 100.0), "new")
        self.assertEqual(self.store.add_flow("U1", "2026-10-06", newer, "U1", 500.0), "replaced")   # for U1 only
        self.assertEqual(self.store.add_flow("U3", "2026-10-06", newer, "U3", 50.0), "older")

    def test_a_message_with_arrivals_only_says_there_is_nothing_to_forecast(self):
        arrivals = "2026年10月06日\n【國內 – 到站】\n9-10時：7架473人\n10-11時：3架145人\n"
        with patch("all_staff.bot.today", return_value=TODAY):
            reply = self.ask(arrivals)
        self.assertIn("只有到站／入境，人流預測只看離站／出境的人數，所以沒有記下", reply)
        self.assertEqual(self.store.flow_dates("U1"), [])

    def test_a_malformed_date_in_a_button_is_ignored(self):
        self.post()
        before = len(self.transport.replies)
        self.bot.handle(postback_event("U1", "flow|not-a-date"))
        self.bot.handle(postback_event("U1", "flow|2026-10-06|extra"))
        self.assertEqual(len(self.transport.replies), before)


ARRIVALS = "2026年10月06日\n【國內 – 到站】\n6-7時：2架74人\n7-8時：3架120人\n8-9時：1架40人"
UNDATED = MESSAGE.split("\n", 1)[1]


def group_text(user, value, sent, group="Cgroup"):
    """A message posted in a group at ``sent`` (seconds): LINE's event time, which a redelivery keeps."""
    return {**text_event(user, value, group), "timestamp": int(sent * 1000)}


def taiwan(sent) -> str:
    when = datetime.fromtimestamp(int(sent * 1000) / 1000, TAIPEI)
    return f"{when.month}/{when.day} {when:%H:%M}"


class FlowNoticeCase(BotCase):
    """A group that posts 航班人數 messages, with administrators: the helpers of the two classes below."""
    settings = {"admins": ("U9",), "notify": False}

    def setUp(self):
        super().setUp()
        self.sent = time.time() - 3600

    def post(self, value=MESSAGE, user="U2", *, after=0, group="Cgroup"):
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(group_text(user, value, self.sent + after, group))

    def say(self, value, user="U9"):
        with patch("all_staff.bot.today", return_value=TODAY):
            return self.ask(value, user)

    def pending(self):
        return self.store.undelivered_flow_notices()

    def notice(self, number, status="ok", detail="new", sent=None):
        """Queue ``number`` notices straight into the store, a minute apart."""
        for index in range(number):
            self.store.add_flow_notice("Cgroup", "U2", "華華", (sent or self.sent) + 60 * index, status,
                                       "2026-10-06", detail, f"國際出境 {index + 1:,} 人")


class FlowNoticeTests(FlowNoticeCase):
    """When the push of a group 航班人數 message reaches no administrator (here U9 is not a friend; or the monthly
    quota is used up), how it was read still reaches them for free: with their next reply. What waits is never pushed
    later (every push counts against the monthly quota; replies do not)."""
    friends = {"U1", "U2", "U3"}

    def test_a_kept_message_rides_on_the_administrators_next_reply_once(self):
        self.post()
        self.assertEqual((self.transport.replies, self.transport.pushes), ([], []))   # nothing reached U9 either
        notice, = self.pending()
        self.assertEqual((notice["status"], notice["detail"], notice["flow_date"], notice["poster"]),
                         ("ok", "new", "2026-10-06", "華華"))
        self.say("我的設定")
        first, answer = self.transport.raw[-1]
        self.assertEqual(first["text"], f"📊 人流資料讀取結果（1 則）\n\n✅ 10/6（二）的航班人數已記下\n"
                                        f"華華・{taiwan(self.sent)} 貼到群組\n國際出境 3,924 人、國內離站 3,412 人")
        self.assertNotIn("quickReply", first)
        self.assertIn("姓名：", answer["text"])
        self.assertIn("quickReply", answer)                                  # the answer keeps its buttons
        self.assertEqual(self.pending(), [])
        self.assertNotIn("人流資料", self.say("我的設定"))                    # shown once
        self.assertEqual(self.transport.pushes, [])

    def test_skipped_messages_say_why_and_what_to_do(self):
        self.post(ARRIVALS)
        self.post(UNDATED, "U1", after=60)
        self.assertEqual(self.store.flow_dates("U9"), [])                   # neither was kept
        self.assertEqual([(n["status"], n["detail"]) for n in self.pending()],
                         [("skipped", "arrivals"), ("skipped", "undated")])
        reply = self.say("我的設定")
        self.assertIn("📊 人流資料讀取結果（2 則）", reply)
        self.assertIn(f"⚠️ 沒有記下：只有到站／入境\n華華・{taiwan(self.sent)} 貼到群組\n10/6（二）・國內到站 234 人\n"
                      "👉 人流預測只看離站／出境", reply)
        self.assertIn(f"⚠️ 沒有記下：訊息裡沒有日期\n明明・{taiwan(self.sent + 60)} 貼到群組\n"
                      "國際出境 3,924 人、國內離站 3,412 人\n👉 請連同日期那一行", reply)
        self.assertLess(reply.index("只有到站"), reply.index("沒有日期"))      # newest last
        self.assertEqual(self.transport.pushes, [])

    def test_a_later_message_of_the_day_says_it_replaced_the_earlier_one(self):
        self.post()
        self.post(MESSAGE.replace("7-8時：2架368人", "7-8時：2架999人"), "U1", after=60)
        self.post(MESSAGE.replace("7-8時：2架368人", "7-8時：2架500人"), "U1", after=-60)   # sent before both
        self.assertEqual([n["detail"] for n in self.pending()], ["new", "replaced", "older"])
        reply = self.say("我的設定")
        self.assertIn("國際出境 4,555 人、國內離站 3,412 人\n🔄 取代了同一天較早貼的那則", reply)
        self.assertIn("國際出境 4,056 人、國內離站 3,412 人\n🗂️ 同一天已有更晚貼的一則，預測仍用那則", reply)
        self.assertEqual(reply.count("取代了"), 1)

    def test_a_registered_poster_is_named_by_their_registered_name(self):
        self.register("U1", "林小明")
        self.post(user="U1")
        self.assertEqual(self.pending()[0]["poster"], "林小明")
        self.assertIn(f"林小明・{taiwan(self.sent)} 貼到群組", self.say("我的設定"))

    def test_a_redelivered_webhook_leaves_no_second_notice(self):
        for value in (MESSAGE, ARRIVALS, UNDATED):
            self.post(value)
            self.post(value)
        self.assertEqual([n["status"] for n in self.pending()], ["ok", "skipped", "skipped"])

    def test_nobody_else_sees_them(self):
        self.post()
        self.register("U1", "林小明")
        for user in ("U1", "U2", "U3"):
            self.assertNotIn("人流資料", self.say("我的設定", user))
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("人流資料", self.say("我的設定"))
        self.assertEqual(self.transport.pushes, [])

    def test_a_button_tap_by_the_administrator_carries_them_too(self):
        self.post()
        with patch("all_staff.bot.today", return_value=TODAY):
            self.bot.handle(postback_event("U9", "flow|2026-10-06"))
        first, card = self.transport.raw[-1]
        self.assertIn("📊 人流資料讀取結果", first["text"])
        self.assertEqual(card["type"], "flex")                               # the forecast itself follows

    def test_private_pastes_leave_no_notice(self):
        for user in ("U1", "U9"):
            self.say(MESSAGE, user)
            self.say(ARRIVALS, user)
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.transport.pushes, [])

    def test_ordinary_group_chat_leaves_no_notice(self):
        for value in ("大家辛苦了", "9-10時：7架473人", "明天 10/7 班表出來了嗎"):
            self.post(value)
        self.assertEqual(self.pending(), [])

    def test_a_reply_that_fails_has_shown_nothing(self):
        self.post()
        with patch.object(self.transport, "reply", side_effect=RuntimeError("LINE API 400: Invalid reply token")):
            with self.assertRaises(RuntimeError):
                self.say("我的設定")
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("人流資料", self.say("我的設定"))
        self.assertEqual(self.pending(), [])

    def test_feedback_and_notices_each_take_a_message_when_there_is_room(self):
        self.post()
        self.store.add_feedback("U1", "林小明", "明明", "第二則")
        self.say("我的設定")
        feedback, notice, answer = self.transport.raw[-1]
        self.assertIn("📮 新的意見回饋（1 則）", feedback["text"])
        self.assertIn("📊 人流資料讀取結果（1 則）", notice["text"])
        self.assertIn("姓名：", answer["text"])
        self.assertEqual((self.store.undelivered_feedback(), self.pending()), ([], []))

    def test_a_reply_of_three_still_carries_both_and_loses_nothing(self):
        self.post()
        self.store.add_feedback("U1", "林小明", "明明", "第二則")
        own = [{"type": "text", "text": str(n)} for n in range(3)]
        self.bot._turn.admin = True
        self.bot._reply("token", own)
        sent = self.transport.raw[-1]
        self.assertEqual(len(sent), 5)
        self.assertIn("意見回饋", sent[0]["text"]); self.assertIn("人流資料", sent[1]["text"])
        self.assertEqual(sent[2:], own)
        self.assertEqual((self.store.undelivered_feedback(), self.pending()), ([], []))

    def test_with_room_for_one_feedback_and_notices_share_it(self):
        self.post()
        self.store.add_feedback("U1", "林小明", "明明", "第二則")
        own = [{"type": "text", "text": str(n)} for n in range(4)]
        self.bot._turn.admin = True
        self.bot._reply("token", own)
        sent = self.transport.raw[-1]
        self.assertEqual(len(sent), 5)
        self.assertEqual(sent[1:], own)
        self.assertIn("📮 新的意見回饋（1 則）", sent[0]["text"])
        self.assertIn("第二則", sent[0]["text"])
        self.assertIn("📊 人流資料讀取結果（1 則）", sent[0]["text"])
        self.assertEqual((self.store.undelivered_feedback(), self.pending()), ([], []))

    def test_sharing_one_message_keeps_it_under_the_limit_and_the_rest_waits(self):
        for index in range(11):   # as long as they come: a long LINE name, a two-part summary
            self.store.add_flow_notice("Cgroup", "U2", "王" * 20, self.sent + 60 * index, "skipped", "2026-10-06",
                                       "arrivals", f"國際入境 {3900 + index:,} 人、國內到站 {3400 + index:,} 人")
        for number in range(1, 8):
            self.store.add_feedback("U1", "林小明", "明明", f"第 {number} 則" + "字" * 990)
        self.bot._turn.admin = True
        self.bot._reply("token", [{"type": "text", "text": str(n)} for n in range(4)])
        shared = self.transport.raw[-1][0]["text"]
        self.assertLess(len(shared), TEXT_LIMIT)                              # made to fit, not cut by text()
        self.assertIn("📊 人流資料讀取結果（11 則）", shared)
        self.assertIn("國際入境 3,910 人、國內到站 3,410 人", shared)            # the newest notice, whole
        self.assertIn("第 1 則", shared)
        self.assertNotIn("第 7 則", shared)
        self.assertIn("…還有", shared)
        self.assertEqual(self.pending(), [])
        waiting = self.store.undelivered_feedback()
        self.assertTrue(waiting)                                              # the rest come with the next reply
        self.assertIn(f"第 {waiting[0]['id']} 則", self.say("我的設定"))

    def test_replies_with_photos_carry_them_too_but_a_push_never_does(self):
        """``_answer`` (查班表, 勤務人員, a private photo's result…) replies as ``_reply`` does; past the reply window, or
        with the reply refused, it pushes the answer alone and the notices wait."""
        self.post()
        self.transport.friends.add("U9")                                     # added the bot after the post
        self.bot._turn.admin = True
        self.bot._answer("token", "U9", time.monotonic(), [{"type": "text", "text": "勤務卡"}])
        notice, card = self.transport.raw[-1]
        self.assertIn("📊 人流資料讀取結果（1 則）", notice["text"])
        self.assertEqual(card["text"], "勤務卡")
        self.assertEqual(self.pending(), [])
        self.notice(1)

        def refused(token, messages):
            raise RuntimeError("LINE API 400: Invalid reply token")
        self.transport.reply = refused
        self.bot._turn.admin = True
        self.bot._answer("token", "U9", time.monotonic(), [{"type": "text", "text": "勤務卡"}])
        self.assertEqual(self.transport.raw_pushes[-1], ("U9", [{"type": "text", "text": "勤務卡"}]))
        self.assertEqual(len(self.pending()), 1)                    # not shown: it comes with the next reply
        self.bot._turn.admin = True
        self.bot._answer("token", "U9", time.monotonic() - 3600, [{"type": "text", "text": "勤務卡"}])   # too late
        self.assertEqual(self.transport.raw_pushes[-1], ("U9", [{"type": "text", "text": "勤務卡"}]))
        self.assertEqual(len(self.pending()), 1)

    def test_a_full_reply_is_left_alone(self):
        self.post()
        self.store.add_feedback("U1", "林小明", "明明", "第二則")
        five = [{"type": "text", "text": str(n)} for n in range(5)]
        self.bot._turn.admin = True
        self.bot._reply("token", five)
        self.assertEqual(self.transport.raw[-1], five)
        self.assertEqual((len(self.store.undelivered_feedback()), len(self.pending())), (1, 1))

    def test_the_feedback_list_still_has_the_notices_in_front(self):
        self.post()
        self.store.add_feedback("U1", "林小明", "明明", "第一則")
        self.say("意見列表")
        notice, listing = self.transport.raw[-1]
        self.assertIn("人流資料", notice["text"])
        self.assertNotIn("意見回饋", notice["text"])
        self.assertIn("📮 最近的意見回饋（1 則）", listing["text"])
        self.assertEqual(self.pending(), [])

    def test_the_newest_are_listed_and_the_older_ones_counted(self):
        self.notice(3, "skipped", "arrivals")
        self.store.mark_flow_notices([1])                                    # one of them was seen already
        self.notice(FLOW_NOTICES_SHOWN + 1, sent=self.sent + 3600)
        reply = self.say("我的設定")
        first = self.transport.raw[-1][0]["text"]
        self.assertTrue(first.startswith(f"📊 人流資料讀取結果（{FLOW_NOTICES_SHOWN + 3} 則）\n\n"
                                         "另有 3 則較早的：✅ 1 則已記下、⚠️ 2 則沒有記下。\n\n✅ "), first)
        self.assertEqual(first.count("✅ 10/6（二）"), FLOW_NOTICES_SHOWN)
        self.assertNotIn("國際出境 1 人", first)                              # the oldest kept one is only counted
        self.assertLess(first.index("國際出境 2 人"), first.index(f"國際出境 {FLOW_NOTICES_SHOWN + 1} 人"))
        self.assertTrue(first.endswith(f"國際出境 {FLOW_NOTICES_SHOWN + 1} 人"))   # newest last
        self.assertIn("姓名：", reply)
        self.assertEqual(self.pending(), [])                                 # the counted ones are accounted for
        message, shown = flow_digest([{"id": n, "chat_id": "Cgroup", "user_id": "U2", "poster": "華華",
                                       "sent_at": self.sent + n, "status": "ok", "flow_date": "2026-10-06",
                                       "detail": "new", "summary": None, "created_at": self.sent}
                                      for n in range(FLOW_NOTICES_SHOWN + 2)])
        self.assertIn("另有 2 則較早的：都已記下 ✅。", message["text"])
        self.assertEqual(len(shown), FLOW_NOTICES_SHOWN + 2)
        self.assertEqual(flow_digest([]), (None, []))

    def test_notices_survive_a_restart_and_old_databases_get_the_table(self):
        self.post()
        self.store.db.close()
        again = Store(self.path)
        self.assertEqual([n["poster"] for n in again.undelivered_flow_notices()], ["華華"])
        again.db.execute("drop table flow_notices"); again.db.commit(); again.db.close()
        upgraded = Store(self.path)
        self.assertEqual(upgraded.undelivered_flow_notices(), [])
        self.assertEqual(upgraded.add_flow_notice("Cgroup", "U2", "華華", 1.0, "ok", "2026-10-06", "new", None), 1)
        upgraded.db.close()


def report_rows(card) -> list[tuple[str, str]]:
    """A 人流資料 report's (label, value) rows: the white box under the band."""
    box = card["contents"]["body"]["contents"][0]
    return [(line["contents"][0]["text"], line["contents"][1]["text"]) for line in box["contents"]]


def report_link(card) -> dict:
    """The action of a report's footer row, the date's 人流預測."""
    action, = [node["action"] for node in flex_nodes(card["contents"]["footer"]) if node.get("action")]
    return action


class FlowReportTests(FlowNoticeCase):
    """Each 航班人數 message posted in a group is pushed to every administrator at once, like the photo reports: one
    card, in one push each (a push counts once per recipient against the monthly quota). Once one of them has it,
    the notice is delivered and rides on no reply."""
    friends = {"U1", "U2", "U3", "U8", "U9"}
    settings = {"admins": ("U9", "U8"), "notify": False}

    def tearDown(self):
        from test_all_staff_duty_card import check_limits   # here: that module imports this one through another
        for _, messages in self.transport.raw_pushes:
            for message in messages:
                check_limits(self, message)
        super().tearDown()

    def cards(self, admin="U9") -> list[dict]:
        """What ``admin`` was pushed, a card a push."""
        pushed = [messages for to, messages in self.transport.raw_pushes if to == admin]
        self.assertTrue(all(len(messages) == 1 for messages in pushed), pushed)
        return [messages[0] for messages in pushed]

    def test_a_kept_message_is_pushed_to_each_administrator_at_once(self):
        self.post()
        self.assertEqual(self.transport.replies, [])                         # the group hears nothing
        self.assertEqual([to for to, _ in self.transport.raw_pushes], ["U9", "U8"])   # one push each
        card, = self.cards("U9")
        self.assertEqual(self.cards("U8"), [card])
        bubble = card["contents"]
        self.assertEqual(flex_texts(bubble["header"]), ["✅ 群組人流資料讀取成功", f"華華｜{taiwan(self.sent)} 傳到群組"])
        self.assertEqual(bubble["header"]["background"]["startColor"], REPORT_UI["ok"][0])
        self.assertEqual(report_rows(card), [("📅 資料日期", "10/6（二）"), ("✈️ 國際出境", "3,924 人"),
                                             ("🏠 國內離站", "3,412 人")])
        self.assertEqual(flex_texts(bubble["footer"]), ["📊 看 10/6（二）的人流預測"])
        self.assertEqual(report_link(card), {"type": "postback", "label": "人流預測 10/6", "data": "flow|2026-10-06",
                                             "displayText": "人流預測 10/6"})
        self.assertEqual(card["altText"], f"✅ 群組人流資料讀取成功｜華華 {taiwan(self.sent)}｜資料日期 10/6（二），"
                                          "國際出境 3,924 人，國內離站 3,412 人")
        self.assertEqual(self.pending(), [])                                 # delivered: no reply carries it
        for admin in ("U9", "U8"):
            self.assertNotIn("人流資料", self.say("我的設定", admin))
        self.assertEqual(len(self.transport.raw_pushes), 2)

    def test_a_reply_sent_while_the_card_is_being_pushed_does_not_carry_it_too(self):
        push, replied = self.transport.push, []

        def slow(to, messages):   # another worker answers U8 while the first push is still out
            if not replied:
                self.bot._turn.admin = True
                self.bot._reply("token", [{"type": "text", "text": "我的設定"}])
                replied.append(self.transport.raw[-1])
            return push(to, messages)
        self.transport.push = slow
        self.post()
        self.assertEqual(replied, [[{"type": "text", "text": "我的設定"}]])   # no 人流資料 digest in front of it
        self.assertEqual(len(self.cards("U8")), 1)
        self.assertEqual(self.pending(), [])

    def test_the_button_opens_that_days_forecast(self):
        self.post()
        self.post(MESSAGE.replace("2026年10月06日", "2026年10月07日"), after=60)   # the next day's, posted later
        first, second = self.cards("U8")
        for card, day in ((first, "10/6（二）"), (second, "10/7（三）")):
            with patch("all_staff.bot.today", return_value=TODAY):
                self.bot.handle(postback_event("U8", report_link(card)["data"]))
            forecast = self.transport.raw[-1][-1]
            self.assertTrue(forecast["altText"].startswith(f"{day}人流預測｜"), forecast["altText"])

    def test_a_skipped_message_is_pushed_with_why_and_what_to_do(self):
        self.post(ARRIVALS)
        self.post(UNDATED, "U1", after=60)
        self.assertEqual(self.store.flow_dates("U9"), [])                   # neither was kept
        arrivals, undated = self.cards("U9")
        self.assertEqual(self.cards("U8"), [arrivals, undated])
        for card, who, sent in ((arrivals, "華華", self.sent), (undated, "明明", self.sent + 60)):
            self.assertEqual(flex_texts(card["contents"]["header"]),
                             ["⚠️ 群組人流資料沒有記下", f"{who}｜{taiwan(sent)} 傳到群組"])
            self.assertEqual(card["contents"]["header"]["background"]["startColor"], REPORT_UI["failed"][0])
            self.assertNotIn("footer", card["contents"])                     # nothing kept to forecast
        self.assertEqual(report_rows(arrivals), [("原因", "只有到站／入境"), ("📅 資料日期", "10/6（二）"),
                                                 ("🏠 國內到站", "234 人")])
        self.assertTrue(flex_texts(arrivals["contents"]["body"])[-1].startswith("👉 人流預測只看離站／出境"))
        self.assertEqual(report_rows(undated), [("原因", "訊息裡沒有日期"), ("✈️ 國際出境", "3,924 人"),
                                                ("🏠 國內離站", "3,412 人")])
        self.assertEqual(flex_texts(undated["contents"]["body"])[-1],
                         "👉 請連同日期那一行（例如 2026年10月06日）重貼到群組。")
        self.assertIn("原因 訊息裡沒有日期", undated["altText"])
        self.assertEqual(self.pending(), [])
        self.assertNotIn("人流資料", self.say("我的設定"))

    def test_the_version_row_says_how_it_compares_with_the_days_others(self):
        later = MESSAGE.replace("7-8時：2架368人", "7-8時：2架999人")
        self.post()
        self.post(later, "U1", after=60)
        self.post(MESSAGE.replace("7-8時：2架368人", "7-8時：2架500人"), "U1", after=-60)   # sent before both
        self.post(later, after=120)                                         # the newest one, posted again
        versions = [[row for row in report_rows(card) if row[0].endswith("版本")] for card in self.cards()]
        self.assertEqual(versions, [[], [("🔄 版本", "取代了同一天較早貼的那則")],
                                    [("🗂️ 版本", "同一天已有更晚貼的一則，預測仍用那則")],
                                    [("🔁 版本", "和同一天原本的資料相同")]])
        self.assertTrue(all("footer" in card["contents"] for card in self.cards()))

    def test_a_redelivered_webhook_pushes_nothing(self):
        for value in (MESSAGE, ARRIVALS, UNDATED):
            self.post(value)
            self.post(value)                                                 # LINE sends the same event again
        self.assertEqual(len(self.cards("U9")), 3)
        self.assertEqual(len(self.cards("U8")), 3)
        self.assertEqual(len(self.store.db.execute("select id from flow_notices").fetchall()), 3)
        self.assertEqual(self.pending(), [])

    def test_a_redelivery_after_nobody_was_reached_pushes_nothing_either(self):
        with patch.object(self.transport, "push", side_effect=RuntimeError("LINE API 429: monthly limit")):
            self.post()
        self.post()                                                          # the quota came back meanwhile
        self.assertEqual(self.transport.raw_pushes, [])
        self.assertEqual(len(self.pending()), 1)                             # it waits for a reply instead

    def test_nobody_but_the_administrators_is_pushed(self):
        self.register("U1", "林小明")
        self.post(user="U1")
        self.post(ARRIVALS, "U3", after=60)
        self.say(MESSAGE, "U2")                                              # a private paste is answered, not pushed
        self.assertEqual({to for to, _ in self.transport.raw_pushes}, {"U9", "U8"})
        self.assertEqual(len(self.transport.raw_pushes), 4)
        self.assertEqual(flex_texts(self.cards()[0]["contents"]["header"])[1], f"林小明｜{taiwan(self.sent)} 傳到群組")

    def test_private_pastes_push_nothing(self):
        for user in ("U1", "U9"):
            self.say(MESSAGE, user)
            self.say(ARRIVALS, user)
        self.assertEqual(self.transport.raw_pushes, [])
        self.assertEqual(self.pending(), [])

    def test_one_administrator_reached_is_enough(self):
        self.transport.friends.discard("U8")                                 # blocked the bot, say
        self.post()
        self.assertEqual([to for to, _ in self.transport.raw_pushes], ["U9"])
        self.assertEqual(self.pending(), [])
        self.assertNotIn("人流資料", self.say("我的設定", "U8"))

    def test_with_nobody_reached_it_rides_on_the_next_reply_as_before(self):
        with patch.object(self.transport, "push", side_effect=RuntimeError("LINE API 429: monthly limit")):
            self.post()
        self.assertEqual(len(self.pending()), 1)
        self.say("我的設定", "U8")
        first, answer = self.transport.raw[-1]
        self.assertTrue(first["text"].startswith("📊 人流資料讀取結果（1 則）\n\n✅ 10/6（二）的航班人數已記下"))
        self.assertIn("姓名：", answer["text"])
        self.assertEqual(self.pending(), [])
        self.assertNotIn("人流資料", self.say("我的設定", "U9"))             # shown once, to one of them

    def test_the_card_stays_within_lines_limits(self):
        from test_all_staff_duty_card import check_limits
        sent = datetime(2026, 10, 13, 16, 14, tzinfo=TAIPEI).timestamp()
        long = "國際出境 123,456 人、國內離站 98,765 人"
        for status, detail, day in (("ok", "new", "2026-10-13"), ("ok", "replaced", "2026-10-13"),
                                    ("ok", "again", "2026-12-31"), ("ok", "older", "2026-10-13"),
                                    ("skipped", "arrivals", "2026-10-13"), ("skipped", "undated", None),
                                    ("skipped", "something new", None)):
            card = flow_report({"poster": "王" * 60, "sent_at": sent, "status": status, "flow_date": day,
                                "detail": detail, "summary": long})
            check_limits(self, card)
            self.assertIn("｜10/13 16:14 傳到群組", flex_texts(card["contents"]["header"])[1])   # Taiwan time
        unknown = flow_report({"poster": None, "sent_at": sent, "status": "skipped", "flow_date": None,
                               "detail": "something new", "summary": None})
        self.assertEqual(report_rows(unknown), [("原因", "讀不出可用的資料")])
        self.assertEqual(flex_texts(unknown["contents"]["header"])[1], "不明的上傳者｜10/13 16:14 傳到群組")


class FlowNoticeWithoutAdministratorsTests(BotCase):
    settings = {"notify": False}

    def test_nothing_is_queued_or_pushed(self):
        for value in (MESSAGE, ARRIVALS, UNDATED):
            self.bot.handle(text_event("U2", value, "Cgroup"))
        self.assertEqual(self.store.undelivered_flow_notices(), [])
        self.assertEqual(self.transport.pushes, [])
        self.assertEqual(self.store.flow_dates("U1"), [TODAY])              # the message itself is still kept


if __name__ == "__main__":
    unittest.main()
