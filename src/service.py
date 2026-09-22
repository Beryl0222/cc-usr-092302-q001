"""家庭持续阅读服务：纯事件溯源内核。

设计要点
- 一切状态变化都是追加事件；本模块同时是归约器（fold 事件流）和命令 API。
- 命令以 request_id 去重：重复扫码 / 断网补传在同一 request_id 下直接回放首次结果，
  不会第二次扣库存或占用借阅名额（见 self._results）。
- 授权、年龄适配、库存、名额、闭店改约等全部不变量在事件落库之前校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from itertools import count

from .events import BASE_LOAN_QUOTA, CONSENT_SCOPES, make_event

CST = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(CST)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def age_on(birth_date: str, day: date) -> int:
    bd = date.fromisoformat(birth_date)
    return day.year - bd.year - ((day.month, day.day) < (bd.month, bd.day))


class DomainError(Exception):
    """命令违反业务不变量。"""


@dataclass
class Consent:
    consent_id: str
    granted: bool = True
    # 孩子本人撤回后置 True：家长此后不得替其重新授权，直到孩子本人重新授权。
    blocked_by_child: bool = False


@dataclass
class Title:
    title_id: str
    title: str
    author: str
    min_age: int
    max_age: int
    interests: list[str]
    revisions: dict[int, str] = field(default_factory=dict)
    current_revision: int = 1


@dataclass
class Loan:
    loan_id: str
    reader_id: str
    edition_id: str
    owning_store_id: str
    pickup_store_id: str
    status: str = "accepted"          # accepted/in_transit/ready/picked_up/returned
    due_at: datetime | None = None
    returned_at: datetime | None = None
    return_store_id: str | None = None
    request_id: str | None = None


class ReadingService:
    def __init__(self, events: list[dict] | None = None):
        # 归约状态
        self.readers: dict[str, dict] = {}
        self.guardianships: dict[str, dict] = {}          # child_id -> 监护声明
        self.consents: dict[tuple[str, str], Consent] = {}
        self.titles: dict[str, Title] = {}
        self.editions: dict[str, dict] = {}
        self.stores: dict[str, dict] = {}
        self.closures: dict[str, dict] = {}
        self.inventory: dict[tuple[str, str], int] = {}
        self.loans: dict[str, Loan] = {}
        self.appointments: dict[str, dict] = {}
        self.guides: dict[str, dict] = {}
        self.recommendations: dict[str, dict] = {}
        self.author_events: dict[str, dict] = {}
        self.memberships: dict[str, dict] = {}
        self.benefits: list[dict] = []
        self.feedbacks: list[dict] = []
        # 幂等索引
        self.batches: set[str] = set()
        self.requests: dict[str, list[str]] = {}          # request_id -> 首次产生的 event_id
        self.events: list[dict] = list(events or [])
        for event in self.events:
            self.apply(event)
        self._versions = count(max((e["version"] for e in self.events), default=0) + 1)
        # 重放后重建幂等索引，使断网补传对“已折叠过的历史流”同样安全。
        for event in self.events:
            rid = event.get("request_id")
            if rid:
                self.requests.setdefault(rid, []).append(event["event_id"])

    # ------------------------------------------------------------------ 归约
    def apply(self, event: dict) -> None:
        kind = event["kind"]
        p = event
        if kind == "READER_REGISTERED":
            self.readers[p["reader_id"]] = {
                "family_id": p["family_id"], "display_name": p["display_name"],
                "birth_date": p["birth_date"], "is_minor": p["is_minor"],
                "interests": list(p["interests"]),
            }
        elif kind == "GUARDIANSHIP_DECLARED":
            self.guardianships[p["child_id"]] = {
                "family_id": p["family_id"], "guardian_id": p["guardian_id"],
                "relationship": p["relationship"],
            }
        elif kind == "CONSENT_GRANTED":
            key = (p["reader_id"], p["scope"])
            consent = self.consents.get(key) or Consent(p["consent_id"])
            consent.granted = True
            # 孩子本人授权可以解除自己之前的撤回封锁；家长代办不能。
            if not p["on_behalf_of_child"] and p["actor_id"] == p["reader_id"]:
                consent.blocked_by_child = False
            self.consents[key] = consent
        elif kind == "CONSENT_WITHDRAWN":
            key = (p["reader_id"], p["scope"])
            consent = self.consents.get(key) or Consent(p["consent_id"], granted=False)
            consent.granted = False
            if p.get("self_withdrawal"):
                consent.blocked_by_child = True
            self.consents[key] = consent
        elif kind == "BOOK_TITLE_REGISTERED":
            self.titles[p["title_id"]] = Title(
                p["title_id"], p["title"], p["author"], p["min_age"], p["max_age"],
                list(p["interests"]), revisions={p["revision"]: p["summary"]},
                current_revision=p["revision"],
            )
        elif kind == "BOOK_EDITION_PUBLISHED":
            self.editions[p["edition_id"]] = {
                "title_id": p["title_id"], "isbn": p["isbn"], "publisher": p["publisher"],
            }
        elif kind == "TITLE_DESCRIPTION_UPDATED":
            title = self.titles[p["title_id"]]
            title.revisions[p["revision"]] = p["summary"]
            title.current_revision = p["revision"]
        elif kind == "STORE_REGISTERED":
            self.stores[p["store_id"]] = {"store_id": p["store_id"], "city": p["city"], "name": p["name"]}
        elif kind == "STORE_CLOSURE_SCHEDULED":
            self.closures[p["closure_id"]] = {
                "closure_id": p["closure_id"], "store_id": p["store_id"],
                "starts_at": parse_dt(p["starts_at"]), "ends_at": parse_dt(p["ends_at"]),
                "reason": p["reason"], "reopened": False,
            }
        elif kind == "STORE_REOPENED":
            self.closures[p["closure_id"]]["reopened"] = True
        elif kind == "COPIES_ADDED":
            self.batches.add(p["batch_id"])
            key = (p["store_id"], p["edition_id"])
            self.inventory[key] = self.inventory.get(key, 0) + p["quantity"]
        elif kind == "LOAN_REQUEST_ACCEPTED":
            loan = Loan(
                p["loan_id"], p["reader_id"], p["edition_id"],
                p["owning_store_id"], p["pickup_store_id"], status="accepted",
                request_id=p["request_id"],
            )
            self.loans[p["loan_id"]] = loan
            self._dec(p["owning_store_id"], p["edition_id"])
        elif kind == "LOAN_IN_TRANSIT":
            self.loans[p["loan_id"]].status = "in_transit"
        elif kind == "LOAN_READY_FOR_PICKUP":
            self.loans[p["loan_id"]].status = "ready"
        elif kind == "LOAN_PICKED_UP":
            loan = self.loans[p["loan_id"]]
            loan.status = "picked_up"
            loan.due_at = parse_dt(p["due_at"])
        elif kind == "LOAN_RETURNED":
            loan = self.loans[p["loan_id"]]
            loan.status = "returned"
            loan.returned_at = parse_dt(p["at"])
            loan.return_store_id = p["return_store_id"]
            key = (p["return_store_id"], loan.edition_id)
            self.inventory[key] = self.inventory.get(key, 0) + 1
        elif kind == "APPOINTMENT_SCHEDULED":
            self.appointments[p["appointment_id"]] = {
                "appointment_id": p["appointment_id"], "loan_id": p["loan_id"],
                "appointment_kind": p["appointment_kind"],
                "store_id": p["store_id"],
                "scheduled_at": parse_dt(p["scheduled_at"]), "status": "scheduled",
            }
        elif kind == "APPOINTMENT_RESCHEDULED":
            appt = self.appointments[p["appointment_id"]]
            appt["store_id"] = p["new_store_id"]
            appt["scheduled_at"] = parse_dt(p["new_scheduled_at"])
            appt["rescheduled_for"] = p["closure_id"]
            # 取书改店时，借阅的取书店同步改：家庭与门店看到的地点必须一致。
            if appt["appointment_kind"] == "pickup":
                self.loans[appt["loan_id"]].pickup_store_id = p["new_store_id"]
        elif kind == "APPOINTMENT_FULFILLED":
            self.appointments[p["appointment_id"]]["status"] = "fulfilled"
        elif kind == "READING_GUIDE_ASSIGNED":
            self.guides[p["guide_id"]] = dict(p)
        elif kind == "RECOMMENDATION_MADE":
            self.recommendations[p["recommendation_id"]] = dict(p)
        elif kind == "AUTHOR_EVENT_SCHEDULED":
            self.author_events[p["author_event_id"]] = {
                "author_event_id": p["author_event_id"], "store_id": p["store_id"],
                "title": p["title"], "author": p["author"],
                "starts_at": parse_dt(p["starts_at"]), "capacity": p["capacity"],
                "registrations": {}, "waitlist": [],
            }
        elif kind == "EVENT_REGISTERED":
            ae = self.author_events[p["author_event_id"]]
            ae["registrations"][p["registration_id"]] = {
                "reader_id": p["reader_id"], "status": "registered", "request_id": p["request_id"],
            }
        elif kind == "EVENT_WAITLISTED":
            ae = self.author_events[p["author_event_id"]]
            ae["waitlist"].append({
                "entry_id": p["entry_id"], "reader_id": p["reader_id"],
                "request_id": p["request_id"], "position": p["position"], "status": "waiting",
            })
        elif kind == "EVENT_REGISTRATION_CANCELLED":
            ae = self.author_events[p["author_event_id"]]
            ae["registrations"][p["registration_id"]]["status"] = "cancelled"
        elif kind == "WAITLIST_ENTRY_PROMOTED":
            ae = self.author_events[p["author_event_id"]]
            entry = next(e for e in ae["waitlist"] if e["entry_id"] == p["entry_id"])
            entry["status"] = "promoted"
            ae["registrations"][p["registration_id"]] = {
                "reader_id": p["reader_id"], "status": "registered", "from_waitlist": True,
            }
        elif kind == "EVENT_ATTENDANCE_RECORDED":
            ae = self.author_events[p["author_event_id"]]
            ae["registrations"][p["registration_id"]]["status"] = "attended"
        elif kind == "MEMBERSHIP_GRANTED":
            self.memberships[p["reader_id"]] = dict(p)
        elif kind == "BENEFIT_USED":
            self.benefits.append(dict(p))
        elif kind == "FAMILY_FEEDBACK_RECORDED":
            self.feedbacks.append(dict(p))
        else:
            raise DomainError(f"归约器不认识事件 {kind}")

    def _dec(self, store_id: str, edition_id: str) -> None:
        key = (store_id, edition_id)
        if self.inventory.get(key, 0) <= 0:
            raise DomainError("库存不足，不能为负")
        self.inventory[key] -= 1

    # -------------------------------------------------------------- 内部工具
    def _append(self, kind: str, payload: dict, at: datetime,
                request_id: str | None = None) -> dict:
        if request_id is not None:
            payload = {**payload, "request_id": request_id}
        event = make_event(kind, payload, version=next(self._versions), occurred_at=at)
        self.events.append(event)
        self.apply(event)
        return event

    def _dedupe(self, request_id: str) -> list[dict] | None:
        """同一 request_id 重复提交：回放首次事件，不再产生任何状态变化。"""
        if request_id in self.requests:
            return [self._by_id[eid] for eid in self.requests[request_id]]
        return None

    @property
    def _by_id(self) -> dict[str, dict]:
        return {e["event_id"]: e for e in self.events}

    def _require_reader(self, reader_id: str) -> dict:
        if reader_id not in self.readers:
            raise DomainError(f"读者不存在: {reader_id}")
        return self.readers[reader_id]

    def _require_consent(self, reader_id: str, scope: str) -> None:
        if scope not in CONSENT_SCOPES:
            raise DomainError(f"未知授权范围: {scope}")
        consent = self.consents.get((reader_id, scope))
        if not consent or not consent.granted:
            raise DomainError(f"读者 {reader_id} 未授予 {scope} 授权（或已撤回）")

    def _guardian_may_act(self, actor_id: str, child_id: str) -> bool:
        decl = self.guardianships.get(child_id)
        return bool(decl and decl["guardian_id"] == actor_id)

    def _resolve_actor(self, reader_id: str, actor_id: str | None, on_behalf: bool) -> str:
        actor_id = actor_id or reader_id
        if on_behalf and not self._guardian_may_act(actor_id, reader_id):
            raise DomainError("仅有已声明监护人可代办")
        return actor_id

    # ---------------------------------------------------------------- 读者域
    def register_reader(self, reader_id: str, family_id: str, display_name: str,
                        birth_date: str, interests: list[str], *, at: datetime | None = None) -> dict:
        if reader_id in self.readers:
            raise DomainError("读者已存在")
        at = at or now()
        is_minor = age_on(birth_date, at.date()) < 18
        return self._append("READER_REGISTERED", {
            "reader_id": reader_id, "family_id": family_id, "display_name": display_name,
            "birth_date": birth_date, "is_minor": is_minor, "interests": interests,
        }, at)

    def declare_guardianship(self, family_id: str, guardian_id: str, child_id: str,
                             relationship: str, *, at: datetime | None = None) -> dict:
        self._require_reader(guardian_id)
        child = self._require_reader(child_id)
        if not child["is_minor"]:
            raise DomainError("监护关系只能针对未成年读者")
        return self._append("GUARDIANSHIP_DECLARED", {
            "family_id": family_id, "guardian_id": guardian_id,
            "child_id": child_id, "relationship": relationship,
        }, at or now())

    def grant_consent(self, consent_id: str, reader_id: str, scope: str, *,
                      actor_id: str | None = None, on_behalf: bool = False,
                      at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        if scope not in CONSENT_SCOPES:
            raise DomainError(f"未知授权范围: {scope}")
        actor_id = self._resolve_actor(reader_id, actor_id, on_behalf)
        existing = self.consents.get((reader_id, scope))
        if on_behalf and existing and existing.blocked_by_child:
            # 家长不能越过孩子本人的撤回选择。
            raise DomainError("孩子已本人撤回该授权，家长代办不能恢复")
        return self._append("CONSENT_GRANTED", {
            "consent_id": consent_id, "reader_id": reader_id, "scope": scope,
            "actor_id": actor_id, "on_behalf_of_child": on_behalf,
        }, at or now())

    def withdraw_consent(self, consent_id: str, reader_id: str, scope: str, *,
                         actor_id: str | None = None, at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        actor_id = actor_id or reader_id
        self_withdrawal = actor_id == reader_id
        if not self_withdrawal and not self._guardian_may_act(actor_id, reader_id):
            raise DomainError("非监护人不能代为撤回")
        return self._append("CONSENT_WITHDRAWN", {
            "consent_id": consent_id, "reader_id": reader_id, "scope": scope,
            "actor_id": actor_id, "self_withdrawal": self_withdrawal,
        }, at or now())

    # ---------------------------------------------------------------- 书目域
    def register_title(self, title_id: str, title: str, author: str, min_age: int,
                       max_age: int, interests: list[str], summary: str, *,
                       revision: int = 1, at: datetime | None = None) -> dict:
        if title_id in self.titles:
            raise DomainError("书目已存在")
        if min_age > max_age:
            raise DomainError("适龄区间非法")
        return self._append("BOOK_TITLE_REGISTERED", {
            "title_id": title_id, "title": title, "author": author,
            "min_age": min_age, "max_age": max_age, "interests": interests,
            "summary": summary, "revision": revision,
        }, at or now())

    def publish_edition(self, edition_id: str, title_id: str, isbn: str,
                        publisher: str, *, at: datetime | None = None) -> dict:
        if title_id not in self.titles:
            raise DomainError("书目不存在")
        return self._append("BOOK_EDITION_PUBLISHED", {
            "edition_id": edition_id, "title_id": title_id, "isbn": isbn, "publisher": publisher,
        }, at or now())

    def update_title_description(self, title_id: str, summary: str, change_note: str, *,
                                 at: datetime | None = None) -> dict:
        title = self.titles.get(title_id)
        if not title:
            raise DomainError("书目不存在")
        return self._append("TITLE_DESCRIPTION_UPDATED", {
            "title_id": title_id, "revision": title.current_revision + 1,
            "summary": summary, "change_note": change_note,
        }, at or now())

    # ---------------------------------------------------------------- 门店域
    def register_store(self, store_id: str, city: str, name: str, *,
                       at: datetime | None = None) -> dict:
        return self._append("STORE_REGISTERED", {
            "store_id": store_id, "city": city, "name": name,
        }, at or now())

    def add_copies(self, batch_id: str, store_id: str, edition_id: str, quantity: int, *,
                   at: datetime | None = None) -> dict:
        if batch_id in self.batches:
            raise DomainError("入库批次重复提交")
        if quantity <= 0:
            raise DomainError("入库数量必须为正")
        if store_id not in self.stores or edition_id not in self.editions:
            raise DomainError("门店或版本不存在")
        return self._append("COPIES_ADDED", {
            "batch_id": batch_id, "store_id": store_id,
            "edition_id": edition_id, "quantity": quantity,
        }, at or now())

    def schedule_closure(self, closure_id: str, store_id: str, starts_at: datetime,
                         ends_at: datetime, reason: str, *,
                         alternate_store_id: str | None = None,
                         at: datetime | None = None) -> list[dict]:
        """临时闭店：登记闭店，并只改落在闭店窗口内、且尚未完成的取/还安排。"""
        if closure_id in self.closures:
            raise DomainError("闭店单重复提交")
        if ends_at <= starts_at:
            raise DomainError("闭店时间区间非法")
        at = at or now()
        produced = [self._append("STORE_CLOSURE_SCHEDULED", {
            "closure_id": closure_id, "store_id": store_id,
            "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "reason": reason,
        }, at)]
        new_store = alternate_store_id or store_id
        if new_store != store_id and new_store not in self.stores:
            raise DomainError("替代门店不存在")
        for appt in self.appointments.values():
            if appt["status"] != "scheduled":
                continue
            if appt["store_id"] != store_id:
                continue
            when = appt["scheduled_at"]
            if starts_at <= when < ends_at:
                produced.append(self._append("APPOINTMENT_RESCHEDULED", {
                    "appointment_id": appt["appointment_id"],
                    "new_store_id": new_store,
                    "new_scheduled_at": ends_at.isoformat(),
                    "closure_id": closure_id,
                }, at))
        return produced

    def reopen_store(self, closure_id: str, *, at: datetime | None = None) -> dict:
        if closure_id not in self.closures:
            raise DomainError("闭店单不存在")
        return self._append("STORE_REOPENED", {
            "closure_id": closure_id, "reopened_at": (at or now()).isoformat(),
        }, at or now())

    # ---------------------------------------------------------------- 借阅域
    def _active_loan_count(self, reader_id: str) -> int:
        return sum(1 for ln in self.loans.values()
                   if ln.reader_id == reader_id and ln.status != "returned")

    def _loan_quota(self, reader_id: str, day: date) -> int:
        m = self.memberships.get(reader_id)
        if m and date.fromisoformat(m["valid_from"]) <= day <= date.fromisoformat(m["valid_until"]):
            return m["loan_quota"]
        return BASE_LOAN_QUOTA

    def request_loan(self, request_id: str, reader_id: str, edition_id: str,
                     pickup_store_id: str, *, actor_id: str | None = None,
                     on_behalf: bool = False, pickup_at: datetime | None = None,
                     at: datetime | None = None) -> list[dict]:
        replay = self._dedupe(request_id)
        if replay is not None:
            return replay
        self._require_reader(reader_id)
        actor_id = self._resolve_actor(reader_id, actor_id, on_behalf)
        self._require_consent(reader_id, "loan")
        edition = self.editions.get(edition_id)
        if not edition:
            raise DomainError("版本不存在")
        title = self.titles[edition["title_id"]]
        reader = self.readers[reader_id]
        age = age_on(reader["birth_date"], (at or now()).date())
        if reader["is_minor"] and not (title.min_age <= age <= title.max_age):
            raise DomainError("该书不在该儿童适龄范围内")
        if pickup_store_id not in self.stores:
            raise DomainError("取书店不存在")
        if self._active_loan_count(reader_id) >= self._loan_quota(reader_id, (at or now()).date()):
            raise DomainError("借阅名额已满")

        # 同店有货即到店可借；否则从任一有货门店跨店调拨（在途）。
        if self.inventory.get((pickup_store_id, edition_id), 0) > 0:
            owning_store_id = pickup_store_id
        else:
            owners = [s for (s, e), n in self.inventory.items()
                      if e == edition_id and n > 0 and s in self.stores]
            if not owners:
                raise DomainError("全网无可用库存")
            owning_store_id = owners[0]

        at = at or now()
        loan_id = f"loan-{request_id}"
        pickup_when = pickup_at or at
        produced = [self._append("LOAN_REQUEST_ACCEPTED", {
            "loan_id": loan_id, "request_id": request_id, "reader_id": reader_id,
            "edition_id": edition_id, "owning_store_id": owning_store_id,
            "pickup_store_id": pickup_store_id, "at": at.isoformat(),
        }, at, request_id=request_id)]
        produced.append(self._append("BENEFIT_USED", {
            "reader_id": reader_id, "benefit": "loan",
            "ref_id": loan_id, "at": at.isoformat(),
        }, at, request_id=request_id))
        produced.append(self._append("APPOINTMENT_SCHEDULED", {
            "appointment_id": f"appt-pickup-{loan_id}", "loan_id": loan_id,
            "appointment_kind": "pickup", "store_id": pickup_store_id,
            "scheduled_at": pickup_when.isoformat(),
        }, at, request_id=request_id))
        if owning_store_id == pickup_store_id:
            produced.append(self._append("LOAN_READY_FOR_PICKUP", {
                "loan_id": loan_id, "store_id": pickup_store_id, "at": at.isoformat(),
            }, at, request_id=request_id))
        else:
            produced.append(self._append("LOAN_IN_TRANSIT", {
                "loan_id": loan_id, "at": at.isoformat(),
            }, at, request_id=request_id))
        self.requests[request_id] = [e["event_id"] for e in produced]
        return produced

    def mark_loan_arrived(self, loan_id: str, *, at: datetime | None = None) -> dict:
        loan = self.loans.get(loan_id)
        if not loan or loan.status not in ("in_transit", "accepted"):
            raise DomainError("借阅不在在途状态")
        return self._append("LOAN_READY_FOR_PICKUP", {
            "loan_id": loan_id, "store_id": loan.pickup_store_id,
            "at": (at or now()).isoformat(),
        }, at or now())

    def pick_up(self, loan_id: str, *, at: datetime | None = None,
                loan_days: int = 21) -> list[dict]:
        at = at or now()
        loan = self.loans.get(loan_id)
        if not loan:
            raise DomainError("借阅不存在")
        if loan.status not in ("ready",):
            raise DomainError("仅可领取已到店的书")
        if self._store_closed_at(loan.pickup_store_id, at):
            raise DomainError("门店闭店中，取书安排已改约，请按新安排领取")
        due = at + timedelta(days=loan_days)
        produced = [self._append("LOAN_PICKED_UP", {
            "loan_id": loan_id, "reader_id": loan.reader_id,
            "store_id": loan.pickup_store_id, "at": at.isoformat(),
            "due_at": due.isoformat(),
        }, at)]
        appt_id = f"appt-pickup-{loan_id}"
        if self.appointments.get(appt_id, {}).get("status") == "scheduled":
            produced.append(self._append("APPOINTMENT_FULFILLED", {
                "appointment_id": appt_id, "at": at.isoformat(),
            }, at))
        return produced

    def schedule_return(self, loan_id: str, return_store_id: str,
                        scheduled_at: datetime, *, at: datetime | None = None) -> dict:
        loan = self.loans.get(loan_id)
        if not loan or loan.status != "picked_up":
            raise DomainError("仅借出状态可约还书")
        if return_store_id not in self.stores:
            raise DomainError("还书门店不存在")
        return self._append("APPOINTMENT_SCHEDULED", {
            "appointment_id": f"appt-return-{loan_id}", "loan_id": loan_id,
            "appointment_kind": "return", "store_id": return_store_id,
            "scheduled_at": scheduled_at.isoformat(),
        }, at or now())

    def return_loan(self, loan_id: str, return_store_id: str, *,
                    at: datetime | None = None) -> list[dict]:
        at = at or now()
        loan = self.loans.get(loan_id)
        if not loan or loan.status != "picked_up":
            raise DomainError("仅借出状态可还书")
        if return_store_id not in self.stores:
            raise DomainError("还书门店不存在")
        produced = [self._append("LOAN_RETURNED", {
            "loan_id": loan_id, "reader_id": loan.reader_id,
            "return_store_id": return_store_id, "at": at.isoformat(),
        }, at)]
        appt_id = f"appt-return-{loan_id}"
        if self.appointments.get(appt_id, {}).get("status") == "scheduled":
            produced.append(self._append("APPOINTMENT_FULFILLED", {
                "appointment_id": appt_id, "at": at.isoformat(),
            }, at))
        return produced

    def _store_closed_at(self, store_id: str, moment: datetime) -> bool:
        return any(c["store_id"] == store_id and not c["reopened"]
                   and c["starts_at"] <= moment < c["ends_at"]
                   for c in self.closures.values())

    # ------------------------------------------------------------ 指导与推荐
    ALLOWED_GUIDE_BASES = {"interest", "age_fit", "author_event", "family_feedback", "guardian_request"}

    def assign_reading_guide(self, guide_id: str, reader_id: str, topic: str,
                             basis: str, *, actor_id: str | None = None,
                             at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        # 阅读指导的分配依据是白名单：任何消费金额类依据在类型层面就无法通过。
        if basis not in self.ALLOWED_GUIDE_BASES:
            raise DomainError("阅读指导只能依据兴趣/年龄/活动/反馈/家长请求，与消费金额无关")
        self._require_consent(reader_id, "guidance")
        return self._append("READING_GUIDE_ASSIGNED", {
            "guide_id": guide_id, "reader_id": reader_id, "topic": topic,
            "assigned_by": actor_id or "librarian", "basis": basis,
        }, at or now())

    def recommend_title(self, recommendation_id: str, reader_id: str, title_id: str,
                        rationale: str, source: str, *, recommended_by: str = "librarian",
                        at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        self._require_consent(reader_id, "recommendation")
        title = self.titles.get(title_id)
        if not title:
            raise DomainError("书目不存在")
        at = at or now()
        reader = self.readers[reader_id]
        age = age_on(reader["birth_date"], at.date())
        if reader["is_minor"] and not (title.min_age <= age <= title.max_age):
            raise DomainError("不能向儿童推荐超龄内容")
        # 关键：把推荐时刻的书目说明修订版一并冻结，事后说明更新也能还原当时理由。
        return self._append("RECOMMENDATION_MADE", {
            "recommendation_id": recommendation_id, "reader_id": reader_id,
            "title_id": title_id, "title_description_revision": title.current_revision,
            "rationale": rationale, "recommended_by": recommended_by, "source": source,
        }, at)

    # ---------------------------------------------------------------- 活动域
    def schedule_author_event(self, author_event_id: str, store_id: str, title: str,
                              author: str, starts_at: datetime, capacity: int, *,
                              at: datetime | None = None) -> dict:
        if author_event_id in self.author_events:
            raise DomainError("活动已存在")
        if store_id not in self.stores:
            raise DomainError("门店不存在")
        return self._append("AUTHOR_EVENT_SCHEDULED", {
            "author_event_id": author_event_id, "store_id": store_id,
            "title": title, "author": author, "starts_at": starts_at.isoformat(),
            "capacity": capacity,
        }, at or now())

    def register_for_event(self, request_id: str, author_event_id: str, reader_id: str, *,
                           actor_id: str | None = None, on_behalf: bool = False,
                           at: datetime | None = None) -> list[dict]:
        replay = self._dedupe(request_id)
        if replay is not None:
            return replay
        self._require_reader(reader_id)
        self._resolve_actor(reader_id, actor_id, on_behalf)
        self._require_consent(reader_id, "event")
        ae = self.author_events.get(author_event_id)
        if not ae:
            raise DomainError("活动不存在")
        active = {r["reader_id"] for r in ae["registrations"].values()
                  if r["status"] in ("registered", "attended")}
        waiting = {e["reader_id"] for e in ae["waitlist"] if e["status"] == "waiting"}
        if reader_id in active or reader_id in waiting:
            raise DomainError("该读者已在报名/候补名单中")
        at = at or now()
        registration_id = f"reg-{author_event_id}-{reader_id}"
        if len(active) < ae["capacity"]:
            produced = [self._append("EVENT_REGISTERED", {
                "author_event_id": author_event_id, "registration_id": registration_id,
                "request_id": request_id, "reader_id": reader_id,
            }, at, request_id=request_id)]
        else:
            entry_id = f"wl-{author_event_id}-{reader_id}"
            position = sum(1 for e in ae["waitlist"] if e["status"] == "waiting") + 1
            produced = [self._append("EVENT_WAITLISTED", {
                "author_event_id": author_event_id, "entry_id": entry_id,
                "request_id": request_id, "reader_id": reader_id, "position": position,
            }, at, request_id=request_id)]
        self.requests[request_id] = [e["event_id"] for e in produced]
        return produced

    def cancel_event_registration(self, author_event_id: str, reader_id: str, *,
                                  at: datetime | None = None) -> list[dict]:
        ae = self.author_events.get(author_event_id)
        if not ae:
            raise DomainError("活动不存在")
        at = at or now()
        reg_id = f"reg-{author_event_id}-{reader_id}"
        reg = ae["registrations"].get(reg_id)
        if not reg or reg["status"] not in ("registered", "attended"):
            raise DomainError("没有有效的活动报名")
        produced = [self._append("EVENT_REGISTRATION_CANCELLED", {
            "author_event_id": author_event_id, "registration_id": reg_id,
            "reader_id": reader_id,
        }, at)]
        # 名额空出：按候补顺位递补（候补与报名始终分开计数）。
        active = {r["reader_id"] for r in ae["registrations"].values()
                  if r["status"] in ("registered", "attended")}
        if len(active) < ae["capacity"]:
            nxt = next((e for e in ae["waitlist"] if e["status"] == "waiting"), None)
            if nxt:
                produced.append(self._append("WAITLIST_ENTRY_PROMOTED", {
                    "author_event_id": author_event_id, "entry_id": nxt["entry_id"],
                    "registration_id": f"reg-{author_event_id}-{nxt['reader_id']}",
                    "reader_id": nxt["reader_id"],
                }, at))
        return produced

    def record_attendance(self, author_event_id: str, reader_id: str, *,
                          at: datetime | None = None) -> dict:
        ae = self.author_events.get(author_event_id)
        reg_id = f"reg-{author_event_id}-{reader_id}"
        if not ae or ae["registrations"].get(reg_id, {}).get("status") != "registered":
            raise DomainError("仅已报名读者可签到")
        return self._append("EVENT_ATTENDANCE_RECORDED", {
            "author_event_id": author_event_id, "registration_id": reg_id,
            "reader_id": reader_id,
        }, at or now())

    # ---------------------------------------------------------------- 会员域
    def grant_membership(self, reader_id: str, plan: str, loan_quota: int,
                         valid_from: str, valid_until: str, *,
                         at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        return self._append("MEMBERSHIP_GRANTED", {
            "reader_id": reader_id, "plan": plan, "loan_quota": loan_quota,
            "valid_from": valid_from, "valid_until": valid_until,
        }, at or now())

    # ---------------------------------------------------------------- 反馈域
    def record_feedback(self, feedback_id: str, family_id: str, reader_id: str,
                        title_id: str, rating: int, note: str, *,
                        loan_id: str | None = None, author_event_id: str | None = None,
                        actor_id: str | None = None, on_behalf: bool = False,
                        at: datetime | None = None) -> dict:
        self._require_reader(reader_id)
        self._resolve_actor(reader_id, actor_id, on_behalf)
        self._require_consent(reader_id, "feedback")
        if title_id not in self.titles:
            raise DomainError("书目不存在")
        if loan_id and loan_id not in self.loans:
            raise DomainError("借阅不存在")
        if author_event_id and author_event_id not in self.author_events:
            raise DomainError("活动不存在")
        return self._append("FAMILY_FEEDBACK_RECORDED", {
            "feedback_id": feedback_id, "family_id": family_id, "reader_id": reader_id,
            "title_id": title_id, "loan_id": loan_id, "author_event_id": author_event_id,
            "rating": rating, "note": note, "at": (at or now()).isoformat(),
        }, at or now())
