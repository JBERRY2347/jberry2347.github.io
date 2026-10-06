#!/usr/bin/env python3
"""Demo AppSec pipeline: secrets, SAST, SCA, IaC, container, SBOM, mini-DAST, release gate.

Standard library only. Every stage is a small, readable stand-in for the production tool it
represents (see the README table), so the whole pipeline can run offline in a couple of seconds.

  python3 run_pipeline.py                       # all apps under ../apps, write ./sample-run
  python3 run_pipeline.py --app payments-portal # one app
  python3 run_pipeline.py --enforce             # exit 3 if any release gate fails (1 = pipeline error)
"""
import argparse
import ast
import datetime as dt
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0.0"
GATE_FAILED = 3  # exit code when --enforce and a gate fails; distinct from 1, which means the pipeline itself broke
HERE = Path(__file__).resolve().parent
SEVERITIES = ["critical", "high", "medium", "low", "info"]

# rule id -> (stage, severity, title, CWE, OWASP Top 10 2021, remediation)
RULES = {
    "SEC-AWS-ACCESS-KEY": ("secrets", "critical", "AWS access key id committed to source", "CWE-798", "A07", "Revoke the key, move to a secrets manager or an IAM role, rewrite history."),
    "SEC-AWS-SECRET": ("secrets", "critical", "AWS secret access key committed to source", "CWE-798", "A07", "Revoke the key, move to a secrets manager or an IAM role, rewrite history."),
    "SEC-PRIVATE-KEY": ("secrets", "critical", "Private key committed to source", "CWE-321", "A02", "Rotate the key pair and load keys from a secrets manager."),
    "SEC-HARDCODED-SECRET": ("secrets", "high", "Hard-coded credential or signing secret", "CWE-798", "A07", "Load the value from the environment or a secrets manager and rotate it."),
    "SAST-SQLI": ("sast", "critical", "SQL built by string formatting", "CWE-89", "A03", "Use parameterised queries (placeholders) for every value."),
    "SAST-CMDI": ("sast", "critical", "OS command built from input", "CWE-78", "A03", "Avoid the shell; call subprocess with an argument list and validate the host against an allow-list."),
    "SAST-PATH-TRAVERSAL": ("sast", "high", "File opened from caller-supplied path without containment", "CWE-22", "A01", "Resolve the path and verify it stays inside the export directory, or map names through an allow-list."),
    "SAST-UNSAFE-DESERIALIZATION": ("sast", "high", "Unsafe deserialisation or dynamic evaluation", "CWE-502", "A08", "Use yaml.safe_load / JSON; never pickle or eval untrusted data."),
    "SAST-WEAK-HASH": ("sast", "medium", "MD5/SHA-1 used (password hashing?)", "CWE-328", "A02", "Use a password KDF (argon2id, scrypt or PBKDF2-HMAC-SHA256 with a high work factor and per-user salt)."),
    "SAST-INSECURE-RANDOM": ("sast", "medium", "Non-cryptographic RNG used for a token or secret", "CWE-338", "A02", "Use the secrets module (secrets.token_urlsafe)."),
    "SAST-COOKIE-FLAGS": ("sast", "medium", "Session cookie missing Secure and/or HttpOnly", "CWE-614", "A05", "Set Secure; HttpOnly; SameSite=Lax (or Strict) on session cookies."),
    "SAST-VERBOSE-ERRORS": ("sast", "medium", "Exception detail returned to the client", "CWE-209", "A05", "Log the detail server-side and return a generic error with a correlation id."),
    "SAST-DEBUG": ("sast", "medium", "Debug mode enabled", "CWE-489", "A05", "Disable debug outside local development."),
    "SCA-VULNERABLE-DEPENDENCY": ("sca", "high", "Dependency with a known vulnerability", "CWE-1104", "A06", "Upgrade to the fixed version."),
    "IAC-S3-PUBLIC-ACL": ("iac", "high", "S3 bucket has a public ACL", "CWE-732", "A05", "Remove the public ACL and add an aws_s3_bucket_public_access_block."),
    "IAC-SG-OPEN-ADMIN-PORT": ("iac", "high", "Admin port (SSH/RDP) open to 0.0.0.0/0", "CWE-284", "A05", "Restrict to a bastion/VPN CIDR or use SSM Session Manager."),
    "IAC-RDS-PUBLIC": ("iac", "high", "Database publicly accessible", "CWE-284", "A05", "Set publicly_accessible = false and keep the database in private subnets."),
    "IAC-RDS-UNENCRYPTED": ("iac", "medium", "Database storage not encrypted", "CWE-311", "A02", "Set storage_encrypted = true with a customer-managed KMS key."),
    "DOCKER-ROOT": ("container", "medium", "Container runs as root", "CWE-250", "A05", "Add a non-root USER."),
    "DOCKER-LATEST": ("container", "low", "Base image not pinned (latest or untagged)", "CWE-1104", "A06", "Pin the base image to a version tag or digest."),
    "DOCKER-CURL-PIPE": ("container", "high", "Remote script piped into a shell during build", "CWE-494", "A08", "Download, verify a checksum, then run."),
    "DOCKER-NO-HEALTHCHECK": ("container", "low", "No HEALTHCHECK defined", "CWE-693", "A05", "Add a HEALTHCHECK so the orchestrator can restart unhealthy containers."),
    "DAST-SQLI": ("dast", "critical", "SQL injection confirmed against the running app", "CWE-89", "A03", "Use parameterised queries."),
    "DAST-PATH-TRAVERSAL": ("dast", "high", "Path traversal confirmed against the running app", "CWE-22", "A01", "Contain file access to an allow-listed directory."),
    "DAST-UNAUTH-ACCESS": ("dast", "high", "Sensitive endpoint reachable without authentication", "CWE-306", "A01", "Require authentication and role checks on every non-public route."),
    "DAST-MISSING-HEADERS": ("dast", "low", "Security response headers missing", "CWE-693", "A05", "Send X-Content-Type-Options, X-Frame-Options and a Content-Security-Policy."),
}

STAGES = [
    ("inventory", "Inventory", "Enumerate files, languages and manifests"),
    ("secrets", "Secret scan", "Regex rules for keys, tokens and credentials (gitleaks-style)"),
    ("sast", "SAST", "AST rules for injection, crypto and misconfiguration (Semgrep/CodeQL-style)"),
    ("sca", "Dependency scan", "Pinned packages vs. advisory data (pip-audit/Dependabot-style)"),
    ("iac", "IaC scan", "Terraform misconfiguration (Checkov/tfsec-style)"),
    ("container", "Container scan", "Dockerfile hygiene (Hadolint/Trivy-style)"),
    ("sbom", "SBOM", "CycloneDX 1.5 bill of materials (Syft-style)"),
    ("dast", "DAST", "Starts the app and probes it over HTTP (ZAP-style)"),
    ("gate", "Release gate", "Applies policy.json and dated waivers"),
]


def finding(rule, rel, line, detail="", **extra):
    stage, severity, title, cwe, owasp, fix = RULES[rule]
    key = "%s:%s:%s" % (rule, rel, line)
    out = {"id": key, "rule": rule, "stage": stage, "severity": severity, "title": title, "file": rel, "line": line,
           "cwe": cwe, "owasp": owasp, "detail": detail, "remediation": fix, "waived": False}
    out.update(extra)
    return out


def rel(path, root):
    return path.relative_to(root).as_posix()


def text_files(root):
    skip = {".git", "node_modules", "__pycache__"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not skip & set(path.parts) and path.suffix not in {".png", ".pdf", ".zip"}:
            yield path


# --------------------------------------------------------------------------- stages
def stage_inventory(app):
    files = list(text_files(app))
    langs = {}
    for path in files:
        langs[path.suffix or path.name] = langs.get(path.suffix or path.name, 0) + 1
    loc = sum(len(p.read_text(errors="ignore").splitlines()) for p in files if p.suffix in {".py", ".tf"} or p.name == "Dockerfile")
    return [], {"files": len(files), "lines_of_code": loc, "types": langs}


SECRET_RULES = [
    ("SEC-AWS-ACCESS-KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("SEC-AWS-SECRET", re.compile(r"aws_secret_access_key\s*[=:]\s*[\"'][A-Za-z0-9/+=]{40}[\"']", re.I)),
    ("SEC-PRIVATE-KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("SEC-HARDCODED-SECRET", re.compile(r"\b\w*(?:secret|passw(?:or)?d|api[_-]?key|token)\w*\s*=\s*[\"'][^\"'\s]{8,}[\"']", re.I)),
]


def stage_secrets(app):
    found = []
    for path in text_files(app):
        if path.suffix not in {".py", ".tf", ".json", ".env", ".yml", ".yaml", ".txt", ""} and path.name != "Dockerfile":
            continue
        for number, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
            if "environ" in line or "getenv" in line:
                continue
            for rule, pattern in SECRET_RULES:
                if pattern.search(line):
                    found.append(finding(rule, rel(path, app), number, "value redacted"))
                    break
    return found, {"rules": len(SECRET_RULES)}


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def is_dynamic(node):
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return not (isinstance(node.left, ast.Constant) and isinstance(node.right, ast.Constant))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
        return True
    return False


SQL_WORDS = re.compile(r"\b(select|insert|update|delete)\b", re.I)


def has_sql(node):
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str) and SQL_WORDS.search(n.value) for n in ast.walk(node))


def sast_python(tree, name):
    out = []
    for scope in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.Module))]:
        body_nodes = list(ast.walk(scope))
        tainted = {t.id for n in body_nodes if isinstance(n, ast.Assign) and is_dynamic(n.value) and has_sql(n.value)
                   for t in n.targets if isinstance(t, ast.Name)}
        names_used = {dotted(n) for n in body_nodes if isinstance(n, (ast.Attribute, ast.Name))}
        sanitised = any(w in " ".join(names_used) for w in ("realpath", "abspath", "basename", "resolve", "commonpath"))
        params = {a.arg for a in scope.args.args} if isinstance(scope, ast.FunctionDef) else set()
        for n in body_nodes:
            if not isinstance(n, ast.Call):
                continue
            func = dotted(n.func)
            arg0 = n.args[0] if n.args else None
            if func.endswith((".execute", ".executescript")) and arg0 is not None and (
                    (is_dynamic(arg0) and has_sql(arg0)) or (isinstance(arg0, ast.Name) and arg0.id in tainted)):
                out.append(("SAST-SQLI", n.lineno, "query assembled with string formatting"))
            elif func in {"os.system", "os.popen"} and arg0 is not None and not isinstance(arg0, ast.Constant):
                out.append(("SAST-CMDI", n.lineno, func + " with a non-constant command"))
            elif func.startswith("subprocess.") and any(k.arg == "shell" and getattr(k.value, "value", False) is True for k in n.keywords) \
                    and arg0 is not None and not isinstance(arg0, ast.Constant):
                out.append(("SAST-CMDI", n.lineno, func + "(shell=True) with a non-constant command"))
            elif func in {"hashlib.md5", "hashlib.sha1"}:
                out.append(("SAST-WEAK-HASH", n.lineno, func))
            elif func in {"pickle.loads", "pickle.load", "eval", "exec"} or (func == "yaml.load" and not any(k.arg == "Loader" for k in n.keywords)):
                out.append(("SAST-UNSAFE-DESERIALIZATION", n.lineno, func))
            elif func == "open" and arg0 is not None and not isinstance(arg0, ast.Constant) and not sanitised \
                    and params & {x.id for x in ast.walk(arg0) if isinstance(x, ast.Name)}:
                out.append(("SAST-PATH-TRAVERSAL", n.lineno, "open() on a path derived from parameter(s) %s" % ", ".join(sorted(params & {x.id for x in ast.walk(arg0) if isinstance(x, ast.Name)}))))
            elif func in {"random.choice", "random.randint", "random.random", "random.getrandbits"} and isinstance(scope, ast.FunctionDef) \
                    and re.search(r"token|secret|password|reset|session|nonce|key", scope.name, re.I):
                out.append(("SAST-INSECURE-RANDOM", n.lineno, func + " inside " + scope.name + "()"))
            if any(k.arg == "debug" and getattr(k.value, "value", False) is True for k in n.keywords):
                out.append(("SAST-DEBUG", n.lineno, "debug=True"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Tuple) and len(n.elts) == 2 and isinstance(n.elts[0], ast.Constant) and str(n.elts[0].value).lower() == "set-cookie":
            literal = " ".join(x.value for x in ast.walk(n.elts[1]) if isinstance(x, ast.Constant) and isinstance(x.value, str)).lower()
            missing = [f for f in ("secure", "httponly") if f not in literal]
            if missing:
                out.append(("SAST-COOKIE-FLAGS", n.lineno, "missing " + ", ".join({"secure": "Secure", "httponly": "HttpOnly"}[m] for m in missing)))
        if isinstance(n, ast.ExceptHandler):
            for c in [x for x in ast.walk(n) if isinstance(x, ast.Call) and dotted(x.func).endswith("reply")]:
                for sub in ast.walk(c):
                    if isinstance(sub, ast.Call) and (dotted(sub.func) == "traceback.format_exc" or (
                            dotted(sub.func) == "str" and sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == n.name)):
                        out.append(("SAST-VERBOSE-ERRORS", c.lineno, "exception text returned in the response"))
    return list(dict.fromkeys(out))


def stage_sast(app):
    found, scanned = [], 0
    for path in text_files(app):
        if path.suffix == ".py":
            scanned += 1
            for rule, line, detail in sast_python(ast.parse(path.read_text()), path.name):
                found.append(finding(rule, rel(path, app), line, detail))
    return found, {"python_files": scanned, "rules": sum(1 for r in RULES.values() if r[0] == "sast")}


def vtuple(version):
    return tuple(int(x) for x in re.findall(r"\d+", version)[:4])


def requirements(app):
    out = []
    for path in app.rglob("requirements*.txt"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([0-9][^\s;#]*)", line)
            if match:
                out.append((match.group(1).lower(), match.group(2), rel(path, app), number))
    return out


def stage_sca(app, advisories):
    found, reqs = [], requirements(app)
    for name, version, path, number in reqs:
        for adv in advisories:
            if adv["package"] == name and vtuple(version) < vtuple(adv["fixed"]):
                item = finding("SCA-VULNERABLE-DEPENDENCY", path, number, "%s %s: %s (%s). Fixed in %s." % (name, version, adv["summary"], adv["id"], adv["fixed"]),
                               advisory=adv["id"], package=name, version=version, fixed=adv["fixed"])
                item["severity"], item["cwe"] = adv["severity"], adv["cwe"]
                item["id"] = "%s:%s" % (adv["id"], path)
                found.append(item)
    return found, {"packages": len(reqs), "advisories_loaded": len(advisories)}


def tf_blocks(text):
    for match in re.finditer(r'resource\s+"(\w+)"\s+"(\w+)"\s*\{', text):
        depth, i = 1, match.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield match.group(1), match.group(2), text[:match.start()].count("\n") + 1, text[match.end():i - 1]


def stage_iac(app):
    found, blocks = [], 0
    for path in sorted(app.rglob("*.tf")):
        text = path.read_text()
        for kind, name, line, body in tf_blocks(text):
            blocks += 1
            where = rel(path, app)
            if kind == "aws_s3_bucket" and re.search(r'acl\s*=\s*"public-read(-write)?"', body):
                found.append(finding("IAC-S3-PUBLIC-ACL", where, line, "aws_s3_bucket." + name))
            if kind == "aws_security_group":
                for ing in re.finditer(r"ingress\s*\{(.*?)\n\s*\}", body, re.S):
                    block = ing.group(1)
                    low, high = (int(re.search(k + r"\s*=\s*(\d+)", block).group(1)) for k in ("from_port", "to_port"))
                    if "0.0.0.0/0" in block and any(low <= p <= high for p in (22, 3389)):
                        found.append(finding("IAC-SG-OPEN-ADMIN-PORT", where, line + body[:ing.start()].count("\n") + 1, "aws_security_group.%s ports %d-%d" % (name, low, high)))
            if kind == "aws_db_instance":
                if re.search(r"publicly_accessible\s*=\s*true", body):
                    found.append(finding("IAC-RDS-PUBLIC", where, line, "aws_db_instance." + name))
                if not re.search(r"storage_encrypted\s*=\s*true", body):
                    found.append(finding("IAC-RDS-UNENCRYPTED", where, line, "aws_db_instance." + name))
    return found, {"resources": blocks}


def stage_container(app):
    found = []
    for path in sorted(app.rglob("Dockerfile")):
        lines = path.read_text().splitlines()
        where = rel(path, app)
        for number, line in enumerate(lines, 1):
            if line.upper().startswith("FROM "):
                image = line.split()[1]
                if image.endswith(":latest") or (":" not in image and "@" not in image):
                    found.append(finding("DOCKER-LATEST", where, number, image))
            if re.search(r"(curl|wget)[^|]*\|\s*(sh|bash)", line):
                found.append(finding("DOCKER-CURL-PIPE", where, number))
        users = [l.split()[1] for l in lines if l.upper().startswith("USER ")]
        if not users or users[-1] in {"root", "0"}:
            found.append(finding("DOCKER-ROOT", where, 1, "no non-root USER"))
        if not any(l.upper().startswith("HEALTHCHECK") for l in lines):
            found.append(finding("DOCKER-NO-HEALTHCHECK", where, 1))
    return found, {"dockerfiles": len(list(app.rglob("Dockerfile")))}


def stage_sbom(app, meta, out_dir):
    comps = [{"type": "library", "name": n, "version": v, "purl": "pkg:pypi/%s@%s" % (n, v)} for n, v, _, _ in requirements(app)]
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "metadata": {"component": {"type": "application", "name": meta["name"]}, "tools": {"components": [{"type": "application", "name": "appsec-demo-pipeline", "version": VERSION}]}},
            "components": comps}
    (out_dir / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2) + "\n")
    return [], {"components": len(comps), "file": "sbom.cdx.json"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(base, path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, dict(resp.headers), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read().decode(errors="replace")


def rows_in(body):
    try:
        data = json.loads(body)
    except ValueError:
        return 0
    return len(data) if isinstance(data, list) else 0


def stage_dast(app, meta):
    port = free_port()
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1")
    proc = subprocess.Popen([sys.executable, "app.py"], cwd=app, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base, found, probes = "http://127.0.0.1:%d" % port, [], 0
    try:
        for _ in range(50):
            try:
                http(base, "/health")
                break
            except OSError:
                time.sleep(0.1)
        else:
            return [], {"skipped": "app did not start"}
        cfg = meta.get("dast", {})
        status, headers, _ = http(base, "/health")
        probes += 1
        missing = [h for h in ("X-Content-Type-Options", "X-Frame-Options", "Content-Security-Policy") if h.lower() not in {k.lower() for k in headers}]
        if missing:
            found.append(finding("DAST-MISSING-HEADERS", "GET /health", 0, "missing: " + ", ".join(missing)))
        for probe in cfg.get("sqli", []):
            hdr = probe.get("headers")
            quote = urllib.request.quote
            base_n = rows_in(http(base, "%s?%s=zzzz" % (probe["path"], probe["param"]), hdr)[2])
            err_body = http(base, "%s?%s=%s" % (probe["path"], probe["param"], quote("'")), hdr)[2]
            or_n = rows_in(http(base, "%s?%s=%s" % (probe["path"], probe["param"], quote("zzzz' OR '1'='1' --")), hdr)[2])
            probes += 3
            error_based = bool(re.search(r"unrecognized token|syntax error|sqlite", err_body, re.I))
            if error_based or or_n > base_n:
                signal = "a lone quote produced a database error" if error_based else "OR-true returned %d row(s) vs %d" % (or_n, base_n)
                found.append(finding("DAST-SQLI", "GET " + probe["path"], 0, "parameter '%s': %s" % (probe["param"], signal)))
        for probe in cfg.get("traversal", []):
            for payload in ("../" * 10 + "etc/passwd", "../app.py"):
                _, _, body = http(base, "%s?%s=%s" % (probe["path"], probe["param"], payload))
                probes += 1
                if "root:" in body or "import " in body:
                    found.append(finding("DAST-PATH-TRAVERSAL", "GET " + probe["path"], 0, "parameter '%s' returned file content for %s" % (probe["param"], payload)))
                    break
        for path in cfg.get("unauthenticated", []):
            status, _, _ = http(base, path)
            probes += 1
            if status == 200:
                found.append(finding("DAST-UNAUTH-ACCESS", "GET " + path.split("?")[0], 0, "HTTP 200 with no credentials"))
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    return found, {"probes": probes, "target": "ephemeral localhost instance"}


def apply_waivers(findings, waivers, today):
    used = []
    for w in waivers:
        expired = w.get("expires", "9999-12-31") < today
        for f in findings:
            if f["rule"] == w["rule"] and (not w.get("file") or w["file"] == f["file"]) and not expired:
                f["waived"], f["waiver"] = True, {"reason": w.get("reason", ""), "expires": w.get("expires")}
                used.append(f["id"])
    return used


def stage_gate(findings, policy, waivers, today):
    apply_waivers(findings, waivers, today)
    live = [f for f in findings if not f["waived"]]
    count = lambda sev: sum(1 for f in live if f["severity"] == sev)
    reasons = ["%d %s finding(s)" % (count(s), s) for s in policy["fail_on"] if count(s)]
    if count("medium") > policy["max_medium"]:
        reasons.append("%d medium findings exceed the limit of %d" % (count("medium"), policy["max_medium"]))
    return [], {"result": "fail" if reasons else "pass", "reasons": reasons, "waived": sum(1 for f in findings if f["waived"]), "policy": policy["name"]}


# --------------------------------------------------------------------------- driver
def run_app(app, advisories, policy, out_root, run_dast, today):
    meta = json.loads((app / "appsec.json").read_text())
    out_dir = out_root / meta["slug"]
    out_dir.mkdir(parents=True, exist_ok=True)
    results, findings = [], []
    plan = {"inventory": lambda: stage_inventory(app), "secrets": lambda: stage_secrets(app), "sast": lambda: stage_sast(app),
            "sca": lambda: stage_sca(app, advisories), "iac": lambda: stage_iac(app), "container": lambda: stage_container(app),
            "sbom": lambda: stage_sbom(app, meta, out_dir), "dast": lambda: stage_dast(app, meta) if run_dast else ([], {"skipped": "--no-dast"})}
    for key, label, blurb in STAGES:
        started = time.perf_counter()
        if key == "gate":
            items, info = stage_gate(findings, policy, meta.get("waivers", []), today)
        else:
            items, info = plan[key]()
            findings.extend(items)
        status = "skipped" if "skipped" in info else ("fail" if key == "gate" and info["result"] == "fail" else ("warn" if any(i["severity"] in {"critical", "high", "medium"} for i in items) else "pass"))
        if key == "gate":
            status = info["result"]
        results.append({"key": key, "label": label, "description": blurb, "status": status, "duration_ms": round((time.perf_counter() - started) * 1000),
                        "findings": len(items), "info": info})
    gate = results[-1]["info"]
    counts = {s: sum(1 for f in findings if f["severity"] == s and not f["waived"]) for s in SEVERITIES}
    findings.sort(key=lambda f: (SEVERITIES.index(f["severity"]), f["stage"], f["file"], f["line"]))
    (out_dir / "results.sarif").write_text(json.dumps(sarif(findings, meta), indent=2) + "\n")
    return {"slug": meta["slug"], "name": meta["name"], "tier": meta["tier"], "data_classification": meta["data_classification"],
            "expected_gate": meta["expected_gate"], "gate": gate["result"], "gate_reasons": gate["reasons"], "counts": counts,
            "stages": results, "findings": findings}


def sarif(findings, meta):
    level = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
    rules = sorted({f["rule"] for f in findings})
    return {"version": "2.1.0", "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{"tool": {"driver": {"name": "appsec-demo-pipeline", "version": VERSION, "rules": [
                {"id": r, "shortDescription": {"text": RULES[r][2]}, "properties": {"cwe": RULES[r][3], "owasp": RULES[r][4]}} for r in rules]}},
                "results": [{"ruleId": f["rule"], "level": level[f["severity"]], "message": {"text": f["title"] + (": " + f["detail"] if f["detail"] else "")},
                             "locations": [{"physicalLocation": {"artifactLocation": {"uri": f["file"]}, "region": {"startLine": max(f["line"], 1)}}}]} for f in findings]}]}


def markdown(report):
    lines = ["# AppSec demo pipeline", "", "Run `%s` at %s (commit `%s`)" % (report["run"]["id"], report["run"]["finished"], report["run"]["commit"]), "",
             "| App | Gate | Critical | High | Medium | Low | Info |", "|---|---|---|---|---|---|---|"]
    for app in report["apps"]:
        c = app["counts"]
        lines.append("| %s | %s | %d | %d | %d | %d | %d |" % (app["name"], "PASS" if app["gate"] == "pass" else "BLOCKED", c["critical"], c["high"], c["medium"], c["low"], c["info"]))
    for app in report["apps"]:
        lines += ["", "## %s - %s" % (app["name"], "gate passed" if app["gate"] == "pass" else "gate blocked: " + "; ".join(app["gate_reasons"])), ""]
        lines += ["| Severity | Rule | Location | Detail |", "|---|---|---|---|"]
        lines += ["| %s | `%s` | `%s%s` | %s |" % (f["severity"], f["rule"], f["file"], ":%d" % f["line"] if f["line"] else "", f["detail"].replace("|", "/")) for f in app["findings"]] or ["| - | - | - | No findings |"]
    return "\n".join(lines) + "\n"


def git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apps-dir", type=Path, default=HERE.parent / "apps")
    ap.add_argument("--app", action="append", help="app slug (repeatable); default all")
    ap.add_argument("--policy", type=Path, default=HERE / "policy.json")
    ap.add_argument("--out", type=Path, default=HERE / "sample-run")
    ap.add_argument("--enforce", action="store_true", help="exit %d when any release gate fails" % GATE_FAILED)
    ap.add_argument("--no-dast", action="store_true")
    args = ap.parse_args()

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    now = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc) if epoch else dt.datetime.now(dt.timezone.utc)
    policy = json.loads(args.policy.read_text())
    advisories = json.loads((HERE / "advisories.json").read_text())["advisories"]
    slugs = args.app or sorted(p.name for p in args.apps_dir.iterdir() if (p / "appsec.json").exists())
    args.out.mkdir(parents=True, exist_ok=True)
    apps = []
    for slug in slugs:
        print("== %s" % slug)
        app = run_app(args.apps_dir / slug, advisories, policy, args.out, not args.no_dast, now.date().isoformat())
        for stage in app["stages"]:
            print("  %-16s %-7s %3d finding(s)" % (stage["label"], stage["status"], stage["findings"]))
        print("  GATE: %s  %s" % (app["gate"].upper(), "; ".join(app["gate_reasons"])))
        apps.append(app)
    report = {"pipeline": {"name": "appsec-demo-pipeline", "version": VERSION, "policy": policy},
              "run": {"id": now.strftime("%Y%m%d-%H%M%S"), "finished": now.strftime("%Y-%m-%d %H:%M UTC"), "commit": git_commit()}, "apps": apps}
    (args.out / "pipeline-run.json").write_text(json.dumps(report, indent=2) + "\n")
    (args.out / "summary.md").write_text(markdown(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write(markdown(report))
    if args.enforce and any(a["gate"] == "fail" for a in apps):
        sys.exit(GATE_FAILED)


if __name__ == "__main__":
    main()
