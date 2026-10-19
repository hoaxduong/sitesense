"""Train weather-aware individual-business HGB models outside Streamlit."""

import argparse
from pathlib import Path

from sitesense import business_hgb, business_hgb_training


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=business_hgb.MODEL_DIR)
    args = parser.parse_args()
    manifest = business_hgb_training.train_from_database(args.output)
    print(f"Saved HGB models for {manifest['final_businesses']:,} business/category records.")


if __name__ == "__main__":
    main()
