from __future__ import annotations

import os

from starlette.testclient import TestClient


TEST_CALLER_TOKEN = "bhm-test-caller-token-0000000000000001"

# Tests must be hermetic: a persisted workstation caller token must never
# change the in-process auth contract or leak into test requests.
os.environ["BHM_CALLER_TOKEN"] = TEST_CALLER_TOKEN
os.environ["BHM_CALLER_ID"] = "pytest"
os.environ["BHM_CALLER_PROJECTS"] = "*"
os.environ["BHM_CALLER_DEFAULT_PROJECT"] = "blackholememory"


_ORIGINAL_TEST_CLIENT_INIT = TestClient.__init__


def _authenticated_test_client_init(self, *args, headers=None, **kwargs):
    default_headers = dict(headers or {})
    default_headers.setdefault("Authorization", f"Bearer {TEST_CALLER_TOKEN}")
    return _ORIGINAL_TEST_CLIENT_INIT(self, *args, headers=default_headers, **kwargs)


TestClient.__init__ = _authenticated_test_client_init
