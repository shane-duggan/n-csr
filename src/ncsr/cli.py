"""Command-line entry point.

    python -m ncsr.cli DOCUMENT.htm HEADER.hdr

Emits the manifest as JSON -- the same payload written to DynamoDB as the
commit marker at the end of a successful run.
"""

from __future__ import annotations

import argparse
import json
import sys

from .pipeline import analyze


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ncsr", description=__doc__)
    parser.add_argument("document", help="primary N-CSR document (.htm)")
    parser.add_argument("header", help="EDGAR *-index-headers.html for the filing")
    parser.add_argument(
        "--spans", action="store_true", help="include Item 7 span offsets"
    )
    args = parser.parse_args(argv)

    result = analyze(_read(args.document), _read(args.header))
    payload = result.manifest()
    if args.spans:
        payload["spans"] = [
            {"start": s.start, "end": s.end, "length": s.length} for s in result.spans
        ]

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if (result.supported or result.skip_reason) else 1


if __name__ == "__main__":
    raise SystemExit(main())
