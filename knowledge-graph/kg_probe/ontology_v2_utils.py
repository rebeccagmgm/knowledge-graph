#!/usr/bin/env python3
"""Shared helpers for ontology_v2 candidate discovery."""

from __future__ import annotations

import html
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable


HTML_TAG_RE = re.compile(r"<[^>]+>")
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")

LOW_VALUE_COLUMN_NAMES = {
    "id",
    "index_id",
    "index_val",
    "busi_date",
    "data_time",
    "modify_time",
    "modify_operator",
    "status",
    "tag_id",
    "pt",
    "dt",
    "etl_date",
    "etl_time",
    "create_time",
    "update_time",
}

GENERIC_TOKENS = {
    "a",
    "b",
    "c",
    "tmp",
    "temp",
    "ods",
    "dwd",
    "dws",
    "dm",
    "pdata",
    "odata",
    "index",
    "idx",
    "ind",
    "id",
    "code",
    "cd",
    "type",
    "flag",
    "status",
    "date",
    "time",
    "day",
    "month",
    "mon",
    "year",
    "data",
    "modify",
    "operator",
    "create",
    "update",
    "busi",
    "biz",
    "pt",
    "dt",
    "grp",
    "tag",
    "无",
    "表",
    "字段",
    "信息",
    "数据",
    "明细",
    "结果",
    "日报",
    "代码",
    "类型",
    "标识",
    "日期",
    "时间",
}

SYNONYMS = {
    "amt": "amount",
    "amount": "amount",
    "money": "amount",
    "bal": "balance",
    "balance": "balance",
    "cnt": "count",
    "count": "count",
    "num": "count",
    "times": "count",
    "cust": "customer",
    "customer": "customer",
    "client": "customer",
    "cutp": "counterparty",
    "pty": "party",
    "party": "party",
    "emp": "employee",
    "employee": "employee",
    "org": "organization",
    "organization": "organization",
    "dept": "department",
    "branch": "branch",
    "prd": "product",
    "prod": "product",
    "product": "product",
    "agt": "agreement",
    "agreement": "agreement",
    "contr": "contract",
    "contract": "contract",
    "fee": "fee",
    "rate": "rate",
    "cms": "commission",
    "income": "revenue",
    "revenue": "revenue",
    "sales": "sales",
    "sale": "sales",
    "nom": "notional",
    "prin": "principal",
    "marg": "margin",
    "undrl": "underlying",
    "otc": "otc",
    "hk": "hongkong",
    "hks": "hongkong",
    "inr": "interest",
    "fxr": "fx",
    "rmb": "rmb",
    "cfg": "config",
    "ref": "reference",
    "rela": "relationship",
    "map": "mapping",
    "mapping": "mapping",
    "次数": "count",
    "笔数": "count",
    "数量": "count",
    "金额": "amount",
    "余额": "balance",
    "规模": "balance",
    "客户": "customer",
    "交易对手": "counterparty",
    "当事人": "party",
    "员工": "employee",
    "经办人": "operator",
    "机构": "organization",
    "部门": "department",
    "分公司": "branch",
    "产品": "product",
    "合约": "contract",
    "协议": "agreement",
    "费率": "rate",
    "费用": "fee",
    "佣金": "commission",
    "收入": "revenue",
    "销售": "sales",
    "本金": "principal",
    "名义": "notional",
    "保证金": "margin",
    "标的": "underlying",
    "场外": "otc",
    "衍生品": "derivative",
    "港股": "hongkong",
    "香港": "hongkong",
    "利率": "interest",
    "汇率": "fx",
    "配置": "config",
    "参数": "config",
    "参考": "reference",
    "关系": "relationship",
    "映射": "mapping",
}

CONCEPT_KEYWORDS = {
    "agreement": {"agreement", "contract", "协议", "合约", "提前终止", "状态", "编号"},
    "counterparty_customer": {"counterparty", "customer", "party", "客户", "交易对手", "当事人"},
    "organization_role": {"organization", "department", "branch", "employee", "operator", "机构", "部门", "分公司", "员工", "经办人", "客户经理"},
    "product_underlying": {"product", "underlying", "产品", "标的", "代签", "类别"},
    "sales_revenue": {"sales", "revenue", "commission", "销售", "收入", "计提", "创收", "佣金"},
    "rate_fee": {"rate", "fee", "commission", "费率", "费用", "佣金", "收益率", "利差"},
    "principal_margin": {"notional", "principal", "margin", "本金", "名义", "保证金", "余额"},
    "risk_qualification": {"risk", "qual", "credit", "margin", "风控", "资质", "授信", "保证金", "关联方"},
    "reference_config": {"config", "reference", "mapping", "配置", "参数", "参考", "映射"},
    "time_lifecycle": {"date", "term", "maturity", "期限", "到期", "终止", "计提", "生命周期"},
}


def clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " ".join(clean_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(clean_text(item) for item in value.values())
    text = html.unescape(str(value))
    text = HTML_TAG_RE.sub("", text).strip()
    if text.lower() in {"null", "none", "nan"}:
        return ""
    return text


def tokenize(value: object) -> list[str]:
    text = clean_text(value).lower()
    text = text.replace("_", " ").replace("-", " ").replace(".", " ")
    raw = TOKEN_RE.findall(text)
    tokens: list[str] = []
    for token in raw:
        if not token:
            continue
        if len(token) == 1 and token.isascii() and token.isalpha():
            continue
        mapped = SYNONYMS.get(token, token)
        if mapped in GENERIC_TOKENS:
            continue
        tokens.append(mapped)
    return tokens


def stable_hash(value: object, length: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def graph_paths(base: Path, prefix: str) -> tuple[Path, Path]:
    llm_nodes = base / f"{prefix}_llm_graph_nodes.jsonl"
    llm_edges = base / f"{prefix}_llm_graph_edges.jsonl"
    if llm_nodes.exists() and llm_edges.exists():
        return llm_nodes, llm_edges
    return base / f"{prefix}_graph_nodes.jsonl", base / f"{prefix}_graph_edges.jsonl"


def labels_of(node: dict) -> set[str]:
    return set(node.get("labels", []))


def props_of(item: dict) -> dict:
    return item.get("properties", {})


def node_quality(props: dict) -> float:
    score = props.get("quality_score")
    if isinstance(score, (int, float)):
        return float(score)
    confidence = str(props.get("confidence") or "medium").lower()
    return {"high": 80.0, "medium": 60.0, "low": 35.0}.get(confidence, 45.0)


def normalize_dataset_name(value: object) -> str:
    text = clean_text(value).lower()
    if text.startswith("dataset:"):
        text = text.split(":", 1)[1]
    return text


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def infer_concepts(text: object, *, limit: int = 6) -> list[str]:
    tokens = set(tokenize(text))
    concepts: list[tuple[str, int]] = []
    raw_text = clean_text(text).lower()
    for concept, keywords in CONCEPT_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            key = SYNONYMS.get(keyword, keyword)
            if key in tokens or keyword.lower() in raw_text:
                score += 1
        if score:
            concepts.append((concept, score))
    return [name for name, _ in sorted(concepts, key=lambda item: (-item[1], item[0]))[:limit]]


def infer_table_role(name: str, comment: str, layer: str, column_names: list[str], upstream_count: int, downstream_count: int) -> str:
    name_text = " ".join([name, comment]).lower()
    column_text = " ".join(column_names[:80]).lower()
    if name.startswith("temp.") or ".tmp_" in name or "_tmp" in name or "_mid_" in name:
        return "intermediate_table"
    if layer in {"dm", "dm_index_n"}:
        return "result_fact_table"
    if any(token in name_text for token in ["cfg", "config", "参数", "配置", "ref_", "_ref", "参考"]):
        return "reference_config_table"
    if any(token in name_text for token in ["rela", "mapping", "map", "关系", "映射"]):
        return "relationship_mapping_table"
    if any(token in name_text for token in ["log", "event", "evt", "日志", "事件"]):
        return "event_log_table"
    if any(token in name_text for token in ["base", "info", "master", "客户", "员工", "机构", "基础"]):
        return "master_data_table"
    if layer == "odata":
        return "source_sync_table"
    if any(token in column_text for token in ["cfg", "config", "参数", "配置"]) and len(column_names) <= 20:
        return "reference_config_table"
    if downstream_count >= 3:
        return "shared_upstream_table"
    if upstream_count >= 3:
        return "derived_detail_table"
    return "data_table"


def load_graph(base: Path, prefix: str) -> tuple[dict[str, dict], list[dict], dict]:
    nodes_path, edges_path = graph_paths(base, prefix)
    nodes = {item["id"]: item for item in load_jsonl(nodes_path)}
    edges = list(load_jsonl(edges_path))
    meta = {"nodes_path": str(nodes_path), "edges_path": str(edges_path), "node_count": len(nodes), "edge_count": len(edges)}
    return nodes, edges, meta


def group_by_concept(columns: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for column in columns:
        name = clean_text(column.get("name"))
        comment = clean_text(column.get("comment"))
        if name.lower() in LOW_VALUE_COLUMN_NAMES:
            continue
        concepts = infer_concepts(" ".join([name, comment]), limit=4)
        if concepts:
            for concept in concepts[:2]:
                groups[concept].append(column)
        else:
            tokens = [token for token in tokenize(" ".join([name, comment])) if token not in GENERIC_TOKENS]
            if tokens:
                groups["other_" + tokens[0]].append(column)
    return groups


def compact_counter(values: Iterable[str], limit: int = 10) -> list[dict]:
    counter = Counter(value for value in values if value)
    return [{"value": key, "count": count} for key, count in counter.most_common(limit)]
