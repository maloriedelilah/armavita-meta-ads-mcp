# Copyright (C) 2025 ArmaVita LLC
# SPDX-License-Identifier: AGPL-3.0-only

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import armavita_meta_ads_mcp
from armavita_meta_ads_mcp.core import mcp_runtime


def test_package_exports():
    assert armavita_meta_ads_mcp.__version__ == "2.0.0"
    assert callable(armavita_meta_ads_mcp.entrypoint)


def test_runtime_rejects_unknown_transport(capsys):
    # stdio and streamable-http are the only transports; anything else is a usage error.
    with patch.object(sys, "argv", ["armavita-meta-ads-mcp", "--transport", "sse"]):
        with pytest.raises(SystemExit) as exc:
            mcp_runtime.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


SECRET = "s" * 40


def test_runtime_streamable_http_dispatches_to_http_runner(monkeypatch):
    monkeypatch.setenv("ARMAVITA_MCP_SHARED_SECRET", SECRET)
    argv = ["armavita-meta-ads-mcp", "--transport", "streamable-http", "--port", "9999", "--allowed-host", "mcp.example.com"]
    with patch.object(sys, "argv", argv), patch.object(mcp_runtime, "_run_streamable_http", return_value=0) as runner:
        assert mcp_runtime.main() == 0

    runner.assert_called_once_with("127.0.0.1", 9999, ["mcp.example.com"], "/mcp", SECRET)


def test_http_mode_refuses_to_start_without_a_shared_secret(monkeypatch, capsys):
    monkeypatch.delenv("ARMAVITA_MCP_SHARED_SECRET", raising=False)
    argv = ["armavita-meta-ads-mcp", "--transport", "streamable-http"]
    with patch.object(sys, "argv", argv), patch.object(mcp_runtime, "_run_streamable_http", return_value=0) as runner:
        assert mcp_runtime.main() == 2
    runner.assert_not_called()
    assert "ARMAVITA_MCP_SHARED_SECRET" in capsys.readouterr().err


def test_http_mode_refuses_a_short_secret(monkeypatch):
    monkeypatch.setenv("ARMAVITA_MCP_SHARED_SECRET", "short")
    with patch.object(sys, "argv", ["x", "--transport", "streamable-http"]), patch.object(
        mcp_runtime, "_run_streamable_http", return_value=0
    ) as runner:
        assert mcp_runtime.main() == 2
    runner.assert_not_called()


def test_no_shared_secret_is_only_for_loopback_without_a_public_host(monkeypatch):
    monkeypatch.delenv("ARMAVITA_MCP_SHARED_SECRET", raising=False)
    for extra in (["--host", "0.0.0.0"], ["--allowed-host", "mcp.example.com"]):
        argv = ["x", "--transport", "streamable-http", "--no-shared-secret", *extra]
        with patch.object(sys, "argv", argv), patch.object(mcp_runtime, "_run_streamable_http", return_value=0) as runner:
            assert mcp_runtime.main() == 2
        runner.assert_not_called()

    with patch.object(sys, "argv", ["x", "--transport", "streamable-http", "--no-shared-secret"]), patch.object(
        mcp_runtime, "_run_streamable_http", return_value=0
    ) as runner:
        assert mcp_runtime.main() == 0
    runner.assert_called_once_with("127.0.0.1", 8765, [], "/mcp", None)


def test_runtime_version_flag(capsys):
    with patch.object(sys, "argv", ["armavita-meta-ads-mcp", "--version"]):
        exit_code = mcp_runtime.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Armavita Meta Ads MCP v2.0.0" in captured.out


def test_the_version_agrees_everywhere():
    """ADS-207: 1.2.0 sat unchanged for months. pyproject, _version, the README's contract
    version and a CHANGELOG heading must name the same version."""
    import tomllib

    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert armavita_meta_ads_mcp.__version__ == version
    assert f"Current contract version: `{version}`" in (root / "README.md").read_text()
    assert f"\n## {version} " in (root / "CHANGELOG.md").read_text()


@pytest.mark.parametrize("direct_url, shown", [
    ('{"url": "https://github.com/x/armavita-meta-ads-mcp", "vcs_info": {"vcs": "git", "commit_id": "d406929f00ba1"}}',
     "git d406929"),
    ('{"url": "file:///src", "dir_info": {"editable": true}}', "editable source checkout"),
    ('{"url": "https://example.invalid/x.whl", "archive_info": {}}', ""),
    ('{"vcs_info": {"commit_id": "<script>"}}', ""),
    (None, ""),
    ("not json", ""),
])
def test_version_shows_the_commit_it_was_installed_from(capsys, direct_url, shown):
    class Dist:
        def read_text(self, name):
            assert name == "direct_url.json"
            return direct_url

    with patch("importlib.metadata.distribution", return_value=Dist()), \
            patch.object(sys, "argv", ["armavita-meta-ads-mcp", "--version"]):
        assert mcp_runtime.main() == 0
    out = capsys.readouterr().out.strip()
    assert out == "Armavita Meta Ads MCP v2.0.0" + (f" ({shown})" if shown else "")

