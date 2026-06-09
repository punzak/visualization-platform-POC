"""Optional X-Ray tracing helper. No-ops if aws_xray_sdk is not installed."""
from __future__ import annotations

try:
    from aws_xray_sdk.core import xray_recorder
    XRAY_AVAILABLE = True
except ImportError:
    XRAY_AVAILABLE = False


def begin_subsegment(name: str):
    """Begin an X-Ray subsegment. Returns the subsegment or None if X-Ray unavailable."""
    if XRAY_AVAILABLE:
        return xray_recorder.begin_subsegment(name)
    return None


def end_subsegment() -> None:
    """End the current X-Ray subsegment. No-op if X-Ray unavailable."""
    if XRAY_AVAILABLE:
        try:
            xray_recorder.end_subsegment()
        except Exception:
            pass


def put_annotation(key: str, value) -> None:
    """Add an annotation to the current X-Ray subsegment. No PII or secrets."""
    if XRAY_AVAILABLE:
        try:
            xray_recorder.put_annotation(key, value)
        except Exception:
            pass
