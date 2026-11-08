from deploy.hermes_setup import (
    ALLOWED_TOOLSETS, API_TOOLSETS, DENIED_TOOLSETS, env_values, replace_env_lines, set_yaml_path, toolset_violations,
    voice_config,
)

ENV = "A=1\r\nAPI_SERVER_KEY=old\r\nAPI_SERVER_CORS_ORIGINS=x\r\n# note\r\nAPI_SERVER_KEY=dup\r\nB=2\r\n"
UP = {"API_SERVER_ENABLED": "true", "API_SERVER_KEY": "new", "API_SERVER_HOST": "127.0.0.1"}


def test_env_replaces_only_managed_lines_and_keeps_crlf():
    out = replace_env_lines(ENV, UP)
    assert out == ("A=1\r\nAPI_SERVER_KEY=new\r\nAPI_SERVER_CORS_ORIGINS=x\r\n# note\r\nB=2\r\n"
                   "API_SERVER_ENABLED=true\r\nAPI_SERVER_HOST=127.0.0.1\r\n")
    assert replace_env_lines(out, UP) == out  # idempotent


def test_env_empty_file():
    assert replace_env_lines("", {"K": "v"}) == "K=v\n"


CFG = """model:
  default: x  # keep
platforms:
  discord:
    extra:
      a: 1
platform_toolsets:
  cli:
    - browser
    - terminal
  discord:
    - web
# tail comment
known: 1
"""


def test_yaml_insert_new_sections_preserves_rest():
    out = set_yaml_path(CFG, ["gateway", "api_server", "max_concurrent_runs"], 6)
    assert out.startswith(CFG.rstrip("\n"))
    assert out.endswith("gateway:\n  api_server:\n    max_concurrent_runs: 6\n")


def test_yaml_add_list_then_replace_it():
    out = set_yaml_path(CFG, ["platform_toolsets", "api_server"], API_TOOLSETS)
    assert "  api_server:\n    - web\n    - memory\n    - no_mcp\n# tail comment\nknown: 1\n" in out
    assert "  cli:\n    - browser\n    - terminal\n  discord:\n    - web\n" in out
    again = set_yaml_path(out, ["platform_toolsets", "api_server"], ["memory"])
    assert again.count("api_server") == 1 and "no_mcp" not in again
    assert set_yaml_path(out, ["platform_toolsets", "api_server"], API_TOOLSETS) == out


def test_yaml_scalar_replace_keeps_crlf_and_other_lines():
    text = CFG.replace("\n", "\r\n")
    out = set_yaml_path(text, ["model", "default"], "y")
    assert out == text.replace("default: x  # keep", "default: y")


def test_empty_list_is_written_as_flow_empty_not_null():
    out = set_yaml_path(CFG, ["platform_toolsets", "cron"], [])
    assert "  cron: []\n" in out  # a bare "cron:" is null, which Hermes reads as "platform default"


# What `hermes profile create voice --no-alias --no-skills` writes (checked against hermes-agent f86276d).
SEEDED = """model:
  default: poolside/laguna-s-2.1:free
  provider: openrouter
  reasoning_effort: high
_config_version: 50
"""


def test_voice_config_locks_down_and_is_idempotent():
    out = voice_config(SEEDED)
    assert voice_config(out) == out
    assert "gateway:\n  standalone: true\n" in out
    assert "  api_server:\n    - web\n    - memory\n    - no_mcp\n  cron: []\n" in out
    assert "  reasoning_effort: none\n" in out and "high" not in out
    assert "  default: poolside/laguna-s-2.1:free\n" in out


def test_toolset_policy():
    assert set(API_TOOLSETS) - {"no_mcp"} == ALLOWED_TOOLSETS == {"web", "memory"}
    dangerous = {"terminal", "file", "code_execution", "browser", "computer_use", "delegation", "cronjob", "kanban",
                 "skills", "session_search", "connections"}
    assert dangerous <= set(DENIED_TOOLSETS)
    # Composites holding web/memory tools must not be denied, or web_search disappears with them.
    assert not {"web", "memory", "coding", "debugging", "safe", "search"} & set(DENIED_TOOLSETS)


def test_env_values_copies_only_named_keys_raw():
    text = 'A=1\r\nexport OPENROUTER_API_KEY="k1"\r\nFIRECRAWL_API_KEY=\r\nOPENROUTER_API_KEY=dup\r\n'
    assert env_values(text, ("OPENROUTER_API_KEY", "FIRECRAWL_API_KEY")) == {"OPENROUTER_API_KEY": '"k1"'}


def test_toolset_violations():
    assert toolset_violations(["memory", "web"]) == []
    assert toolset_violations(["web", "spotify", "my-mcp"]) == ["my-mcp", "spotify"]  # names outside the allowed set, MCP included
    assert toolset_violations(None) == ["<unreadable>"]


def test_env_never_backed_up(tmp_path):
    from deploy.hermes_setup import _write_if_changed
    f = tmp_path / ".env"
    f.write_text("A=1\n")
    assert _write_if_changed(f, "A=1\n", "A=2\n", backup=False)
    assert [x.name for x in tmp_path.iterdir()] == [".env"]


def test_resolver_runs_in_hermes_venv_and_matches_allowed():
    """Live proof against the installed Hermes (skipped where it is absent)."""
    import pytest
    from deploy import hermes_setup
    if not (hermes_setup.VOICE_HOME / "config.yaml").exists():
        pytest.skip("voice profile not installed")
    assert hermes_setup.verify_toolsets() == []
