# GCP + GitHub Actions Setup Guide

**Dr. Dave Wanik and Rohit Akole** -- Dept. Operations and Information Management -- University of Connecticut

This guide walks you through setting up GCP permissions, service accounts, and GitHub Actions CI/CD
for deploying the myscrapers pipeline. Follow the sections **in order** -- each one builds on the last.

> **Before starting:** open [Cloud Shell](https://shell.cloud.google.com) in your GCP console (top-right terminal icon).
> Copy/paste each block into Cloud Shell and run it.

---

## Quick Reference -- GitHub Repository Variables

Once you finish this guide, you'll need to add these to your forked repo under
**Settings --> Secrets and variables --> Actions --> Variables tab**.

| Variable | What it is | Example |
|---|---|---|
| `PROJECT_ID` | Your GCP project ID | `jdoe-scraper-v1` |
| `REGION` | GCP region | `us-central1` |
| `BUCKET_NAME` | GCS bucket name (globally unique) | `jdoe-scraper-bucket` |
| `RUNTIME_SA` | Runtime SA email (full) | `cf-runtime@jdoe-scraper-v1.iam.gserviceaccount.com` |
| `DEPLOYER_SA` | Deployer SA email (full) | `cf-deployer@jdoe-scraper-v1.iam.gserviceaccount.com` |
| `WORKLOAD_IDENTITY_PROVIDER` | Full WIF provider resource name | `projects/123456/locations/global/workloadIdentityPools/gh-pool/providers/gh-provider` |
| `FUNCTION_NAME` | Your scraper Cloud Function name | `listing-scraper` |
| `FUNCTION_DIR` | Path to function source in repo | `cloud_function/scraper_cars` |
| `BASE_SITE` | Base URL of the site to scrape | `https://newhaven.craigslist.org` |
| `SEARCH_PATH` | Search path on that site | `/search/cto` |
| `USER_AGENT` | Browser user-agent string | `Mozilla/5.0 (compatible; research-bot/1.0)` |

Optional overrides (workflows have sensible defaults if not set):

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `vertex` | LLM backend (`vertex` or `anthropic`) |
| `LLM_MODEL` | `gemini-2.5-flash` | Model for LLM extraction |
| `LLM_OVERWRITE` | `false` | Re-extract already-processed listings |
| `LLM_MAX_FILES` | `0` (all) | Max listings per LLM run |
| `STRUCTURED_PREFIX` | `structured` | GCS folder prefix for LLM output |
| `SCHEDULE_BODY` | `{"pages":1,"max":3}` | JSON body sent to the scraper on each scheduled run |
| `EXTRACTOR_LLM_BODY` | `{"overwrite":false,"max_files":0}` | JSON body for the LLM extractor scheduler |
| `MATERIALIZE_BODY` | `{}` | JSON body for the materialize scheduler |
| `TRAIN_DT_BODY` | `{"dry_run":true}` | JSON body for the train-dt scheduler |

---

## Step 1 -- Variables (run this first, everything else uses these)

Open Cloud Shell and paste this block. **Only edit the top section.**

```bash
# ==============================================================
# EDITABLE -- fill these in, then run this block
# ==============================================================
PROJECT_ID="YOUR_PROJECT_ID"                   # e.g. "jdoe-scraper-v1"
REGION="us-central1"                           # or nearest region e.g. us-east1
BUCKET_NAME="YOUR_BUCKET_NAME"                 # must be globally unique
GITHUB_REPO="YOUR_GITHUB_USERNAME/YOUR_REPO"   # e.g. "jdoe/myscrapers-labs"

# Scraper config -- also set these as GitHub Repository Variables
BASE_SITE="https://newhaven.craigslist.org"    # base URL of the site to scrape
SEARCH_PATH="/search/cto"                      # cto=cars+trucks, apa=apartments, etc.
USER_AGENT="Mozilla/5.0 (compatible; research-bot/1.0)"
FUNCTION_NAME="listing-scraper"                # your GCP Cloud Function name
FUNCTION_DIR="cloud_function/scraper_cars"     # path to function source in the repo

# ==============================================================
# AUTO-DERIVED -- do not edit below this line
# ==============================================================

# Service account name fragments (just the IDs, not full emails)
RUNTIME_SA_ID="cf-runtime"      # was RUNTIME_SA_ID in original setup
DEPLOYER_SA_ID="cf-deployer"    # was DEPLOYER_SA_ID
SCHED_SA_ID="cf-scheduler"      # was SCHED_SA_ID

# WIF pool and provider names
WIF_POOL="gh-pool"              # was WIF_POOL
WIF_PROVIDER="gh-provider"      # was WIF_PROVIDER

# Full service account emails (used in gcloud commands and as GitHub Variables)
RUNTIME_SA="${RUNTIME_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_SA="${DEPLOYER_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="${SCHED_SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

# Project number (needed for WIF principal set and compute SA references)
# In GitHub Actions workflows this is derived at runtime via:
#   gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)'
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")

# Workload Identity principal set -- links your GitHub repo to GCP
# This becomes the --member in the WIF binding (Step 4c)
PRINCIPAL_SET="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_REPO}"

# Cloud Scheduler's managed service agent (auto-created by GCP, not by you)
# Used in Step 6 to grant TokenCreator so Scheduler can invoke your functions
# In the deploy.yml workflow this is derived as:
#   SCHED_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"
SCHEDULER_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com"

echo "Variables set for project: ${PROJECT_ID} (${PROJECT_NUMBER})"
```

---

## Step 2 -- Enable Required APIs

Safe to re-run -- gcloud skips APIs that are already enabled.

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

Three service accounts handle different roles:
- **cf-runtime** (`RUNTIME_SA`) -- the identity Cloud Functions run as; used in `--service-account` when deploying
- **cf-deployer** (`DEPLOYER_SA`) -- the identity GitHub Actions impersonates via WIF to deploy
- **cf-scheduler** (`SCHED_SA`) -- used by Cloud Scheduler to invoke functions (some workflows derive this automatically)

```bash
gcloud iam service-accounts create "${RUNTIME_SA_ID}"  --display-name="Cloud Functions Runtime" || true
gcloud iam service-accounts create "${DEPLOYER_SA_ID}" --display-name="GitHub Deployer" || true
gcloud iam service-accounts create "${SCHED_SA_ID}"    --display-name="Scheduler Invoker" || true

# Verify
gcloud iam service-accounts list
```

---

## Step 4 -- Workload Identity Federation (WIF)

This is how GitHub Actions authenticates to GCP **without any stored JSON keys**.
GCP trusts GitHub's OIDC token directly. Three sub-steps -- run them in order.

### 4a -- Create the WIF pool (once per project)

```bash
gcloud iam workload-identity-pools create "${WIF_POOL}" \
  --location="global" \
  --display-name="GitHub Actions Pool" || true
```

### 4b -- Create the OIDC provider (links GitHub to this pool)

```bash
gcloud iam workload-identity-pools providers create-oidc "${WIF_PROVIDER}" \
  --workload-identity-pool="${WIF_POOL}" \
  --location="global" \
  --display-name="GitHub OIDC Provider" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
  --attribute-condition="attribute.repository=='${GITHUB_REPO}' && attribute.ref=='refs/heads/main'" || true
```

### 4c -- Allow GitHub Actions (for your repo) to impersonate the deployer SA

```bash
# PRINCIPAL_SET was defined in Step 1 -- it encodes your GitHub repo into a GCP identity
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_SA}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="${PRINCIPAL_SET}"
```

> **Tip:** To get the full `WORKLOAD_IDENTITY_PROVIDER` string you need for the GitHub Variable, run:
> ```bash
> echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"
> ```
> Copy that output and paste it as your `WORKLOAD_IDENTITY_PROVIDER` repo variable.

---

## Step 5 -- Project-Level Roles for the Deployer SA

The deployer SA needs these roles to deploy Cloud Functions, manage Artifact Registry
images, manage Schedulers, and enable APIs.

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

echo "Project roles granted to ${DEPLOYER_SA}"
```

---

## Step 6 -- Service Account Relationships

These bindings let the pieces talk to each other. In the deploy workflows, some of these
are re-applied automatically (idempotent) as part of the "Bootstrap IAM for Scheduler OIDC" step.

```bash
# Allow deployer to deploy functions that run AS the runtime SA
# (used in deploy.yml: --service-account="${RUNTIME_SA}")
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountUser"

# Allow deployer to administer the runtime SA
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountAdmin"

# Allow deployer to act as the default compute SA (needed for some Cloud Run operations)
gcloud iam service-accounts add-iam-policy-binding \
  "projects/-/serviceAccounts/${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/iam.serviceAccountUser"

# Allow Cloud Scheduler's managed agent (SCHEDULER_AGENT) to mint OIDC tokens for the runtime SA
# This is what lets Scheduler actually invoke your Cloud Functions on a cron schedule
# In deploy.yml this is SCHED_AGENT, derived as:
#   service-${PROJECT_NUMBER}@gcp-sa-cloudscheduler.iam.gserviceaccount.com
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${SCHEDULER_AGENT}" \
  --role="roles/iam.serviceAccountTokenCreator"

# Also allow your explicit scheduler SA (SCHED_SA) the same token creator role
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_SA}" \
  --member="serviceAccount:${SCHED_SA}" \
  --role="roles/iam.serviceAccountTokenCreator"

echo "SA relationships configured."
```

---

## Step 7 -- Create Bucket and Grant Access

The scraper writes raw listing text here. The extractor, materializer, and trainer all read from it.
The deployer SA also needs read access so the `sync-data` workflow can push results back to GitHub.

```bash
# Create the bucket if it does not already exist
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}"
  echo "Bucket gs://${BUCKET_NAME} created."
else
  echo "Bucket gs://${BUCKET_NAME} already exists, skipping."
fi

# Grant runtime SA full object access (read + write)
# Referenced in deploy-extractor-llm.yml as: roles/storage.objectAdmin on gs://${BUCKET_NAME}
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/storage.objectAdmin"

# Grant deployer SA read access (needed for sync-data.yml to push results to GitHub)
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${DEPLOYER_SA}" \
  --role="roles/storage.objectViewer"

echo "Bucket permissions set."
```

---

## Step 8 -- Vertex AI Permissions (for LLM Extractor)

The `extractor-llm-poc` Cloud Function calls Vertex AI (Gemini) to extract structured fields
from raw listing text. The runtime SA needs the Vertex AI User role.

```bash
# Referenced in deploy-extractor-llm.yml as: roles/aiplatform.user on PROJECT_ID
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/aiplatform.user"

echo "Vertex AI User role granted to ${RUNTIME_SA}"

# Extend Cloud Run timeout for the LLM function -- LLM calls on large batches can be slow
# Note: REGION used here, not hardcoded us-central1
gcloud run services update extractor-llm-poc \
  --region="${REGION}" \
  --timeout="3600s"
```

---

## Step 9 -- Verify Everything

Run these to confirm all pieces are in place before deploying.

```bash
# List service accounts (should see cf-runtime, cf-deployer, cf-scheduler)
gcloud iam service-accounts list

# Confirm WIF pool and provider exist
gcloud iam workload-identity-pools list --location=global
gcloud iam workload-identity-pools providers list \
  --workload-identity-pool="${WIF_POOL}" --location=global

# Check IAM bindings for the deployer SA on the project
gcloud projects get-iam-policy "${PROJECT_ID}" | grep "${DEPLOYER_SA}" -A3

# Check IAM bindings on the runtime SA (should see tokenCreator, serviceAccountUser)
gcloud iam service-accounts get-iam-policy "${RUNTIME_SA}"
```

---

## Step 10 -- Add Variables to GitHub

Go to your forked repo --> **Settings** --> **Secrets and variables** --> **Actions** --> **Variables tab**

Run this copy/paste helper in Cloud Shell (after running Step 1) to print the exact values:

```bash
echo "--- Copy these into GitHub Repository Variables ---"
echo "WORKLOAD_IDENTITY_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"
echo "DEPLOYER_SA=${DEPLOYER_SA}"
echo "RUNTIME_SA=${RUNTIME_SA}"
echo "PROJECT_ID=${PROJECT_ID}"
echo "REGION=${REGION}"
echo "BUCKET_NAME=${BUCKET_NAME}"
echo "FUNCTION_NAME=${FUNCTION_NAME}"
echo "FUNCTION_DIR=${FUNCTION_DIR}"
echo "BASE_SITE=${BASE_SITE}"
echo "SEARCH_PATH=${SEARCH_PATH}"
echo "USER_AGENT=${USER_AGENT}"
```

---

## Step 11 -- Deploy and Test

Push to `main` to trigger the GitHub Actions workflows. **Start with one at a time** to
make it easier to debug if something goes wrong.

Recommended order:
1. `.github/workflows/deploy-extractor.yml` -- simplest, good first test
2. `.github/workflows/deploy-materialize-master.yml`
3. `.github/workflows/deploy-train-dt.yml`
4. `.github/workflows/deploy.yml` -- the scraper itself
5. `.github/workflows/deploy-extractor-llm.yml` -- needs Vertex AI (Step 8)

Confirm in GCP Console, or run:

```bash
gcloud functions list --regions="${REGION}"
gcloud scheduler jobs list --location="${REGION}"

# Expected scheduler jobs after all deploys (job name = function name + -hourly):
# extractor-per-listing-hourly
# materialize-master-hourly
# train-dt-hourly
# listing-scraper-hourly   <-- matches your FUNCTION_NAME variable
```

---

## Architecture Summary

```
GitHub Actions (OIDC)
        |
        |  WIF -- no JSON keys needed
        v
  cf-deployer SA                     (vars.DEPLOYER_SA in all workflows)
        |
        |-- deploys Cloud Functions Gen2
        |-- creates/updates Cloud Scheduler jobs
        |-- reads from GCS bucket (sync-data workflow)
        |
        v
  cf-runtime SA                      (vars.RUNTIME_SA in all workflows)
        |
        |-- executes function logic
        |-- reads/writes GCS bucket
        |-- calls Vertex AI (LLM extractor)
        |
  Cloud Scheduler
        |
        |  OIDC token via SCHEDULER_AGENT (TokenCreator on cf-runtime)
        v
  Cloud Function HTTP endpoint        (invoked hourly per CRON_EXPR in each workflow)
```

Results from GCS are synced back to the `results/` folder in your GitHub repo
by the `sync-data.yml` workflow so you can see model predictions without opening the GCP console.

---

## For Your Project

Swap out `BASE_SITE` and `SEARCH_PATH` to scrape something other than cars.
The entire pipeline -- scraper, extractor, materializer, trainer -- works the same way
for apartments, jobs, furniture, or any other listings site.

Good luck, and reach out to Dr. Dave if you get stuck!
