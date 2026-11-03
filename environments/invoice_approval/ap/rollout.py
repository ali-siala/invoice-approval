"""Per-rollout databases: one seed template per process, one stripped copy per rollout."""

import atexit
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import schema, seed

HIDDEN_COLUMNS = ("hidden_trap", "hidden_outcome")

_template: str | None = None


def seed_template() -> str:
    """Path of the seed-42 database, built once per process and deleted at exit."""
    global _template
    if _template is None:
        tmp = tempfile.mkdtemp(prefix="ap-seed-")
        atexit.register(shutil.rmtree, tmp, ignore_errors=True)
        _template = seed.build_db(Path(tmp) / "seed.sqlite")
    return _template


def prepare_db(dest: str | Path, approver_id: str, template: str | None = None) -> str:
    """Copy the seed to `dest`, drop the hidden columns, and set the acting approver.

    The hidden columns hold the generator's intended outcomes; dropping them (and vacuuming, so no
    old bytes stay in free pages) means nothing the agent can reach ever contains them.
    """
    shutil.copyfile(template or seed_template(), dest)
    conn = sqlite3.connect(dest)
    try:
        for column in HIDDEN_COLUMNS:
            conn.execute(f"ALTER TABLE invoices DROP COLUMN {column}")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()
    schema.set_acting_approver(str(dest), approver_id)
    return str(dest)
