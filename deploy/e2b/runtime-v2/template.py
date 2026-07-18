"""E2B template definition for Feedling Runtime V2 artifact processing."""

from e2b import Template


template = (
    Template()
    .from_base_image()
    .pip_install(["pypdf==5.9.0"])
    .make_dir("/opt/feedling/bin", mode=0o755)
    .copy(
        "extract_artifact.py",
        "/opt/feedling/bin/extract-artifact",
        user="root",
        mode=0o755,
    )
)

