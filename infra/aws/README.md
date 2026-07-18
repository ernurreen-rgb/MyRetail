# MyRetail AWS production baseline

Status: deployable infrastructure baseline, intentionally fail-closed. This directory does not
claim that a live environment exists. `traffic_enabled` defaults to `false`, so the first apply
creates no running API or web tasks.

The baseline is one isolated tenant per stack in a dedicated MyRetail production AWS account. The
Terraform deployment role is workload control-plane authority for that account and must not be used
in a shared account:

- three-AZ VPC with public ALB/NAT, private ECS and isolated database subnets;
- RDS PostgreSQL 18 Multi-AZ DB cluster with encryption, forced TLS, automated backup/PITR,
  deletion protection and PostgreSQL log export;
- a vault-locked daily AWS Backup snapshot and failure event in addition to native RDS PITR (AWS
  Backup continuous PITR is not supported for RDS Multi-AZ DB clusters);
- two-or-more private ECS/Fargate API and web replicas during the monitored smoke stage;
- private API discovery at `api.myretail.internal`; only the Next.js BFF is public through HTTPS;
- an explicit ALB `X-Forwarded-For` append boundary: the BFF forwards only the validated
  ALB-added client address, and the API trusts proxy metadata only from private application
  subnet peers reachable through the web security group;
- Secrets Manager containers for auth, application database, migration database and ERPNext
  credentials; secret values never enter Terraform variables or state;
- immutable, KMS-encrypted ECR repositories and CloudWatch/SNS operational signals;
- a separate external production-like ERPNext site/database supplied through HTTPS configuration.

Terraform and the AWS provider are exact-pinned in both roots. The S3 backend uses native
`use_lockfile = true`; deprecated DynamoDB locking is not used.

## Roots

- `bootstrap/` creates the versioned, encrypted S3 state bucket, its KMS key, the GitHub OIDC
  deployment role and all fixed runtime/service IAM roles. Run this root only with a trusted
  break-glass/admin identity. The deployment role can read/pass the exact pre-provisioned roles but
  cannot create, mutate or pass arbitrary IAM roles.
- `production/` creates the tenant stack. Copy `backend.hcl.example` and
  `terraform.tfvars.example` outside the repository, fill non-secret identifiers, and keep the
  resulting files out of Git.

## Controlled order

1. Apply `bootstrap/` with a trusted break-glass/admin identity. Record its state/KMS/OIDC outputs;
   do not create long-lived deployment access keys.
2. Configure a protected GitHub `production` environment with required reviewers. Set the dedicated
   account as `AWS_ACCOUNT_ID` and the exact bootstrap OIDC role as `AWS_ROLE_ARN`, then initialize
   `production/` with the private backend.
3. Apply `production/` with `traffic_enabled = false`. Placeholder image digests are allowed only
   for this zero-task bootstrap apply.
4. Build and push the API, migration, database-bootstrap and web images to the created ECR
   repositories. Resolve every image to its immutable `@sha256:` URI and re-apply.
5. Populate the four empty Secrets Manager containers out-of-band. Required JSON keys are:
   - `auth`: `auth_secret`, `auth_rate_limit_secret`;
   - `state-app`: `password`, `database_url` for `myretail_api`;
   - `state-migration`: `password`, `database_url` for `myretail_state_migrator`;
   - `erpnext`: `api_key`, `api_secret`, `erpnext_pos_user_map`,
     `erpnext_pos_credentials_map`, `pos_cashier_assignments`.
6. Run the one-shot database-bootstrap task. It connects through the RDS-managed master secret,
   creates the no-login owner plus separate migrator/application roles, and never logs passwords.
7. Run the migration task and require revision `20260718_06`, then run the application-role
   preflight task. Keep their stopped-task status and CloudWatch logs as release evidence.
8. Set `monitoring_enabled = true` and `runtime_enabled = true`. This starts two private API/web
   replicas while the public HTTPS listener continues to return a fixed `503`. Prove provider
   backup/PITR with an isolated restore, deliver/test every required alert, run production-like
   smoke/reconciliation against the separate ERPNext site, and validate the external Phase 6B.3
   evidence manifest in production mode.
9. Only after manual review, configure all four rotation Lambda ARNs, the manifest SHA256 and stable
   HTTPS approval URL. Then set `traffic_enabled = true`. Terraform rejects a cutover missing any
   of those inputs.

The evidence format and rollback boundary remain authoritative in
`docs/security/postgresql-production-cutover-evidence.md`. Before the first PostgreSQL write a
binary rollback is possible; after it, use PostgreSQL recovery/forward-fix only—never dual-write or
SQLite fallback.

## Local validation

```text
terraform fmt -recursive -check infra/aws
terraform -chdir=infra/aws/bootstrap init -backend=false -lockfile=readonly
terraform -chdir=infra/aws/bootstrap validate
terraform -chdir=infra/aws/production init -backend=false -lockfile=readonly
terraform -chdir=infra/aws/production validate
```

CI repeats these checks with Terraform 1.15.8 downloaded by an exact SHA256 checksum and also
builds/smoke-tests the production container targets.

The production backup vault enters immutable compliance mode after its three-day change window.
Review the 14-day minimum retention before the first apply; after the window closes, Terraform and
AWS administrators cannot shorten retention or remove the vault lock until protected recovery
points expire.

## Protected GitHub environment

The bootstrap root creates an OIDC provider and a deployment role whose subject is restricted to
`repo:<owner>/<repository>:environment:production`. Put its output in the protected `production`
environment as `AWS_ROLE_ARN`; configure required reviewers before allowing apply workflows.

The workflows intentionally separate duties:

- `AWS production Terraform` creates an immutable seven-day plan artifact containing the binary
  plan, human-readable review and provenance/digest metadata. Applying requires a separate workflow
  run, the successful plan run ID and another protected-environment approval. Apply verifies the
  source workflow/main commit/account/backend and artifact digest, renders the plan again, and never
  creates a replacement plan. The first zero-runtime plan may use placeholder digests; a
  runtime-enabled plan rejects them.
- `AWS production images` publishes all four Linux/amd64 artifacts under the commit SHA and retains
  a non-secret digest manifest as a GitHub artifact.
- `AWS production runtime bootstrap` initializes only missing secret versions, refuses partially
  initialized database credentials, runs role bootstrap, migration `20260718_06`, application-role
  preflight and the recovery monitor, and records the stopped ECS task ARNs in the job summary.

Required environment variables are `AWS_REGION`, `AWS_ROLE_ARN`, `TF_STATE_BUCKET`,
`TF_STATE_KMS_KEY_ARN`, `AVAILABILITY_ZONES_JSON`, `TENANT_ID`, `TENANT_SLUG`,
`WEB_DOMAIN_NAME`, `ROUTE53_ZONE_ID`, `CERTIFICATE_ARN`, `ERPNEXT_BASE_URL`,
`ERPNEXT_COMPANY`, `ERPNEXT_API_USER` and `ERPNEXT_POS_USER`. After the runtime workflow passes,
set `MONITORING_ENABLED=true`, create/review a plan with `runtime_enabled=true`, and apply that exact
plan by its run ID. Public traffic stays closed until a later independently reviewed plan.

The runtime workflow also requires protected secrets `ERPNEXT_API_KEY`, `ERPNEXT_API_SECRET`,
`ERPNEXT_POS_USER_MAP_JSON`, `ERPNEXT_POS_CREDENTIALS_MAP_JSON` and
`POS_CASHIER_ASSIGNMENTS_JSON`. The three JSON values must be objects. Cutover additionally requires
the four rotation Lambda ARN variables, `TRAFFIC_APPROVAL_URL`, and a base64-encoded validated
manifest in `PRODUCTION_EVIDENCE_MANIFEST_B64`.
