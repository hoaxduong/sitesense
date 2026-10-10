# SiteSense

SiteSense compares historical business activity across candidate areas to support further location research.

## Language

**Business**:
A business listed in the Yelp dataset, with a source identity and a snapshot of its location, categories, rating, and operating status.
_Avoid_: Candidate site, potential store

**Candidate area**:
A group of Yelp-listed businesses sharing a source city, state, and ZIP label, considered for comparison. It is not an exact property or a verified postal boundary.
_Avoid_: Candidate store, exact site, available property

**Area center**:
The mean coordinates of valid business points belonging to a candidate area. It is a representative dataset point, not an official ZIP centroid.
_Avoid_: Postal centroid, ZIP boundary

**Nearby category businesses**:
Yelp-listed businesses matching the selected category within a stated distance of an area center, within the imported city cohort. Their count describes dataset coverage rather than the complete competitive market.
_Avoid_: All competitors, live competition

**Check-in**:
A recorded Yelp activity timestamp associated with a business. It does not identify a unique customer or prove a physical visit.
_Avoid_: Visitor, customer count, footfall

**Recorded activity**:
The number or distribution of recorded check-ins within a stated population and time period. It is an activity proxy whose coverage depends on Yelp usage.
_Avoid_: Sales, revenue, actual demand

**Weather scenario**:
A hypothetical weather condition used to explore how activity might differ. A scenario is not a prediction of the weather or proof of a causal effect.
_Avoid_: Weather forecast, proven weather impact

**Mock content**:
Illustrative text or values used to demonstrate a screen when an applicable result is unavailable. It is explicitly identified as illustrative rather than a dataset finding.
_Avoid_: Measured result, validated recommendation

**Illustrative score**:
A weighted comparison of example factor values used to demonstrate ranking behavior. It is not a measured business-success probability or a validated recommendation.
_Avoid_: Success score, probability of success

**Spatial weather mapping coverage**:
The share of selected businesses associated with an imported weather point. It does not establish that every activity date has a valid weather observation.
_Avoid_: Weather-day match rate, complete weather coverage
