"""Catálogo da plataforma ``wazuh`` (fonte) — APENAS metadata da UI.

Diferente dos demais vendors, Wazuh-como-FONTE não tem um ``BaseCollector`` no
registry (não há stream de coleta registrado — historicamente a plataforma
aparecia na tela de Nova Integração só com os campos de credencial). Mantemos
esse comportamento, mas agora de forma self-describing: este módulo registra só
a ``PlatformRegistration`` (sem ``CollectorRegistration``), eliminando o hardcode
que vivia em ``providers.py`` e no ``IntegrationForm.tsx``.

NB: Wazuh é, antes de tudo, um DESTINO de 1ª classe (syslog/lane catch-all). Este
registro é o lado FONTE (pull do Manager/Indexer).
"""

from __future__ import annotations

from ..capabilities import (
    CAP_QUERY_OPENSEARCH_DSL,
    DIALECT_OPENSEARCH_DSL,
    QUERY_MODE_LIVE,
    SPEC_PASSTHROUGH,
    SPEC_SIGMA,
    QueryCapability,
)

# Contrato de query do Wazuh — OpenSearch DSL ao vivo contra o
# Indexer (``wazuh-alerts-*``), síncrono. Sem teto de janela/rate-limit declarado
# (o Indexer é a fonte do próprio cliente). Fonte ÚNICA: o provider lê isto de volta
# via registry em ``query_capability()``.
WAZUH_QUERY_CAPABILITY = QueryCapability(
    dialect=DIALECT_OPENSEARCH_DSL,
    modes=(QUERY_MODE_LIVE,),
    supports_async=False,
    required_secrets=("indexer_username", "indexer_password"),
    ocsf_mapping_version="1",
    # opensearch_dsl tem backend pySigma → aceita spec_kind=sigma.
    spec_kinds=(SPEC_PASSTHROUGH, SPEC_SIGMA),
)


def _wazuh_provider(integration):
    """Factory tardia do ``WazuhProvider`` rico (alerts/health/query via Indexer).

    Import tardio evita puxar o pacote ``providers`` no boot do registry."""
    from ...providers.wazuh.provider import WazuhProvider

    return WazuhProvider(integration)


def _register() -> None:
    from ..registry import AuthField, PlatformRegistration, register_platform

    register_platform(
        PlatformRegistration(
            platform="wazuh",
            display_name="Wazuh",
            category="SIEM",
            description="Wazuh Manager + Indexer — SIEM open-source.",
            icon_id="wazuh",
            docs_url="https://documentation.wazuh.com/current/",
            order=30,
            provider_factory=_wazuh_provider,
            # as 4 credenciais (manager/indexer user+password) vivem no store
            # integration_credentials. Os usernames são ``type="secret"`` para que o
            # caminho genérico (_assign_credentials) os grave via write_secret —
            # preservando a cifragem-em-repouso que as colunas ``# encrypted`` tinham.
            required_secrets=(
                "manager_api_username", "manager_api_password",
                "indexer_username", "indexer_password",
            ),
            capabilities=frozenset({
                "catalog", "health", "alerts:list", "alerts:search",
                CAP_QUERY_OPENSEARCH_DSL,
                # Wazuh como FONTE — pull de detecções do Indexer.
                "collect:detections",
            }),
            query_capabilities=(WAZUH_QUERY_CAPABILITY,),
            auth_fields=(
                AuthField(key="indexer_url", label="Indexer URL", type="url", required=True,
                          help_text="URL do Wazuh Indexer — fonte de alertas, detecções e consultas (wazuh-alerts-*). Obrigatório."),
                AuthField(key="indexer_username", label="Indexer Username", type="secret", required=True),
                AuthField(key="indexer_password", label="Indexer Password", type="secret", required=True),
                AuthField(key="manager_url", label="Manager URL", type="url", required=False,
                          help_text="Opcional — saúde do servidor e inventário de agentes. Não coleta alertas/detecções."),
                AuthField(key="manager_api_username", label="Manager API Username", type="secret", required=False),
                AuthField(key="manager_api_password", label="Manager API Password", type="secret", required=False),
                AuthField(key="verify_ssl", label="Verificar SSL", type="bool", required=False,
                          help_text="Desativar somente em dev com certificado auto-assinado"),
            ),
        )
    )


_register()
