"""Catálogo da plataforma ``lake`` — search-in-place no S3/lake.

Vendor de QUERY puro (sem collector): consulta os objetos que os sinks
(``s3``/``security_lake``) já escreveram, via ``LakeProvider.run_query`` com filtro
estruturado. Zero-core: só este módulo + o provider. Config NÃO-secreta
(layout/prefix/source) vai p/ ``Integration.config_json``; bucket/region/access-key
reusam as colunas genéricas (convenção do CloudTrail); secret no store.
"""

from __future__ import annotations

from ..capabilities import (
    CAP_QUERY_LAKE_FILTER,
    DIALECT_LAKE_FILTER,
    QUERY_MODE_LIVE,
    QueryCapability,
)

# Sem ``max_window`` (lake é storage frio — sem custo de API por janela); o teto é
# por LINHAS (``QUERY_LAKE_MAX_ROWS``), não por tempo. Passthrough (filtro estruturado).
LAKE_QUERY_CAPABILITY = QueryCapability(
    dialect=DIALECT_LAKE_FILTER,
    modes=(QUERY_MODE_LIVE,),
    supports_async=False,
    required_secrets=("secret_access_key",),
    ocsf_mapping_version="1",
)


def _lake_provider(integration):
    from ...providers.lake.provider import LakeProvider

    return LakeProvider(integration)


def _register() -> None:
    from ..registry import AuthField, PlatformRegistration, register_platform

    register_platform(
        PlatformRegistration(
            platform="lake",
            display_name="Data Lake (S3)",
            category="Data Lake",
            description="Search-in-place sobre o S3/lake escrito pelos destinos.",
            icon_id="database",
            docs_url="https://docs.aws.amazon.com/security-lake/",
            order=40,
            provider_factory=_lake_provider,
            required_secrets=("secret_access_key",),
            capabilities=frozenset({"catalog", "health", CAP_QUERY_LAKE_FILTER}),
            query_capabilities=(LAKE_QUERY_CAPABILITY,),
            auth_fields=(
                AuthField(key="base_url", label="Bucket S3", type="string", required=True,
                          help_text="Nome do bucket onde os destinos gravam (ex.: my-security-lake)"),
                AuthField(key="region", label="Região AWS", type="string", required=True,
                          help_text="ex.: us-east-1"),
                AuthField(key="client_id", label="Access Key ID", type="string", required=True),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret", required=True),
                AuthField(key="layout", label="Layout", type="select", required=True,
                          options=("s3_ndjson", "security_lake_parquet"),
                          help_text="s3_ndjson (sink s3, partição org=) ou security_lake_parquet"),
                AuthField(key="prefix", label="Prefixo (s3_ndjson)", type="string", required=False,
                          help_text="Prefixo do sink s3 (default: centralops)"),
                AuthField(key="source", label="Custom source (security_lake)", type="string", required=False,
                          help_text="Nome da custom source (default: centralops)"),
                AuthField(key="tenant_id", label="Account ID (security_lake)", type="string", required=False,
                          help_text="AWS Account ID — compõe a partição accountId="),
            ),
        )
    )


_register()
