# Observed hourly weather

Prepare hourly weather explicitly before using the observed activity/weather charts:

```sh
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python scripts/prepare_hourly_weather.py
```

The default scope is 2009-01-01 through 2026-10-09 for Philadelphia, Nashville and Tampa.
Use `--city Philadelphia`, `--start-date YYYY-MM-DD` and `--end-date YYYY-MM-DD` to
prepare a smaller scope. `--download-only` prepares and validates the files without
connecting to PostgreSQL. Acquisition runs outside Streamlit with three concurrent
station-year downloads; completed files are reused after SHA-256 verification.

Original NOAA LCDv2 station-year CSVs are retained as gzip files in
`data/raw/hourly_weather/raw/`, with a download manifest for each file.
The completed `data/raw/hourly_weather/manifest.json` identifies the source hashes,
selected dates, station metadata, units, missing counts and normalized observation
file. The normalized filename contains its content hash, and the manifest is
published atomically after every file has been verified. Corrupt caches fail
validation; `--refresh` explicitly replaces raw downloads.

| City | Airport station | Local standard offset |
| --- | --- | --- |
| Philadelphia, PA | USW00013739 | UTC−05:00 |
| Nashville, TN | USW00013897 | UTC−06:00 |
| Tampa, FL | USW00012842 | UTC−05:00 |

The source is [NOAA NCEI Local Climatological Data v2](https://www.ncei.noaa.gov/oa/local-climatological-data/v2/).
Its [documentation](https://www.ncei.noaa.gov/oa/local-climatological-data/v2/doc/lcdv2_DOCUMENTATION.pdf)
defines Celsius temperature, millimeter precipitation, missing values and quality
flags. The parser uses only routine `FM-15` hourly reports. It retains the latest
report in each station/local-standard hour; conflicting reports at the same
timestamp use a deterministic raw-row tie break. Special `FM-16` reports are
excluded to avoid counting overlapping precipitation intervals.

`HourlyDryBulbTemperature` and `HourlyPrecipitation` are already decimal Celsius
and millimeters. They are not divided by ten. Blank, nonnumeric, suspect (`s`)
and erroneous (`*`) readings remain missing. Zero precipitation is retained.
Trace precipitation (`T`) is stored as zero with `is_trace=true`; its original
text is preserved. Missing hours are never fabricated.

`DATE` is station local standard time throughout the year. The database preserves
the report timestamp without a timezone and stores the fixed UTC offset alongside
it. For example, a summer Philadelphia report at `13:54` local standard time is
`18:54Z`, which is `14:54` on the city's daylight-saving clock. Convert through
UTC before aligning observations with civil-time activity periods.

The migration adds `sitesense.hourly_weather_observations`, with a unique index
that enforces one observation per station and local-standard hour. Imports replace
only selected station/date ranges in one transaction and preserve observations
outside that scope. Registry key `basic_app_hourly_weather_v1` has its own import
revision. Daily ACIS observations, business mappings, model inputs and their
revision remain unchanged.

Dataset metadata retains every distinct completed import manifest and its source
path in `import_history`. Reimporting the same exact snapshot is deduplicated.
Partial refreshes preserve earlier manifests and hashes so observations retained
outside the refreshed scope keep their source lineage.

These are observed airport station reports. They provide city-level weather
context, not measurements at each ZIP area or operational weather forecasts.

Temperature charts use these hourly measurements. Precipitation boxplots use
accepted ACIS daily totals (mm/day), including dry days, because hourly rainfall
is mostly zero. Missing or rejected daily reports remain excluded. The stored
hourly precipitation remains available as source data; it is not summed into
calendar-day totals in the chart.
