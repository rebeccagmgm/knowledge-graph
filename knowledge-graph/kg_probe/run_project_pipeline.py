#!/usr/bin/env python3
"""Run the reusable project-level KG pipeline for multiple result task IDs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


DEFAULT_OUTPUT_ROOT = "/Applications/personal-work/kg-code-snapshots/projects"
DEFAULT_LINEAGE_ROOT = "/Applications/personal-work/kg-code-snapshots/lineage_batch"


def parse_tasks(value: str | None, task_file: str | None) -> list[str]:
    if task_file:
        text = Path(task_file).read_text()
        return [item.strip() for item in text.replace(",", "\n").splitlines() if item.strip()]
    if value:
        return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    raise SystemExit("Either --tasks or --task-file is required")


def run_step(name: str, cmd: list[str], *, dry_run: bool = False) -> dict:
    started_at = time.time()
    if dry_run:
        result = {
            "step": name,
            "returncode": 0,
            "dry_run": True,
            "cmd": cmd,
            "elapsed_seconds": 0,
        }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(cmd, capture_output=True, check=False, env=env)
    stdout = decode_subprocess_output(proc.stdout)
    stderr = decode_subprocess_output(proc.stderr)
    stdout_lines = [line for line in stdout.splitlines() if line.strip()]
    stderr_lines = [line for line in stderr.splitlines() if line.strip()]
    result = {
        "step": name,
        "returncode": proc.returncode,
        "elapsed_seconds": round(time.time() - started_at, 2),
        "stdout_tail": stdout_lines[-5:],
        "stderr_tail": stderr_lines[-5:],
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"Step failed: {name}")
    return result


def decode_subprocess_output(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def enforce_collection_quality(project_dir: Path, stage: str) -> None:
    lineage = json.loads((project_dir / "lineage.json").read_text())
    if stage == "lineage":
        missing = lineage.get("missing_root_task_ids", [])
        errors = lineage.get("errors", [])
        if missing or errors:
            raise SystemExit(
                f"Collection quality gate failed after lineage: "
                f"missing_roots={len(missing)}, errors={len(errors)}"
            )
        return
    if stage == "details":
        details = json.loads((project_dir / "task_details.json").read_text())
        errors = json.loads((project_dir / "task_detail_errors.json").read_text())
        expected = len(lineage.get("nodes", []))
        if errors or len(details) != expected:
            raise SystemExit(
                f"Collection quality gate failed after details: "
                f"expected={expected}, collected={len(details)}, errors={len(errors)}"
            )
        return
    if stage == "page_code":
        errors_path = project_dir / "code_artifacts_page_errors.json"
        errors = json.loads(errors_path.read_text()) if errors_path.exists() else []
        if errors:
            raise SystemExit(f"Collection quality gate failed after page code: errors={len(errors)}")
        return
    if stage == "logs":
        details = json.loads((project_dir / "task_details.json").read_text())
        expected_ids = {
            str(item["task_id"])
            for item in details
            if item.get("task_type") in {"hiveTask", "hiveTask-2.0"}
        }
        logs = json.loads((project_dir / "log_artifacts_full.json").read_text())
        log_ids = {str(item["task_id"]) for item in logs}
        errors_path = project_dir / "log_collection_errors.json"
        errors = json.loads(errors_path.read_text()) if errors_path.exists() else []
        if errors or expected_ids - log_ids:
            raise SystemExit(
                f"Collection quality gate failed after logs: "
                f"expected={len(expected_ids)}, collected={len(log_ids)}, "
                f"missing={len(expected_ids - log_ids)}, errors={len(errors)}"
            )


def add_flag(cmd: list[str], flag: str, enabled: bool) -> list[str]:
    if enabled:
        cmd.append(flag)
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--tasks", default=None, help="Comma/newline-separated result task IDs")
    parser.add_argument("--task-file", default=None)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--lineage-root", default=None)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=float, default=0.5)
    parser.add_argument("--sz-sleep", type=float, default=0.02)
    parser.add_argument("--sz-flush-every", type=int, default=50)
    parser.add_argument("--column-lineage-passes", type=int, default=2)
    parser.add_argument("--skip-logs", action="store_true")
    parser.add_argument("--skip-sz-metadata", action="store_true")
    parser.add_argument("--import-neo4j", action="store_true")
    parser.add_argument("--neo4j-batch-size", type=int, default=2000)
    parser.add_argument("--build-llm", action="store_true", help="Build LLM evidence, definitions, comparisons, and enhanced graph")
    parser.add_argument("--llm-provider", choices=["mock", "openai-compatible"], default="mock")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-output-prefix", default=None)
    parser.add_argument("--llm-max-sql-chars", type=int, default=12000)
    parser.add_argument("--llm-sleep", type=float, default=0.0)
    parser.add_argument("--force-page-code", action="store_true")
    parser.add_argument("--force-logs", action="store_true")
    parser.add_argument("--force-details", action="store_true")
    parser.add_argument("--force-lineage", action="store_true")
    parser.add_argument("--allow-collection-errors", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    py = sys.executable
    tasks = parse_tasks(args.tasks, args.task_file)
    lineage_root = Path(args.lineage_root or Path(DEFAULT_LINEAGE_ROOT) / args.project_id)
    project_dir = Path(args.output_root) / args.project_id
    graph_prefix = "strategy" if not args.skip_logs else "page"
    llm_output_prefix = args.llm_output_prefix or f"{graph_prefix}_llm"
    steps = []

    lineage_cmd = [
        py,
        str(script_dir / "collect_lineage_batch.py"),
        "--tasks",
        ",".join(tasks),
        "--max-depth",
        str(args.max_depth),
        "--max-nodes",
        str(args.max_nodes),
        "--sleep",
        str(args.sleep),
        "--retries",
        str(args.retries),
        "--backoff",
        str(args.backoff),
        "--output-root",
        str(lineage_root),
    ]
    add_flag(lineage_cmd, "--force", args.force_lineage)
    steps.append(run_step("collect_lineage_batch", lineage_cmd, dry_run=args.dry_run))

    merge_cmd = [
        py,
        str(script_dir / "merge_lineage_project.py"),
        "--project-id",
        args.project_id,
        "--tasks",
        ",".join(tasks),
        "--lineage-root",
        str(lineage_root),
        "--output-root",
        args.output_root,
    ]
    steps.append(run_step("merge_lineage_project", merge_cmd, dry_run=args.dry_run))
    if not args.dry_run and not args.allow_collection_errors:
        enforce_collection_quality(project_dir, "lineage")

    details_cmd = [
        py,
        str(script_dir / "collect_details.py"),
        str(project_dir),
        "--sleep",
        str(args.sleep),
        "--retries",
        str(args.retries),
        "--backoff",
        str(args.backoff),
    ]
    add_flag(details_cmd, "--force", args.force_details)
    steps.append(run_step("collect_details", details_cmd, dry_run=args.dry_run))
    if not args.dry_run and not args.allow_collection_errors:
        enforce_collection_quality(project_dir, "details")

    page_cmd = [
        py,
        str(script_dir / "collect_page_code.py"),
        str(project_dir),
        "--sleep",
        str(args.sleep),
        "--retries",
        str(args.retries),
        "--backoff",
        str(args.backoff),
    ]
    add_flag(page_cmd, "--force", args.force_page_code)
    steps.append(run_step("collect_page_code", page_cmd, dry_run=args.dry_run))
    if not args.dry_run and not args.allow_collection_errors:
        enforce_collection_quality(project_dir, "page_code")

    if not args.skip_logs:
        logs_cmd = [
            py,
            str(script_dir / "collect_logs.py"),
            str(project_dir),
            "--task-types",
            "hiveTask,hiveTask-2.0",
            "--sleep",
            str(args.sleep),
        ]
        add_flag(logs_cmd, "--force", args.force_logs)
        steps.append(run_step("collect_hive_logs", logs_cmd, dry_run=args.dry_run))
        if not args.dry_run and not args.allow_collection_errors:
            enforce_collection_quality(project_dir, "logs")

        steps.append(
            run_step(
                "extract_hive_log_sql_facts",
                [
                    py,
                    str(script_dir / "extract_sql_facts.py"),
                    str(project_dir),
                    "--dialect",
                    args.dialect,
                    "--log-artifacts",
                    "log_artifacts_full.json",
                    "--prefix",
                    "hive_log",
                ],
                dry_run=args.dry_run,
            )
        )

    steps.append(
        run_step(
            "extract_page_sql_facts",
            [
                py,
                str(script_dir / "extract_sql_facts.py"),
                str(project_dir),
                "--dialect",
                args.dialect,
                "--log-artifacts",
                "code_artifacts_page.json",
                "--prefix",
                "page",
            ],
            dry_run=args.dry_run,
        )
    )

    if not args.skip_logs:
        steps.append(
            run_step(
                "merge_strategy_sql_facts",
                [py, str(script_dir / "merge_strategy_sql_facts.py"), str(project_dir)],
                dry_run=args.dry_run,
            )
        )

    def build_graph(step_name: str) -> None:
        steps.append(
            run_step(
                step_name,
                [
                    py,
                    str(script_dir / "build_graph_facts.py"),
                    str(project_dir),
                    "--prefix",
                    graph_prefix,
                ],
                dry_run=args.dry_run,
            )
        )

    def collect_sz(step_name: str) -> None:
        if args.skip_sz_metadata:
            return
        steps.append(
            run_step(
                step_name,
                [
                    py,
                    str(script_dir / "collect_sz_metadata.py"),
                    str(project_dir),
                    "--prefix",
                    graph_prefix,
                    "--from-graph",
                    "--sleep",
                    str(args.sz_sleep),
                    "--retries",
                    str(args.retries),
                    "--backoff",
                    str(args.backoff),
                    "--flush-every",
                    str(args.sz_flush_every),
                ],
                dry_run=args.dry_run,
            )
        )

    # Build once before column lineage so task PRODUCES facts can identify statement targets.
    build_graph("build_graph_facts_initial")
    collect_sz("collect_sz_metadata_initial")

    for pass_no in range(max(args.column_lineage_passes, 0)):
        steps.append(
            run_step(
                f"extract_column_lineage_pass_{pass_no + 1}",
                [
                    py,
                    str(script_dir / "extract_column_lineage.py"),
                    str(project_dir),
                    "--prefix",
                    graph_prefix,
                    "--dialect",
                    args.dialect,
                ],
                dry_run=args.dry_run,
            )
        )
        build_graph(f"build_graph_facts_column_pass_{pass_no + 1}")
        collect_sz(f"collect_sz_metadata_column_pass_{pass_no + 1}")

    build_graph("build_graph_facts_final")

    for step_name, script_name in [
        ("audit_graph_facts", "audit_graph_facts.py"),
        ("export_neo4j_schema", "export_neo4j_schema.py"),
        ("export_neo4j_cypher", "export_neo4j_cypher.py"),
        ("export_query_templates", "export_query_templates.py"),
        ("validate_graph_queries", "validate_graph_queries.py"),
    ]:
        steps.append(
            run_step(
                step_name,
                [py, str(script_dir / script_name), str(project_dir), "--prefix", graph_prefix],
                dry_run=args.dry_run,
            )
        )

    if args.build_llm:
        steps.append(
            run_step(
                "build_llm_evidence",
                [
                    py,
                    str(script_dir / "build_llm_evidence.py"),
                    str(project_dir),
                    "--prefix",
                    graph_prefix,
                    "--max-sql-chars",
                    str(args.llm_max_sql_chars),
                ],
                dry_run=args.dry_run,
            )
        )
        steps.append(
            run_step(
                "generate_llm_requests",
                [py, str(script_dir / "generate_llm_requests.py"), str(project_dir)],
                dry_run=args.dry_run,
            )
        )
        code_definition_cmd = [
            py,
            str(script_dir / "generate_code_definitions.py"),
            str(project_dir),
            "--provider",
            args.llm_provider,
            "--sleep",
            str(args.llm_sleep),
        ]
        if args.llm_model:
            code_definition_cmd.extend(["--model", args.llm_model])
        steps.append(run_step("generate_code_definitions", code_definition_cmd, dry_run=args.dry_run))

        compare_cmd = [
            py,
            str(script_dir / "compare_definitions.py"),
            str(project_dir),
            "--provider",
            args.llm_provider,
            "--sleep",
            str(args.llm_sleep),
        ]
        if args.llm_model:
            compare_cmd.extend(["--model", args.llm_model])
        steps.append(run_step("compare_definitions", compare_cmd, dry_run=args.dry_run))
        steps.append(
            run_step(
                "merge_llm_facts",
                [
                    py,
                    str(script_dir / "merge_llm_facts.py"),
                    str(project_dir),
                    "--prefix",
                    graph_prefix,
                    "--output-prefix",
                    llm_output_prefix,
                ],
                dry_run=args.dry_run,
            )
        )
        for step_name, script_name in [
            ("audit_llm_graph_facts", "audit_graph_facts.py"),
            ("export_llm_neo4j_cypher", "export_neo4j_cypher.py"),
            ("validate_llm_graph_queries", "validate_graph_queries.py"),
        ]:
            steps.append(
                run_step(
                    step_name,
                    [py, str(script_dir / script_name), str(project_dir), "--prefix", llm_output_prefix],
                    dry_run=args.dry_run,
                )
            )

    if args.import_neo4j:
        import_prefix = llm_output_prefix if args.build_llm else graph_prefix
        steps.append(
            run_step(
                "import_and_validate_neo4j",
                [
                    py,
                    str(script_dir / "import_and_validate_neo4j.py"),
                    str(project_dir),
                    "--prefix",
                    import_prefix,
                    "--batch-size",
                    str(args.neo4j_batch_size),
                ],
                dry_run=args.dry_run,
            )
        )

    steps.append(
        run_step(
            "report_project",
            [
                py,
                str(script_dir / "report_project.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
            dry_run=args.dry_run,
        )
    )

    manifest = {
        "project_id": args.project_id,
        "project_dir": str(project_dir),
        "lineage_root": str(lineage_root),
        "root_task_ids": tasks,
        "graph_prefix": graph_prefix,
        "llm_output_prefix": llm_output_prefix if args.build_llm else None,
        "llm_provider": args.llm_provider if args.build_llm else None,
        "import_neo4j": args.import_neo4j,
        "steps": steps,
    }
    if not args.dry_run:
        project_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = project_dir / "project_pipeline_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        manifest["project_pipeline_manifest"] = str(manifest_path)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
