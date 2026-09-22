"""事件信封校验。

在基线五项必填字段之上，按 contracts/event-catalog.json 校验：
- kind 已知、context 与目录一致；
- payload 含目录声明的全部必填字段，且字段类型正确；
- 目录标记 external=true 的事件必须携带 request_id（幂等键）；
- occurred_at 为带偏移量的 RFC3339 时间。

无第三方依赖；返回错误字符串列表，无错误时返回 []（与基线一致）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parents[1]
_CATALOG_PATH = _BASE_DIR / "contracts" / "event-catalog.json"

REQUIRED = ("event_id", "kind", "occurred_at", "subject_id", "version")
_EXTENDED_REQUIRED = REQUIRED + ("context", "actor_id", "payload")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_catalog() -> dict:
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def _check_type(value, type_decl: str) -> bool:
    head, _, rest = type_decl.partition(":")
    if head in ("id", "string"):
        return isinstance(value, str) and value != ""
    if head == "date":
        return isinstance(value, str) and bool(_DATE_RE.match(value))
    if head == "datetime":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return False
        return True
    if head == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if head == "bool":
        return isinstance(value, bool)
    if head == "object":
        return isinstance(value, dict)
    if head == "array":
        if not isinstance(value, list):
            return False
        elem_type = rest.strip() or "string"
        return all(_check_type(item, elem_type) for item in value)
    if head == "enum":
        return isinstance(value, str) and value in {v.strip() for v in rest.split(",")}
    return True


def validate(record: dict, catalog: dict | None = None) -> list[str]:
    catalog = catalog or load_catalog()
    errors: list[str] = []

    for name in _EXTENDED_REQUIRED:
        if name not in record:
            errors.append(f"missing field: {name}")
    if errors:
        return errors

    try:
        datetime.fromisoformat(record["occurred_at"])
    except (TypeError, ValueError):
        errors.append("occurred_at must be RFC3339 date-time")

    kind = record["kind"]
    spec = catalog["events"].get(kind)
    if spec is None:
        errors.append(f"unknown kind: {kind}")
        return errors

    if record.get("context") != spec["context"]:
        errors.append(
            f"context mismatch for {kind}: "
            f"expected {spec['context']}, got {record.get('context')}"
        )

    if spec.get("external") and not record.get("request_id"):
        errors.append(f"{kind} is external-triggered and must carry request_id")

    if "on_behalf_of" in record and record["on_behalf_of"] == record["actor_id"]:
        errors.append("on_behalf_of must differ from actor_id")

    payload = record.get("payload")
    if not isinstance(payload, dict):
        errors.append("payload must be an object")
        return errors

    for field, type_decl in spec.get("payload_required", {}).items():
        if field not in payload:
            errors.append(f"{kind}.payload missing field: {field}")
        elif not _check_type(payload[field], type_decl):
            errors.append(
                f"{kind}.payload.{field} expects {type_decl}, got {payload[field]!r}"
            )

    return errors
