# AppSec demo pipeline

Run `20261006-173842` at 2026-10-06 17:38 UTC (commit `9a4fe57`)

| App | Gate | Critical | High | Medium | Low | Info |
|---|---|---|---|---|---|---|
| Acme Inventory API | PASS | 0 | 0 | 0 | 0 | 0 |
| Acme Leave Tracker | PASS | 0 | 0 | 4 | 2 | 0 |
| Acme Payments Portal | BLOCKED | 6 | 8 | 7 | 3 | 0 |

## Acme Inventory API - gate passed

| Severity | Rule | Location | Detail |
|---|---|---|---|
| - | - | - | No findings |

## Acme Leave Tracker - gate passed

| Severity | Rule | Location | Detail |
|---|---|---|---|
| medium | `SAST-INSECURE-RANDOM` | `app.py:27` | random.choice inside new_reset_token() |
| medium | `SAST-VERBOSE-ERRORS` | `app.py:67` | exception text returned in the response |
| medium | `SAST-COOKIE-FLAGS` | `app.py:71` | missing Secure, HttpOnly |
| medium | `SCA-VULNERABLE-DEPENDENCY` | `requirements.txt:1` | requests 2.25.1: Proxy-Authorization header leaked to the destination server on HTTPS redirects. (CVE-2023-32681). Fixed in 2.31.0. |
| low | `DOCKER-NO-HEALTHCHECK` | `Dockerfile:1` |  |
| low | `DAST-MISSING-HEADERS` | `GET /health` | missing: X-Frame-Options, Content-Security-Policy |

## Acme Payments Portal - gate blocked: 6 critical finding(s); 8 high finding(s); 7 medium findings exceed the limit of 5

| Severity | Rule | Location | Detail |
|---|---|---|---|
| critical | `DAST-SQLI` | `GET /search` | parameter 'q': a lone quote produced a database error |
| critical | `SAST-SQLI` | `app.py:30` | query assembled with string formatting |
| critical | `SAST-CMDI` | `app.py:59` | os.system with a non-constant command |
| critical | `SCA-VULNERABLE-DEPENDENCY` | `requirements.txt:1` | pyyaml 5.3: Arbitrary code execution via yaml.full_load / FullLoader on untrusted input. (CVE-2020-14343). Fixed in 5.4. |
| critical | `SEC-AWS-ACCESS-KEY` | `app.py:14` | value redacted |
| critical | `SEC-AWS-SECRET` | `app.py:15` | value redacted |
| high | `DAST-UNAUTH-ACCESS` | `GET /admin` | HTTP 200 with no credentials |
| high | `DAST-PATH-TRAVERSAL` | `GET /download` | parameter 'file' returned file content for ../../../../../../../../../../etc/passwd |
| high | `IAC-S3-PUBLIC-ACL` | `infra/main.tf:1` | aws_s3_bucket.exports |
| high | `IAC-SG-OPEN-ADMIN-PORT` | `infra/main.tf:10` | aws_security_group.portal ports 22-22 |
| high | `SAST-PATH-TRAVERSAL` | `app.py:34` | open() on a path derived from parameter(s) name |
| high | `SCA-VULNERABLE-DEPENDENCY` | `requirements.txt:2` | requests 2.19.0: Authorization header sent to a different host after a redirect. (CVE-2018-18074). Fixed in 2.20.0. |
| high | `SCA-VULNERABLE-DEPENDENCY` | `requirements.txt:3` | jinja2 2.10: Sandbox escape through str.format_map. (CVE-2019-10906). Fixed in 2.10.1. |
| high | `SEC-HARDCODED-SECRET` | `app.py:13` | value redacted |
| medium | `DOCKER-ROOT` | `Dockerfile:1` | no non-root USER |
| medium | `IAC-RDS-UNENCRYPTED` | `infra/main.tf:24` | aws_db_instance.ledger |
| medium | `SAST-WEAK-HASH` | `app.py:25` | hashlib.md5 |
| medium | `SAST-VERBOSE-ERRORS` | `app.py:67` | exception text returned in the response |
| medium | `SAST-WEAK-HASH` | `app.py:74` | hashlib.md5 |
| medium | `SAST-COOKIE-FLAGS` | `app.py:78` | missing Secure, HttpOnly |
| medium | `SCA-VULNERABLE-DEPENDENCY` | `requirements.txt:2` | requests 2.19.0: Proxy-Authorization header leaked to the destination server on HTTPS redirects. (CVE-2023-32681). Fixed in 2.31.0. |
| low | `DOCKER-LATEST` | `Dockerfile:1` | python:latest |
| low | `DOCKER-NO-HEALTHCHECK` | `Dockerfile:1` |  |
| low | `DAST-MISSING-HEADERS` | `GET /health` | missing: X-Content-Type-Options, X-Frame-Options, Content-Security-Policy |
