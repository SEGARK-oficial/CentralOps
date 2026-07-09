#!/usr/bin/env python3
"""Script de re-encrypt de secrets entre backends.

Re-cifra todas as colunas sensíveis dos modelos SQLAlchemy de um backend
de origem para um backend de destino. Seguro para rodar em produção:

- Idempotente: detecta o prefixo do token e pula registros já migrados.
- Transacional por linha: falha em um registro não afeta os demais.
- Dry-run: com ``--dry-run`` apenas conta sem alterar nenhum dado.
- Relatório no stdout ao final.

Uso
~~~
::

    # Migrar de LocalFernet (enc::) para KmsWrappedFernet (kmsenc::)
    APP_MASTER_KEY=... SECRETS_BACKEND=local_fernet \\
        python scripts/reencrypt_secrets.py \\
        --from-backend local_fernet \\
        --to-backend kms_wrapped_fernet \\
        --database-url sqlite:///data/sophos.db

    # Visualizar sem alterar
    python scripts/reencrypt_secrets.py \\
        --from-backend local_fernet \\
        --to-backend kms_wrapped_fernet \\
        --dry-run

Colunas cifradas por modelo
~~~~~~~~~~~~~~~~~~~~~~~~~~~
- ``Integration``: client_secret, manager_api_username, manager_api_password,
  indexer_username, indexer_password, api_username, api_password,
  access_token, refresh_token (colunas LEGADAS — sophos/wazuh migraram p/ o
  store ``integration_credentials`` na F1b; ficam NULL e são puladas).
- ``IntegrationCredential``: secret_ref (store vendor-neutro de fonte
- ``Destination``: secret_ref (store vendor-neutro de destino
- ``EmailConfig``: smtp_password.
- ``ThreatIntelApiKey``: api_key.

F1.5 (P0): SEM ``IntegrationCredential``/``Destination`` aqui, uma
rotação de key Transit re-cifrava só as colunas legadas e deixava os stores
``secret_ref`` indecifráveis ao retirar a master antiga. ``key_version`` é
atualizado para a key do backend destino na rotação.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Garante que a raiz do repositório (pai de backend/) está no sys.path.
# Isso permite importar tanto via ``backend.app.*`` (invocação da raiz do
# repo) quanto manter compatibilidade com o padrão de imports do projeto.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("reencrypt_secrets")


# ── Estrutura de resultado ────────────────────────────────────────────


@dataclass
class ReencryptStats:
    """Contadores por modelo/coluna de uma execução."""

    migrated: int = 0
    skipped_already_migrated: int = 0
    skipped_null: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ReencryptReport:
    """Relatório completo da execução."""

    dry_run: bool = False
    stats_by_model: dict[str, ReencryptStats] = field(default_factory=dict)

    def total_migrated(self) -> int:
        return sum(s.migrated for s in self.stats_by_model.values())

    def total_skipped(self) -> int:
        return sum(
            s.skipped_already_migrated + s.skipped_null
            for s in self.stats_by_model.values()
        )

    def total_failed(self) -> int:
        return sum(s.failed for s in self.stats_by_model.values())

    def print_summary(self) -> None:
        prefix = "[DRY-RUN] " if self.dry_run else ""
        print(f"\n{prefix}=== Relatório de re-encrypt ===")
        for model_name, stats in sorted(self.stats_by_model.items()):
            print(f"\n  {model_name}:")
            print(f"    migrados  : {stats.migrated}")
            print(f"    já ok     : {stats.skipped_already_migrated}")
            print(f"    nulos     : {stats.skipped_null}")
            print(f"    falhas    : {stats.failed}")
            for err in stats.errors[:5]:
                print(f"      - {err}")
            if len(stats.errors) > 5:
                print(f"      ... e mais {len(stats.errors) - 5} erros")

        print(
            f"\n{prefix}Total: {self.total_migrated()} migrados, "
            f"{self.total_skipped()} pulados, "
            f"{self.total_failed()} falhas."
        )


# ── Mapeamento de colunas cifradas por modelo ─────────────────────────

# Cada entrada: (nome_do_modelo, lista_de_colunas_cifradas)
_ENCRYPTED_COLUMNS: dict[str, list[str]] = {
    "Integration": [
        "client_secret",
        "manager_api_username",
        "manager_api_password",
        "indexer_username",
        "indexer_password",
        "api_username",
        "api_password",
        "access_token",
        "refresh_token",
    ],
    "EmailConfig": ["smtp_password"],
    "ThreatIntelApiKey": ["api_key"],
    # 5: stores vendor-neutros (1 ciphertext por linha em ``secret_ref``).
    # Imprescindíveis na rotação de key — sem eles o store fica órfão (P0).
    "IntegrationCredential": ["secret_ref"],
    "Destination": ["secret_ref"],
    # entra_client_secret é cifrado pelo MESMO backend
    # pluggable (identity_config.py → core.crypto.encrypt). Sem ele aqui, uma
    # rotação Transit deixava o segredo de login Entra ID indecifrável ao retirar
    # a master antiga — o mesmo P0 que o store fechou, numa coluna vizinha.
    "IdentityConfig": ["entra_client_secret"],
}


# ── Detecção de prefixo ───────────────────────────────────────────────

_FROM_BACKEND_PREFIXES: dict[str, str] = {
    "local_fernet": "enc::",
    "kms_wrapped_fernet": "kmsenc::",
}

_TO_BACKEND_PREFIXES: dict[str, str] = {
    "local_fernet": "enc::",
    "kms_wrapped_fernet": "kmsenc::",
}


def _already_migrated(value: str, to_prefix: str) -> bool:
    """Verdade se o valor já tem o prefixo do backend destino."""
    return value.startswith(to_prefix)


def _is_source_format(value: str, from_prefix: str) -> bool:
    """Verdade se o valor está no formato do backend fonte."""
    return value.startswith(from_prefix)


def _is_plaintext_legacy(value: str) -> bool:
    """Verdade se o valor não tem prefixo reconhecido — plaintext pré-cifragem."""
    return not any(
        value.startswith(p) for p in _FROM_BACKEND_PREFIXES.values()
    )


def _backend_key_version(backend) -> Optional[str]:
    """Identidade da key do backend destino, p/ carimbar ``key_version`` na rotação.

    Best-effort: ``KmsWrappedFernet`` expõe via ``self.kms.key_id()``; backends
    KMS/Vault diretos via ``key_id()``. ``LocalFernet`` não versiona key ⇒ None
    (deixa ``key_version`` como está)."""
    for getter in (
        getattr(backend, "key_id", None),
        getattr(getattr(backend, "kms", None), "key_id", None),
    ):
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001
                return None
    return None


# ── Lógica principal ──────────────────────────────────────────────────


def build_backend(backend_name: str, *, kms_key_path: Optional[str] = None):  # type: ignore[return]
    """Instancia o backend de secrets pelo nome.

    Args:
        backend_name: "local_fernet" ou "kms_wrapped_fernet".
        kms_key_path: Caminho do arquivo de master key do stub KMS
            (apenas para "kms_wrapped_fernet").

    Returns:
        Instância de :class:`~backend.app.core.secrets.backend.SecretsBackend`.
    """
    from backend.app.core.secrets.local_fernet import LocalFernetBackend

    if backend_name == "local_fernet":
        return LocalFernetBackend()

    if backend_name == "kms_wrapped_fernet":
        from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
        from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

        # --kms-key-path é obrigatório para kms_wrapped_fernet (HIGH 1 — F5-S5).
        # Sem default /tmp/... (world-writable, inseguro).
        if not kms_key_path:
            raise ValueError(
                "kms_key_path é obrigatório para backend 'kms_wrapped_fernet'. "
                "Forneça --kms-key-path com um caminho seguro (ex: /var/lib/centralops/kms-master.key)."
            )
        kms = LocalKmsStubBackend(master_key_path=kms_key_path)
        # legacy_fallback permite decifrar tokens "enc::" legados durante migração.
        return KmsWrappedFernetBackend(
            kms=kms,
            legacy_fallback=LocalFernetBackend(),
        )

    raise ValueError(
        f"Backend desconhecido: '{backend_name}'. "
        "Valores válidos: 'local_fernet', 'kms_wrapped_fernet'."
    )


def run_reencrypt(
    *,
    from_backend_name: str,
    to_backend_name: str,
    database_url: str,
    dry_run: bool = False,
    kms_key_path: Optional[str] = None,
) -> ReencryptReport:
    """Executa o re-encrypt de todos os modelos mapeados.

    Função pura (sem CLI) — testável diretamente.

    Args:
        from_backend_name: Nome do backend de origem ("local_fernet" etc.).
        to_backend_name: Nome do backend de destino.
        database_url: URL de conexão SQLAlchemy (ex: sqlite:///data/sophos.db).
        dry_run: Se True, apenas conta sem alterar o banco.
        kms_key_path: Caminho do arquivo de master key do stub KMS.

    Returns:
        :class:`ReencryptReport` com os contadores por modelo.
    """
    from sqlalchemy import create_engine, inspect as sa_inspect
    from sqlalchemy.orm import sessionmaker

    # Importa os modelos para garantir que os metadados SQLAlchemy estejam registrados.
    import backend.app.db.models as _models_module  # noqa: F401

    from_backend = build_backend(from_backend_name, kms_key_path=kms_key_path)
    to_backend = build_backend(to_backend_name, kms_key_path=kms_key_path)

    to_prefix = _TO_BACKEND_PREFIXES.get(to_backend_name, "")

    # Configura engine com os mesmos kwargs do database.py.
    engine_kwargs: dict = {}
    if database_url.startswith("sqlite:///"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **engine_kwargs)
    Session = sessionmaker(bind=engine)

    report = ReencryptReport(dry_run=dry_run)

    # Mapa de classe de modelo pelo nome.
    model_map: dict[str, type] = {
        "Integration": _models_module.Integration,
        "EmailConfig": _models_module.EmailConfig,
        "ThreatIntelApiKey": _models_module.ThreatIntelApiKey,
        "IntegrationCredential": _models_module.IntegrationCredential,
        "Destination": _models_module.Destination,
        "IdentityConfig": _models_module.IdentityConfig,
    }

    # trava anti-omissão — todo modelo listado em
    # _ENCRYPTED_COLUMNS precisa estar resolvível aqui, senão a rotação ignora
    # silenciosamente a tabela e órfã o segredo. Falhar cedo é melhor que furo P0.
    _missing = set(_ENCRYPTED_COLUMNS) - set(model_map)
    if _missing:
        raise RuntimeError(
            f"reencrypt: modelos em _ENCRYPTED_COLUMNS sem entrada em model_map: {sorted(_missing)}"
        )

    # Verifica quais tabelas existem para não quebrar em esquemas parciais.
    existing_tables = set(sa_inspect(engine).get_table_names())

    for model_name, columns in _ENCRYPTED_COLUMNS.items():
        model_cls = model_map[model_name]
        table_name = model_cls.__tablename__  # type: ignore[attr-defined]

        if table_name not in existing_tables:
            logger.warning("Tabela '%s' não existe no banco — ignorando.", table_name)
            continue

        stats = ReencryptStats()
        report.stats_by_model[model_name] = stats

        with Session() as session:
            rows = session.query(model_cls).all()
            logger.info(
                "Processando %d registros de %s (%d colunas)",
                len(rows),
                model_name,
                len(columns),
            )

            for row in rows:
                row_id = getattr(row, "id", "?")
                for col in columns:
                    value: Optional[str] = getattr(row, col, None)

                    # Pula nulos.
                    if value is None or value == "":
                        stats.skipped_null += 1
                        continue

                    # Pula já migrados (já tem o prefixo do destino).
                    if _already_migrated(value, to_prefix):
                        stats.skipped_already_migrated += 1
                        continue

                    # Tenta decifrar com o backend de origem.
                    try:
                        plaintext = from_backend.decrypt(value)
                    except Exception as exc:
                        msg = f"{model_name} id={row_id} col={col}: decrypt falhou: {exc}"
                        logger.error(msg)
                        stats.failed += 1
                        stats.errors.append(msg)
                        continue

                    # Recifra com o backend de destino.
                    if not dry_run:
                        try:
                            new_value = to_backend.encrypt(plaintext)
                            setattr(row, col, new_value)
                            # 5: o store ``secret_ref`` carrega a
                            # ``key_version`` do KMS — carimba a key do destino na
                            # rotação (best-effort; backend sem key_id ⇒ no-op).
                            if col == "secret_ref" and hasattr(row, "key_version"):
                                kv = _backend_key_version(to_backend)
                                if kv is not None:
                                    row.key_version = kv
                        except Exception as exc:
                            msg = f"{model_name} id={row_id} col={col}: encrypt falhou: {exc}"
                            logger.error(msg)
                            stats.failed += 1
                            stats.errors.append(msg)
                            continue

                    stats.migrated += 1
                    logger.debug(
                        "%s id=%s col=%s: %s",
                        model_name,
                        row_id,
                        col,
                        "DRY-RUN" if dry_run else "migrado",
                    )

            if not dry_run:
                try:
                    session.commit()
                    logger.info("%s: commit realizado.", model_name)
                except Exception as exc:
                    session.rollback()
                    logger.error("%s: commit falhou: %s", model_name, exc)
                    # Marca todas as migrações do modelo como falha.
                    stats.failed += stats.migrated
                    stats.migrated = 0
                    stats.errors.append(f"commit falhou: {exc}")

    return report


# ── CLI ───────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-encrypt secrets entre backends (Fase 4 Sprint 1).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--from-backend",
        default="local_fernet",
        choices=list(_FROM_BACKEND_PREFIXES),
        help="Backend de origem (default: local_fernet).",
    )
    parser.add_argument(
        "--to-backend",
        default="kms_wrapped_fernet",
        choices=list(_TO_BACKEND_PREFIXES),
        help="Backend de destino (default: kms_wrapped_fernet).",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", "sqlite:///data/sophos.db"),
        help="URL de conexão SQLAlchemy. Lê DATABASE_URL do env se não passado.",
    )
    parser.add_argument(
        "--kms-key-path",
        default=os.environ.get(
            "KMS_LOCAL_STUB_MASTER_KEY_PATH", "/tmp/centralops-kms-stub.key"
        ),
        help="Caminho do arquivo de master key do stub KMS (dev/test).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Conta sem alterar nenhum dado.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """Entrypoint CLI. Retorna 0 em sucesso, 1 se houve falhas."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.from_backend == args.to_backend:
        logger.error(
            "from-backend e to-backend são iguais ('%s'). Nada a fazer.",
            args.from_backend,
        )
        return 1

    logger.info(
        "Iniciando re-encrypt: %s → %s | database=%s | dry_run=%s",
        args.from_backend,
        args.to_backend,
        args.database_url,
        args.dry_run,
    )

    report = run_reencrypt(
        from_backend_name=args.from_backend,
        to_backend_name=args.to_backend,
        database_url=args.database_url,
        dry_run=args.dry_run,
        kms_key_path=args.kms_key_path,
    )

    report.print_summary()

    return 1 if report.total_failed() > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
