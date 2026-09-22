"""Config loading: defaults, file values, env interpolation."""

import pytest

from gojira.config import Config, load_config


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.database.url == "sqlite:///gojira.db"
    assert cfg.defaults.environments == ["dev", "qa", "prod"]


def test_loads_file_values(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("database:\n  url: postgresql://localhost/gojira\n")
    cfg = load_config(path)
    assert cfg.database.url == "postgresql://localhost/gojira"
    assert cfg.defaults.environments == ["dev", "qa", "prod"]  # defaults still apply


def test_env_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("GOJIRA_GITHUB_APP_ID", "98765")
    path = tmp_path / "config.yaml"
    path.write_text("github:\n  app_id: ${GOJIRA_GITHUB_APP_ID}\n")
    cfg = load_config(path)
    assert cfg.github.app_id == "98765"


def test_env_interpolation_default(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("github:\n  app_id: ${GOJIRA_UNSET_VAR:-fallback}\n")
    cfg = load_config(path)
    assert cfg.github.app_id == "fallback"


def test_unset_env_without_default_raises(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("github:\n  app_id: ${GOJIRA_DEFINITELY_UNSET}\n")
    with pytest.raises(ValueError, match="GOJIRA_DEFINITELY_UNSET"):
        load_config(path)


def test_config_is_pure_defaults():
    cfg = Config()
    assert cfg.github.app_id == ""
    assert cfg.defaults.environments == ["dev", "qa", "prod"]
