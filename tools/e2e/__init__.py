"""Feedling E2E harness — the executable half of docs/testing/RELEASE_TESTING_PROTOCOL.md.

Entry point: ``python3 tools/e2e/p0.py`` (P0 release smoke, §3 of the protocol).
Test env ONLY — the client hard-refuses prod hosts. Every account this package
creates is deleted in teardown (test-account-hygiene).
"""
