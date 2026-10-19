"""Explain the checked-in database architecture without opening a connection."""

from pathlib import Path

import streamlit as st

st.caption("How our data is stored and connected")
st.markdown(
    "SiteSense uses **PostgreSQL** to connect Yelp business listings and recorded "
    "check-ins with station weather observations. Tables live in the **`sitesense` schema**. "
    "Python imports prepared source files; the Streamlit app reads the imported records "
    "through a shared query layer."
)
st.info(
    "This guide describes the schema and code in this checkout, including migrations "
    "001–004. It does not inspect a running database or report live row counts, "
    "data coverage or applied migrations."
)

st.subheader("1. From source files to the app")
st.code(
    "Yelp archives       ACIS daily reports       NOAA hourly reports\n"
    "      │                     │                        │\n"
    "      └────────── Prepared local files ──────────────┘\n"
    "                            │\n"
    "                    Explicit Python imports\n"
    "                            │\n"
    "                  PostgreSQL · sitesense schema\n"
    "                            │\n"
    "                  repository.py · SQL reads\n"
    "                            │\n"
    "                   Analysis → Streamlit pages",
    language=None,
)
st.markdown("""
- **Preparation:** notebooks and scripts acquire and prepare source data outside the app.
- **Import:** source adapters write business, activity and weather records, with dataset
  provenance. A combined Yelp/daily-weather import commits together or rolls back on failure.
  Weather-only refreshes and hourly-weather imports also have explicit entry points.
- **Read:** `repository.py` uses parameterized SQL through Psycopg. Python modules calculate
  summaries and model estimates; page scripts render the results. Streamlit reruns do not
  download datasets, run migrations or train models.
- **Outside PostgreSQL:** raw archives, staged files, notebooks and trained model bundles
  remain files. The app's business HGB bundle lives under `models/business_hgb/`.
""")

st.subheader("2. What each table stores")
st.caption("Keys below describe one stored row. All table names belong to the sitesense schema.")
st.markdown(
    "| Table | One row represents | Primary key |\n"
    "| --- | --- | --- |\n"
    "| `datasets` | A source dataset: URI, version and JSON metadata for provenance, "
    "import state and coverage | `id` |\n"
    "| `candidate_areas` | A retained city/state/ZIP grouping and representative coordinates "
    "| `id`; unique `(city, state, postal_code)` |\n"
    "| `businesses` | A Yelp snapshot: location, exact tags, rating, review count and open status "
    "| `business_id` |\n"
    "| `business_activity_hourly` | A positive check-in count for one business/date/hour "
    "| `(business_id, activity_date, hour_of_day)` |\n"
    "| `weather_cells` | An ACIS station or legacy ERA5 point, distinguished by `source_kind` "
    "| `weather_cell_id` |\n"
    "| `station_weather_observations` | One daily variable: `tmin`, `tmax` or `pcpn`, "
    "with value and source flags | `(weather_cell_id, observation_date, variable)` |\n"
    "| `hourly_weather_observations` | A NOAA report: temperature, precipitation and source flags "
    "| `(station_id, observation_time_local_standard)` |\n"
    "| `schema_migrations` | An applied migration, its checksum and application time | `version` |"
)

st.subheader("3. Entity relationship diagram (ERD)")
st.caption(
    "PK = primary key · FK = foreign key · 1 = exactly one · 0..1 = optional one · "
    "0..* = zero or many. Multiple PK fields form one composite key. "
    "Arrows run from referenced parent to child; dashed lines are dataset foreign keys."
)
erd = (Path(__file__).parents[1] / "database_erd.dot").read_text(encoding="utf-8")
st.graphviz_chart(
    erd,
    width="stretch",
    alt=(
        "Eight-table SiteSense ERD: datasets supplies provenance to five tables; "
        "businesses optionally reference areas and weather cells; hourly activity references "
        "businesses; daily observations reference weather cells. Hourly weather has only a "
        "dataset foreign key, and schema_migrations is independent."
    ),
)
st.download_button(
    "Download ERD source",
    data=erd,
    file_name="sitesense-erd.dot",
    mime="text/vnd.graphviz",
    icon=":material/download:",
)
st.caption(
    "Fields without a nullable marker are required. The two observation tables are separate; "
    "city/time matching for hourly chart weather is application logic, not a foreign key. "
    "schema_migrations has no relationships to the source data."
)
st.markdown("""
**A business is the main link.** Its `area_id` optionally references a ZIP grouping;
its `weather_cell_id` optionally references a station or legacy weather point.
Check-in rows reference the business. Daily weather rows reference the weather point,
so businesses mapped to the same station share its observations.

**Provenance follows the records.** `businesses`, `business_activity_hourly`,
`weather_cells`, `station_weather_observations` and `hourly_weather_observations`
each have a foreign key, `dataset_id`, to `datasets`. Areas and migration history do not.

**Hourly weather is a separate path.** It has no foreign key to a business or
`weather_cells`; the repository selects representative station samples by city/state
and time. It does not replace the mapped daily ACIS station used by analysis and HGB.
""")

st.subheader("4. Reading activity and weather correctly")
st.markdown("""
**Activity is sparse.** The database stores only positive hourly check-in buckets.
The application fills absent buckets with zero only inside completed imported activity
coverage. Missing imports and dates outside that coverage remain unavailable.
Check-in date/hour labels use the unverified `assumed_source_local` timestamp policy.
Check-ins are an activity proxy, not unique customers or revenue.

**Daily weather preserves the source.** ACIS values are stored in Fahrenheit and inches,
alongside raw values, flags, trace markers and observation times. Missing values stay
null. The read layer excludes unsuitable flags and converts accepted values to Celsius
and millimeters for analysis. Station report windows and check-in timezone assumptions
limit exact time alignment.

**Hourly weather serves chart samples.** NOAA reports store Celsius and millimeters,
with fixed local-standard timestamps and UTC offsets. A unique station/hour index
allows at most one retained report per station-hour. Temperature charts can use these
samples; precipitation charts still use accepted daily ACIS totals. Hourly rainfall
is not summed into those daily totals.

**Geography and profiles have boundaries.** Imported city cohorts cover Philadelphia,
Nashville and Tampa; the “Metro area” label does not establish official metro boundaries.
ZIP grouping points do not define postal polygons. Ratings and open status describe
the archive snapshot, and exact source category tags can overlap.
""")

st.subheader("5. Consistency, migrations and local storage")
st.markdown("""
- **Relationships and validation:** foreign keys enforce the links above. Composite
  keys prevent duplicate activity/weather records. Constraints validate coordinates,
  hours, counts, weather units and trace values. Business activity is deleted with its
  parent business through `ON DELETE CASCADE`.
- **Query indexes:** businesses have city/state and area indexes plus a GIN index on
  category tags. Weather tables have dataset indexes; hourly weather also has a
  city/state/time index.
- **Import consistency:** reimports replace generated records for the selected cohorts
  transactionally. Completed import revisions identify source changes, letting cached
  results refresh. Hourly display weather has its own revision; it does not change
  daily model provenance. Model serving additionally checks its saved provenance.
- **Schema changes:** the migration runner records checksums in `schema_migrations`,
  requires an exact prefix of known migrations, serializes migration attempts with an
  advisory transaction lock, and applies pending migrations atomically.
- **Connections:** `database.py` reads the connection setting from the environment.
  Connection contexts commit on success, roll back on failure and close on exit;
  individual open connections are not cached across Streamlit sessions.
- **Local persistence:** Compose defines PostgreSQL 18, binds its port to localhost
  and keeps data in a named Docker volume. Stopping the container retains the volume;
  removing the volume deletes the local database.
""")

with st.expander("Implementation sources"):
    st.markdown("""
    - Schema: `src/sitesense/migrations/001_datasets.sql`, `002_basic_app.sql`,
      `003_station_weather.sql`, `004_hourly_weather.sql`.
    - Connections and migration history: `src/sitesense/database.py`.
    - Import adapters and orchestration: `src/sitesense/import_data/`.
    - Read queries and unit conversion: `src/sitesense/repository.py`.
    - Model loading and provenance checks: `src/sitesense/business_hgb.py`.
    - Local database service: `compose.yaml`.
    - Operational documentation: `docs/database.md` and `docs/hourly-weather.md`.
    """)
