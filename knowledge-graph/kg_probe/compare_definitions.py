#!/usr/bin/env python3
"""Compare generated code definitions with registered metric definitions."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_templates(path: Path) -> dict[str, dict]:
    raw = json.loads(path.read_text())
    return {item["template_id"]: item for item in raw["templates"]}


def parse_json_content(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def render(template: str, variables: dict[str, str]) -> str:
    value = template
    for key, replacement in variables.items():
        value = value.replace("{{" + key + "}}", replacement)
    return value


def mock_comparison(definition_row: dict, evidence: dict) -> dict:
    registry = evidence.get("registered_definitions", [])
    code_status = definition_row.get("status")
    if code_status != "ok" or not definition_row.get("definition"):
        status = "code_evidence_insufficient"
    elif not registry:
        status = "registry_missing"
    elif definition_row.get("definition", {}).get("confidence") == "low":
        status = "code_evidence_insufficient"
    else:
        status = "partially_consistent"
    return {
        "status": status,
        "agreement_points": [],
        "conflict_points": [],
        "missing_in_registry": [] if registry else ["未找到登记口径。"],
        "insufficient_code_evidence": [] if status != "code_evidence_insufficient" else ["代码证据不足，mock 模式不做实质冲突判断。"],
        "recommended_definition": definition_row.get("definition", {}).get("summary", ""),
        "confidence": "low" if definition_row.get("provider") == "mock" else "medium",
    }


def post_chat_completion(base_url: str, api_key: str, model: str, messages: list[dict], timeout: int, max_tokens: int) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    content = raw["choices"][0]["message"]["content"]
    return {"raw_response": raw, "parsed": parse_json_content(content)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--templates", default=str(Path(__file__).with_name("llm_prompt_templates.json")))
    parser.add_argument("--provider", choices=["mock", "openai-compatible"], default="mock")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=4096)
    args = parser.parse_args()

    base = Path(args.project_dir)
    llm_dir = base / "llm"
    definitions = load_jsonl(llm_dir / "code_definitions.jsonl")
    evidences = {item["bundle_id"]: item["payload"] for item in load_jsonl(llm_dir / "evidence_bundles.jsonl")}
    if args.limit:
        definitions = definitions[: args.limit]

    template = load_templates(Path(args.templates))["metric_definition_compare.v1"]
    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("codex_ds_API_KEY")
        or os.environ.get("CODEX_DS_API_KEY")
    )
    if args.provider != "mock" and not api_key:
        raise SystemExit("LLM_API_KEY or OPENAI_API_KEY is required for openai-compatible provider")

    out_path = llm_dir / "definition_comparisons.jsonl"
    rows = []
    done_definition_ids = set()
    if args.resume and out_path.exists():
        rows = [item for item in load_jsonl(out_path) if item.get("status") == "ok"]
        done_definition_ids = {item.get("definition_id") for item in rows}

    def process_definition(row: dict) -> dict:
        evidence = evidences.get(row["evidence_bundle_id"], {})
        comparison_input = {
            "metric": evidence.get("metric", {}),
            "code_definition": row.get("definition", {}),
            "registered_definitions": evidence.get("registered_definitions", []),
            "evidence_bundle_id": row["evidence_bundle_id"],
        }
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "ok"
        error = None
        raw_response = None
        try:
            if args.provider == "mock":
                parsed = mock_comparison(row, evidence)
                raw_response = {"provider": "mock"}
            else:
                messages = [
                    {"role": "system", "content": template["system"]},
                    {
                        "role": "user",
                        "content": render(
                            template["user"],
                            {"comparison_json": json.dumps(comparison_input, ensure_ascii=False, sort_keys=True)},
                        ),
                    },
                ]
                result = post_chat_completion(args.base_url, api_key, args.model, messages, args.timeout, args.max_tokens)
                parsed = result["parsed"]
                raw_response = result["raw_response"]
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
            KeyError,
            TimeoutError,
        ) as exc:
            status = "error"
            error = str(exc)
            parsed = {}

        return {
            "comparison_id": "definition_comparison:" + row["definition_id"].split(":", 1)[1],
            "definition_id": row["definition_id"],
            "generation_id": row["generation_id"],
            "metric_node_id": row["metric_node_id"],
            "metric_id": row["metric_id"],
            "evidence_bundle_id": row["evidence_bundle_id"],
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "prompt_template_id": template["template_id"],
            "prompt_template_version": template["version"],
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "error": error,
            "comparison": parsed,
            "raw_response": raw_response,
        }

    pending_definitions = [row for row in definitions if row["definition_id"] not in done_definition_ids]
    if args.workers > 1 and pending_definitions:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_definition, row): row for row in pending_definitions}
            for future in as_completed(futures):
                row = futures[future]
                rows.append(future.result())
                out_path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows))
                if args.progress_every and len(rows) % args.progress_every == 0:
                    print(
                        json.dumps(
                            {
                                "progress": len(rows),
                                "total": len(definitions),
                                "last_metric_id": row.get("metric_id"),
                                "error_count": sum(1 for item in rows if item["status"] != "ok"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    else:
        for row in pending_definitions:
            rows.append(process_definition(row))
            out_path.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in rows))
            if args.progress_every and len(rows) % args.progress_every == 0:
                print(
                    json.dumps(
                        {
                            "progress": len(rows),
                            "total": len(definitions),
                            "last_metric_id": row.get("metric_id"),
                            "error_count": sum(1 for item in rows if item["status"] != "ok"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if args.sleep:
                time.sleep(args.sleep)

    out_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    print(json.dumps({"comparison_count": len(rows), "error_count": sum(1 for r in rows if r["status"] != "ok"), "output_path": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
