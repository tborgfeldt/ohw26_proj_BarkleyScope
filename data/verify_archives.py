#!/usr/bin/env python3
"""Checks that the C-PROOF glider archives are safe to build analysis on.

Run this after a rebuild, or any time the archives look suspect::

    python data/verify_archives.py                  # both archives
    python data/verify_archives.py --mode realtime  # only the tracked one

It exercises the things that would quietly poison downstream work if they broke:
CF metadata and time decoding via xarray (which is how most people will open these
files), the ``last_days`` windowing a dashboard depends on, per-variable coverage,
and the updater's idempotency. The catch-up test appends nothing -- it only checks
that a simulated outage would resume from the right place.

Exit status is 0 if every check passes, 1 otherwise. A requested archive that is
missing counts as a failure, which is why ``--mode`` exists: the delayed archive is
gitignored and so never present in a fresh checkout, and the scheduled job -- which
only ever touches the real-time archive -- must not fail on its absence.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np                                                  # noqa: E402
import pandas as pd                                                 # noqa: E402

import cproof_glider as cproof                                      # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def check_xarray_roundtrip(path: Path) -> None:
    """Open the archive the way an analyst would and confirm it decodes sanely."""
    print(f"\nxarray round-trip: {path.name}")
    try:
        import xarray as xr
    except ImportError:
        print("  [SKIP] xarray not installed")
        return

    with xr.open_dataset(path) as ds:
        check("Conventions attribute present", ds.attrs.get("Conventions", "").startswith("CF-"),
              ds.attrs.get("Conventions", "missing"))
        check("featureType is trajectoryProfile",
              ds.attrs.get("featureType") == "trajectoryProfile")

        times = pd.to_datetime(ds["time"].values)
        # The bug this guards: microsecond-resolution parsing divided as if it were
        # nanoseconds silently lands every observation in January 1970.
        check("time decodes to plausible dates", times.min().year >= 2019,
              f"{times.min()} -> {times.max()}")
        check("time is monotonically sane", times.max() <= pd.Timestamp.now("UTC").tz_localize(None)
              + pd.Timedelta(days=1), f"latest {times.max()}")

        depth = ds["depth"].values
        check("depth within instrument range",
              float(np.nanmin(depth)) >= -1 and float(np.nanmax(depth)) <= 1200,
              f"{np.nanmin(depth):.1f} to {np.nanmax(depth):.1f} m")

        for name in cproof.SCIENCE_VARS:
            if name not in ds:
                check(f"{name} present in file", False)
                continue
            values = ds[name].values.astype("f8")
            values = values[np.isfinite(values) & (values < cproof.FILL_VALUE * 0.99)]
            if values.size == 0:
                print(f"  [WARN] {name}: no finite values in this archive")
                continue
            low, high = cproof.QC_RANGES[name]
            check(f"{name} within its QC range",
                  float(values.min()) >= low and float(values.max()) <= high,
                  f"{values.min():.4g} to {values.max():.4g}, "
                  f"{100 * values.size / ds.sizes['obs']:.1f}% coverage")


def check_dashboard_window(path: Path) -> None:
    """The exact call the dashboard makes."""
    print(f"\nDashboard windowing: {path.name}")
    recent = cproof.read_archive(path, last_days=7)
    print(f"  last 7 days: {len(recent):,} observations")

    if len(recent):
        span = pd.Timestamp.now(tz="UTC") - recent["time"].min()
        check("window really is <= 7 days", span <= pd.Timedelta(days=7, hours=1),
              f"oldest is {span.days}d old")
        check("window carries every column", list(recent.columns) == cproof.COLUMNS)
    else:
        print("  [WARN] no observations in the last 7 days -- no glider in the water?")

    subset = cproof.read_archive(path, last_days=30, variables=["temperature"])
    check("variables= subsets the columns",
          list(subset.columns) ==
          ["deployment", "glider", "time", "latitude", "longitude", "depth", "temperature"])

    try:
        cproof.read_archive(path, variables=["not_a_variable"])
        check("unknown variable is rejected", False)
    except ValueError:
        check("unknown variable is rejected", True)


def check_high_water_marks(path: Path) -> None:
    """The updater's state, derived from the archive rather than a sidecar file."""
    print(f"\nUpdate state: {path.name}")
    marks = cproof.high_water_marks(path)
    check("every deployment has a high-water mark", len(marks) > 0, f"{len(marks)} deployments")

    frame = cproof.read_archive(path, variables=["temperature"])
    if frame.empty:
        return
    observed = frame.groupby("deployment")["time"].max()
    agree = all(
        abs(pd.Timestamp(marks[name]) - moment) <= pd.Timedelta(seconds=1)
        for name, moment in observed.items() if name in marks
    )
    check("marks match the data in the file", agree,
          f"checked {len(observed)} deployments")

    # A missed run must resume from the stored mark, not from "yesterday". This is
    # what makes an outage self-healing.
    oldest = min(marks.values())
    check("a stale deployment still resumes from its own mark",
          pd.Timestamp(oldest) < pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1),
          f"oldest mark {oldest}")


def check_idempotency(mode: str) -> None:
    """A second run in a row must append nothing."""
    print(f"\nIdempotency: {mode}")
    summary = cproof.update_archive(mode=mode, log=lambda *_: None)
    check("re-running appends zero rows", summary["appended"] == 0,
          f"appended {summary['appended']:,}, total {summary['total']:,}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["realtime", "delayed", "both"], default="both",
                        help="which archives to verify (default: both). The scheduled job "
                             "passes --mode realtime, because the delayed archive is "
                             "gitignored and never exists in a fresh checkout")
    args = parser.parse_args(argv)

    realtime, delayed = cproof.REALTIME_ARCHIVE, cproof.DELAYED_ARCHIVE
    wanted = {"realtime": [realtime], "delayed": [delayed],
              "both": [realtime, delayed]}[args.mode]

    for path in wanted:
        if not path.exists():
            print(f"MISSING: {path.name} -- build it with "
                  f"`python data/update_cproof_glider.py --mode "
                  f"{'realtime' if path == realtime else 'delayed'}`")
            FAILURES.append(f"{path.name} missing")
            continue
        check_xarray_roundtrip(path)

    if realtime in wanted and realtime.exists():
        check_dashboard_window(realtime)
        check_high_water_marks(realtime)
        check_idempotency("realtime")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
