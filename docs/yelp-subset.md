# Import a small Yelp geographic subset

The repository includes a small **fictional demo**, under `data/demo/yelp_subset/`,
so collaborators can run imports immediately after cloning. The importer defaults
to that directory and labels its database registry entry `Synthetic activity demo`.
It contains 15 fictional businesses and 2,160 invented check-ins, not Yelp records.

Real Yelp records remain local under ignored `data/local/yelp_subset/`. The compact
bundle contains `yelp_academic_dataset_business.json.gz`,
`yelp_academic_dataset_checkin.json.gz`, `weather_cells.csv`, a source manifest and
the supplied dataset agreement. It keeps only necessary business fields, check-ins
and selected cells. Compressed JSON lines are read directly without extraction.
Reviews, users and tips are not included.

The default selection is **Restaurants** in Philadelphia (PA), Tampa (FL), and
Nashville (TN), matched by city and state, ignoring case and surrounding spaces.
These are city labels in Yelp, not metropolitan boundaries. Closed businesses and
businesses without check-ins remain in the selection to avoid an artificial filter.

## Schema

`001_datasets.sql` remains unchanged. `002_yelp_subset.sql` adds:

| Object | Purpose |
| --- | --- |
| `import_runs` | Selected regions/category, source SHA-256 hashes, counts and import identity |
| `businesses` | Business metadata and category array, scoped to an import run |
| `weather_cells` | Only the weather cells needed by selected businesses |
| `business_weather_mapping` | Nearest 0.25-degree ERA5 grid cell per selected business |
| `checkin_events` | Naive source timestamps with event multiplicity |
| `business_activity_daily` (view) | Recorded activity aggregated by naive source calendar day |
| `weather_hourly` | Reserved for actual hourly weather, with source dataset provenance |

The same sources and scope produce the same import key; rerunning reuses the
completed import. A changed source or scope creates a separate snapshot. Queries
must select an `import_run_id` to avoid combining overlapping snapshots. All import
writes commit together or roll back together. Concurrent imports are serialized.
Migrations must run explicitly before import.

## Commands

With `DATABASE_URL` configured in ignored `.env`:

```sh
uv run --frozen python -m sitesense.yelp_import audit
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python -m sitesense.yelp_import import
```

Those commands use the committed **synthetic demo**. Import a licensed local Yelp
bundle explicitly:

```sh
uv run --frozen --env-file .env python -m sitesense.yelp_import import --archive data/local/yelp_subset --output data/processed/yelp_subset_import.json
```

Choose a smaller scope with `--region Tampa:FL --region Nashville:TN --category Restaurants`.
Repeating `--region` replaces the default cities. A filtered bundle cannot supply
regions/categories removed during export; obtain the original source to expand scope.
Use `--all-categories` explicitly to include every category in the selected cities.
An empty or unmatched region fails before any database writes.

## Prepare real data locally

Obtain your own archive through the [Yelp dataset download page](https://www.yelp.com/dataset/download)
and review its terms. Stage business/check-in JSON lines and the weather cell table
in an ignored local source directory, such as `archive/`. The supplied 2021 dataset
agreement restricts distribution in Section 4, including public display of data and
transfer of any part of the dataset. Filtering or compressing does not establish a
new permission to share it. Keep real data out of Git unless you have the necessary
permission. Commit the fictional demo, importer and export code instead.

```sh
uv run --frozen python -m sitesense.dataset_bundle export --archive archive --destination data/local/yelp_subset
```

Export audits the original selection, writes deterministic gzip files with only
needed fields, and independently checks that counts, coverage and date ranges match.
It retains repeated check-in events and businesses without check-ins. The manifest
records both original hashes and compact file hashes. Export refuses to overwrite
an existing directory and never deletes its inputs automatically.

After a verified compact export, the full source JSON files can be removed to reclaim
disk space. Windows may prevent removal while an editor or indexer holds them open.
To rebuild a different scope after removal, acquire the original data again.
Notebook workflows that require full Yelp data still need their own source files.

On Windows, close the archive JSON tabs/viewers, then remove just the old archive
with the helper. It verifies the retained compact checksums and refuses to delete
business/check-in source files that differ from the verified export:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/remove_full_yelp_archive.ps1
```

Commit `src/`, scripts, docs and `data/demo/yelp_subset/`. The `.gitignore` excludes
`archive/`, `data/local/`, `data/interim/`, generated reports, `.env` and `.idea/`.
No PostgreSQL data directory or real Yelp bundle is included in the shared repository.

The original database import remains intact. Compressed files have different source
hashes from uncompressed originals, so importing the bundle into that same database
creates another snapshot. Select one `import_run_id` in analyses to avoid double
counting. On a fresh database, the compact bundle recreates the same selected data.

## Isolated Windows development database

If Docker is unavailable and PostgreSQL binaries are already installed, the helper
can initialize a separate local development cluster. It creates ignored `.env`
with a generated password, binds to `127.0.0.1:55432`, and creates database
`sitesense`. It never replaces an existing `.env` during initialization.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_local_database.ps1 -PostgresBin D:\PostgreSQL\bin
```

The cluster persists under `data/interim/postgres/`. Restart it with the same helper
and port. Stop it while retaining data:

```powershell
& D:\PostgreSQL\bin\pg_ctl.exe stop -D data/interim/postgres -m fast -w
```

This is a development database, not a Windows service; after a reboot it needs to
be started again. Keep a protected backup of important data before clearing local
research files. Docker remains the standard project setup.

## Inspect imported data

```sql
SELECT id, scope, summary FROM sitesense.import_runs ORDER BY id DESC;

SELECT city, state, count(*) AS businesses
FROM sitesense.businesses
WHERE import_run_id = 1
GROUP BY city, state ORDER BY city;

SELECT source_date, sum(checkin_count) AS recorded_activity
FROM sitesense.business_activity_daily
WHERE import_run_id = 1
GROUP BY source_date ORDER BY source_date;
```

Replace `1` with the import ID reported by the command. The initial local archive
audit found 11,327 Restaurants, 11,036 with check-ins, and 2,329,792 recorded events
across 16 grid cells. Source timestamps span 2010-01-16 through 2022-01-19.

## Interpretation limits

`weather_cells.csv` contains grid metadata, not weather observations. Its date
bounds do not establish weather coverage. This import leaves `weather_hourly`
empty; hourly ERA5 needs a separate acquisition/import step.

Yelp timestamp timezone semantics remain unresolved. The daily view aggregates
source dates without asserting UTC or local time. Do not join that view to UTC
weather until timestamp alignment is established. Missing daily rows are not
verified zero demand or store closure. Check-ins represent recorded activity,
not unique customers or revenue; weather associations do not establish causality.

The Streamlit research page remains unconnected to these tables and trained models.
