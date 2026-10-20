CREATE TABLE sitesense.review_rating_daily (
    business_id text NOT NULL REFERENCES sitesense.businesses(business_id) ON DELETE CASCADE,
    review_date date NOT NULL,
    star_sum integer NOT NULL,
    review_count integer NOT NULL CHECK (review_count > 0),
    dataset_id bigint NOT NULL REFERENCES sitesense.datasets(id),
    PRIMARY KEY (business_id, review_date),
    CHECK (star_sum BETWEEN review_count AND 5::bigint * review_count)
);
CREATE INDEX review_rating_daily_date_idx ON sitesense.review_rating_daily (review_date);
