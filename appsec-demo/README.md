# AppSec review demo

A self-contained proof of concept for an application security review programme. Open
`index.html` (live at `/appsec-demo/` on the hub) and follow the five-minute walkthrough.

> Everything about the company is fictional (Acme Corp, the people, tickets and dates). The code,
> the scanner output and the code excerpts inside the PDFs are real and generated from this repo.
> **`payments-portal` and `leave-tracker` are deliberately vulnerable. Run them on localhost only.**

## What is here

| Path | What it is |
|---|---|
| `apps/payments-portal` | Tier 1 app with seeded Critical flaws (SQLi, command injection, committed cloud keys, no auth, public bucket). Outcome: **not approved**, gate **blocked**. |
| `apps/leave-tracker` | Tier 2 app with moderate flaws plus an IDOR no scanner can see. Outcome: **approved with conditions**, gate **passes**. |
| `apps/inventory-api` | Tier 3 hardened reference. Outcome: **approved**, zero findings. |
| `pipeline/run_pipeline.py` | The demo pipeline: inventory, secrets, SAST, dependency scan, IaC, container, SBOM, DAST, release gate. Standard library only. |
| `pipeline/policy.json`, `advisories.json` | Gate policy and the curated advisory subset the SCA stage uses offline. |
| `pipeline/index.html` | Viewer that replays `pipeline/sample-run/pipeline-run.json`. |
| `reviews/content.py` | Filled-out review content: findings, STRIDE, ASVS coverage, roadmap, intake answers. |
| `reviews/build_pdfs.py` | Builds the PDFs below from that content, the real code and the real pipeline output. |
| `reviews/pdf/` | The generated PDFs (committed so the page works without a build). |
| `tests/test_demo.py` | Keeps the layers consistent (see below). |
| `../.github/workflows/appsec-demo-pipeline.yml` | Runs the tests and the pipeline per app on every push. |

### PDFs (`reviews/pdf/`)

| File | Contents |
|---|---|
| `00-portfolio-summary.pdf` | Roll-up of all three apps, what the pipeline missed, themes, the review process, recommendations. |
| `00-pipeline-run-report.pdf` | The pipeline run as a report: stage results and every finding per app. |
| `00-intake-questionnaire-blank.pdf` | Blank intake form for new review requests. |
| `01/02/03-<app>-intake.pdf` | The intake form filled out for each app, with AppSec triage. |
| `01/02/03-<app>-security-review.pdf` | Full review: summary and decision, scope, architecture and trust boundaries, STRIDE, findings with CVSS 3.1 and CWE/OWASP mapping and code evidence, ASVS coverage, pipeline evidence, roadmap, sign-off. |

## Run it

```bash
# 1. Run the pipeline against every app (writes pipeline/sample-run/)
python3 appsec-demo/pipeline/run_pipeline.py
python3 appsec-demo/pipeline/run_pipeline.py --app payments-portal --enforce   # exit 3 = gate blocked

# 2. Rebuild the PDFs from it (needs: pip install reportlab)
python3 appsec-demo/reviews/build_pdfs.py

# 3. Tests
python3 -m unittest discover -s appsec-demo/tests -v

# 4. Preview the pages
python3 -m http.server 8000     # then open http://localhost:8000/appsec-demo/
```

Re-run steps 1 and 2 after changing an app or `content.py`; the tests fail if the committed sample run
or PDF index is stale.

## Pipeline stages and their production equivalents

Each stage is a small, readable stand-in so the whole thing runs offline in about a second. In a real
pipeline you would swap the stage for the named tool and keep the same gate.

| Stage | Demo implementation | Production equivalent |
|---|---|---|
| Secret scan | Regex rules (AWS keys, private keys, hard-coded secrets); values never printed | gitleaks, GitHub secret scanning |
| SAST | Python `ast` rules: SQLi, command injection, path traversal, weak hash, insecure RNG, cookie flags, verbose errors | Semgrep, CodeQL |
| Dependency scan | `requirements.txt` pins vs. `advisories.json` | pip-audit, Dependabot, OSV-Scanner |
| IaC scan | Terraform block parser: public S3, open SSH/RDP, public or unencrypted RDS | Checkov, tfsec, Trivy |
| Container scan | Dockerfile rules: root user, unpinned base, curl-pipe-shell, no HEALTHCHECK | Hadolint, Trivy |
| SBOM | CycloneDX 1.5 JSON from pinned packages | Syft, cdxgen |
| DAST | Starts the app on a random localhost port and probes headers, SQLi, path traversal, unauthenticated routes | OWASP ZAP, Burp |
| Release gate | `policy.json`: block on Critical/High, cap on Medium, dated waivers in `appsec.json` | Required status check / branch protection |

Outputs per app: `results.sarif` (uploadable to GitHub code scanning), `sbom.cdx.json`; per run:
`pipeline-run.json`, `summary.md` (also written to the Actions job summary).

## How the pieces stay honest

`tests/test_demo.py` asserts that:

- each app's gate result equals the `expected_gate` it declares in `appsec.json`;
- every Critical/High/Medium pipeline finding is covered by a finding in the review, and every pipeline rule a review cites was really found;
- every code location the review quotes still exists in the app source;
- the finding counts written in each executive summary equal the CVSS-scored findings;
- CVSS scores match FIRST reference values, and secret values never appear in pipeline output;
- the committed `sample-run` and `pdf/index.json` are not stale.

## Limits worth stating in a demo

- The SCA data is a four-advisory curated subset, not a live feed; the PDFs say so. Dependency findings for
  `payments-portal` and `leave-tracker` also note that the packages are not imported by the code (no current reachability).
- The mini-DAST covers four generic probes. It cannot find authorisation flaws between users, which is why the
  Leave Tracker IDOR (LV-01) is a manual finding.
- The pipeline's file/line locations are heuristics, not data-flow analysis; a real SAST tool will be broader and noisier.
- The AWS key in `payments-portal` is the public AWS documentation example key, not a credential.
