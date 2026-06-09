"""Property test for structured log fields.

**Property 19: Structured Log Fields**
**Validates: Requirements 10.2**
"""
from __future__ import annotations
import json
import os
import sys
from io import StringIO

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

from hypothesis import given, settings, assume
from hypothesis import strategies as st
from shared.logger import StructuredLogger

RESERVED_KEYS = {"job_id", "stage", "outcome", "level"}

log_levels = st.sampled_from(["info", "error", "warning"])
printable_text = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd", "Pc", "Pd")),
    min_size=1, max_size=50,
)
safe_key = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20).filter(
    lambda k: k not in RESERVED_KEYS
)
extra_kwargs = st.dictionaries(
    keys=safe_key,
    values=st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
    max_size=5,
)

@given(stage=printable_text, job_id=printable_text, outcome=printable_text, level=log_levels, kwargs=extra_kwargs)
@settings(max_examples=200)
def test_structured_log_always_contains_required_fields(stage, job_id, outcome, level, kwargs):
    """**Property 19: Structured Log Fields** -- **Validates: Requirements 10.2**"""
    logger = StructuredLogger(stage)
    captured = StringIO()
    original_stdout = sys.stdout
    sys.stdout = captured
    try:
        getattr(logger, level)(job_id=job_id, stage=stage, outcome=outcome, **kwargs)
    finally:
        sys.stdout = original_stdout
    output = captured.getvalue().strip()
    assert output
    record = json.loads(output)
    assert "job_id" in record
    assert "stage" in record
    assert "outcome" in record
    assert "level" in record
    assert record["job_id"] == job_id
    assert record["stage"] == stage
    assert record["outcome"] == outcome
    for key, value in kwargs.items():
        assert key in record
        assert record[key] == value

@given(stage=printable_text, job_id=printable_text, outcome=printable_text)
@settings(max_examples=100)
def test_log_output_is_valid_json(stage, job_id, outcome):
    """Every log entry must be valid JSON."""
    logger = StructuredLogger(stage)
    captured = StringIO()
    original_stdout = sys.stdout
    sys.stdout = captured
    try:
        logger.info(job_id=job_id, stage=stage, outcome=outcome)
    finally:
        sys.stdout = original_stdout
    record = json.loads(captured.getvalue().strip())
    assert isinstance(record, dict)
