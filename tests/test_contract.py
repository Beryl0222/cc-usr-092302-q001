import json
import unittest
from pathlib import Path

from src.contract import load_catalog, validate

ROOT = Path(__file__).parents[1]


def load_json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


class ContractTest(unittest.TestCase):
    def test_sample(self):
        data = load_json("fixtures/event.json")
        self.assertEqual(validate(data), [])

    def test_relay_fixture_events_all_valid(self):
        relay = load_json("fixtures/relay.json")
        for event in relay["events"]:
            self.assertEqual(validate(event), [], f"event {event['event_id']} 校验失败")

    def test_unknown_kind(self):
        data = load_json("fixtures/event.json")
        data["kind"] = "NO_SUCH_KIND"
        errors = validate(data)
        self.assertTrue(any("unknown kind" in e for e in errors))

    def test_context_mismatch(self):
        data = load_json("fixtures/event.json")
        data["context"] = "loan"
        errors = validate(data)
        self.assertTrue(any("context mismatch" in e for e in errors))

    def test_external_event_requires_request_id(self):
        relay = load_json("fixtures/relay.json")
        event = next(e for e in relay["events"] if e["kind"] == "LOAN_PICKED_UP")
        del event["request_id"]
        errors = validate(event)
        self.assertTrue(any("request_id" in e for e in errors))

    def test_payload_missing_field_and_bad_type(self):
        event = {
            "event_id": "e-wl-test",
            "kind": "EVENT_WAITLISTED",
            "context": "event",
            "occurred_at": "2026-09-05T10:00:00+08:00",
            "subject_id": "evt-x",
            "actor_id": "person-zl-001",
            "request_id": "req-wl-test",
            "version": 1,
            "payload": {
                "author_event_id": "evt-x",
                "reader_id": "person-lxm-001",
                "position": 1,
            },
        }
        self.assertEqual(validate(event), [])
        # 缺 position
        event["payload"].pop("position")
        errors = validate(event)
        self.assertTrue(any("payload missing field: position" in e for e in errors))
        # 类型错误
        event["payload"]["position"] = "two"
        errors = validate(event)
        self.assertTrue(any("position expects int" in e for e in errors))

    def test_catalog_and_schema_enums_are_in_sync(self):
        catalog = load_catalog()
        schema = load_json("contracts/event.schema.json")
        kind_enum = set(schema["properties"]["kind"]["enum"])
        context_enum = set(schema["properties"]["context"]["enum"])

        catalog_kinds = set(catalog["events"])
        self.assertEqual(catalog_kinds, kind_enum, "目录 kind 与 schema 枚举必须一一对应")
        catalog_contexts = {spec["context"] for spec in catalog["events"].values()}
        self.assertTrue(catalog_contexts <= context_enum)


if __name__ == "__main__":
    unittest.main()
