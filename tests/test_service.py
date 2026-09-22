"""家庭持续阅读服务测试：逐条锁定业务不变量。"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path

from src import readmodels as rm
from src.events import PAYLOAD_REQUIRED
from src.service import DomainError, ReadingService, age_on

T = datetime.fromisoformat  # "2026-09-15T10:00+08:00" -> aware datetime
ROOT = Path(__file__).parents[1]


def build_world() -> ReadingService:
    """搭一个含武汉/北京/长沙三店、家长+8岁孩子、若干书目的最小世界。"""
    svc = ReadingService()
    svc.register_store("store-bj", "北京", "句象书店北京店", at=T("2026-08-01T09:00+08:00"))
    svc.register_store("store-wh", "武汉", "句象书店武汉店", at=T("2026-08-01T09:00+08:00"))
    svc.register_store("store-cs", "长沙", "句象书店长沙店", at=T("2026-08-01T09:00+08:00"))

    svc.register_reader("r-parent", "fam-1", "林一", "1988-03-10",
                        ["历史"], at=T("2026-08-02T10:00+08:00"))
    svc.register_reader("r-kid", "fam-1", "林小夏", "2018-06-01",
                        ["宇宙", "动物"], at=T("2026-08-02T10:05+08:00"))
    svc.declare_guardianship("fam-1", "r-parent", "r-kid", "母亲",
                             at=T("2026-08-02T10:10+08:00"))

    # 家长自己（成人无需年龄限制）；孩子由家长代办授权，孩子本人也授予推荐/反馈范围
    svc.grant_consent("c-kid-loan", "r-kid", "loan",
                      actor_id="r-parent", on_behalf=True,
                      at=T("2026-08-02T10:15+08:00"))
    svc.grant_consent("c-kid-rec", "r-kid", "recommendation",
                      actor_id="r-kid", at=T("2026-08-02T10:16+08:00"))
    svc.grant_consent("c-kid-fb", "r-kid", "feedback",
                      actor_id="r-kid", at=T("2026-08-02T10:17+08:00"))
    svc.grant_consent("c-kid-guide", "r-kid", "guidance",
                      actor_id="r-kid", at=T("2026-08-02T10:18+08:00"))
    svc.grant_consent("c-kid-event", "r-kid", "event",
                      actor_id="r-parent", on_behalf=True,
                      at=T("2026-08-02T10:19+08:00"))
    for scope in ("loan", "recommendation", "feedback", "guidance", "event", "profile"):
        svc.grant_consent(f"c-parent-{scope}", "r-parent", scope,
                          at=T("2026-08-02T10:20+08:00"))

    svc.register_title("t-space", "星星信使", "某某", 6, 10, ["宇宙"],
                       "初版说明：一场飞向土星的信。",
                       at=T("2026-08-03T09:00+08:00"))
    svc.publish_edition("e-space", "t-space", "978-1-0", "未来出版社",
                        at=T("2026-08-03T09:05+08:00"))
    svc.register_title("t-teen", "暗夜推理社", "另某", 13, 17, ["推理"],
                       "青少年推理故事。", at=T("2026-08-03T09:10+08:00"))
    svc.publish_edition("e-teen", "t-teen", "978-2-0", "未来出版社",
                        at=T("2026-08-03T09:15+08:00"))
    svc.register_title("t-bear", "小熊的森林", "某甲", 4, 8, ["动物"],
                       "森林里的小熊。", at=T("2026-08-03T09:20+08:00"))
    svc.publish_edition("e-bear", "t-bear", "978-3-0", "未来出版社",
                        at=T("2026-08-03T09:25+08:00"))
    return svc


class TestAgeAndConsent(unittest.TestCase):
    def test_age_calculation(self):
        # 2026-09-22 时 2018-06-01 出生 = 8 岁
        self.assertEqual(age_on("2018-06-01", date(2026, 9, 22)), 8)
        self.assertEqual(age_on("2018-12-31", date(2026, 9, 22)), 7)

    def test_minor_only_sees_age_appropriate_catalog(self):
        svc = build_world()
        cat = rm.visible_catalog(svc, "r-kid", date(2026, 9, 22))
        ids = {row["title_id"] for row in cat}
        self.assertEqual(ids, {"t-space", "t-bear"})
        # 成人不受适龄过滤
        self.assertEqual(
            {r["title_id"] for r in rm.visible_catalog(svc, "r-parent", date(2026, 9, 22))},
            {"t-space", "t-teen", "t-bear"},
        )

    def test_cannot_borrow_or_recommend_over_age(self):
        svc = build_world()
        svc.add_copies("b1", "store-wh", "e-teen", 2, at=T("2026-08-04T09:00+08:00"))
        with self.assertRaises(DomainError):
            svc.request_loan("req-teen", "r-kid", "e-teen", "store-wh",
                             actor_id="r-parent", on_behalf=True,
                             at=T("2026-08-04T10:00+08:00"))
        with self.assertRaises(DomainError):
            svc.recommend_title("rec-teen", "r-kid", "t-teen", "试试看", "librarian",
                                at=T("2026-08-04T10:00+08:00"))

    def test_consent_required_for_loan(self):
        svc = build_world()
        svc.register_reader("r-noconsent", "fam-2", "小满", "2019-01-01", ["动物"],
                            at=T("2026-08-02T11:00+08:00"))
        svc.add_copies("b2", "store-wh", "e-bear", 1, at=T("2026-08-04T09:00+08:00"))
        with self.assertRaises(DomainError):
            svc.request_loan("req-nc", "r-noconsent", "e-bear", "store-wh",
                             at=T("2026-08-04T10:00+08:00"))

    def test_child_self_withdrawal_blocks_guardian_regrant(self):
        svc = build_world()
        # 孩子本人撤回 feedback 授权
        svc.withdraw_consent("c-kid-fb", "r-kid", "feedback",
                             actor_id="r-kid", at=T("2026-09-01T09:00+08:00"))
        with self.assertRaises(DomainError):
            svc.record_feedback("f1", "fam-1", "r-kid", "t-bear", 5, "喜欢",
                                actor_id="r-parent", on_behalf=True,
                                at=T("2026-09-01T10:00+08:00"))
        # 家长不能越过孩子的撤回
        with self.assertRaises(DomainError):
            svc.grant_consent("c-kid-fb-2", "r-kid", "feedback",
                              actor_id="r-parent", on_behalf=True,
                              at=T("2026-09-01T10:05+08:00"))
        # 孩子本人重新授权即可恢复
        svc.grant_consent("c-kid-fb-3", "r-kid", "feedback",
                          actor_id="r-kid", at=T("2026-09-01T10:10+08:00"))
        svc.record_feedback("f1", "fam-1", "r-kid", "t-bear", 5, "喜欢",
                            actor_id="r-kid", at=T("2026-09-01T10:15+08:00"))

    def test_stranger_cannot_act_for_child(self):
        svc = build_world()
        svc.register_reader("r-stranger", "fam-9", "路人", "1990-01-01", [],
                            at=T("2026-08-02T12:00+08:00"))
        with self.assertRaises(DomainError):
            svc.grant_consent("cx", "r-kid", "feedback",
                              actor_id="r-stranger", on_behalf=True)


class TestInventoryAndIdempotency(unittest.TestCase):
    def test_inventory_never_negative_and_returns_restock(self):
        svc = build_world()
        svc.add_copies("b10", "store-wh", "e-space", 1, at=T("2026-08-04T09:00+08:00"))
        svc.request_loan("req-1", "r-parent", "e-space", "store-wh",
                         at=T("2026-08-04T10:00+08:00"))
        self.assertEqual(svc.inventory[("store-wh", "e-space")], 0)
        with self.assertRaises(DomainError):
            svc.request_loan("req-2", "r-parent", "e-space", "store-wh",
                             at=T("2026-08-04T10:01+08:00"))

    def test_duplicate_scan_and_offline_replay_do_not_double_charge(self):
        svc = build_world()
        svc.add_copies("b11", "store-wh", "e-space", 2, at=T("2026-08-04T09:00+08:00"))
        first = svc.request_loan("req-dup", "r-parent", "e-space", "store-wh",
                                 at=T("2026-08-04T10:00+08:00"))
        # 同一 request_id：店员重复扫码
        second = svc.request_loan("req-dup", "r-parent", "e-space", "store-wh",
                                  at=T("2026-08-04T10:05+08:00"))
        # 断网补传：把事件流折叠到新实例后再补一次
        svc2 = ReadingService(svc.events)
        third = svc2.request_loan("req-dup", "r-parent", "e-space", "store-wh",
                                  at=T("2026-08-04T10:10+08:00"))
        self.assertEqual([e["event_id"] for e in first],
                         [e["event_id"] for e in second])
        self.assertEqual([e["event_id"] for e in first],
                         [e["event_id"] for e in third])
        self.assertEqual(svc.inventory[("store-wh", "e-space")], 1)
        self.assertEqual(svc2.inventory[("store-wh", "e-space")], 1)
        self.assertEqual(len(svc.loans), 1)
        # 借阅权益（次数）也只记一次
        used = [b for b in svc.benefits if b["benefit"] == "loan"]
        self.assertEqual(len(used), 1)

    def test_duplicate_add_copies_batch_rejected(self):
        svc = build_world()
        svc.add_copies("b12", "store-wh", "e-space", 1, at=T("2026-08-04T09:00+08:00"))
        with self.assertRaises(DomainError):
            svc.add_copies("b12", "store-wh", "e-space", 1,
                           at=T("2026-08-04T09:01+08:00"))

    def test_event_registration_idempotent(self):
        svc = build_world()
        svc.schedule_author_event(
            "ev-1", "store-wh", "与《星星信使》对谈", "某某",
            T("2026-09-20T14:00+08:00"), capacity=1,
            at=T("2026-09-01T09:00+08:00"))
        svc.register_for_event("evreq-1", "ev-1", "r-parent",
                               at=T("2026-09-02T09:00+08:00"))
        replay = svc.register_for_event("evreq-1", "ev-1", "r-parent",
                                        at=T("2026-09-02T09:05+08:00"))
        self.assertEqual(len(replay), 1)
        roster = rm.event_roster(svc, "ev-1")
        self.assertEqual(roster["registered_count"], 1)


class TestCrossStoreLoan(unittest.TestCase):
    def test_cross_store_transit_then_ready_and_overdue(self):
        svc = build_world()
        # 只有北京有《小熊的森林》，武汉家庭跨店借
        svc.add_copies("b20", "store-bj", "e-bear", 1, at=T("2026-08-04T09:00+08:00"))
        produced = svc.request_loan(
            "req-cross", "r-kid", "e-bear", "store-wh",
            actor_id="r-parent", on_behalf=True, at=T("2026-08-05T10:00+08:00"))
        loan_id = "loan-req-cross"
        view = rm.loan_view(svc, loan_id, date(2026, 8, 5))
        self.assertTrue(view["cross_store"])
        self.assertEqual(view["status"], "in_transit")
        self.assertEqual(view["status_label"], "跨店在途")
        # 武汉门店可借视图显示“在途 incoming”，北京在架归零
        wh = {r["edition_id"]: r for r in rm.store_availability(svc, "store-wh")}
        self.assertEqual(wh["e-bear"]["incoming_in_transit"], 1)
        self.assertEqual(wh["e-bear"]["available_now"], 0)

        svc.mark_loan_arrived(loan_id, at=T("2026-08-08T11:00+08:00"))
        picked = svc.pick_up(loan_id, at=T("2026-08-09T11:00+08:00"))
        self.assertTrue(any(e["kind"] == "LOAN_PICKED_UP" for e in picked))
        # 未到期
        self.assertFalse(rm.loan_view(svc, loan_id, date(2026, 8, 20))["overdue"])
        # 逾期（21 天应还 8/30）
        self.assertTrue(rm.loan_view(svc, loan_id, date(2026, 9, 2))["overdue"])

        # 还到武汉：武汉库存 +1
        svc.schedule_return(loan_id, "store-wh", T("2026-09-01T11:00+08:00"),
                            at=T("2026-08-20T09:00+08:00"))
        svc.return_loan(loan_id, "store-wh", at=T("2026-09-01T11:00+08:00"))
        self.assertEqual(svc.inventory[("store-wh", "e-bear")], 1)
        self.assertEqual(rm.loan_view(svc, loan_id, date(2026, 9, 1))["status"], "returned")


class TestClosure(unittest.TestCase):
    def test_closure_only_reschedules_affected_appointments(self):
        svc = build_world()
        svc.add_copies("b30", "store-wh", "e-space", 1, at=T("2026-08-04T09:00+08:00"))
        # 武汉取书预约落在 9/15
        svc.request_loan("req-close", "r-parent", "e-space", "store-wh",
                         pickup_at=T("2026-09-15T10:00+08:00"),
                         at=T("2026-09-01T09:00+08:00"))
        loan_id = "loan-req-close"

        # 武汉店 9/14–9/16 临时闭店（书尚未取）：取书改到长沙店、闭店结束后
        svc.schedule_closure(
            "cl-1", "store-wh", T("2026-09-14T00:00+08:00"),
            T("2026-09-16T22:00+08:00"), "场馆检修",
            alternate_store_id="store-cs", at=T("2026-09-05T09:00+08:00"))

        pickup = rm.loan_view(svc, loan_id, date(2026, 9, 5))["pickup_appointment"]
        self.assertEqual(pickup["store_id"], "store-cs")
        self.assertEqual(pickup["scheduled_at"], "2026-09-16T22:00:00+08:00")
        self.assertEqual(pickup["rescheduled_for_closure"], "cl-1")
        # 借阅的取书店也同步为长沙——家庭与门店同源一致
        self.assertEqual(svc.loans[loan_id].pickup_store_id, "store-cs")

        # 闭店结束后按新安排到长沙店取书，再约 9/25 回武汉还书（在闭店窗口外，不受影响）
        svc.pick_up(loan_id, at=T("2026-09-17T10:00+08:00"))
        svc.schedule_return(loan_id, "store-wh", T("2026-09-25T10:00+08:00"),
                            at=T("2026-09-17T11:00+08:00"))
        ret = rm.loan_view(svc, loan_id, date(2026, 9, 17))["return_appointment"]
        self.assertEqual(ret["store_id"], "store-wh")
        self.assertEqual(ret["scheduled_at"], "2026-09-25T10:00:00+08:00")
        # 门店仪表盘能列出受影响安排
        dash = rm.store_dashboard(svc, "store-wh", date(2026, 9, 5))
        self.assertEqual(len(dash["affected_appointments"]), 1)

    def test_closure_does_not_touch_other_stores(self):
        svc = build_world()
        svc.add_copies("b31", "store-bj", "e-space", 1, at=T("2026-08-04T09:00+08:00"))
        svc.request_loan("req-bj", "r-parent", "e-space", "store-bj",
                         pickup_at=T("2026-09-15T10:00+08:00"),
                         at=T("2026-09-01T09:00+08:00"))
        svc.schedule_closure(
            "cl-2", "store-wh", T("2026-09-14T00:00+08:00"),
            T("2026-09-16T22:00+08:00"), "检修",
            at=T("2026-09-05T09:00+08:00"))
        appt = rm.loan_view(svc, "loan-req-bj", date(2026, 9, 5))["pickup_appointment"]
        self.assertIsNone(appt["rescheduled_for_closure"])
        self.assertEqual(appt["store_id"], "store-bj")


class TestRecommendationRevision(unittest.TestCase):
    def test_rationale_reproducible_after_description_update(self):
        svc = build_world()
        rec = svc.recommend_title(
            "rec-1", "r-kid", "t-space", "对宇宙感兴趣，适龄且有作家对谈",
            "librarian:event=ev-wh", at=T("2026-09-10T10:00+08:00"))
        frozen_rev = rec["title_description_revision"]
        self.assertEqual(frozen_rev, 1)
        # 事后书目说明更新
        svc.update_title_description(
            "t-space", "修订说明：新版增加了木星章节。", "增补木星章节",
            at=T("2026-09-12T10:00+08:00"))
        restored = rm.recommendation_as_made(svc, "rec-1")
        self.assertTrue(restored["summary_changed_since"])
        self.assertEqual(restored["summary_then"], "初版说明：一场飞向土星的信。")
        self.assertEqual(restored["current_revision"], 2)
        self.assertEqual(restored["frozen_revision"], 1)
        # 家庭当前目录看到的是新说明，推荐回放看到的是旧说明
        cat_now = next(r for r in rm.visible_catalog(svc, "r-kid", date(2026, 9, 13))
                       if r["title_id"] == "t-space")
        self.assertEqual(cat_now["summary"], "修订说明：新版增加了木星章节。")


class TestEventWaitlistAndMembership(unittest.TestCase):
    def _ev(self, svc, cap: int = 1):
        svc.schedule_author_event(
            "ev-w", "store-wh", "作家对谈", "某某",
            T("2026-09-20T14:00+08:00"), capacity=cap,
            at=T("2026-09-01T09:00+08:00"))

    def test_registration_waitlist_promotion_separate_counts(self):
        svc = build_world()
        svc.register_reader("r-kid2", "fam-2", "朵朵", "2017-02-02", ["宇宙"],
                            at=T("2026-08-02T11:00+08:00"))
        svc.grant_consent("c2-event", "r-kid2", "event",
                          at=T("2026-08-02T11:05+08:00"))
        self._ev(svc, cap=1)
        svc.register_for_event("er-1", "ev-w", "r-parent",
                               at=T("2026-09-02T09:00+08:00"))
        svc.register_for_event("er-2", "ev-w", "r-kid",
                               actor_id="r-parent", on_behalf=True,
                               at=T("2026-09-02T09:01+08:00"))
        roster = rm.event_roster(svc, "ev-w")
        self.assertEqual(roster["registered_count"], 1)
        self.assertEqual(roster["waitlist_count"], 1)
        self.assertEqual(roster["seats_left"], 0)
        # 取消报名 → 候补自动递补
        svc.cancel_event_registration("ev-w", "r-parent",
                                      at=T("2026-09-03T09:00+08:00"))
        roster2 = rm.event_roster(svc, "ev-w")
        self.assertEqual(roster2["registered_count"], 1)
        self.assertEqual(roster2["waitlist_count"], 0)
        promoted = roster2["registered"][0]
        self.assertTrue(promoted["from_waitlist"])
        self.assertEqual(promoted["reader_id"], "r-kid")

    def test_membership_quota_independent_from_event_seats(self):
        svc = build_world()
        svc.add_copies("b40", "store-wh", "e-space", 10,
                       at=T("2026-08-04T09:00+08:00"))
        # 会员把借阅名额提到 8；活动座位仍是 1，互不影响
        svc.grant_membership("r-parent", "家庭年卡", 8,
                             "2026-09-01", "2027-08-31",
                             at=T("2026-09-01T08:00+08:00"))
        self._ev(svc, cap=1)
        for i in range(8):
            svc.request_loan(f"req-q{i}", "r-parent", "e-space", "store-wh",
                             at=T(f"2026-09-02T10:0{i}+08:00"))
        self.assertEqual(svc._active_loan_count("r-parent"), 8)
        with self.assertRaises(DomainError):
            svc.request_loan("req-q9", "r-parent", "e-space", "store-wh",
                             at=T("2026-09-02T11:00+08:00"))
        # 权益使用记录 8 条 loan，与活动报名计数分开
        self.assertEqual(
            len([b for b in svc.benefits if b["reader_id"] == "r-parent"
                 and b["benefit"] == "loan"]), 8)
        svc.register_for_event("er-m", "ev-w", "r-parent",
                               at=T("2026-09-03T09:00+08:00"))
        roster = rm.event_roster(svc, "ev-w")
        self.assertEqual(roster["registered_count"], 1)
        self.assertEqual(roster["seats_left"], 0)


class TestReadingGuideFairness(unittest.TestCase):
    def test_spending_basis_rejected(self):
        svc = build_world()
        with self.assertRaises(DomainError):
            svc.assign_reading_guide("g1", "r-kid", "亲子共读方法", "spend_amount",
                                     at=T("2026-09-01T09:00+08:00"))
        with self.assertRaises(DomainError):
            svc.assign_reading_guide("g2", "r-kid", "亲子共读方法", "vip_paid_tier",
                                     at=T("2026-09-01T09:00+08:00"))

    def test_guide_allowed_on_non_spending_basis(self):
        svc = build_world()
        ev = svc.assign_reading_guide("g3", "r-kid", "宇宙主题延伸阅读", "interest",
                                      at=T("2026-09-01T09:00+08:00"))
        self.assertEqual(ev["kind"], "READING_GUIDE_ASSIGNED")
        svc.assign_reading_guide("g4", "r-kid", "对谈后阅读规划", "author_event",
                                 at=T("2026-09-21T09:00+08:00"))

    def test_guide_requires_consent(self):
        svc = build_world()
        svc.register_reader("r-noguide", "fam-3", "阿星", "2018-01-01", ["宇宙"],
                            at=T("2026-08-02T12:00+08:00"))
        with self.assertRaises(DomainError):
            svc.assign_reading_guide("g5", "r-noguide", "x", "interest")


class TestTraceability(unittest.TestCase):
    def test_trace_from_event_to_loans_and_feedback(self):
        svc = build_world()
        svc.schedule_author_event(
            "ev-trace", "store-wh", "《星星信使》武汉对谈", "某某",
            T("2026-09-18T14:00+08:00"), capacity=10,
            at=T("2026-09-01T09:00+08:00"))
        svc.register_for_event("etr-1", "ev-trace", "r-kid",
                               actor_id="r-parent", on_behalf=True,
                               at=T("2026-09-02T09:00+08:00"))
        svc.record_attendance("ev-trace", "r-kid",
                              at=T("2026-09-18T15:30+08:00"))
        # 对谈后产生推荐、指导与借阅
        svc.recommend_title("rec-tr", "r-kid", "t-space",
                            "对谈后孩子想继续读宇宙主题", "author_event:ev-trace",
                            at=T("2026-09-18T16:00+08:00"))
        svc.assign_reading_guide("g-tr", "r-kid", "宇宙主题共读四周计划",
                                 "author_event", at=T("2026-09-18T16:05+08:00"))
        svc.add_copies("b50", "store-wh", "e-space", 2,
                       at=T("2026-09-17T09:00+08:00"))
        svc.request_loan("req-tr", "r-kid", "e-space", "store-wh",
                         actor_id="r-parent", on_behalf=True,
                         at=T("2026-09-19T10:00+08:00"))
        svc.record_feedback(
            "fb-tr", "fam-1", "r-kid", "t-space", 5, "对谈后读得停不下来",
            loan_id="loan-req-tr", author_event_id="ev-trace",
            actor_id="r-parent", on_behalf=True,
            at=T("2026-09-22T10:00+08:00"))

        trace = rm.trace_event_followup(svc, "ev-trace", date(2026, 9, 22))
        self.assertEqual(trace["attended"], ["r-kid"])
        self.assertEqual(len(trace["loans_after_event"]), 1)
        self.assertEqual(trace["loans_after_event"][0]["loan_id"], "loan-req-tr")
        self.assertEqual(len(trace["family_feedback"]), 1)
        self.assertEqual(trace["family_feedback"][0]["feedback_id"], "fb-tr")
        self.assertEqual(len(trace["guides_from_event"]), 1)


class TestEventReplayAndSchemaSync(unittest.TestCase):
    def test_replay_produces_equivalent_state(self):
        svc = build_world()
        svc.add_copies("b60", "store-bj", "e-bear", 1,
                       at=T("2026-08-04T09:00+08:00"))
        svc.request_loan("req-r", "r-kid", "e-bear", "store-wh",
                         actor_id="r-parent", on_behalf=True,
                         at=T("2026-08-05T10:00+08:00"))
        svc2 = ReadingService(list(svc.events))
        self.assertEqual(svc2.inventory, svc.inventory)
        self.assertEqual(set(svc2.loans), set(svc.loans))
        self.assertIn("req-r", svc2.requests)

    def test_schema_enum_matches_event_catalog(self):
        schema = json.loads(
            (ROOT / "contracts" / "domain-events.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["properties"]["kind"]["enum"]),
                         set(PAYLOAD_REQUIRED))

    def test_envelope_contract_still_valid(self):
        data = json.loads((ROOT / "fixtures" / "event.json").read_text(encoding="utf-8"))
        from src.contract import validate
        self.assertEqual(validate(data), [])

    def test_end_to_end_fixture_events_are_well_formed_and_replayable(self):
        from src.contract import validate as validate_envelope
        data = json.loads((ROOT / "fixtures" / "family-reading-flow.json")
                          .read_text(encoding="utf-8"))
        for event in data["events"]:
            self.assertEqual(validate_envelope(event), [])
            for field_name in PAYLOAD_REQUIRED[event["kind"]]:
                self.assertIn(field_name, event,
                              f"{event['kind']} 缺载荷字段 {field_name}")
        # 样例事件流必须能完整重放
        svc = ReadingService(data["events"])
        self.assertEqual(len(svc.events), len(data["events"]))
        self.assertIn("loan-req-cross", svc.loans)


if __name__ == "__main__":
    unittest.main()
