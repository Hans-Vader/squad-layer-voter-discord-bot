import pytest

import database


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the DB layer at a throwaway SQLite file and create the schema."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    database.init_db()
    return database
