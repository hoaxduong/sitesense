CREATE TABLE sitesense.datasets (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name text NOT NULL CHECK (length(btrim(name)) > 0),
    source_uri text NOT NULL CHECK (length(btrim(source_uri)) > 0),
    version text,
    created_at timestamptz NOT NULL DEFAULT now()
);
