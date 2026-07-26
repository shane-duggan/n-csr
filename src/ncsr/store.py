"""Storage boundary.

Three destinations, deliberately separate:

* the **archival tree** -- immutable evidence, browsable by fund, never queried
  by Athena;
* the **analytical tables** -- compacted Iceberg rows that answer questions;
* the **manifest** -- the commit marker, written *last*.

Ordering is the durability design. Rows are idempotent on
``(accession, pipeline_version)``, so a crash mid-write leaves orphaned rows
that the next run supersedes. Only the manifest marks a filing complete, which
means a partial run is never mistaken for a finished one. This replaces a
transaction: DynamoDB caps ``TransactWriteItems`` at 100 items, and Guardian VP
Trust alone produces far more section rows than that.

``LocalStore`` is the reference implementation and what the tests exercise. An
S3 + Athena implementation satisfies the same protocol; nothing above this
module knows which is in use.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional


class Store:
    """Protocol for a destination. Implementations must be idempotent."""

    def put_object(self, key: str, body: str) -> None:
        raise NotImplementedError

    def append_rows(self, table: str, rows: Iterable[Dict[str, Any]]) -> int:
        raise NotImplementedError

    def get_manifest(self, accession: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def put_manifest(self, manifest: Dict[str, Any]) -> None:
        raise NotImplementedError

    def is_processed(self, accession: str, pipeline_version: int) -> bool:
        """True when this accession is already complete at this version.

        A stored manifest from an older pipeline version does not count, which
        is what makes a version bump trigger a backfill without deleting
        anything first.
        """
        manifest = self.get_manifest(accession)
        return bool(
            manifest and manifest.get("pipeline_version") == pipeline_version
        )


class LocalStore(Store):
    """Filesystem-backed store mirroring the S3 layout."""

    def __init__(self, root: str):
        self.root = root

    def _path(self, *parts: str) -> str:
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def put_object(self, key: str, body: str) -> None:
        with open(self._path(key), "w", encoding="utf-8") as handle:
            handle.write(body)

    def append_rows(self, table: str, rows: Iterable[Dict[str, Any]]) -> int:
        materialized: List[Dict[str, Any]] = list(rows)
        if not materialized:
            return 0
        # One file per (table, fiscal_period) stands in for an Iceberg append;
        # the partitioning is what matters at this layer.
        by_partition: Dict[str, List[Dict[str, Any]]] = {}
        for row in materialized:
            by_partition.setdefault(row.get("fiscal_period", "unknown"), []).append(row)
        for partition, batch in by_partition.items():
            path = self._path("tables", table, f"fiscal_period={partition}", "rows.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                for row in batch:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        return len(materialized)

    def get_manifest(self, accession: str) -> Optional[Dict[str, Any]]:
        path = os.path.join(self.root, "manifests", f"{accession}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def put_manifest(self, manifest: Dict[str, Any]) -> None:
        accession = manifest["accession"]
        with open(self._path("manifests", f"{accession}.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
