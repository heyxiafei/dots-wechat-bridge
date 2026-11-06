"""Canonical public notification formatting, shared with the bridge."""

from probe.aihot_notice_consumer.text_b import (
    RICH_STYLE, UNAVAILABLE_STYLE, build_display, evidence_signature, format_b,
    format_rich, legacy_display, validate_display,
)

__all__ = ['RICH_STYLE', 'UNAVAILABLE_STYLE', 'build_display',
           'evidence_signature', 'format_b', 'format_rich', 'legacy_display',
           'validate_display']
