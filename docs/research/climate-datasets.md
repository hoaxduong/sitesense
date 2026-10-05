# Climate and historical weather datasets for SiteSense

Research checked on 2026-10-05. Recommendation: use **Open-Meteo Historical Weather with an explicit ERA5 model** for the first weather-feature experiment, then validate temperature and precipitation against **NOAA GHCN-Daily** observations. Use hourly data to define the same calendar day as the check-in target; finalize timestamp semantics before joining.

## Project scope and evidence

- The [Yelp exploration notebook](../../notebooks/01_yelp_dataset_exploration.ipynb) defaults to Philadelphia, Pennsylvania, Restaurants. The current local experiment artifact contains **one selected business**, with 18,615 recorded check-ins across a 4,376-day calendar from 2010-01-24 through 2022-01-16. This is not the full Philadelphia or Yelp date range. Derive acquisition dates from all records included in the final experiment.
- The current selected-business target is daily recorded check-ins; the pilot trains through 2019-08-24 and tests from 2019-08-25. The notebook timestamps are naive, so their original timezone remains unresolved. The local evidence lives in ignored `data/processed/yelp_exploration/experiment_manifest.json` and `selected_business_daily_checkins.csv`; reproduce the notebook to refresh it.
- [Architecture](../architecture.md) and [database boundaries](../database.md) confirm no weather ingestion or trained scoring service is connected. Check-ins are an activity proxy; neither these datasets nor weather scenarios establish causation.

## Recommended sources

| Source | Resolution, coverage and useful variables | Acquisition and project role |
| --- | --- | --- |
| [Open-Meteo Historical Weather](https://open-meteo.com/en/docs/historical-weather-api), fixed **ERA5** | Global hourly reanalysis from 1940; approximately 0.25° grid. Temperature, precipitation, relative humidity and wind are available. | Hosted JSON API; fastest route to consistent features for the 2010–2022 pilot. Select `models=era5` explicitly and retain the returned grid coordinates. |
| [NOAA GHCN-Daily](https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily) | Daily station observations worldwide; station-specific periods. Core fields are maximum/minimum temperature, precipitation, snowfall and snow depth. | [Station files and downloads](https://www.ncei.noaa.gov/pub/data/ghcn/daily/), or [daily-summary search](https://www.ncei.noaa.gov/access/search/data-search/daily-summaries). Use nearby stations to benchmark ERA5 rain and temperature. |
| [Copernicus ERA5-Land time-series](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-timeseries) | Global hourly reanalysis from 1950; 0.1° grid. Includes temperature, dew point, wind components, precipitation and snow variables. | Direct CDS point time-series downloads in CSV/NetCDF; nearest grid cell. Useful when we need direct provider files and reproducible provenance beyond the hosted interface. |
| [NOAA GHCNh](https://www.ncei.noaa.gov/products/global-historical-climatology-network-hourly) | Global hourly/synoptic station observations; station-specific periods. Temperature, dew point/humidity, precipitation, wind, visibility and weather codes. | PSV/Parquet station/year or period-of-record downloads. Choose this observation source when hourly station benchmarks or weather codes are required. |

Reanalysis combines observations with a model to estimate conditions on a grid. It is not a measurement at each shop. Default Open-Meteo “Best Match” combines products, including IFS from 2017 onward; fixing ERA5 avoids that source change within this pilot. Open-Meteo's variable matrix lists precipitation and wind under ERA5 rather than its ERA5-Land-only selection. Use fixed ERA5 for the initial four-variable experiment; direct CDS ERA5-Land has a different variable catalog, as shown above. [Historical API model documentation](https://open-meteo.com/en/docs/historical-weather-api)

### Costs, licensing and reproducibility

- Open-Meteo's **free hosted API is for noncommercial use** and has request limits. Its data is CC BY 4.0 and requires attribution; hosting terms and data rights are distinct. Check the applicable plan if SiteSense becomes a commercial deployment. [Terms](https://open-meteo.com/en/terms)
- NOAA's GHCN-Daily metadata says electronic downloads are generally free; custom orders and certification may cost money. Cite the dataset DOI, subset and access date. Retain source-specific terms when obtaining non-U.S. data; do not infer that every redistributed observation has identical rights. [GHCNd metadata and constraints](https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/gov.noaa.ncdc:C00861/html), [CDO dataset documentation](https://www.ncei.noaa.gov/pub/data/cdo/documentation/GHCND_documentation.pdf)
- The direct CDS ERA5-Land time-series catalog lists CC BY licensing. Record the exact catalog license/version accepted at download. Its raw units and accumulation conventions require conversion before joining, particularly precipitation. [Dataset catalog](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-land-timeseries)
- GHCNh metadata lists CC0-1.0 and generally free electronic downloads. Actual reporting hours, quality flags and precipitation accumulation intervals still need auditing. [GHCNh metadata](https://www.ncei.noaa.gov/metadata/geoportal/rest/metadata/item/gov.noaa.ncdc:C01688/html)

## What is actually verified

Two small public Open-Meteo requests succeeded on 2026-10-05, one for 2010-01-24 and one for 2022-01-16. Requested Philadelphia city-center coordinates were 39.9526, -75.1652, with `models=era5`, `timezone=UTC`, and wind in m/s. Each returned **24 hourly rows**, with temperature, relative humidity, precipitation and wind speed present in all 24 rows. Returned grid coordinates were **40.0, -75.25**, and returned timezone was GMT.

Those initial probes confirmed availability for **two city-center days only**. The subsequent [all-location acquisition](../weather-data.md) now covers all 150,346 Yelp businesses through 111 ERA5 cells, from 2009-12-29 through 2022-01-20 in UTC. All 11,737,584 hourly rows passed an independent file audit, with no missing core values. Station completeness and the correct check-in timezone remain unverified; no activity-weather temporal join has been performed.

[Verified one-day public request](https://archive-api.open-meteo.com/v1/archive?latitude=39.9526&longitude=-75.1652&start_date=2010-01-24&end_date=2010-01-24&hourly=temperature_2m%2Crelative_humidity_2m%2Cprecipitation%2Cwind_speed_10m&models=era5&timezone=UTC&wind_speed_unit=ms)

### Philadelphia observation benchmark

**Philadelphia International Airport, GHCN station `USW00013739`, is a candidate**, confirmed by NOAA's [official station history](https://www.ncei.noaa.gov/pub/access/cebrequests/2023lcdannual/01202313PHL.pdf). Nearby-station suitability and actual variable completeness across the selected date range remain unverified. The airport may differ from conditions at an individual restaurant.

Start with `TMAX`, `TMIN` and `PRCP`, then add `SNOW`/`SNWD` if useful. Mean temperature, wind and humidity are not guaranteed at every GHCNd station. In raw `.dly` files, temperature is in tenths of °C, precipitation in tenths of mm, and snowfall/snow depth in mm. Preserve measurement, quality and source flags; `-9999` represents missing values. A trace is not the same as a verified dry day. [GHCNd README](https://www.ncei.noaa.gov/pub/data/ghcn/daily/readme.txt)

NOAA provides a [station inventory](https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt) and [element inventory](https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-inventory.txt). Inventory start/end years do not prove every day is complete. Daily summaries can use different observation-day boundaries, and GHCNd has not been homogenized for changes in observing practices. [Product limitations](https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily)

## Extensions only when needed

- **Existing ISD workflows:** NOAA says ISD stopped updates after **2025-08-24**, and announced retirement of the old NCEI FTP/HTTPS services for July 31, 2026. Legacy formats remain through NOAA Open Data Dissemination. Choose GHCNh for new work rather than assuming old ISD download URLs remain current. [Official service-change notice](https://www.nesdis.noaa.gov/news/service-location-change-integrated-surface-data-global-hourly)
- **Edmonton extension:** [Environment and Climate Change Canada Historical Climate Data](https://climate.weather.gc.ca/) supplies Canadian station hourly, daily and monthly weather. Use [station/proximity search](https://climate.weather.gc.ca/historical_data/search_historic_data_e.html) and station CSV exports; verify overlap and variables for the selected Edmonton businesses. The linked [historical-data license](https://www.climate.weather.gc.ca/prods_servs/attachment1_e.html) requires source acknowledgment and conditions on redistribution. [Custom extraction services](https://climate.weather.gc.ca/new_price_announce_e.html) have separate charges; do not treat those as required for ordinary portal downloads.
- **Meteostat convenience option:** [free dataset downloads](https://dev.meteostat.net/data) provide station CSV and bulk Parquet without signup. Redistributed data is [CC BY 4.0](https://dev.meteostat.net/license), with Meteostat/provider attribution. Its interfaces can mix observations and model-derived values by default; observation-only settings and provider provenance must be recorded. [Quality documentation](https://dev.meteostat.net/quality.html)
- **Future climate scenarios:** [Open-Meteo Climate API](https://open-meteo.com/en/docs/climate-api) offers daily CMIP6 simulations for 1950–2050. These are appropriate for future scenario exploration, not actual weather on historical Yelp check-in dates.

## Integration after acquisition

1. Resolve Yelp timestamp semantics; define local days explicitly. For Philadelphia, use `America/New_York` only after confirming it matches the target. Include boundary hours around the selected dates if converting UTC records to local days.
2. Use the downloaded fixed-ERA5 hourly temperature, precipitation, relative humidity and wind, then aggregate to correctly aligned local daily features: min/mean/max temperature, total precipitation, rain indicator, mean humidity and mean/max wind.
3. Audit missing values, duplicate timestamps, units, grid mappings and source versions. Reindex using the defined calendar; daylight-saving transitions can make local days 23 or 25 hours. Never replace unknown rain with zero.
4. Audit candidate NOAA stations by distance, elevation and valid per-variable daily coverage. Compare temperature and precipitation with ERA5; keep station observations and reanalysis provenance distinguishable.
5. Store raw responses outside Git and register source URLs, requested/returned coordinates, retrieval time, model, license, checksums, timezone and feature definitions. The existing dataset registry records provenance; importing weather records remains a separate implementation.
6. Compare calendar-only and weather-augmented models on the same forward holdout. Fit imputation and preprocessing only on training data. Evaluate forecast-time weather separately from experiments using realized historical weather; the latter alone does not prove operational prediction quality. [Project research requirements](../architecture.md)
