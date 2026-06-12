"""Shared kernel for the feedling backend.

Modules here sit at the bottom of the dependency stack: they may import
db / content_encryption / provider_client, but never a domain package
(accounts, chat, hosted, ...) and never app.py. Cross-module calls go
through module attributes (``from core import enclave; enclave.func()``)
so monkeypatching the defining module reaches every caller.
"""
