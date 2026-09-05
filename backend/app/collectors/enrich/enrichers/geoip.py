"""Enricher **GeoIP / ASN** — base MaxMind ``.mmdb`` local (ADR-LOCAL-0002, W4.4).

O enriquecimento mais pedido em POC e o único do catálogo que não faz I/O
nenhum: país, cidade, coordenadas e ASN a partir de um arquivo ``.mmdb`` que o
operador coloca no worker. Roda em ``mode="local"``, no seam por evento — e por
isso ALIMENTA a detecção em voo (``_centralops.enrichment.geo.country_iso ne
"BR"`` é uma regra, não uma consulta).

**A base NÃO é embarcada.** GeoLite2 tem EULA com license key individual e
GeoIP2 é comercial: ``redistributable=False`` é o que impede alguém de colar o
arquivo na imagem publicada (o registro recusa ``embedded_dataset=True``). O
operador baixa a base com a própria conta, monta em ``ENRICH_GEOIP_DIR`` e
aponta a fonte para o NOME do arquivo — nunca um caminho: a config é escrita por
admin de organização via API, e um caminho livre seria ler qualquer arquivo do
worker que o parser mmdb aceitasse.

**Memória.** O reader abre o arquivo por ``mmap``: as páginas residentes são
page cache do SO, compartilhadas entre os N forks do worker, não heap por fork.
Por isso ``approx_bytes`` declara um resident estimado pequeno, e não os 70 MB
do GeoLite2-City — declarar o tamanho do arquivo faria o teto por fork
(``ENRICH_MAX_TABLE_BYTES``, 32 MiB) recusar a base mais útil do catálogo por
um custo que ela não tem.

**Saída achatada e estável.** O mmdb devolve dicts aninhados e localizados
(``names.en``, ``names.pt-BR``…); a tabela expõe campos planos com nome fixo
(``country_iso``, ``city``, ``asn``…) para a DSL poder validar ``from`` contra
``output_fields`` e para a regra não depender do idioma da base.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

from ..contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from ..registry import register

logger = logging.getLogger(__name__)

#: Resident estimado de um reader mmap: cabeçalho + metadados + páginas quentes.
#: O arquivo inteiro NÃO é heap do fork (ver docstring do módulo).
_MMAP_RESIDENT_ESTIMATE = 512 * 1024

_KINDS = ("city", "country", "asn")


class GeoIPConfig(BaseModel):
    model_config = {"extra": "forbid"}

    #: NOME do arquivo dentro de ``ENRICH_GEOIP_DIR`` — nunca caminho.
    file: str = Field(..., min_length=1, max_length=255, description="ex.: GeoLite2-City.mmdb")
    #: O que o arquivo é. Decide os campos de saída; ``city`` inclui os de ``country``.
    kind: str = Field("city", description="city | country | asn")

    @field_validator("file")
    @classmethod
    def _basename_only(cls, value: str) -> str:
        """Só o nome: barra, ``..`` ou separador de diretório são recusados. É o
        que impede um admin de org apontar a fonte para fora do diretório de
        bases — a config é escrita via API, não por quem administra o worker."""
        v = value.strip()
        if not v or v != os.path.basename(v) or ".." in v or "/" in v or "\\" in v:
            raise ValueError("file deve ser só o NOME do arquivo .mmdb (sem diretório)")
        if not v.lower().endswith(".mmdb"):
            raise ValueError("file deve terminar em .mmdb")
        return v

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in _KINDS:
            raise ValueError(f"kind inválido: {value!r}. Válidos: {list(_KINDS)}")
        return value


def _names_en(node: Any) -> Optional[str]:
    if not isinstance(node, Mapping):
        return None
    names = node.get("names")
    if isinstance(names, Mapping):
        return names.get("en") or next(iter(names.values()), None)
    return None


def flatten_record(record: Any, kind: str) -> Optional[Dict[str, Any]]:
    """mmdb (aninhado, localizado) → campos planos com nome fixo. ``None`` se vazio."""
    if not isinstance(record, Mapping) or not record:
        return None
    out: Dict[str, Any] = {"source": "geoip"}
    if kind == "asn":
        asn = record.get("autonomous_system_number")
        org = record.get("autonomous_system_organization")
        if asn is None and org is None:
            return None
        out["asn"] = int(asn) if asn is not None else None
        out["asn_org"] = org
        return out
    country = record.get("country") or {}
    registered = record.get("registered_country") or {}
    continent = record.get("continent") or {}
    out["country_iso"] = country.get("iso_code") or registered.get("iso_code")
    out["country_name"] = _names_en(country) or _names_en(registered)
    out["continent_code"] = continent.get("code")
    if kind == "city":
        city = record.get("city") or {}
        location = record.get("location") or {}
        subdivisions = record.get("subdivisions") or []
        out["city"] = _names_en(city)
        out["subdivision"] = _names_en(subdivisions[0]) if subdivisions else None
        out["subdivision_iso"] = (subdivisions[0].get("iso_code") if subdivisions and isinstance(subdivisions[0], Mapping) else None)
        out["latitude"] = location.get("latitude")
        out["longitude"] = location.get("longitude")
        out["accuracy_radius_km"] = location.get("accuracy_radius")
        out["timezone"] = location.get("time_zone")
    if all(v is None for k, v in out.items() if k != "source"):
        return None
    return out


class _MmdbLookupTable:
    """:class:`~..contract.LookupTable` sobre o reader mmdb. Pura: ``get`` é
    leitura de memória mapeada, nunca I/O de rede, nunca levanta."""

    __slots__ = ("_reader", "_kind", "_entries", "_bytes")

    def __init__(self, reader: Any, kind: str, *, entries: int, approx_bytes: int) -> None:
        self._reader = reader
        self._kind = kind
        self._entries = int(entries)
        self._bytes = int(approx_bytes)

    def lookup(self, key: str) -> Optional[Mapping[str, Any]]:
        try:
            return flatten_record(self._reader.get(key), self._kind)
        except Exception:  # noqa: BLE001 — IP inválido/IPv6 malformado = miss, nunca erro
            return None

    @property
    def entry_count(self) -> int:
        return self._entries

    @property
    def approx_bytes(self) -> int:
        return self._bytes


def _geoip_dir() -> str:
    from ....core.config import settings

    return str(getattr(settings, "ENRICH_GEOIP_DIR", "") or "/var/lib/centralops/geoip")


def _open_reader(path: str) -> Any:
    """Isolado para os testes trocarem o leitor sem um .mmdb real no disco."""
    import maxminddb

    return maxminddb.open_database(path, maxminddb.MODE_AUTO)


class GeoIPEnricher:
    caps: EnricherCapabilities  # definido abaixo

    def __init__(self, config: Mapping[str, Any]) -> None:
        self._cfg = GeoIPConfig(**dict(config or {}))

    async def load(self, ctx: EnrichContext) -> _MmdbLookupTable:
        import asyncio

        path = os.path.join(_geoip_dir(), self._cfg.file)
        if not os.path.isfile(path):
            # Fail-LOUD na carga (vira ``config`` na métrica e no ring): uma
            # base ausente não pode virar "todo IP é miss" em silêncio.
            raise FileNotFoundError(
                f"base GeoIP {self._cfg.file!r} não encontrada em {_geoip_dir()} — "
                "baixe-a com a sua conta MaxMind e monte o diretório no worker "
                "(ENRICH_GEOIP_DIR)"
            )
        reader = await asyncio.to_thread(_open_reader, path)
        meta = reader.metadata()
        db_type = str(getattr(meta, "database_type", "") or "")
        # Base de um tipo e config de outro é o erro silencioso clássico aqui:
        # GeoLite2-ASN aberto como ``city`` devolve None para tudo.
        if self._cfg.kind == "asn" and "ASN" not in db_type:
            logger.warning(
                "enrich: base %r é %r, mas a fonte declara kind=asn — verifique a config",
                self._cfg.file, db_type, extra={"event": "enrich.geoip.kind_mismatch"},
            )
        if self._cfg.kind != "asn" and "ASN" in db_type:
            logger.warning(
                "enrich: base %r é %r, mas a fonte declara kind=%s — verifique a config",
                self._cfg.file, db_type, self._cfg.kind,
                extra={"event": "enrich.geoip.kind_mismatch"},
            )
        return _MmdbLookupTable(
            reader, self._cfg.kind,
            entries=int(getattr(meta, "node_count", 0) or 0),
            approx_bytes=_MMAP_RESIDENT_ESTIMATE,
        )


_CAPS = EnricherCapabilities(
    key_kinds=frozenset({"ip"}),
    mode="local",
    supports_bulk=True,
    p99_budget_ms=10.0,
    # A base muda a cada semana (GeoLite2) ou mês; o valor de um IP não muda
    # em horas. O cache aqui é o próprio mmap.
    suggested_ttl_s=86_400,
    suggested_negative_ttl_s=3_600,
    emits_pii=False,
    license="MaxMind GeoLite2 EULA / GeoIP2 (comercial)",
    # É o caso concreto que motivou o campo: a base NÃO pode ir na imagem.
    redistributable=False,
    egress="none",
)
GeoIPEnricher.caps = _CAPS

OUTPUT_FIELDS: Dict[str, str] = {
    "country_iso": "ISO 3166-1 alpha-2 do país (city, country)",
    "country_name": "Nome do país em inglês (city, country)",
    "continent_code": "Código do continente (city, country)",
    "city": "Cidade (city)",
    "subdivision": "Estado/província (city)",
    "subdivision_iso": "Código ISO da subdivisão (city)",
    "latitude": "Latitude aproximada (city)",
    "longitude": "Longitude aproximada (city)",
    "accuracy_radius_km": "Raio de precisão em km (city)",
    "timezone": "Fuso horário IANA (city)",
    "asn": "Autonomous System Number (asn)",
    "asn_org": "Organização dona do AS (asn)",
    "source": "Constante 'geoip', proveniência",
}

register(
    EnricherRegistration(
        name="geoip",
        factory=lambda cfg: GeoIPEnricher(cfg),
        caps=_CAPS,
        config_schema=GeoIPConfig,
        required_secrets=(),
        label="GeoIP / ASN (MaxMind mmdb)",
        category="Geolocalização",
        description=(
            "País, cidade, coordenadas e ASN a partir de uma base MaxMind .mmdb "
            "montada no worker (GeoLite2 gratuita com conta, ou GeoIP2). Sem rede: "
            "roda por evento e alimenta a detecção em voo. A base não é distribuída "
            "com o produto — aponte o NOME do arquivo em ENRICH_GEOIP_DIR."
        ),
        icon_id="globe",
        docs_url="https://dev.maxmind.com/geoip/geolite2-free-geolocation-data",
        tier="beta",
        order=5,
        output_fields=OUTPUT_FIELDS,
    )
)
