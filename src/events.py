"""领域事件目录。

所有状态变化都以追加事件表达；事件信封沿用基线合同
(event_id/kind/occurred_at/subject_id/version)，载荷字段扁平拼接。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

ENVELOPE_REQUIRED = ("event_id", "kind", "occurred_at", "subject_id", "version")
SUBJECT = "family-reading-continuity"

# 每种事件必需的载荷字段（信封字段除外）。
PAYLOAD_REQUIRED: dict[str, tuple[str, ...]] = {
    # —— 读者、监护关系与隐私授权 ——
    "READER_REGISTERED": (
        "reader_id", "family_id", "display_name", "birth_date", "is_minor", "interests",
    ),
    "GUARDIANSHIP_DECLARED": ("family_id", "guardian_id", "child_id", "relationship"),
    "CONSENT_GRANTED": ("consent_id", "reader_id", "scope", "actor_id", "on_behalf_of_child"),
    "CONSENT_WITHDRAWN": ("consent_id", "reader_id", "scope", "actor_id", "self_withdrawal"),
    # —— 书目与版本 ——
    "BOOK_TITLE_REGISTERED": (
        "title_id", "title", "author", "min_age", "max_age", "interests", "summary", "revision",
    ),
    "BOOK_EDITION_PUBLISHED": ("edition_id", "title_id", "isbn", "publisher"),
    "TITLE_DESCRIPTION_UPDATED": ("title_id", "revision", "summary", "change_note"),
    # —— 门店、库存与临时闭店 ——
    "STORE_REGISTERED": ("store_id", "city", "name"),
    "STORE_CLOSURE_SCHEDULED": ("closure_id", "store_id", "starts_at", "ends_at", "reason"),
    "STORE_REOPENED": ("closure_id", "reopened_at"),
    "COPIES_ADDED": ("batch_id", "store_id", "edition_id", "quantity"),
    # —— 共享借阅（含跨店在途） ——
    "LOAN_REQUEST_ACCEPTED": (
        "loan_id", "request_id", "reader_id", "edition_id",
        "owning_store_id", "pickup_store_id", "at",
    ),
    "LOAN_IN_TRANSIT": ("loan_id", "at"),
    "LOAN_READY_FOR_PICKUP": ("loan_id", "store_id", "at"),
    "LOAN_PICKED_UP": ("loan_id", "reader_id", "store_id", "at", "due_at"),
    "LOAN_RETURNED": ("loan_id", "reader_id", "return_store_id", "at"),
    # —— 取还安排 ——
    "APPOINTMENT_SCHEDULED": (
        "appointment_id", "loan_id", "appointment_kind", "store_id", "scheduled_at",
    ),
    "APPOINTMENT_RESCHEDULED": (
        "appointment_id", "new_store_id", "new_scheduled_at", "closure_id",
    ),
    "APPOINTMENT_FULFILLED": ("appointment_id", "at"),
    # —— 阅读指导与推荐（推荐理由绑定书目说明修订版） ——
    "READING_GUIDE_ASSIGNED": (
        "guide_id", "reader_id", "topic", "assigned_by", "basis",
    ),
    "RECOMMENDATION_MADE": (
        "recommendation_id", "reader_id", "title_id",
        "title_description_revision", "rationale", "recommended_by", "source",
    ),
    # —— 作家活动：报名 / 候补 分开记账（活动标识用 author_event_id，避开信封 event_id） ——
    "AUTHOR_EVENT_SCHEDULED": (
        "author_event_id", "store_id", "title", "author", "starts_at", "capacity",
    ),
    "EVENT_REGISTERED": ("author_event_id", "registration_id", "request_id", "reader_id"),
    "EVENT_WAITLISTED": ("author_event_id", "entry_id", "request_id", "reader_id", "position"),
    "EVENT_REGISTRATION_CANCELLED": ("author_event_id", "registration_id", "reader_id"),
    "WAITLIST_ENTRY_PROMOTED": ("author_event_id", "entry_id", "registration_id", "reader_id"),
    "EVENT_ATTENDANCE_RECORDED": ("author_event_id", "registration_id", "reader_id"),
    # —— 会员权益（独立于报名/候补计数） ——
    "MEMBERSHIP_GRANTED": (
        "reader_id", "plan", "loan_quota", "valid_from", "valid_until",
    ),
    "BENEFIT_USED": ("reader_id", "benefit", "ref_id", "at"),
    # —— 家庭共读反馈 ——
    "FAMILY_FEEDBACK_RECORDED": (
        "feedback_id", "family_id", "reader_id", "title_id",
        "loan_id", "author_event_id", "rating", "note", "at",
    ),
}

# 授权范围。儿童本人对自己的数据始终有最终撤回权。
CONSENT_SCOPES = (
    "profile", "loan", "recommendation", "guidance", "event", "feedback",
)
BASE_LOAN_QUOTA = 5


def make_event(
    kind: str,
    payload: dict[str, Any],
    *,
    version: int,
    occurred_at: datetime,
    event_id: str | None = None,
) -> dict[str, Any]:
    if kind not in PAYLOAD_REQUIRED:
        raise ValueError(f"未知事件类型: {kind}")
    missing = [f for f in PAYLOAD_REQUIRED[kind] if f not in payload]
    if missing:
        raise ValueError(f"事件 {kind} 缺少字段: {missing}")
    event = {
        "event_id": event_id or f"e-{version:06d}",
        "kind": kind,
        "occurred_at": occurred_at.isoformat(),
        "subject_id": SUBJECT,
        "version": version,
    }
    event.update(payload)
    return event
