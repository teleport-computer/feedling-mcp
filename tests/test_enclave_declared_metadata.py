"""Offline contract/config regressions; no KMS or chain verification implied."""
import json
import os
from pathlib import Path
import runpy

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = '0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5'
URL = 'https://github.com/teleport-computer/feedling-mcp/raw/test/deploy/docker-compose.phala.test.yaml'


def load(monkeypatch, values):
    for key in list(os.environ):
        if key.startswith('FEEDLING_') or key == 'DSTACK_SIMULATOR_ENDPOINT':
            monkeypatch.delenv(key)
    for key, value in values.items():
        monkeypatch.setenv(key, str(value))
    return runpy.run_path(str(ROOT / 'backend/enclave/config.py'))


@pytest.mark.parametrize('service', ['enclave', 'enclave-domain'])
def test_test_compose_metadata_is_test_policy(monkeypatch, service):
    compose = yaml.safe_load((ROOT / 'deploy/docker-compose.phala.test.yaml').read_text())
    env = compose['services'][service]['environment']
    config = load(monkeypatch, env)
    assert config['APP_AUTH']['contract'] == CONTRACT
    assert config['APP_AUTH']['chain_id'] == 11155111
    assert config['APP_AUTH']['deploy_tx'] is None
    assert config['RELEASE']['image_digest'] is None
    assert config['RELEASE']['compose_yaml_url'] == URL


@pytest.mark.parametrize('value', ['', 'unknown', 'sha256:dev', '0x123', 'x'*64])
def test_unavailable_or_malformed_metadata_is_null(monkeypatch, value):
    config = load(monkeypatch, {'FEEDLING_IMAGE_DIGEST':value,'FEEDLING_APP_AUTH_DEPLOY_TX':value})
    assert json.loads(json.dumps(config['RELEASE']))['image_digest'] is None
    assert json.loads(json.dumps(config['APP_AUTH']))['deploy_tx'] is None


def test_absent_metadata_does_not_fabricate_digest_or_historical_transaction(monkeypatch):
    config = load(monkeypatch, {})
    assert config['APP_AUTH']['deploy_tx'] is None
    assert config['RELEASE']['image_digest'] is None


def test_explicit_metadata_retained_as_declaration(monkeypatch):
    digest, tx = 'sha256:'+'ab'*32, '0x'+'cd'*32
    config = load(monkeypatch, {'FEEDLING_IMAGE_DIGEST':digest,'FEEDLING_APP_AUTH_DEPLOY_TX':tx})
    assert config['APP_AUTH']['deploy_tx'] == tx
    assert config['RELEASE']['image_digest'] == digest
