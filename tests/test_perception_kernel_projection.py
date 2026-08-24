"""投影与一瞥搬进内核后，io 壳与内核必须是同一批对象；一瞥必须仍然只出 bool。"""
from __future__ import annotations

import pathlib
import sys

# Self-contained sys.path bootstrap (mirrors tests/test_perception_kernel_catalog.py):
# conftest.py only adds backend/ to sys.path inside its DB-provisioning try-block,
# so on a no-Postgres machine this file must add backend/ itself.
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import perception.agent_fields as io_fields
import perception.glance as io_glance
import perception.permissions as io_permissions
import perception_kernel.fields as kernel_fields
import perception_kernel.glance as kernel_glance


def test_io_shells_reexport_kernel_objects():
    assert io_fields.project_signal is kernel_fields.project_signal
    assert io_fields.AGENT_PERCEPTION_SIGNALS is kernel_fields.AGENT_PERCEPTION_SIGNALS
    assert io_permissions.permission_states_reason is kernel_fields.permission_states_reason
    assert io_glance.build_perception_glance is kernel_glance.build_perception_glance


def test_glance_emits_only_booleans():
    # 这是设计里「坚决不给配」的第一条：一瞥永远不出数值。
    out = kernel_glance.build_perception_glance(
        {
            "location": {"place_label": {"v": "office"}},
            "sleep": {"asleep_minutes": {"v": 312}},
        },
        notable_changes=[{"signal": "health_sleep", "field": "asleep_minutes"}],
    )
    for group in out.values():
        for value in group.values():
            assert isinstance(value, bool), out


def test_glance_of_empty_input_is_still_a_dict():
    assert isinstance(kernel_glance.build_perception_glance({}), dict)
