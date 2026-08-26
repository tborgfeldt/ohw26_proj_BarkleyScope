# BarkleyScope glider archive — handoff

**Date:** 2026-08-26 (updated)
**Status:** pushed, running, and open for review. Nothing is blocked.

---

## What happened this session

The previous handoff's open decision — which remote — is resolved, and its premise turned
out to be wrong in a way worth recording.

**You have admin on the org repo.** The old plan assumed `oceanhackweek` would restrict
the Actions token, which is why the workflow was written for the fork. It wouldn't have:
neither `main` is branch-protected, and although both repos default the Actions token to
read-only, the workflow declares `permissions: contents: write`, which overrides that
default. The job would have run in either repo unchanged.

The fork was still the right home for the cron, for a different reason: it commits a
3.1 MB binary back to `main` every night — roughly 1.1 GB of history a year — and does it
in a repo four other people are actively pushing to. So: **cron on the fork, code to the
org repo by PR.**

**`main` had diverged.** Teammates pushed 10 commits on 26 Aug that the old handoff knew
nothing about — the glider curtain plot, two map apps, `glider_lib.py`, La Perouse buoy
data, Folger Passage CSVs. Local `main` was rebased onto them. Exactly one file
conflicted, `.gitignore`, resolved as a union of both sides.

---

## Where things stand

| | |
|---|---|
| `fork/main` | 4 commits on top of `origin/main`; carries the workflow |
| `fork/glider-pipeline` | PR branch — same work **minus** the workflow file |
| PR | [oceanhackweek#3](https://github.com/oceanhackweek/ohw26_proj_BarkleyScope/pull/3) — open, awaiting review |
| Daily job | **Live**, 00:00 UTC on the fork. Proven green by hand. |
| Backup tag | `backup/pre-rebase-main` → `5cb6acf`, the pre-rebase state |

The commits are no longer pod-only — they are on the fork. The 35 MB delayed archive is
still local and gitignored, still reproducible in a couple of minutes.

---

## The one bug this session found

The first dispatched run **failed**, and the failure was worth having.

`verify_archives.py` required *both* archives. The delayed one is gitignored — it rebuilds
from ERDDAP in minutes — so it can never exist in a fresh CI checkout. The harvest
succeeded and every real-time check passed, but the verifier called the missing file a
failure and the job exited 1.

The guard did its job: the commit step was skipped rather than pushing an archive CI
couldn't vouch for, and the artifact upload still ran, so nothing was lost.

Fix: `verify_archives.py` now takes `--mode {realtime,delayed,both}`, defaulting to `both`
so local behaviour is unchanged; the workflow passes `--mode realtime`. A missing archive
that *was* asked for still fails — verified by moving the delayed archive aside and
confirming `--mode both` fails while `--mode realtime` passes.

**The lesson generalises:** anything the scheduled job runs has to work against a fresh
checkout containing only tracked files. Local success proves less than it looks like.

---

## What's next

1. **Get PR #3 reviewed and merged.** It's the only outstanding item.
2. **Reconcile the two glider libraries.** A teammate wrote `final_notebooks/glider_lib.py`
   — loaders and plot helpers factored out of `Glider_Curtain_Plot.ipynb` — which overlaps
   in intent with `data/cproof_glider.py`. Nothing conflicts today, and the PR touches
   neither, but the repo is on track to carry two glider libraries that drift apart.
   Its own docstring already admits the notebook duplicates it and they are "kept in sync
   by hand for now."
3. **Watch the first unattended run** (00:00 UTC tonight, on the fork).

### Expect a no-op tonight

The harvest currently finds nothing new: 203,895 observations, latest 2026-08-23
21:34:10, unchanged locally and in CI. The proven run reported *"No new observations;
nothing to commit."* That is the correct outcome for a box with no glider currently
reporting — a green run with no new commit is success, not a silent failure. Both paths
are now exercised.

---

## Things worth knowing (carried forward, all still true)

**The DAC catalogue is not stable.** Identical searches returned 10, 25, 27, and 38
datasets within minutes as the server reloads datasets. This is why update state is
derived from the archive itself rather than a sidecar file, and why updates are additive:
a deployment missed by one run is picked up by the next. Corollary — **avoid `--rebuild`**
unless you have a reason; a thin catalogue at that moment yields a thin archive.

**Delayed-mode is hundreds of times denser**, not just better calibrated — 185×, 348×, and
1788× on the three deployments present in both. The real-time feed is decimated for
satellite bandwidth. Hence two files, not one blended one.

**Delayed dataset IDs are the real-time ID plus a `-delayed` suffix.** Joining the two
archives on `deployment` without stripping it silently yields an empty intersection.

**A bug to not reintroduce:** pandas parses these ERDDAP timestamps at *microsecond*
resolution, so the usual `.astype("int64") / 1e9` puts every observation in January 1970.
This silently broke the high-water marks and made the updater re-append all 3M rows.
`_epoch_seconds()` handles it resolution-independently, with a comment explaining why.

**The DAC throws transient 502s.** `erddap_csv()` retries 5xx and dropped connections four
times with linear backoff, but never retries a 404 — that is ERDDAP saying "no matching
data".

**GitHub disables scheduled workflows after 60 days of repo inactivity** — a real concern
for a hackweek repo that goes quiet. `workflow_dispatch` is the manual restart.

**Close the notebook in JupyterLab before editing it again**, or a stale tab overwrites it.

---

## Quick reference

```bash
# daily update (what the workflow runs)
python data/update_cproof_glider.py --mode realtime

# refresh the historical reference record (additive, a couple of minutes)
python data/update_cproof_glider.py --mode delayed

# confirm nothing is broken
python data/verify_archives.py                  # both archives
python data/verify_archives.py --mode realtime  # only the tracked one -- what CI runs

# read it back
python -c "
import sys; sys.path.insert(0, 'data')
import cproof_glider as c
print(c.read_archive(c.REALTIME_ARCHIVE, last_days=7).head())
"

# watch the scheduled job
gh run list  --repo tborgfeldt/ohw26_proj_BarkleyScope
gh run watch --repo tborgfeldt/ohw26_proj_BarkleyScope <run-id>
```
