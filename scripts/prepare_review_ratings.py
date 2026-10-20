"""Explicitly stream complete local Yelp reviews into dated star totals, without text."""

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

import psycopg

from sitesense.database import ConfigurationError
from sitesense.import_data.review_ratings import (
    DEFAULT_SOURCE,
    ReviewRatingImportError,
    import_review_ratings,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--businesses",
        type=Path,
        help="Matching full business JSONL; required only with a full review JSONL input.",
    )
    args = parser.parse_args()
    try:
        summary = import_review_ratings(
            args.source,
            business_source=args.businesses,
            progress=lambda count: print(
                f"Validated {count:,} complete-source reviews…", flush=True
            ),
        )
        print(asdict(summary))
    except (ReviewRatingImportError, ConfigurationError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except psycopg.Error:
        print(
            "Review import failed. Check database connectivity and pending migrations.",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError):
        print("Review import failed. Check the complete local source files.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
