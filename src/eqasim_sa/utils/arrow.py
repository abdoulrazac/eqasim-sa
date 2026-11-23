"""Small, schema-driven helpers for Arrow tables."""

import pyarrow as pa
import pyarrow.parquet as pq


def rows_to_table(rows: list[tuple], schema: pa.Schema) -> pa.Table:
    """Build a table from row tuples while enforcing every Arrow field type."""
    return pa.Table.from_arrays(
        [
            pa.array((row[index] for row in rows), type=field.type)
            for index, field in enumerate(schema)
        ],
        schema=schema,
    )


def read_parquet(source, columns: list[str] | None = None) -> pa.Table:
    """Read a Parquet file (path, stream or open ``ParquetFile``) without threads.

    Arrow's worker threads can stay parked on the GIL while Python shuts down,
    which hangs the process at exit, so every Parquet read goes through here.
    """
    parquet = source if hasattr(source, "schema_arrow") else pq.ParquetFile(source)
    return parquet.read(columns=columns, use_threads=False)
