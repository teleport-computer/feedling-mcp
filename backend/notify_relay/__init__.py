"""Notify Relay — push relay for self-hosted deployments (自部署推送中继).

Self-hosted backends hold no official APNs .p8 key, so the official hosted
backend pushes on their behalf: the app enrolls anonymously for a relay auth
token (``nrt_…``), and the self-hosted backend calls the push endpoint with
that token. See deploy/SELF_HOSTING.md ("Push relay") for the public contract.
"""
