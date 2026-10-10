# Historical ERA5 weather for Yelp locations

The app now uses [Climate Explorer/ACIS station observations](climate-explorer-data.md). On 2026-10-10 the local ERA5 dataset cache under `data/processed/yelp_weather/` and its daily/monthly UTC summaries under `data/processed/yelp_weather_exploration/` were removed. Unused ERA5 database grid points and their registry entry were also removed. The acquisition workflow and historical research results are retained; rerun notebook 02 to reacquire ERA5 before reproducing an older comparison. The details below describe that historical dataset and its reproducible acquisition.

The acquisition scope includes all **150,346 businesses**, including the 18,416 without check-ins. Their coordinates map to **111 ERA5 grid cells**. Each cell receives the same hourly interval: **2009-12-29 through 2022-01-20**, inclusive. This adds one day before and after the archive's observed check-in dates to support later timezone alignment.

These are historical reanalysis values from the [Open-Meteo public ERA5 archive](https://github.com/open-meteo/open-data). The nearest 0.25° grid center represents each business; halfway ties round toward positive coordinates. Several businesses can share a cell. Values receive no elevation downscaling and do not describe street-level weather.

The complete local acquisition was verified on 2026-10-05: **11,737,584 hourly rows**, 105,744 hours per cell, and no missing values in the four core variables. The weather partitions occupy 143,798,783 compressed bytes.

## Acquisition and analysis

Stage `business.jsonl` and `checkin.jsonl` with the [Yelp exploration notebook](../notebooks/01_yelp_dataset_exploration.ipynb), then start Jupyter from the repository root:

```sh
uv sync --frozen --group notebooks
uv run --frozen --group notebooks jupyter lab --notebook-dir=.
```

Open [the weather notebook](../notebooks/02_yelp_weather_dataset_exploration.ipynb) and run its cells in order. Its **Download data** section prepares the business-to-cell mapping and acquires weather before analysis. All acquisition code lives in the notebook. The optional `notebooks` dependency group includes the locked `omfiles` decoder and is not selected by the app container. Acquisition needs internet access and no API key; it reads selected compressed byte ranges from public archive files.

The notebook writes one partition per year and reuses files whose checksums match the manifest. Missing or corrupt cached partitions are fetched again. Changed source inputs or acquisition settings require a new `WEATHER_DIR`. Keep the mapping and manifests alongside the hourly files. Once acquired, the notebook can run offline.

## Local files

All generated files are ignored by Git and live under `data/processed/yelp_weather/`:

| File | Contents |
| --- | --- |
| `business_weather_mapping.csv` | Every business ID, city/state, original coordinates, weather cell and coordinates, check-in count, and first/last naive check-in timestamps |
| `weather_cells.csv` | Unique weather cell coordinates, business counts, and shared inclusive acquisition dates |
| `scope.json` | Source paths, sizes and SHA-256 hashes, counts, date bounds, grid selection, and timezone caveat |
| `hourly/era5_YEAR.csv.gz` | Compressed CSV containing hourly weather for each scoped cell in that year |
| `acquisition.json` | Download status, partition checksums, variable units, source object URLs/ETags, and coverage audit |
| `ATTRIBUTION.txt` | Source attribution and license information to retain when sharing the weather subset |

The dataset contains these five metadata/mapping files and 14 hourly partitions. Source provenance lives in `scope.json`; download checksums, source object versions, and the coverage audit live in `acquisition.json`.

An acquisition is complete only when `acquisition.json` has `status: "complete"`. The analysis then checks every partition's cell-hour sequence and missingness. The original local acquisition also passed an independent scan of all 14 partitions, confirming **11,737,584 cell-hours** with no duplicate or missing cell-hours.

## Hourly schema

Each row is one weather cell at one UTC hour:

| Column | Meaning / unit |
| --- | --- |
| `weather_cell_id` | Join key to the business mapping and cell table |
| `timestamp_utc` | ISO 8601 timestamp with `+00:00` offset |
| `temperature_2m` | Air temperature at 2 m, °C |
| `relative_humidity_2m` | Relative humidity at 2 m, %; derived from temperature and dew point |
| `precipitation` | Total precipitation over the preceding hour, mm |
| `wind_speed_10m` | Wind speed at 10 m, m/s; derived from the two wind components |

Values are rounded to four decimal places. Missing values are empty CSV fields and are counted in the audit. Partition metadata records value ranges and expected row counts. Source ETags identify downloaded object versions; local SHA-256 hashes verify the generated files. ETags are not treated as content SHA-256 hashes.

## Join and interpret carefully

Yelp check-in timestamps are retained exactly as naive source values. Their timezone meaning remains unresolved, and no check-in/weather temporal join has been performed. Resolve this meaning before assigning UTC hours or local calendar days; do not assume the timestamps are UTC or a business's local time.

For analysis, select the businesses and activity hours or dates you need, attach their `weather_cell_id` through the mapping, then join the corresponding weather hours or correctly aggregated dates. Joining every hourly row to every business in its cell can create billions of business-hour rows. Keep weather stored once per cell and avoid that expansion unless the analysis requires it.

Check-ins are an activity proxy. Weather associations and scenario estimates do not establish causal effects.

## Attribution

The [Open-Meteo open-data distribution](https://github.com/open-meteo/open-data#license) is licensed under CC BY 4.0. Retain `ATTRIBUTION.txt` and the manifests when sharing the weather subset, for example: “Copernicus ERA5 data redistributed by Open-Meteo; nearest-grid-cell extraction and derived humidity/wind speed by SiteSense.” Yelp source data retains its own terms.
