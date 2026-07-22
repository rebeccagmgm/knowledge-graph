"""Evidence-bounded structural summaries for exact branch-sharing groups."""

from __future__ import annotations

from collections import defaultdict


STRUCTURAL_RELATIONSHIP_TYPES = (
    "COMPUTED_BY",
    "DATASET_DEPENDS_ON",
    "DEPENDS_ON",
    "DERIVED_FROM",
    "EMITS_SQL",
    "HAS_COLUMN",
    "HAS_DEFINITION",
    "PRODUCES",
    "READS",
    "STORED_IN",
)

RelationshipKey = tuple[str, str, str, str]


def _relationship_key(row: dict) -> RelationshipKey | None:
    values = (
        row.get("relationship_id"),
        row.get("relationship_type"),
        row.get("from_entity_id"),
        row.get("to_entity_id"),
    )
    if any(value is None or value == "" for value in values):
        return None
    return tuple(str(value) for value in values)


def _connected_component_count(
    connected_entity_ids: set[str],
    adjacency: dict[str, set[str]],
) -> int:
    remaining = set(connected_entity_ids)
    component_count = 0
    while remaining:
        component_count += 1
        pending = [min(remaining)]
        component = set()
        while pending:
            entity_id = pending.pop()
            if entity_id in component:
                continue
            component.add(entity_id)
            pending.extend(sorted(adjacency[entity_id] - component, reverse=True))
        remaining -= component
    return component_count


def _select_query_anchors(
    entity_types: dict[str, str],
    observed_degrees: dict[str, int],
) -> list[str]:
    anchors = []
    selected_types = set()
    remaining = set(entity_types)
    while remaining and len(anchors) < 3:
        highest_degree = max(observed_degrees[entity_id] for entity_id in remaining)
        candidates = [
            entity_id for entity_id in remaining
            if observed_degrees[entity_id] == highest_degree
        ]
        diverse_candidates = [
            entity_id for entity_id in candidates
            if entity_types[entity_id] not in selected_types
        ]
        chosen = min(diverse_candidates or candidates)
        anchors.append(chosen)
        selected_types.add(entity_types[chosen])
        remaining.remove(chosen)
    return anchors


def attach_structural_summaries(
    shared_groups: list[dict],
    topology_rows: list[dict],
) -> list[dict]:
    """Return exact-sharing groups with compact observed topology summaries."""
    relationship_memberships: dict[RelationshipKey, set[str]] = defaultdict(set)
    for row in topology_rows:
        key = _relationship_key(row)
        observed_branch_ids = row.get("observed_branch_ids")
        if not isinstance(observed_branch_ids, list):
            observed_branch_ids = [row.get("branch_id")]
        if key:
            relationship_memberships[key].update(
                str(branch_id) for branch_id in observed_branch_ids
                if branch_id
            )

    results = []
    for group in shared_groups:
        member_branch_ids = set(group["branch_ids"])
        entity_types = {
            entity_id: entity_type
            for entity_type, entity_ids in group["entity_ids_by_type"].items()
            for entity_id in entity_ids
        }
        entity_ids = set(entity_types)
        observed_relationships = sorted(
            key
            for key, memberships in relationship_memberships.items()
            if memberships == member_branch_ids
            and key[2] in entity_ids
            and key[3] in entity_ids
        )

        adjacency: dict[str, set[str]] = {
            entity_id: set() for entity_id in entity_ids
        }
        observed_degrees = {entity_id: 0 for entity_id in entity_ids}
        connected_entity_ids = set()
        for _, _, from_entity_id, to_entity_id in observed_relationships:
            adjacency[from_entity_id].add(to_entity_id)
            adjacency[to_entity_id].add(from_entity_id)
            connected_entity_ids.update((from_entity_id, to_entity_id))
            observed_degrees[from_entity_id] += 1
            if to_entity_id != from_entity_id:
                observed_degrees[to_entity_id] += 1

        summary = {
            "membership_mode": "EXACT_BRANCH_MEMBERSHIP",
            "observed_connected_component_count": _connected_component_count(
                connected_entity_ids, adjacency
            ),
            "observed_connected_entity_count": len(connected_entity_ids),
            "unconnected_shared_entity_count": len(entity_ids) - len(connected_entity_ids),
            "observed_relationship_count": len(observed_relationships),
            "observed_relationship_types": sorted({
                relationship[1] for relationship in observed_relationships
            }),
            "query_anchor_entity_ids": _select_query_anchors(
                entity_types, observed_degrees
            ),
            "evidence_status": (
                "OBSERVED_IN_EXACT_MEMBERSHIP_INDUCED_SUBGRAPH"
            ),
        }
        results.append({**group, "structural_summary": summary})
    return results
