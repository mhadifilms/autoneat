"""Shot-id extraction and filter matching for timeline clips."""

from __future__ import annotations

from typing import Any, List, Sequence


def shot_id_from_name(name: str) -> str:
    """Best-effort short shot id for a timeline clip name.

    ``Shot_0000590.[00001001-00001066].exr`` → ``Shot_0000590``
    ``ep01_054_v002.mov`` → ``ep01_054_v002``
    Falls back to the raw name if no obvious suffix to strip.
    """
    head = name.split(".[", 1)[0]
    head = head.rsplit(".", 1)[0] if "." in head else head
    return head or name


def normalize_shot_id(shot_id: str) -> str:
    """Strip leading zeros (``0000590`` → ``590``)."""
    s = str(shot_id or "").strip().lower()
    if not s:
        return ""
    stripped = s.lstrip("0")
    return stripped or "0"


def shot_id_tokens(name: str) -> List[str]:
    """Candidate tokens for matching a clip name against ``--shot-ids`` filters.

    For ``Shot_0000590`` we yield ``shot_0000590``, ``0000590``, and ``590``.
    """
    head = shot_id_from_name(name)
    tokens: List[str] = [head.lower()]
    for segment in head.replace("-", "_").split("_"):
        seg = segment.strip().lower()
        if seg and seg != head.lower():
            tokens.append(seg)
            normalized = normalize_shot_id(seg)
            if normalized and normalized != seg:
                tokens.append(normalized)
    return tokens


def filter_clips(clips: Sequence[Any], shot_ids: Sequence[str], *, name_of: Any) -> List[Any]:
    """Filter timeline clips by user-supplied shot IDs.

    Numeric needles (e.g. ``590``, ``0000590``) match only as discrete tokens
    or leading-zero-normalized forms — never as raw substrings, otherwise
    frame ranges like ``[00001001-00001066]`` would over-match every clip.

    Non-numeric needles fall back to a case-insensitive substring match
    against the cleaned shot-id head (the part before any ``.[...]`` frame
    range or extension).

    ``name_of`` is a callable that returns the display name for a clip.
    """
    if not shot_ids:
        return list(clips)

    needles = [str(s).strip() for s in shot_ids if str(s).strip()]
    if not needles:
        return list(clips)

    numeric_raw = {n.lower() for n in needles if n.isdigit()}
    numeric_norm = {normalize_shot_id(n) for n in numeric_raw}
    text_needles = {n.lower() for n in needles if not n.isdigit()}

    out: List[Any] = []
    for clip in clips:
        name = name_of(clip)
        tokens = set(shot_id_tokens(name))
        norm_tokens = {normalize_shot_id(t) for t in tokens}
        head_lower = shot_id_from_name(name).lower()

        if tokens & numeric_raw or norm_tokens & numeric_norm:
            out.append(clip)
            continue
        if tokens & text_needles or any(n in head_lower for n in text_needles):
            out.append(clip)
    return out
