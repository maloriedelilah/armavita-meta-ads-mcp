# SPDX-License-Identifier: AGPL-3.0-only

"""ADS-63: logging hygiene, sanitizer gaps, test isolation and annotations."""

import logging
import os
import pathlib
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from armavita_meta_ads_mcp.core import graph_client, log_redaction, media_helpers
from armavita_meta_ads_mcp.core.mcp_runtime import _import_tool_modules, mcp_server

_import_tool_modules()

TOKEN = "EAAB" + "q" * 40


def test_download_urls_are_logged_without_credentials_or_query():
    url = "https://user:secret@scontent.xx.fbcdn.net/v/t45/123.jpg?oh=signed&oe=abc&access_token=" + TOKEN
    shown = media_helpers.loggable_url(url)
    assert shown == "https://scontent.xx.fbcdn.net/v/t45/123.jpg"
    assert "secret" not in shown and "signed" not in shown and TOKEN not in shown


def test_an_encoded_token_inside_another_parameter_is_redacted():
    text = "https://example.com/cb?next=https%3A%2F%2Fgraph.facebook.com%2Fme%3Faccess_token%3D" + TOKEN[:30] + "abc"
    assert TOKEN[:30] not in log_redaction.redact(text)
    assert TOKEN[:30] not in graph_client._sanitize_url(text)


def test_a_token_in_plain_text_is_redacted_by_the_url_sanitizer():
    message = f"connection reset while sending access_token={TOKEN} to graph"
    cleaned = graph_client._sanitize_url(message)
    assert TOKEN not in cleaned and "connection reset" in cleaned


def test_the_log_file_rotates_and_every_file_stays_owner_only(tmp_path):
    handler = media_helpers._PrivateFileHandler(tmp_path / "server.log", maxBytes=200, backupCount=2, delay=True)
    log = logging.getLogger("ads63-rotation")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        for i in range(30):
            log.info("line %03d %s", i, "x" * 40)
    finally:
        log.removeHandler(handler)
        handler.close()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["server.log", "server.log.1", "server.log.2"]
    for name in files:
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600


def test_research_logs_dont_carry_the_query_or_record_ids():
    import re

    source = (Path(__file__).resolve().parents[1] / "src/armavita_meta_ads_mcp/core/research_tools.py").read_text()
    log_calls = re.findall(r"logger\.\w+\((.*)\)", source)
    assert log_calls
    for call in log_calls:
        assert not re.search(r"\b(query|record_id|resource_id)\b", call), call


def test_the_suite_runs_in_a_throwaway_home():
    assert "armavita-tests-home-" in str(pathlib.Path.home())
    assert str(media_helpers._resolve_log_file()).startswith(str(pathlib.Path.home()))


def test_create_report_is_marked_read_only():
    tool = mcp_server._tool_manager.get_tool("create_report")
    assert tool.annotations.readOnlyHint is True


def test_requests_floor_is_past_the_netrc_leak():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"requests>=2.32.4"' in pyproject
