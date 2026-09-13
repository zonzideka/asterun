import json

from asterun.cli import main
from asterun.config import load_config


def test_migration_cli_dryrun_then_explicit_new_file(config_path, tmp_path, capsys):
    original = config_path.read_bytes()
    assert main(["--config", str(config_path), "config-migrate"]) == 0
    report = json.loads(capsys.readouterr().out)["data"]
    assert report["dry_run"] is True
    assert config_path.read_bytes() == original
    destination, backup = tmp_path / "v2.json", tmp_path / "v1.backup"
    assert main(["--config", str(config_path), "config-migrate", "--output", str(destination),
                 "--backup", str(backup), "--expected-source-sha256", report["source_sha256"]]) == 0
    assert load_config(destination).schema_version == 2
    assert config_path.read_bytes() == backup.read_bytes() == original
    assert not (tmp_path / "asterun.sqlite").exists()


def test_migration_cli_write_requires_review_digest(config_path, tmp_path, capsys):
    destination = tmp_path / "v2.json"
    assert main(["--config", str(config_path), "config-migrate", "--output", str(destination)]) == 1
    assert not destination.exists()
