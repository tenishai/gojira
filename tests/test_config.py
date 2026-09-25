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


def test_namespace_labels_and_emails_accept_install_value_strings():
    """The in-cluster configmap hands both knobs over as single env strings
    (GOJIRA_NAMESPACE_LABELS is a YAML flow map, GOJIRA_ALLOWED_EMAILS a
    comma list); empty strings mean 'none'."""
    cfg = Config.model_validate({
        "defaults": {"namespace_labels": "{example.com/team: sre}"},
        "auth": {"allowed_emails": "a@x.com, b@x.com,"},
    })
    assert cfg.defaults.namespace_labels == {"example.com/team": "sre"}
    assert cfg.auth.allowed_emails == ["a@x.com", "b@x.com"]
    empty = Config.model_validate({"defaults": {"namespace_labels": ""}, "auth": {"allowed_emails": ""}})
    assert empty.defaults.namespace_labels == {} and empty.auth.allowed_emails == []


def test_observe_clusters_accept_install_value_string():
    """GOJIRA_OBSERVE_CLUSTERS arrives as one comma list; each name gets the
    argus defaults. Empty means no cluster is observed. The mapping form an
    operator writes by hand still round-trips."""
    from gojira.config import ObserveClusterConfig

    cfg = Config.model_validate({"observe": {"clusters": "fubo-dev, sandbox,"}})
    assert cfg.observe.clusters == {
        "fubo-dev": ObserveClusterConfig(), "sandbox": ObserveClusterConfig(),
    }
    assert Config.model_validate({"observe": {"clusters": ""}}).observe.clusters == {}
    hand = Config.model_validate(
        {"observe": {"clusters": {"dev": {"namespace": "obs", "service_account": "sa"}}}}
    )
    assert hand.observe.clusters["dev"].namespace == "obs"
