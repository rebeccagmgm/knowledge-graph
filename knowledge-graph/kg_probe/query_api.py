#!/usr/bin/env python3
"""Lightweight HTTP API for graph query primitives.

This intentionally uses Python stdlib HTTP server so the query layer can be
shared into restricted environments without introducing another web framework.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


for local_path in [
    Path("/Applications/personal-work/kg-python-userbase/lib/python/site-packages"),
    Path("/Applications/personal-work/kg-python-userbase/lib/python3.9/site-packages"),
]:
    if local_path.exists():
        sys.path.insert(0, str(local_path))

from query_layer.neo4j_store import Neo4jStore  # noqa: E402
from query_layer.service import QueryService  # noqa: E402


ALIASES = {
    "/api/entities/search": "search_entities",
    "/api/impact/analyze": "analyze_impact",
    "/api/definitions/issues": "find_definition_issues",
    "/api/metrics/context": "get_metric_context",
    "/api/metrics/definition-compare": "compare_metric_definitions",
    "/api/tasks/context": "get_task_context",
    "/api/datasets/context": "get_dataset_context",
    "/api/columns/context": "get_column_context",
    "/api/lineage/upstream": "trace_upstream",
    "/api/lineage/downstream": "trace_downstream",
    "/api/path/explain": "explain_lineage_path",
}


def read_password(value: str | None, password_file: str | None) -> str:
    if value:
        return value
    if password_file:
        return Path(password_file).read_text().strip()
    raise ValueError("Either --password or --password-file is required")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def make_handler(store: Neo4jStore, default_project_id: str, project_dir_root: Path | None):
    class QueryApiHandler(BaseHTTPRequestHandler):
        server_version = "KGProbeQueryAPI/0.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _service(self, project_id: str, payload: dict) -> QueryService:
            project_dir = payload.get("project_dir")
            if not project_dir and project_dir_root:
                candidate = project_dir_root / project_id
                if candidate.exists():
                    project_dir = str(candidate)
            return QueryService(store, project_id=project_id, project_dir=project_dir)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                try:
                    store.verify()
                    json_response(self, 200, {"status": "ok", "time": round(time.time(), 3)})
                except Exception as exc:  # noqa: BLE001
                    json_response(self, 503, {"status": "error", "error": str(exc)})
                return
            if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/graph-status"):
                project_id = parsed.path.removeprefix("/api/projects/").removesuffix("/graph-status").strip("/")
                if not project_id:
                    json_response(self, 400, {"status": "error", "error": "project_id is required"})
                    return
                payload = {"project_id": project_id}
                result = self._service(project_id, payload).get_graph_status(project_id)
                json_response(self, 200, {"status": "ok", "data": result})
                return
            json_response(self, 404, {"status": "error", "error": "Not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                payload = parse_json_body(self)
            except json.JSONDecodeError as exc:
                json_response(self, 400, {"status": "error", "error": f"Invalid JSON: {exc}"})
                return

            primitive = ALIASES.get(parsed.path)
            if parsed.path.startswith("/api/query/"):
                primitive = parsed.path.removeprefix("/api/query/").strip("/")
            if not primitive:
                json_response(self, 404, {"status": "error", "error": "Not found"})
                return

            query_params = parse_qs(parsed.query)
            project_id = (
                payload.get("project_id")
                or (query_params.get("project_id") or [None])[0]
                or default_project_id
            )
            payload["project_id"] = project_id
            result = self._service(project_id, payload).execute(primitive, payload)
            http_status = 200 if result.get("status") != "error" else 400
            json_response(self, http_status, result)

    return QueryApiHandler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--uri", default="bolt://localhost:7687")
    parser.add_argument("--user", default="neo4j")
    parser.add_argument("--password", default=None)
    parser.add_argument("--password-file", default="/Applications/personal-work/kg-code-snapshots/neo4j_password.txt")
    parser.add_argument("--database", default="neo4j")
    parser.add_argument("--project-id", default="trial_project")
    parser.add_argument("--project-dir-root", default="/Applications/personal-work/kg_probe")
    args = parser.parse_args()

    password = read_password(args.password, args.password_file)
    store = Neo4jStore(args.uri, args.user, password, args.database)
    project_dir_root = Path(args.project_dir_root) if args.project_dir_root else None
    handler = make_handler(store, args.project_id, project_dir_root)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"status": "ready", "host": args.host, "port": args.port}, ensure_ascii=False))
    try:
        server.serve_forever()
    finally:
        store.close()


if __name__ == "__main__":
    main()
