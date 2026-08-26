"""C-PROOF glider observations for the BarkleyScope project.

This module is the single implementation shared by the exploration notebook
(``Glider_ERDDAP_DataPull.ipynb``) and the scheduled updater
(``update_cproof_glider.py``), so the two cannot drift apart.

It maintains two netCDF archives of glider data inside the BarkleyScope study
box, each carrying every science variable C-PROOF gliders routinely fly --
temperature, salinity, density, oxygen, chlorophyll, backscatter, and CDOM:

``cproof_glider_delayed.nc``
    The historical reference record, built from the reprocessed ``-delayed``
    datasets. These are calibrated, quality controlled, and far higher
    resolution than the real-time feeds. Rebuilt on demand; gitignored because
    of its size.

``cproof_glider_realtime.nc``
    The rolling record, built from the real-time feeds and appended to daily.
    This is what a "last 7 days" dashboard reads, and it is committed to the repo.

The full science variable set is carried even when a given analysis only needs one
of them, so that adding a parameter later is a plotting change rather than a
re-harvest. Not every glider flies every sensor -- roughly a third of C-PROOF
deployments have no oxygen optode -- so variables are requested per deployment and
absent ones are stored as missing values.

Data source: the IOOS Glider DAC ERDDAP, https://gliders.ioos.us/erddap
"""

from __future__ import annotations

import io
import time as _time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

ERDDAP = "https://gliders.ioos.us/erddap"
INSTITUTION = "C-PROOF"

#: BarkleyScope study region (degrees east, degrees north).
BOX = {"lon": (-126.80, -124.50), "lat": (47.85, 49.36)}

#: Earliest C-PROOF deployment on the glider DAC is July 2019.
START_OF_RECORD = "2019-01-01T00:00:00Z"

#: How far back the daily job looks for *datasets* to update. Generous on purpose:
#: it is what lets the job recover after a multi-week outage.
DISCOVERY_LOOKBACK_DAYS = 30

CORE_VARS = ["time", "latitude", "longitude", "depth"]

#: Every science variable C-PROOF gliders carry. Requested per deployment -- a glider
#: without an optode simply yields missing oxygen rather than failing the request.
SCIENCE_VARS = [
    "temperature",            # degree_C
    "salinity",               # 1e-3 (PSU)
    "density",                # kg m-3
    "oxygen_concentration",   # umol L-1
    "chlorophyll",            # mg m-3
    "backscatter_700",        # m-1 sr-1
    "cdom",                   # ppb
]

#: Gross-range checks. Values outside these bounds are blanked, not dropped, so one
#: bad sensor does not cost the other variables on the same observation. Bounds are
#: deliberately generous -- this is a gross-range screen for obvious instrument
#: failures and fill values, not a substitute for the provider's QARTOD flags.
QC_RANGES = {
    "temperature": (-2.0, 30.0),
    "salinity": (2.0, 42.0),
    "density": (1000.0, 1040.0),
    "oxygen_concentration": (0.0, 600.0),
    # The optical channels are raw counts converted with factory coefficients and are
    # not dark-corrected, so small negative values are ordinary instrument behaviour in
    # clear deep water rather than bad data. Floors of 0 here would blank roughly a
    # quarter of the chlorophyll and backscatter record: the observed 1st percentiles
    # are about -0.46 mg m-3 and -1.0e-4 m-1 sr-1 respectively.
    "chlorophyll": (-2.0, 60.0),
    "backscatter_700": (-0.001, 0.05),
    "cdom": (-5.0, 100.0),
    "depth": (-1.0, 1200.0),
}

DATA_DIR = Path(__file__).resolve().parent
REALTIME_ARCHIVE = DATA_DIR / "cproof_glider_realtime.nc"
DELAYED_ARCHIVE = DATA_DIR / "cproof_glider_delayed.nc"

TIME_UNITS = "seconds since 1970-01-01T00:00:00Z"

#: Columns of every DataFrame this module passes around.
COLUMNS = ["deployment", "glider", "time", "latitude", "longitude", "depth"] + SCIENCE_VARS


def archive_path(mode: str) -> Path:
    """Return the archive file matching ``mode`` (``"realtime"`` or ``"delayed"``)."""
    if mode == "realtime":
        return REALTIME_ARCHIVE
    if mode == "delayed":
        return DELAYED_ARCHIVE
    raise ValueError(f"mode must be 'realtime' or 'delayed', not {mode!r}")


def utcnow_iso(offset_days: float = 0.0) -> str:
    """An ERDDAP-shaped UTC timestamp, optionally shifted into the past."""
    moment = datetime.now(timezone.utc) - timedelta(days=offset_days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# ERDDAP access
# --------------------------------------------------------------------------------------

#: The glider DAC returns transient 5xx and drops connections under load, which an
#: unattended daily job will hit sooner or later. Retry those, but never a 404.
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 5


def erddap_csv(url: str, timeout: int = 300, attempts: int = RETRY_ATTEMPTS,
               log=None) -> pd.DataFrame | None:
    """Fetch an ERDDAP ``.csv`` response as a DataFrame.

    Returns ``None`` when the query matched nothing -- ERDDAP signals that with a
    404 rather than an empty body. Row 1 of an ERDDAP CSV holds units, not data,
    so it is skipped. Server errors and dropped connections are retried with a
    linear backoff; the last failure is re-raised if every attempt fails.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 404:
                return None
            if response.status_code >= 500:
                response.raise_for_status()
        except (requests.RequestException, OSError) as error:
            if attempt == attempts:
                raise
            delay = RETRY_BACKOFF_SECONDS * attempt
            if log:
                log(f"  ERDDAP {type(error).__name__}, retrying in {delay}s "
                    f"(attempt {attempt}/{attempts - 1})")
            _time.sleep(delay)
            continue

        response.raise_for_status()
        return pd.read_csv(io.StringIO(response.text), skiprows=[1], low_memory=False)

    return None  # unreachable; the final attempt either returns or raises


def find_datasets(mode: str = "realtime", start_time: str | None = None,
                  end_time: str | None = None, log=None) -> pd.DataFrame:
    """Find C-PROOF datasets overlapping the study box within a time window.

    ``mode`` selects real-time feeds or the reprocessed ``-delayed`` archives.
    Returns a DataFrame with ``datasetID``, ``glider``, and the dataset's own
    time and space extents, most recently active first.
    """
    (lon_min, lon_max), (lat_min, lat_max) = BOX["lon"], BOX["lat"]
    query = (
        "search/advanced.csv?page=1&itemsPerPage=1000&protocol=tabledap"
        "&cdm_data_type=(ANY)&institution=(ANY)&ioos_category=(ANY)&keywords=(ANY)"
        "&long_name=(ANY)&standard_name=(ANY)&variableName=(ANY)"
        f"&minLon={lon_min}&maxLon={lon_max}&minLat={lat_min}&maxLat={lat_max}"
        f"&minTime={start_time or ''}&maxTime={end_time or ''}"
        f"&searchFor={INSTITUTION}"
    )
    hits = erddap_csv(f"{ERDDAP}/{query}", log=log)
    if hits is None:
        return pd.DataFrame(columns=["datasetID", "glider", "minTime", "maxTime"])

    # searchFor is a free-text match, so confirm the institution attribute too.
    hits = hits[hits["Institution"] == INSTITUTION]

    catalogue = erddap_csv(
        f"{ERDDAP}/tabledap/allDatasets.csv?datasetID,minTime,maxTime,"
        "minLongitude,maxLongitude,minLatitude,maxLatitude",
        log=log,
    )
    found = catalogue[catalogue.datasetID.isin(hits["Dataset ID"])].copy()

    delayed = found.datasetID.str.endswith("-delayed")
    found = found[delayed] if mode == "delayed" else found[~delayed]

    found["glider"] = found.datasetID.str.split("-").str[1]
    return found.sort_values("maxTime", ascending=False).reset_index(drop=True)


#: Info-endpoint results are stable for the life of a run and each lookup is a
#: round trip, so cache them. Keyed by dataset ID.
_variable_cache: dict[str, set[str]] = {}


def dataset_variables(dataset_id: str, log=None) -> set[str]:
    """Which variables a deployment actually publishes.

    Necessary because ERDDAP rejects the whole request if it names a variable the
    dataset does not have, and C-PROOF gliders are not identically equipped: about a
    third of the deployments in this box fly no oxygen optode. Asking first is one
    cheap round trip per deployment; guessing wrong costs the entire download.

    Returns an empty set if the info endpoint cannot be read, which callers treat as
    "fall back to the core variables only".
    """
    if dataset_id in _variable_cache:
        return _variable_cache[dataset_id]

    try:
        info = erddap_csv(f"{ERDDAP}/info/{dataset_id}/index.csv", timeout=60, log=log)
    except Exception as error:                                      # noqa: BLE001
        if log:
            log(f"  {dataset_id}: could not read variable list ({error})")
        info = None

    names: set[str] = set()
    if info is not None and "Variable Name" in info:
        names = set(info.loc[info["Row Type"] == "variable", "Variable Name"])

    _variable_cache[dataset_id] = names
    return names


def science_variables(dataset_id: str, log=None) -> list[str]:
    """The subset of :data:`SCIENCE_VARS` this deployment can supply, in fixed order."""
    available = dataset_variables(dataset_id, log=log)
    if not available:
        return []
    return [name for name in SCIENCE_VARS if name in available]


def build_url(dataset_id: str, start_time: str | None = None, end_time: str | None = None,
              exclusive_start: bool = False, variables: list[str] | None = None) -> str:
    """Assemble the tabledap URL for one dataset, constrained to the study box.

    The box is applied server-side, and because the BarkleyScope region is a single
    rectangle the ERDDAP constraint is exact -- no client-side spatial filtering needed.
    Set ``exclusive_start`` for incremental updates so the observation we already hold
    is not fetched again.
    """
    (lon_min, lon_max), (lat_min, lat_max) = BOX["lon"], BOX["lat"]
    constraints = (
        f"&longitude>={lon_min}&longitude<={lon_max}"
        f"&latitude>={lat_min}&latitude<={lat_max}"
    )
    if start_time:
        constraints += f"&time{'>' if exclusive_start else '>='}{start_time}"
    if end_time:
        constraints += f"&time<={end_time}"

    science = SCIENCE_VARS if variables is None else variables
    query_vars = ",".join(CORE_VARS + list(science))
    return f"{ERDDAP}/tabledap/{dataset_id}.csv?{query_vars}{constraints}"


def fetch_dataset(dataset_id: str, start_time: str | None = None, end_time: str | None = None,
                  exclusive_start: bool = False, log=None) -> pd.DataFrame:
    """Download observations for one deployment inside the study box.

    Only the science variables this deployment actually carries are requested; the
    rest come back as missing values so that every frame this module produces has
    the same :data:`COLUMNS`, whatever the glider was carrying.
    """
    science = science_variables(dataset_id, log=log)
    data = erddap_csv(
        build_url(dataset_id, start_time, end_time, exclusive_start, variables=science),
        log=log,
    )
    if data is None or data.empty:
        return pd.DataFrame(columns=COLUMNS)

    data["time"] = pd.to_datetime(data["time"], utc=True, format="ISO8601")
    for column in CORE_VARS[1:] + science:
        data[column] = pd.to_numeric(data[column], errors="coerce")

    for column in SCIENCE_VARS:
        if column not in data:
            data[column] = np.nan

    data.insert(0, "deployment", dataset_id)
    data.insert(1, "glider", dataset_id.split("-")[1])
    return data[COLUMNS]


def fetch_many(requests_: list[tuple[str, str | None, bool]], end_time: str | None = None,
               max_workers: int = 6, log=print) -> pd.DataFrame:
    """Download several deployments concurrently.

    ``requests_`` is a list of ``(dataset_id, start_time, exclusive_start)`` tuples.
    A deployment that fails is logged and skipped rather than aborting the run.
    """
    def one(job):
        dataset_id, start_time, exclusive = job
        try:
            return dataset_id, fetch_dataset(dataset_id, start_time, end_time,
                                             exclusive, log=log), None
        except Exception as error:                                  # noqa: BLE001
            return dataset_id, pd.DataFrame(columns=COLUMNS), error

    frames = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for dataset_id, frame, error in pool.map(one, requests_):
            if error is not None:
                log(f"  {dataset_id}: FAILED ({error})")
                continue
            log(f"  {dataset_id}: {len(frame):>9,} observations")
            if not frame.empty:
                frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------------------
# Quality control
# --------------------------------------------------------------------------------------

def apply_gross_range_qc(data: pd.DataFrame, ranges: dict | None = None,
                         log=print) -> pd.DataFrame:
    """Blank values outside a plausible range, then drop rows left with no data at all.

    Real-time glider feeds are not calibrated and carry obviously bad values. Blanking
    rather than dropping keeps a row's other variables usable -- a failed optode should
    not cost that observation its temperature. A row is only discarded when every
    science variable on it is missing, which means it carries nothing an analysis
    could use.
    """
    ranges = QC_RANGES if ranges is None else ranges
    clean = data.copy()

    for variable, (low, high) in ranges.items():
        if variable not in clean:
            continue
        present = clean[variable].notna()
        bad = present & ~clean[variable].between(low, high)
        if bad.any():
            log(f"  {variable}: flagged {bad.sum():,} of {present.sum():,} "
                f"values outside [{low}, {high}]")
            clean.loc[bad, variable] = np.nan

    present = [name for name in SCIENCE_VARS if name in clean]
    before = len(clean)
    if present:
        clean = clean[clean[present].notna().any(axis=1)]
    if before != len(clean):
        log(f"  dropped {before - len(clean):,} rows with no usable science data")

    return clean.reset_index(drop=True)


def tidy(data: pd.DataFrame, log=print) -> pd.DataFrame:
    """QC, de-duplicate, and sort a freshly downloaded batch."""
    if data.empty:
        return data
    clean = apply_gross_range_qc(data, log=log)
    before = len(clean)
    clean = clean.drop_duplicates(subset=["deployment", "time", "depth"])
    if before != len(clean):
        log(f"  dropped {before - len(clean):,} duplicate observations")
    return clean.sort_values(["deployment", "time", "depth"]).reset_index(drop=True)


# --------------------------------------------------------------------------------------
# netCDF archive
# --------------------------------------------------------------------------------------

_VARIABLE_ATTRS = {
    "time": {"standard_name": "time", "long_name": "profile time", "units": TIME_UNITS,
             "calendar": "standard", "axis": "T"},
    "latitude": {"standard_name": "latitude", "long_name": "profile latitude",
                 "units": "degrees_north", "axis": "Y"},
    "longitude": {"standard_name": "longitude", "long_name": "profile longitude",
                  "units": "degrees_east", "axis": "X"},
    "depth": {"standard_name": "depth", "long_name": "depth", "units": "m",
              "positive": "down", "axis": "Z"},
    "temperature": {"standard_name": "sea_water_temperature",
                    "long_name": "sea water temperature", "units": "degree_C"},
    "salinity": {"standard_name": "sea_water_practical_salinity",
                 "long_name": "sea water practical salinity", "units": "1e-3"},
    "density": {"standard_name": "sea_water_density",
                "long_name": "sea water density", "units": "kg m-3"},
    "oxygen_concentration": {
        "standard_name": "mole_concentration_of_dissolved_molecular_oxygen_in_sea_water",
        "long_name": "dissolved oxygen concentration", "units": "umol L-1",
        "comment": "absent on deployments flown without an oxygen optode"},
    "chlorophyll": {
        "standard_name": "mass_concentration_of_chlorophyll_in_sea_water",
        "long_name": "chlorophyll concentration", "units": "mg m-3"},
    "backscatter_700": {
        "long_name": "optical backscatter at 700 nm", "units": "m-1 sr-1"},
    "cdom": {"long_name": "coloured dissolved organic matter", "units": "ppb"},
}

#: Coordinates keep float64; science variables are float32, which is well beyond
#: sensor precision and halves the archive on disk.
_DTYPES = {"time": "f8", "latitude": "f8", "longitude": "f8", "depth": "f4"}
_DTYPES.update({name: "f4" for name in SCIENCE_VARS})

#: netCDF fill value used for a variable a deployment did not carry.
FILL_VALUE = np.float32(9.96921e36)


def _history_line(message: str) -> str:
    return f"{utcnow_iso()}: {message}"


def _epoch_seconds(times: pd.Series) -> np.ndarray:
    """Seconds since 1970 as float64, whatever datetime resolution pandas handed us.

    Do not reach for ``.astype("int64") / 1e9`` here: pandas 2 parses these ERDDAP
    timestamps at microsecond resolution, not nanosecond, so that division silently
    lands every observation in January 1970.
    """
    return ((times - pd.Timestamp("1970-01-01T00:00:00Z")) / pd.Timedelta(seconds=1)).to_numpy()


def create_archive(path: Path, mode: str) -> None:
    """Create an empty archive with the right dimensions, variables, and metadata."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    (lon_min, lon_max), (lat_min, lat_max) = BOX["lon"], BOX["lat"]

    with netCDF4.Dataset(path, "w", format="NETCDF4") as nc:
        nc.createDimension("obs", None)              # unlimited: appended to in place
        nc.createDimension("deployment", None)

        names = nc.createVariable("deployment", str, ("deployment",))
        names.long_name = "ERDDAP dataset ID of the glider deployment"
        names.cf_role = "trajectory_id"

        index = nc.createVariable("deployment_index", "i4", ("obs",),
                                  zlib=True, complevel=4, chunksizes=(100_000,))
        index.long_name = "index into the deployment variable"
        index.instance_dimension = "deployment"

        for name, dtype in _DTYPES.items():
            # Science variables get an explicit fill value: a deployment flown without
            # an optode still occupies rows here, with oxygen simply absent.
            kwargs = {"fill_value": FILL_VALUE} if name in SCIENCE_VARS else {}
            variable = nc.createVariable(name, dtype, ("obs",), zlib=True, complevel=4,
                                         chunksizes=(100_000,), **kwargs)
            variable.setncatts(_VARIABLE_ATTRS[name])

        nc.setncatts({
            "Conventions": "CF-1.10",
            "featureType": "trajectoryProfile",
            "title": f"C-PROOF glider observations in the BarkleyScope region ({mode})",
            "summary": (
                f"Temperature, salinity, density, dissolved oxygen, chlorophyll, optical "
                f"backscatter, and CDOM from {INSTITUTION} gliders inside the BarkleyScope "
                f"study box, harvested from the IOOS Glider DAC ERDDAP. This archive holds "
                f"{mode} data. Not every deployment carries every sensor; variables a "
                f"glider did not fly are stored as missing values."
            ),
            "institution": INSTITUTION,
            "source": f"{ERDDAP} ({mode} datasets)",
            "processing_level": ("delayed-mode, calibrated and quality controlled by the "
                                 "data provider" if mode == "delayed" else
                                 "real-time, uncalibrated; gross-range screened only"),
            "geospatial_lon_min": lon_min, "geospatial_lon_max": lon_max,
            "geospatial_lat_min": lat_min, "geospatial_lat_max": lat_max,
            "date_created": utcnow_iso(),
            "history": _history_line(f"created empty {mode} archive"),
        })


def append_archive(path: Path, data: pd.DataFrame, message: str | None = None) -> int:
    """Append observations to an archive, creating it if absent. Returns rows written."""
    path = Path(path)
    if data.empty:
        return 0

    with netCDF4.Dataset(path, "a") as nc:
        names = list(nc.variables["deployment"][:])
        lookup = {name: position for position, name in enumerate(names)}

        for name in data["deployment"].unique():
            if name not in lookup:
                lookup[name] = len(lookup)
                nc.variables["deployment"][lookup[name]] = name

        start = nc.dimensions["obs"].size
        stop = start + len(data)

        nc.variables["deployment_index"][start:stop] = (
            data["deployment"].map(lookup).to_numpy(dtype="i4")
        )
        nc.variables["time"][start:stop] = _epoch_seconds(data["time"])
        for name in ["latitude", "longitude", "depth"] + SCIENCE_VARS:
            values = data[name].to_numpy(dtype="f8")
            # Write NaN through as the fill value so absent sensors read back as
            # missing rather than as a spurious 9.97e36.
            nc.variables[name][start:stop] = np.where(
                np.isnan(values), FILL_VALUE, values
            ) if name in SCIENCE_VARS else values

        _refresh_coverage(nc)
        nc.history = "\n".join([
            nc.history,
            _history_line(message or f"appended {len(data):,} observations"),
        ])

    return len(data)


def _refresh_coverage(nc: netCDF4.Dataset) -> None:
    """Update the time-coverage globals from what is now in the file."""
    if nc.dimensions["obs"].size == 0:
        return
    times = nc.variables["time"][:]
    for attribute, value in (("time_coverage_start", times.min()),
                             ("time_coverage_end", times.max())):
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
        setattr(nc, attribute, moment.strftime("%Y-%m-%dT%H:%M:%SZ"))


def _as_utc(moment) -> pd.Timestamp:
    """Coerce a bound to a UTC Timestamp, whether or not it already carries a zone.

    ``pd.Timestamp(value, tz="UTC")`` raises if ``value`` is already tz-aware, which
    is exactly what ``last_days`` produces -- so the two ways of asking for a window
    have to be normalised separately rather than through one constructor.
    """
    stamp = pd.Timestamp(moment)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def read_archive(path: Path, start: str | None = None, end: str | None = None,
                 last_days: float | None = None,
                 variables: list[str] | None = None) -> pd.DataFrame:
    """Read an archive into a DataFrame, optionally windowed in time.

    This is the entry point the dashboard and the analysis notebooks use::

        read_archive(REALTIME_ARCHIVE, last_days=7)
        read_archive(DELAYED_ARCHIVE, variables=["temperature", "oxygen_concentration"])

    ``last_days`` is the convenience a "last week" view wants. ``variables`` limits which
    science columns are loaded, which matters on the delayed archive -- reading all seven
    across three million observations is several hundred megabytes in memory, and most
    plots need one or two. Absent sensors come back as ``NaN``.
    """
    path = Path(path)
    wanted = list(SCIENCE_VARS if variables is None else variables)
    unknown = [name for name in wanted if name not in SCIENCE_VARS]
    if unknown:
        raise ValueError(f"unknown variable(s) {unknown}; choose from {SCIENCE_VARS}")
    columns = ["deployment", "glider", "time", "latitude", "longitude", "depth"] + wanted

    if not path.exists():
        return pd.DataFrame(columns=columns)

    with netCDF4.Dataset(path, "r") as nc:
        if nc.dimensions["obs"].size == 0:
            return pd.DataFrame(columns=columns)
        nc.set_auto_mask(False)
        names = np.array(list(nc.variables["deployment"][:]))
        fields = {
            "deployment": names[nc.variables["deployment_index"][:]],
            "time": pd.to_datetime(nc.variables["time"][:], unit="s", utc=True),
            "latitude": nc.variables["latitude"][:],
            "longitude": nc.variables["longitude"][:],
            "depth": nc.variables["depth"][:],
        }
        for name in wanted:
            values = np.asarray(nc.variables[name][:], dtype="f4")
            # Auto-masking is off for speed, so translate the fill value ourselves.
            fields[name] = np.where(values >= FILL_VALUE * 0.99, np.nan, values)
        frame = pd.DataFrame(fields)

    frame.insert(1, "glider", frame["deployment"].str.split("-").str[1])

    if last_days is not None:
        start = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=last_days)
    if start is not None:
        frame = frame[frame["time"] >= _as_utc(start)]
    if end is not None:
        frame = frame[frame["time"] <= _as_utc(end)]

    return frame[columns].sort_values("time").reset_index(drop=True)


def high_water_marks(path: Path) -> dict[str, str]:
    """Latest stored observation time per deployment, as ERDDAP-shaped strings.

    This is the archive's own state -- deliberately derived from the data rather than
    kept in a sidecar file, so the updater cannot drift out of sync with what it holds
    and a missed run simply picks up where the data left off.
    """
    path = Path(path)
    if not path.exists():
        return {}

    with netCDF4.Dataset(path, "r") as nc:
        if nc.dimensions["obs"].size == 0:
            return {}
        names = np.array(list(nc.variables["deployment"][:]))
        index = np.asarray(nc.variables["deployment_index"][:])
        times = np.asarray(nc.variables["time"][:])

    latest = {}
    for position in np.unique(index):
        moment = datetime.fromtimestamp(float(times[index == position].max()), tz=timezone.utc)
        latest[str(names[position])] = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    return latest


# --------------------------------------------------------------------------------------
# The update itself
# --------------------------------------------------------------------------------------

def update_archive(mode: str = "realtime", path: Path | None = None, rebuild: bool = False,
                   start: str | None = None, end: str | None = None,
                   max_workers: int = 6, log=print) -> dict:
    """Bring an archive up to date and return a summary of what changed.

    Idempotent: each deployment is fetched only from the last observation already
    stored, so a second run in a row appends nothing, and a run after a missed week
    backfills the whole gap rather than only the last day.

    Updates are additive, which matters more than it looks: the glider DAC's catalogue
    is not stable from one minute to the next -- repeated identical searches have
    returned 25, 29, and 38 datasets as the server reloads them. Because state lives in
    the archive rather than in a sidecar file, a deployment missed by one run is simply
    picked up by the next, and successive runs converge on complete coverage.
    """
    path = Path(path) if path is not None else archive_path(mode)

    if rebuild and path.exists():
        log(f"WARNING: rebuilding {path.name} from scratch. If the DAC catalogue is "
            f"incomplete right now, the rebuilt archive will be too -- prefer an "
            f"additive update (no --rebuild), which converges on full coverage.")
        path.unlink()
    if not path.exists():
        create_archive(path, mode)

    marks = high_water_marks(path)

    # A full history sweep for the delayed reference record, a rolling window for the
    # daily real-time job -- wide enough to recover from an outage.
    if start is not None:
        discovery_start = start
    elif mode == "delayed" or not marks:
        discovery_start = START_OF_RECORD
    else:
        discovery_start = utcnow_iso(offset_days=DISCOVERY_LOOKBACK_DAYS)

    datasets = find_datasets(mode=mode, start_time=discovery_start, end_time=end, log=log)
    log(f"{len(datasets)} {INSTITUTION} {mode} deployment(s) to check "
        f"since {discovery_start}")
    if datasets.empty:
        return {"mode": mode, "path": str(path), "datasets": 0, "appended": 0,
                "total": _archive_size(path)}

    jobs = [(dataset_id, marks.get(dataset_id, START_OF_RECORD), dataset_id in marks)
            for dataset_id in datasets.datasetID]

    log("Downloading:")
    fetched = fetch_many(jobs, end_time=end, max_workers=max_workers, log=log)

    if fetched.empty:
        log("Nothing new.")
        return {"mode": mode, "path": str(path), "datasets": len(datasets), "appended": 0,
                "total": _archive_size(path)}

    log("Quality control:")
    clean = tidy(fetched, log=log)

    # Belt and braces: never re-append anything at or before a stored high-water mark.
    if marks:
        keep = np.ones(len(clean), dtype=bool)
        for deployment, mark in marks.items():
            rows = clean["deployment"] == deployment
            keep &= ~(rows & (clean["time"] <= pd.Timestamp(mark)))
        clean = clean[keep].reset_index(drop=True)

    appended = append_archive(
        path, clean, message=f"appended {len(clean):,} {mode} observations"
    )
    total = _archive_size(path)
    log(f"Appended {appended:,} observations -> {total:,} total in {path.name}")

    return {"mode": mode, "path": str(path), "datasets": len(datasets),
            "appended": appended, "total": total}


def _archive_size(path: Path) -> int:
    path = Path(path)
    if not path.exists():
        return 0
    with netCDF4.Dataset(path, "r") as nc:
        return nc.dimensions["obs"].size
