import os

# Importing `bot` runs config.load_dotenv() and sys.exit(1)s when the token is
# missing, which would abort collection for every test module that imports it.
# setdefault, so a real .env still wins.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")

import pytest  # noqa: E402

import database  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the DB layer at a throwaway SQLite file and create the schema."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    database.init_db()
    return database
