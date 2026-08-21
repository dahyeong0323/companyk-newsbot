[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [Parameter(Mandatory = $true)] [string]$StateBucket,
    [Parameter(Mandatory = $true)] [string]$ProductionRecipient,
    [Parameter(Mandatory = $true)] [string]$EmailFrom,
    [ValidateSet("resend", "gmail")] [string]$EmailProvider = "gmail",
    [string]$Region = "us-central1",
    [string]$ArtifactRepository = "companyk-newsbot",
    [string]$ImageTag = "",
    [string]$RuntimeServiceAccount = "companyk-newsbot-runtime"
)

$ErrorActionPreference = "Stop"
if (-not $ImageTag) { $ImageTag = (git rev-parse --short=12 HEAD).Trim() }
if ($ProductionRecipient.Contains("|") -or $EmailFrom.Contains("|")) {
    throw "ProductionRecipient and EmailFrom must not contain the reserved env-var delimiter |"
}
$image = "$Region-docker.pkg.dev/$ProjectId/$ArtifactRepository/companyk-newsbot:$ImageTag"
$runtimeEmail = "$RuntimeServiceAccount@$ProjectId.iam.gserviceaccount.com"
$shared = @(
    "ROUTE_B_ENABLED=false",
    "PORTFOLIO_REGISTRY_PATH=config/portfolio_registry.yaml",
    "DIRECT_EVENT_MODEL=gpt-5.6-luna",
    "DIRECT_EVENT_REASONING=low",
    "DIRECT_GROUNDING_MODEL=gpt-5.6-luna",
    "DIRECT_GROUNDING_REASONING=low",
    "RSS_MIN_SUCCESS_RATIO=0.90",
    "STATE_BACKEND=gcs",
    "STATE_GCS_BUCKET=$StateBucket",
    "EMAIL_PROVIDER=$EmailProvider"
)
$shadowEnv = "^|^" + (($shared + @(
    "RUN_MODE=full_shadow",
    "PRODUCTION_EMAIL_ENABLED=false",
    "SHADOW_TEST_EMAIL=false",
    "STATE_GCS_OBJECT=shadow/newsbot_state.json",
    "ARTIFACT_DIR=/tmp/artifacts"
)) -join "|")
$productionEnv = "^|^" + (($shared + @(
    "RUN_MODE=live",
    "PRODUCTION_EMAIL_ENABLED=true",
    "STATE_GCS_OBJECT=production/newsbot_state.json",
    "NEWSBOT_RECIPIENT=$ProductionRecipient",
    "EMAIL_FROM=$EmailFrom"
)) -join "|")
$secrets = if ($EmailProvider -eq "gmail") {
    "OPENAI_API_KEY=OPENAI_API_KEY:latest,GMAIL_CLIENT_ID=GMAIL_CLIENT_ID:latest,GMAIL_CLIENT_SECRET=GMAIL_CLIENT_SECRET:latest,GMAIL_REFRESH_TOKEN=GMAIL_REFRESH_TOKEN:latest"
} else {
    "OPENAI_API_KEY=OPENAI_API_KEY:latest,RESEND_API_KEY=RESEND_API_KEY:latest"
}

gcloud config set project $ProjectId | Out-Null
gcloud builds submit --tag $image
gcloud run jobs deploy companyk-newsbot-shadow --image $image --region $Region --service-account $runtimeEmail --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 30m --set-env-vars $shadowEnv --set-secrets $secrets
gcloud run jobs deploy companyk-newsbot-prod --image $image --region $Region --service-account $runtimeEmail --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 30m --set-env-vars $productionEnv --set-secrets $secrets

Write-Host "Shadow and production jobs were updated but not executed. No Scheduler job was created."
