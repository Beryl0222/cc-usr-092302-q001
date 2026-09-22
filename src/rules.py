"""家庭持续阅读接力 · 关键业务规则（纯函数折叠）。

所有规则都以"事件流折叠"方式工作：事实只追加，当前状态是折叠结果。
本模块不依赖网络、数据库与第三方库，门店端、家庭端、运营端与测试共用同一份语义。

约定：事件为 dict，形如 {"kind", "occurred_at", "request_id", "payload", ...}，
与 contracts/event.schema.json 的信封一致（本模块不重复信封校验）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


class Rejection(Exception):
    """事件被业务规则拒绝（不进入折叠、不产生任何效果）。"""


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _d(value: str) -> date:
    return date.fromisoformat(value)


# ---------------------------------------------------------------------------
# 1. 年龄与适龄视图
# ---------------------------------------------------------------------------


def age_on(birth_date: str, on: date) -> int:
    """按出生日期在查询时刻动态计算年龄（不存静态年龄）。"""
    bd = _d(birth_date)
    return on.year - bd.year - ((on.month, on.day) < (bd.month, bd.day))


def visible_for_age(min_age: int, max_age: int, age: int) -> bool:
    return min_age <= age <= max_age


def child_view(entries: list[dict], birth_date: str, on: date) -> list[dict]:
    """儿童视图：只保留适龄、且推荐/内容未被撤回的条目。

    任何一方撤回（孩子本人或监护人代办）都使内容从孩子视图消失；
    两者的差别在于监护人不能越过孩子的撤回把内容重新开启（见 consent_active）。
    """
    age = age_on(birth_date, on)
    out = []
    for e in entries:
        if not visible_for_age(e["min_age"], e["max_age"], age):
            continue
        if e.get("withdrawn_by_relation") in ("self", "guardian"):
            continue
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# 2. 隐私授权折叠：儿童本人撤回优先于监护人后续授予
# ---------------------------------------------------------------------------


def consent_active(events: list[dict], person_id: str, scope: str, at: datetime) -> bool:
    """折叠某人的授权事件，判断 scope 在 at 时刻是否有效。

    规则：
    - 授予带有效期与范围；任一有效授予使 scope 生效；
    - 撤回后失效；
    - 儿童本人(self)一旦撤回，监护人(person_id 之外的 granted_by)之后的授予不能恢复；
      只有儿童本人重新授予才能恢复（孩子的撤回选择不被越过）。
    """
    active = False
    self_withdrawn = False
    for ev in events:
        p = ev.get("payload", {})
        if p.get("person_id") != person_id or scope not in p.get("scopes", []):
            continue
        kind = ev["kind"]
        when = _dt(ev["occurred_at"])
        if when > at:
            continue
        if kind == "CONSENT_GRANTED":
            if self_withdrawn and p.get("granted_by") != person_id:
                # 家长不能越过孩子的撤回
                continue
            if _dt(p["valid_from"]) <= at <= _dt(p["valid_to"]):
                active = True
                self_withdrawn = False
        elif kind == "CONSENT_WITHDRAWN":
            active = False
            if p.get("withdrawn_by_relation") == "self":
                self_withdrawn = True
    return active


def can_act_on_behalf(
    events: list[dict], guardian_id: str, child_id: str, scope: str, at: datetime
) -> bool:
    """监护人是否可在 scope 内为孩子代办：存在当时有效、覆盖该 scope 的监护关系。"""
    for ev in events:
        p = ev.get("payload", {})
        if ev["kind"] == "GUARDIANSHIP_GRANTED" and p.get("guardian_id") == guardian_id \
                and p.get("child_id") == child_id and scope in p.get("scopes", []) \
                and _dt(p["valid_from"]) <= at <= _dt(p["valid_to"]):
            return True
        if ev["kind"] == "GUARDIANSHIP_REVOKED" and p.get("guardian_id") == guardian_id \
                and p.get("child_id") == child_id:
            return False
    return False


def assert_actor_allowed(
    events: list[dict], *, actor_id: str, on_behalf_of: str | None, scope: str, at: datetime
) -> None:
    """代行护栏：本人操作总是允许；代办必须有有效监护授权。"""
    if on_behalf_of is None or actor_id == on_behalf_of:
        return
    if not can_act_on_behalf(events, actor_id, on_behalf_of, scope, at):
        raise Rejection(
            f"guardian {actor_id} lacks active '{scope}' grant for child {on_behalf_of}"
        )
    if not consent_active(events, on_behalf_of, scope, at):
        raise Rejection(f"child {on_behalf_of} consent for '{scope}' is not active")


# ---------------------------------------------------------------------------
# 3. 偏好折叠：孩子撤回的偏好不再进入推荐
# ---------------------------------------------------------------------------


def active_interests(events: list[dict], person_id: str) -> set[str]:
    """折叠偏好声明/撤回。孩子本人撤回后，家长之后的代录不能把该兴趣加回。"""
    kept: set[str] = set()
    self_removed: set[str] = set()
    for ev in events:
        p = ev.get("payload", {})
        if p.get("person_id") != person_id:
            continue
        interests = p.get("interests", [])
        if ev["kind"] == "PREFERENCE_DECLARED":
            by_self = ev.get("actor_id") == person_id
            for tag in interests:
                if tag in self_removed and not by_self:
                    continue
                kept.add(tag)
                self_removed.discard(tag)
        elif ev["kind"] == "PREFERENCE_WITHDRAWN":
            kept.difference_update(interests)
            if p.get("withdrawn_by") == person_id:
                self_removed.update(interests)
    return kept


# ---------------------------------------------------------------------------
# 4. 推荐理由快照：书目说明更新后仍可逐字还原
# ---------------------------------------------------------------------------


def recommendation_as_issued(rec_event: dict) -> dict:
    """取推荐发出时的快照（说明、适龄区间、理由、依据版本），不读当前书目表。"""
    if rec_event["kind"] != "RECOMMENDATION_MADE":
        raise Rejection("not a RECOMMENDATION_MADE event")
    p = rec_event["payload"]
    return {
        "edition_id": p["edition_id"],
        "description": p["edition_description_snapshot"],
        "age_gating": p["age_gating_snapshot"],
        "reason_codes": list(p["reason_codes"]),
        "reason_text": p["reason_text"],
        "basis_versions": dict(p["basis_versions"]),
    }


# ---------------------------------------------------------------------------
# 5. 幂等日志：重复扫码 / 断网补传只生效一次
# ---------------------------------------------------------------------------


class IdempotencyLog:
    """request_id -> 首次受理的 event_id。重复请求被识别为重复，绝不产生第二次效果。"""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def admit(self, event: dict) -> bool:
        rid = event.get("request_id")
        if rid is None:
            return True  # 系统事件无 request_id，不参与外部幂等
        if rid in self._seen:
            return False
        self._seen[rid] = event.get("event_id", rid)
        return True

    def first_event_id(self, request_id: str) -> str | None:
        return self._seen.get(request_id)

    @classmethod
    def fold(cls, events: list[dict]) -> "IdempotencyLog":
        log = cls()
        for ev in events:
            log.admit(ev)
        return log


# ---------------------------------------------------------------------------
# 6. 单册状态机 + 借阅次数：重复补传不重复扣库存/次数
# ---------------------------------------------------------------------------

ITEM_AVAILABLE = "AVAILABLE"
ITEM_RESERVED = "RESERVED"
ITEM_ON_LOAN = "ON_LOAN"
ITEM_IN_TRANSIT = "IN_TRANSIT"
ITEM_AT_HOLDING = "AT_HOLDING"
ITEM_MISSING = "MISSING"

_ALLOWED_TRANSITIONS = {
    "LOAN_RESERVED": {ITEM_AVAILABLE: ITEM_RESERVED},
    "LOAN_PICKED_UP": {
        ITEM_RESERVED: ITEM_ON_LOAN,
        ITEM_AVAILABLE: ITEM_ON_LOAN,
    },
    "LOAN_IN_TRANSIT": {
        ITEM_ON_LOAN: ITEM_IN_TRANSIT,
        ITEM_AT_HOLDING: ITEM_IN_TRANSIT,
    },
    "LOAN_ARRIVED": {ITEM_IN_TRANSIT: ITEM_AT_HOLDING},
    "LOAN_RETURNED": {
        ITEM_ON_LOAN: ITEM_AVAILABLE,
        ITEM_IN_TRANSIT: ITEM_AVAILABLE,
        ITEM_AT_HOLDING: ITEM_AVAILABLE,
    },
    "LOAN_MISSING_MARKED": {
        ITEM_RESERVED: ITEM_MISSING,
        ITEM_ON_LOAN: ITEM_MISSING,
        ITEM_IN_TRANSIT: ITEM_MISSING,
        ITEM_AT_HOLDING: ITEM_MISSING,
    },
}

_LOAN_LIFECYCLE_KINDS = set(_ALLOWED_TRANSITIONS)


@dataclass
class ItemState:
    item_id: str
    state: str = ITEM_AVAILABLE
    current_loan_id: str | None = None
    overdue_loans: set[str] = field(default_factory=set)
    loans_picked_up: int = 0  # 以"被受理的不同借阅单"计，重复 request 不计数
    rejected: list[dict] = field(default_factory=list)


def fold_items(events: list[dict]) -> dict[str, ItemState]:
    """折叠借阅相关事件为每个单册的当前状态。

    - 重复 request_id：拒绝为 duplicate_request，状态与计数不变；
    - 非法状态迁移（如在途册再借出）：拒绝为 illegal_transition；
    - LOAN_OVERDUE_MARKED 对同一借阅单幂等。
    """
    idem = IdempotencyLog()
    items: dict[str, ItemState] = {}

    def state_of(item_id: str) -> ItemState:
        return items.setdefault(item_id, ItemState(item_id=item_id))

    for ev in events:
        kind = ev["kind"]
        p = ev.get("payload", {})
        if kind == "ITEM_RECORDED":
            state_of(p["item_id"]).state = ITEM_AVAILABLE
            continue

        if kind == "LOAN_OVERDUE_MARKED":
            st = state_of(_item_of_loan(events, p["loan_id"], items))
            if p["loan_id"] not in st.overdue_loans:
                st.overdue_loans.add(p["loan_id"])
            continue

        if kind not in _LOAN_LIFECYCLE_KINDS:
            continue

        item_id = p.get("item_id")
        if item_id is None:
            continue
        st = state_of(item_id)

        if not idem.admit(ev):
            st.rejected.append(
                {"event_id": ev.get("event_id"), "reason": "duplicate_request"}
            )
            continue

        target = _ALLOWED_TRANSITIONS[kind].get(st.state)
        if target is None:
            st.rejected.append(
                {
                    "event_id": ev.get("event_id"),
                    "reason": "illegal_transition",
                    "from": st.state,
                    "kind": kind,
                }
            )
            continue

        st.state = target
        if kind == "LOAN_PICKED_UP":
            st.current_loan_id = p["loan_id"]
            st.loans_picked_up += 1
        elif kind == "LOAN_RESERVED":
            st.current_loan_id = p["loan_id"]
        elif kind in ("LOAN_RETURNED", "LOAN_MISSING_MARKED"):
            st.current_loan_id = None
    return items


def _item_of_loan(events: list[dict], loan_id: str, known: dict[str, ItemState]) -> str:
    for st in known.values():
        if st.current_loan_id == loan_id or loan_id in st.overdue_loans:
            return st.item_id
    for ev in events:
        p = ev.get("payload", {})
        if p.get("loan_id") == loan_id and p.get("item_id"):
            return p["item_id"]
    raise Rejection(f"unknown loan {loan_id}")


# ---------------------------------------------------------------------------
# 7. 临时闭店：只重排命中闭店区间的未完成取/还安排
# ---------------------------------------------------------------------------


def plans_affected_by_closure(closure: dict, plans: list[dict]) -> list[dict]:
    """闭店事件影响面。

    closure: STORE_TEMPORARILY_CLOSED 的 payload；
    plans: 未完成安排 {loan_id, kind: pickup|return, store_id, planned_at(datetime)}。
    仅当 门店命中 且 计划时间落在闭店区间内 才受影响；
    其他门店、其他时间、已完成安排一律不动。
    """
    cf, ct = _dt(closure["closed_from"]), _dt(closure["closed_to"])
    affected = []
    for plan in plans:
        if plan.get("status") == "done":
            continue
        if plan["store_id"] != closure["store_id"]:
            continue
        at = plan["planned_at"]
        at = at if isinstance(at, datetime) else _dt(at)
        if cf <= at < ct:
            affected.append(plan)
    return affected


# ---------------------------------------------------------------------------
# 8. 活动报名 / 候补 / 会员权益：三本账分开
# ---------------------------------------------------------------------------


@dataclass
class EventRegistration:
    registered: set[str] = field(default_factory=set)
    cancelled: set[str] = field(default_factory=set)
    waitlist: list[str] = field(default_factory=list)
    promoted: set[str] = field(default_factory=set)
    attended: set[str] = field(default_factory=set)

    @property
    def seats_taken(self) -> int:
        """出席名额只数"已报名且未取消"，与候补、权益无关。"""
        return len(self.registered - self.cancelled)


def fold_event(events: list[dict], author_event_id: str) -> EventRegistration:
    reg = EventRegistration()
    idem = IdempotencyLog()
    for ev in events:
        p = ev.get("payload", {})
        if p.get("author_event_id") != author_event_id:
            continue
        if not idem.admit(ev):
            continue
        kind, reader = ev["kind"], p.get("reader_id")
        if kind == "EVENT_REGISTERED":
            reg.registered.add(reader)
        elif kind == "EVENT_REGISTRATION_CANCELLED":
            reg.cancelled.add(reader)
        elif kind == "EVENT_WAITLISTED":
            if reader not in reg.waitlist:
                reg.waitlist.append(reader)
        elif kind == "EVENT_REGISTRATION_PROMOTED":
            reg.promoted.add(reader)
            if reader in reg.waitlist:
                reg.waitlist.remove(reader)
        elif kind == "EVENT_ATTENDED":
            reg.attended.add(reader)
    return reg


def decide_registration(state: EventRegistration, capacity: int) -> str:
    """名额未满 -> register；已满 -> waitlist。报名系统不算权益、不算借阅。"""
    return "EVENT_REGISTERED" if state.seats_taken < capacity else "EVENT_WAITLISTED"


@dataclass
class BenefitLedger:
    used: dict[str, set[str]] = field(default_factory=dict)
    restored: dict[str, set[str]] = field(default_factory=dict)

    def net_used(self, benefit_type: str) -> int:
        return len(self.used.get(benefit_type, set()) - self.restored.get(benefit_type, set()))


def fold_benefits(events: list[dict]) -> BenefitLedger:
    """权益账按 (benefit_type, ref) 去重；取消时由 BENEFIT_RESTORED 独立返还。

    与报名账、借阅账完全分离：这里的计数不会出现在另外两本账里，反之亦然。
    """
    ledger = BenefitLedger(used={}, restored={})
    for ev in events:
        p = ev.get("payload", {})
        if ev["kind"] == "BENEFIT_USED":
            ledger.used.setdefault(p["benefit_type"], set()).add(p["ref"])
        elif ev["kind"] == "BENEFIT_RESTORED":
            ledger.restored.setdefault(p["benefit_type"], set()).add(p["ref"])
    return ledger


# ---------------------------------------------------------------------------
# 9. 阅读指导：资格来自角色授权，禁止任何消费维度
# ---------------------------------------------------------------------------

# 任何形如"消费/充值/积分"的入参一旦出现，直接拒绝——防止门槛被悄悄塞回。
FORBIDDEN_SPEND_FIELDS = {
    "spend_amount",
    "total_spend",
    "purchase_amount",
    "consumption_amount",
    "recharge_amount",
    "loyalty_points",
    "membership_fee_paid",
}


def assert_guidance_allowed(
    *,
    guide_qualified: bool,
    consent_scope_active: bool,
    age_appropriate: bool,
    factors: dict | None = None,
) -> None:
    factors = factors or {}
    forbidden = sorted(set(factors) & FORBIDDEN_SPEND_FIELDS)
    if forbidden:
        raise Rejection(
            f"reading guidance must never depend on spend; forbidden factors: {forbidden}"
        )
    if not guide_qualified:
        raise Rejection("guide qualification is missing or expired")
    if not consent_scope_active:
        raise Rejection("guardianship/consent scope 'guidance' is not active")
    if not age_appropriate:
        raise Rejection("content is not age-appropriate for the reader")


# ---------------------------------------------------------------------------
# 10. 对谈 → 借阅 → 反馈 的接力追踪链
# ---------------------------------------------------------------------------

_STAGE_ORDER = {
    "EVENT_ATTENDED": 0,
    "RECOMMENDATION_MADE": 1,
    "LOAN_RESERVED": 2,
    "LOAN_PICKED_UP": 2,
    "LOAN_IN_TRANSIT": 2,
    "LOAN_RETURNED": 2,
    "GUIDANCE_SESSION_HELD": 3,
    "FAMILY_FEEDBACK_RECORDED": 4,
}


def trace_relay(events: list[dict], correlation_id: str) -> list[dict]:
    """按一场对谈的 correlation_id 取全链，按发生时间排序。

    返回精简节点：kind / reader_id / occurred_at / 关键引用（版本、单册、活动）。
    运营可从一次对谈追到后续推荐版本、借阅、指导与家庭反馈。
    """
    chain = []
    for ev in events:
        if ev.get("correlation_id") != correlation_id:
            continue
        if ev["kind"] not in _STAGE_ORDER:
            continue
        p = ev.get("payload", {})
        chain.append(
            {
                "stage": _STAGE_ORDER[ev["kind"]],
                "kind": ev["kind"],
                "reader_id": p.get("reader_id"),
                "occurred_at": ev["occurred_at"],
                "ref": p.get("edition_id")
                or p.get("item_id")
                or p.get("session_id")
                or p.get("feedback_id")
                or p.get("author_event_id"),
                "event_id": ev.get("event_id"),
            }
        )
    chain.sort(key=lambda x: (_dt(x["occurred_at"]), x["stage"]))
    return chain


def relay_edges(events: list[dict], correlation_id: str) -> list[tuple[str, str]]:
    """取 causation 直连边（前一事件 -> 本事件），用于核对链路没有断头。"""
    edges = []
    for ev in events:
        if ev.get("correlation_id") != correlation_id:
            continue
        if ev.get("causation_id"):
            edges.append((ev["causation_id"], ev.get("event_id")))
    return edges
