from pathlib import Path

import pytest

from tools.strict_yaml import load_yaml_strict


ROOT = Path(__file__).resolve().parents[1]
CASES = [
    ("docker-compose.phala.test.yaml", "test-api.feedling.app", "test-enclave.feedling.app"),
    ("docker-compose.phala.pre.yaml", "pre-api.feedling.app", "pre-enclave.feedling.app"),
    ("docker-compose.phala.yaml", "api.feedling.app", "enclave.feedling.app"),
]


@pytest.mark.parametrize(("filename", "api_domain", "enclave_domain"), CASES)
def test_main_cvm_compose_has_internal_enclave_domain_service(filename, api_domain, enclave_domain):
    compose = load_yaml_strict(
        (ROOT / "deploy" / filename).read_text(), source_name=filename
    )
    services = compose["services"]
    direct = services["enclave"]
    domain = services["enclave-domain"]
    direct_env = direct["environment"]
    domain_env = domain["environment"]

    assert direct["image"] == domain["image"]
    assert direct["command"] == domain["command"]
    assert direct["volumes"] == domain["volumes"]
    assert direct_env["FEEDLING_ENCLAVE_PORT"] == "5003"
    assert direct_env["FEEDLING_ENCLAVE_TLS"] == "true"
    assert direct_env["FEEDLING_ENCLAVE_TRANSPORT_MODE"] == "direct_tls"
    assert domain_env["FEEDLING_ENCLAVE_PORT"] == "5004"
    assert domain_env["FEEDLING_ENCLAVE_TLS"] == "false"
    assert domain_env["FEEDLING_ENCLAVE_TRANSPORT_MODE"] == "attested_ingress"
    assert not domain.get("ports")
    assert domain.get("expose") == ["5004"]

    allowed_differences = {
        "FEEDLING_ENCLAVE_PORT",
        "FEEDLING_ENCLAVE_TLS",
        "FEEDLING_ENCLAVE_TRANSPORT_MODE",
    }
    for key in set(direct_env) | set(domain_env):
        if key not in allowed_differences:
            assert direct_env.get(key) == domain_env.get(key), key

    ingress_env = services["ingress"]["environment"]
    domains = ingress_env["DOMAINS"].split()
    routes = ingress_env["ROUTING_MAP"].split()
    assert domains == [api_domain, enclave_domain]
    assert routes == [
        f"{api_domain}=backend:5001",
        f"{enclave_domain}=enclave-domain:5004",
    ]
    assert "enclave-domain" in services["ingress"]["depends_on"]
    assert "5003:5003" in direct["ports"]
