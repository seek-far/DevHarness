from pathlib import Path


def default_sqlite_path():
    """Standard location: ``~/.sdlcma/checkpoints/<run_id>.sqlite``.

    Overridable via the ``BF_CHECKPOINT_PATH`` environment variable so each
    run gets its own isolated checkpoint database.
    """
    home = Path.home()
    return home / ".sdlcma" / "checkpoints" / "state.sqlite"
