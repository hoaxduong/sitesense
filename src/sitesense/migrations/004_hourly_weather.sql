CREATE TABLE sitesense.hourly_weather_observations (
    station_id text NOT NULL,
    city text NOT NULL,
    state text NOT NULL,
    station_name text NOT NULL,
    utc_offset_hours smallint NOT NULL CHECK (utc_offset_hours IN (-5, -6)),
    observation_time_local_standard timestamp without time zone NOT NULL,
    temperature_c double precision
        CHECK (temperature_c IS NULL OR (temperature_c > '-Infinity'::double precision
                                        AND temperature_c < 'Infinity'::double precision)),
    precipitation_mm double precision
        CHECK (precipitation_mm IS NULL OR (precipitation_mm >= 0
                                            AND precipitation_mm < 'Infinity'::double precision)),
    raw_temperature text NOT NULL,
    raw_precipitation text NOT NULL,
    is_trace boolean NOT NULL,
    report_type text NOT NULL CHECK (report_type = 'FM-15'),
    source_code text NOT NULL,
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    PRIMARY KEY (station_id, observation_time_local_standard),
    CHECK (NOT is_trace OR (precipitation_mm IS NOT NULL AND precipitation_mm = 0))
);
CREATE UNIQUE INDEX hourly_weather_station_hour_idx
    ON sitesense.hourly_weather_observations
        (station_id, date_trunc('hour', observation_time_local_standard));
CREATE INDEX hourly_weather_city_time_idx
    ON sitesense.hourly_weather_observations (city, state, observation_time_local_standard);
CREATE INDEX hourly_weather_dataset_idx
    ON sitesense.hourly_weather_observations (dataset_id);
