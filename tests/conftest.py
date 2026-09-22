"""Shared pytest fixtures: isolate the suite from the developer's environment.

Two kinds of leakage are handled here. API keys are replaced with placeholders
so an absent key cannot hang CI on a real call. And the ``TRADINGAGENTS_*``
overlay is stripped, because ``tradingagents/__init__.py`` calls ``load_dotenv``
at package import: with a populated ``.env`` in the repo, a developer's own
provider and model settings become what the tests read as the framework's
defaults, and assertions about those defaults fail on their machine while
passing in CI.
"""

import copy
import importlib
import os
from unittest.mock import MagicMock, patch

import pytest


def _strip_env_overlay() -> dict:
    """Return DEFAULT_CONFIG as the framework ships it, ignoring any .env.

    Clearing the environment inside a fixture is not enough on its own:
    ``DEFAULT_CONFIG`` is built by ``_apply_env_overrides`` at *import* time, so
    by the time any fixture runs the dict already holds the developer's values
    and the originals are gone. The module is reloaded here with the overlay
    removed to recover them.

    The pre-reload dict object is put back as the module attribute afterwards,
    because modules that did ``from tradingagents.default_config import
    DEFAULT_CONFIG`` hold that object; rebinding the name would leave them on
    the polluted copy.
    """
    import tradingagents.default_config as default_config

    shared = default_config.DEFAULT_CONFIG
    saved = {k: v for k, v in os.environ.items() if k.startswith("TRADINGAGENTS_")}
    for key in saved:
        del os.environ[key]
    try:
        importlib.reload(default_config)
        pristine = copy.deepcopy(default_config.DEFAULT_CONFIG)
    finally:
        os.environ.update(saved)
    default_config.DEFAULT_CONFIG = shared
    shared.clear()
    shared.update(pristine)
    return pristine


_PRISTINE_DEFAULTS = _strip_env_overlay()


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_env_overlay(monkeypatch):
    """Keep a developer's .env out of every test, and out of DEFAULT_CONFIG.

    Tests that exercise the overlay deliberately (test_env_overrides.py) set
    their own vars and reload the module, so starting from a clean slate is
    what they want too.
    """
    for key in [k for k in os.environ if k.startswith("TRADINGAGENTS_")]:
        monkeypatch.delenv(key, raising=False)

    import tradingagents.default_config as default_config

    shared = default_config.DEFAULT_CONFIG
    before = copy.deepcopy(shared)
    shared.clear()
    shared.update(copy.deepcopy(_PRISTINE_DEFAULTS))
    yield
    shared.clear()
    shared.update(before)


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
