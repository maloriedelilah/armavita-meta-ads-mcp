# SPDX-License-Identifier: AGPL-3.0-only

"""Keep the test suite out of the developer's real home folder (ADS-63).

The server keeps server.log and the Meta token cache under the user's config folder
(~/.config, ~/Library/Application Support or %APPDATA%). The paths are resolved when the
package is imported, so HOME (and APPDATA) are pointed at a throwaway folder here, before
any test module imports the package. A test that logs, or that clears a cached token on a
190, then touches only that folder.
"""

import atexit
import os
import shutil
import tempfile

_FAKE_HOME = tempfile.mkdtemp(prefix="armavita-tests-home-")
os.environ["HOME"] = _FAKE_HOME
os.environ["USERPROFILE"] = _FAKE_HOME
os.environ["APPDATA"] = os.path.join(_FAKE_HOME, "AppData", "Roaming")
os.environ.pop("XDG_CONFIG_HOME", None)
atexit.register(shutil.rmtree, _FAKE_HOME, ignore_errors=True)


def pytest_report_header(config):
    return f"HOME for this run: {_FAKE_HOME}"
