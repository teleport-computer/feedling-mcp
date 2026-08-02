# Enclave Custom-Domain Dual Entry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DNS-only Feedling enclave domains through the existing in-CVM `dstack-ingress` while preserving the current Phala `-5003s` TLS-pinned endpoint unchanged.

**Architecture:** Each main-CVM compose gains an internal-only `enclave-domain:5004` replica with TLS disabled and an explicit `attested_ingress` transport mode. The existing ingress terminates Let's Encrypt TLS for the new domain and routes plaintext only across the measured CVM's Docker network; the existing `enclave:5003` service and gateway passthrough remain the legacy/audit path.

**Tech Stack:** Python 3.12, FastAPI, gunicorn/uvicorn, Docker Compose, dstack-ingress 2.2, Phala CVM, Cloudflare DNS-01, pytest, PyYAML, cryptography, Next.js/Fumadocs documentation.

## Global Constraints

- Domains are exactly `test-enclave.feedling.app`, `pre-enclave.feedling.app`, and `enclave.feedling.app`.
- Cloudflare remains DNS-only; do not enable orange-cloud proxying.
- Existing `-5003s` URLs, host port `5003`, in-process TLS, certificate fingerprint pinning, and CI `*_MAIN_ENCLAVE_URL` variables remain unchanged in this plan.
- `enclave-domain` must have no host `ports`; only compose-network port `5004` may be exposed.
- The custom-domain listener must declare `FEEDLING_ENCLAVE_TLS=false` and `FEEDLING_ENCLAVE_TRANSPORT_MODE=attested_ingress` as measured compose literals.
- The direct listener must declare `FEEDLING_ENCLAVE_TLS=true` and `FEEDLING_ENCLAVE_TRANSPORT_MODE=direct_tls` as measured compose literals.
- A new domain is not considered equivalent to the pinned direct endpoint until a client verifies both ingress certificate evidence and enclave attestation.
- This plan prepares and deploys the server path but does not switch iOS clients; the iOS verifier requires a separate plan in the client repository.
- Follow repository flow: ordinary branches target `test`; production promotion must originate from `test` or `pre`, with test-environment evidence recorded first.
- Public architecture/trust-boundary changes and `Unreleased` changelog text ship with the code.

---

### Task 1: Make listener transport semantics explicit in enclave health and attestation

**Files:**
- Modify: `backend/enclave/config.py:15-30`
- Modify: `backend/enclave/routes/health.py:15-102`
- Test: `tests/test_enclave_routes_health.py`
- Create: `tests/test_enclave_config.py`

**Interfaces:**
- Produces: `enclave.config.ENCLAVE_TRANSPORT_MODE: str`, constrained to `direct_tls`, `attested_ingress`, or `operator_tls`.
- Produces: additive `transport_mode` in both `/healthz` and `/attestation` JSON.
- Preserves: `tls_enabled`, `tls_in_enclave`, `phase`, and `enclave_tls_cert_fingerprint_hex` for old clients.

- [ ] **Step 1: Write failing configuration tests**

Add subprocess-isolated tests so an intentionally invalid import cannot leave the
pytest process with a partially reloaded config module:

```python
ROOT = Path(__file__).resolve().parents[1]


def _probe(tls: str, mode: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    env["FEEDLING_ENCLAVE_TLS"] = tls
    if mode is None:
        env.pop("FEEDLING_ENCLAVE_TRANSPORT_MODE", None)
    else:
        env["FEEDLING_ENCLAVE_TRANSPORT_MODE"] = mode
    return subprocess.run(
        [sys.executable, "-c", "from enclave import config; print(config.ENCLAVE_TRANSPORT_MODE)"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(("tls", "mode", "expected"), [
    ("true", None, "direct_tls"),
    ("false", None, "operator_tls"),
    ("false", "attested_ingress", "attested_ingress"),
])
def test_enclave_transport_mode_defaults_and_override(tls, mode, expected):
    result = _probe(tls, mode)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("tls", "mode"),
    [("true", "attested_ingress"), ("false", "direct_tls"), ("false", "bogus")],
)
def test_enclave_transport_mode_rejects_invalid_combinations(tls, mode):
    result = _probe(tls, mode)
    assert result.returncode != 0
    assert "FEEDLING_ENCLAVE_TRANSPORT_MODE" in result.stderr
```

- [ ] **Step 2: Run the configuration tests and confirm red**

Run: `uv run pytest tests/test_enclave_config.py -q`

Expected: FAIL because `ENCLAVE_TRANSPORT_MODE` does not exist and invalid combinations are accepted.

- [ ] **Step 3: Implement the validated transport mode**

Add directly after `ENCLAVE_TLS` in `backend/enclave/config.py`:

```python
_transport_raw = os.environ.get("FEEDLING_ENCLAVE_TRANSPORT_MODE", "").strip().lower()
ENCLAVE_TRANSPORT_MODE = _transport_raw or ("direct_tls" if ENCLAVE_TLS else "operator_tls")
if ENCLAVE_TRANSPORT_MODE not in {"direct_tls", "attested_ingress", "operator_tls"}:
    raise RuntimeError(
        "FEEDLING_ENCLAVE_TRANSPORT_MODE must be direct_tls, attested_ingress, or operator_tls"
    )
if ENCLAVE_TRANSPORT_MODE == "direct_tls" and not ENCLAVE_TLS:
    raise RuntimeError("FEEDLING_ENCLAVE_TRANSPORT_MODE=direct_tls requires FEEDLING_ENCLAVE_TLS=true")
if ENCLAVE_TRANSPORT_MODE == "attested_ingress" and ENCLAVE_TLS:
    raise RuntimeError("FEEDLING_ENCLAVE_TRANSPORT_MODE=attested_ingress requires FEEDLING_ENCLAVE_TLS=false")
```

- [ ] **Step 4: Write failing route-contract tests**

Extend `tests/test_enclave_routes_health.py`:

```python
def test_health_and_attestation_expose_attested_ingress_transport(monkeypatch, client):
    monkeypatch.setattr("enclave.routes.health.config.ENCLAVE_TRANSPORT_MODE", "attested_ingress")
    monkeypatch.setitem(enclave_state._state, "tls_enabled", False)
    monkeypatch.setitem(enclave_state._state, "content_pk_hex", "aa" * 32)
    monkeypatch.setitem(enclave_state._state, "signing_pk_hex", "bb" * 32)
    monkeypatch.setitem(enclave_state._state, "booted_at", 123.0)
    monkeypatch.setitem(enclave_state._state, "attestation", {
        "tdx_quote_hex": "cc" * 64,
        "event_log_json": "[]",
        "measurements": {"mrtd": "00"},
        "compose_hash": "h",
        "app_id": "app",
        "instance_id": "inst",
    })

    health = client.get("/healthz").get_json()
    attestation = client.get("/attestation").get_json()
    assert health["transport_mode"] == "attested_ingress"
    assert attestation["transport_mode"] == "attested_ingress"
    assert attestation["tls_in_enclave"] is False
    assert "dstack-ingress evidence" in attestation["notes"]
```

- [ ] **Step 5: Run the route test and confirm red**

Run: `uv run pytest tests/test_enclave_routes_health.py::test_health_and_attestation_expose_attested_ingress_transport -q`

Expected: FAIL with missing `transport_mode`.

- [ ] **Step 6: Add transport mode to health and attestation responses**

In `_health_body()`, add:

```python
"transport_mode": config.ENCLAVE_TRANSPORT_MODE,
```

In the attestation bundle, add the same field and replace the two-way `notes` conditional with:

```python
notes_by_transport = {
    "direct_tls": (
        "phase-3: TLS terminated by this enclave listener; clients must compare "
        "the live cert DER fingerprint with enclave_tls_cert_fingerprint_hex."
    ),
    "attested_ingress": (
        "TLS terminated by dstack-ingress inside the measured CVM. Clients must "
        "verify dstack-ingress evidence separately; the enclave TLS fingerprint "
        "describes only the direct -5003s listener."
    ),
    "operator_tls": (
        "TLS termination is not attested by this listener; do not treat ordinary "
        "WebPKI as equivalent to enclave certificate pinning."
    ),
}
```

Set `bundle["notes"] = notes_by_transport[config.ENCLAVE_TRANSPORT_MODE]` without changing existing legacy fields.

- [ ] **Step 7: Run focused enclave tests**

Run: `uv run pytest tests/test_enclave_config.py tests/test_enclave_routes_health.py tests/test_enclave_keys_attestation.py tests/test_enclave_serving_asgi.py -q`

Expected: PASS.

- [ ] **Step 8: Commit the transport contract**

```bash
git add backend/enclave/config.py backend/enclave/routes/health.py tests/test_enclave_config.py tests/test_enclave_routes_health.py
git commit -m "feat(enclave): expose listener transport mode"
```

---

### Task 2: Lock the three compose files to the dual-entry topology

**Files:**
- Create: `tests/test_enclave_domain_compose.py`
- Modify: `deploy/docker-compose.phala.test.yaml`
- Modify: `deploy/docker-compose.phala.pre.yaml`
- Modify: `deploy/docker-compose.phala.yaml`

**Interfaces:**
- Consumes: `FEEDLING_ENCLAVE_TRANSPORT_MODE` from Task 1.
- Produces: compose service `enclave-domain` on internal port `5004` in all environments.
- Produces: SNI routes from the three custom domains to `enclave-domain:5004`.
- Preserves: existing service `enclave`, port `5003:5003`, image pinning, volumes, secrets, and direct healthcheck.

- [ ] **Step 1: Write failing topology contract tests**

Create `tests/test_enclave_domain_compose.py`:

```python
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
```

- [ ] **Step 2: Run the topology tests and confirm red**

Run: `uv run pytest tests/test_enclave_domain_compose.py -q`

Expected: three failures because `enclave-domain` does not exist.

- [ ] **Step 3: Anchor the existing enclave values and add the direct transport literal**

In each file, attach YAML anchors to the existing direct service's image,
command, environment mapping, and volumes. Preserve every current literal; only
add `FEEDLING_ENCLAVE_TRANSPORT_MODE` after `FEEDLING_ENCLAVE_TLS`:

```yaml
enclave:
  # Test uses :077ddf8, pre uses :453d65e, prod uses :e94482d.
  image: &enclave-image ghcr.io/teleport-computer/feedling:077ddf8
  command: &enclave-command ["python", "-u", "backend/enclave_app.py"]
  environment: &enclave-environment
    FEEDLING_ENCLAVE_PORT: "5003"
    FEEDLING_DATA_DIR: "/data"
    FEEDLING_ENCLAVE_TLS: "true"
    FEEDLING_ENCLAVE_TRANSPORT_MODE: "direct_tls"
    # All remaining existing entries stay exactly where they are.
  volumes: &enclave-volumes
    # Keep this file's existing environment-specific volume literals unchanged.
```

The code block shows the test literal. Use `453d65e` on the corresponding pre
line and `e94482d` on the corresponding prod line. If an earlier implementation
commit has legitimately repinned an environment before this step, retain that
file's newly pinned literal; the alias ensures both listeners resolve to exactly
one image value.

- [ ] **Step 4: Add `enclave-domain` to each compose**

Add `enclave-domain` using the anchors created in Step 3, so there is no copied
environment block that can drift:

```yaml
enclave-domain:
  image: *enclave-image
  command: *enclave-command
  restart: unless-stopped
  environment:
    <<: *enclave-environment
    FEEDLING_ENCLAVE_PORT: "5004"
    FEEDLING_ENCLAVE_TLS: "false"
    FEEDLING_ENCLAVE_TRANSPORT_MODE: "attested_ingress"
  expose:
    - "5004"
  volumes: *enclave-volumes
  healthcheck:
    test: ["CMD-SHELL", "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5004/healthz | grep -qE '^(200|503)$'"]
    interval: 15s
    timeout: 5s
    retries: 20
    start_period: 120s
```

Do not add a `ports` key. Run `docker compose config` before committing to prove
the YAML merge keys resolve as intended.

- [ ] **Step 5: Extend ingress domain and routing maps**

Apply the exact environment mapping:

```yaml
# test
DOMAINS: |
  test-api.feedling.app
  test-enclave.feedling.app
ROUTING_MAP: |
  test-api.feedling.app=backend:5001
  test-enclave.feedling.app=enclave-domain:5004

# pre
DOMAINS: |
  pre-api.feedling.app
  pre-enclave.feedling.app
ROUTING_MAP: |
  pre-api.feedling.app=backend:5001
  pre-enclave.feedling.app=enclave-domain:5004

# prod
DOMAINS: |
  api.feedling.app
  enclave.feedling.app
ROUTING_MAP: |
  api.feedling.app=backend:5001
  enclave.feedling.app=enclave-domain:5004
```

Add `enclave-domain: {condition: service_started}` under each ingress `depends_on` while retaining backend.

- [ ] **Step 6: Validate strict YAML and resolved Compose**

Run:

```bash
uv run pytest tests/test_enclave_domain_compose.py tests/test_deploy_yaml_strict.py -q
docker compose -f deploy/docker-compose.phala.test.yaml config --quiet
docker compose -f deploy/docker-compose.phala.pre.yaml config --quiet
docker compose -f deploy/docker-compose.phala.yaml config --quiet
```

Expected: all tests PASS and all three compose commands exit 0.

- [ ] **Step 7: Commit the measured topology change**

```bash
git add tests/test_enclave_domain_compose.py deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.pre.yaml deploy/docker-compose.phala.yaml
git commit -m "feat(deploy): add enclave custom-domain listeners"
```

---

### Task 3: Add a deterministic ingress-evidence deployment verifier

**Files:**
- Create: `tools/verify_enclave_domain.py`
- Create: `tests/test_verify_enclave_domain.py`
- Modify: `deploy/DEPLOYMENTS.md`

**Interfaces:**
- Produces: `parse_sha256_manifest(text: str) -> dict[str, str]`.
- Produces: `verify_ingress_evidence(domain: str, peer_der: bytes, files: Mapping[str, bytes], quote_parser: Callable[[bytes], TDXQuote] = parse_quote) -> list[str]`; returns human-readable passed checks and raises `EvidenceError` on mismatch.
- Produces CLI, for example: `python tools/verify_enclave_domain.py --domain test-enclave.feedling.app --expected-compose-hash 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef --expected-content-pk 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef`.
- Limitation: this repository's DCAP helper parses quote measurements but does not validate Intel's signature chain. The CLI must print that limitation; cryptographic quote-chain validation belongs to the separate iOS verifier plan and cannot be claimed by this tool.

- [ ] **Step 1: Write failing evidence-unit tests**

Cover these exact cases using an in-memory self-signed certificate and a synthetic quote object supplied through a parser seam:

```python
def test_manifest_rejects_path_traversal():
    with pytest.raises(EvidenceError, match="unsafe evidence filename"):
        parse_sha256_manifest("00" * 32 + "  ../cert.pem\n")


def test_evidence_rejects_peer_certificate_mismatch(evidence_fixture):
    with pytest.raises(EvidenceError, match="TLS peer certificate"):
        verify_ingress_evidence(
            domain="test-enclave.feedling.app",
            peer_der=b"different",
            files=evidence_fixture.files,
            quote_parser=evidence_fixture.quote_parser,
        )


def test_evidence_accepts_peer_manifest_and_report_data(evidence_fixture):
    checks = verify_ingress_evidence(
        domain="test-enclave.feedling.app",
        peer_der=evidence_fixture.peer_der,
        files=evidence_fixture.files,
        quote_parser=evidence_fixture.quote_parser,
    )
    assert checks == [
        "peer certificate matches cert-test-enclave.feedling.app.pem",
        "manifest hashes match all referenced evidence files",
        "quote report_data binds sha256sum.txt",
    ]
```

- [ ] **Step 2: Run tests and confirm red**

Run: `uv run pytest tests/test_verify_enclave_domain.py -q`

Expected: FAIL because the verifier module is absent.

- [ ] **Step 3: Implement manifest, certificate, and quote report-data checks**

The implementation must:

```python
CERT_NAME = f"cert-{domain}.pem"
manifest = parse_sha256_manifest(files["sha256sum.txt"].decode("utf-8"))
for name, expected in manifest.items():
    actual = hashlib.sha256(files[name]).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise EvidenceError(f"evidence hash mismatch: {name}")

cert = x509.load_pem_x509_certificate(files[CERT_NAME])
evidence_der = cert.public_bytes(serialization.Encoding.DER)
if not hmac.compare_digest(hashlib.sha256(peer_der).digest(), hashlib.sha256(evidence_der).digest()):
    raise EvidenceError("TLS peer certificate does not match ingress evidence")

quote_json = json.loads(files["quote.json"])
quote = quote_parser(bytes.fromhex(quote_json["quote"]))
manifest_digest = hashlib.sha256(files["sha256sum.txt"]).digest()
if not hmac.compare_digest(quote.body.report_data[:32], manifest_digest):
    raise EvidenceError("quote report_data does not bind sha256sum.txt")
```

The live CLI fetches `quote.json` and `sha256sum.txt`, fetches every safe filename listed in the manifest from `/evidences/`, captures the live TLS peer certificate with an `ssl.create_default_context()` socket, and separately fetches `/attestation` to compare `compose_hash`, `enclave_content_pk_hex`, and `transport_mode=attested_ingress` with the supplied expectations.

- [ ] **Step 4: Run verifier tests**

Run: `uv run pytest tests/test_verify_enclave_domain.py tools/dcap/test_dcap_parse.py -q`

Expected: PASS.

- [ ] **Step 5: Document exact live verification commands**

Add to the current-environment sections of `deploy/DEPLOYMENTS.md`:

```bash
python tools/verify_enclave_domain.py \
  --domain test-enclave.feedling.app \
  --expected-compose-hash "$TEST_COMPOSE_HASH" \
  --expected-content-pk "$TEST_ENCLAVE_CONTENT_PK_BASELINE"
```

State explicitly that `tools/verify_enclave_domain.py` checks evidence binding and measurements structurally, while the client release gate must additionally perform Intel DCAP signature-chain verification.

- [ ] **Step 6: Commit the operations verifier**

```bash
git add tools/verify_enclave_domain.py tests/test_verify_enclave_domain.py deploy/DEPLOYMENTS.md
git commit -m "feat(ops): verify enclave ingress evidence"
```

---

### Task 4: Synchronize public architecture and trust-boundary documentation

**Files:**
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs-site/content/docs/workflows/chat.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `deploy/DEPLOYMENTS.md`

**Interfaces:**
- Documents: two supported enclave transports and their distinct verification chains.
- Documents: Cloudflare DNS-only role and the CVM-internal plaintext hop.
- Preserves: old `-5003s` endpoint as the compatibility/audit route.

- [ ] **Step 1: Add architecture and trust-model text**

Document the two paths verbatim in the relevant pages:

```text
Custom-domain path: client TLS -> dstack-ingress inside the measured TDX CVM
-> Docker-internal HTTP -> enclave-domain. Verify WebPKI, ingress certificate
evidence, and enclave attestation.

Direct audit path: client TLS -> Phala -5003s passthrough -> enclave. Verify the
live self-signed certificate fingerprint against enclave attestation REPORT_DATA.
```

State that Cloudflare manages DNS-01 records only and does not proxy traffic.

- [ ] **Step 2: Add the `Unreleased` changelog entry**

Add one bullet describing the new domain names, old-client compatibility, and the requirement that new clients verify both evidence bundles before claiming pinning-equivalent security.

- [ ] **Step 3: Run documentation checks**

Run from `docs-site`:

```bash
npm run types:check
npm run lint
npm run build
```

Expected: all commands exit 0. OpenAPI regeneration is not required because no public HTTP path or schema changes; the additive attestation fields are an operational trust payload outside the generated public API contract. If contract tests prove otherwise, update the OpenAPI source and run `npm run openapi:generate` before committing.

- [ ] **Step 4: Commit documentation**

```bash
git add docs-site/content/docs/architecture.mdx docs-site/content/docs/self-hosting.mdx docs-site/content/docs/workflows/chat.mdx docs-site/content/docs/changelog.mdx deploy/DEPLOYMENTS.md
git commit -m "docs: explain enclave dual-entry trust model"
```

---

### Task 5: Run the local release gate before any environment mutation

**Files:**
- No new files unless a failing test requires an in-scope correction.

**Interfaces:**
- Validates Tasks 1-4 together before test deployment.

- [ ] **Step 1: Run focused tests with a real local Postgres available**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_enclave_config.py \
  tests/test_enclave_routes_health.py \
  tests/test_enclave_keys_attestation.py \
  tests/test_enclave_serving_asgi.py \
  tests/test_enclave_domain_compose.py \
  tests/test_deploy_yaml_strict.py \
  tests/test_verify_enclave_domain.py -q
```

Expected: PASS with no skipped listed module.

- [ ] **Step 2: Run the repository L1 suite**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests -q \
  --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
```

Expected: compare against the documented current baseline; report every failure and do not call the change green unless new failures are zero.

- [ ] **Step 3: Re-run compose and docs gates**

Run the three `docker compose ... config --quiet` commands from Task 2 and the three docs-site commands from Task 4. Record exact exit codes and summaries in the eventual test PR.

- [ ] **Step 4: Review measured-compose diff**

Run:

```bash
git diff origin/test...HEAD -- deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.pre.yaml deploy/docker-compose.phala.yaml
```

Confirm that every security-relevant literal is intentional, all image tags still match their environment's direct service, and no secret value was added.

---

### Task 6: Deploy and validate the dormant test-domain path

**Files:**
- Modify only if test evidence reveals a defect; do not edit prod values during this task.

**Interfaces:**
- Produces: live `https://test-enclave.feedling.app` while clients still use the old URL.
- Preserves: `TEST_MAIN_ENCLAVE_URL` and the current direct canary.

- [ ] **Step 1: Open a PR targeting `test`**

Include the design, implementation plan, local test evidence, compose diff, and rollback statement: reverting the compose removes only the dormant custom domain while old clients remain on `-5003s`.

- [ ] **Step 2: Merge through normal review and monitor `deploy-test-cvm`**

Do not bypass branch-flow or attestation gates. Record the deployed image tag, new compose hash, AppAuth publication transaction, and CI run URL.

- [ ] **Step 3: Verify public DNS without changing proxy mode**

Run:

```bash
dig +short test-enclave.feedling.app CNAME
dig +short _dstack-app-address.test-enclave.feedling.app TXT
curl -sS -I https://test-enclave.feedling.app/healthz
```

Expected: CNAME to the prod9 dstack gateway, TXT containing the test app ID and `:443`, and no Cloudflare proxy response headers.

- [ ] **Step 4: Verify the custom-domain evidence and attestation**

Run `tools/verify_enclave_domain.py` with the deployed compose hash and test content-key baseline. Require `transport_mode=attested_ingress`, matching peer certificate evidence, manifest hashes, quote report data, compose hash, and content key.

- [ ] **Step 5: Re-run the original direct-path gates**

Run the existing `deploy/attestation-gate.sh`, `tools/audit_live_cvm.py`, decrypt self-check, and `tools/deploy_canary.py` against the test `-5003s` URL. Expected: the original TLS fingerprint pin and content key remain unchanged.

- [ ] **Step 6: Validate from an affected network**

From at least one region/network where `*.phala.network` fails, record that `test-enclave.feedling.app/healthz` and `/attestation` are reachable. Do not use a successful local probe as evidence for the original regional-access requirement.

- [ ] **Step 7: Hold the server path dormant for observation**

Observe DNS/TLS, ingress, both enclave services, decrypt self-check, API, WebSocket, and runner health for the agreed test soak window. No client endpoint switch occurs in this task.

---

### Task 7: Promote server readiness through pre and prod, then hand off to the iOS plan

**Files:**
- No code changes expected; any defect starts a new tested fix commit and repeats the affected environment gate.

**Interfaces:**
- Produces: dormant, validated `pre-enclave.feedling.app` and `enclave.feedling.app`.
- Hands off: live URLs, reference measurements, evidence fixtures, compose hashes, and failure semantics to the iOS repository.

- [ ] **Step 1: Promote test-approved work to pre**

Use the repository's accepted test/pre merge direction. Deploy the pre main CVM, publish the compose hash, and repeat every DNS, evidence, attestation, direct-pin, decrypt, API, WebSocket, runner, and affected-network check from Task 6 with pre values.

- [ ] **Step 2: Record pre evidence and rollback criteria**

The promotion record must include the CI run, image, compose hash, AppAuth transaction, domain certificate fingerprint, evidence manifest hash, old direct certificate fingerprint, content public key, probe timestamps, and rollback condition.

- [ ] **Step 3: Promote only from `test` or `pre` to `main`**

Open the production PR from an allowed source branch. Include test and pre evidence and state explicitly that existing clients and `PROD_MAIN_ENCLAVE_URL` remain on the old path.

- [ ] **Step 4: Deploy production and repeat the full dormant-path gate**

Validate `enclave.feedling.app` plus the production `-5003s` route. A custom-domain failure rolls back the new compose/domain route; a direct-path pin, content-key, or canary regression blocks/rolls back production immediately.

- [ ] **Step 5: Create the iOS-repository implementation plan**

In the actual iOS repository, write a separate spec-linked plan covering endpoint selection, WebPKI peer-certificate capture, `/evidences/` verification, Intel DCAP signature-chain verification, approved-measurement matching, enclave AppAuth verification, explicit direct-path fallback, audit-card copy, fixtures, and staged rollout. Do not switch production clients until that plan's tests and test/pre evidence are complete.

- [ ] **Step 6: Preserve the direct endpoint indefinitely**

Keep monitoring and documentation for both routes. Removing `-5003s` requires a separate approved retirement design with client adoption evidence; it is not a cleanup step in this plan.
