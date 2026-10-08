"""Small, schema-driven helpers for Arrow tables."""

import pyarrow as pa


def rows_to_table(rows: list[tuple], schema: pa.Schema) -> pa.Table:
    """Build a table from row tuples while enforcing every Arrow field type."""
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in rows), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )
