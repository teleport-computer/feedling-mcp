from __future__ import annotations

from pathlib import Path


TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "aws"
    / "cloudformation"
    / "runner-controller.yml"
).read_text(encoding="utf-8")


def _statement(sid: str, next_sid: str | None = None) -> str:
    start = TEMPLATE.index(f"              - Sid: {sid}\n")
    if next_sid is None:
        end = TEMPLATE.index("\nOutputs:\n", start)
    else:
        end = TEMPLATE.index(f"              - Sid: {next_sid}\n", start)
    return TEMPLATE[start:end]


def test_stack_has_no_ingress_and_only_ipv4_https_egress():
    security_group = TEMPLATE[
        TEMPLATE.index("  RunnerSecurityGroup:\n") : TEMPLATE.index(
            "  RunnerControllerRole:\n"
        )
    ]

    assert "SecurityGroupIngress: []" in security_group
    assert security_group.count("IpProtocol:") == 1
    assert "IpProtocol: tcp" in security_group
    assert "FromPort: 443" in security_group
    assert "ToPort: 443" in security_group
    assert "CidrIp: 0.0.0.0/0" in security_group
    assert "CidrIpv6:" not in security_group
    assert "FromPort: 53" not in security_group


def test_oidc_trust_is_bound_to_the_protected_github_environment():
    trust = TEMPLATE[
        TEMPLATE.index("      AssumeRolePolicyDocument:\n") : TEMPLATE.index(
            "      Policies:\n"
        )
    ]

    assert "Action: sts:AssumeRoleWithWebIdentity" in trust
    assert "arn:aws:iam" in TEMPLATE
    assert "aws-us-gov" not in TEMPLATE
    assert '"token.actions.githubusercontent.com:aud": sts.amazonaws.com' in trust
    assert (
        '"token.actions.githubusercontent.com:sub": !Sub '
        "repo:${Repository}:environment:${GitHubEnvironment}" in trust
    )


def test_launch_role_cannot_pass_roles_or_mutate_network_policy():
    forbidden = (
        "iam:PassRole",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:CreateSecurityGroup",
        "ec2:ModifyInstanceAttribute",
    )
    assert all(action not in TEMPLATE for action in forbidden)

    fixed = _statement(
        "LaunchWithFixedInfrastructure", "LaunchOnlyTaggedDisposableResources"
    )
    assert "${CanonicalAmiId}" in fixed
    assert "${PublicSubnetId}" in fixed
    assert "${RunnerSecurityGroup}" in fixed
    assert "network-interface/*" in fixed


def test_instance_and_volume_launch_contracts_are_fail_closed():
    instance = _statement(
        "LaunchOnlyTaggedDisposableResources", "LaunchOnlyTaggedDisposableVolumes"
    )
    volume = _statement("LaunchOnlyTaggedDisposableVolumes", "TagOnlyAsPartOfLaunch")
    required_tags = (
        "ManagedBy",
        "Purpose",
        "Repository",
        "RunId",
        "RunAttempt",
        "TargetSHA",
        "ControllerSHA",
        "ExpiresAt",
    )

    assert '"ec2:InstanceType": m7i.xlarge' in instance
    assert '"ec2:MetadataHttpEndpoint": enabled' in instance
    assert '"ec2:MetadataHttpTokens": required' in instance
    assert '"ec2:MetadataHttpPutResponseHopLimit": 1' in instance
    assert '"ec2:MetadataTags": disabled' in instance
    assert '"ec2:Encrypted": "true"' in volume
    assert '"ec2:VolumeType": gp3' in volume
    assert '"ec2:VolumeSize": 40' in volume
    for tag in required_tags:
        assert f'"aws:RequestTag/{tag}": "false"' in instance
        assert f'"aws:RequestTag/{tag}": "false"' in volume


def test_termination_is_limited_to_managed_repository_instances():
    terminate = _statement("TerminateOnlyManagedRepositoryRunners")

    assert "Action: ec2:TerminateInstances" in terminate
    assert '"ec2:ResourceTag/ManagedBy": feedling-agentic-e2e' in terminate
    assert '"ec2:ResourceTag/Purpose": api-key-e2e' in terminate
    assert '"ec2:ResourceTag/Repository": !Ref Repository' in terminate
