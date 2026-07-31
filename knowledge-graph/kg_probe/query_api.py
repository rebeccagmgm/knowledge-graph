#!/usr/bin/env python3
"""Lightweight HTTP API for graph query primitives.

This intentionally uses Python stdlib HTTP server so the query layer can be
shared into restricted environments without introducing another web framework.
"""

from __future__ import annotations

import argparse
import json
import os
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
    "/api/graph/neighborhood": "get_graph_neighborhood",
    "/api/definitions/issues": "find_definition_issues",
    "/api/metrics/context": "get_metric_context",
    "/api/metrics/definition-compare": "compare_metric_definitions",
    "/api/tasks/context": "get_task_context",
    "/api/datasets/context": "get_dataset_context",
    "/api/columns/context": "get_column_context",
    "/api/lineage/upstream": "trace_upstream",
    "/api/lineage/downstream": "trace_downstream",
    "/api/path/explain": "explain_lineage_path",
    "/api/changes/recent": "get_recent_changes",
}


class QueryThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def read_password(value: str | None, password_file: str | None) -> str:
    if value:
        return value
    env_password = os.environ.get("NEO4J_PASSWORD")
    if env_password:
        return env_password
    if password_file:
        return Path(password_file).read_text().strip()
    raise ValueError("Either --password or --password-file is required")


def response_headers(handler: BaseHTTPRequestHandler, content_type: str = "application/json; charset=utf-8") -> None:
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    response_headers(handler)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    body = html.encode("utf-8")
    handler.send_response(status)
    response_headers(handler, "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def parse_json_body(handler: BaseHTTPRequestHandler, max_body_bytes: int) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    if length > max_body_bytes:
        raise ValueError(f"request body too large: {length} bytes > {max_body_bytes} bytes")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def make_handler(
    store: Neo4jStore,
    default_project_id: str,
    project_dir_root: Path | None,
    max_body_bytes: int = 2 * 1024 * 1024,
):
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

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            response_headers(self)
            self.end_headers()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                try:
                    store.verify()
                    json_response(self, 200, {"status": "ok", "time": round(time.time(), 3)})
                except Exception as exc:  # noqa: BLE001
                    json_response(self, 503, {"status": "error", "error": str(exc)})
                return
            if parsed.path == "/api/primitives":
                json_response(self, 200, {"status": "ok", "data": {"aliases": ALIASES, "query_prefix": "/api/query/"}})
                return
            if parsed.path == "/api/projects":
                try:
                    rows = store.query(
                        """
                        MATCH (p:Project)
                        WITH DISTINCT p.project_id AS project_id
                        WHERE project_id IS NOT NULL
                        CALL (project_id) {
                          MATCH (n:KGNode {project_key: project_id})
                          RETURN count(n) AS node_count
                        }
                        CALL (project_id) {
                          MATCH (:KGNode {project_key: project_id})-[r]->(:KGNode {project_key: project_id})
                          RETURN count(r) AS edge_count
                        }
                        CALL (project_id) {
                          MATCH (t:ScheduleTask {project_key: project_id})
                          RETURN count(t) AS task_count
                        }
                        CALL (project_id) {
                          MATCH (m:Metric {project_key: project_id})
                          RETURN count(m) AS metric_count
                        }
                        RETURN project_id, node_count, edge_count, task_count, metric_count
                        ORDER BY project_id
                        """
                    )
                    json_response(self, 200, {"status": "ok", "data": {"projects": rows}})
                except Exception as exc:  # noqa: BLE001
                    json_response(self, 500, {"status": "error", "error": str(exc)})
                return
            if parsed.path in {"/showcase", "/kg-showcase"}:
                page_path = Path(__file__).resolve().parent / "reports" / "kg_graph_showcase.html"
                if not page_path.exists():
                    json_response(self, 404, {"status": "error", "error": f"showcase page not found: {page_path}"})
                    return
                html_response(self, 200, page_path.read_text(encoding="utf-8"))
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
                payload = parse_json_body(self, max_body_bytes)
            except json.JSONDecodeError as exc:
                json_response(self, 400, {"status": "error", "error": f"Invalid JSON: {exc}"})
                return
            except ValueError as exc:
                json_response(self, 413 if "too large" in str(exc) else 400, {"status": "error", "error": str(exc)})
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
    parser.add_argument("--host", default=os.environ.get("KG_QUERY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("KG_QUERY_PORT", "8790")))
    parser.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    parser.add_argument("--password", default=None)
    parser.add_argument("--password-file", default=os.environ.get("NEO4J_PASSWORD_FILE", "/Applications/personal-work/kg-code-snapshots/neo4j_password.txt"))
    parser.add_argument("--database", default=os.environ.get("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--project-id", default=os.environ.get("KG_PROJECT_ID", "trial_project"))
    parser.add_argument("--project-dir-root", default=os.environ.get("KG_PROJECT_DIR_ROOT", "/Applications/personal-work/kg_probe"))
    parser.add_argument("--max-body-bytes", type=int, default=int(os.environ.get("KG_QUERY_MAX_BODY_BYTES", str(2 * 1024 * 1024))))
    args = parser.parse_args()

    password = read_password(args.password, args.password_file)
    store = Neo4jStore(args.uri, args.user, password, args.database)
    project_dir_root = Path(args.project_dir_root) if args.project_dir_root else None
    handler = make_handler(store, args.project_id, project_dir_root, args.max_body_bytes)
    server = QueryThreadingHTTPServer((args.host, args.port), handler)
    print(json.dumps({"status": "ready", "host": args.host, "port": args.port}, ensure_ascii=False))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
