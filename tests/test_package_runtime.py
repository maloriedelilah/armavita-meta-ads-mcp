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
    assert armavita_meta_ads_mcp.__version__ == "1.2.0"
    assert callable(armavita_meta_ads_mcp.entrypoint)


def test_runtime_rejects_unknown_transport(capsys):
    # stdio and streamable-http are the only transports; anything else is a usage error.
    with patch.object(sys, "argv", ["armavita-meta-ads-mcp", "--transport", "sse"]):
        with pytest.raises(SystemExit) as exc:
            mcp_runtime.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_runtime_streamable_http_dispatches_to_http_runner():
    argv = ["armavita-meta-ads-mcp", "--transport", "streamable-http", "--port", "9999", "--allowed-host", "mcp.example.com"]
    with patch.object(sys, "argv", argv), patch.object(mcp_runtime, "_run_streamable_http", return_value=0) as runner:
        assert mcp_runtime.main() == 0

    runner.assert_called_once_with("127.0.0.1", 9999, ["mcp.example.com"], "/mcp")


def test_runtime_version_flag(capsys):
    with patch.object(sys, "argv", ["armavita-meta-ads-mcp", "--version"]):
        exit_code = mcp_runtime.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Armavita Meta Ads MCP v1.2.0" in captured.out
