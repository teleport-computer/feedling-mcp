# Disposable AWS evaluator

The protected qualification workflow creates one fresh `m7i.xlarge` GitHub
Actions runner for each run. The VM is the test **driver**; it does not host a
Feedling backend or Runtime V2. It exists only while the agentic API-key suite
is running against the already deployed `test` environment.

There is no SSH key, inbound security-group rule, or EC2 instance profile. The
runner registers with a one-job GitHub JIT configuration, uses encrypted
delete-on-termination storage, and terminates after the job. A root-owned hard
expiry, the workflow's hosted cleanup job, and the scheduled AWS reaper are
independent cleanup paths.

## Trust boundary

- A collaborator dispatches the workflow from protected `main`.
- GitHub-hosted controller jobs validate the deployed `test` image pin, mint a
  one-use runner configuration, and use GitHub OIDC to launch EC2.
- The EC2 runner checks out only the immutable protected controller SHA. It
  never checks out or executes the deployed candidate branch.
- Provider keys, the test-admin token, and the run-scoped ChatGPT OAuth copy are
  exposed only to the qualification job after the runner is online.
- Application-under-test code receives provider keys through the real deployed
  BYOK APIs. Arbitrary branch previews therefore require the separate scoped
  provider-broker design; this runner does not weaken that boundary.

IMDS remains enabled because Canonical cloud-init reads EC2 user-data through
it. The launch request requires IMDSv2 tokens, a hop limit of one, disabled
instance-metadata tags, and no instance role, so IMDS cannot return AWS
credentials.

## 1. AWS prerequisites

This first version supports the commercial `aws` partition; it intentionally
rejects GovCloud because the pinned Canonical AMI ownership contract differs.
Choose one AWS region, an existing VPC, and a public IPv4 subnet. The subnet
must have `MapPublicIpOnLaunch=true` and one Internet Gateway default route.
The controller rejects a private/NAT subnet in this first version so network
behavior is explicit and reproducible. The generated security group has no
ingress and permits only HTTPS egress. AmazonProvidedDNS traffic is handled by
the VPC resolver and cannot be blocked by a security group; no public port-53
egress is opened.

Select and pin a region-local Canonical Ubuntu Server 24.04 amd64 EBS AMI. The
controller verifies the owner ID (`099720109477`), architecture, image name,
root-device type, virtualization type, and availability before it launches
anything. An AMI ID is not portable between regions.

The AWS account also needs the standard GitHub Actions OIDC provider for
`https://token.actions.githubusercontent.com`. Pass that provider's ARN to the
stack; do not create a second provider if the account already has one.

Deploy the checked-in stack from a trusted checkout:

```bash
aws cloudformation deploy \
  --region "$AWS_REGION" \
  --stack-name feedling-agentic-e2e \
  --template-file qa/aws/cloudformation/runner-controller.yml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    VpcId="$VPC_ID" \
    PublicSubnetId="$PUBLIC_SUBNET_ID" \
    CanonicalAmiId="$UBUNTU_AMI_ID" \
    GitHubOidcProviderArn="$GITHUB_OIDC_PROVIDER_ARN"
```

The role can inspect EC2 launch inputs, launch only the stack's AMI/subnet/
security group with the required Feedling tags, and terminate only matching
managed instances. It cannot pass an IAM role, create ingress, or operate an
untagged instance. Record the four stack outputs without placing them in a
repository file.

The launch policy assumes the volume uses the AWS-managed `aws/ebs` key. If the
account's default EBS key is a customer-managed KMS key, add only the specific
KMS permissions/grant required for that key; do not broaden the controller role
to arbitrary KMS resources.

## 2. GitHub App and runner group

Create a GitHub App owned by the organization and install it only on
`teleport-computer/feedling-mcp`. Disable webhooks. Grant repository
`Administration: Read and write` (plus the implicit metadata read permission),
which is needed to generate JIT runner configuration, inspect the exact runner,
and delete a stale registration. Grant organization `Self-hosted runners: Read`
so the controller can fail closed if the configured runner-group ID, name, or
workflow restriction drifts. Do not grant code or secret permissions.

Generate one App private key. Store the PEM only as the `feedling-e2e-test`
Environment secret `QA_RUNNER_GITHUB_APP_PRIVATE_KEY`; store the numeric App ID
as the Environment variable `QA_RUNNER_GITHUB_APP_ID`.

Use a dedicated GitHub Actions runner group. Configure its workflow access to
allow **only**
`teleport-computer/feedling-mcp/.github/workflows/api-key-e2e.yml@refs/heads/main`;
repository-only access is too broad because another workflow could race for a
new runner through its generic `self-hosted` label. Record the group's positive
numeric ID as `QA_RUNNER_GROUP_ID` and its exact name as
`QA_RUNNER_GROUP_NAME`. The qualification job selects both that group and its
unique `feedling-e2e-<run>-<attempt>` label.

## 3. GitHub Environment configuration

The `feedling-e2e-test` Environment must allow only protected `main` and must
not require a reviewer. This makes runs self-service while keeping changes to
the secret-bearing controller behind normal branch protection.

Add these Environment variables from the AWS stack and selected region:

- `QA_AWS_ROLE_ARN`
- `QA_AWS_REGION`
- `QA_AWS_AMI_ID`
- `QA_AWS_SUBNET_ID`
- `QA_AWS_SECURITY_GROUP_ID`
- `QA_RUNNER_GITHUB_APP_ID`
- `QA_RUNNER_GROUP_ID`
- `QA_RUNNER_GROUP_NAME`

Add the App PEM as the one new infrastructure secret:

- `QA_RUNNER_GITHUB_APP_PRIVATE_KEY`

The provider, model, admin, and Codex variables/secrets are listed in the
parent [`qa/README.md`](../README.md). Store a complete refreshable ChatGPT
`auth.json` as base64 without printing it:

```bash
openssl base64 -A -in "$HOME/.codex/auth.json" |
  gh secret set QA_CODEX_AUTH_JSON_B64 --env feedling-e2e-test
```

Routine runs do not require signing out other devices. Rotate the copied OAuth
session only if exposure is suspected, and prefer a capped dedicated QA account
after the first version is proven.

## 4. Run it

After the QA controller is merged to protected `main` and the candidate is
deployed through the normal protected `test` process, any collaborator with
write access can run:

```bash
gh workflow run ci.yml --ref main -f runtime_target=deployed_current
```

Use `hosted_resident` only when the deployed environment is expected to satisfy
the strict Runtime V2 receipts. The current-runtime target still verifies the
exact deployed backend SHA; it does not claim Runtime V2.

Provisioning waits for an exact-name, exact-label, online, idle runner before
the secret-bearing job is queued. Startup failure triggers immediate hosted
rollback. Final hosted cleanup runs even when qualification fails or is
cancelled, and tag-based discovery still works if the instance-ID output was
lost.

Before enabling team-wide runs, perform one infrastructure canary with capped
QA credentials. Confirm the real GitHub runner-group response passes the exact
policy check, the job lands on the unique JIT runner, EC2 terminates after the
job, the runner registration disappears, and an intentionally expired tagged
canary is removed by the hourly reaper. Textual/unit contracts cannot substitute
for that first live AWS/GitHub integration proof.

## Cost and lifetime

There is no always-on evaluator. Each dispatch creates at most one on-demand
`m7i.xlarge` and one encrypted 40 GiB gp3 root volume. Normal completion shuts
it down immediately; EC2's shutdown behavior is `terminate`. The maximum
lifetime is bounded to five hours, with the scheduled reaper as a final safety
net. Provider/model usage is separately limited by the dedicated capped QA
accounts and keys.
