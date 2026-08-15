"""Immutable segmented conversation-summary frontier for Hosted Runtime V2.

Raw ``chat_messages`` remain the durable source ledger.  A level-0 segment is
an encrypted, append-only summary of one exact contiguous source-message
batch.  A higher-level checkpoint is an encrypted, append-only summary of an
exact ordered list of child segments.  Checkpoints never mutate or delete
their children; they only replace those children in the *model-facing
canonical cover*.

This module is deliberately pure.  It contains no database, envelope, worker,
or provider code.  Persisted metadata can therefore be validated before any
plaintext is assembled, and roll-up policy can be tested without a running
service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


SUMMARY_FORMAT_VERSION = 1
DEFAULT_ROLLUP_FANOUT = 8
DEFAULT_MAX_FRONTIER_SEGMENTS = 24
DEFAULT_MAX_FRONTIER_CHARS = 48_000


class SummaryFrontierIntegrityError(RuntimeError):
    """The persisted canonical cover cannot prove complete source coverage."""

    code = "v2_summary_frontier_integrity_error"

    def __init__(self, detail: str) -> None:
        self.detail = str(detail)
        super().__init__(self.code)


class SummaryFrontierExhausted(RuntimeError):
    """The immutable frontier needs a roll-up but no safe batch can be formed."""

    code = "v2_summary_frontier_exhausted"

    def __init__(self, detail: str) -> None:
        self.detail = str(detail)
        super().__init__(self.code)


@dataclass(frozen=True)
class SummarySegment:
    """One decrypted segment plus content-free persisted provenance."""

    segment_id: int
    coverage_kind: str
    level: int
    start_seq: int
    end_seq: int
    source_message_count: int
    legacy_opaque_through_seq: int
    child_segment_ids: tuple[int, ...]
    text: str

    def __post_init__(self) -> None:
        if int(self.segment_id) <= 0:
            raise SummaryFrontierIntegrityError("invalid_segment_id")
        if int(self.level) < 0:
            raise SummaryFrontierIntegrityError("invalid_level")
        coverage_kind = str(self.coverage_kind)
        opaque_through = int(self.legacy_opaque_through_seq)
        if coverage_kind == "exact":
            if int(self.start_seq) <= 0 or int(self.end_seq) < int(self.start_seq):
                raise SummaryFrontierIntegrityError("invalid_exact_seq_range")
            if int(self.source_message_count) <= 0 or opaque_through != 0:
                raise SummaryFrontierIntegrityError("invalid_exact_source_witness")
        elif coverage_kind == "legacy_opaque":
            if (
                int(self.start_seq) != 0
                or opaque_through < 0
                or int(self.end_seq) < opaque_through
                or int(self.source_message_count) < 0
            ):
                raise SummaryFrontierIntegrityError("invalid_legacy_coverage")
        else:
            raise SummaryFrontierIntegrityError("unknown_coverage_kind")
        if not str(self.text or "").strip():
            raise SummaryFrontierIntegrityError("empty_segment_text")
        children = tuple(int(value) for value in self.child_segment_ids)
        if len(children) != len(set(children)):
            raise SummaryFrontierIntegrityError("duplicate_child_segment")
        if self.level == 0 and children:
            raise SummaryFrontierIntegrityError("leaf_has_children")
        if self.level > 0 and not children:
            raise SummaryFrontierIntegrityError("checkpoint_has_no_children")


@dataclass(frozen=True)
class RollupCandidate:
    """An ordered canonical prefix/run that may become one immutable parent."""

    children: tuple[SummarySegment, ...]
    parent_level: int
    reason: str

    @property
    def child_segment_ids(self) -> tuple[int, ...]:
        return tuple(item.segment_id for item in self.children)

    @property
    def start_seq(self) -> int:
        return self.children[0].start_seq

    @property
    def end_seq(self) -> int:
        return self.children[-1].end_seq

    @property
    def source_message_count(self) -> int:
        return sum(item.source_message_count for item in self.children)

    @property
    def coverage_kind(self) -> str:
        return (
            "legacy_opaque"
            if any(item.coverage_kind == "legacy_opaque" for item in self.children)
            else "exact"
        )

    @property
    def legacy_opaque_through_seq(self) -> int:
        return max(item.legacy_opaque_through_seq for item in self.children)


@dataclass(frozen=True)
class SummaryFrontierSnapshot:
    """One head-version-pinned canonical cover used for checkpoint CAS."""

    segments: tuple[SummarySegment, ...]
    head_version: int
    watermark_seq: int

    def __post_init__(self) -> None:
        if int(self.head_version) <= 0 or int(self.watermark_seq) < 0:
            raise SummaryFrontierIntegrityError("invalid_frontier_snapshot")
        if not self.segments or self.segments[-1].end_seq != self.watermark_seq:
            raise SummaryFrontierIntegrityError("snapshot_watermark_mismatch")


def segment_chars(segment: SummarySegment) -> int:
    """Conservative rendered size including one separator."""

    return len(segment.text) + 2


def frontier_chars(frontier: Sequence[SummarySegment]) -> int:
    return sum(segment_chars(item) for item in frontier)


def validate_canonical_frontier(
    frontier: Sequence[SummarySegment],
    *,
    watermark_seq: int,
    first_source_seq: int,
    covered_source_count: int,
) -> tuple[SummarySegment, ...]:
    """Prove that one non-overlapping canonical cover reaches the watermark.

    ``chat_messages.seq`` is global, so adjacent rows for one user need not be
    numerically consecutive.  Exact coverage is proved by all three witnesses:
    the first/last source seq, strict non-overlap/order, and the sum of exact
    source-row counts carried through every checkpoint.
    """

    ordered = tuple(frontier)
    watermark = int(watermark_seq)
    first = int(first_source_seq)
    count = int(covered_source_count)
    if watermark == 0:
        if not ordered:
            if first != 0 or count != 0:
                raise SummaryFrontierIntegrityError("zero_watermark_has_source")
            return ordered
        if not (
            len(ordered) == 1
            and ordered[0].coverage_kind == "legacy_opaque"
            and ordered[0].start_seq == 0
            and ordered[0].end_seq == 0
            and ordered[0].legacy_opaque_through_seq == 0
            and ordered[0].source_message_count == 0
            and first == 0
            and count == 0
        ):
            raise SummaryFrontierIntegrityError("invalid_zero_watermark_legacy")
        return ordered
    if not ordered:
        raise SummaryFrontierIntegrityError("missing_canonical_frontier")
    opaque = [item for item in ordered if item.coverage_kind == "legacy_opaque"]
    if len(opaque) > 1 or (opaque and ordered[0] is not opaque[0]):
        raise SummaryFrontierIntegrityError("invalid_legacy_frontier_position")
    opaque_through = opaque[0].legacy_opaque_through_seq if opaque else 0
    if opaque:
        if opaque[0].start_seq != 0:
            raise SummaryFrontierIntegrityError("legacy_frontier_start_mismatch")
        if count < 0 or (count == 0 and first != 0) or (count > 0 and first <= opaque_through):
            raise SummaryFrontierIntegrityError("invalid_post_legacy_source_witness")
    else:
        if first <= 0 or count <= 0:
            raise SummaryFrontierIntegrityError("missing_source_witness")
        if ordered[0].start_seq != first:
            raise SummaryFrontierIntegrityError("frontier_start_mismatch")
    if ordered[-1].end_seq != watermark:
        raise SummaryFrontierIntegrityError("frontier_end_mismatch")
    seen_ids: set[int] = set()
    previous: SummarySegment | None = None
    for item in ordered:
        if item.segment_id in seen_ids:
            raise SummaryFrontierIntegrityError("duplicate_canonical_segment")
        seen_ids.add(item.segment_id)
        if (
            previous is not None
            and item.coverage_kind != "legacy_opaque"
            and previous.end_seq >= item.start_seq
        ):
            raise SummaryFrontierIntegrityError("overlapping_canonical_segments")
        previous = item
    # A deployed legacy blob can cover source rows that an older retention
    # policy already removed.  Its immutable envelope remains the only honest
    # provenance, while every post-cutover row has an exact count witness.
    if sum(item.source_message_count for item in ordered) != count:
        raise SummaryFrontierIntegrityError("source_count_mismatch")
    return ordered


def render_replacement(
    frontier: Sequence[SummarySegment],
    *,
    child_segment_ids: Sequence[int],
    parent_text: str,
) -> str:
    """Render the materialized prompt view after replacing one exact run."""

    child_ids = tuple(int(value) for value in child_segment_ids)
    items = tuple(frontier)
    indexes = [
        index for index, item in enumerate(items) if item.segment_id in child_ids
    ]
    if (
        not child_ids
        or len(indexes) != len(child_ids)
        or tuple(items[index].segment_id for index in indexes) != child_ids
        or indexes != list(range(indexes[0], indexes[-1] + 1))
        or not str(parent_text or "").strip()
    ):
        raise SummaryFrontierIntegrityError("invalid_materialized_replacement")
    rendered = [item.text.strip() for item in items[: indexes[0]]]
    rendered.append(str(parent_text).strip())
    rendered.extend(item.text.strip() for item in items[indexes[-1] + 1 :])
    return "\n\n--- historical summary checkpoint ---\n\n".join(rendered)


def _candidate(
    children: Sequence[SummarySegment], *, reason: str
) -> RollupCandidate:
    selected = tuple(children)
    if not selected:
        raise SummaryFrontierExhausted("empty_rollup_candidate")
    return RollupCandidate(
        children=selected,
        parent_level=max(item.level for item in selected) + 1,
        reason=reason,
    )


def choose_rollup_candidate(
    frontier: Sequence[SummarySegment],
    *,
    fanout: int = DEFAULT_ROLLUP_FANOUT,
    max_frontier_segments: int = DEFAULT_MAX_FRONTIER_SEGMENTS,
    max_frontier_chars: int = DEFAULT_MAX_FRONTIER_CHARS,
) -> RollupCandidate | None:
    """Choose a deterministic immutable roll-up, or ``None`` when bounded.

    Normal base-``fanout`` runs preserve a long stable prefix: a parent is
    created only when a complete same-level run closes.  The hard count/char
    frontier is an admission ceiling, not a truncation policy; if it is crossed,
    the oldest safe prefix is checkpointed even when levels differ.  A legacy
    pre-segmentation blob may be a single oversized node, so a unary checkpoint
    is permitted only for that size-reduction case.
    """

    items = tuple(frontier)
    if not items:
        return None
    fanout = int(fanout)
    max_segments = int(max_frontier_segments)
    max_chars = int(max_frontier_chars)
    if fanout < 2 or max_segments < 1 or max_chars < 1:
        raise ValueError("summary frontier limits must be positive")

    # One inherited legacy blob can already exceed the desired frontier before
    # segmented storage lands.  Checkpoint it without deleting the original.
    # Parent text is derived from metadata, so inherited child prose size does
    # not constrain the roll-up operation.
    for item in items:
        if segment_chars(item) > max_chars:
            return _candidate((item,), reason="oversized_legacy_segment")

    # Standard hierarchical carry: oldest complete same-level run first.
    run_start = 0
    while run_start < len(items):
        run_end = run_start + 1
        while run_end < len(items) and items[run_end].level == items[run_start].level:
            run_end += 1
        if run_end - run_start >= fanout:
            selected = items[run_start : run_start + fanout]
            return _candidate(selected, reason="fanout")
        run_start = run_end

    if len(items) <= max_segments and frontier_chars(items) <= max_chars:
        return None

    # Emergency bound: metadata-only roll-up can always take the oldest
    # structural prefix without rendering child text into a provider request.
    selected = list(items[: min(fanout, len(items))])
    if len(selected) < 2:
        raise SummaryFrontierExhausted("no_reducing_rollup_candidate")
    return _candidate(selected, reason="frontier_bound")
