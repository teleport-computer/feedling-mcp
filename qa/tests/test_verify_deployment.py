from __future__ import annotations

import json
import stat

import pytest

from qa import verify_deployment as deployment


SHA = "a" * 40
ENV = {
    "QA_FEEDLING_BASE_URL": "https://test-api.feedling.app",
    "QA_TEST_ADMIN_TOKEN": "test-admin-token",
}


class FakeAdmin:
    def __init__(self, status=200, identity=None):
        self.status = status
        self.identity = (
            {
                "schema_version": 1,
                "environment": "test",
                "backend_sha": SHA,
                "deployment_sha": SHA,
                "identity_verified": True,
            }
            if identity is None
            else identity
        )
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        assert path == "/v1/admin/qa/build-identity"
        return self.status, self.identity


def test_strict_v2_receipt_defers_runtime_proof_to_profile_evidence(tmp_path):
    receipt_path = tmp_path / "deployment.json"
    fake = FakeAdmin()
    receipt = deployment.verify_deployment(
        SHA, receipt_path, env=ENV, admin_client=fake
    )
    assert fake.calls == [("GET", "/v1/admin/qa/build-identity", None)]
    assert receipt["observed_backend_sha"] == SHA
    assert receipt["observed_worker_sha"] is None
    assert json.loads(receipt_path.read_text())["live_worker_count"] is None
    assert receipt["runtime_evidence_source"] == (
        "per_profile_runtime_readback_and_live_scenarios"
    )
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o400


def test_baseline_receipt_proves_authoritative_backend_build_identity(tmp_path):
    receipt_path = tmp_path / "deployment.json"
    fake = FakeAdmin()

    receipt = deployment.verify_deployment(
        SHA,
        receipt_path,
        env=ENV,
        admin_client=fake,
        expected_runtime=deployment.BASELINE_RUNTIME,
    )

    assert fake.calls == [("GET", "/v1/admin/qa/build-identity", None)]
    assert receipt["expected_runtime"] == "deployed_current"
    assert receipt["liveness_verified"] is True
    assert receipt["deployment_identity_verified"] is True
    assert receipt["observed_backend_sha"] == SHA
    assert receipt["observed_deployment_sha"] == SHA
    assert receipt["observed_worker_sha"] is None
    assert receipt["live_worker_count"] is None
    assert receipt["runtime_evidence_source"] == "deployed_runtime_readback"


def test_local_discovery_uses_the_authoritative_backend_sha(tmp_path):
    receipt = deployment.verify_deployment(
        None,
        tmp_path / "deployment.json",
        env=ENV,
        admin_client=FakeAdmin(),
        expected_runtime=deployment.BASELINE_RUNTIME,
    )

    assert receipt["expected_deployment_sha"] == SHA
    assert receipt["observed_backend_sha"] == SHA


@pytest.mark.parametrize(
    ("identity", "message"),
    (
        ({}, "invalid"),
        (
            {
                "schema_version": 1,
                "environment": "test",
                "backend_sha": SHA,
                "deployment_sha": "b" * 40,
                "identity_verified": True,
            },
            "invalid",
        ),
        (
            {
                "schema_version": 1,
                "environment": "test",
                "backend_sha": "b" * 40,
                "deployment_sha": "b" * 40,
                "identity_verified": True,
            },
            "does not match",
        ),
    ),
)
def test_test_build_identity_must_be_valid_and_match_candidate(
    tmp_path, identity, message
):
    with pytest.raises(deployment.DeploymentVerificationError, match=message):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env=ENV,
            admin_client=FakeAdmin(identity=identity),
            expected_runtime=deployment.BASELINE_RUNTIME,
        )


def test_unavailable_endpoint_and_missing_inputs_fail_closed(tmp_path):
    with pytest.raises(deployment.DeploymentVerificationError, match="unavailable"):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env=ENV,
            admin_client=FakeAdmin(status=404),
        )
    with pytest.raises(
        deployment.DeploymentVerificationError, match="QA_TEST_ADMIN_TOKEN"
    ):
        deployment.verify_deployment(
            SHA,
            tmp_path / "deployment.json",
            env={"QA_FEEDLING_BASE_URL": ENV["QA_FEEDLING_BASE_URL"]},
            admin_client=FakeAdmin(),
        )
