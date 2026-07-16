from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase


def to_plain(value: Any):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_plain(v) for v in value]
    if hasattr(value, "labels") and hasattr(value, "items"):
        return {
            "id": value.get("id"),
            "labels": sorted(value.labels),
            "properties": {str(k): to_plain(v) for k, v in value.items()},
        }
    if hasattr(value, "type") and hasattr(value, "start_node"):
        return {
            "id": value.get("id"),
            "type": value.type,
            "from": value.start_node.get("id"),
            "to": value.end_node.get("id"),
            "properties": {str(k): to_plain(v) for k, v in value.items()},
        }
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {
            "nodes": [to_plain(node) for node in value.nodes],
            "edges": [to_plain(rel) for rel in value.relationships],
        }
    if hasattr(value, "iso_format"):
        return value.iso_format()
    return str(value)


class Neo4jStore:
    def __init__(self, uri: str, user: str, password: str, database: str = "neo4j"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database

    @classmethod
    def from_password_file(
        cls,
        uri: str,
        user: str,
        password_file: str | Path,
        database: str = "neo4j",
    ):
        password = Path(password_file).read_text().strip()
        return cls(uri, user, password, database)

    def close(self) -> None:
        self.driver.close()

    def verify(self) -> None:
        self.driver.verify_connectivity()

    def query(self, cypher: str, parameters: dict | None = None) -> list[dict]:
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher, parameters or {})
            return [{key: to_plain(value) for key, value in record.items()} for record in result]

