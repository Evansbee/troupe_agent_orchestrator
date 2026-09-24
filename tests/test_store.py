import sqlite3

from troupe.store import Store


def test_start_run_persists_prompt_and_system(project):
    cfg, store = project
    run_id = store.start_run("lead", "task", None, "/tmp", False, prompt="do X", system="you are lead")
    run = store.one("SELECT * FROM runs WHERE id=?", run_id)
    assert run["prompt"] == "do X"
    assert run["system"] == "you are lead"


def test_existing_db_without_prompt_columns_migrates_cleanly(tmp_path):
    db_path = tmp_path / "troupe.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE runs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, started REAL, ended REAL, reason TEXT,
          status TEXT DEFAULT 'running', cost REAL DEFAULT 0, tokens INTEGER DEFAULT 0,
          task_id INTEGER, cwd TEXT, summary TEXT DEFAULT '', chat INTEGER DEFAULT 0
        );
    """)
    conn.execute("INSERT INTO runs(agent,started,reason,cwd,chat) VALUES('lead',0,'task','/tmp',0)")
    conn.commit()
    conn.close()

    store = Store(db_path)  # must not raise despite the pre-existing table lacking the new columns
    cols = {r["name"] for r in store.q("PRAGMA table_info(runs)")}
    assert {"prompt", "system"} <= cols
    run = store.one("SELECT * FROM runs WHERE id=1")
    assert run["prompt"] == "" and run["system"] == ""

    # a second open (simulating another process attaching to the now-migrated db) is also a no-op
    Store(db_path)
