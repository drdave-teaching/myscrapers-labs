# GCP Concepts Explainer
## What is all this stuff, and why does it matter?

**Dr. Dave Wanik** -- Dept. Operations and Information Management -- University of Connecticut

This companion to `GCP_SETUP.md` explains the *why* behind each piece of infrastructure.
You don't need to memorize this -- just read it once so the setup steps make sense.

---

## The Big Picture

You are building a **production data pipeline** that runs automatically in the cloud.
No laptop needs to be open. No one needs to press a button. It just runs.

Here is what happens every hour, automatically:

```
Craigslist website
      |
      | (scraper Cloud Function fetches listings)
      v
Google Cloud Storage (raw .txt files)
      |
      | (extractor Cloud Function parses fields)
      v
listings_master.csv  (in Cloud Storage)
      |
      | (train-dt Cloud Function trains ML model)
      v
champion vs challenger comparison.csv
```

Every piece of infrastructure below exists to make one part of that chain work safely and automatically.

---

## Service Accounts -- "Robot Identities"

A **service account** is an identity for a program, not a person.

When you log into GCP, you use your Google account (your email + password).
But when a Cloud Function runs automatically at 2am, there is no human to log in.
So GCP uses a service account -- a fake "robot" Google account that programs can use.

We create three:

| Service Account | Job |
|---|---|
| `cf-runtime` | The identity Cloud Functions run as. Has permission to read/write Cloud Storage. |
| `cf-deployer` | The identity GitHub Actions uses to push new code to GCP. Has permission to deploy functions. |
| `cf-scheduler` | The identity Cloud Scheduler uses to call (invoke) Cloud Functions on a timer. |

**Why three?** Least-privilege. Each robot only has the permissions it needs for its job.
If someone steals the scheduler's credentials, they can only trigger functions -- not deploy new code or read your data.

---

## IAM -- "The Permission System"

**IAM** stands for Identity and Access Management. It is GCP's system for answering the question:
*"Is this identity allowed to do this thing?"*

Every action in GCP goes through IAM. Examples:

- Can `cf-runtime` write a file to Cloud Storage? (Yes -- we grant it `roles/storage.objectAdmin`)
- Can `cf-deployer` deploy a new Cloud Function? (Yes -- we grant it `roles/cloudfunctions.developer`)
- Can a random person on the internet delete your bucket? (No -- they have no IAM role)

**Roles** are bundles of permissions. Instead of granting 50 individual permissions, you grant one role like `roles/storage.objectAdmin` which includes all the storage permissions a runtime needs.

---

## Workload Identity Federation (WIF) -- "Passwordless Login for GitHub"

This is the trickiest concept but also the most important for security.

**The problem:** GitHub Actions needs to deploy code to GCP. How does GitHub prove to GCP that it is *your* GitHub repo and not some random attacker?

**The bad old way:** Create a JSON key file for a service account, download it, paste it into GitHub Secrets. Works, but that key file is a password that never expires and could be leaked.

**WIF -- the modern way:** Instead of a password, GitHub proves its identity using a short-lived cryptographic token (OIDC). Here is the flow:

```
GitHub Actions starts a workflow run
      |
      | "I am github.com/drdww/myscrapers-labs, branch master.
      |  Here is a signed token from GitHub proving it."
      v
GCP Workload Identity Pool
      | (checks: is this token from GitHub? is it from the right repo and branch?)
      v
GCP grants a temporary access token (expires in ~1 hour)
      |
      v
GitHub Actions uses that token to deploy Cloud Functions
```

No password file. No long-lived credentials. If the token leaks, it expires in an hour anyway.

**Why we specify the branch (`refs/heads/master`):** We only want deployments from the `master` branch -- not from someone's experimental branch or a pull request. The attribute-condition enforces this.

---

## Cloud Functions -- "Code That Runs on Demand"

A **Cloud Function** is a piece of Python code that GCP runs for you. You do not manage any servers.

You give GCP:
- Your Python file
- A `requirements.txt`
- An entry point (function name)

GCP gives you back an HTTPS URL. When that URL is called, your function runs.

We use **Gen2** Cloud Functions, which run on top of Cloud Run. They can run longer (up to 60 minutes) and handle more memory than Gen1.

---

## Cloud Storage (GCS) -- "Google's S3"

**Cloud Storage** is Google's object storage -- like a hard drive in the cloud, accessible from anywhere.

We use one **bucket** (a top-level folder) with this structure:

```
craigslist-scraper-bucket/
  scrapes/                  <- raw .txt files from the scraper
  structured/
    datasets/               <- listings_master.csv (all parsed listings)
    preds/                  <- model predictions, organized by date/hour
  models/                   <- champion .pkl files (trained ML models)
```

Cloud Functions read and write from this bucket. This is how they pass data to each other
without being directly connected.

---

## Cloud Scheduler -- "Cron Jobs in the Cloud"

**Cloud Scheduler** is like a cron job (a timer). You give it a schedule and a URL,
and it calls that URL on schedule.

We use it to call each Cloud Function every hour (or every few hours for training).
The scheduler uses the `cf-scheduler` service account to authenticate its calls so
the functions don't accept requests from just anyone.

---

## GitHub Actions -- "Automated Deployment"

**GitHub Actions** is GitHub's built-in CI/CD system. A **workflow** is a YAML file that
describes a series of steps to run automatically when something happens in the repo
(like a push to master).

Our workflows do this:
1. Authenticate to GCP (via WIF)
2. Package the Python code
3. Deploy it as a Cloud Function
4. Create/update the Cloud Scheduler job
5. Run a smoke test to verify it works

This means every time you push code to the `master` branch, GCP automatically gets the new version.
No manual uploads, no copy-pasting.

---

## Artifact Registry -- "Where Docker Images Live"

Cloud Functions Gen2 package your code as a **Docker container** before running it.
**Artifact Registry** is where GCP stores those container images.

You never interact with it directly -- the deploy process handles it automatically.
We just need to enable the API and give the deployer permission to write there.

---

## Putting It All Together

Here is the full trust chain for a deployment:

```
You push code to drdww/myscrapers-labs (master branch)
      |
      v
GitHub Actions workflow triggers
      |
      | WIF: GitHub proves identity to GCP
      v
GCP grants temporary token to cf-deployer
      |
      | cf-deployer builds container, pushes to Artifact Registry
      | cf-deployer deploys new Cloud Function version
      | cf-deployer updates Cloud Scheduler job
      v
Cloud Function is live at its HTTPS URL
      |
      | Every hour: Cloud Scheduler calls the URL
      | using cf-scheduler identity
      v
Cloud Function runs as cf-runtime
      | reads/writes Cloud Storage
      v
Data pipeline runs automatically, forever
```

---

## Common Questions

**Q: Why not just use my personal Google account for everything?**
A: Your personal account has admin access to everything. If a function is hacked or misbehaves,
it could delete your entire project. Service accounts with minimal permissions limit the blast radius.

**Q: What does "enable APIs" actually do?**
A: GCP services are off by default to save money and reduce attack surface. Enabling an API
turns on that service for your project and allows billing for it.

**Q: Why does the WIF setup care about the branch name?**
A: So that only code merged to `master` gets deployed to production. A pull request or
feature branch should not be able to overwrite your live Cloud Functions.

**Q: What happens if a Cloud Function crashes?**
A: GCP logs the error in Cloud Logging and Error Reporting. The next scheduled run will try again.
Nothing permanently breaks -- the old data in Cloud Storage is still there.
