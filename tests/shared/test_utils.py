"""Unit tests for shared utilities."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src")))

import pytest
from shared.utils import parse_s3_key, validate_uuid, now_iso


def test_parse_s3_key_returns_job_id_and_filename():
    key = "property_photos/550e8400-e29b-41d4-a716-446655440000/front.jpg"
    job_id, filename = parse_s3_key(key)
    assert job_id == "550e8400-e29b-41d4-a716-446655440000"
    assert filename == "front.jpg"


def test_parse_s3_key_uppercase_uuid():
    key = "property_photos/550E8400-E29B-41D4-A716-446655440000/image.png"
    job_id, filename = parse_s3_key(key)
    assert job_id.lower() == "550e8400-e29b-41d4-a716-446655440000"
    assert filename == "image.png"


def test_parse_s3_key_filename_with_spaces_and_dots():
    key = "property_photos/550e8400-e29b-41d4-a716-446655440000/my photo.jpeg"
    job_id, filename = parse_s3_key(key)
    assert filename == "my photo.jpeg"


def test_parse_s3_key_filename_with_extra_slashes_becomes_part_of_filename():
    key = "property_photos/550e8400-e29b-41d4-a716-446655440000/subdir/file.jpg"
    job_id, filename = parse_s3_key(key)
    assert filename == "subdir/file.jpg"


def test_parse_s3_key_raises_for_empty_string():
    with pytest.raises(ValueError):
        parse_s3_key("")


def test_parse_s3_key_raises_for_missing_prefix():
    with pytest.raises(ValueError):
        parse_s3_key("550e8400-e29b-41d4-a716-446655440000/front.jpg")


def test_parse_s3_key_raises_for_wrong_prefix():
    with pytest.raises(ValueError):
        parse_s3_key("uploads/550e8400-e29b-41d4-a716-446655440000/front.jpg")


def test_parse_s3_key_raises_for_missing_filename():
    with pytest.raises(ValueError):
        parse_s3_key("property_photos/550e8400-e29b-41d4-a716-446655440000/")


def test_parse_s3_key_raises_for_non_uuid_job_id():
    with pytest.raises(ValueError):
        parse_s3_key("property_photos/not-a-uuid/front.jpg")


def test_parse_s3_key_raises_for_uuid_v1():
    with pytest.raises(ValueError):
        parse_s3_key("property_photos/550e8400-e29b-11d4-a716-446655440000/front.jpg")


def test_parse_s3_key_raises_for_no_slashes():
    with pytest.raises(ValueError):
        parse_s3_key("property_photos")


def test_parse_s3_key_raises_for_only_prefix_and_uuid():
    with pytest.raises(ValueError):
        parse_s3_key("property_photos/550e8400-e29b-41d4-a716-446655440000")


def test_validate_uuid_returns_true_for_valid_v4():
    assert validate_uuid("550e8400-e29b-41d4-a716-446655440000") is True


def test_validate_uuid_returns_false_for_non_uuid():
    assert validate_uuid("not-a-uuid") is False


def test_validate_uuid_returns_false_for_non_string():
    assert validate_uuid(12345) is False


def test_validate_uuid_returns_false_for_uuid_v1():
    assert validate_uuid("550e8400-e29b-11d4-a716-446655440000") is False


def test_now_iso_returns_string():
    result = now_iso()
    assert isinstance(result, str)


def test_now_iso_contains_utc_offset():
    result = now_iso()
    assert "+00:00" in result or result.endswith("Z")
