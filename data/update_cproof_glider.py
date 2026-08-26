#!/usr/bin/env python3
"""Bring a C-PROOF glider archive up to date.

Run daily (00:00 UTC) by .github/workflows/update-glider-archive.yml:

    python data/update_cproof_glider.py --mode realtime

Rebuild the historical delayed-mode reference record on demand:

    python data/update_cproof_glider.py --mode delayed --rebuild

The update is idempotent -- running it twice appends nothing the second time -- and
self-healing, because each deployment resumes from the last observation already in the
archive rather than from a fixed "last 24 hours" window.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cproof_glider as cproof  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["realtime", "delayed"], default="realtime",
                        help="which C-PROOF data mode to harvest (default: realtime)")
    parser.add_argument("--rebuild", action="store_true",
                        help="discard the existing archive and rebuild it from scratch")
    parser.add_argument("--path", type=Path, default=None,
                        help="archive to write (default: chosen from --mode)")
    parser.add_argument("--start", default=None,
                        help="override the discovery start time, e.g. 2019-01-01T00:00:00Z")
    parser.add_argument("--end", default=None, help="optional discovery end time")
    parser.add_argument("--workers", type=int, default=6,
                        help="concurrent ERDDAP downloads (default: 6)")
    args = parser.parse_args(argv)

    summary = cproof.update_archive(
        mode=args.mode,
        path=args.path,
        rebuild=args.rebuild,
        start=args.start,
        end=args.end,
        max_workers=args.workers,
    )

    print(f"\n{summary['mode']}: checked {summary['datasets']} deployment(s), "
          f"appended {summary['appended']:,}, archive now holds {summary['total']:,} "
          f"observations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
