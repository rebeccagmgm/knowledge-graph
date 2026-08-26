#!/usr/bin/env python3
"""Refine ontology_v2 concept candidates with an OpenAI-compatible LLM."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

from ontology_v2_utils import clean_text, load_jsonl, now_iso, stable_hash, write_json, write_jsonl


SYSTEM_PROMPT = """你是券商大数据 ontology 发现助手。
你的任务不是编造正式 ontology，而是基于代码逆向得到的表、字段组、血缘证据，判断一个概念候选是否像真实业务概念。
请只基于输入证据回答；证据不足时要明确说明。输出必须是严格 JSON。"""

USER_TEMPLATE = """请审阅以下 ontology 概念候选。

你需要判断：
1. 这个候选更合适的业务概念名称是什么；
2. 它大概表达什么业务对象、业务事实或业务属性；
3. 它是否应该拆成多个子概念，或者与其他概念合并；
4. 哪些成员表/字段组是强证据，哪些只是弱相关；
5. 后续如果要让业务专家确认，应该问什么问题。

输入 JSON:
{{candidate_json}}

请输出 JSON，字段包括：
recommended_name, concept_type, business_definition, scope, key_fields, strong_evidence, weak_evidence,
split_suggestions, merge_suggestions, evidence_boundary, review_questions, confidence。

字段要求：
- concept_type 只能取 business_object, business_event, business_measure, business_attribute, lifecycle_attribute, reference_data, unclear。
- confidence 只能取 high, medium, low。
- key_fields、strong_evidence、weak_evidence、split_suggestions、merge_suggestions、review_questions 必须是数组。
"""


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


def post_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    timeout: int,
    max_tokens: int,
    use_response_format: bool = True,
) -> dict:
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if use_response_format:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if use_response_format and exc.code in {400, 422}:
            return post_chat_completion(base_url, api_key, model, messages, timeout, max_tokens, use_response_format=False)
        raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from exc
    content = raw["choices"][0]["message"]["content"]
    return {"raw_response": raw, "parsed": parse_json_content(content)}


def compact_member(member: dict, group_by_id: dict[str, dict], table_by_dataset: dict[str, dict]) -> dict:
    group = group_by_id.get(member.get("field_group_id"), {})
    table = table_by_dataset.get(member.get("dataset"), {})
    fields = []
    for field in (group.get("fields") or [])[:30]:
        fields.append(
            {
                "name": clean_text(field.get("name")),
                "comment": clean_text(field.get("comment")),
                "lineage_touch_count": field.get("lineage_touch_count", 0),
            }
        )
    return {
        "field_group_id": member.get("field_group_id"),
        "dataset": clean_text(member.get("dataset")),
        "table_subject": clean_text(table.get("business_subject")),
        "table_role": clean_text(table.get("table_role")),
        "group_name": clean_text(member.get("group_name")),
        "group_type": clean_text(group.get("group_type") or member.get("group_type")),
        "field_count": member.get("field_count"),
        "evidence_level": member.get("evidence_level"),
        "verified_evidence_score": member.get("verified_evidence_score"),
        "incoming_field_lineage_count": group.get("incoming_field_lineage_count", 0),
        "outgoing_field_lineage_count": group.get("outgoing_field_lineage_count", 0),
        "sample_fields": fields,
        "sample_upstream_datasets": [clean_text(item).removeprefix("dataset:") for item in (group.get("upstream_datasets") or [])[:12]],
        "sample_downstream_datasets": [clean_text(item).removeprefix("dataset:") for item in (group.get("downstream_datasets") or [])[:12]],
    }


def candidate_payload(
    concept: dict,
    group_by_id: dict[str, dict],
    table_by_dataset: dict[str, dict],
    relations_by_concept: dict[str, list[dict]],
    max_members: int,
    max_relations: int,
) -> dict:
    members = sorted(
        concept.get("members") or [],
        key=lambda item: (-(item.get("verified_evidence_score") or 0), item.get("dataset") or ""),
    )[:max_members]
    relations = sorted(
        relations_by_concept.get(concept["id"], []),
        key=lambda item: (-float(item.get("score") or 0), item.get("relationship_type") or ""),
    )[:max_relations]
    return {
        "concept_candidate": {
            "id": concept.get("id"),
            "rule_name": clean_text(concept.get("concept_name")),
            "rule_key": clean_text(concept.get("concept_key")),
            "member_count": concept.get("member_count"),
            "relationship_count": concept.get("relationship_count"),
            "relationship_types": concept.get("relationship_types"),
            "avg_score": concept.get("avg_score"),
            "max_score": concept.get("max_score"),
            "rule_confidence": concept.get("confidence"),
        },
        "members": [compact_member(member, group_by_id, table_by_dataset) for member in members],
        "top_relations": [
            {
                "relationship_type": rel.get("relationship_type"),
                "score": rel.get("score"),
                "from_dataset": clean_text(rel.get("from_dataset")),
                "from_group_name": clean_text(rel.get("from_group_name")),
                "to_dataset": clean_text(rel.get("to_dataset")),
                "to_group_name": clean_text(rel.get("to_group_name")),
                "evidence": {
                    "field_token_similarity": (rel.get("evidence") or {}).get("field_token_similarity"),
                    "table_context_similarity": (rel.get("evidence") or {}).get("table_context_similarity"),
                    "upstream_overlap": (rel.get("evidence") or {}).get("upstream_overlap"),
                    "downstream_overlap": (rel.get("evidence") or {}).get("downstream_overlap"),
                    "group_lineage_bridge": (rel.get("evidence") or {}).get("group_lineage_bridge"),
                    "shared_tokens": [clean_text(item) for item in ((rel.get("evidence") or {}).get("shared_tokens") or [])[:20]],
                },
            }
            for rel in relations
        ],
    }


def build_relation_index(concepts: list[dict], relations: list[dict]) -> dict[str, list[dict]]:
    concept_by_group: dict[str, list[str]] = {}
    for concept in concepts:
        for member in concept.get("members") or []:
            concept_by_group.setdefault(member.get("field_group_id"), []).append(concept["id"])
    indexed: dict[str, list[dict]] = {concept["id"]: [] for concept in concepts}
    for rel in relations:
        concept_ids = set(concept_by_group.get(rel.get("from_group_id"), []) + concept_by_group.get(rel.get("to_group_id"), []))
        for concept_id in concept_ids:
            indexed.setdefault(concept_id, []).append(rel)
    return indexed


def mock_refinement(payload: dict) -> dict:
    candidate = payload["concept_candidate"]
    members = payload.get("members") or []
    strong = [item for item in members if item.get("evidence_level") == "strong"][:5]
    weak = [item for item in members if item.get("evidence_level") == "weak"][:5]
    return {
        "recommended_name": candidate.get("rule_name") or "未命名概念",
        "concept_type": "unclear",
        "business_definition": "mock 模式仅汇总规则候选；真实业务定义需切换 LLM provider。",
        "scope": "项目内候选概念。",
        "key_fields": sorted({field["comment"] or field["name"] for item in members for field in item.get("sample_fields", [])})[:12],
        "strong_evidence": [f"{item.get('dataset')} / {item.get('group_name')}" for item in strong],
        "weak_evidence": [f"{item.get('dataset')} / {item.get('group_name')}" for item in weak],
        "split_suggestions": [],
        "merge_suggestions": [],
        "evidence_boundary": "mock 模式不做语义审阅。",
        "review_questions": [],
        "confidence": "low",
    }


def graph_nodes(rows: list[dict]) -> list[dict]:
    nodes = []
    for row in rows:
        refinement = row.get("refinement") or {}
        nodes.append(
            {
                "id": row["refinement_id"],
                "labels": ["OntologyLLMRefinement", "OntologyCandidate"],
                "properties": {
                    "project_key": row["project_key"],
                    "concept_candidate_id": row["concept_candidate_id"],
                    "recommended_name": clean_text(refinement.get("recommended_name")),
                    "concept_type": clean_text(refinement.get("concept_type")),
                    "business_definition": clean_text(refinement.get("business_definition")),
                    "scope": clean_text(refinement.get("scope")),
                    "confidence": clean_text(refinement.get("confidence") or row.get("status")),
                    "evidence_boundary": clean_text(refinement.get("evidence_boundary")),
                    "model": row.get("model"),
                    "provider": row.get("provider"),
                    "prompt_template_id": row.get("prompt_template_id"),
                    "prompt_template_version": row.get("prompt_template_version"),
                    "input_hash": row.get("input_hash"),
                    "status": row.get("status"),
                    "error": row.get("error"),
                    "fact_type": "ontology_llm_refinement",
                    "inferred": True,
                    "built_at": row.get("finished_at"),
                    "knowledge_admission": "needs_review",
                    "quality_tier": "ontology_candidate",
                },
            }
        )
    return nodes


def graph_edges(rows: list[dict]) -> list[dict]:
    return [
        {
            "id": f"{row['concept_candidate_id']}->REFINED_BY_LLM->{row['refinement_id']}",
            "from": row["concept_candidate_id"],
            "to": row["refinement_id"],
            "type": "REFINED_BY_LLM",
            "properties": {
                "project_key": row["project_key"],
                "fact_type": "ontology_llm_refinement",
                "confidence": (row.get("refinement") or {}).get("confidence") or row.get("status"),
                "model": row.get("model"),
                "provider": row.get("provider"),
                "source_type": "ontology_llm_refinement.v1",
                "inferred": True,
                "built_at": row.get("finished_at"),
                "knowledge_admission": "needs_review",
                "quality_tier": "ontology_candidate",
            },
        }
        for row in rows
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--provider", choices=["mock", "openai-compatible"], default="mock")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.72)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-members", type=int, default=16)
    parser.add_argument("--max-relations", type=int, default=12)
    args = parser.parse_args()

    project_dir = Path(args.project_dir)
    out_dir = Path(args.output_dir) if args.output_dir else project_dir / "ontology_v2"
    concepts = list(load_jsonl(out_dir / "concept_candidates.jsonl"))
    groups = list(load_jsonl(out_dir / "verified_field_groups.jsonl"))
    tables = list(load_jsonl(out_dir / "table_profiles.jsonl"))
    relations = list(load_jsonl(out_dir / "concept_relations.jsonl"))

    group_by_id = {item["id"]: item for item in groups}
    table_by_dataset = {item["dataset"]: item for item in tables}
    relations_by_concept = build_relation_index(concepts, relations)

    selected = [
        item
        for item in concepts
        if float(item.get("max_score") or 0) >= args.min_score
    ]
    selected = sorted(selected, key=lambda item: (-float(item.get("max_score") or 0), -int(item.get("member_count") or 0), item["id"]))
    if args.limit:
        selected = selected[: args.limit]

    api_key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("codex_ds_API_KEY")
        or os.environ.get("CODEX_DS_API_KEY")
    )
    if args.provider != "mock" and not api_key:
        raise SystemExit("LLM_API_KEY or OPENAI_API_KEY is required for openai-compatible provider")

    out_path = out_dir / "llm_refined_concepts.jsonl"
    rows = []
    done = set()
    if args.resume and out_path.exists():
        rows = [item for item in load_jsonl(out_path)]
        done = {item.get("concept_candidate_id") for item in rows if item.get("status") == "ok"}

    for idx, concept in enumerate(selected, start=1):
        if concept["id"] in done:
            continue
        payload = candidate_payload(concept, group_by_id, table_by_dataset, relations_by_concept, args.max_members, args.max_relations)
        input_hash = stable_hash(payload)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.replace("{{candidate_json}}", json.dumps(payload, ensure_ascii=False, sort_keys=True))},
        ]
        started_at = datetime.now().isoformat(timespec="seconds")
        status = "ok"
        error = None
        raw_response = None
        try:
            if args.provider == "mock":
                refinement = mock_refinement(payload)
                raw_response = {"provider": "mock"}
            else:
                result = post_chat_completion(args.base_url, api_key, args.model, messages, args.timeout, args.max_tokens)
                refinement = result["parsed"]
                raw_response = result["raw_response"]
        except (
            RuntimeError,
            urllib.error.URLError,
            http.client.IncompleteRead,
            json.JSONDecodeError,
            KeyError,
            TimeoutError,
        ) as exc:
            status = "error"
            error = str(exc)
            refinement = {}

        row = {
            "refinement_id": "ontology_llm_refinement:" + stable_hash([concept["id"], input_hash]),
            "concept_candidate_id": concept["id"],
            "project_key": concept.get("project_key") or project_dir.name,
            "rule_concept_name": concept.get("concept_name"),
            "rule_concept_key": concept.get("concept_key"),
            "input_hash": input_hash,
            "prompt_template_id": "ontology_concept_refinement.v1",
            "prompt_template_version": "1.0.0",
            "provider": args.provider,
            "model": args.model if args.provider != "mock" else "mock",
            "started_at": started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "error": error,
            "request_payload": payload,
            "refinement": refinement,
            "raw_response": raw_response,
        }
        rows.append(row)
        write_jsonl(out_path, rows)
        print(
            json.dumps(
                {
                    "progress": len(rows),
                    "selected_total": len(selected),
                    "rule_concept_name": concept.get("concept_name"),
                    "status": status,
                    "error": error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.sleep and idx < len(selected):
            time.sleep(args.sleep)

    write_jsonl(out_dir / "ontology_llm_refinement_graph_nodes.jsonl", graph_nodes(rows))
    write_jsonl(out_dir / "ontology_llm_refinement_graph_edges.jsonl", graph_edges(rows))
    summary = {
        "project_key": selected[0].get("project_key") if selected else project_dir.name,
        "generated_at": now_iso(),
        "selected_count": len(selected),
        "refinement_count": len(rows),
        "error_count": sum(1 for item in rows if item.get("status") != "ok"),
        "provider": args.provider,
        "model": args.model if args.provider != "mock" else "mock",
        "status_distribution": dict(Counter(item.get("status") for item in rows)),
        "confidence_distribution": dict(Counter((item.get("refinement") or {}).get("confidence", "") for item in rows if item.get("status") == "ok")),
        "output_path": str(out_path),
    }
    write_json(out_dir / "ontology_llm_refinement_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
