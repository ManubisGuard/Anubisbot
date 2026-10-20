#!/usr/bin/env python3
"""CLI wrapper for MANUBIS Bot 2's safe SQLite backup/verification utilities."""
from pathlib import Path
import sys

# Support running this script directly from a source checkout without requiring
# an editable install; installed deployments continue using their package path.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if (_SOURCE_ROOT / "manubisguard").is_dir():
    sys.path.insert(0, str(_SOURCE_ROOT))

from manubisguard.database_maintenance import (
    DatabaseMaintenanceError,
    backup_database,
    main,
    restore_database,
    verify_database,
)

if __name__ == "__main__":
    raise SystemExit(main())
