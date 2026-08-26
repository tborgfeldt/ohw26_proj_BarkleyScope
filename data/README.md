# C-PROOF glider data for BarkleyScope

Glider observations inside the BarkleyScope study box —
longitude **-126.80 to -124.50**, latitude **47.85 to 49.36** — harvested from the
[IOOS Glider DAC ERDDAP](https://gliders.ioos.us/erddap) and kept as two netCDF archives.

**If you are building a visualization, everything you need is `read_archive()`. Skip to
"Reading the data".**

## The two archives

| File | What it is | Size | In git? |
|---|---|---|---|
| `cproof_glider_realtime.nc` | Rolling record from the real-time feeds, updated daily. What a "last 7 days" view reads. | ~3 MB | **Yes** |
| `cproof_glider_delayed.nc` | Historical reference record from the reprocessed `-delayed` datasets. Calibrated, quality controlled, and far denser. | ~37 MB | No — rebuild it |

They are deliberately kept separate rather than blended, because the difference between
them is scientific, not cosmetic:

- **Delayed-mode is calibrated; real-time is not.** Real-time values have had only a
  gross-range screen applied here. Do not present them as calibrated measurements.
- **Delayed-mode is roughly 650× denser.** Deployment `dfo-walle652-20210902` carries
  1,316 real-time observations against **861,530** delayed ones. The real-time feed is
  heavily decimated for bandwidth.
- **They cover different periods.** Delayed-mode data lag real time by months to years,
  because reprocessing happens after the glider is recovered.

A reasonable dashboard shows the real-time archive for "what is happening now" and the
delayed archive for "what is normal for this time of year" — and labels which is which.

The delayed archive is not in git because of its size. Build it once, locally:

```bash
python data/update_cproof_glider.py --mode delayed      # a couple of minutes
```

## Variables

Every archive carries all seven science variables C-PROOF gliders fly, on a single
`obs` dimension, alongside `time`, `latitude`, `longitude`, and `depth`.

| Variable | Units | Notes |
|---|---|---|
| `temperature` | °C | Present on every deployment |
| `salinity` | 1e-3 (PSU) | Present on every deployment |
| `density` | kg m⁻³ | Present on every deployment |
| `oxygen_concentration` | µmol L⁻¹ | **Missing on ~30% of deployments** — not every glider flies an optode |
| `chlorophyll` | mg m⁻³ | Fluorometer; see the note on negatives below |
| `backscatter_700` | m⁻¹ sr⁻¹ | Optical backscatter at 700 nm |
| `cdom` | ppb | Coloured dissolved organic matter |

Variables a glider did not carry come back as `NaN`. **Always check coverage before
plotting** — an oxygen panel will be empty for a third of the deployments:

```python
frame["oxygen_concentration"].notna().mean()      # fraction of rows with oxygen
```

### Negative chlorophyll and backscatter are not errors

The optical channels are raw counts converted with factory coefficients and are **not
dark-corrected**, so small negative values are ordinary instrument behaviour in clear
deep water. The 1st percentile of chlorophyll is about **-0.46 mg m⁻³**. Clipping at
zero would blank roughly a quarter of the bio-optical record. If you need a
presentation-friendly axis, clamp the *colour scale*, not the data.

## Reading the data

```python
import sys; sys.path.insert(0, "data")
import cproof_glider as cproof

# The last week of real-time data — the dashboard call
recent = cproof.read_archive(cproof.REALTIME_ARCHIVE, last_days=7)

# The whole historical record, temperature only (much lighter in memory)
history = cproof.read_archive(cproof.DELAYED_ARCHIVE, variables=["temperature"])

# A fixed window, a couple of variables
window = cproof.read_archive(
    cproof.REALTIME_ARCHIVE,
    start="2026-06-01", end="2026-08-01",
    variables=["temperature", "oxygen_concentration"],
)
```

You get a tidy pandas DataFrame, one row per observation, sorted by time:

```
deployment  glider  time (UTC, tz-aware)  latitude  longitude  depth  <variables…>
```

`deployment` is the ERDDAP dataset ID; `glider` is the vehicle name pulled out of it
(`eva035`, `marvin1003`, …). A single glider appears across several deployments.

**Use `variables=` on the delayed archive.** Reading all seven columns across three
million observations is several hundred megabytes in memory, and most plots need one
or two.

Prefer xarray? The files are CF-1.10 and open directly:

```python
import xarray as xr
ds = xr.open_dataset("data/cproof_glider_delayed.nc")
```

## Keeping it up to date

```bash
python data/update_cproof_glider.py --mode realtime     # what the daily job runs
python data/update_cproof_glider.py --mode delayed      # refresh the reference record
python data/verify_archives.py                          # confirm nothing is broken
```

Updates are **additive and idempotent**. Each deployment resumes from the last
observation already stored — state is derived from the archive itself, not from a
sidecar file — so running twice appends nothing, and a run after a missed week
backfills the whole gap rather than only the last day.

That design is a response to a real property of the source: **the DAC catalogue is not
stable.** Repeated identical searches have returned 10, 25, and 38 datasets within
minutes as the server reloads datasets. Because updates only ever add, a deployment
missed by one run is picked up by the next and successive runs converge on full
coverage. For the same reason, **avoid `--rebuild`** unless you have a specific reason:
if the catalogue happens to be thin at that moment, the rebuilt archive will be thin too.

## Files

| File | Purpose |
|---|---|
| `cproof_glider.py` | The shared library — discovery, fetching, QC, netCDF I/O, update logic |
| `update_cproof_glider.py` | CLI entry point for the scheduled job |
| `verify_archives.py` | Post-rebuild checks; exits non-zero if anything is wrong |
| `Glider_ERDDAP_DataPull.ipynb` | Annotated walkthrough of the same pipeline |

The notebook and the scheduled job both import `cproof_glider.py`, so they cannot drift
apart — a GitHub Action cannot import functions defined in notebook cells.
