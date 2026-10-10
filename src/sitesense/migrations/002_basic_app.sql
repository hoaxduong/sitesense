ALTER TABLE sitesense.datasets ADD COLUMN metadata jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(metadata) = 'object');

CREATE TABLE sitesense.candidate_areas (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    city text NOT NULL,
    state text NOT NULL,
    postal_code text NOT NULL CHECK (postal_code ~ '^[0-9]{5}$'),
    display_name text NOT NULL,
    latitude double precision CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision CHECK (longitude BETWEEN -180 AND 180),
    UNIQUE (city, state, postal_code)
);

CREATE TABLE sitesense.weather_cells (
    weather_cell_id text PRIMARY KEY,
    latitude double precision NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id)
);

CREATE TABLE sitesense.businesses (
    business_id text PRIMARY KEY,
    area_id bigint REFERENCES sitesense.candidate_areas(id),
    name text NOT NULL,
    address text NOT NULL,
    city text NOT NULL,
    state text NOT NULL,
    canonical_city text NOT NULL,
    canonical_state text NOT NULL,
    postal_code text NOT NULL,
    latitude double precision CHECK (latitude BETWEEN -90 AND 90),
    longitude double precision CHECK (longitude BETWEEN -180 AND 180),
    categories text[] NOT NULL,
    stars double precision CHECK (stars BETWEEN 0 AND 5),
    review_count integer NOT NULL CHECK (review_count >= 0),
    is_open boolean NOT NULL,
    weather_cell_id text REFERENCES sitesense.weather_cells(weather_cell_id),
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id)
);
CREATE INDEX businesses_scope_idx ON sitesense.businesses (canonical_city, canonical_state);
CREATE INDEX businesses_area_idx ON sitesense.businesses (area_id);
CREATE INDEX businesses_categories_idx ON sitesense.businesses USING gin (categories);

CREATE TABLE sitesense.business_activity_hourly (
    business_id text NOT NULL REFERENCES sitesense.businesses(business_id) ON DELETE CASCADE,
    activity_date date NOT NULL,
    hour_of_day smallint NOT NULL CHECK (hour_of_day BETWEEN 0 AND 23),
    checkin_count integer NOT NULL CHECK (checkin_count > 0),
    timestamp_policy text NOT NULL CHECK (timestamp_policy = 'assumed_source_local'),
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    PRIMARY KEY (business_id, activity_date, hour_of_day)
);
