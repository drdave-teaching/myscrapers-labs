# GCP + GitHub Actions Setup Guide
## craigslist-scraper deployment -- drdww

**Dr. Dave Wanik** -- Dept. Operations and Information Management -- University of Connecticut

This documents the exact steps used to deploy the myscrapers pipeline to GCP.
Follow in order -- each step builds on the last.

> Open [Cloud Shell](https://shell.cloud.google.com) and paste each block.
> GCP account: dww05002.s26.bittbridge@gmail.com
> Project: craigslist-scraper-499015

---

## Quick Reference -- GitHub Repository Variables

Set these at: **Settings → Secrets and variables → Actions → Variables tab**

| Variable | Value |
|---|---|
| `PROJECT_ID` | `craigslist-scraper-499015` |
| `REGION` | `us-central1` |
| `BUCKET_NAME` | `craigslist-scraper-bucket` |
| `RUNTIME_SA` | `cf-runtime@craigslist-scraper-499015.iam.gserviceaccount.com` |
| `DEPLOYER_SA` | `cf-deployer@craigslist-scraper-499015.iam.gserviceaccount.com` |
| `WORKLOAD_IDENTITY_PROVIDER` | `projects/598481111020/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider` |
| `FUNCTION_NAME` | `listing-scraper` |
| `FUNCTION_DIR` | `cloud_function/scraper_cars` |
| `BASE_SITE` | `https://newhaven.craigslist.org` |
| `SEARCH_PATH` | `/search/cto` |
| `USER_AGENT` | `Mozilla/5.0 (compatible; research-bot/1.0)` |

---

## Step 1 -- Variables (run this first)

```bash
PROJECT_ID="craigslist-scraper-499015"
GITHUB_REPO="drdww/myscrapers-labs"

# --- auto-derived ---
REGION="us-central1"
BUCKET_NAME="craigslist-scraper-bucket"
RUNTIME_SA_ID="cf-runtime"
DEPLOYER_SA_ID="cf-deployer"
SCHED_SA_ID="cf-scheduler"
WIF_POOL="gh-pool"
WIF_PROVIDER="gh-provider"
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_SA="${DEPLOYER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="${SCHED_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}"
SCHEDULER_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
echo "Ready. Project: ${PROJECT_ID} (${PROJECT_NUMBER})"
# Expected: Ready. Project: craigslist-scraper-499015 (598481111020)
```

---

## Step 2 -- Enable APIs

```bash
gcloud services enable \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudbuild.googleapis.com \
  eventarc.googleapis.com \
  storage.googleapis.com \
  serviceusage.googleapis.com \
  cloudresourcemanager.googleapis.com \
  pubsub.googleapis.com \
  logging.googleapis.com \
  compute.googleapis.com \
  aiplatform.googleapis.com

echo "APIs enabled."
```

---

## Step 3 -- Create Service Accounts

```bash
gcloud iam service-accounts create "${RUNTIME_SA_ID}"  --display-name="Cloud Functions Runtime" || true
gcloud iam service-accounts create "${DEPLOYER_SA_ID}" --display-name="GitHub Deployer" || true
gcloud iam service-accounts create "${SCHED_SA_ID}"    --display-name="Scheduler Invoker" || true

gcloud iam service-accounts list
```

---

## Step 4 -- Workload Identity Federation (WIF)

GitHub Actions authenticates to GCP without any stored JSON keys.

```bash
# 4a - Create WIF pool
gcloud iam workload-identity-pools create "${WIF_POOL}" \
  --location="global" \
  --display-name="GitHub Actions Pool" || true

# 4b - Create OIDC provider
gcloud iam workload-identity-pools providers create-oidc "${WIF_PROVIDER}" \
  --workload-identity-pool="${WIF_POOL}" \
  --location="global" \
  --display-name="GitHub OIDC Provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="attribute.repository=='${GITHUB_REPO}' && attribute.ref=='refs/heads/master'" || true

# 4c - Allow GitHub Actions to impersonate deployer SA
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="${PRINCIPAL_SET}"

echo "WIF done."
```

> **Note:** attribute-condition uses `refs/heads/master` (not main) -- this repo's default branch is master.

---

## Step 5 -- Deployer SA Roles

```bash
for ROLE in \
  roles/cloudfunctions.developer \
  roles/run.admin \
  roles/cloudscheduler.admin \
  roles/artifactregistry.writer \
  roles/serviceusage.serviceUsageAdmin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${DEPLOYER_SA}" \
      --role="${ROLE}"
done

echo "Deployer roles done."
```

---

## Step 6 -- Service Account Relationships

```bash
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountUser"

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountAdmin"

gcloud iam service-accounts add-iam-policy-binding \
  "projects/-/serviceAccounts/${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountUser"

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${SCHEDULER_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator"

gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${SCHED_SA}" \
  --role="roles/iam.serviceAccountTokenCreator"

echo "SA relationships done."
```

---

## Step 7 -- Create Bucket

```bash
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}"
  echo "Bucket created."
else
  echo "Bucket already exists, skipping."
fi

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/storage.objectAdmin"

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/storage.objectViewer"

echo "Bucket done."
```

---

## Step 8 -- Vertex AI Access

```bash
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user"

echo "Vertex AI done."
```

---

## Step 9 -- Deploy Workflows (in order)

Go to **Actions** tab and trigger each workflow manually via **Run workflow**:

1. `deploy-extractor.yml`
2. `deploy-materialize-master.yml`
3. `deploy-train-dt.yml`
4. `deploy.yml` -- the scraper
5. `deploy-extractor-llm.yml` -- Vertex AI / Gemini

All should show green. If WIF fails, check that the attribute-condition branch matches your default branch.

---

## Notes from First Deploy (June 2026)

- WIF initially failed because template used `refs/heads/main` but repo default branch is `master` -- fixed in Step 4b above
- `sync-data.yml` workflow was deleted -- it ran hourly, tried to push to `main` (wrong branch), caused noise
- `materialize-master/main.py` updated to incremental append (only scans last 75 min) instead of full rescan
- Free trial: $300 credit, used ~$65 in setup/first deploys, expires July 7 2026

---

## Architecture

```
GitHub Actions (OIDC / master branch)
        |
        |  WIF -- no JSON keys
        v
  cf-deployer SA
        |
        |-- deploys Cloud Functions Gen2
        |-- creates Cloud Scheduler jobs
        |
        v
  cf-runtime SA
        |
        |-- scraper writes raw listings to GCS
        |-- extractor parses regex fields
        |-- extractor-llm calls Vertex AI (Gemini)
        |-- materialize-master appends to listings_master.csv
        |-- train-dt trains decision tree on master CSV
        |
  Cloud Scheduler (hourly cron)
        |
        v
  Cloud Function HTTP endpoints
```
