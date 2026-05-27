#!/usr/bin/python3

"""search_client — Elasticsearch / OpenSearch abstraction.

Mirrors the pattern from sparrow-droneid's elasticsearch_engine: a thin
:class:`SearchClient` ABC with concrete implementations for elasticsearch-py
(8.x) and opensearch-py (2.x).  Hides the ILM-vs-ISM split, lifecycle API
differences, and import-time wiring.

Index strategy: traditional index + write alias + rollover.  Data streams
are not used because OpenSearch's ISM is alias-based; using the same shape
across both backends keeps everything coherent.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Sequence, Tuple

log = logging.getLogger("ais_elastic.search_client")


# ---------------------------------------------------------------------------
# Auto-detect helpers
# ---------------------------------------------------------------------------

def detect_backend(url, username="", password="", api_key="",
                   verify_tls=True, timeout=5.0):
    """Probe *url* and return 'elasticsearch' or 'opensearch'.

    Falls through to 'elasticsearch' on any failure (with WARN log).
    """
    import requests
    try:
        kwargs = {"timeout": timeout, "verify": verify_tls}
        if api_key:
            kwargs["headers"] = {"Authorization": f"ApiKey {api_key}"}
        elif username:
            kwargs["auth"] = (username, password)
        if not verify_tls:
            _suppress_urllib3_warnings()
        resp = requests.get(url.rstrip("/") + "/", **kwargs)
        resp.raise_for_status()
        info = resp.json()
        dist = (info.get("version") or {}).get("distribution", "")
        if str(dist).lower() == "opensearch":
            return "opensearch"
        # Reverse-proxy fallback: probe /_plugins/_ism/policies (OS-only endpoint)
        try:
            probe = requests.get(
                url.rstrip("/") + "/_plugins/_ism/policies", **kwargs
            )
            if probe.status_code in (200, 401, 403):
                return "opensearch"
        except Exception:
            pass
        return "elasticsearch"
    except Exception as exc:
        log.warning("backend probe failed (%s); defaulting to elasticsearch", exc)
        return "elasticsearch"


def resolve_backend(requested, url, **probe_kwargs):
    """Honor explicit 'elasticsearch'/'opensearch'; auto-probe on 'auto'.

    *probe_kwargs* are forwarded to detect_backend (username, password,
    api_key, verify_tls, timeout).
    """
    r = (requested or "auto").lower()
    if r in ("elasticsearch", "es"):
        return "elasticsearch"
    if r in ("opensearch", "os"):
        return "opensearch"
    if r == "auto":
        choice = detect_backend(url, **probe_kwargs)
        log.info("backend auto-detect: %s", choice)
        return choice
    raise ValueError(
        f"backend-type must be 'auto', 'elasticsearch', or 'opensearch' "
        f"(got {requested!r})"
    )


class SearchClient(ABC):
    """Thin abstraction over the ES / OpenSearch Python clients."""

    @abstractmethod
    def ping(self) -> bool: ...

    @abstractmethod
    def cluster_info(self) -> Dict: ...

    @abstractmethod
    def bulk(self, actions: Sequence[Dict]) -> Tuple[int, List[Dict]]: ...

    @abstractmethod
    def put_index_template(self, name: str, body: Dict) -> None: ...

    @abstractmethod
    def put_lifecycle_policy(self, name: str, body: Dict) -> None: ...

    @abstractmethod
    def alias_exists(self, alias: str) -> bool: ...

    @abstractmethod
    def create_initial_index(self, index_name: str, alias: str) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...

    backend: str = ""


# ---------------------------------------------------------------------------
# Helpers shared between backends
# ---------------------------------------------------------------------------

def _is_resource_exists(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in (400, 409):
        msg = str(exc).lower()
        return "resource_already_exists" in msg or "already exists" in msg
    return False


def _suppress_urllib3_warnings():
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Elasticsearch client (elasticsearch-py >= 8)
# ---------------------------------------------------------------------------

class ElasticsearchClient(SearchClient):
    backend = "elasticsearch"

    def __init__(self, url: str, username: str = "", password: str = "",
                 api_key: str = "", verify_tls: bool = True):
        from elasticsearch import Elasticsearch, helpers  # pyright: ignore[reportMissingImports]
        self._helpers = helpers

        kwargs: Dict[str, Any] = {
            "hosts": [url],
            "verify_certs": verify_tls,
            "request_timeout": 30,
        }
        if not verify_tls:
            _suppress_urllib3_warnings()
            kwargs["ssl_show_warn"] = False
        if api_key:
            kwargs["api_key"] = api_key
        elif username:
            kwargs["basic_auth"] = (username, password)
        self._client = Elasticsearch(**kwargs)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def cluster_info(self) -> Dict:
        resp = self._client.info()
        raw = resp.body if hasattr(resp, "body") else resp
        return json.loads(json.dumps(dict(raw), default=str))

    def bulk(self, actions):
        success, errors = self._helpers.bulk(
            self._client, actions,
            raise_on_error=False, raise_on_exception=False,
        )
        return success, errors if isinstance(errors, list) else []

    def put_index_template(self, name, body):
        self._client.indices.put_index_template(name=name, body=body)

    def put_lifecycle_policy(self, name, body):
        # Body is the full {"policy": {...}} dict; elasticsearch-py wants
        # just the policy contents.
        policy = body.get("policy", body) if isinstance(body, dict) else body
        self._client.ilm.put_lifecycle(name=name, policy=policy)

    def alias_exists(self, alias):
        try:
            return bool(self._client.indices.exists_alias(name=alias))
        except Exception:
            return False

    def create_initial_index(self, index_name, alias):
        try:
            self._client.indices.create(
                index=index_name,
                body={"aliases": {alias: {"is_write_index": True}}},
            )
            return True
        except Exception as exc:
            if _is_resource_exists(exc):
                return False
            raise

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# OpenSearch client (opensearch-py >= 2)
# ---------------------------------------------------------------------------

class OpenSearchClient(SearchClient):
    backend = "opensearch"

    def __init__(self, url: str, username: str = "", password: str = "",
                 verify_tls: bool = True):
        from opensearchpy import OpenSearch, helpers  # pyright: ignore[reportMissingImports]
        self._helpers = helpers

        kwargs: Dict[str, Any] = {
            "hosts": [url],
            "verify_certs": verify_tls,
            "timeout": 30,
        }
        if not verify_tls:
            _suppress_urllib3_warnings()
            kwargs["ssl_show_warn"] = False
        if username:
            kwargs["http_auth"] = (username, password)
        self._client = OpenSearch(**kwargs)

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def cluster_info(self) -> Dict:
        return json.loads(json.dumps(dict(self._client.info()), default=str))

    def bulk(self, actions):
        success, errors = self._helpers.bulk(
            self._client, actions,
            raise_on_error=False, raise_on_exception=False,
        )
        return success, errors if isinstance(errors, list) else []

    def put_index_template(self, name, body):
        self._client.indices.put_index_template(name=name, body=body)

    def put_lifecycle_policy(self, name, body):
        # OpenSearch uses ISM (Index State Management) via the _plugins API.
        self._client.transport.perform_request(
            "PUT", f"/_plugins/_ism/policies/{name}", body=body,
        )

    def alias_exists(self, alias):
        try:
            return bool(self._client.indices.exists_alias(name=alias))
        except Exception:
            return False

    def create_initial_index(self, index_name, alias):
        try:
            self._client.indices.create(
                index=index_name,
                body={"aliases": {alias: {"is_write_index": True}}},
            )
            return True
        except Exception as exc:
            if _is_resource_exists(exc):
                return False
            raise

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Factory + setup helpers
# ---------------------------------------------------------------------------

def make_client(backend: str, url: str, username: str = "",
                password: str = "", api_key: str = "",
                verify_tls: bool = True) -> SearchClient:
    if backend == "opensearch":
        return OpenSearchClient(
            url=url, username=username, password=password,
            verify_tls=verify_tls,
        )
    if backend == "elasticsearch":
        return ElasticsearchClient(
            url=url, username=username, password=password,
            api_key=api_key, verify_tls=verify_tls,
        )
    raise ValueError(f"unknown backend: {backend!r}")


def wait_for_cluster(client: SearchClient, stop_event,
                     max_backoff: float = 60.0) -> bool:
    """Block until the cluster responds to ping, with capped exponential backoff.

    Returns True when reachable, False if the stop event fires first.  Designed
    so startup survives the backend being unreachable (boot before ES is up,
    overnight maintenance windows, host reboots) without exiting the process.
    """
    backoff = 1.0
    attempt = 0
    while not stop_event.is_set():
        if client.ping():
            if attempt > 0:
                log.info("%s cluster reachable after %d attempt(s)",
                         client.backend, attempt + 1)
            return True
        attempt += 1
        log.warning("%s cluster unreachable (attempt %d); retrying in %.1fs",
                    client.backend, attempt, backoff)
        if stop_event.wait(backoff):
            return False
        backoff = min(backoff * 2, max_backoff)
    return False


def ensure_index_setup(client: SearchClient, *, template_name: str,
                       template_body: Dict, policy_name: str,
                       policy_body: Dict, alias: str,
                       initial_index: str) -> None:
    """Idempotently install index template, lifecycle policy, and write
    alias + initial backing index.

    Safe to call on every startup.  Logs but does not raise on policy/template
    write errors (the cluster may not allow them under the current user)."""
    try:
        client.put_lifecycle_policy(policy_name, policy_body)
        log.info("installed %s lifecycle policy %s", client.backend, policy_name)
    except Exception as exc:
        log.warning("could not install lifecycle policy %s: %s",
                    policy_name, exc)

    # Bind the lifecycle policy to the template for Elasticsearch.  OpenSearch
    # ISM ties the policy to indices via the policy's own ism_template; for ES,
    # ILM requires the template to carry index.lifecycle.name and rollover_alias
    # — without these, indices never roll over.
    if client.backend == "elasticsearch":
        settings = template_body.setdefault("template", {}).setdefault("settings", {})
        settings["index.lifecycle.name"] = policy_name
        settings["index.lifecycle.rollover_alias"] = alias

    try:
        client.put_index_template(template_name, template_body)
        log.info("installed index template %s", template_name)
    except Exception as exc:
        log.warning("could not install index template %s: %s",
                    template_name, exc)
    # Wrap the alias check + initial-index create: if either raises during a
    # backend hiccup we'd rather log and let the indexer carry on (it will
    # auto-create or retry on bulk) than crash startup.
    try:
        if not client.alias_exists(alias):
            created = client.create_initial_index(initial_index, alias)
            log.info("write alias %s -> %s (%s)", alias, initial_index,
                     "created" if created else "exists")
        else:
            log.info("write alias %s already exists", alias)
    except Exception as exc:
        log.warning("could not verify/create initial index %s: %s",
                    initial_index, exc)


def build_bulk_actions(docs, target, *, op_type=""):
    """Yield bulk-helper action dicts targeting *target*.

    Pass op_type='create' for ES data stream writes (required by data streams;
    rejects _op_type=index).  Leave empty for write-alias targets on either
    backend.
    """
    for doc in docs:
        action = {"_index": target, "_source": doc}
        if op_type:
            action["_op_type"] = op_type
        yield action
