"""Pure iLink helpers extracted from the pinned MIT CowAgent source.

Copyright (c) 2022 zhayujie. Adaptations: bridge contributors, 2026.
Source: zhayujie/CowAgent@19e84350d6c5459133fc7d0c573d1b6600e55ff7
See licenses/CowAgent-MIT.txt and docs/COWAGENT-PROVENANCE.md.

This module performs no I/O, imports no CowAgent engine, stores no credentials,
and does not start polling, resend messages or change the existing transport.
The default split length is the audited upstream policy, not an independently
verified official platform limit. Chunking is never permission to send extra
messages; a caller must journal and authorize each outgoing item.
"""

UPSTREAM_REPOSITORY = "https://github.com/zhayujie/CowAgent"
UPSTREAM_COMMIT = "19e84350d6c5459133fc7d0c573d1b6600e55ff7"
UPSTREAM_TEXT_CHUNK_LIMIT = 4000

# MessageItemType values read from the pinned WeixinMessage implementation.
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5


def split_text(text: str, limit: int = UPSTREAM_TEXT_CHUNK_LIMIT) -> list[str]:
    """Split as upstream does, preferring paragraphs and then line boundaries.

    Newline separators at a split boundary are removed, matching the audited
    source. Invalid limits are rejected before the loop; upstream has no guard
    for a zero limit and would fail to advance on a nonempty input.
    """
    if not isinstance(text, str):
        raise TypeError("text_must_be_string")
    if type(limit) is not int or limit <= 0:
        raise ValueError("chunk_limit_must_be_positive_integer")
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def aes_ecb_padded_size(plaintext_size: int) -> int:
    """Calculate upstream AES-128-ECB PKCS#7 size; does not encrypt data."""
    if type(plaintext_size) is not int or plaintext_size < 0:
        raise ValueError("plaintext_size_must_be_nonnegative_integer")
    return ((plaintext_size + 1 + 15) // 16) * 16
