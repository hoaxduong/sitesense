# Business analyzer

Open **Business analysis** and choose **Metro area → Business category → Source
category → Date range**. This page uses the intersection of selected dates and
imported check-in coverage. A range outside that history has no recorded analysis.
For shared filters, date rules and projections on the other pages, see
[Business location workspace](business-location-workspace.md).

## Discover source categories

**Top source categories** ranks every additional source tag within the selected
parent category and city by **Recorded check-ins**. The selected sidebar source
category does not hide alternatives from this discovery ranking.

Read totals alongside listed businesses, businesses with check-ins and the
largest business's share. **Minimum businesses** defaults to one; raising it
excludes smaller groups without establishing statistical significance.
**Categories shown** limits the display to 10, 20 or 50 groups. The CSV retains
the complete eligible ranking and its geographic, date and minimum-business scope.

Tags overlap, so group counts and activity are not additive. All matching snapshot
listings, including zero-activity businesses, remain in the group count. Notes for
fewer than five businesses or twenty check-ins flag small samples; they do not
establish statistical reliability.

## Research competitors

**Top businesses and competitors** ranks businesses in the selected exact source
category by recorded check-ins. Name search narrows the displayed list without
changing the industry benchmark. Stable business IDs distinguish duplicate names;
zero-activity listings remain available and ties use stable ordering.

Choose up to four businesses in this page's selector to compare their recorded
totals, days with check-ins, weekend share and snapshot profile fields. A monthly
timeline uses selected covered dates, including partial months and recorded zeros.
These analysis-page choices are separate from the map's comparison selection.

For a shared weather chart with one activity line per business, check rows on
**Location map** and open **Compare businesses**. That page combines the weather
and activity chart with a side-by-side profile/activity matrix.

## Investigate one business's weather associations

Use **Business to investigate** to display one business's **Weather and recorded
activity** chart. Activity totals and weather boxes share calendar weeks for up to
90 selected days, then calendar months. Tooltips retain selected period boundaries
and covered calendar-day counts. Activity remains visible for periods without
weather reports.

Temperature boxes use the city's representative airport hourly measurements when
available, with mapped daily ACIS lows/highs as the fallback. Rainfall boxes use
accepted daily ACIS totals in mm/day, including dry days. Hourly and daily samples
are never pooled into one box. Boxes show Q1–Q3, median, minimum/maximum whiskers
and mean. A dry period can have a flat rainfall box. See
[Observed hourly weather](hourly-weather.md) for source and timestamp policies.

Expand **Analysis details** for daily paired-day counts, missing weather, and
unadjusted Pearson correlation. These associations always use the business's
mapped daily ACIS station, independently of the chart's hourly temperature source.
High and low temperature correlations remain separate. The observations table
preserves missing weather; weather-band summaries include paired-day counts and
activity quartiles.

Only covered activity is zero-filled. Missing or rejected weather is excluded
from daily pairs; valid zero rainfall and trace rainfall represented as zero are
included. Pearson correlation requires at least three paired days and variation
in both measures. Fewer than thirty paired days receives a sample warning.
Unmapped businesses and absent reports show an unavailable state.

The correlations do not control for weekdays, seasonality or other confounders,
and do not establish causation or statistical significance. Activity dates use
assumed source-local labels; alignment with station report dates is unverified.
Check-ins belong to the whole listing, so a hotel's activity cannot be attributed
specifically to its spa. Profile fields remain archive snapshots, regardless of
the activity dates.
