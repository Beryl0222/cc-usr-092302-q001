import json
import unittest
from datetime import date, datetime
from pathlib import Path

from src import rules
from src.rules import (
    BenefitLedger,
    IdempotencyLog,
    ITEM_AVAILABLE,
    ITEM_AT_HOLDING,
    ITEM_IN_TRANSIT,
    ITEM_ON_LOAN,
    ITEM_RESERVED,
    Rejection,
)

ROOT = Path(__file__).parents[1]


def load_relay() -> list[dict]:
    data = json.loads((ROOT / "fixtures" / "relay.json").read_text(encoding="utf-8"))
    return data["events"]


def find(events, kind, **where):
    for ev in events:
        if ev["kind"] == kind and all(ev.get("payload", {}).get(k) == v for k, v in where.items()):
            return ev
    raise KeyError(kind)


class AgeAndChildViewTest(unittest.TestCase):
    def test_age_computed_dynamically(self):
        # 2016-04-10 出生；2026-09-22 时 10 岁
        self.assertEqual(rules.age_on("2016-04-10", date(2026, 9, 22)), 10)
        # 生日前一天仍为 9 岁
        self.assertEqual(rules.age_on("2016-04-10", date(2026, 4, 9)), 9)

    def test_child_view_filters_age_and_self_withdrawn(self):
        entries = [
            {"edition_id": "ok", "min_age": 8, "max_age": 12},
            {"edition_id": "too-young", "min_age": 3, "max_age": 6},
            {"edition_id": "self-withdrawn", "min_age": 8, "max_age": 12,
             "withdrawn_by_relation": "self"},
            {"edition_id": "guardian-withdrawn", "min_age": 8, "max_age": 12,
             "withdrawn_by_relation": "guardian"},
        ]
        visible = rules.child_view(entries, "2016-04-10", date(2026, 9, 22))
        ids = [e["edition_id"] for e in visible]
        self.assertEqual(ids, ["ok"])


class ConsentAndGuardianshipTest(unittest.TestCase):
    def test_active_consent_basic(self):
        events = [{
            "kind": "CONSENT_GRANTED",
            "occurred_at": "2026-09-01T10:00:00+08:00",
            "payload": {
                "person_id": "c", "granted_by": "g", "scopes": ["guidance"],
                "valid_from": "2026-09-01T10:00:00+08:00",
                "valid_to": "2027-09-01T10:00:00+08:00",
            },
        }]
        at = datetime.fromisoformat("2026-09-10T10:00:00+08:00")
        self.assertTrue(rules.consent_active(events, "c", "guidance", at))

    def test_child_self_withdraw_blocks_guardian_regrant(self):
        base = "2026-09-{:02d}T10:00:00+08:00"
        events = [
            {"kind": "CONSENT_GRANTED", "occurred_at": base.format(1),
             "payload": {"person_id": "c", "granted_by": "g", "scopes": ["recommendation"],
                         "valid_from": base.format(1), "valid_to": "2027-01-01T00:00:00+08:00"}},
            # 孩子本人撤回
            {"kind": "CONSENT_WITHDRAWN", "occurred_at": base.format(5),
             "payload": {"person_id": "c", "scopes": ["recommendation"],
                         "withdrawn_by_relation": "self", "withdrawn_by": "c"}},
            # 家长试图重新授权 —— 不能越过孩子的撤回
            {"kind": "CONSENT_GRANTED", "occurred_at": base.format(6),
             "payload": {"person_id": "c", "granted_by": "g", "scopes": ["recommendation"],
                         "valid_from": base.format(6), "valid_to": "2027-01-01T00:00:00+08:00"}},
        ]
        at = datetime.fromisoformat("2026-09-10T10:00:00+08:00")
        self.assertFalse(rules.consent_active(events, "c", "recommendation", at))

        # 孩子本人重新授予可以恢复
        events.append(
            {"kind": "CONSENT_GRANTED", "occurred_at": base.format(7),
             "payload": {"person_id": "c", "granted_by": "c", "scopes": ["recommendation"],
                         "valid_from": base.format(7), "valid_to": "2027-01-01T00:00:00+08:00"}}
        )
        self.assertTrue(rules.consent_active(events, "c", "recommendation", at))

    def test_guardian_withdraw_can_be_regranted(self):
        base = "2026-09-{:02d}T10:00:00+08:00"
        events = [
            {"kind": "CONSENT_GRANTED", "occurred_at": base.format(1),
             "payload": {"person_id": "c", "granted_by": "g", "scopes": ["guidance"],
                         "valid_from": base.format(1), "valid_to": "2027-01-01T00:00:00+08:00"}},
            {"kind": "CONSENT_WITHDRAWN", "occurred_at": base.format(5),
             "payload": {"person_id": "c", "scopes": ["guidance"],
                         "withdrawn_by_relation": "guardian", "withdrawn_by": "g"}},
            {"kind": "CONSENT_GRANTED", "occurred_at": base.format(6),
             "payload": {"person_id": "c", "granted_by": "g", "scopes": ["guidance"],
                         "valid_from": base.format(6), "valid_to": "2027-01-01T00:00:00+08:00"}},
        ]
        at = datetime.fromisoformat("2026-09-10T10:00:00+08:00")
        self.assertTrue(rules.consent_active(events, "c", "guidance", at))

    def test_act_on_behalf_requires_guardianship(self):
        at = datetime.fromisoformat("2026-09-23T20:50:00+08:00")
        events = load_relay()
        # 样例中监护授权有效：代办允许
        rules.assert_actor_allowed(
            events, actor_id="person-zl-001",
            on_behalf_of="person-lxm-001", scope="recommendation", at=at,
        )
        with self.assertRaises(Rejection):
            rules.assert_actor_allowed(
                events, actor_id="stranger", on_behalf_of="person-lxm-001",
                scope="recommendation", at=at,
            )


class PreferenceTest(unittest.TestCase):
    def test_child_withdrawn_preference_not_readded_by_guardian(self):
        events = [
            {"kind": "PREFERENCE_DECLARED", "actor_id": "g",
             "payload": {"person_id": "c", "interests": ["江河", "奇幻"]}},
            {"kind": "PREFERENCE_WITHDRAWN", "actor_id": "c",
             "payload": {"person_id": "c", "interests": ["奇幻"], "withdrawn_by": "c"}},
            # 家长再次代录孩子已撤回的兴趣
            {"kind": "PREFERENCE_DECLARED", "actor_id": "g",
             "payload": {"person_id": "c", "interests": ["奇幻"]}},
            # 孩子自己重新声明则恢复
            {"kind": "PREFERENCE_DECLARED", "actor_id": "c",
             "payload": {"person_id": "c", "interests": ["奇幻"]}},
        ]
        self.assertEqual(rules.active_interests(events, "c"), {"江河", "奇幻"})


class RecommendationSnapshotTest(unittest.TestCase):
    def test_snapshot_reproduces_reason_as_issued_after_edition_update(self):
        events = load_relay()
        rec = find(events, "RECOMMENDATION_MADE")
        snapshot = rules.recommendation_as_issued(rec)
        update = find(events, "EDITION_UPDATED")

        # 当前书目说明已变（v2），但快照逐字保持推荐当时（v1）
        self.assertNotEqual(snapshot["description"], update["payload"]["note"])
        self.assertEqual(
            snapshot["description"],
            "女孩在江边长堤捡到一封没有收件人的信，顺着江声寻找寄信人的夏天。",
        )
        self.assertEqual(snapshot["basis_versions"]["catalog_note"], "1")
        self.assertEqual(snapshot["age_gating"]["min_age"], 8)

        with self.assertRaises(Rejection):
            rules.recommendation_as_issued(update)


class IdempotencyTest(unittest.TestCase):
    def test_duplicate_request_admitted_once(self):
        log = IdempotencyLog()
        ev1 = {"event_id": "e1", "request_id": "req-1"}
        ev2 = {"event_id": "e1-dup", "request_id": "req-1"}  # 断网补传/重复扫码
        self.assertTrue(log.admit(ev1))
        self.assertFalse(log.admit(ev2))
        self.assertEqual(log.first_event_id("req-1"), "e1")

    def test_system_events_without_request_id_are_separate(self):
        log = IdempotencyLog()
        self.assertTrue(log.admit({"event_id": "s1"}))
        self.assertTrue(log.admit({"event_id": "s2"}))


class LoanStateMachineTest(unittest.TestCase):
    def test_relay_fixture_item_ends_available_and_counted_once(self):
        events = load_relay()
        items = rules.fold_items(events)
        item = items["item-wh-01-00077"]
        self.assertEqual(item.state, ITEM_AVAILABLE)
        self.assertEqual(item.loans_picked_up, 1)
        self.assertEqual(item.current_loan_id, None)
        self.assertEqual(item.rejected, [])

    def test_duplicate_pickup_does_not_double_deduct(self):
        events = [
            {"kind": "ITEM_RECORDED", "event_id": "i", "payload": {"item_id": "x"}},
            {"kind": "LOAN_PICKED_UP", "event_id": "p1", "request_id": "r1",
             "payload": {"loan_id": "L1", "item_id": "x", "due_at": "2026-10-01T00:00:00+08:00"}},
            # 同一 request_id 的补传：拒绝为重复，不扣第二次
            {"kind": "LOAN_PICKED_UP", "event_id": "p2", "request_id": "r1",
             "payload": {"loan_id": "L1", "item_id": "x", "due_at": "2026-10-01T00:00:00+08:00"}},
        ]
        items = rules.fold_items(events)
        self.assertEqual(items["x"].state, ITEM_ON_LOAN)
        self.assertEqual(items["x"].loans_picked_up, 1)
        self.assertEqual([r["reason"] for r in items["x"].rejected], ["duplicate_request"])

    def test_in_transit_item_cannot_be_lent_again(self):
        events = [
            {"kind": "ITEM_RECORDED", "payload": {"item_id": "x"}},
            {"kind": "LOAN_PICKED_UP", "event_id": "p1", "request_id": "r1",
             "payload": {"loan_id": "L1", "item_id": "x"}},
            {"kind": "LOAN_IN_TRANSIT", "event_id": "t1", "request_id": "r2",
             "payload": {"loan_id": "L1", "item_id": "x",
                         "from_store_id": "s1", "to_store_id": "s2"}},
            # 另一门店在途期间尝试借出 —— 非法迁移
            {"kind": "LOAN_PICKED_UP", "event_id": "p9", "request_id": "r9",
             "payload": {"loan_id": "L9", "item_id": "x"}},
        ]
        items = rules.fold_items(events)
        self.assertEqual(items["x"].state, ITEM_IN_TRANSIT)
        self.assertEqual([r["reason"] for r in items["x"].rejected], ["illegal_transition"])
        self.assertEqual(items["x"].loans_picked_up, 1)

    def test_cross_store_returns_flow_transit_to_available(self):
        events = [
            {"kind": "ITEM_RECORDED", "payload": {"item_id": "x"}},
            {"kind": "LOAN_PICKED_UP", "request_id": "r1",
             "payload": {"loan_id": "L1", "item_id": "x"}},
            {"kind": "LOAN_IN_TRANSIT", "request_id": "r2",
             "payload": {"loan_id": "L1", "item_id": "x", "from_store_id": "s2", "to_store_id": "s1"}},
            {"kind": "LOAN_ARRIVED", "request_id": "r3",
             "payload": {"loan_id": "L1", "item_id": "x", "store_id": "s1"}},
            {"kind": "LOAN_RETURNED", "request_id": "r4",
             "payload": {"loan_id": "L1", "item_id": "x", "return_store_id": "s1"}},
        ]
        states = []
        # 逐步折叠检查中间态
        for n in range(1, len(events) + 1):
            states.append(rules.fold_items(events[:n])["x"].state)
        self.assertEqual(
            states,
            [ITEM_AVAILABLE, ITEM_ON_LOAN, ITEM_IN_TRANSIT, ITEM_AT_HOLDING, ITEM_AVAILABLE],
        )

    def test_overdue_marking_idempotent(self):
        events = [
            {"kind": "ITEM_RECORDED", "payload": {"item_id": "x"}},
            {"kind": "LOAN_PICKED_UP", "request_id": "r1",
             "payload": {"loan_id": "L1", "item_id": "x"}},
            {"kind": "LOAN_OVERDUE_MARKED", "payload": {"loan_id": "L1"}},
            {"kind": "LOAN_OVERDUE_MARKED", "payload": {"loan_id": "L1"}},
        ]
        items = rules.fold_items(events)
        self.assertEqual(items["x"].overdue_loans, {"L1"})
        self.assertEqual(items["x"].state, ITEM_ON_LOAN)


class ClosureImpactTest(unittest.TestCase):
    def test_only_overlapping_open_plans_at_closed_store_are_affected(self):
        closure = {
            "store_id": "s1",
            "closed_from": "2026-09-24T09:00:00+08:00",
            "closed_to": "2026-09-25T09:00:00+08:00",
            "alternative_store_id": "s2",
        }
        plans = [
            {"loan_id": "hit", "kind": "pickup", "store_id": "s1",
             "planned_at": datetime.fromisoformat("2026-09-24T14:00:00+08:00")},
            # 闭店区间之后：不受影响
            {"loan_id": "after", "kind": "return", "store_id": "s1",
             "planned_at": datetime.fromisoformat("2026-09-25T10:00:00+08:00")},
            # 其他门店：不受影响
            {"loan_id": "other-store", "kind": "pickup", "store_id": "s2",
             "planned_at": datetime.fromisoformat("2026-09-24T14:00:00+08:00")},
            # 已完成：不受影响
            {"loan_id": "done", "kind": "return", "store_id": "s1", "status": "done",
             "planned_at": datetime.fromisoformat("2026-09-24T08:00:00+08:00")},
        ]
        affected = rules.plans_affected_by_closure(closure, plans)
        self.assertEqual([p["loan_id"] for p in affected], ["hit"])

    def test_relay_fixture_reschedule_matches_closure_rule(self):
        events = load_relay()
        closure = find(events, "STORE_TEMPORARILY_CLOSED")["payload"]
        reserve = find(events, "LOAN_RESERVED")["payload"]
        plan = {
            "loan_id": reserve["loan_id"], "kind": "pickup",
            "store_id": reserve["pickup_plan"]["store_id"],
            "planned_at": reserve["pickup_plan"]["planned_at"],
        }
        affected = rules.plans_affected_by_closure(closure, [plan])
        self.assertEqual(len(affected), 1)
        re = find(events, "LOAN_PLAN_RESCHEDULED")["payload"]
        self.assertEqual(re["new_store_id"], "store-wh-02")


class RegistrationAndBenefitsTest(unittest.TestCase):
    EVID = "evt-wh-shenzhixing-0923"

    def test_full_event_goes_to_waitlist(self):
        events = []
        for i in range(3):
            events.append({
                "kind": "EVENT_REGISTERED", "request_id": f"r{i}",
                "payload": {"author_event_id": "E", "reader_id": f"u{i}"},
            })
        state = rules.fold_event(events, "E")
        self.assertEqual(rules.decide_registration(state, capacity=3), "EVENT_WAITLISTED")
        self.assertEqual(rules.decide_registration(state, capacity=4), "EVENT_REGISTERED")

    def test_cancel_releases_seat_promotion_removes_waitlist(self):
        events = [
            {"kind": "EVENT_REGISTERED", "request_id": "r1",
             "payload": {"author_event_id": "E", "reader_id": "u1"}},
            {"kind": "EVENT_WAITLISTED", "request_id": "w1",
             "payload": {"author_event_id": "E", "reader_id": "u2", "position": 1}},
            {"kind": "EVENT_REGISTRATION_CANCELLED", "request_id": "c1",
             "payload": {"author_event_id": "E", "reader_id": "u1"}},
            {"kind": "EVENT_REGISTRATION_PROMOTED", "request_id": "p1",
             "payload": {"author_event_id": "E", "reader_id": "u2", "position": 1}},
        ]
        state = rules.fold_event(events, "E")
        self.assertEqual(state.seats_taken, 0)
        self.assertEqual(state.waitlist, [])
        self.assertIn("u2", state.promoted)

    def test_benefits_are_separate_from_registration_and_loans(self):
        events = load_relay()
        ledger = rules.fold_benefits(events)
        # 权益账只记了一次优先报名，与报名计数、借阅次数互不影响
        self.assertEqual(ledger.net_used("priority_registration"), 1)
        state = rules.fold_event(events, self.EVID)
        self.assertEqual(state.seats_taken, 1)
        item = rules.fold_items(events)["item-wh-01-00077"]
        self.assertEqual(item.loans_picked_up, 1)

        # 取消报名 + 权益返还：报名账释放，权益账归零，互不串账
        more = events + [
            {"kind": "EVENT_REGISTRATION_CANCELLED", "request_id": "cx",
             "payload": {"author_event_id": self.EVID, "reader_id": "person-lxm-001"}},
            {"kind": "BENEFIT_RESTORED",
             "payload": {"reader_id": "person-lxm-001", "benefit_type": "priority_registration",
                         "ref": f"{self.EVID}::person-lxm-001", "reason": "cancel"}},
        ]
        self.assertEqual(rules.fold_event(more, self.EVID).seats_taken, 0)
        self.assertEqual(rules.fold_benefits(more).net_used("priority_registration"), 0)
        # 借阅次数不被活动取消影响
        self.assertEqual(rules.fold_items(more)["item-wh-01-00077"].loans_picked_up, 1)


class GuidanceEligibilityTest(unittest.TestCase):
    def test_allows_with_qualified_guide_and_consent(self):
        rules.assert_guidance_allowed(
            guide_qualified=True, consent_scope_active=True, age_appropriate=True
        )

    def test_rejects_each_missing_prong(self):
        kwargs = dict(guide_qualified=True, consent_scope_active=True, age_appropriate=True)
        for missing in ("guide_qualified", "consent_scope_active", "age_appropriate"):
            bad = dict(kwargs)
            bad[missing] = False
            with self.assertRaises(Rejection):
                rules.assert_guidance_allowed(**bad)

    def test_spend_factor_is_contract_level_rejection(self):
        for spend_key in ("spend_amount", "loyalty_points", "recharge_amount"):
            with self.assertRaises(Rejection):
                rules.assert_guidance_allowed(
                    guide_qualified=True, consent_scope_active=True,
                    age_appropriate=True, factors={spend_key: 9999},
                )


class RelayTraceTest(unittest.TestCase):
    CORR = "evt-wh-shenzhixing-0923"

    def test_trace_follows_talk_to_feedback(self):
        events = load_relay()
        chain = rules.trace_relay(events, self.CORR)
        kinds = [n["kind"] for n in chain]
        self.assertEqual(kinds[0], "EVENT_ATTENDED")
        self.assertIn("RECOMMENDATION_MADE", kinds)
        self.assertIn("LOAN_PICKED_UP", kinds)
        self.assertIn("LOAN_RETURNED", kinds)
        self.assertIn("GUIDANCE_SESSION_HELD", kinds)
        # 家庭反馈是接力链的最后一个业务阶段（归还在时间上可能更晚，但阶段不高于反馈）
        feedback_nodes = [n for n in chain if n["kind"] == "FAMILY_FEEDBACK_RECORDED"]
        self.assertEqual(len(feedback_nodes), 1)
        self.assertTrue(all(n["stage"] <= 4 for n in chain))
        # 时间有序
        times = [n["occurred_at"] for n in chain]
        self.assertEqual(times, sorted(times))

    def test_chain_is_linked_by_causation(self):
        events = load_relay()
        edges = rules.relay_edges(events, self.CORR)
        edge_from = {a for a, _ in edges}
        # 出席 -> 推荐 -> 预约 ... 关键前因都存在
        self.assertIn("e-0506", edge_from)  # 出席触发推荐
        self.assertIn("e-0401", edge_from)  # 推荐触发预约
        self.assertIn("e-0604", edge_from)  # 借出触发在途/指导

    def test_loan_traceable_back_to_edition_and_event(self):
        events = load_relay()
        chain = rules.trace_relay(events, self.CORR)
        picked = next(n for n in chain if n["kind"] == "LOAN_PICKED_UP")
        self.assertEqual(picked["ref"], "item-wh-01-00077")
        rec = next(n for n in chain if n["kind"] == "RECOMMENDATION_MADE")
        self.assertEqual(rec["ref"], "ed-riverlight-01-cn")


if __name__ == "__main__":
    unittest.main()
