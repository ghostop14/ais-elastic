#!/usr/bin/python3

"""create_dashboards — Push ais-elastic ECS template, lifecycle policy, and
Kibana / OpenSearch Dashboards saved objects.

The bridge installs the template + policy on startup, but this script lets
you do the same manually and also push the dashboard NDJSON file (which the
bridge never touches).

Works against either Elasticsearch+Kibana or OpenSearch+OSD.

Usage:
    python3 create_dashboards.py \\
        --backend-type elasticsearch \\
        --server-url http://user:pass@es.host:9200 \\
        --dashboards-url http://kibana.host:5601

    python3 create_dashboards.py --dashboards-only \\
        --dashboards-url http://kibana.host:5601
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

HERE = Path(__file__).resolve().parent
NDJSON_FILE = HERE / "kibana_saved_objects.ndjson"
INDEX_TEMPLATE_FILE = HERE / "ais_elastic_index_template.json"
ILM_POLICY_FILE = HERE / "ais_elastic_lifecycle_policy.json"
ISM_POLICY_FILE = HERE / "ais_elastic_ism_policy.json"

TEMPLATE_NAME = "ais-elastic"
POLICY_NAME = "ais-elastic"
ALIAS = "ais"


def _split_auth(url):
    """Return (clean_url, (user,pass) or None) — split userinfo from URL."""
    parts = urlparse(url)
    auth = None
    if "@" in (parts.netloc or ""):
        userinfo, host = parts.netloc.split("@", 1)
        if ":" in userinfo:
            u, p = userinfo.split(":", 1)
        else:
            u, p = userinfo, ""
        auth = (u, p)
        parts = parts._replace(netloc=host)
    return str(urlunparse(parts)), auth


def _request(method, url, **kw):
    return requests.request(method, url, timeout=30, **kw)


def create_index_template(server_url, auth, overwrite, verify, backend):
    url, urlauth = _split_auth(server_url)
    if urlauth and not auth:
        auth = urlauth
    target = f"{url.rstrip('/')}/_index_template/{TEMPLATE_NAME}"
    body = json.loads(INDEX_TEMPLATE_FILE.read_text())
    if backend == "elasticsearch":
        # ILM requires these on every backing index to drive rollover; ISM
        # binds via the policy's own ism_template so OpenSearch skips them.
        settings = body.setdefault("template", {}).setdefault("settings", {})
        settings["index.lifecycle.name"] = POLICY_NAME
        settings["index.lifecycle.rollover_alias"] = ALIAS
    if not overwrite:
        r = _request("GET", target, auth=auth, verify=verify)
        if r.status_code == 200:
            print(f"[OK]  Index template '{TEMPLATE_NAME}' already exists "
                  f"(use --overwrite to replace)")
            return True
    r = _request("PUT", target, json=body, auth=auth, verify=verify,
                 headers={"Content-Type": "application/json"})
    if r.ok and r.json().get("acknowledged"):
        print(f"[OK]  Index template '{TEMPLATE_NAME}' created/updated")
        return True
    print(f"[ERR] template install: {r.status_code} {r.text[:300]}")
    return False


def create_lifecycle_policy(backend, server_url, auth, overwrite, verify):
    url, urlauth = _split_auth(server_url)
    if urlauth and not auth:
        auth = urlauth
    if backend == "opensearch":
        body = json.loads(ISM_POLICY_FILE.read_text())
        target = f"{url.rstrip('/')}/_plugins/_ism/policies/{POLICY_NAME}"
    else:
        body = json.loads(ILM_POLICY_FILE.read_text())
        target = f"{url.rstrip('/')}/_ilm/policy/{POLICY_NAME}"
    if not overwrite:
        r = _request("GET", target, auth=auth, verify=verify)
        if r.status_code == 200:
            print(f"[OK]  Lifecycle policy '{POLICY_NAME}' already exists "
                  f"(use --overwrite to replace)")
            return True
    r = _request("PUT", target, json=body, auth=auth, verify=verify,
                 headers={"Content-Type": "application/json"})
    if r.ok:
        print(f"[OK]  Lifecycle policy '{POLICY_NAME}' created/updated "
              f"({backend})")
        return True
    print(f"[ERR] policy install: {r.status_code} {r.text[:300]}")
    return False


def import_saved_objects(dashboards_url, auth, overwrite, verify):
    if not NDJSON_FILE.exists():
        print(f"[ERR] NDJSON file not found: {NDJSON_FILE}")
        return False
    url, urlauth = _split_auth(dashboards_url)
    if urlauth and not auth:
        auth = urlauth
    endpoint = f"{url.rstrip('/')}/api/saved_objects/_import"
    if overwrite:
        endpoint += "?overwrite=true"
    with open(NDJSON_FILE, "rb") as f:
        r = _request("POST", endpoint,
                     files={"file": ("kibana_saved_objects.ndjson", f,
                                     "application/ndjson")},
                     headers={"kbn-xsrf": "true", "osd-xsrf": "true"},
                     auth=auth, verify=verify)
    if not r.ok:
        print(f"[ERR] Dashboards import: {r.status_code} {r.text[:300]}")
        return False
    payload = r.json()
    if payload.get("success"):
        print(f"[OK]  Imported {payload.get('successCount', 0)} saved objects")
        return True
    errors = payload.get("errors", [])
    sc = payload.get("successCount", 0)
    if errors and all(e.get("error", {}).get("type") == "conflict"
                      for e in errors) and not overwrite:
        print(f"[OK]  All {len(errors)} saved objects already exist "
              f"(use --overwrite to replace)")
        return True
    print(f"[WARN] Imported {sc} with {len(errors)} error(s):")
    for e in errors[:5]:
        print(f"       {e.get('type')}/{e.get('id')}: "
              f"{e.get('error', {}).get('message', e.get('error'))}")
    return sc > 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend-type",
                    choices=["elasticsearch", "opensearch"],
                    default="elasticsearch")
    ap.add_argument("--server-url",
                    help="ES / OpenSearch URL (creds may be embedded)")
    ap.add_argument("--dashboards-url",
                    help="Kibana / OpenSearch Dashboards URL")
    ap.add_argument("--username", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--no-verify-tls", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dashboards-only", action="store_true")
    ap.add_argument("--server-only", action="store_true")
    args = ap.parse_args()

    if not args.server_url and not args.dashboards_url:
        ap.error("provide --server-url and/or --dashboards-url")
    if args.dashboards_only and args.server_only:
        ap.error("--dashboards-only and --server-only are mutually exclusive")

    auth = (args.username, args.password) if args.username else None
    verify = not args.no_verify_tls
    ok = True

    if args.server_url and not args.dashboards_only:
        print(f"\n--- {args.backend_type}: {args.server_url} ---")
        if not create_index_template(args.server_url, auth,
                                     args.overwrite, verify,
                                     args.backend_type):
            ok = False
        if not create_lifecycle_policy(args.backend_type, args.server_url,
                                       auth, args.overwrite, verify):
            ok = False

    if args.dashboards_url and not args.server_only:
        label = ("Kibana" if args.backend_type == "elasticsearch"
                 else "OpenSearch Dashboards")
        print(f"\n--- {label}: {args.dashboards_url} ---")
        if not import_saved_objects(args.dashboards_url, auth,
                                    args.overwrite, verify):
            ok = False

    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
