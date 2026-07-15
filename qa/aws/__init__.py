"""AWS helpers for disposable Feedling qualification runners.

The modules in this package deliberately depend only on the AWS CLI.  The
GitHub controller authenticates the CLI with OIDC; evaluator instances never
receive an instance profile or other AWS credentials.
"""
