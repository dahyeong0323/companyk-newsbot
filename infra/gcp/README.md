# GCP migration runbook (not live)

This directory prepares an additive migration from the current **Railway live** job to Cloud Run Jobs. It does not deploy, execute, schedule, or cut over anything by itself. Railway remains the production and rollback runtime until the operator completes the cutover checklist.

## Architecture

```text
Cloud Scheduler (08:00 Asia/Seoul)
  -> Cloud Run Job (python -m companyk_newsbot.main)
  -> Google News RSS / existing frozen pipeline / Resend
  -> GCS state object with generation-precondition writes
```

`OPENAI_API_KEY` and `RESEND_API_KEY` are referenced from Secret Manager. The image is built once with Cloud Build into Artifact Registry and used by both jobs. State is stored through the GCS API, never through a GCS FUSE `STATE_DIR` mount.

## Application configuration

| Variable | Meaning |
| --- | --- |
| `STATE_BACKEND=filesystem` | Default; current Railway behavior. |
| `STATE_BACKEND=gcs` | Use `GcsJsonStateStore`. |
| `STATE_GCS_BUCKET` | Required with the GCS backend. |
| `STATE_GCS_OBJECT` | Optional object path; defaults to `newsbot_state.json`. |

GCS production and shadow use distinct objects:

```text
production/newsbot_state.json
shadow/newsbot_state.json
```

Each save uses the object generation observed by the previous load. A conflicting concurrent writer raises an error instead of silently overwriting delivery checkpoints or fingerprints.

## Prerequisites

Install and authenticate the Google Cloud CLI, select a billing-enabled project, and review the scripts before running them. No script contains credentials, API keys, recipient addresses, or an active scheduler by default.

## 1. Bootstrap reusable resources

```powershell
.\infra\gcp\bootstrap.ps1 -ProjectId <project-id>
```

This enables the required APIs and creates (if missing) the Docker Artifact Registry repository, runtime and scheduler service accounts, a regional Standard state bucket with uniform bucket-level access, and the two empty Secret Manager resources. It does **not** add secret versions, create Cloud Run Jobs, or create a Scheduler job.

Add secret values separately, without placing them in source control:

```powershell
gcloud secrets versions add OPENAI_API_KEY --data-file=<local-secret-file>
gcloud secrets versions add RESEND_API_KEY --data-file=<local-secret-file>
```

Keep only the currently required secret versions active where operationally possible.

## 2. Build and create/update unscheduled jobs

```powershell
.\infra\gcp\deploy.ps1 `
  -ProjectId <project-id> `
  -StateBucket companyk-newsbot-state-<project-id> `
  -ProductionRecipient <operator-supplied-recipient> `
  -EmailFrom <operator-supplied-sender>
```

The script builds one commit-tagged image, then updates two one-task Cloud Run Jobs with `max retries = 0`:

- `companyk-newsbot-shadow`: `RUN_MODE=full_shadow`, no production email, distinct shadow state object.
- `companyk-newsbot-prod`: `RUN_MODE=live`, production state object, but is never executed by this script.

`max retries = 0` avoids an automatic post-email retry if a process fails before state persistence. The production recipient and sender are mandatory operator parameters and are never hardcoded in this repository.

## 3. Run and inspect a shadow manually

```powershell
gcloud run jobs execute companyk-newsbot-shadow --region us-central1 --wait
gcloud run jobs executions list --job companyk-newsbot-shadow --region us-central1
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="companyk-newsbot-shadow"' --limit 100
gcloud storage ls -L gs://<bucket>/shadow/newsbot_state.json
```

Shadow pass criteria:

- execution succeeded;
- registry contains 155 companies;
- RSS coverage is healthy and no systemic RSS failure is reported;
- OpenAI auth/runtime and the pipeline complete successfully;
- production email sent is `false`;
- the shadow state object exists; and
- Railway production state is unchanged.

An isolated GCP RSS failure is not evidence that the model or state backend is faulty; investigate collection health separately rather than changing the frozen news pipeline.

## 4. Migrate the existing Railway state without resetting it

Export Railway's `/data/newsbot_state.json` using an operator-controlled method. Do not modify or delete the Railway source file. Validate and upload the exported copy:

```powershell
$env:PYTHONPATH = "src"
python .\tools\upload_state_to_gcs.py <exported-newsbot-state.json> `
  --bucket <bucket> --object production/newsbot_state.json
```

The utility refuses to overwrite an existing object unless `--force` is supplied, validates `RunState`, uses object-generation preconditions, and reports only fingerprint counts, the delivery checkpoint, and destination. Verify that the destination contains the last successful delivery checkpoint plus article and event fingerprints.

## 5. Explicit cutover only after validation

Before creating the GCP scheduler:

1. Run and validate the GCP full shadow.
2. Keep Railway production unchanged while validating.
3. Upload and inspect the production state copy in GCS.
4. Review the GCP production job configuration.
5. Disable the Railway cron manually.
6. Run `cutover.ps1` and explicitly type its confirmation phrase.
7. Observe the first 08:00 KST GCP execution, email arrival, and GCS state advancement.
8. Keep Railway available for rollback until GCP is stable.

```powershell
.\infra\gcp\cutover.ps1 -ProjectId <project-id>
```

The resulting scheduler uses `0 8 * * *` and `Asia/Seoul`, sends an authenticated Cloud Run Jobs v2 `:run` request, and sets zero retry attempts. Never allow both Railway and GCP to run an active 08:00 KST production schedule.

## Rollback

If the first GCP production run is not healthy, pause/delete the GCP scheduler and re-enable the pre-existing Railway schedule only after confirming it will not overlap. Do not reset the GCS or Railway state objects. Railway remains a valid runtime because `STATE_BACKEND` defaults to `filesystem`.

## Cost guardrails

This architecture is expected to be approximately **$0/month at the current daily workload only while it remains within Google Cloud free-tier limits**. It is not an unconditional free-cost claim. In `us-central1`, current published allowances include Cloud Run Jobs 240,000 vCPU-seconds and 450,000 GiB-seconds/month, Cloud Scheduler three free jobs per billing account, Cloud Storage 5 GB-month Standard plus 5,000 Class A and 50,000 Class B operations (in eligible US regions), Secret Manager six active versions and 10,000 access operations, and Artifact Registry 0.5 GiB-month storage. Network egress, image retention, and any usage beyond allowances must be checked continuously.

Keep one current application image where practical and inspect storage periodically:

```powershell
gcloud artifacts docker images list us-central1-docker.pkg.dev/<project-id>/companyk-newsbot --include-tags
gcloud storage du -s gs://<bucket>
```

Official pricing references: [Cloud Run](https://cloud.google.com/run/pricing), [Cloud Scheduler](https://cloud.google.com/scheduler/pricing), [Cloud Storage](https://cloud.google.com/storage/pricing), [Secret Manager](https://cloud.google.com/secret-manager/pricing), and [Artifact Registry](https://cloud.google.com/artifact-registry/pricing).
