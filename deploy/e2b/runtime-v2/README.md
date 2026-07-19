# Runtime V2 E2B artifact template

This template supplies the fixed `/opt/feedling/bin/extract-artifact` contract
used by `workspace.e2b_sandbox`. It handles bounded UTF-8 text, DOCX, XLSX, and
text-bearing PDFs. The command accepts no arguments: the runner writes input,
metadata, and output at fixed paths, so model-authored filenames or MIME values
never enter a shell command.

Build it from this directory:

```bash
export E2B_API_KEY='...'
python build_template.py
python verify_template.py
```

The script derives a tag from the extractor and digest-pinned Python base-image
build contract, refuses
an arbitrary human alias, never rebuilds an existing content tag, and prints a
JSON lock containing the tag, full content SHA-256, and (for a new build) E2B
template/build IDs. Preserve that output with the deployment record.
`verify_template.py` then creates a secure, offline canary microVM, checks the
full version digest, and runs a fixed text artifact through the extractor.

Set the printed `template` tag, the API key, and
`FEEDLING_V2_SANDBOX_PROVIDER=e2b` through the runner's encrypted environment
channel. Each acquired sandbox must expose the matching full digest at
`/opt/feedling/TEMPLATE_VERSION`; a mismatched or mutable alias fails closed.
Internet access is disabled by default. Enabling E2B sends decrypted
artifact bytes from Feedling's CVM into an E2B microVM and therefore requires
the product's explicit data-boundary/consent decision.
