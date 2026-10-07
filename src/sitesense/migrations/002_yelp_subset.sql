CREATE TABLE sitesense.import_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_key text NOT NULL UNIQUE,
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    scope jsonb NOT NULL,
    source_manifest jsonb NOT NULL,
    summary jsonb NOT NULL,
    timestamp_semantics text NOT NULL DEFAULT 'unresolved_naive_source',
    completed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sitesense.weather_cells (
    weather_cell_id text PRIMARY KEY,
    latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180)
);

CREATE TABLE sitesense.businesses (
    import_run_id bigint NOT NULL REFERENCES sitesense.import_runs(id),
    business_id text NOT NULL,
    name text NOT NULL,
    address text NOT NULL,
    city text NOT NULL,
    state text NOT NULL,
    postal_code text NOT NULL,
    latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    stars double precision NOT NULL CHECK (stars BETWEEN 0 AND 5),
    review_count integer NOT NULL CHECK (review_count >= 0),
    is_open boolean NOT NULL,
    categories text[] NOT NULL,
    PRIMARY KEY (import_run_id, business_id)
);
CREATE INDEX businesses_region_idx ON sitesense.businesses (state, city);

CREATE TABLE sitesense.business_weather_mapping (
    import_run_id bigint NOT NULL,
    business_id text NOT NULL,
    weather_cell_id text NOT NULL REFERENCES sitesense.weather_cells(weather_cell_id),
    PRIMARY KEY (import_run_id, business_id),
    FOREIGN KEY (import_run_id, business_id)
        REFERENCES sitesense.businesses(import_run_id, business_id)
);

-- Keep source timestamps naive: their timezone meaning is not established.
-- Multiplicity retains repeated source events at the same timestamp.
CREATE TABLE sitesense.checkin_events (
    import_run_id bigint NOT NULL,
    business_id text NOT NULL,
    timestamp_naive timestamp without time zone NOT NULL,
    event_count integer NOT NULL CHECK (event_count > 0),
    PRIMARY KEY (import_run_id, business_id, timestamp_naive),
    FOREIGN KEY (import_run_id, business_id)
        REFERENCES sitesense.businesses(import_run_id, business_id)
);
CREATE INDEX checkin_events_time_idx ON sitesense.checkin_events (timestamp_naive);

-- Observed source days only; absent rows are not proof of business closure.
CREATE VIEW sitesense.business_activity_daily AS
SELECT import_run_id, business_id, timestamp_naive::date AS source_date,
       sum(event_count)::bigint AS checkin_count
FROM sitesense.checkin_events
GROUP BY import_run_id, business_id, timestamp_naive::date;

-- Reserved for actual weather observations, not populated from weather_cells.csv.
CREATE TABLE sitesense.weather_hourly (
    weather_cell_id text NOT NULL REFERENCES sitesense.weather_cells(weather_cell_id),
    timestamp_utc timestamptz NOT NULL,
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    temperature_2m double precision,
    relative_humidity_2m double precision CHECK (relative_humidity_2m BETWEEN 0 AND 100),
    precipitation double precision CHECK (precipitation >= 0),
    wind_speed_10m double precision CHECK (wind_speed_10m >= 0),
    PRIMARY KEY (dataset_id, weather_cell_id, timestamp_utc)
);
