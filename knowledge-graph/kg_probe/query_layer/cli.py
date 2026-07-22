from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


for local_path in [
    Path("/Applications/personal-work/kg-python-userbase/lib/python/site-packages"),
    Path("/Applications/personal-work/kg-python-userbase/lib/python3.9/site-packages"),
]:
    if local_path.exists():
        sys.path.insert(0, str(local_path))

from .neo4j_store import Neo4jStore  # noqa: E402
from .service import QueryService  # noqa: E402


def load_payload(raw: str | None, path: str | None) -> dict:
    if path:
        return json.loads(Path(path).read_text())
    if raw:
        return json.loads(raw)
    if not sys.stdin.isatty():
        text = sys.stdin.read().strip()
        if text:
            return json.loads(text)
    return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the code knowledge graph through stable primitives")
    parser.add_argument("primitive", help="Primitive name or get_graph_status")
    parser.add_argument("--json", dest="payload", default=None, help="Request payload as JSON")
    parser.add_argument("--file", default=None, help="Read request payload from a JSON file")
    parser.add_argument("--project-id", default=os.environ.get("KG_PROJECT_ID", "trial_project"))
    parser.add_argument("--project-dir", default=os.environ.get("KG_PROJECT_DIR", "/Applications/personal-work/kg-code-snapshots/projects/trial_project"))
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--password-file", default=os.environ.get("NEO4J_PASSWORD_FILE", "/Applications/personal-work/kg-code-snapshots/neo4j_password.txt"))
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    payload = load_payload(args.payload, args.file)
    payload.setdefault("project_id", args.project_id)
    store = Neo4jStore.from_password_file(args.uri, args.user, args.password_file, args.database)
    try:
        service = QueryService(store, args.project_id, args.project_dir)
        if args.primitive == "get_graph_status":
            result = service.get_graph_status(args.project_id)
        elif args.primitive == "get_graph_native_capabilities":
            result = service.get_graph_native_capabilities(args.project_id)
        else:
            result = service.execute(args.primitive, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    finally:
        store.close()


if __name__ == "__main__":
    main()
