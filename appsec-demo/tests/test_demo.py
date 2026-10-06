"""Self-tests for the AppSec demo. Run: python3 -m unittest discover -s appsec-demo/tests -v

They keep the three layers honest: the pipeline finds what the apps contain, the review documents
cover what the pipeline finds, and the committed sample run / PDF index are not stale.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pipeline"), str(ROOT / "reviews")]

import run_pipeline as rp  # noqa: E402
import content as C  # noqa: E402
from cvss import base_score, severity  # noqa: E402

POLICY = json.loads((ROOT / "pipeline" / "policy.json").read_text())
ADVISORIES = json.loads((ROOT / "pipeline" / "advisories.json").read_text())["advisories"]


def run(slug, dast=True):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        return rp.run_app(ROOT / "apps" / slug, ADVISORIES, POLICY, Path(tmp), dast, "2026-10-06")


class Cvss(unittest.TestCase):
    def test_reference_vectors(self):
        self.assertEqual(base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H"), 10.0)
        self.assertEqual(base_score("CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:N/A:N"), 6.1)
        self.assertEqual(base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"), 0.0)
        self.assertEqual([severity(x) for x in (9.0, 7.0, 4.0, 0.1, None)], ["Critical", "High", "Medium", "Low", "Info"])


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {s: run(s) for s in C.APPS}

    def test_gate_matches_declared_expectation(self):
        for slug, result in self.runs.items():
            self.assertEqual(result["gate"], result["expected_gate"], slug)

    def test_clean_app_has_no_findings(self):
        self.assertEqual(self.runs["inventory-api"]["findings"], [])

    def test_dast_actually_ran(self):
        dast = next(s for s in self.runs["payments-portal"]["stages"] if s["key"] == "dast")
        self.assertGreater(dast["info"]["probes"], 5)
        self.assertTrue({"DAST-SQLI", "DAST-UNAUTH-ACCESS", "DAST-PATH-TRAVERSAL"} <= {f["rule"] for f in self.runs["payments-portal"]["findings"]})

    def test_waiver_unblocks_and_expires(self):
        findings = [rp.finding("SAST-SQLI", "app.py", 3)]
        waivers = [{"rule": "SAST-SQLI", "file": "app.py", "reason": "demo", "expires": "2026-12-31"}]
        self.assertEqual(rp.stage_gate(findings, POLICY, waivers, "2026-10-06")[1]["result"], "pass")
        findings = [rp.finding("SAST-SQLI", "app.py", 3)]
        self.assertEqual(rp.stage_gate(findings, POLICY, waivers, "2027-01-01")[1]["result"], "fail")

    def test_secret_values_are_never_reported(self):
        blob = json.dumps(self.runs["payments-portal"]["findings"])
        for secret in ("wJalrXUtnFEMI", "AKIAIOSFODNN7", "s3cr3t-jwt"):
            self.assertNotIn(secret, blob)


class ReviewsCoverPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {s: run(s, dast=True) for s in C.APPS}

    def test_every_medium_or_worse_tool_finding_is_in_the_review(self):
        for slug, result in self.runs.items():
            covered = {r for f in C.APPS[slug]["findings"] for r in f["rules"]}
            missing = {f["rule"] for f in result["findings"] if f["severity"] in {"critical", "high", "medium"}} - covered
            self.assertFalse(missing, "%s: review does not cover %s" % (slug, sorted(missing)))

    def test_review_rules_exist_and_were_actually_found(self):
        for slug, result in self.runs.items():
            found = {f["rule"] for f in result["findings"]}
            for f in C.APPS[slug]["findings"]:
                for rule in f["rules"]:
                    self.assertIn(rule, rp.RULES)
                    self.assertIn(rule, found, "%s cites %s but the pipeline did not find it" % (f["id"], rule))

    def test_finding_ids_unique_and_locations_resolve(self):
        for slug, app in C.APPS.items():
            ids = [f["id"] for f in app["findings"]]
            self.assertEqual(len(ids), len(set(ids)))
            for f in app["findings"]:
                name, needle = f["loc"]
                self.assertIn(needle, (ROOT / "apps" / slug / name).read_text(), f["id"])
                self.assertIn(f["owasp"], C.OWASP)

    def test_summary_counts_match_scored_findings(self):
        for slug in ("payments-portal", "leave-tracker"):
            counts = {s: 0 for s in ("Critical", "High", "Medium", "Low")}
            for f in C.APPS[slug]["findings"]:
                counts[severity(base_score("CVSS:3.1/" + f["vector"]))] += 1
            text = "%d findings: %d Critical, %d High, %d Medium and %d Low" % (len(C.APPS[slug]["findings"]), *counts.values())
            self.assertIn(text, " ".join(C.APPS[slug]["summary"]), slug)

    def test_every_app_answers_every_intake_question_and_asvs_chapter(self):
        ids = [q for _, qs in C.INTAKE_SECTIONS for q, _ in qs]
        for slug, app in C.APPS.items():
            self.assertEqual(sorted(ids), sorted(app["intake"]), slug)
            self.assertEqual(sorted(C.ASVS_CHAPTERS), sorted(app["asvs"]), slug)


class CommittedOutputs(unittest.TestCase):
    def test_sample_run_is_not_stale(self):
        sample = json.loads((ROOT / "pipeline" / "sample-run" / "pipeline-run.json").read_text())
        for app in sample["apps"]:
            fresh = run(app["slug"])
            self.assertEqual(sorted(f["id"] for f in app["findings"]), sorted(f["id"] for f in fresh["findings"]), "re-run run_pipeline.py: " + app["slug"])
            self.assertEqual(app["gate"], fresh["gate"])

    def test_pdf_index_matches_content(self):
        try:
            import build_pdfs as bp
        except ImportError:
            self.skipTest("reportlab not installed")
        index = json.loads((ROOT / "reviews" / "pdf" / "index.json").read_text())
        for entry in index["apps"]:
            self.assertEqual(entry["counts"], bp.counts_of(bp.enrich(entry["slug"], C.APPS[entry["slug"]])), "re-run build_pdfs.py")
            self.assertTrue((ROOT / "reviews" / "pdf" / ("%s-%s-security-review.pdf" % (C.APPS[entry["slug"]]["number"], entry["slug"]))).exists())


if __name__ == "__main__":
    unittest.main()
