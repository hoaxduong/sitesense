# Business analyzer

Open **Business analysis**. The sidebar inputs follow **Metro area → Business
category → Source category → Date range**. Source categories are exact labels
already stored for the businesses in the selected broad category and city.
For example, Restaurant → Pizza requires both `Restaurants` and `Pizza`.
**All source categories** allows an initial overview before choosing an industry.

The supported metro controls use the imported Philadelphia/PA, Nashville/TN and
Tampa/FL canonical city cohorts, rather than official metro boundaries. All
comparisons use the intersection of the requested dates and imported check-in
coverage. Dates outside that history remain unavailable.

## Which source categories lead within the broad category?

**Top source categories** ranks every additional source label within the selected
broad category, city and effective period. Choosing a source category in the
sidebar does not remove the other groups from this discovery ranking.

The ranking uses **Recorded check-ins** in the effective period. Read the counts
alongside group size, the number of businesses with check-ins and the largest
business's share of activity.
Use **Minimum businesses per source category** to inspect rankings beyond very
small groups. Its default of one keeps every source category available; increasing
it is a visible comparison filter, not a statistical significance test.

Source categories overlap. One listing may appear in several groups, so group
counts and check-ins must not be added together. All matching snapshot listings,
including zero-activity listings, remain in the denominator. Groups with fewer
than five listings or twenty check-ins receive a data note; these thresholds do
not establish statistical reliability. The selected top-N view is a display
limit; the CSV exports the complete eligible ranking with its geographic/date
scope and minimum-business filter.

## Who are the leading businesses and competitors?

**Top businesses and competitors** uses the selected exact source category,
with optional name search. Rank by Recorded check-ins in the effective
period. Stable source IDs distinguish repeated names. Zero-activity businesses
remain available. Ordinal positions use stable tie ordering.

Research each business using its Recorded check-ins, dates with
check-ins, weekend activity share, full source labels, snapshot rating,
review count and operating status. Search narrows the visible list without
changing its industry benchmark. Choose up to four businesses for a shared
weather/activity chart. Each line totals Recorded check-ins over the same
calendar weeks or months, retaining partial periods and covered zero-activity days.

On Location map, check rows in the ranking table and use its toolbar action to
open Compare businesses. Its single weather/activity chart displays one activity
line per selected business, up to four, with shared weather boxplots. The map's
weather/activity timeline always totals the full filtered
business scope. City, broad-category and source-category changes reset
incompatible selections; ordinary date changes retain valid business IDs.
The comparison matrix also places snapshot profile fields, recorded totals,
days with check-ins and weekend share side by side for those same businesses.

## How does weather correlate with one business's activity?

The activity/weather chart totals Recorded check-ins over the same calendar periods
as its weather boxes: Monday-based calendar weeks for selected ranges of up to
90 calendar days, and calendar months for longer ranges. The activity line and weather boxes use
the same period midpoint. Tooltips identify the week or month, covered start/end
dates and calendar-day count; partial periods include only the selected dates
inside imported activity coverage. Zero-activity periods remain visible, including
periods with no weather reports. Weather boxplots use the right axis. When available,
boxes use routine hourly observations from the city's representative airport,
shared across ZIP and business views. Temperature uses one distribution of measured
temperatures. Precipitation uses accepted daily station totals in mm/day, including dry days. Boxes span Q1–Q3, with a
median line, min/max whiskers and mean diamond. Tooltips identify the
station, source, frequency, sample count and hourly coverage. Missing reports are
excluded; dry hours remain zero. A dry period can have a zero-height rainfall box.

When hourly data is unavailable, the chart labels its daily ACIS fallback. Daily
summary temperature combines available lows/highs; its mean describes those
summaries rather than hourly measured temperature. Hourly and daily samples are
never pooled into one box. See [hourly weather](hourly-weather.md).

Daily paired-day counts, correlations, bands and the observations table continue
to use the selected business's mapped ACIS station. High and low temperature
correlations remain separate from each other and from the hourly display source.

The dated observations table retains missing weather. The weather-band summary
remains available as a table with paired-day counts and activity quartiles.
Pearson correlation uses raw daily pairs rather than period or band summaries.

Activity is zero-filled only inside imported coverage; missing or rejected weather
stays missing and is excluded from paired days. Valid zero precipitation and trace
rainfall represented as zero remain included. The page reports total calendar
days, paired days, missing weather days and unadjusted Pearson correlation.
Correlation is unavailable with fewer than three paired days or a constant
variable; fewer than thirty paired days receives a sample warning. Unmapped
businesses and absent station observations show an unavailable state.

The correlation does not control for weekdays, seasonality or other confounders,
and does not establish causation or statistical significance. Source check-in
dates use assumed-local labels; alignment with station report dates is unverified.

Check-ins describe recorded Yelp activity, not customers, revenue, market share
or business success. Ratings, reviews, category tags and operating status describe
the archive snapshot. Historical opening dates are unavailable; zero check-ins
do not establish whether a business was open or closed. Activity belongs to the
whole listing, so a hotel's check-ins cannot be attributed specifically to its
spa. Smaller source-category views do not extend the broad-sector forecast model.
All primary pages now use [individual businesses](business-location-workspace.md).
Location map and Compare businesses share the exact Source category filter and
show individual recorded activity or independently trained business projections.
The business analysis page no longer groups or filters its peer list by ZIP.
