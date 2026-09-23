import json
from copy import deepcopy

import pytest

from tv_history.mcp_response import mcp_result


@pytest.mark.parametrize("is_error", [False, True])
@pytest.mark.parametrize("image_bytes", [None, b"chart-image"])
def test_public_response_omits_internal_metadata_without_mutating_input(is_error, image_bytes):
    metadata = {
        "timestamp_normalization": {"unverified_rows": 1, "unverified_source_timestamps": ["old"]},
        "daily_timestamp_normalization": {"ambiguous_rows": 1},
        "history_coverage": {"status": "uncertain", "unresolved_gaps": [{"after": "old"}]},
        "daily_history_coverage": {"status": "uncertain"},
    }
    public = {
        "bars": [{"source_timestamp": "original", "timestamp_basis": "four_hour_confirmed",
                  "timestamp_evidence": "evidence", "timestamp_ambiguous": False, "c": 102}],
        "provider_metadata": {"delay_seconds": 600},
        "data_quality": {"status": "uncertain"},
    }
    payload = {**public, **deepcopy(metadata)}
    if is_error:
        public["error"] = {"code": "INSUFFICIENT_HISTORY_COVERAGE", "message": "missing", "retryable": False}
        payload["error"] = {**public["error"], **deepcopy(metadata)}
    original = deepcopy(payload)

    result = mcp_result(payload, image_bytes)

    assert payload == original
    assert result.structuredContent == public
    assert json.loads(result.content[0].text) == public
    assert result.isError is is_error
    assert [item.type for item in result.content] == (["text", "image"] if image_bytes else ["text"])
    if image_bytes:
        import base64
        assert base64.b64decode(result.content[1].data) == image_bytes
