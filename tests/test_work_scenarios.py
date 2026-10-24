"""Replay realistic work scenarios without using Feishu or the live database."""

from datetime import datetime
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from app.config import Settings
from app.periods import SHANGHAI
from app.reporting import build_report
from app.storage import Storage
from tests.helpers import temporary_directory


SELF = "ou_scenario000000000000000000000001"
COLLEAGUE = "ou_scenario000000000000000000000002"
ALPHA = "oc_scenario000000000000000000000001"
BETA = "oc_scenario000000000000000000000002"


def moment(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=SHANGHAI).timestamp())


def record(mid, at, text, sender=SELF, chat=ALPHA, name=None):
    return {
        "message_id": mid, "chat_id": chat, "sender_id": sender,
        "sender_name": name or sender, "text": text, "created_at": moment(at),
    }


def work_sample():
    """Fictional, stable work records; dates and authors are deliberately varied."""
    return [
        record("sep30", "2026-09-30T23:59:59", "已交付九月边界归档"),
        record("oct01", "2026-10-01T00:00:00", "已完成十月初初始化"),
        record("oct18", "2026-10-18T23:59:59", "已交付上周同事交付物", COLLEAGUE),
        record("oct19", "2026-10-19T00:00:00", "已完成本周首日测试环境准备"),
        record("done", "2026-10-20T00:00:00", "已完成接口回归并交付测试记录"),
        record("active", "2026-10-20T10:00:00", "正在联调登录接口，尚未完成验收"),
        record("blocked", "2026-10-20T11:00:00", "阻塞：测试账号未开通，需要管理员协助"),
        record("plan", "2026-10-20T12:00:00", "明天计划完成订单回归并准备评审"),
        record("colleague-done", "2026-10-20T13:00:00", "已上线同事专属支付模块", COLLEAGUE),
        record("colleague-mention", "2026-10-20T14:00:00", "已交付同事专属部署清单，请 @阿舟 帮忙核对", COLLEAGUE),
        record("last-second", "2026-10-20T23:59:59", "正在整理当日最后一份日志", COLLEAGUE),
        record("oct21", "2026-10-21T00:00:00", "已完成次日零点发布检查"),
        record("oct25", "2026-10-25T23:59:59", "已交付本周末同事验收记录", COLLEAGUE),
        record("oct26", "2026-10-26T00:00:00", "已完成下周边界初始化"),
        record("oct31", "2026-10-31T23:59:59", "已完成十月最后归档"),
        record("nov01", "2026-11-01T00:00:00", "已完成十一月边界初始化"),
        record("beta-self", "2026-10-20T09:00:00", "已完成乙群专属需求梳理", chat=BETA),
        record("beta-colleague", "2026-10-20T10:00:00", "正在处理乙群同事专属任务", COLLEAGUE, BETA),
    ]


class WorkScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.store = Storage(Path(self.tmp.name) / "work-scenarios.sqlite3")
        self.store.save_chats("feishu", [
            {"chat_id": ALPHA, "name": "甲项目工作群"},
            {"chat_id": BETA, "name": "乙项目工作群"},
        ])
        # A completed month makes calendar-window tests independent of today's clock.
        clock = patch("app.storage.time.time", return_value=moment("2026-11-02T12:00:00"))
        clock.start()
        self.addCleanup(clock.stop)

    def load_sample(self):
        self.store.save_messages("feishu", work_sample())

    def generate(self, **options):
        arguments = dict(source="feishu", period="daily", anchor_date="2026-10-20", chat_ids=[ALPHA])
        arguments.update(options)
        return build_report(self.store, self.settings, **arguments)

    @staticmethod
    def source_ids(report):
        appendix = report["markdown"].split("## 消息来源", 1)[1]
        return re.findall(r"消息标识：([^\n]+)", appendix)

    @staticmethod
    def section(report, name, next_name):
        return report["markdown"].split("## " + name + "\n", 1)[1].split("## " + next_name + "\n", 1)[0]

    def test_mixed_work_states_stay_in_their_sections(self):
        self.load_sample()
        report = self.generate(audience="personal", sender_ids=[SELF])
        cases = [
            ("本期成果", "工作进展", "已完成接口回归"),
            ("工作进展", "风险与待协调", "正在联调登录接口"),
            ("风险与待协调", "下一步计划", "测试账号未开通"),
            ("下一步计划", "补充记录", "明天计划完成订单回归"),
        ]
        for name, following, text in cases:
            with self.subTest(state=name):
                self.assertIn(text, self.section(report, name, following))
        finished = self.section(report, "本期成果", "工作进展")
        self.assertNotIn("尚未完成验收", finished)
        self.assertNotIn("明天计划", finished)
        self.assertEqual(report["message_count"], 4)

    def test_one_numbered_message_can_contain_four_states_without_creating_extra_messages(self):
        self.store.save_messages("feishu", [record("mixed-checklist", "2026-10-20T18:00:00",
            "1、已完成接口回归\n2、正在整理测试报告\n3、阻塞：验收环境无法访问\n4、明天计划完成评审")])
        report = self.generate(audience="personal", sender_ids=[SELF])
        for name, following, text in [
            ("本期成果", "工作进展", "已完成接口回归"),
            ("工作进展", "风险与待协调", "正在整理测试报告"),
            ("风险与待协调", "下一步计划", "验收环境无法访问"),
            ("下一步计划", "补充记录", "明天计划完成评审"),
        ]:
            with self.subTest(state=name):
                section = self.section(report, name, following)
                self.assertIn(text, section)
                self.assertIn("[M001]", section)
        self.assertEqual(report["message_count"], 1)
        self.assertEqual(self.source_ids(report), ["mixed-checklist"])
        self.assertNotIn("[M002]", report["markdown"])

    def test_explicit_completion_variants_do_not_promote_negative_or_conditional_statements(self):
        statements = [
            "接口测试已经完成", "接口开发完成", "接口未开发完成", "预计接口开发完成",
            "接口开发完成后再测试", "接口开发完成度80%",
        ]
        self.store.save_messages("feishu", [
            record(f"status-{index}", f"2026-10-20T{index + 9:02}:00:00", text)
            for index, text in enumerate(statements)
        ])
        report = self.generate()
        completed = self.section(report, "本期成果", "工作进展")
        for text in statements[:2]:
            self.assertIn(text, completed)
        for text in statements[2:]:
            with self.subTest(statement=text):
                self.assertNotIn(text, completed)
        self.assertIn("[M001]", completed)
        self.assertIn("[M002]", completed)
        for index in range(3, 7):
            self.assertNotIn(f"[M{index:03}]", completed)
        self.assertEqual(report["message_count"], 6)

    def test_personal_only_uses_selected_author_even_if_a_colleague_mentions_them(self):
        self.load_sample()
        report = self.generate(audience="personal", sender_ids=[SELF])
        self.assertEqual(set(self.source_ids(report)), {"done", "active", "blocked", "plan"})
        self.assertNotIn("同事专属", report["markdown"])
        self.assertNotIn("乙群专属", report["markdown"])
        self.assertNotIn(COLLEAGUE, report["markdown"])
        self.assertEqual(report["sender_ids"], [SELF])

    def test_project_preserves_both_authors_and_ignores_personal_selection(self):
        self.load_sample()
        report = self.generate(audience="project", sender_ids=[SELF])
        self.assertEqual(report["message_count"], 7)
        self.assertEqual(report["sender_ids"], [])
        finished = self.section(report, "本期成果", "工作进展")
        self.assertRegex(finished, r"成员·[A-Za-z0-9]{6}：已完成接口回归")
        self.assertRegex(finished, r"成员·[A-Za-z0-9]{6}：已上线同事专属支付模块")
        audit = report["markdown"].split("## 成员标识核对", 1)[1].replace("\\_", "_")
        self.assertIn(SELF, audit)
        self.assertIn(COLLEAGUE, audit)

    def test_daily_weekly_monthly_counts_for_both_audiences(self):
        self.load_sample()
        for period, project_count, personal_count, start, end in [
            ("daily", 7, 4, "2026-10-20", "2026-10-20"),
            ("weekly", 10, 6, "2026-10-19", "2026-10-25"),
            ("monthly", 14, 9, "2026-10-01", "2026-10-31"),
        ]:
            for audience, expected in [("project", project_count), ("personal", personal_count)]:
                with self.subTest(period=period, audience=audience):
                    report = self.generate(period=period, audience=audience, sender_ids=[SELF])
                    self.assertEqual(report["message_count"], expected)
                    self.assertEqual(len(self.source_ids(report)), expected)
                    self.assertEqual((report["start_date"], report["end_date"]), (start, end))

    def test_shanghai_day_includes_midnight_and_last_second_but_excludes_next_midnight(self):
        self.load_sample()
        ids = set(self.source_ids(self.generate()))
        self.assertIn("done", ids)  # 10-20 00:00 Shanghai = 10-19 16:00 UTC.
        self.assertIn("last-second", ids)
        self.assertNotIn("oct19", ids)
        self.assertNotIn("oct21", ids)

    def test_week_boundary_is_monday_to_sunday_in_shanghai(self):
        self.load_sample()
        ids = set(self.source_ids(self.generate(period="weekly")))
        self.assertIn("oct19", ids)
        self.assertIn("oct25", ids)
        self.assertNotIn("oct18", ids)
        self.assertNotIn("oct26", ids)

    def test_month_boundary_keeps_october_and_excludes_adjacent_months(self):
        self.load_sample()
        ids = set(self.source_ids(self.generate(period="monthly")))
        self.assertIn("oct01", ids)
        self.assertIn("oct31", ids)
        self.assertNotIn("sep30", ids)
        self.assertNotIn("nov01", ids)

    def test_group_selection_changes_scope_without_leaking_other_group(self):
        self.load_sample()
        beta = self.generate(chat_ids=[BETA])
        self.assertEqual(set(self.source_ids(beta)), {"beta-self", "beta-colleague"})
        self.assertIn("乙项目工作群", beta["markdown"])
        self.assertNotIn("甲项目工作群", beta["markdown"])
        self.assertNotIn("接口回归", beta["markdown"])
        combined = self.generate(chat_ids=[ALPHA, BETA])
        self.assertEqual(combined["message_count"], 9)
        self.assertEqual(len(self.source_ids(combined)), 9)

    def test_replaying_same_message_ids_does_not_inflate_any_report(self):
        self.load_sample()
        self.store.save_messages("feishu", work_sample())
        self.assertEqual(self.store.count("feishu"), 18)
        self.assertEqual(self.generate()["message_count"], 7)
        self.assertEqual(self.generate(period="monthly")["message_count"], 14)

    def test_repeated_statement_merges_body_but_keeps_every_distinct_message_reference(self):
        records = [
            record("repeat-one", "2026-10-20T09:00:00", "已完成重复同步验收"),
            record("repeat-two", "2026-10-20T10:00:00", "已完成重复同步验收"),
        ]
        self.store.save_messages("feishu", records)
        self.store.save_messages("feishu", records)
        report = self.generate()
        finished = self.section(report, "本期成果", "工作进展")
        self.assertEqual(finished.count("已完成重复同步验收"), 1)
        self.assertIn("[M001] [M002]", finished)
        self.assertEqual(self.source_ids(report), ["repeat-one", "repeat-two"])
        self.assertEqual(report["message_count"], 2)

    def test_missing_selected_group_remains_named_in_coverage_warning(self):
        self.load_sample()
        self.store.save_messages("feishu", [], coverage=(
            [ALPHA], moment("2026-10-01T00:00:00"), moment("2026-11-01T00:00:00")))
        report = self.generate(period="monthly", chat_ids=[ALPHA, BETA])
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_chat_ids"], [BETA])
        warning = next(line for line in report["markdown"].splitlines() if "同步覆盖提醒" in line)
        self.assertIn("乙项目工作群", warning)
        self.assertNotIn("甲项目工作群", warning)
        self.assertIn("部分消息草稿", warning)

    def test_date_gap_requires_a_successful_query_even_if_it_has_no_new_messages(self):
        self.load_sample()
        for start, end in [("2026-10-01", "2026-10-20"), ("2026-10-21", "2026-11-01")]:
            self.store.save_messages("feishu", [], coverage=(
                [ALPHA], moment(start + "T00:00:00"), moment(end + "T00:00:00")))
        before = self.generate(period="monthly")
        self.assertIn("部分消息草稿", before["markdown"])
        self.assertFalse(before["coverage"]["complete"])
        self.store.save_messages("feishu", [], coverage=(
            [ALPHA], moment("2026-10-20T00:00:00"), moment("2026-10-21T00:00:00")))
        after = self.generate(period="monthly")
        self.assertTrue(after["coverage"]["complete"])
        self.assertNotIn("部分消息草稿", after["markdown"])
        self.assertEqual(after["message_count"], before["message_count"])
        self.assertIn("已查询", after["markdown"])

    def test_identifiers_stay_out_of_report_body_but_remain_in_audit_mapping(self):
        self.load_sample()
        report = self.generate()
        body, appendix = report["markdown"].split("## 消息来源", 1)
        for opaque_id in (SELF, COLLEAGUE, ALPHA, BETA):
            self.assertNotIn(opaque_id, body.replace("\\_", "_"))
        self.assertRegex(body, r"成员·[A-Za-z0-9]{6}")
        self.assertEqual(len({m["sender_name"] for m in report["evidence"]}), 2)
        self.assertIn(SELF, appendix.replace("\\_", "_"))
        self.assertIn(COLLEAGUE, appendix.replace("\\_", "_"))

    def test_successful_query_during_current_month_does_not_claim_future_days_are_observed(self):
        now = moment("2026-10-20T12:00:00")
        with patch("app.storage.time.time", return_value=now):
            self.store.save_messages("feishu", [record("today", "2026-10-20T10:00:00", "已完成当日回归")],
                coverage=([ALPHA], moment("2026-10-01T00:00:00"), moment("2026-11-01T00:00:00")))
            report = self.generate(period="monthly")
        self.assertTrue(report["coverage"]["complete"])
        self.assertTrue(report["coverage"]["period_has_future"])
        self.assertEqual(report["coverage"]["checked_through_ts"], now)
        self.assertEqual(report["coverage"]["observed_message_count"], 1)
        self.assertEqual(report["coverage"]["active_day_count"], 1)
        self.assertIn("查询截至：2026-10-20 12:00:00", report["markdown"])
        self.assertIn("周期尚未结束", report["markdown"])
        self.assertIn("不能代表整个周期的最终成果", report["markdown"])

    def test_multi_group_common_query_time_uses_the_earlier_group_and_survives_saved_report(self):
        month = (moment("2026-10-01T00:00:00"), moment("2026-11-01T00:00:00"))
        earlier, later = moment("2026-10-20T11:00:00"), moment("2026-10-20T12:00:00")
        with patch("app.storage.time.time", return_value=earlier):
            self.store.save_messages("feishu", [record("alpha", "2026-10-20T10:00:00", "已完成甲群回归")],
                coverage=([ALPHA], *month))
        with patch("app.storage.time.time", return_value=later):
            self.store.save_messages("feishu", [record("beta", "2026-10-20T10:00:00", "已完成乙群回归", chat=BETA)],
                coverage=([BETA], *month))
            report = self.generate(period="monthly", chat_ids=[ALPHA, BETA])
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["checked_through_ts"], earlier)
        self.assertEqual(report["coverage"]["last_synced_at"], later)
        self.assertIn("查询截至：2026-10-20 11:00:00", report["markdown"])
        self.assertEqual(self.store.report(report["id"])["coverage"], report["coverage"])


if __name__ == "__main__":
    unittest.main()
