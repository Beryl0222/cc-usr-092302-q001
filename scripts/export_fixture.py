"""导出端到端事件流样例到 fixtures/family-reading-flow.json。

运行：python -m scripts.export_fixture
场景：8 岁孩子参加武汉作家对谈 → 跨店从北京调拨《小熊的森林》→ 闭店改约
→ 取阅、逾期判定、归还 → 家庭反馈；中间还包含一次书目说明修订后的推荐回放。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from src import readmodels as rm
from src.service import ReadingService

T = datetime.fromisoformat


def build() -> tuple[ReadingService, dict]:
    svc = ReadingService()
    svc.register_store("store-bj", "北京", "句象书店北京店", at=T("2026-08-01T09:00+08:00"))
    svc.register_store("store-wh", "武汉", "句象书店武汉店", at=T("2026-08-01T09:00+08:00"))
    svc.register_store("store-cs", "长沙", "句象书店长沙店", at=T("2026-08-01T09:00+08:00"))
    svc.register_reader("r-parent", "fam-1", "林一", "1988-03-10", ["历史"],
                        at=T("2026-08-02T10:00+08:00"))
    svc.register_reader("r-kid", "fam-1", "林小夏", "2018-06-01", ["宇宙", "动物"],
                        at=T("2026-08-02T10:05+08:00"))
    svc.declare_guardianship("fam-1", "r-parent", "r-kid", "母亲",
                             at=T("2026-08-02T10:10+08:00"))
    svc.grant_consent("c-kid-loan", "r-kid", "loan", actor_id="r-parent",
                      on_behalf=True, at=T("2026-08-02T10:15+08:00"))
    svc.grant_consent("c-kid-rec", "r-kid", "recommendation", actor_id="r-kid",
                      at=T("2026-08-02T10:16+08:00"))
    svc.grant_consent("c-kid-fb", "r-kid", "feedback", actor_id="r-kid",
                      at=T("2026-08-02T10:17+08:00"))
    svc.grant_consent("c-kid-event", "r-kid", "event", actor_id="r-parent",
                      on_behalf=True, at=T("2026-08-02T10:18+08:00"))

    svc.register_title("t-bear", "小熊的森林", "某甲", 4, 8, ["动物"],
                       "初版说明：森林里的小熊。", at=T("2026-08-03T09:20+08:00"))
    svc.publish_edition("e-bear", "t-bear", "978-3-0", "未来出版社",
                        at=T("2026-08-03T09:25+08:00"))
    svc.add_copies("b-bj-bear", "store-bj", "e-bear", 1,
                   at=T("2026-08-04T09:00+08:00"))

    svc.schedule_author_event("ev-wh", "store-wh", "武汉作家对谈·动物与童年", "某甲",
                              T("2026-09-12T14:00+08:00"), capacity=20,
                              at=T("2026-09-01T09:00+08:00"))
    svc.register_for_event("er-1", "ev-wh", "r-kid", actor_id="r-parent",
                           on_behalf=True, at=T("2026-09-02T09:00+08:00"))
    svc.record_attendance("ev-wh", "r-kid", at=T("2026-09-12T15:30+08:00"))

    svc.recommend_title("rec-1", "r-kid", "t-bear",
                        "对谈后对动物故事意犹未尽，4-8 岁适龄", "author_event:ev-wh",
                        at=T("2026-09-12T16:00+08:00"))
    svc.request_loan("req-cross", "r-kid", "e-bear", "store-wh",
                     actor_id="r-parent", on_behalf=True,
                     pickup_at=T("2026-09-18T10:00+08:00"),
                     at=T("2026-09-13T10:00+08:00"))
    # 武汉店 9/17-9/19 临时闭店：只改这笔落在窗口内的取书安排到长沙店
    svc.schedule_closure("cl-mid", "store-wh", T("2026-09-17T00:00+08:00"),
                         T("2026-09-19T22:00+08:00"), "场馆检修",
                         alternate_store_id="store-cs",
                         at=T("2026-09-14T09:00+08:00"))
    svc.mark_loan_arrived("loan-req-cross", at=T("2026-09-20T11:00+08:00"))
    svc.pick_up("loan-req-cross", at=T("2026-09-20T11:05+08:00"))
    # 说明在推荐后更新，但推荐理由可按冻结修订版还原
    svc.update_title_description("t-bear", "修订说明：小熊学会了辨认星座。",
                                 "增补星座情节", at=T("2026-09-25T10:00+08:00"))
    svc.record_feedback("fb-1", "fam-1", "r-kid", "t-bear", 5,
                        "共读了两遍，孩子开始追问星座。",
                        loan_id="loan-req-cross", author_event_id="ev-wh",
                        actor_id="r-kid", at=T("2026-10-05T19:00+08:00"))

    summary = {
        "roster": rm.event_roster(svc, "ev-wh"),
        "loan": rm.loan_view(svc, "loan-req-cross", date(2026, 10, 6)),
        "recommendation_as_made": rm.recommendation_as_made(svc, "rec-1"),
        "trace": rm.trace_event_followup(svc, "ev-wh", date(2026, 10, 6)),
    }
    return svc, summary


def main() -> None:
    svc, summary = build()
    out = Path(__file__).resolve().parents[1] / "fixtures" / "family-reading-flow.json"
    out.write_text(json.dumps(
        {"events": svc.events, "projections_at": "2026-10-06", "summary": summary},
        ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    print(f"已写出 {len(svc.events)} 个事件到 {out}")


if __name__ == "__main__":
    main()
