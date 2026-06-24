import subprocess
import json
import time
import httpx
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Cache for discovered hosts
_hosts_cache: List[str] = []
_hosts_cache_time: float = 0
_HOSTS_CACHE_TTL = 60  # seconds

# Cache for the full discover_models() result so the keepalive loop can
# short-circuit and avoid the 50-thread port scan when nothing has changed.
_endpoint_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
_endpoint_cache_key: Optional[tuple] = None
_endpoint_cache_time: float = 0
_ENDPOINT_CACHE_TTL = 300  # 5 minutes

# Shared sync httpx.Client — lazily created on first use so import-time
# construction never binds to a closed event loop. All _check_port and
# _fingerprint_provider calls reuse its connection pool.
_shared_client: Optional[httpx.Client] = None


def _get_shared_client() -> httpx.Client:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.Client(
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(3.0, connect=2.0),
        )
    return _shared_client


def _endpoint_cache_key(default_host: str) -> tuple:
    return (
        os.getenv("LLM_HOSTS", "").strip(),
        os.getenv("LLM_HOST", "").strip(),
        default_host,
        os.getenv("OLLAMA_BASE_URL", "").strip(),
        os.getenv("OLLAMA_URL", "").strip(),
        os.getenv("LM_STUDIO_URL", "").strip(),
    )


def _parse_tailscale_status(raw: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _first_tailscale_ipv4(value: Any) -> Optional[str]:
    if not isinstance(value, list):
        return None
    for ip in value:
        if isinstance(ip, str) and "." in ip:
            return ip
    return None


def discover_tailscale_hosts() -> List[str]:
    """Discover online Tailscale peers, returning their IPv4 addresses."""
    global _hosts_cache, _hosts_cache_time

    now = time.time()
    if _hosts_cache and (now - _hosts_cache_time) < _HOSTS_CACHE_TTL:
        return list(_hosts_cache)

    hosts = []
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"], capture_output=True, text=True, timeout=2
        )
        if result.returncode != 0:
            return hosts

        data = _parse_tailscale_status(result.stdout)
        if not data:
            return hosts

        # Add self
        self_data = data.get("Self") if isinstance(data.get("Self"), dict) else {}
        self_ip = _first_tailscale_ipv4(self_data.get("TailscaleIPs"))
        if self_ip:
            hosts.append(self_ip)

        # Add online peers (skip funnel-ingress-nodes and android devices)
        peers = data.get("Peer") if isinstance(data.get("Peer"), dict) else {}
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            if not peer.get("Online"):
                continue
            hostname = peer.get("HostName", "")
            if hostname == "funnel-ingress-node":
                continue
            os_name = peer.get("OS", "")
            if os_name == "android":
                continue
            peer_ip = _first_tailscale_ipv4(peer.get("TailscaleIPs"))
            if peer_ip:
                hosts.append(peer_ip)

        _hosts_cache = hosts
        _hosts_cache_time = now
        logger.info(f"Tailscale discovery found {len(hosts)} hosts: {hosts}")
    except FileNotFoundError:
        logger.debug("tailscale command not found")
    except Exception as e:
        logger.warning(f"Tailscale discovery failed: {e}")

    return hosts


class ModelDiscovery:
    def __init__(self, default_host: str, openai_api_key: Optional[str] = None):
        self.default_host = default_host
        self.openai_api_key = openai_api_key
        self.openai_compat_path = "/v1/chat/completions"
        # Custom ports from env vars, merged into the scan list by discover_models.
        self._extra_ports: set = set()

    def _get_hosts(self) -> List[str]:
        """Get all hosts to scan, using env override, Tailscale, or default."""
        self._extra_ports = set()

        def _append_host(out: List[str], host: str) -> None:
            host = (host or "").strip()
            if not host or host in out:
                return
            out.append(host)

        def _append_env_hosts(out: List[str]) -> None:
            """Add hosts (and any custom ports) from provider-specific env vars."""
            for env_name in ("OLLAMA_BASE_URL", "OLLAMA_URL", "LM_STUDIO_URL"):
                raw = os.getenv(env_name, "").strip()
                if not raw:
                    continue
                try:
                    parsed = urlparse(raw if "://" in raw else "http://" + raw)
                    _append_host(out, parsed.hostname or "")
                    if parsed.port:
                        self._extra_ports.add(parsed.port)
                except Exception:
                    pass

        # Manual override takes priority
        extra = os.getenv("LLM_HOSTS", "").strip()
        if extra:
            hosts = [h.strip() for h in extra.split(",") if h.strip()]
            # Always include the default host too
            if self.default_host not in hosts:
                hosts.insert(0, self.default_host)
            _append_host(hosts, "host.docker.internal")
            _append_env_hosts(hosts)
            return hosts

        # Try Tailscale discovery
        ts_hosts = discover_tailscale_hosts()
        if ts_hosts:
            # Ensure default_host is included
            if self.default_host not in ts_hosts:
                ts_hosts.insert(0, self.default_host)
            _append_host(ts_hosts, "host.docker.internal")
            _append_env_hosts(ts_hosts)
            return ts_hosts

        hosts = [self.default_host]
        # Docker desktop/Linux compose maps this to the host machine. That is
        # the common "I started Ollama normally on this computer" case.
        _append_host(hosts, "host.docker.internal")
        _append_env_hosts(hosts)
        return hosts

    def _fingerprint_provider(self, host: str, port: int) -> Optional[str]:
        """Identify the server software via its native API, independent of port."""
        try:
            r = _get_shared_client().get(
                f"http://{host}:{port}/api/v1/models", timeout=1.5
            )
            if r.is_success:
                models = (r.json() or {}).get("models")
                if (
                    isinstance(models, list)
                    and models
                    and isinstance(models[0], dict)
                    and "key" in models[0]
                    and "architecture" in models[0]
                ):
                    return "lmstudio"
        except Exception:
            pass
        return None

    def _check_port(self, host: str, port: int) -> Optional[Dict[str, Any]]:
        """Check a single host:port for models."""
        base = f"http://{host}:{port}/v1"
        try:
            r = _get_shared_client().get(f"{base}/models", timeout=3)
            if not r.is_success:
                return None
            data = r.json() or {}
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if ids:
                return {
                    "host": host,
                    "port": port,
                    "url": f"http://{host}:{port}{self.openai_compat_path}",
                    "models": ids,
                    "models_display": [i.lstrip("/") for i in ids],
                    "provider": self._fingerprint_provider(host, port),
                }
        except Exception:
            pass
        return None

    def discover_models(self) -> Dict[str, List[Dict[str, Any]]]:
        """Discover available models from all reachable hosts."""
        global _endpoint_cache, _endpoint_cache_key, _endpoint_cache_time

        cache_key = _endpoint_cache_key(self.default_host)
        now = time.time()
        if (
            _endpoint_cache is not None
            and _endpoint_cache_key == cache_key
            and (now - _endpoint_cache_time) < _ENDPOINT_CACHE_TTL
        ):
            return _endpoint_cache

        hosts = self._get_hosts()
        items = []

        logger.info(f"Scanning {len(hosts)} hosts for models: {hosts}")

        # Well-known ports: 8000-8020 (vLLM, llama.cpp, SGLang, Cookbook),
        # 1234 (LM Studio), 11434 (Ollama), 11435 for APFEL as its default port is
        # occupied by Ollama. The env vars can add more ports which will be merged in.
        ports = list(range(8000, 8021)) + [1234, 11434, 11435]
        ports += [p for p in sorted(self._extra_ports) if p not in ports]
        targets = [(h, p) for h in hosts for p in ports]

        seen_models = (
            set()
        )  # dedupe by (port, model_ids) to avoid same machine via different IPs

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self._check_port, h, p): (h, p) for h, p in targets}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    key = (result["port"], tuple(sorted(result["models"])))
                    if key not in seen_models:
                        seen_models.add(key)
                        items.append(result)

        # Sort by host then port for consistent ordering
        items.sort(key=lambda x: (x["host"], x["port"]))

        logger.info(
            f"Discovered {len(items)} model endpoints across {len(hosts)} hosts"
        )
        result = {"hosts": hosts, "items": items}
        _endpoint_cache = result
        _endpoint_cache_key = cache_key
        _endpoint_cache_time = now
        return result

    def warmup_ping_urls(self, limit: int = 5) -> List[str]:
        """The ``/models`` URLs of up to ``limit`` discovered endpoints.

        Used by the startup warmup / keepalive loop to prime connections. Each
        discovered item already carries a ``/v1/chat/completions`` url; swap the
        suffix for the cheap ``/models`` probe. Failures degrade to an empty list
        so warmup never crashes the caller.
        """
        try:
            items = (self.discover_models() or {}).get("items", [])
        except Exception:
            return []
        urls: List[str] = []
        for ep in items[:limit]:
            url = (ep.get("url") or "").replace("/chat/completions", "/models")
            if url:
                urls.append(url)
        return urls

    def ping_known_urls(self, limit: int = 5) -> List[str]:
        """Return ``/models`` URLs from the cached discovery result only.

        Unlike ``warmup_ping_urls``, this does NOT trigger a port scan: it
        reads the same TTL cache that ``discover_models`` populates. The
        keepalive loop should prefer this method so a hot call is O(1) and
        never starts a thread pool. Returns an empty list when the cache is
        empty, stale, or the inputs have changed since it was filled.
        """
        if _endpoint_cache is None:
            return []
        if _endpoint_cache_key != _endpoint_cache_key(self.default_host):
            return []
        if (time.time() - _endpoint_cache_time) >= _ENDPOINT_CACHE_TTL:
            return []
        items = _endpoint_cache.get("items", [])
        urls: List[str] = []
        for ep in items[:limit]:
            url = (ep.get("url") or "").replace("/chat/completions", "/models")
            if url:
                urls.append(url)
        return urls

    def get_providers(self) -> Dict[str, Any]:
        """Get all available providers"""
        discovery = self.discover_models()
        items = discovery["items"]
        providers = [{"provider": "vllm", "hosts": discovery["hosts"], "items": items}]

        if self.openai_api_key:
            openai_models = [
                "gpt-5.2-codex",
                "gpt-4o-mini",
                "gpt-image-1.5",
                "gpt-4o",
                "gpt-5.2",
                "gpt-5.2-pro",
            ]
            providers.append(
                {
                    "provider": "openai",
                    "items": [
                        {
                            "url": "https://api.openai.com/v1/chat/completions",
                            "models": openai_models,
                        }
                    ],
                }
            )

        return {"providers": providers}
