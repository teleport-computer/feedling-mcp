import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import effect_id

def test_same_inputs_same_id():
    assert effect_id.derive(job_id=7, effect_type="reply", ordinal=0) == \
           effect_id.derive(job_id=7, effect_type="reply", ordinal=0)

def test_different_effect_different_id():
    a = effect_id.derive(job_id=7, effect_type="reply", ordinal=0)
    b = effect_id.derive(job_id=7, effect_type="status", ordinal=0)
    c = effect_id.derive(job_id=7, effect_type="reply", ordinal=1)
    d = effect_id.derive(job_id=8, effect_type="reply", ordinal=0)
    assert len({a, b, c, d}) == 4

def test_control_effect_id_no_job():
    x = effect_id.derive_control(generation=5, effect_type="cursor", key="s42")
    assert x == effect_id.derive_control(generation=5, effect_type="cursor", key="s42")
    assert x != effect_id.derive_control(generation=6, effect_type="cursor", key="s42")

def test_id_is_stable_string_shape():
    # No randomness, no timestamps: pure function of inputs.
    assert effect_id.derive(job_id=1, effect_type="memory", ordinal=2) == "job1:memory:2"
    assert effect_id.derive_control(generation=3, effect_type="schedule", key="wk") == "gen3:schedule:wk"
