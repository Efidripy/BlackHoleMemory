import httpx
import pytest

from blackholememory import bhm_mcp_base, bhm_mcp_code
from blackholememory.project_registry import canonical_project_id


def test_code_tools_default_to_registry_default_project():
    # "e-github-workspace" has no indexed root, so omitted-root calls ended in 409 repository_root_ambiguous.
    assert bhm_mcp_base.CODE_DEFAULT_PROJECT == canonical_project_id(None)
    assert bhm_mcp_code.bhm_index_status.__defaults__[0] == bhm_mcp_base.CODE_DEFAULT_PROJECT


def test_client_error_detail_reaches_the_caller():
    response = httpx.Response(409, json={"detail": {"error": "repository_root_ambiguous"}}, request=httpx.Request("POST", "http://x/bhm/code-tools"))
    with pytest.raises(RuntimeError, match="409.*repository_root_ambiguous"):
        bhm_mcp_base._raise_for_status_with_detail(response)
