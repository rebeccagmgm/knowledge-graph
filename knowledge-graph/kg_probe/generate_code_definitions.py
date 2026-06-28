#!/usr/bin/env python3
"""Generate code-first metric definitions from LLM request JSONL."""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))


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


def extract_evidence_from_request(request: dict) -> dict:
    user_content = request["messages"][1]["content"]
    marker = "证据 JSON:\n"
    start = user_content.index(marker) + len(marker)
    end = user_content.index("\n\n请输出 JSON", start)
    return json.loads(user_content[start:end])


def mock_definition(request: dict) -> dict:
    evidence = extract_evidence_from_request(request)
    metric = evidence.get("metric", {})
    read_tables = [item.get("name") for item in evidence.get("read_tables", []) if item.get("name")]
    write_tables = [item.get("name") for item in evidence.get("write_tables", []) if item.get("name")]
    stored_in = [item.get("name") for item in evidence.get("stored_in", []) if item.get("name")]
    tasks = [str(item.get("task_id") or item.get("id")) for item in evidence.get("computed_by", [])]
    has_sql = bool(evidence.get("sql_statements"))
    confidence = "medium" if has_sql else "low"
    return {
        "summary": f"{metric.get('chinese_name') or metric.get('english_name_clean') or metric.get('metric_id')} 的代码口径草稿。",
        "calculation_logic": "mock 模式仅汇总结构化证据；真实计算逻辑需切换 LLM provider 生成。",
        "source_tables": read_tables,
        "target_table": (write_tables or stored_in or [None])[0],
        "filters": [],
        "grain": metric.get("index_gran") or metric.get("business_cycle"),
        "schedule_tasks": tasks,
        "evidence_ids": evidence.get("evidence_node_ids", []),
        "confidence": confidence,
        "uncertainties": [] if has_sql else ["未找到可用于生成代码口径的 SQL 证据。"],
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
    requests = load_jsonl(llm_dir / "code_definition_requests.jsonl")
    if args.limit:
        requests = requests[: args.limit]

    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("codex_ds_API_KEY")
        or os.environ.get("CODEX_DS_API_KEY")
    )
    if args.provider != "mock" and not api_key:
        raise SystemExit("LLM_API_KEY or OPENAI_API_KEY is required for openai-compatible provider")

    out_path = llm_dir / "code_definitions.jsonl"
    rows = []
    done_generation_ids = set()
    if args.resume and out_path.exists():
        rows = [row for row in load_jsonl(out_path) if row.get("status") == "ok"]
        done_generation_ids = {row.get("generation_id") for row in rows}

    def process_request(req: dict) -> dict:
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "ok"
        error = None
        raw_response = None
        try:
            if args.provider == "mock":
                parsed = mock_definition(req)
                raw_response = {"provider": "mock"}
            else:
                result = post_chat_completion(args.base_url, api_key, args.model, req["messages"], args.timeout, args.max_tokens)
                parsed = result["parsed"]
                raw_response = result["raw_response"]
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError) as exc:
            status = "error"
            error = str(exc)
            parsed = {}
        return {
            "definition_id": "code_definition:" + req["generation_id"].split(":", 1)[1],
            "generation_id": req["generation_id"],
            "metric_node_id": req["metric_node_id"],
            "metric_id": req["metric_id"],
            "evidence_bundle_id": req["evidence_bundle_id"],
            "input_hash": req["input_hash"],
            "prompt_template_id": req["prompt_template_id"],
            "prompt_template_version": req["prompt_template_version"],
            "prompt_template_hash": req["prompt_template_hash"],
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "error": error,
            "definition": parsed,
            "raw_response": raw_response,
        }

    pending_requests = [req for req in requests if req["generation_id"] not in done_generation_ids]
    if args.workers > 1 and pending_requests:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(process_request, req): req for req in pending_requests}
            for future in as_completed(futures):
                req = futures[future]
                rows.append(future.result())
                write_jsonl(out_path, rows)
                if args.progress_every and len(rows) % args.progress_every == 0:
                    print(
                        json.dumps(
                            {
                                "progress": len(rows),
                                "total": len(requests),
                                "last_metric_id": req.get("metric_id"),
                                "error_count": sum(1 for r in rows if r["status"] != "ok"),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    else:
        for idx, req in enumerate(requests, start=1):
            if req["generation_id"] in done_generation_ids:
                continue
            rows.append(process_request(req))
            write_jsonl(out_path, rows)
            if args.progress_every and len(rows) % args.progress_every == 0:
                print(
                    json.dumps(
                        {
                            "progress": len(rows),
                            "total": len(requests),
                            "last_metric_id": req.get("metric_id"),
                            "error_count": sum(1 for r in rows if r["status"] != "ok"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if args.sleep:
                time.sleep(args.sleep)

    write_jsonl(out_path, rows)
    print(json.dumps({"definition_count": len(rows), "error_count": sum(1 for r in rows if r["status"] != "ok"), "output_path": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
