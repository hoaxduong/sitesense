-- Serving tables read by the Streamlit pages. Published by `python -m sitesense.pipeline`.
-- Number 002 is reserved for the Yelp core import on the vinhnq branch.

CREATE TABLE sitesense.publish_runs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metro text NOT NULL,
    method_version text NOT NULL,
    settings jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sitesense.area (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    name text NOT NULL,
    centroid_lat double precision NOT NULL,
    centroid_lon double precision NOT NULL,
    business_count integer NOT NULL CHECK (business_count > 0),
    weather_cell_id text,
    weather_cell_km double precision,
    PRIMARY KEY (metro, category, area_id)
);

CREATE TABLE sitesense.activity_daily (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    obs_date date NOT NULL,
    checkins integer NOT NULL CHECK (checkins > 0),
    PRIMARY KEY (metro, category, area_id, obs_date)
);

CREATE TABLE sitesense.activity_profile (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    year smallint NOT NULL,
    weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    hour smallint NOT NULL CHECK (hour BETWEEN 0 AND 23),
    checkins integer NOT NULL CHECK (checkins > 0),
    PRIMARY KEY (metro, category, area_id, year, weekday, hour)
);

CREATE TABLE sitesense.area_factor (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    avg_weekly_checkins double precision NOT NULL,
    weekend_share double precision NOT NULL,
    growth_yoy double precision NOT NULL,
    avg_stars double precision NOT NULL,
    competitor_count integer NOT NULL,
    weather_resilience double precision NOT NULL,
    seasonality_ratio double precision NOT NULL,
    peak_month smallint NOT NULL,
    peak_hour smallint NOT NULL,
    busiest_day smallint NOT NULL,
    demand_norm double precision NOT NULL,
    growth_norm double precision NOT NULL,
    rating_norm double precision NOT NULL,
    competition_norm double precision NOT NULL,
    resilience_norm double precision NOT NULL,
    PRIMARY KEY (metro, category, area_id)
);

CREATE TABLE sitesense.weather_threshold (
    metro text NOT NULL,
    event_type text NOT NULL,
    threshold_level text NOT NULL,
    label text NOT NULL,
    office text NOT NULL,
    rule text NOT NULL,
    condition_note text NOT NULL,
    source_url text NOT NULL,
    source_status text NOT NULL,
    available boolean NOT NULL,
    days_per_year double precision,
    share_of_days double precision,
    PRIMARY KEY (metro, event_type, threshold_level)
);

CREATE TABLE sitesense.weather_effect (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text,  -- NULL = whole metro
    event_type text NOT NULL,
    threshold_level text NOT NULL,
    n_events integer NOT NULL,
    effect_pct double precision,
    ci_low double precision,
    ci_high double precision,
    p_value double precision,
    n_strict integer,
    effect_strict double precision,
    ci_low_strict double precision,
    ci_high_strict double precision,
    status text NOT NULL CHECK (
        status IN ('supported', 'partial', 'insufficient', 'not_supported', 'not_available')
    ),
    UNIQUE NULLS NOT DISTINCT (metro, category, area_id, event_type, threshold_level)
);

CREATE TABLE sitesense.anomaly_response (
    metro text NOT NULL,
    category text NOT NULL,
    week_start date NOT NULL,
    temp_anomaly_c double precision NOT NULL,
    checkin_change_pct double precision NOT NULL,
    PRIMARY KEY (metro, category, week_start)
);

CREATE TABLE sitesense.category_trend (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    year smallint NOT NULL,
    review_count integer NOT NULL,
    review_share double precision NOT NULL,
    new_businesses integer NOT NULL,
    avg_stars double precision,
    PRIMARY KEY (metro, category, area_id, year)
);

CREATE TABLE sitesense.typical_week (
    metro text NOT NULL,
    category text NOT NULL,
    area_id text NOT NULL,
    week smallint NOT NULL CHECK (week BETWEEN 1 AND 52),
    median double precision NOT NULL,
    p10 double precision NOT NULL,
    p90 double precision NOT NULL,
    years smallint NOT NULL,
    PRIMARY KEY (metro, category, area_id, week)
);
