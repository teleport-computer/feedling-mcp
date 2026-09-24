"""Pure V2 count splitting shared by admin readers.

The caller supplies the producer's classifier; this module has no dependency
on the database or runtime. Frozen V2 cells never measured the V1 outcome
columns, so readers classify their terminal failure codes instead.
"""
from collections.abc import Callable, Mapping
from typing import NamedTuple


class V2OutcomeCounts(NamedTuple):
    operational: int
    control: int
    user_unavailable: int
    user_codes: dict[str, int]
    remaining_codes: dict[str, int]


def split_v2_outcomes(
    failed: int, codes: Mapping[str, int], *, classify: Callable[[str], str],
) -> V2OutcomeCounts:
    """Split failed + expired, preserving the daily summary's exact semantics.

    Only explicit control/account codes leave the numerator. Unknown codes,
    generic provider errors, timeouts and failures with no code stay operational.
    ``codes`` contains sanitized identifiers and nonnegative counts, as supplied
    by the rollup readers. Superseded jobs are not part of ``failed``.
    """
    classes = {code: classify(code) for code in codes}
    user_codes = {code: n for code, n in codes.items()
                  if classes[code] == "user_unavailable"}
    control = sum(n for code, n in codes.items() if classes[code] == "control")
    user = sum(user_codes.values())
    remaining = {code: n for code, n in codes.items()
                 if classes[code] != "control" and code not in user_codes}
    return V2OutcomeCounts(max(0, failed - control - user), control, user,
                           user_codes, remaining)
