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

The default ranking is **total recorded check-ins**, followed by **check-ins per
day**: the group's total check-ins divided by inclusive calendar days in the
effective period. The separate **check-ins per business per day** option divides
again by the number of matching listed businesses. Listed business count is also
available. For a common date window, total and per-day counts have the same rank;
the daily average makes the time denominator explicit. Read normalized activity
alongside the median, group size, number of businesses with check-ins and largest
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
with optional ZIP and name search. Rank by recorded check-ins in the effective
period by default, followed by check-ins per day, source-snapshot rating or
source-snapshot review count. Stable source
IDs distinguish repeated names. Missing ratings sort last and zero-activity
businesses remain available. Ordinal positions use stable tie ordering.

Research each business using its recorded activity, daily average, dates with
check-ins, weekend activity share, ZIP, full source labels, snapshot rating,
review count and operating status. Search narrows the visible list without
changing its industry benchmark. Choose up to four businesses for a comparison
and monthly activity chart of daily averages. Each monthly rate uses the selected calendar days
inside that month, including partial months and zero-activity days.

**Business to investigate** selects one business for weather analysis independently
of the comparison picks. Searches expose additional choices while retaining the
current investigated business. City, broad-category and source-category changes
reset incompatible selections; ordinary date changes retain valid business IDs.

## How does weather correlate with one business's activity?

The weather section uses only the selected business's mapped ACIS station and
accepted daily observations. Choose maximum temperature in Celsius or precipitation
in millimeters. **Box plot** is the default chart: it compares the distribution
of daily check-ins across fixed weather bands, with paired-day counts on each
band. Temperature bands are below 0, 0–<10, 10–<20, 20–<30 and at least 30 °C.
Rainfall bands are zero (dry or trace), above 0–<1, 1–<5, 5–<10, 10–<20 and at
least 20 mm. The groups include every paired day exactly once.

Each box spans the first to third quartiles, with a median line. Whiskers reach
the observed values within 1.5 times the interquartile range; more extreme values
appear as outlier points. Boxes use the raw daily counts, not averages of those
counts. Empty groups have no box and no invented zero-valued quartiles. Groups
with fewer than five paired days receive a warning. Zero-heavy distributions
can collapse to a line; the band summary reports zero-activity days and exact
quartiles alongside sample sizes.

Switch **Weather chart** to **Scatter plot** to inspect the individual daily
pairs. An optional pair of daily time-series charts and a dated observations
table support inspection. Pearson correlation always uses the raw daily pairs,
not the band medians, regardless of chart choice.

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
