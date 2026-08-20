[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [string]$Region = "us-central1",
    [string]$ArtifactRepository = "companyk-newsbot",
    [string]$StateBucket = "",
    [string]$RuntimeServiceAccount = "companyk-newsbot-runtime",
    [string]$SchedulerServiceAccount = "companyk-newsbot-scheduler"
)

$ErrorActionPreference = "Stop"
if (-not $StateBucket) { $StateBucket = "companyk-newsbot-state-$ProjectId" }
$runtimeEmail = "$RuntimeServiceAccount@$ProjectId.iam.gserviceaccount.com"
$schedulerEmail = "$SchedulerServiceAccount@$ProjectId.iam.gserviceaccount.com"

gcloud config set project $ProjectId | Out-Null
foreach ($api in "run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com", "storage.googleapis.com") {
    gcloud services enable $api
}
if (-not (gcloud artifacts repositories describe $ArtifactRepository --location $Region 2>$null)) {
    gcloud artifacts repositories create $ArtifactRepository --repository-format docker --location $Region --description "Company K Newsbot images"
}
foreach ($account in @($RuntimeServiceAccount, $SchedulerServiceAccount)) {
    if (-not (gcloud iam service-accounts describe "$account@$ProjectId.iam.gserviceaccount.com" 2>$null)) {
        gcloud iam service-accounts create $account --display-name $account
    }
}
if (-not (gcloud storage buckets describe "gs://$StateBucket" 2>$null)) {
    gcloud storage buckets create "gs://$StateBucket" --location $Region --default-storage-class STANDARD --uniform-bucket-level-access
}
gcloud storage buckets add-iam-policy-binding "gs://$StateBucket" --member "serviceAccount:$runtimeEmail" --role "roles/storage.objectUser"
foreach ($secret in "OPENAI_API_KEY", "RESEND_API_KEY") {
    if (-not (gcloud secrets describe $secret 2>$null)) { gcloud secrets create $secret --replication-policy automatic }
    gcloud secrets add-iam-policy-binding $secret --member "serviceAccount:$runtimeEmail" --role "roles/secretmanager.secretAccessor"
}
gcloud projects add-iam-policy-binding $ProjectId --member "serviceAccount:$schedulerEmail" --role "roles/run.invoker"

Write-Host "Bootstrap complete. No secret values, Cloud Run jobs, or Scheduler jobs were created."
Write-Host "Add exactly one current version to OPENAI_API_KEY and RESEND_API_KEY separately; do not paste secrets into this script."
