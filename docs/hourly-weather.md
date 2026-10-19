# Observed hourly weather

The activity/weather charts use **NOAA hourly temperature** when available.
Otherwise temperature boxes use mapped daily ACIS lows/highs. **Precipitation
boxes always use accepted daily ACIS totals (mm/day)**, including dry days.
Stored hourly rainfall is source data and is not summed into chart daily totals.

Location map uses one weather distribution for its whole filtered business scope.
Compare businesses shares one weather distribution across its selected activity
lines. Daily weather samples are pooled from distinct mapped stations: each
station/date contributes once, regardless of how many businesses share it. The
single-business investigation uses that business's mapped daily station. All
charts keep hourly measurements and daily fallback summaries separate.

These are observed station reports, not measurements at each business or weather
forecasts. Daily correlations and the retained area-level weather models use ACIS
sources. Business HGB scenarios also use accepted daily ACIS training inputs; hourly
chart samples do not replace those model features.

## Prepare the optional hourly import

Run acquisition and import explicitly outside Streamlit:

```sh
uv run --frozen --env-file .env python -m sitesense.database migrate
uv run --frozen --env-file .env python scripts/prepare_hourly_weather.py
```

The script defaults to **2009-01-01 through 2026-10-09** for Philadelphia,
Nashville and Tampa. Use `--city Philadelphia`, `--start-date YYYY-MM-DD` and
`--end-date YYYY-MM-DD` for a smaller scope. `--download-only` validates local
files without connecting to PostgreSQL. Verified completed files are reused;
`--refresh` explicitly replaces raw downloads.

| City | Airport station | Local standard offset |
| --- | --- | --- |
| Philadelphia, PA | USW00013739 | UTC−05:00 |
| Nashville, TN | USW00013897 | UTC−06:00 |
| Tampa, FL | USW00012842 | UTC−05:00 |

The source is [NOAA NCEI Local Climatological Data v2](https://www.ncei.noaa.gov/oa/local-climatological-data/v2/).
Its [documentation](https://www.ncei.noaa.gov/oa/local-climatological-data/v2/doc/lcdv2_DOCUMENTATION.pdf)
defines the units, missing values and quality flags.

## Samples and timestamps

The parser accepts routine `FM-15` reports and retains the latest report in each
station/local-standard hour, using a deterministic tie break for conflicting
reports at the same timestamp. Special `FM-16` reports are excluded to avoid
overlapping precipitation intervals.

`HourlyDryBulbTemperature` and `HourlyPrecipitation` are decimal Celsius and
millimeters, without a factor-of-ten conversion. Blank, nonnumeric, suspect (`s`)
and erroneous (`*`) readings remain missing. Zero precipitation is valid; trace
(`T`) is stored as zero with `is_trace=true` and the original text. Missing hours
are never fabricated.

`DATE` uses station local standard time throughout the year, without daylight
saving. The database stores the original timestamp and fixed UTC offset; chart
rows retain that offset. For example, summer Philadelphia `13:54` local standard
time is `18:54Z`, or `14:54` on the city's daylight-saving clock. Activity dates
retain assumed source-local labels. Their alignment with weather dates has not
been verified; civil-time analysis would require conversion through UTC.

## Provenance and refreshes

Raw station-year CSVs are retained as gzip files under
`data/raw/hourly_weather/raw/`, with per-file download manifests.
`data/raw/hourly_weather/manifest.json` records source SHA-256 hashes, dates,
stations, units, missing counts and the content-addressed normalized file. The
manifest is published only after file verification; corrupt caches fail validation.

Imports replace only the selected station/date ranges in one transaction. A unique
index allows one observation per station/local-standard hour. Dataset key
`basic_app_hourly_weather_v1` has its own revision, separate from daily ACIS data,
business mappings and activity/model inputs. Distinct completed manifests and
their paths remain in `import_history`; exact reimports are deduplicated and
partial refreshes preserve lineage for observations outside the refreshed range.

An unavailable hourly import does not block the app's daily weather fallback.
Missing or rejected daily reports stay excluded; a mostly dry period can still
have a zero-height rainfall box.
