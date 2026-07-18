# Runtime V2 E2B artifact template

This template supplies the fixed `/opt/feedling/bin/extract-artifact` contract
used by `workspace.e2b_sandbox`. It handles bounded UTF-8 text, DOCX, XLSX, and
text-bearing PDFs. The command accepts no arguments: the runner writes input,
metadata, and output at fixed paths, so model-authored filenames or MIME values
never enter a shell command.

Build it from this directory:

```bash
export E2B_API_KEY='...'
export FEEDLING_V2_E2B_TEMPLATE='feedling-runtime-v2-artifacts-v1'
python build_template.py
```

Set the same template tag, the API key, and
`FEEDLING_V2_SANDBOX_PROVIDER=e2b` through the runner's encrypted environment
channel. Internet access is disabled by default. Enabling E2B sends decrypted
artifact bytes from Feedling's CVM into an E2B microVM and therefore requires
the product's explicit data-boundary/consent decision.

