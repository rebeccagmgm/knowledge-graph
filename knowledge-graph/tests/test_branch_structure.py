#!/usr/bin/env python3

import unittest

from query_layer.branch_structure import attach_structural_summaries


BRANCH_IDS = ["task:A", "task:B", "task:C"]


def shared_group():
    return {
        "branch_ids": BRANCH_IDS,
        "shared_branch_count": 3,
        "entity_count": 4,
        "entity_counts_by_type": {"dataset": 2, "metric": 1, "schedule_task": 1},
        "entity_ids_by_type": {
            "dataset": ["dataset:one", "dataset:two"],
            "metric": ["metric:one"],
            "schedule_task": ["task:shared"],
        },
        "evidence_status": "CONFIRMED",
    }


def relationship_rows():
    relationships = [
        ("edge:1", "DEPENDS_ON", "dataset:one", "task:shared"),
        ("edge:2", "STORED_IN", "metric:one", "dataset:one"),
    ]
    return [
        {
            "branch_id": branch_id,
            "relationship_id": relationship_id,
            "relationship_type": relationship_type,
            "from_entity_id": from_entity_id,
            "to_entity_id": to_entity_id,
        }
        for branch_id in BRANCH_IDS
        for relationship_id, relationship_type, from_entity_id, to_entity_id
        in relationships
    ]


class BranchStructureTest(unittest.TestCase):
    def test_summary_uses_only_relationships_observed_in_every_member_branch(self):
        rows = relationship_rows() + [
            {
                "branch_id": branch_id,
                "relationship_id": "edge:partial",
                "relationship_type": "READS",
                "from_entity_id": "dataset:two",
                "to_entity_id": "dataset:one",
            }
            for branch_id in BRANCH_IDS[:2]
        ] + [
            {
                "observed_branch_ids": BRANCH_IDS,
                "relationship_id": "edge:cross-signature",
                "relationship_type": "READS",
                "from_entity_id": "dataset:two",
                "to_entity_id": "dataset:outside-group",
            }
        ]

        result = attach_structural_summaries([shared_group()], rows)

        self.assertEqual(result[0]["structural_summary"], {
            "membership_mode": "EXACT_BRANCH_MEMBERSHIP",
            "observed_connected_component_count": 1,
            "observed_connected_entity_count": 3,
            "unconnected_shared_entity_count": 1,
            "observed_relationship_count": 2,
            "observed_relationship_types": ["DEPENDS_ON", "STORED_IN"],
            "query_anchor_entity_ids": [
                "dataset:one", "metric:one", "task:shared",
            ],
            "evidence_status": (
                "OBSERVED_IN_EXACT_MEMBERSHIP_INDUCED_SUBGRAPH"
            ),
        })

    def test_full_relationship_key_must_match_across_branches(self):
        rows = relationship_rows()
        rows[-2] = {
            **rows[-2],
            "to_entity_id": "dataset:two",
        }

        result = attach_structural_summaries([shared_group()], rows)

        summary = result[0]["structural_summary"]
        self.assertEqual(summary["observed_relationship_count"], 1)
        self.assertEqual(summary["observed_connected_entity_count"], 2)
        self.assertEqual(summary["unconnected_shared_entity_count"], 2)

    def test_output_is_stable_and_input_groups_are_not_mutated(self):
        group = shared_group()
        rows = relationship_rows()

        forward = attach_structural_summaries([group], rows)
        reverse = attach_structural_summaries([group], list(reversed(rows)))

        self.assertEqual(forward, reverse)
        self.assertNotIn("structural_summary", group)

    def test_successful_empty_observation_returns_bounded_zero_summary(self):
        result = attach_structural_summaries([shared_group()], [])

        summary = result[0]["structural_summary"]
        self.assertEqual(summary["observed_connected_component_count"], 0)
        self.assertEqual(summary["observed_connected_entity_count"], 0)
        self.assertEqual(summary["unconnected_shared_entity_count"], 4)
        self.assertEqual(summary["observed_relationship_count"], 0)
        self.assertEqual(summary["observed_relationship_types"], [])
        self.assertEqual(
            summary["query_anchor_entity_ids"],
            ["dataset:one", "metric:one", "task:shared"],
        )
        self.assertEqual(
            summary["evidence_status"],
            "OBSERVED_IN_EXACT_MEMBERSHIP_INDUCED_SUBGRAPH",
        )

    def test_disconnected_relationship_sets_form_multiple_components(self):
        rows = [
            {
                "observed_branch_ids": BRANCH_IDS,
                "relationship_id": "edge:left",
                "relationship_type": "DEPENDS_ON",
                "from_entity_id": "dataset:one",
                "to_entity_id": "task:shared",
            },
            {
                "observed_branch_ids": BRANCH_IDS,
                "relationship_id": "edge:right",
                "relationship_type": "STORED_IN",
                "from_entity_id": "metric:one",
                "to_entity_id": "dataset:two",
            },
        ]

        result = attach_structural_summaries([shared_group()], rows)

        summary = result[0]["structural_summary"]
        self.assertEqual(summary["observed_connected_component_count"], 2)
        self.assertEqual(summary["observed_connected_entity_count"], 4)
        self.assertEqual(summary["unconnected_shared_entity_count"], 0)
        self.assertEqual(summary["observed_relationship_count"], 2)


if __name__ == "__main__":
    unittest.main()
