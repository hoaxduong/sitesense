# SiteSense domain glossary

SiteSense compares historical activity at individual Yelp-listed businesses to
support further business and location research.

## Current application terms

**Business**: A Yelp listing with a stable source identity and snapshot location,
categories, rating, reviews and operating status. It is an existing archived
listing, not an available property or proposed new store.

**Metro area**: The imported Philadelphia/PA, Nashville/TN or Tampa/FL city cohort.
These groups do not represent complete official metropolitan boundaries or markets.

**Business category**: A broad parent group. Restaurant matches the source tag
`Restaurants`; Spa matches `Day Spas`.

**Source category**: An exact source tag within the parent category, such as
Restaurant → Pizza. All source categories retains the parent cohort. Tags can
overlap; whole-business check-ins are not divided among them, so category totals
must not be added together as distinct activity.

**Recorded check-ins**: Yelp activity timestamps counted for a stated population
and period. They do not identify unique customers or prove physical visits.
Covered dates without check-ins are zero. Failed reads and dates outside imported
coverage are unavailable, not zero.

**Projected check-ins**: Calendar-model estimates for supported individual
businesses on dates strictly after imported activity coverage. They extrapolate
archived patterns without future weather, rent or later market conditions. Missing
support stays unavailable. They do not estimate new-store revenue or success.

**Business profile**: Source snapshot fields such as categories, rating, review
count and status. Selecting historical activity dates does not turn these fields
into historically aligned profile observations.

**Observed weather**: Accepted station reports with stated dates, units and
coverage. Chart temperature uses representative city-airport hourly observations
when available, with a labeled daily ACIS fallback. Rain uses daily ACIS totals.
Shared charts pool each mapped station/date once; their weekly or monthly activity
lines use counts for the same calendar periods. Station context is not a local
measurement at every business or evidence of a causal weather effect.

## Retained research terms

These terms occur in older notebooks and the [research archive](docs/archive/README.md).
They do not define the current business activity ranking.

**Candidate area**: Businesses grouped by source city, state and ZIP label. This
is neither an exact property nor a verified postal boundary.

**Area center**: The mean of valid business coordinates in a candidate area,
not an official ZIP centroid.

**Spatial weather mapping coverage**: The share of selected businesses linked to
an imported weather point. It does not establish valid observations on every date.

**Weather scenario**: Hypothetical weather supplied to an area-model experiment.
It is neither a weather forecast nor proof of a causal effect.
