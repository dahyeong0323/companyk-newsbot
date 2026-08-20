[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [string]$Region = "us-central1",
    [string]$SchedulerServiceAccount = "companyk-newsbot-scheduler",
    [string]$SchedulerJob = "companyk-newsbot-production-0800-kst",
    [string]$ProductionJob = "companyk-newsbot-prod"
)

$ErrorActionPreference = "Stop"
Write-Host "Confirm before proceeding:"
Write-Host "[ ] GCP full shadow, RSS coverage, OpenAI calls, and GCS shadow state passed"
Write-Host "[ ] Railway production state has been copied and inspected in the GCS production object"
Write-Host "[ ] Production job configuration is reviewed and Railway cron will be disabled manually before activation"
if ((Read-Host "Type CREATE_GCP_SCHEDULER to continue") -cne "CREATE_GCP_SCHEDULER") {
    throw "Cutover cancelled; no scheduler was created."
}
$schedulerEmail = "$SchedulerServiceAccount@$ProjectId.iam.gserviceaccount.com"
$uri = "https://run.googleapis.com/v2/projects/$ProjectId/locations/$Region/jobs/$ProductionJob:run"
gcloud config set project $ProjectId | Out-Null
if (gcloud scheduler jobs describe $SchedulerJob --location $Region 2>$null) {
    gcloud scheduler jobs update http $SchedulerJob --location $Region --schedule "0 8 * * *" --time-zone "Asia/Seoul" --uri $uri --http-method POST --oauth-service-account-email $schedulerEmail --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" --max-retry-attempts 0
} else {
    gcloud scheduler jobs create http $SchedulerJob --location $Region --schedule "0 8 * * *" --time-zone "Asia/Seoul" --uri $uri --http-method POST --oauth-service-account-email $schedulerEmail --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform" --max-retry-attempts 0
}
Write-Host "Scheduler created/updated. Verify Railway cron was disabled manually; never leave two active 08:00 KST schedulers."
