from __future__ import annotations

import uuid
import base64
import json
from datetime import datetime
from typing import Any


VALID_STATUSES = {"ok", "partial", "ambiguous", "not_found", "error"}
VALID_MODES = {"strict", "balanced", "exploratory"}
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}


def request_id() -> str:
    return f"req_{uuid.uuid4().hex[:20]}"


def encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": max(0, int(offset))}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return max(0, int(payload.get("offset", 0)))
    except Exception as exc:  # noqa: BLE001
        raise ValueError("cursor is invalid") from exc


def warning(code: str, message: str, severity: str = "warning", related_entity_ids=None) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "related_entity_ids": related_entity_ids or [],
    }


def response(
    primitive: str,
    *,
    status: str = "ok",
    answer: str = "",
    data: dict | None = None,
    entities: list | None = None,
    paths: list | None = None,
    evidence: list | None = None,
    warnings: list | None = None,
    graph_context: dict | None = None,
    page: dict | None = None,
    diagnostics: dict | None = None,
    req_id: str | None = None,
) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported response status: {status}")
    return {
        "request_id": req_id or request_id(),
        "primitive": primitive,
        "status": status,
        "answer": answer,
        "data": data or {},
        "entities": entities or [],
        "paths": paths or [],
        "evidence": evidence or [],
        "warnings": warnings or [],
        "graph_context": graph_context or {},
        "page": page or {"limit": 0, "returned": 0, "next_cursor": None, "has_more": False},
        "diagnostics": diagnostics or {},
    }


def validate_common(payload: dict) -> dict:
    result = dict(payload)
    mode = result.get("mode", "balanced")
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}")
    result["mode"] = mode
    max_hops = int(result.get("max_hops", 20))
    if not 1 <= max_hops <= 50:
        raise ValueError("max_hops must be between 1 and 50")
    result["max_hops"] = max_hops
    limit = int(result.get("limit", 100))
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    result["limit"] = limit
    result["cursor_offset"] = decode_cursor(result.get("cursor"))
    result["include_evidence"] = bool(result.get("include_evidence", True))
    result["include_properties"] = bool(result.get("include_properties", True))
    confidence = result.get("confidence_min", "medium")
    if confidence not in CONFIDENCE_RANK:
        raise ValueError("confidence_min must be low, medium, or high")
    result["confidence_min"] = confidence
    return result


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compact_properties(properties: dict, include_properties: bool = True) -> dict:
    if not include_properties:
        return {}
    return {key: value for key, value in properties.items() if value is not None}
