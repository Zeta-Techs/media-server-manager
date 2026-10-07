from pathlib import Path

from clp_web.bulk_media import parse_line, row_dict
from clp_web.db import connect, init_db, new_secret, utcnow


def test_bulk_input_parsing():
    assert parse_line("550") == {"kind": "id", "id": 550, "media_type": ""}
    assert parse_line("https://www.themoviedb.org/tv/1399-breaking-bad?language=zh-CN")["media_type"] == "tv"
    assert parse_line("流浪地球 2019")["year"] == 2019


def test_bulk_schema_is_initialized(tmp_path: Path):
    db_file = tmp_path / "clp.db"
    init_db(db_file)
    with connect(db_file) as db:
        now = utcnow()
        server_id = db.execute(
            "INSERT INTO servers(name,address,token,webhook_secret,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("Plex", "http://plex", "token", new_secret(), now, now),
        ).lastrowid
        batch_id = db.execute(
            "INSERT INTO media_library_bulk_batches(server_id,created_at,updated_at) VALUES(?,?,?)",
            (server_id, now, now),
        ).lastrowid
        db.execute(
            "INSERT INTO media_library_bulk_rows(batch_id,line_number,input_text,created_at,updated_at) VALUES(?,?,?,?,?)",
            (batch_id, 1, "550", now, now),
        )
        assert db.execute("SELECT COUNT(*) FROM media_library_bulk_rows WHERE batch_id=?", (batch_id,)).fetchone()[0] == 1


def test_bulk_row_arrays_are_returned_as_arrays():
    row = {
        "id": 1,
        "candidates": '[{"media_type":"tv","tmdb_id":1399}]',
        "genres": '["剧情", "动作"]',
        "custom_genres": "[]",
        "raw_json": "{}",
    }
    result = row_dict(row)
    assert isinstance(result["candidates"], list)
    assert result["candidates"][0]["tmdb_id"] == 1399
    assert result["genres"] == ["剧情", "动作"]
