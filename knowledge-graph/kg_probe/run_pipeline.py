#!/usr/bin/env python3
"""Run the KG collection pipeline for one Horae root task."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


DEFAULT_OUTPUT_ROOT = "/Applications/personal-work/kg-code-snapshots/projects"


def run_step(name: str, cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
    stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
    result = {
        "step": name,
        "returncode": proc.returncode,
        "stdout_tail": stdout_lines[-3:],
        "stderr_tail": stderr_lines[-3:],
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"Step failed: {name}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--run-date", default=None)
    parser.add_argument("--max-depth", type=int, default=25)
    parser.add_argument("--max-nodes", type=int, default=3000)
    parser.add_argument("--detail-limit", type=int, default=0)
    parser.add_argument("--instance-limit", type=int, default=30)
    parser.add_argument("--log-limit", type=int, default=0, help="0 means all tasks")
    parser.add_argument("--dialect", default="spark")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--skip-logs", action="store_true")
    parser.add_argument("--skip-sz-metadata", action="store_true")
    parser.add_argument("--force-page-code", action="store_true")
    parser.add_argument("--column-lineage-passes", type=int, default=2)
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_dir = Path(args.output_root) / args.task_id
    py = sys.executable

    steps = []

    collect_cmd = [
        py,
        str(script_dir / "collect_project.py"),
        args.task_id,
        "--max-depth",
        str(args.max_depth),
        "--max-nodes",
        str(args.max_nodes),
        "--detail-limit",
        str(args.detail_limit),
        "--instance-limit",
        str(args.instance_limit),
        "--log-scope",
        "none",
        "--output-root",
        args.output_root,
    ]
    if args.run_date:
        collect_cmd.extend(["--run-date", args.run_date])
    steps.append(run_step("collect_project", collect_cmd))

    page_cmd = [
        py,
        str(script_dir / "collect_page_code.py"),
        str(project_dir),
    ]
    if args.force_page_code:
        page_cmd.append("--force")
    steps.append(run_step("collect_page_code", page_cmd))

    if not args.skip_logs:
        logs_cmd = [
            py,
            str(script_dir / "collect_logs.py"),
            str(project_dir),
            "--limit",
            str(args.log_limit),
            "--task-types",
            "hiveTask,hiveTask-2.0",
        ]
        if args.run_date:
            logs_cmd.extend(["--run-date", args.run_date])
        steps.append(run_step("collect_logs", logs_cmd))

        full_extract_cmd = [
            py,
            str(script_dir / "extract_sql_facts.py"),
            str(project_dir),
            "--dialect",
            args.dialect,
            "--log-artifacts",
            "log_artifacts_full.json",
            "--prefix",
            "hive_log",
        ]
        steps.append(run_step("extract_hive_log_sql_facts", full_extract_cmd))

    page_extract_cmd = [
        py,
        str(script_dir / "extract_sql_facts.py"),
        str(project_dir),
        "--dialect",
        args.dialect,
        "--log-artifacts",
        "code_artifacts_page.json",
        "--prefix",
        "page",
    ]
    steps.append(run_step("extract_page_sql_facts", page_extract_cmd))

    if not args.skip_logs:
        steps.append(
            run_step(
                "merge_strategy_sql_facts",
                [
                    py,
                    str(script_dir / "merge_strategy_sql_facts.py"),
                    str(project_dir),
                ],
            )
        )
        graph_prefix = "strategy"
    else:
        graph_prefix = "page"

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
                    "--flush-every",
                    "50",
                ],
            )
        )

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
            )
        )
        build_graph(f"build_graph_facts_column_pass_{pass_no + 1}")
        collect_sz(f"collect_sz_metadata_column_pass_{pass_no + 1}")

    build_graph("build_graph_facts_final")
    steps.append(
        run_step(
            "audit_graph_facts",
            [
                py,
                str(script_dir / "audit_graph_facts.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
        )
    )
    steps.append(
        run_step(
            "export_neo4j_schema",
            [
                py,
                str(script_dir / "export_neo4j_schema.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
        )
    )
    steps.append(
        run_step(
            "export_neo4j_cypher",
            [
                py,
                str(script_dir / "export_neo4j_cypher.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
        )
    )
    steps.append(
        run_step(
            "export_query_templates",
            [
                py,
                str(script_dir / "export_query_templates.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
        )
    )
    steps.append(
        run_step(
            "validate_graph_queries",
            [
                py,
                str(script_dir / "validate_graph_queries.py"),
                str(project_dir),
                "--prefix",
                graph_prefix,
            ],
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
        )
    )

    manifest = {
        "task_id": args.task_id,
        "project_dir": str(project_dir),
        "graph_prefix": graph_prefix,
        "steps": steps,
    }
    pipeline_path = project_dir / "pipeline_manifest.json"
    pipeline_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"pipeline_manifest": str(pipeline_path), **manifest}, ensure_ascii=False))


if __name__ == "__main__":
    main()
