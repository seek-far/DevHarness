import os
from pathlib import Path


def db_path():
    """Path to the database file.

    Honors the $APP_DB_PATH environment variable; otherwise defaults to
    ~/.app/db.sqlite.
    """
    env = os.environ.get("APP_DB_PATH")
    if env:
        return Path(env)
    return Path.home() / ".app" / "db.sqlite"
