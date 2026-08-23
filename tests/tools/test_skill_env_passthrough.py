"""Test that skill_view registers required env vars in the passthrough registry."""

import json
import os
from unittest.mock import patch

import pytest

import tools.env_passthrough as _ep_mod
from tools.env_passthrough import clear_env_passthrough, is_env_passthrough


@pytest.fixture(autouse=True)
def _clean_passthrough():
    clear_env_passthrough()
    _ep_mod._config_passthrough = None
    yield
    clear_env_passthrough()
    _ep_mod._config_passthrough = None


def _create_skill(tmp_path, name, frontmatter_extra=""):
    """Create a minimal skill directory with SKILL.md."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n"
        f"name: {name}\n"
        f"description: Test skill\n"
        f"{frontmatter_extra}"
        f"---\n\n"
        f"# {name}\n\n"
        f"Test content.\n"
    )
    return skill_dir


class TestSkillViewRegistersPassthrough:
    def test_available_env_vars_registered(self, tmp_path, monkeypatch):
        """When a skill declares required_environment_variables and the var IS set,
        it should be registered in the passthrough."""
        _create_skill(
            tmp_path,
            "test-skill",
            frontmatter_extra=(
                "required_environment_variables:\n"
                "  - name: TENOR_API_KEY\n"
                "    prompt: Enter your Tenor API key\n"
            ),
        )
        monkeypatch.setattr(
            "tools.skills_tool.SKILLS_DIR", tmp_path
        )
        # Set the env var so it's "available"
        monkeypatch.setenv("TENOR_API_KEY", "test-value-123")

        # Patch the secret capture callback to not prompt
        with patch("tools.skills_tool._secret_capture_callback", None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="test-skill"))

        assert result["success"] is True
        assert is_env_passthrough("TENOR_API_KEY")


    def test_no_env_vars_skill_no_registration(self, tmp_path, monkeypatch):
        """Skills without required_environment_variables shouldn't register anything."""
        _create_skill(tmp_path, "simple-skill")
        monkeypatch.setattr(
            "tools.skills_tool.SKILLS_DIR", tmp_path
        )

        with patch("tools.skills_tool._secret_capture_callback", None):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="simple-skill"))

        assert result["success"] is True
        from tools.env_passthrough import get_all_passthrough
        assert len(get_all_passthrough()) == 0

    def test_persisted_skill_env_reaches_all_local_child_paths(
        self, tmp_path, monkeypatch
    ):
        """A profile-only skill credential must reach terminal and execute_code."""
        env_name = "BACKLINKHUB_SITE_TEST_CONTACT_EMAIL"
        env_value = "profile-only@example.test"
        _create_skill(
            tmp_path,
            "profile-env-skill",
            frontmatter_extra=(
                "required_environment_variables:\n"
                f"  - name: {env_name}\n"
                "    prompt: Enter contact email\n"
            ),
        )
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", tmp_path)
        monkeypatch.delenv(env_name, raising=False)

        with (
            patch("tools.skills_tool._secret_capture_callback", None),
            patch("tools.skills_tool.load_env", return_value={env_name: env_value}),
        ):
            from tools.skills_tool import skill_view

            result = json.loads(skill_view(name="profile-env-skill"))

        assert result["success"] is True
        assert is_env_passthrough(env_name)

        from agent.secret_scope import reset_secret_scope, set_secret_scope
        from tools.code_execution_tool import _scrub_child_env
        from tools.environments.local import _make_run_env, _sanitize_subprocess_env

        token = set_secret_scope({env_name: env_value})
        try:
            assert _make_run_env({})[env_name] == env_value
            assert _sanitize_subprocess_env(os.environ)[env_name] == env_value
            assert _scrub_child_env(os.environ)[env_name] == env_value
        finally:
            reset_secret_scope(token)
