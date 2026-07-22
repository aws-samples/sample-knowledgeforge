# Security Policy

> ⚠️ **Important - Sample Code Notice**
>
> This project is provided as **sample / reference code** for educational and
> demonstration purposes. It is **NOT intended for production use** without
> additional security hardening and review. Account IDs, KMS key IDs, tenant
> identifiers, and bucket names in this repository are placeholders.

## Reporting Vulnerabilities

If you discover a potential security issue in this project, please notify
AWS/Amazon Security via the
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public issue.

## Security Controls

### Multi-tenant isolation
- Each tenant has its own AppConfig profile, S3 Vectors bucket/index, Bedrock
  managed prompts, Bedrock guardrail, and KMS key.
- Tenant discovery is data-catalog driven, so onboarding a tenant requires no
  code change and no shared static credentials.

### Data protection
- Customer-managed KMS keys encrypt data at rest (S3, DynamoDB, and per-tenant
  vector indexes).
- TLS is enforced in transit for all AWS service calls.
- Amazon Bedrock Guardrails are applied to model inputs/outputs per tenant.

### IAM
- Least-privilege roles scoped per Lambda / per construct.
- `Resource: *` is used only where AWS requires it (e.g. actions that do not
  support resource-level permissions) - documented at the policy definition.

### Network
- Compute (Lambda, ECS Fargate) runs inside a VPC; egress is restricted via
  security groups.

### Code security
- No hardcoded credentials or real account IDs in source code.
- `.env` and local build artifacts are excluded via `.gitignore`.
- CDK constructs and Lambda handlers ship with unit tests.

## Configuration Files

Configuration is layered (YAML defaults → environment → per-tenant overrides)
and uses placeholders (`111111111111`, `acme`, `bcme`, etc.). Populate real
values per environment; never commit actual account IDs, credentials, or
tenant-specific secrets.

## Dependencies

- Python dependencies are pinned in `requirements.txt` / `pyproject.toml`.
- Node/CDK dependencies are pinned in `package.json` / `package-lock.json`.
- AWS SDK clients use adaptive retry to handle throttling gracefully.
