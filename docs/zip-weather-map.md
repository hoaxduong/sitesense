# ZIP weather map

Weather observations fills eligible ZIP areas with representative observations for
the screen's selected city, category, and dates. Users choose mean daily maximum
temperature, mean daily minimum temperature, or precipitation over valid reports.

Each value is the mean of usable mapped-station summaries, weighted by the number
of listed category businesses linked to each station in the ZIP. Temperature uses
station means; precipitation uses station totals over accepted reports, in mm.
This is a representative station indicator, not an interpolated weather surface or
an estimate of rainfall volume across the ZIP. Category changes can change the
representative value because the mapped business population changes.

Missing station variables are excluded from the weighted mean. Tooltips report
the usable business mapping coverage and weighted report coverage. Zero rainfall
is a measured value. Areas without a usable selected metric are gray; the panel
is hidden when none of the mapped areas has usable observations.

Hover cards stay in the upper-left map corner to avoid clipping at edge ZIPs.
They show the ZIP, emphasized weather value, report dates, weighted valid
days, business coverage, and station count. Station names and individual report
coverage remain in the selected ZIP's detail tables. A map-scoped widget-root
positioning rule restores the missing deck.gl layout stylesheet in Streamlit
1.65; it keeps hover coordinates aligned with the canvas after page scrolling.

## Boundaries

The bundled GeoJSON comes from the U.S. Census Bureau's
[2020 ZIP Code Tabulation Area layer](https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1).
[ZCTAs approximate ZIP areas](https://www.census.gov/programs-surveys/geography/guidance/geo-areas/zctas.html);
they are not USPS delivery-route boundaries. The asset uses WGS84 coordinates,
six decimal places, and Census API generalization of 0.00005 degrees.

The 2026-10-11 preparation requested all 210 imported source-city ZIP labels and
returned 164 polygons. Four currently eligible Restaurant ZIPs have no matching
ZCTA: Philadelphia 19176/19195, Nashville 37246, and Tampa 33601. Their business
and station data remain available in the existing detail views; the map reports
missing boundary coverage and does not invent polygons. All eligible Spa ZIPs have
boundaries.

The asset records the query source, boundary vintage, preparation time, requested
and missing ZIPs, and raw-response SHA-256 values. Its SHA-256 at preparation is
`6586c9d0f1a6bf37d9e4f0223ee4e8a8f744960120d89fd0b2c63781d7939987`.
It is included in both the wheel and source distribution. Runtime rendering reads
the cached packaged asset; it does not download boundary data or write to the
database.

Refresh explicitly after adding source-city ZIP areas:

```sh
uv run --frozen --env-file .env python scripts/prepare_zip_boundaries.py
```

The preparation script reads the local database in a read-only transaction,
downloads public polygons, validates labels and coordinates, and atomically
replaces the asset only after all requested batches succeed.
