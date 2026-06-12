"""Cross-domain environment configuration."""

import os
from pathlib import Path

# FEEDLING_DIR is no longer the source of truth for user data (that lives in
# PostgreSQL now — see db.py). It is still used for non-user-data files that
# ride in the data volume, e.g. the APNs .p8 push key. Kept for compatibility.
FEEDLING_DIR = Path(os.environ.get("FEEDLING_DATA_DIR", str(Path.home() / "feedling-data"))).expanduser()
FEEDLING_DIR.mkdir(parents=True, exist_ok=True)
