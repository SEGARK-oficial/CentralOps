"""Enricher **TAXII 2.1** — threat intel de QUALQUER plataforma, como tabela local.

**Por que este enricher existe, sendo que já temos OpenCTI.** O conector do
OpenCTI fala GraphQL, que é a API interna DELES: serve uma plataforma só e o
schema mudou entre as linhas 5.x e 6.x. TAXII 2.1 é padrão OASIS e é falado por
MISP, OpenCTI, Anomali, ThreatConnect e EclecticIQ, entre outros. Um conector,
todo o mercado de TIP — e o arranjo comum no mercado (MISP agregando feeds
abertos, um TIP comercial na camada do analista, os dois sincronizando por
STIX/TAXII) fica atendido sem escrever nada específico por fornecedor.

**Por que snapshot → tabela local, e não lookup por evento.** Mesma razão do
OpenCTI: o laço de coleta é serial, e um round-trip por evento a milhares de EPS
pediria mais I/O por segundo do que existe segundo. Uma coleção TAXII cabe em
memória e muda em minutos, não em milissegundos. Sendo LOCAL, roda no seam por
evento e portanto **alimenta a detecção em voo**; um lookup remoto resolveria só
no flush do lote e chegaria tarde para isso.

**Deletes.** O TAXII entrega snapshot, sem evento de remoção (o *live stream* do
OpenCTI tem, o TAXII não). Isso não nos custa nada porque a tabela é
RECONSTRUÍDA inteira a cada carga: um indicador removido na origem simplesmente
não vem, e some da tabela. A observação importa se um dia houver carga
incremental — aí o delete deixa de ser implícito e passa a exigir o stream.

**Egresso: entrada.** Nós buscamos a lista; nenhum indicador do ambiente do
cliente sai. Por isso o egresso é ``internal`` e não ``third_party``, mesmo o
servidor sendo de terceiro: o que viaja é a intel pública, não o dado do cliente.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, Literal, Mapping, Optional

import aiohttp
from pydantic import BaseModel, Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register
from ..runtime import DictLookupTable
from ..stix import parse_indicator

logger = logging.getLogger(__name__)

#: Media type obrigatório do TAXII 2.1 (OASIS). Servidor que recebe ``Accept``
#: errado responde 406, e o sintoma vira "coleção vazia" se ignorarmos o status.
_MEDIA_TYPE = "application/taxii+json;version=2.1"


class TaxiiConfig(BaseModel):
    """Config do enricher. Validada no cadastro da fonte, não em runtime."""

    #: SÓ o endereço base (esquema + host + porta). O caminho vai em ``api_root``.
    #: Separar não é preciosismo: ``normalize_service_url``, o guard de egresso do
    #: projeto, RECUSA path de propósito, e é ele que aplica a allowlist de host e
    #: CIDR. Enfiar a api-root aqui exigiria afrouxar o guard justamente no campo
    #: que viaja com a credencial no header.
    url: str = Field(..., description="Base do servidor, ex.: https://tip.exemplo")
    #: Caminho da api-root, o que o padrão chama de ``{api-root}``. Varia por
    #: plataforma (``/taxii2/``, ``/api/v21/``, ``/taxii/api2/``), por isso é
    #: configurável em vez de fixo.
    api_root: str = Field("/taxii2/", description="Caminho da api-root TAXII")
    #: Id da coleção. Obrigatório: sem ele teríamos que adivinhar qual das N
    #: coleções o operador quer, e adivinhar errado traz a intel errada.
    collection: str = Field(..., min_length=1, description="Id da coleção TAXII")
    #: ``basic`` é o esquema sugerido pelo próprio padrão; ``bearer`` é o que a
    #: maioria das plataformas comerciais usa na prática.
    auth_mode: Literal["basic", "bearer", "none"] = "bearer"
    #: Só para ``basic``. A senha é a credencial da fonte, nunca vai na config.
    username: Optional[str] = Field(None, max_length=200)
    #: Piso de confiança do Indicator STIX (0-100).
    min_confidence: int = Field(0, ge=0, le=100)
    page_size: int = Field(500, ge=1, le=5000)
    #: Teto de páginas por carga. Sem ele, uma coleção grande drena o ciclo
    #: inteiro: é o mesmo poison-loop que já derrubou coletor neste produto.
    max_pages: int = Field(40, ge=1, le=1000)
    timeout_s: float = Field(30.0, gt=0, le=300)
    verify_tls: bool = True

    @field_validator("api_root")
    @classmethod
    def _validate_api_root(cls, v: str) -> str:
        """Caminho relativo, sem host e sem subir de diretório.

        Recusar ``..`` e ``//`` impede que a api-root desfaça o guard aplicado à
        base: ``//outro-host/`` no ``urljoin`` trocaria o host inteiro, e aí a
        allowlist teria sido verificada contra um endereço que não é o chamado.
        """
        v = (v or "/").strip()
        if "://" in v or v.startswith("//"):
            raise ValueError("api_root é um caminho, não uma URL")
        if ".." in v:
            raise ValueError("api_root não pode conter '..'")
        if "?" in v or "#" in v:
            raise ValueError("api_root não aceita query nem fragmento")
        if not v.startswith("/"):
            v = "/" + v
        return v if v.endswith("/") else v + "/"

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        """Passa pelo guard de egresso do projeto (``core.url_policy``).

        A URL vem da fonte configurada, escrita por um admin de organização, e
        vai direto para ``aiohttp`` COM a credencial no header ``Authorization``.
        Sem este guard, um endereço de metadados de nuvem ou um host externo
        qualquer transformaria o enricher em SSRF **com exfiltração de
        credencial**. Mesmo guard já aplicado em okta, crowdstrike, veeam e wazuh.
        """
        from ....core.url_policy import normalize_service_url

        try:
            normalized = normalize_service_url(v)
        except ValueError as exc:
            raise ValueError(f"url inválida: {exc}") from exc
        if not normalized:
            raise ValueError("url é obrigatória")
        return normalized


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip", "domain", "url", "file_hash", "mac"}),
    mode="local",
    supports_bulk=True,
    # Orçamento da CARGA (I/O), não da aplicação por evento.
    p99_budget_ms=5_000.0,
    suggested_ttl_s=900,
    suggested_negative_ttl_s=300,
    emits_pii=False,
    license="do provedor do feed",
    redistributable=False,
    # Buscamos a lista; nada do ambiente do cliente sai.
    egress="internal",
)


class TaxiiEnricher:
    """Baixa uma coleção TAXII 2.1 e materializa uma :class:`DictLookupTable`."""

    caps = _CAPS

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = TaxiiConfig(**dict(config or {}))

    def _auth_header(self, secret: Optional[str]) -> Dict[str, str]:
        mode = self._cfg.auth_mode
        if mode == "none" or not secret:
            return {}
        if mode == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        # basic: usuário da config, senha da credencial.
        raw = f"{self._cfg.username or ''}:{secret}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}

    def _objects_url(self) -> str:
        """``{base}{api_root}collections/{id}/objects/``, como manda o padrão.

        Sem GET de discovery a cada carga: discovery pode listar N api-roots, e
        escolher uma por conta própria reintroduz a adivinhação que os campos
        explícitos evitam. Quem sabe qual api-root quer é o operador.
        """
        base = self._cfg.url.rstrip("/")
        return f"{base}{self._cfg.api_root}collections/{self._cfg.collection}/objects/"

    async def load(self, ctx: EnrichContext) -> DictLookupTable:
        secret = await _resolve_secret(ctx)
        headers = {"Accept": _MEDIA_TYPE, **self._auth_header(secret)}
        rows: Dict[str, Dict[str, Any]] = {}
        endpoint = self._objects_url()

        timeout = aiohttp.ClientTimeout(total=self._cfg.timeout_s)
        connector = aiohttp.TCPConnector(ssl=None if self._cfg.verify_tls else False)
        # `match[type]=indicator` filtra NO SERVIDOR. Sem isso a coleção inteira
        # (malware, campaigns, relationships) viria pela rede para ser descartada
        # aqui, e o custo é do cliente E do servidor do feed.
        params: Dict[str, Any] = {
            "limit": self._cfg.page_size,
            "match[type]": "indicator",
        }
        truncated = False

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            for page in range(self._cfg.max_pages):
                async with session.get(endpoint, headers=headers, params=params) as resp:
                    if resp.status in (401, 403):
                        raise PermissionError(
                            f"TAXII recusou a credencial (HTTP {resp.status}) em {endpoint}"
                        )
                    if resp.status == 406:
                        # Sintoma clássico: sem o Accept certo o servidor recusa,
                        # e tratar como "vazio" produziria tabela vazia silenciosa.
                        raise ValueError(
                            "servidor TAXII recusou o media type "
                            f"{_MEDIA_TYPE!r} (HTTP 406) — confira se ele fala 2.1"
                        )
                    resp.raise_for_status()
                    body = await resp.json(content_type=None)

                objects = (body or {}).get("objects") or []
                for obj in objects:
                    if not isinstance(obj, Mapping):
                        continue
                    parsed = parse_indicator(
                        obj, min_confidence=self._cfg.min_confidence, source_name="taxii"
                    )
                    if parsed is not None:
                        key, value = parsed
                        rows[key] = value

                if not (body or {}).get("more"):
                    break
                cursor = (body or {}).get("next")
                if not cursor:
                    # `more: true` sem `next` é servidor fora do padrão. Parar é
                    # o certo: continuar pediria a mesma página para sempre.
                    logger.warning(
                        "TAXII: 'more' verdadeiro sem 'next' — parando em %d indicadores",
                        len(rows),
                        extra={"event": "enrich.taxii.no_cursor"},
                    )
                    break
                params["next"] = cursor
            else:
                truncated = True

        if truncated:
            logger.warning(
                "TAXII: teto de %d páginas atingido — a tabela está TRUNCADA (%d "
                "indicadores). Aumente max_pages ou suba min_confidence.",
                self._cfg.max_pages, len(rows),
                extra={"event": "enrich.taxii.truncated"},
            )
        return DictLookupTable(rows)


async def _resolve_secret(ctx: EnrichContext) -> Optional[str]:
    """Resolve a credencial no cofre. 1×/carga, JAMAIS por evento.

    A referência vem SÓ do servidor (``ctx.secret_ref``, preenchido a partir da
    ``EnrichmentSource`` escopada à org). Config não carrega segredo: como o
    cofre decifra qualquer ciphertext sem olhar organização, aceitar a referência
    pela config deixaria um admin usar a credencial de outra org.
    """
    ref = ctx.secret_ref
    if not ref:
        return None
    try:
        from ....core import secrets as secrets_mod

        backend = secrets_mod.get_default_backend()
        return await asyncio.to_thread(backend.decrypt, ref)
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(f"não foi possível resolver o segredo: {exc}") from exc


register(
    EnricherRegistration(
        name="taxii",
        factory=lambda cfg: TaxiiEnricher(cfg),
        caps=_CAPS,
        config_schema=TaxiiConfig,
        required_secrets=("token",),
        label="TAXII 2.1",
        category="Threat Intel",
        description=(
            "Indicadores de qualquer plataforma que fale TAXII 2.1 (MISP, OpenCTI, "
            "Anomali, ThreatConnect, EclecticIQ), sincronizados como tabela local. "
            "Padrão OASIS: um conector serve todas. Roda no caminho por evento, "
            "então alimenta as regras de detecção em pipeline."
        ),
        icon_id="taxii",
        tier="beta",
        order=5,
    )
)
