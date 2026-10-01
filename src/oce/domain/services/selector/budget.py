"""Fit an oversized leading answer within the code character budget."""

from __future__ import annotations

from dataclasses import replace

from oce.domain.services.search import SearchHit


def fit_leading_hit(hit: SearchHit, max_chars: int) -> SearchHit:
    """Keep complete source lines when possible; a long first line is clipped."""
    if len(hit.content) <= max_chars:
        return hit
    content = hit.content[:max_chars]
    if not hit.content[max_chars:].startswith(("\n", "\r\n")):
        boundary = content.rfind("\n")
        if boundary > 0:
            content = content[:boundary]
    content = content.removesuffix("\r")
    return replace(
        hit,
        content=content,
        end_line=hit.start_line + max(1, len(content.splitlines())) - 1,
    )
