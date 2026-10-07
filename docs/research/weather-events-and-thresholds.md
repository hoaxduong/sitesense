# T020 — Weather events and thresholds for SiteSense

Date: 2026-10-07. Purpose: UI scenarios and weather groups for research. These are not official weather alerts.

## 1. Understand the data

Each row represents one **city and local calendar day**. Use `RR = precipitation_sum` (mm/day), `TX = temp_max`, `TN = temp_min` (°C), and `WX = wind_max` (m/s).

RR includes rain **and the water equivalent of snow**, according to [ECMWF](https://codes.ecmwf.int/grib/param-db/228). Label it “rain/snow” in the UI. Temperature alone does not tell us whether snow fell. RR below 1 mm does not mean “sunny”: the dataset has no cloud or solar radiation variables.

In the [preparation code](../../src/sitesense/forecast_data.py), TX/TN/WX are weighted averages of the daily extremes in individual ERA5 cells. They are not the highest or lowest values anywhere in the city. WX is not a wind gust measurement. `rain_hours` is the weighted average number of hours with precipitation across cells and can be fractional. It does not establish “X hours of continuous rain.”

## 2. Selected thresholds

P95 is a dividing line with roughly **5 out of 100 days** above it. Calculate it separately for each city and season.

| Event flag | Condition, using unrounded values | UI label |
| --- | --- | --- |
| `dry` | RR < 1 | Little rain/snow |
| `wet` | RR ≥ 1 | Rain/snow present |
| `high_precipitation` | RR ≥ 10 | High rain/snow total, ≥ 10 mm/day |
| `very_high_precipitation` | RR ≥ 20 | Very high rain/snow total, ≥ 20 mm/day |
| `hot_day` | TX ≥ 32°C | Hot day, ≥ 32°C |
| `unusually_warm` | TX ≥ P95(TX, city × season) | Warmer than usual |
| `unusually_cold` | TN ≤ P05(TN, city × season) | Lower daily minimum temperature than usual |
| `windy` | WX ≥ P90(WX, city × season) | Windier than usual |
| `very_windy`, optional | WX ≥ P95(WX, city × season) | Wind in the highest 5% |

The **1/10/20 mm/day** levels follow R1mm/R10mm/R20mm in the [Copernicus indices dictionary](https://surfobs.climate.copernicus.eu/userguidance/indicesdictionary.php). These are daily totals, not mm/hour.

**32°C, P95/P05 temperature thresholds, and the P90 wind threshold are project choices.** Climate indices also use percentiles, but [official Climdex definitions](https://climate-scenarios.canada.ca/?page=climdex-indices) use different reference periods and calculations. SiteSense does not reproduce TX90p/TN10p or an official heatwave definition. In Tampa during summer, “colder than usual” can still mean above 22°C.

Precipitation bins do not overlap: **[0, 1), [1, 10), [10, 20), [20, ∞)**. Event flags may overlap: a day can be both hot and wet, and RR ≥ 20 activates all three precipitation flags.

## 3. Freeze thresholds using training data

Use the [executed experiment's panel](../../data/processed/forecast_comparison/140825e2693093ce_20261007T032641646584Z/city_daily_panel.csv), restricted to **2013-01-01 through 2016-12-30**: 4,380 rows and 1,460 days per city. Call this the **four-year training reference**. Seasons are DJF = December/January/February; MAM = March/April/May; JJA = June/July/August; SON = September/October/November.

Use linear interpolation for percentiles and round only for display. Freeze the thresholds and data hashes. Do not adjust them based on check-ins, validation/test scores, or attractive predictions. Models continue to receive continuous weather values.

T020 was defined after the 2019 test scores had already been inspected. Although the thresholds use only training weather, additional event analysis on that test remains exploratory rather than a preregistered experiment. Confirming new conclusions requires an independent holdout that has not been used during development.

For example, the JJA summer reference has 368 days per city:

| City | TX P95, °C | TN P05, °C | WX P90, m/s |
| --- | ---: | ---: | ---: |
| Philadelphia | 33.93 | 13.79 | 5.38 |
| Nashville | 35.24 | 16.35 | 4.38 |
| Tampa | 33.77 | 22.68 | 5.21 |

Values equal to the threshold count as events. The share of qualifying days may differ from exactly 5%/10% because of interpolation or tied values.

## 4. Create UI scenarios

Let users select a city, season, and scenario: **typical; little rain/snow; high rain/snow total; very high rain/snow total; hot; warmer/colder than usual; windier than usual**.

Filter the matching training days. Select the day closest to the group's joint median, scaling each variable by the city-season IQR (the width of the middle half of its training values). Use that day's **complete weather vector**, including humidity. Do not combine percentiles from different days or select a day based on check-ins.

Distance is the sum of the absolute differences across eight variables after dividing each difference by its IQR. Choose the day with the smallest distance. The eight columns are `temp_mean`, `temp_min`, `temp_max`, `precipitation_sum`, `rain_hours`, `humidity_mean`, `wind_mean`, and `wind_max`.

Show the threshold, weather values, example date, and number of matching training days. Disable a scenario when no examples exist. Show sample counts when support is limited; “30 days” is not a universal reliability standard. Do not classify missing data as a dry day.

Keep the target date, weekday, and check-in history fixed when comparing scenarios. **The current winning model uses `no_weather`, so changing weather does not change its prediction.** Scenarios require a named model that uses weather, together with its own evaluation scores; do not present it as a new winner. Label outputs “model estimates,” not “rain reduces customers by X%.” Check-ins are not customer counts.

## 5. Count days and event runs

An event run consists of consecutive days in the same city with an active event flag, including across season boundaries. An inactive flag or missing date ends the run. Five consecutive event days are not five independent examples. A scenario preset only needs one day. Activate a “has lasted three days” label from the third day onward, without looking into the future.

ERA5 describes weather that has already occurred, according to [Copernicus](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels?tab=overview). These scenarios are retrospective simulations, not live weather forecasts or evidence of causal effects.

Evidence: [thresholds](../../data/processed/weather_scenarios/T020/relative_thresholds.csv), [seasonal support counts](../../data/processed/weather_scenarios/T020/train_event_support.csv), [consecutive event runs](../../data/processed/weather_scenarios/T020/event_runs.csv), [examples](../../data/processed/weather_scenarios/T020/scenario_examples.csv), and [manifest](../../data/processed/weather_scenarios/T020/threshold_manifest.json). Runs in the seasonal support table are split at season boundaries; use `event_runs.csv` to count runs across seasons. This specification has not yet been integrated into the UI or forecasting notebook.
