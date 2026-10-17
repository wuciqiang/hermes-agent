"""Cron jobs with empty fleet pins follow the current global model assignment."""

import yaml


def _write_config(path, model, *, cron_model=""):
    path.write_text(
        yaml.safe_dump(
            {
                "model": {"default": model, "provider": "codeplan"},
                "cron": {"model": cron_model, "model_provider": "", "reasoning_effort": ""},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_empty_cron_model_tracks_global_default_across_reload(tmp_path, monkeypatch):
    from cron import scheduler

    monkeypatch.delenv("HERMES_MODEL", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    job = {"model": None}

    _write_config(config, "model-a")
    assert scheduler._load_cron_job_config(job, "job", "job").model == "model-a"
    _write_config(config, "model-b")
    assert scheduler._load_cron_job_config(job, "job", "job").model == "model-b"


def test_job_and_env_and_explicit_cron_model_keep_precedence(tmp_path, monkeypatch):
    from cron import scheduler

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    _write_config(config, "global-model", cron_model="cron-model")
    monkeypatch.setenv("HERMES_MODEL", "env-model")
    assert scheduler._load_cron_job_config({"model": "job-model"}, "job", "job").model == "job-model"
    assert scheduler._load_cron_job_config({"model": None}, "job", "job").model == "cron-model"
    _write_config(config, "global-model", cron_model="")
    assert scheduler._load_cron_job_config({"model": None}, "job", "job").model == "global-model"
    config.write_text("model:\n  default: global-model\n", encoding="utf-8")
    assert scheduler._load_cron_job_config({"model": None}, "job", "job").model == "env-model"
    config.unlink()
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    expected_default = str((scheduler.DEFAULT_CONFIG.get("cron") or {}).get("model") or "").strip()
    assert scheduler._load_cron_job_config({"model": None}, "job", "job").model == expected_default
