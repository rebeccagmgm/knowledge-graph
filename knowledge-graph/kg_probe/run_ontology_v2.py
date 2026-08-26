#!/usr/bin/env python3
"""Run ontology_v2 candidate discovery end to end."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def run_step(name: str, cmd: list[str]) -> dict:
    start = time.time()
    proc = subprocess.run(cmd, text=True, capture_output=True)
    elapsed = round(time.time() - start, 2)
    stdout_lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    stderr_lines = [line for line in (proc.stderr or "").splitlines() if line.strip()]
    result = {
        "step": name,
        "returncode": proc.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": stdout_lines[-5:],
        "stderr_tail": stderr_lines[-10:],
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if proc.returncode != 0:
        raise SystemExit(f"Step failed: {name}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_dir")
    parser.add_argument("--prefix", default="strategy")
    parser.add_argument("--project-key", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--max-pairs", type=int, default=12000)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--refine-ontology-llm", action="store_true", help="Refine concept candidates with an LLM")
    parser.add_argument("--llm-provider", choices=["mock", "openai-compatible"], default="mock")
    parser.add_argument("--llm-model", default=None)
    parser.add_argument("--llm-base-url", default=None)
    parser.add_argument("--llm-limit", type=int, default=10)
    parser.add_argument("--llm-min-score", type=float, default=0.72)
    parser.add_argument("--llm-sleep", type=float, default=0.0)
    parser.add_argument("--llm-resume", action="store_true")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    project_dir = str(Path(args.project_dir))
    common = [project_dir]
    if args.project_key:
        common += ["--project-key", args.project_key]
    if args.output_dir:
        common += ["--output-dir", args.output_dir]

    steps = [
        (
            "build_table_profiles",
            [args.python, str(here / "build_table_profiles.py"), *common, "--prefix", args.prefix],
        ),
        (
            "discover_field_groups",
            [args.python, str(here / "discover_field_groups.py"), *common, "--prefix", args.prefix],
        ),
        (
            "verify_concept_evidence",
            [args.python, str(here / "verify_concept_evidence.py"), *common, "--prefix", args.prefix],
        ),
        (
            "align_concepts",
            [args.python, str(here / "align_concepts.py"), *common, "--max-pairs", str(args.max_pairs)],
        ),
    ]
    if args.refine_ontology_llm:
        refine_cmd = [
            args.python,
            str(here / "refine_ontology_concepts_with_llm.py"),
            *common,
            "--provider",
            args.llm_provider,
            "--limit",
            str(args.llm_limit),
            "--min-score",
            str(args.llm_min_score),
            "--sleep",
            str(args.llm_sleep),
        ]
        if args.llm_model:
            refine_cmd.extend(["--model", args.llm_model])
        if args.llm_base_url:
            refine_cmd.extend(["--base-url", args.llm_base_url])
        if args.llm_resume:
            refine_cmd.append("--resume")
        steps.append(("refine_ontology_concepts_with_llm", refine_cmd))
    results = [run_step(name, cmd) for name, cmd in steps]
    out_dir = Path(args.output_dir) if args.output_dir else Path(args.project_dir) / "ontology_v2"
    manifest = {
        "project_dir": project_dir,
        "project_key": args.project_key,
        "prefix": args.prefix,
        "output_dir": str(out_dir),
        "steps": results,
    }
    (out_dir / "ontology_v2_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "output_dir": str(out_dir)}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
