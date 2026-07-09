"""Thin synchronous HTTP client for DFIR-Iris customer management.

Used by the Sophos Partner sync task to auto-create Iris customers when an
Organization is provisioned from a discovered tenant. Idempotent at the API
layer — a duplicate ``add`` returns 200 with the existing customer.

Configuration:
  * ``DFIR_IRIS_URL``           — base URL (e.g. ``https://iris.internal``)
  * ``DFIR_IRIS_API_KEY``       — bearer token from the Iris UI
  * ``DFIR_IRIS_TLS_SKIP_VERIFY`` — opt-in for self-signed dev environments

This module deliberately does **not** depend on the Iris MCP server. The MCP
server is for the LLM in IASOC; here we need a tiny direct REST integration
inside CentralOps's request/Celery workers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from ..core.config import settings

logger = logging.getLogger(__name__)


class IrisConfigurationError(RuntimeError):
    """Raised when DFIR_IRIS_URL / DFIR_IRIS_API_KEY are not configured."""


class IrisApiError(RuntimeError):
    """Raised when the Iris API returns an unexpected error."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class IrisClient:
    """Minimal sync client for the manage/customers endpoints we need."""

    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        verify_tls: Optional[bool] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = (base_url or settings.DFIR_IRIS_URL or "").rstrip("/")
        self.api_key = api_key or settings.DFIR_IRIS_API_KEY or ""
        if verify_tls is None:
            verify_tls = not bool(settings.DFIR_IRIS_TLS_SKIP_VERIFY)
        self._verify_tls = verify_tls
        self._timeout = timeout
        self._http: Optional[httpx.Client] = None
        # Cache de /manage/customers/list por instância (o sync cria um
        # IrisClient por execução). Evita baixar a lista inteira a CADA tenant
        # — antes eram O(n) GETs idênticos, somando timeouts até estourar o
        # soft time limit da task de sync. ``_list_error`` cacheia a FALHA: se
        # o list deu timeout uma vez, os tenants seguintes falham na hora em
        # vez de repetir o timeout (30s) por tenant.
        self._customers_index: Optional[Dict[str, Dict[str, Any]]] = None
        self._list_error: Optional[IrisApiError] = None

    def _ensure_configured(self) -> None:
        if not self.base_url or not self.api_key:
            raise IrisConfigurationError(
                "DFIR_IRIS_URL and DFIR_IRIS_API_KEY must be configured to "
                "auto-create Iris customers"
            )

    def _client(self) -> httpx.Client:
        if self._http is None:
            # connect curto (falha rápido quando o Iris não é alcançável da
            # rede do worker) separado do read (= self._timeout). Antes um
            # único valor cobria ambos, fazendo cada tenant esperar o timeout
            # cheio só para descobrir que não conecta.
            self._http = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                verify=self._verify_tls,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        return self._http

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                pass
            self._http = None

    def __enter__(self) -> "IrisClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    # ── Customer management ─────────────────────────────────────────

    def _load_customers_index(self) -> Dict[str, Dict[str, Any]]:
        """Carrega + indexa ``/manage/customers/list`` UMA vez por instância.

        Sucesso e falha são cacheados: a primeira chamada faz o GET; as
        seguintes reusam o índice em memória (ou re-levantam o erro), de modo
        que um Iris lento/inacessível custa UM timeout no sync inteiro — não um
        por tenant (o que estourava o soft time limit de 12 min da task).
        """
        if self._customers_index is not None:
            return self._customers_index
        if self._list_error is not None:
            raise self._list_error
        self._ensure_configured()
        try:
            response = self._client().get("/manage/customers/list")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self._list_error = IrisApiError(f"Iris customers/list failed: {exc}")
            raise self._list_error from exc
        body = response.json() or {}
        # Iris wraps responses in {"status": "success", "data": [...]}.
        items = body.get("data") if isinstance(body, dict) else body
        index: Dict[str, Dict[str, Any]] = {}
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("customer_name"):
                    index[str(item["customer_name"])] = item
        self._customers_index = index
        return index

    def find_customer_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Look up an existing customer by exact name match.

        Backed by a per-instance cache of the full customer list (loaded once
        via ``_load_customers_index``). Returns the customer payload (with
        ``customer_id``) or ``None`` when no match is found.
        """
        return self._load_customers_index().get(name)

    def add_customer(
        self,
        *,
        name: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new customer. Returns the created customer payload.

        On duplicate name, falls back to looking up the existing record so
        that the caller can treat creation as idempotent.
        """
        self._ensure_configured()
        existing = self.find_customer_by_name(name)
        if existing is not None:
            return existing
        payload: Dict[str, Any] = {"customer_name": name}
        if description:
            payload["customer_description"] = description
        try:
            response = self._client().post("/manage/customers/add", json=payload)
        except httpx.HTTPError as exc:
            raise IrisApiError(f"Iris customers/add failed: {exc}") from exc

        if response.status_code in (200, 201):
            body = response.json() or {}
            data = body.get("data") if isinstance(body, dict) else None
            result = data if isinstance(data, dict) else (body if isinstance(body, dict) else {})
            # Mantém o índice cacheado coerente para os próximos tenants do
            # mesmo sync (idempotência intra-execução, sem novo GET).
            if self._customers_index is not None and result.get("customer_name"):
                self._customers_index[str(result["customer_name"])] = result
            return result
        # Some Iris versions return 400 on duplicate name — recover via lookup.
        # Invalida o cache: a lista pode ter mudado desde o load inicial.
        if response.status_code == 400:
            self._customers_index = None
            self._list_error = None
            again = self.find_customer_by_name(name)
            if again is not None:
                return again
        raise IrisApiError(
            f"Iris customers/add failed with status={response.status_code}: {response.text[:200]}",
            status_code=response.status_code,
        )

    @staticmethod
    def extract_customer_id(payload: Dict[str, Any]) -> Optional[int]:
        """Pull the numeric ``customer_id`` from various Iris response shapes."""
        if not isinstance(payload, dict):
            return None
        candidate = payload.get("customer_id") or payload.get("id")
        if candidate is None:
            return None
        try:
            return int(candidate)
        except (TypeError, ValueError):
            return None
