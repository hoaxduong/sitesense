# Climate Explorer acquisition

Download the county climate data used by the [U.S. Climate Resilience Toolkit Climate Explorer](https://crt-climate-explorer.nemac.org/). The crawler uses the same public [ACIS GridData service](https://grid2.rcc-acis.org/GridData) as Climate Graphs. The provider also links an [official bulk-download notebook](https://crt-climate-explorer.nemac.org/vendor/ACIS_data_download.ipynb) from its [FAQ](https://crt-climate-explorer.nemac.org/faq/).

The full Yelp scope prepared on 2026-10-10 maps **144,773 U.S. businesses to 58 counties and 52 recently active eligible stations**, using original business coordinates. The **5,573 Alberta businesses** are retained as `outside_us_source`, since this source covers the United States. The Census spatial join also identifies 75 source-state mismatches instead of accepting those labels.

The two full-scope exports are:

| File under `data/raw/climate_explorer/` | Contents |
| --- | --- |
| `yelp_counties/climate.csv.gz` | Annual/monthly Livneh observations, modeled history and RCP4.5/RCP8.5 projections for all 58 counties |
| `yelp_stations/observations.csv.gz` | Daily observed maximum/minimum temperature and precipitation, requested 2009-01-01–2026-10-09, from 52 stations |
| `yelp_scope/business_climate_mapping.csv` | Every business ID, county match, nearest eligible station, distance, mapping status and source-state mismatch |
| `yelp_scope/scope_manifest.json` | Source hashes, business reconciliation, selected counties/stations and distance audit |
| `sources/` | Preserved county geometry, provider catalogues and station metadata for offline reproduction |

The request date range includes 2026. Actual observed dates depend on station and variable availability; inspect `coverage.<variable>.latest_observed_date` and `coverage.<variable>.latest_year.latest_observed_date` for each station in the station manifest. A successfully completed acquisition can contain missing observations.

The app uses the daily station export as its default weather source. To seed the completed snapshot for existing imported businesses, apply migrations and run `uv run --frozen --env-file .env python -m sitesense.import_climate`. The default `sitesense.import_data` command imports Yelp and station weather together. See [Database setup](database.md) for storage, quality handling and refresh behavior. County projections remain separate research data.

From the repository root, reproduce the full-scope downloads:

```sh
uv run --frozen python -m sitesense.climate_explorer \
  --counties-file data/raw/climate_explorer/yelp_scope/counties.json \
  --batch-by-state --gzip --output data/raw/climate_explorer/yelp_counties
uv run --frozen python -m sitesense.climate_stations \
  --stations-file data/raw/climate_explorer/yelp_scope/stations.json \
  --start 2009-01-01 --end 2026-10-09 \
  --output data/raw/climate_explorer/yelp_stations
```

Reproduce the coordinate mapping directly from the staged Yelp `business.jsonl` and retained source snapshots using the existing optional notebooks dependencies. This preparation does not require ERA5 files:

```sh
uv run --frozen --group notebooks python scripts/prepare_climate_scope.py \
  --county-geojson data/raw/climate_explorer/sources/census_counties_2026.geojson.gz \
  --station-metadata data/raw/climate_explorer/sources/acis_station_metadata.json.gz \
  --areas data/raw/climate_explorer/sources/climate_explorer_areas.json.gz
```

The county join uses unsimplified [Census TIGERweb January 2026 county polygons](https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1), with hole handling. Unmatched or multiply matched coordinates remain explicit. The station join chooses the closest station by great-circle distance from the [Climate Explorer whitelist](https://crt-climate-explorer.nemac.org/data/stations_whitelist.json), using ACIS coordinates and requiring all three variable metadata ranges to start on/before 2009-01-01 and extend to at least 2026-09-01. Date ranges are not a guarantee of daily completeness. Station distances are approximately 11.7 km median, 27.7 km at the 95th percentile and 60.4 km maximum; these are nearby observations rather than measurements at individual shops.

For a smaller three-city pilot:

```sh
uv run --frozen python -m sitesense.climate_explorer
```

This command defaults to the three counties corresponding to the forecasting pilot. IDs and labels are checked against the provider's [area catalog](https://crt-climate-explorer.nemac.org/data/ce_areas.json).

| SiteSense city | Provider county | State | FIPS |
| --- | --- | --- | --- |
| Philadelphia | Philadelphia County | PA | `42101` |
| Tampa | Hillsborough County | FL | `12057` |
| Nashville | Davidson County | TN | `47037` |

County averages describe the entire county. They are not measurements at each business or exact city boundaries. Yelp city names alone do not establish county membership.

## Data included

The crawl exports annual and monthly values for the three core Climate Graphs variables:

| Variable | Meaning | Native unit | Temporal reduction |
| --- | --- | --- | --- |
| `tmax` | Average of daily maximum temperature | `degreeF` | Mean |
| `tmin` | Average of daily minimum temperature | `degreeF` | Mean |
| `pcpn` | Total precipitation | `inch` | Sum |

| Dataset | Source | Period | Statistics |
| --- | --- | --- | --- |
| Observations | Livneh gridded observational dataset | 1950–2013 | Observed county average |
| Modeled history | LOCA / CMIP5 | 1950–2005 | Weighted mean, model minimum, model maximum |
| Projections | LOCA / CMIP5, RCP4.5 and RCP8.5 | 2006–2099 | Weighted mean, model minimum, model maximum per scenario |

The Livneh observations are interpolated station observations on a grid, as described in the provider's [About page](https://crt-climate-explorer.nemac.org/about/). They stop in 2013, so the separate station export provides historical observed data through the available portion of 2026. These exports do not include threshold counts, humidity, wind or high-tide flooding.

## Station observations through 2026

`sitesense.climate_stations` downloads [ACIS StnData](https://data.rcc-acis.org/StnData) from the service used by [Historical Weather Data](https://crt-climate-explorer.nemac.org/historical_weather_data/). Station IDs are GHCN-Daily identifiers, but ACIS can merge other networks; each value retains its network ID and source flag. It also retains the original value, flag, local standard observation time and trace indicator. See the [official ACIS documentation](https://www.rcc-acis.org/ACISWSdoc.html).

Daily `tmax` and `tmin` are individual daily maxima/minima, unlike the county table's annual/monthly averages. Temperatures remain Fahrenheit and precipitation remains inches. `M` and numeric missing sentinels become blank normalized values; the raw values remain available. Precipitation marked `T` is a trace, and `A` can represent accumulation over multiple days. Preserve and handle these flags before deriving daily features. Station dates/observation windows are local rather than UTC; joining them to Yelp activity requires an explicit time-alignment policy.

The station manifest audits completeness and actual latest observed date separately for each variable and station, including the 2026 subset. It is not an assertion of complete observations through the requested final date, or of a completed calendar year 2026. Recent values can arrive late or be revised; rerun with `--refresh` and a later `--end` to retrieve them.

Periods follow the available annual Climate Graphs series, including its final year, 2099. The API also supplies monthly projections from 2006; the website's monthly view instead uses later multi-year climatological windows. Monthly exports here are raw year/month values, not those plotted climatologies. Historical model output is requested through the `rcp85` grid's pre-2006 segment and labeled `historical`, rather than as a future emissions scenario.

## Files and reruns

Outputs are local under ignored `data/raw/climate_explorer/`. Raw JSON responses retain the exact request and retrieval time; the CSV contains explicit county, dataset, scenario, statistic, frequency, period, variable and units. The acquisition manifest records scope, row counts, missing values and checksums. Recognized missing values remain missing, including when precipitation is unknown; zero precipitation remains a valid numeric zero.

County requests can use a single county or `--batch-by-state`; the latter downloads state-level response dictionaries, validates every requested county and exports only the selected counties. The three variables are batched together. Station requests use one selected station and the full requested date span. Downloads have bounded retries and timeouts. Reruns reuse matching, validated raw partitions; use `--refresh` to retrieve them again. A manifest is marked complete only after every expected period and variable has been checked and exported.

Choose another set of contiguous-U.S. counties or an output directory:

```sh
uv run --frozen python -m sitesense.climate_explorer \
  --counties 42101 12057 47037 \
  --frequency annual monthly \
  --output data/raw/climate_explorer
```

Alaska, Hawai‘i and island territories use different sources and model conventions, so this crawler currently supports contiguous-U.S. counties only.

## Interpretation and attribution

Climate projections describe conditional long-term climate scenarios, not the actual weather on a historical check-in day or a forecast of a particular future storm. Model minima and maxima are the ensemble envelope, not probabilistic prediction intervals. Prefer decadal summaries for long-term planning, as the provider recommends in its [FAQ](https://crt-climate-explorer.nemac.org/faq/). Keep county projections separate from the daily station observations used by the app. Historical ERA5 comparisons can reacquire their inputs with notebook 02.

Credit the U.S. Climate Resilience Toolkit Climate Explorer, ACIS, Livneh and LOCA/CMIP5 when using the exports. The provider's recommended citation is: U.S. Federal Government, 2025: U.S. Climate Resilience Toolkit Climate Explorer; include the website URL and access date. See the [FAQ](https://crt-climate-explorer.nemac.org/faq/) and [About page](https://crt-climate-explorer.nemac.org/about/) for source references.
