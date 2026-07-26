"""Command-line entry point.

    python -m ncsr.cli DOCUMENT.htm HEADER.hdr

Emits the manifest as JSON -- the same payload written to DynamoDB as the
commit marker at the end of a successful run.
"""

from __future__ import annotations

import argparse
import json
import sys

from .ddl import create_all
from .emit import emit
from .normalize import textify
from .pipeline import analyze
from .store import LocalStore


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ncsr", description=__doc__)
    parser.add_argument("document", nargs="?", help="primary N-CSR document (.htm)")
    parser.add_argument("header", nargs="?", help="EDGAR *-index-headers.html")
    parser.add_argument(
        "--spans", action="store_true", help="include Item 7 span offsets"
    )
    parser.add_argument(
        "--emit", metavar="DIR", help="persist evidence, rows and manifest under DIR"
    )
    parser.add_argument(
        "--ddl", metavar="WAREHOUSE", help="print Iceberg DDL for a warehouse URI and exit"
    )
    args = parser.parse_args(argv)

    if args.ddl:
        sys.stdout.write(create_all(args.ddl) + "\n")
        return 0
    if not (args.document and args.header):
        parser.error("document and header are required unless --ddl is given")

    result = analyze(_read(args.document), _read(args.header))
    payload = result.manifest()
    if args.spans:
        payload["spans"] = [
            {"start": s.start, "end": s.end, "length": s.length} for s in result.spans
        ]

    if args.emit:
        markup = _read(args.document)
        outcome = emit(result, textify(markup), LocalStore(args.emit))
        payload["emitted"] = {
            "objects": outcome.objects,
            "sections": outcome.sections,
            "findings": outcome.findings,
            "skipped": outcome.skipped,
        }

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if (result.supported or result.skip_reason) else 1


if __name__ == "__main__":
    raise SystemExit(main())
