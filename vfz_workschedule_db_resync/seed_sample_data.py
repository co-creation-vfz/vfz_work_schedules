# =============================================================================
# seed_sample_data.py
# Insert a few sample non-working-day rows, for local testing.
#
#   python seed_sample_data.py
#
# Reads the same segredo.ini as the service. Existing rows for the same user
# and date are updated rather than duplicated.
# =============================================================================

import os
import sys
from datetime import date

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

import resync
from config import Config

SAMPLE_ROWS = [
    {
        "user_id": "KUATAIRI",
        "user": "Abigail Hlalele",
        "work_schedule_title": "Testing",
        "work_schedule_id": "IEAFXAOHMIACBDCL",
        "date": date(2026, 8, 25),
    },
    {
        "user_id": "KUABBCDE",
        "user": "Jan de Vries",
        "work_schedule_title": "Four day week",
        "work_schedule_id": "IEAFXAOHMIACBDCM",
        "date": date(2026, 8, 25),
    },
    {
        "user_id": "KUABBCDE",
        "user": "Jan de Vries",
        "work_schedule_title": "Four day week",
        "work_schedule_id": "IEAFXAOHMIACBDCM",
        "date": date(2026, 8, 26),
    },
]


def main() -> None:
    config = Config.from_segredo()
    repository = resync.build_repository(config, pool_name="db_resync_seed")
    try:
        repository.ensure_schema()
        inserted, matched = repository.upsert_non_working(SAMPLE_ROWS)
        print(
            f"Seeded {len(SAMPLE_ROWS)} row(s) into "
            f"{config.mysql_database}.{repository.table} "
            f"({inserted} inserted, {matched} already present); "
            f"the table now holds {repository.count_rows()}."
        )
    finally:
        repository.close()


if __name__ == "__main__":
    main()
