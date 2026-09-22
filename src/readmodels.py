"""只读模型：家庭端 / 门店端 / 运营追溯共用同一份归约状态。

家庭端与门店端不做两套数据：这里的投影全部从同一事件流 fold 出的
ReadingService 状态计算，所以家庭看到的地点、时间、可借状态与门店一致。
"""
from __future__ import annotations

from datetime import date, datetime

from .service import ReadingService, age_on


# ---------------------------------------------------------------- 书目 / 推荐
def visible_catalog(svc: ReadingService, reader_id: str, on_date: date) -> list[dict]:
    """儿童只能看到适龄内容；书目说明按“当前修订版”展示。"""
    reader = svc.readers[reader_id]
    age = age_on(reader["birth_date"], on_date) if reader["is_minor"] else None
    rows = []
    for t in svc.titles.values():
        if age is not None and not (t.min_age <= age <= t.max_age):
            continue
        rows.append({
            "title_id": t.title_id, "title": t.title, "author": t.author,
            "min_age": t.min_age, "max_age": t.max_age,
            "interests": t.interests,
            "summary_revision": t.current_revision,
            "summary": t.revisions[t.current_revision],
        })
    return rows


def recommendation_as_made(svc: ReadingService, recommendation_id: str) -> dict:
    """还原“当时为什么推荐”：即使书目说明后来更新，也按冻结的修订版回放。"""
    rec = svc.recommendations[recommendation_id]
    title = svc.titles[rec["title_id"]]
    rev = rec["title_description_revision"]
    return {
        "recommendation_id": recommendation_id,
        "reader_id": rec["reader_id"],
        "title_id": title.title_id,
        "title": title.title,
        "frozen_revision": rev,
        "summary_then": title.revisions[rev],
        "current_revision": title.current_revision,
        "rationale": rec["rationale"],
        "recommended_by": rec["recommended_by"],
        "source": rec["source"],
        "summary_changed_since": rev != title.current_revision,
    }


# ---------------------------------------------------------------- 库存 / 可借
def store_availability(svc: ReadingService, store_id: str) -> list[dict]:
    # 本地在架为 0 但有跨店在途到货的版本也要出现：
    # 家庭端的“可借/在途”状态与门店端必须一致。
    edition_ids = {e for (s, e) in svc.inventory if s == store_id}
    edition_ids |= {ln.edition_id for ln in svc.loans.values()
                    if ln.pickup_store_id == store_id and ln.status == "in_transit"}
    rows = []
    for edition_id in edition_ids:
        edition = svc.editions[edition_id]
        title = svc.titles[edition["title_id"]]
        qty = svc.inventory.get((store_id, edition_id), 0)
        in_transit = sum(1 for ln in svc.loans.values()
                         if ln.edition_id == edition_id and ln.status == "in_transit"
                         and ln.pickup_store_id == store_id)
        rows.append({
            "store_id": store_id, "edition_id": edition_id,
            "title_id": title.title_id, "title": title.title, "isbn": edition["isbn"],
            "available_now": qty, "incoming_in_transit": in_transit,
        })
    return rows


def edition_availability_network(svc: ReadingService, edition_id: str) -> dict:
    on_shelf = {s: n for (s, e), n in svc.inventory.items() if e == edition_id and n > 0}
    return {
        "edition_id": edition_id,
        "on_shelf_by_store": on_shelf,
        "borrowable": bool(on_shelf),
    }


# ---------------------------------------------------------------- 借阅状态
def loan_view(svc: ReadingService, loan_id: str, on_date: date) -> dict:
    ln = svc.loans[loan_id]
    is_overdue = (
        ln.status == "picked_up" and ln.due_at is not None
        and on_date > ln.due_at.date()
    )
    pickup = svc.appointments.get(f"appt-pickup-{loan_id}")
    ret = svc.appointments.get(f"appt-return-{loan_id}")
    return {
        "loan_id": loan_id, "reader_id": ln.reader_id, "edition_id": ln.edition_id,
        "status": ln.status,
        "status_label": {
            "accepted": "已受理", "in_transit": "跨店在途",
            "ready": "已到店待取", "picked_up": "借阅中", "returned": "已归还",
        }[ln.status],
        "owning_store_id": ln.owning_store_id,
        "pickup_store_id": ln.pickup_store_id,
        "cross_store": ln.owning_store_id != ln.pickup_store_id,
        "due_at": ln.due_at.date().isoformat() if ln.due_at else None,
        "overdue": is_overdue,
        "return_store_id": ln.return_store_id,
        "pickup_appointment": _appt(pickup),
        "return_appointment": _appt(ret),
    }


def _appt(appt: dict | None) -> dict | None:
    if not appt:
        return None
    return {
        "appointment_id": appt["appointment_id"],
        "appointment_kind": appt["appointment_kind"],
        "store_id": appt["store_id"],
        "scheduled_at": appt["scheduled_at"].isoformat(),
        "status": appt["status"],
        "rescheduled_for_closure": appt.get("rescheduled_for"),
    }


def family_loans(svc: ReadingService, family_id: str, on_date: date) -> list[dict]:
    return [loan_view(svc, lid, on_date)
            for lid, ln in svc.loans.items()
            if svc.readers[ln.reader_id]["family_id"] == family_id]


# ---------------------------------------------------------------- 活动 / 追溯
def event_roster(svc: ReadingService, author_event_id: str) -> dict:
    """报名、候补、会员权益三种计数严格分开。"""
    ae = svc.author_events[author_event_id]
    registered = [
        {"reader_id": r["reader_id"], "registration_id": rid,
         "from_waitlist": bool(r.get("from_waitlist"))}
        for rid, r in ae["registrations"].items()
        if r["status"] in ("registered", "attended")
    ]
    waitlist = [
        {"entry_id": e["entry_id"], "reader_id": e["reader_id"], "position": e["position"]}
        for e in ae["waitlist"] if e["status"] == "waiting"
    ]
    attended = [r["reader_id"] for r in ae["registrations"].values()
                if r["status"] == "attended"]
    return {
        "author_event_id": author_event_id, "title": ae["title"],
        "store_id": ae["store_id"], "starts_at": ae["starts_at"].isoformat(),
        "capacity": ae["capacity"],
        "registered_count": len(registered),
        "waitlist_count": len(waitlist),
        "attended_count": len(attended),
        "seats_left": ae["capacity"] - len(registered),
        "registered": registered, "waitlist": waitlist, "attended": attended,
    }


def trace_event_followup(svc: ReadingService, author_event_id: str,
                         on_date: date) -> dict:
    """运营者从一次作家对谈追到：到场 → 后续借阅 → 家庭反馈 → 阅读指导。

    串接依据是事件里显式落库的 author_event_id 反查不到借阅，因此借阅通过
    “活动之后产生、且推荐/指导来源标注为该活动”与反馈里的 author_event_id 串联；
    反馈事件直接携带 author_event_id，是最强证据链。
    """
    roster = event_roster(svc, author_event_id)
    reader_ids = {r["reader_id"] for r in roster["registered"]} | set(roster["attended"])
    starts_at = svc.author_events[author_event_id]["starts_at"]

    feedbacks = [
        {"feedback_id": f["feedback_id"], "reader_id": f["reader_id"],
         "title_id": f["title_id"], "loan_id": f["loan_id"],
         "rating": f["rating"], "note": f["note"]}
        for f in svc.feedbacks if f.get("author_event_id") == author_event_id
    ]
    loans_after = [
        loan_view(svc, lid, on_date)
        for lid, ln in svc.loans.items()
        if ln.reader_id in reader_ids
        and datetime.fromisoformat(
            next(e["occurred_at"] for e in svc.events
                 if e["kind"] == "LOAN_REQUEST_ACCEPTED" and e["loan_id"] == lid)
        ) >= starts_at
    ]
    guides = [
        {"guide_id": g["guide_id"], "reader_id": g["reader_id"],
         "topic": g["topic"], "basis": g["basis"]}
        for g in svc.guides.values()
        if g["reader_id"] in reader_ids and g["basis"] == "author_event"
    ]
    return {
        "author_event_id": author_event_id,
        "attended": roster["attended"],
        "loans_after_event": loans_after,
        "guides_from_event": guides,
        "family_feedback": feedbacks,
    }


# ---------------------------------------------------------------- 家庭总视图
def family_dashboard(svc: ReadingService, family_id: str, on_date: date) -> dict:
    """家庭端首页：对每个成员只暴露其授权且适龄的内容。"""
    members = [rid for rid, r in svc.readers.items() if r["family_id"] == family_id]
    result = {"family_id": family_id, "members": []}
    for rid in members:
        reader = svc.readers[rid]
        granted_scopes = sorted(
            scope for (who, scope), c in svc.consents.items()
            if who == rid and c.granted
        )
        entry = {
            "reader_id": rid, "display_name": reader["display_name"],
            "is_minor": reader["is_minor"], "interests": reader["interests"],
            "granted_scopes": granted_scopes,
            "catalog": visible_catalog(svc, rid, on_date),
            "loans": [loan_view(svc, lid, on_date)
                      for lid, ln in svc.loans.items() if ln.reader_id == rid],
            "recommendations": [
                recommendation_as_made(svc, rec_id)
                for rec_id, rec in svc.recommendations.items() if rec["reader_id"] == rid
            ] if "recommendation" in granted_scopes else [],
            "guides": [
                {"guide_id": g["guide_id"], "topic": g["topic"], "basis": g["basis"]}
                for g in svc.guides.values() if g["reader_id"] == rid
            ] if "guidance" in granted_scopes else [],
        }
        result["members"].append(entry)
    return result


# ---------------------------------------------------------------- 门店总视图
def store_dashboard(svc: ReadingService, store_id: str, on_date: date) -> dict:
    """门店端：库存、闭店窗口、受闭店影响改约的安排，与家庭端同源。"""
    closures = [
        {"closure_id": c["closure_id"], "starts_at": c["starts_at"].isoformat(),
         "ends_at": c["ends_at"].isoformat(), "reason": c["reason"],
         "reopened": c["reopened"]}
        for c in svc.closures.values() if c["store_id"] == store_id
    ]
    affected_appointments = [
        _appt(a) for a in svc.appointments.values()
        if a.get("rescheduled_for") and (
            svc.closures[a["rescheduled_for"]]["store_id"] == store_id
        )
    ]
    return {
        "store_id": store_id,
        "store": svc.stores.get(store_id),
        "closures": closures,
        "availability": store_availability(svc, store_id),
        "affected_appointments": affected_appointments,
        "loans_here": [
            loan_view(svc, lid, on_date)
            for lid, ln in svc.loans.items()
            if ln.pickup_store_id == store_id or ln.return_store_id == store_id
        ],
    }
