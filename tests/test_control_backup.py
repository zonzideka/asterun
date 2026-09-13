from __future__ import annotations

from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest

from asterun.backup import DB_NAME, _validate_snapshot, backup_state, restore_state
from asterun.control_protocol import validate
from asterun.control_store import ControlStore, TABLE_COLUMNS
from asterun.errors import AsterunError
from asterun.sqlite_store import SCHEMA, SCHEMA_VERSION, SqliteStore
from tests.test_control_runtime import create_task, start_command


NS = "nsp_backup"


def seed_state(state_dir: Path) -> tuple[dict, dict]:
    database = SqliteStore(state_dir / DB_NAME)
    try:
        store = ControlStore(database._conn)
        content = "独立控制账本备份\n".encode()
        sha = store.put_blob(NS, content)
        artifact = {
            "schema_version": "runner-control/v1", "kind": "Artifact", "id": "art_backup",
            "namespace_id": NS, "revision": 1, "created_at": "2026-09-10T00:00:00Z",
            "updated_at": "2026-09-10T00:00:00Z", "extensions": {}, "task_id": "tsk_backup",
            "run_id": None, "assignment_id": None, "artifact_type": "document", "media_type": "text/plain",
            "storage_ref": "sqlite:sha256:" + sha, "sha256": sha, "size_bytes": len(content),
            "producer_id": "svc_backup", "verification": "unverified", "evidence_ids": [],
            "sensitivity": "internal", "allowed_recipient_refs": [],
        }
        validate(artifact, "Artifact")
        store.put(artifact)
        upload = {"kind": "Upload", "id": "upl_backup", "namespace_id": NS,
                  "task_id": "tsk_backup", "owner_id": "usr_backup", "sha256": sha, "size_bytes": len(content)}
        store.put(upload)
        return artifact, upload
    finally:
        database.close()


def archive_database(path: Path, dest: Path) -> None:
    data = path.read_bytes()
    manifest = json.dumps({"schema_version": SCHEMA_VERSION, "sha256": hashlib.sha256(data).hexdigest()}).encode()
    with tarfile.open(dest, "w:gz") as stream:
        for name, content in ((DB_NAME, data), ("manifest.json", manifest)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            stream.addfile(info, io.BytesIO(content))


def test_schema_four_upgrade_saves_snapshot_before_control_tables(tmp_path):
    path = tmp_path / DB_NAME
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES('schema_version','4')")
        conn.execute("INSERT INTO meta VALUES('applied_config_revision','7')")
        conn.commit()
    database = SqliteStore(path)
    try:
        assert SCHEMA_VERSION == 6
        snapshot = database.migration_backup
        assert snapshot.is_file()
        assert snapshot.stat().st_mode & 0o777 == 0o600
        assert _validate_snapshot(snapshot) == 4
        assert _validate_snapshot(path) == 6
        with closing(sqlite3.connect(snapshot)) as old:
            assert old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
            assert not old.execute("SELECT name FROM sqlite_master WHERE name LIKE 'control_%'").fetchall()
        assert database._conn.execute("SELECT value FROM meta WHERE key='applied_config_revision'").fetchone()[0] == "7"
    finally:
        database.close()
    reopened = SqliteStore(path)
    try:
        assert reopened.migration_backup is None
    finally:
        reopened.close()


def test_control_entities_and_blob_survive_backup_restore(tmp_path):
    source, target = tmp_path / "source", tmp_path / "restored"
    artifact, upload = seed_state(source)
    archive = tmp_path / "state.tar.gz"
    assert backup_state(source, archive)["schema_version"] == 6
    assert restore_state(archive, target)["schema_version"] == 6
    database = SqliteStore(target / DB_NAME)
    try:
        store = ControlStore(database._conn)
        assert store.get("Artifact", artifact["id"], NS) == artifact
        assert store.get("Upload", upload["id"], NS) == upload
        assert store.get_blob(NS, artifact["sha256"]) == "独立控制账本备份\n".encode()
    finally:
        database.close()


@pytest.mark.parametrize("completed", [False, True])
def test_full_fake_control_ledger_restores_with_pending_or_settled_budget(config_path, isolated_env, completed):
    from asterun.application import Application

    state, target = isolated_env / "control-source", isolated_env / "control-restored"
    app = Application.from_paths(config_path, state)
    try:
        runtime = app.control
        task = create_task(runtime)
        started = runtime.command(start_command(runtime, task))
        if completed:
            runtime.advance()
        expected = runtime.get("Run", started["result_ref"])
        assert expected["status"] == ("SUCCEEDED" if completed else "QUEUED")
        namespace = runtime.namespace
        archive = isolated_env / "full-control.tar.gz"
        backup_state(state, archive)
    finally:
        app.close()
    restore_state(archive, target)
    database = SqliteStore(target / DB_NAME)
    try:
        store = ControlStore(database._conn)
        assert store.get("Run", expected["id"], namespace) == expected
        total = store.budget_totals(namespace, task["budget_id"])[0]
        assert (total["settled"], total["held"]) == ((1, 0) if completed else (0, 1))
        assert bool(store.pending(namespace)) is not completed
    finally:
        database.close()


@pytest.mark.parametrize("table", sorted(TABLE_COLUMNS))
def test_v5_missing_control_table_is_rejected_without_replacing_target(tmp_path, table):
    source, target = tmp_path / "source", tmp_path / "target"
    seed_state(source)
    artifact, _ = seed_state(target)
    original_hash = hashlib.sha256((target / DB_NAME).read_bytes()).hexdigest()
    with closing(sqlite3.connect(source / DB_NAME)) as conn:
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
    archive = tmp_path / "broken.tar.gz"
    archive_database(source / DB_NAME, archive)
    with pytest.raises(AsterunError) as caught:
        restore_state(archive, target)
    assert caught.value.code == "INVALID_REQUEST"
    assert hashlib.sha256((target / DB_NAME).read_bytes()).hexdigest() == original_hash
    assert _validate_snapshot(target / DB_NAME) == 6


def test_v5_missing_control_column_is_rejected(tmp_path):
    source = tmp_path / "source"
    seed_state(source)
    with closing(sqlite3.connect(source / DB_NAME)) as conn:
        conn.execute("ALTER TABLE control_leases RENAME COLUMN fencing_token TO old_token")
        conn.commit()
    with pytest.raises(AsterunError, match="表结构"):
        backup_state(source, tmp_path / "invalid.tar.gz")


@pytest.mark.parametrize("damage", ["invalid_standard_entity", "identity_mismatch", "upload_extra_field", "upload_boolean_size", "duplicate_json_key", "missing_blob", "corrupt_blob", "wrong_storage_ref", "blob_namespace_mismatch"])
def test_control_content_corruption_is_rejected_even_with_matching_archive_hash(tmp_path, damage):
    source = tmp_path / "source"
    artifact, upload = seed_state(source)
    with closing(sqlite3.connect(source / DB_NAME)) as conn:
        if damage == "invalid_standard_entity":
            artifact["verification"] = "PASS"
        elif damage == "identity_mismatch":
            artifact["namespace_id"] = "nsp_other"
        elif damage == "wrong_storage_ref":
            artifact["storage_ref"] = "sqlite:sha256:" + "0" * 64
        elif damage == "upload_extra_field":
            upload["trusted"] = True
        elif damage == "upload_boolean_size":
            upload["size_bytes"] = True
        elif damage == "missing_blob":
            conn.execute("DELETE FROM control_blobs")
        elif damage == "corrupt_blob":
            conn.execute("UPDATE control_blobs SET content=?", (b"modified",))
        elif damage == "blob_namespace_mismatch":
            conn.execute("UPDATE control_blobs SET namespace_id='nsp_other'")
        conn.execute("UPDATE control_entities SET payload=? WHERE kind='Artifact'", (json.dumps(artifact),))
        conn.execute("UPDATE control_entities SET payload=? WHERE kind='Upload'", (json.dumps(upload),))
        if damage == "duplicate_json_key":
            conn.execute("UPDATE control_entities SET payload=? WHERE kind='Upload'", ('{"kind":"Upload",' + json.dumps(upload)[1:],))
        conn.commit()
    archive = tmp_path / "invalid.tar.gz"
    archive_database(source / DB_NAME, archive)
    with pytest.raises(AsterunError) as caught:
        restore_state(archive, tmp_path / "target")
    assert caught.value.code == "INVALID_REQUEST"
    assert not (tmp_path / "target" / DB_NAME).exists()


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_legacy_backup_versions_remain_readable(tmp_path, version):
    state = tmp_path / "legacy"
    state.mkdir()
    with closing(sqlite3.connect(state / DB_NAME)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO meta VALUES('schema_version',?)", (str(version),))
        conn.commit()
    assert _validate_snapshot(state / DB_NAME) == version
    archive = tmp_path / "legacy.tar.gz"
    assert backup_state(state, archive)["schema_version"] == version
    assert restore_state(archive, tmp_path / "restored")["schema_version"] == version
