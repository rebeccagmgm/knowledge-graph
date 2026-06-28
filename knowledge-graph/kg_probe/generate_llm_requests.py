#!/usr/bin/env python3
"""Generate LLM request JSONL from evidence bundles and prompt templates."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_templates(path: Path) -> dict[str, dict]:
    raw = json.loads(path.read_text())
    templates = {}
    for item in raw["templates"]:
        raw_text = json.dumps(item, ensure_ascii=False, sort_keys=True)
        item = dict(item)
        item["template_hash"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        templates[item["template_id"]] = item
    return templates


def render(template: str, variables: dict[str, str]) -> str:
    value = template
    for key, replacement in variables.items():
        value = value.replace("{{" + key + "}}", replacement)
    return value


def prompt_payload(payload: dict, *, max_evidence_nodes: int, max_evidence_edges: int) -> dict:
    pruned = dict(payload)
    node_ids = list(payload.get("evidence_node_ids", []))
    edge_ids = list(payload.get("evidence_edge_ids", []))
    pruned["evidence_node_ids"] = node_ids[:max_evidence_nodes]
    pruned["evidence_edge_ids"] = edge_ids[:max_evidence_edges]
    pruned["evidence_id_truncation"] = {
        "evidence_node_id_count": len(node_ids),
        "evidence_edge_id_count": len(edge_ids),
        "included_node_id_count": len(pruned["evidence_node_ids"]),
        "included_edge_id_count": len(pruned["evidence_edge_ids"]),
    }
    return pruned


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--templates", default=str(Path(__file__).with_name("llm_prompt_templates.json")))
    parser.add_argument("--kind", choices=["code_definition"], default="code_definition")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-evidence-nodes", type=int, default=80)
    parser.add_argument("--max-evidence-edges", type=int, default=80)
    args = parser.parse_args()

    base = Path(args.project_dir)
    llm_dir = base / "llm"
    bundles = load_jsonl(llm_dir / "evidence_bundles.jsonl")
    if args.limit:
        bundles = bundles[: args.limit]
    templates = load_templates(Path(args.templates))
    template = templates["metric_code_definition.v1"]

    rows = []
    for bundle in bundles:
        evidence_json = json.dumps(
            prompt_payload(
                bundle["payload"],
                max_evidence_nodes=args.max_evidence_nodes,
                max_evidence_edges=args.max_evidence_edges,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
        run_seed = {
            "kind": args.kind,
            "metric_node_id": bundle["metric_node_id"],
            "bundle_id": bundle["bundle_id"],
            "input_hash": bundle["input_hash"],
            "template_id": template["template_id"],
            "template_hash": template["template_hash"],
        }
        generation_id = "prompt_run:" + hashlib.sha256(
            json.dumps(run_seed, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        rows.append(
            {
                "generation_id": generation_id,
                "kind": args.kind,
                "metric_node_id": bundle["metric_node_id"],
                "metric_id": bundle["metric_id"],
                "evidence_bundle_id": bundle["bundle_id"],
                "input_hash": bundle["input_hash"],
                "prompt_template_id": template["template_id"],
                "prompt_template_version": template["version"],
                "prompt_template_hash": template["template_hash"],
                "messages": [
                    {"role": "system", "content": template["system"]},
                    {"role": "user", "content": render(template["user"], {"evidence_json": evidence_json})},
                ],
            }
        )

    out_path = llm_dir / "code_definition_requests.jsonl"
    out_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    print(json.dumps({"request_count": len(rows), "output_path": str(out_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
