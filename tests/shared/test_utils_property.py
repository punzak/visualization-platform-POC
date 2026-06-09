"""Property-based tests for shared utilities.

**Validates: Requirements 1.4, 11.1**
"""
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.utils import parse_s3_key


_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_hex4 = st.text(alphabet="0123456789abcdef", min_size=4, max_size=4)
_hex8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)
_hex12 = st.text(alphabet="0123456789abcdef", min_size=12, max_size=12)
_variant_char = st.sampled_from(list("89ab"))

valid_uuid_v4 = st.builds(
    lambda h8, h4a, h4b_suffix, variant, h4c, h12: (
        f"{h8}-{h4a}-4{h4b_suffix}-{variant}{h4c}-{h12}"
    ),
    h8=_hex8,
    h4a=_hex4,
    h4b_suffix=st.text(alphabet="0123456789abcdef", min_size=3, max_size=3),
    variant=_variant_char,
    h4c=st.text(alphabet="0123456789abcdef", min_size=3, max_size=3),
    h12=_hex12,
)

valid_filename = st.text(
    alphabet=st.characters(blacklist_characters="\x00"),
    min_size=1,
    max_size=64,
).filter(lambda s: len(s.strip()) > 0)

valid_s3_key = st.builds(
    lambda uid, fn: f"property_photos/{uid}/{fn}",
    uid=valid_uuid_v4,
    fn=valid_filename,
)


def _looks_valid(k: str) -> bool:
    parts = k.split("/")
    if len(parts) < 3:
        return False
    if parts[0] != "property_photos":
        return False
    if not parts[2]:
        return False
    return bool(_UUID_V4_RE.match(parts[1]))


@given(key=st.text(min_size=0, max_size=200).filter(lambda k: not _looks_valid(k)))
@settings(max_examples=500)
def test_invalid_key_always_raises_value_error(key):
    """Property 8: Any key not matching the expected pattern raises ValueError.

    **Validates: Requirements 1.4, 11.1**
    """
    with pytest.raises(ValueError):
        parse_s3_key(key)


@given(key=valid_s3_key)
@settings(max_examples=300)
def test_valid_key_never_raises(key):
    """Valid keys matching property_photos/{uuid-v4}/{filename} never raise.

    **Validates: Requirements 1.4, 11.1**
    """
    job_id, filename = parse_s3_key(key)
    assert job_id
    assert filename
