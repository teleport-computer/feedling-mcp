import math
from model_api_runtime.v2 import admission


def test_estimate_uses_default_when_no_history():
    # 2 在飞, 1 worker, 无历史 → ceil(2/1)*20 = 40
    assert admission.estimate_wait_sec(
        inflight=2, workers=1, mean_service_sec=None, default_service_sec=20.0
    ) == 40.0


def test_estimate_uses_rolling_mean_when_present():
    # 4 在飞, 2 worker, 均服务 15 → ceil(4/2)*15 = 30
    assert admission.estimate_wait_sec(
        inflight=4, workers=2, mean_service_sec=15.0, default_service_sec=20.0
    ) == 30.0


def test_estimate_ceils_partial_batch():
    # 3 在飞, 2 worker → ceil(3/2)=2 批 → 2*10 = 20
    assert admission.estimate_wait_sec(
        inflight=3, workers=2, mean_service_sec=10.0, default_service_sec=20.0
    ) == 20.0


def test_estimate_zero_inflight_is_zero_wait():
    assert admission.estimate_wait_sec(
        inflight=0, workers=1, mean_service_sec=15.0, default_service_sec=20.0
    ) == 0.0


def test_estimate_zero_or_negative_workers_never_divides_by_zero():
    # 防御：workers<=0 → 返回 0（等价放行，交给上游存活闸）
    assert admission.estimate_wait_sec(
        inflight=5, workers=0, mean_service_sec=15.0, default_service_sec=20.0
    ) == 0.0


def test_should_admit_boundary_equal_sla_admits():
    assert admission.should_admit(60.0, sla_sec=60.0) is True


def test_should_admit_over_sla_rejects():
    assert admission.should_admit(60.1, sla_sec=60.0) is False


def test_should_admit_under_sla_admits():
    assert admission.should_admit(0.0, sla_sec=60.0) is True


def test_module_constants_have_documented_defaults(monkeypatch):
    import importlib
    monkeypatch.delenv("V2_ADMISSION_SLA_SEC", raising=False)
    monkeypatch.delenv("V2_ADMISSION_DEFAULT_SERVICE_SEC", raising=False)
    monkeypatch.delenv("V2_ADMISSION_SAMPLE_N", raising=False)
    mod = importlib.reload(admission)
    assert mod.SLA_SEC == 60.0
    assert mod.DEFAULT_SERVICE_SEC == 20.0
    assert mod.SERVICE_SAMPLE_N == 50
