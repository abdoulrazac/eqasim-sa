"""Test-only stand-in for PublishedAnalysis, duck-typed by the figure renderers."""

from pathlib import Path

import pyarrow as pa


class StubAnalysis:
    def __init__(
        self,
        method: str,
        inference_status: str | None,
        governed_report: dict,
        tables: dict[str, pa.Table],
        generation_id: str = "0" * 64,
        seed_role: str = "replication",
        campaign_root: Path = Path("."),
        documents: dict[str, dict] | None = None,
    ) -> None:
        self.method = method
        self.inference_status = inference_status
        self.governed_report = governed_report
        self.generation_id = generation_id
        self.seed_role = seed_role
        self.campaign_root = campaign_root
        self._tables = tables
        self._documents = documents or {}
        self.outputs = {role: Path(f"{role}.parquet") for role in tables}
        self.outputs.update({role: Path(f"{role}.json") for role in self._documents})

    def table(self, role: str) -> pa.Table:
        if role not in self._tables:
            raise KeyError(f"analysis generation publishes no {role!r} output")
        return self._tables[role]

    def document(self, role: str) -> dict:
        if role not in self._documents:
            raise KeyError(f"analysis generation publishes no {role!r} output")
        return self._documents[role]
