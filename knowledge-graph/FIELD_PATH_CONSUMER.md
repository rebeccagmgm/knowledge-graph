# Field Path Consumer

`kg_probe/field_path_consumer.py` is an external consumer of the existing KG
artifacts. It does not modify the KG builders, graph schema, or existing graph
nodes and edges.

It reads:

- `strategy_column_lineage.json`
- `strategy_graph_edges.jsonl`
- `strategy_sql_statements.json`

and produces target-directed `VALUE_FLOW` paths. `JOIN` and `WHERE` predicates
are emitted separately as `ROWSET_CONTROL`. When an existing KG fact has an
unresolved dataset, an explicit SQL projection alias may narrow it. The
consumer also resolves a logical SQL dataset to its task output through the
existing KG `PRODUCES` edge, scoped by the existing scheduler `DEPENDS_ON`
closure. Unresolved or ambiguous cases remain `CANDIDATE` or `PARTIAL`.

## Usage

```powershell
python -X utf8 kg_probe/field_path_consumer.py `
  <project-dir> `
  --target-dataset dm_rsk_n.v_risk_audit_log `
  --fields entity_id,entity_field_name,modify_date,internal_trade_id `
  --output <project-dir>/kg-field-path.json
```

When `--fields` is omitted, fields found for the target dataset in the existing
column-lineage artifact are used.

The result reports `CONFIRMED`, `CANDIDATE`, and `PARTIAL` paths and includes
`kg_base_modified: false` in its summary. A `COMPLETE` result means every
requested field has only confirmed paths; it does not prove runtime data
arrival or business correctness.
