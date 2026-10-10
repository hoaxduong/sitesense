ALTER TABLE sitesense.weather_cells
    ADD COLUMN source_kind text NOT NULL DEFAULT 'era5'
        CHECK (source_kind IN ('era5', 'acis_station')),
    ADD COLUMN station_name text;

CREATE TABLE sitesense.station_weather_observations (
    weather_cell_id text NOT NULL REFERENCES sitesense.weather_cells(weather_cell_id),
    observation_date date NOT NULL,
    variable text NOT NULL CHECK (variable IN ('tmax', 'tmin', 'pcpn')),
    value double precision
        CHECK (value IS NULL OR (value > '-Infinity'::double precision
                                AND value < 'Infinity'::double precision)),
    unit text NOT NULL,
    raw_value text NOT NULL,
    flag text NOT NULL,
    network_id text NOT NULL,
    source_flag text NOT NULL,
    observation_time_local_standard text NOT NULL,
    is_trace boolean NOT NULL,
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    PRIMARY KEY (weather_cell_id, observation_date, variable),
    CHECK ((variable IN ('tmax', 'tmin') AND unit = 'degreeF')
           OR (variable = 'pcpn' AND unit = 'inch')),
    CHECK (variable <> 'pcpn' OR value IS NULL OR value >= 0),
    CHECK (NOT is_trace OR (variable = 'pcpn' AND value IS NOT NULL AND value = 0))
);
CREATE INDEX station_weather_dataset_idx
    ON sitesense.station_weather_observations (dataset_id);
