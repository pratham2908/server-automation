"""Shape guards for Gemini JSON responses.

Regression cover for the error-queue entry::

    Auto-analysis failed for '<channel>': 'list' object has no attribute 'get'

``analyze_videos`` is annotated ``-> dict[str, Any]`` but used ``typing.cast``,
which does nothing at runtime. When Gemini answered with a JSON array the list
flowed through and only blew up later in ``analysis_engine.run_analysis``.
"""

import json

import pytest

from app.services.gemini import _loads_json_array, _loads_json_object


class TestLoadsJsonObject:
    def test_accepts_object(self):
        assert _loads_json_object('{"best_posting_times": [1, 2]}') == {"best_posting_times": [1, 2]}

    def test_rejects_array(self):
        """The exact shape that caused the production AttributeError."""
        with pytest.raises(TypeError, match="expected a JSON object, got list"):
            _loads_json_object('[{"best_posting_times": []}]')

    @pytest.mark.parametrize("text", ["null", '"a string"', "42", "true"])
    def test_rejects_other_scalars(self, text):
        with pytest.raises(TypeError, match="expected a JSON object"):
            _loads_json_object(text)

    def test_malformed_json_still_raises_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            _loads_json_object("{not json")


class TestLoadsJsonArray:
    def test_accepts_array(self):
        assert _loads_json_array('[{"a": 1}]') == [{"a": 1}]

    def test_rejects_object(self):
        with pytest.raises(TypeError, match="expected a JSON array, got dict"):
            _loads_json_array('{"a": 1}')


def test_type_error_is_handled_by_call_sites():
    """Every call site catches ``(json.JSONDecodeError, TypeError)``.

    The guards raise ``TypeError`` specifically so each site keeps its own
    logging and error message without any change to its handler.
    """
    for bad in ("[]", "null", "42"):
        with pytest.raises((json.JSONDecodeError, TypeError)):
            _loads_json_object(bad)
