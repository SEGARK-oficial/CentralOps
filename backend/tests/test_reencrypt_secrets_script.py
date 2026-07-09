"""Testes para scripts/reencrypt_secrets.py.

Testa a função ``run_reencrypt`` diretamente (não o entrypoint CLI)
para evitar subprocessos. Usa SQLite em arquivo temporário e instâncias
isoladas de backend para não depender de estado global.

O script é importado via sys.path adicional (scripts/ na raiz do repo).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# scripts/ fica na raiz do repo (CentralOps/scripts), dois níveis acima de backend/tests/.
_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from backend.app.core.secrets.local_fernet import LocalFernetBackend  # noqa: E402
from backend.app.db.models import (  # noqa: E402
    Destination,
    EmailConfig,
    Integration,
    IntegrationCredential,
    Organization,
    ThreatIntelApiKey,
)
from backend.app.db.database import Base  # noqa: E402

from reencrypt_secrets import build_backend, run_reencrypt  # noqa: E402


# ── Helpers de seed ───────────────────────────────────────────────────


def _make_engine(db_file: Path):
    return create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )


def _seed_db(db_file: Path, legacy: LocalFernetBackend) -> None:
    """Cria banco e semeia um registro de cada modelo cifrado."""
    engine = _make_engine(db_file)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        org = Organization(name="Test Org", slug="test-org")
        session.add(org)
        session.flush()

        session.add(
            Integration(
                organization_id=org.id,
                name="Test Integration",
                platform="wazuh",
                client_secret=legacy.encrypt("client-secret-value"),
                manager_api_username=legacy.encrypt("admin"),
                manager_api_password=legacy.encrypt("super-secret-pass"),
            )
        )
        session.add(EmailConfig(smtp_password=legacy.encrypt("smtp-pass-value")))
        session.add(
            ThreatIntelApiKey(
                provider="abuseipdb",
                api_key=legacy.encrypt("abuseipdb-key-12345"),
            )
        )
        session.commit()
    engine.dispose()


# ── Testes ─────────────────────────────────────────────────────────────


class TestReencryptDryRun:
    def test_dry_run_does_not_modify_db(self, tmp_path: Path) -> None:
        """Dry-run não deve alterar nenhum valor no banco."""
        db_file = tmp_path / "dry-run.db"
        legacy = LocalFernetBackend()
        _seed_db(db_file, legacy)

        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=True,
            kms_key_path=str(tmp_path / "dry-run.key"),
        )

        assert report.dry_run is True
        # 3 colunas cifradas na Integration, 1 no EmailConfig, 1 no ThreatIntelApiKey.
        assert report.total_migrated() == 5
        assert report.total_failed() == 0

        # Valores no banco não foram alterados — ainda "enc::".
        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            integration = session.query(Integration).first()
            assert integration is not None
            assert integration.client_secret.startswith("enc::")
        engine.dispose()

    def test_dry_run_counts_null_columns(self, tmp_path: Path) -> None:
        """Dry-run conta colunas nulas sem reportar falha."""
        db_file = tmp_path / "dry-run-null.db"
        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)

        with Session() as session:
            org = Organization(name="Org Null Test", slug="org-null-test")
            session.add(org)
            session.flush()
            session.add(Integration(organization_id=org.id, name="Sem Creds", platform="sophos"))
            session.commit()
        engine.dispose()

        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=True,
            kms_key_path=str(tmp_path / "null.key"),
        )

        assert report.total_failed() == 0
        assert report.stats_by_model["Integration"].skipped_null == 9


class TestReencryptMigration:
    def test_migrates_integration_secrets(self, tmp_path: Path) -> None:
        """Re-encrypt deve converter tokens 'enc::' para 'kmsenc::'."""
        db_file = tmp_path / "migrate-integration.db"
        legacy = LocalFernetBackend()
        _seed_db(db_file, legacy)

        kms_key = str(tmp_path / "migrate.key")
        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )

        assert report.total_failed() == 0
        # 3 colunas preenchidas: client_secret, manager_api_username, manager_api_password.
        assert report.stats_by_model["Integration"].migrated == 3

        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            integration = session.query(Integration).first()
            assert integration is not None
            assert integration.client_secret.startswith("kmsenc::")
            assert integration.manager_api_username.startswith("kmsenc::")
            assert integration.manager_api_password.startswith("kmsenc::")

        # Valor decifrado deve ser idêntico ao original.
        from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
        from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

        kms = LocalKmsStubBackend(master_key_path=kms_key)
        kms_backend = KmsWrappedFernetBackend(kms=kms)
        with Session() as session:
            integration = session.query(Integration).first()
            assert integration is not None
            assert kms_backend.decrypt(integration.client_secret) == "client-secret-value"

        engine.dispose()

    def test_migrates_secret_ref_stores_and_stamps_key_version(self, tmp_path: Path) -> None:
        """Re-encrypt cobre os stores ``secret_ref`` vendor-neutros
        (integration_credentials + destinations), não só as colunas legadas — senão
        a rotação de key Transit deixaria o store indecifrável."""
        db_file = tmp_path / "migrate-stores.db"
        legacy = LocalFernetBackend()
        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            org = Organization(name="O", slug="o")
            session.add(org)
            session.flush()
            integ = Integration(organization_id=org.id, name="N", platform="ninjaone")
            session.add(integ)
            session.flush()
            session.add(
                IntegrationCredential(
                    integration_id=integ.id,
                    logical_name="client_secret",
                    secret_ref=legacy.encrypt("ninja-store-secret"),
                    key_version="local",
                )
            )
            session.add(
                Destination(
                    name="lake", kind="s3",
                    secret_ref=legacy.encrypt("dest-store-secret"),
                )
            )
            session.commit()
        engine.dispose()

        kms_key = str(tmp_path / "stores.key")
        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )

        assert report.total_failed() == 0
        assert report.stats_by_model["IntegrationCredential"].migrated == 1
        assert report.stats_by_model["Destination"].migrated == 1

        from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
        from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

        kms_backend = KmsWrappedFernetBackend(kms=LocalKmsStubBackend(master_key_path=kms_key))
        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            cred = session.query(IntegrationCredential).first()
            assert cred is not None
            assert cred.secret_ref.startswith("kmsenc::")
            assert kms_backend.decrypt(cred.secret_ref) == "ninja-store-secret"
            # key_version carimbado com a key do destino (não mais "local")
            assert cred.key_version and cred.key_version != "local"

            dest = session.query(Destination).first()
            assert dest is not None
            assert dest.secret_ref.startswith("kmsenc::")
            assert kms_backend.decrypt(dest.secret_ref) == "dest-store-secret"
        engine.dispose()

    def test_migrates_identity_config_entra_secret(self, tmp_path: Path) -> None:
        """Re-encrypt cobre IdentityConfig.entra_client_secret.

        É cifrado pelo MESMO backend pluggable; sem cobertura a rotação Transit
        deixava o segredo de login Entra ID indecifrável (mesmo do store)."""
        from backend.app.db.models import IdentityConfig

        db_file = tmp_path / "migrate-identity.db"
        legacy = LocalFernetBackend()
        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(IdentityConfig(
                entra_enabled=True,
                entra_client_secret=legacy.encrypt("entra-oidc-secret"),
            ))
            session.commit()
        engine.dispose()

        kms_key = str(tmp_path / "identity.key")
        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )

        assert report.total_failed() == 0
        assert report.stats_by_model["IdentityConfig"].migrated == 1

        from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend
        from backend.app.core.secrets.local_kms_stub import LocalKmsStubBackend

        kms_backend = KmsWrappedFernetBackend(kms=LocalKmsStubBackend(master_key_path=kms_key))
        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            cfg = session.query(IdentityConfig).first()
            assert cfg is not None
            assert cfg.entra_client_secret.startswith("kmsenc::")
            assert kms_backend.decrypt(cfg.entra_client_secret) == "entra-oidc-secret"
        engine.dispose()

    def test_every_encrypted_model_is_resolvable(self) -> None:
        """Trava anti-omissão — todo modelo de
        _ENCRYPTED_COLUMNS tem entrada no model_map (senão a rotação ignora a
        tabela silenciosamente e órfã o segredo)."""
        import reencrypt_secrets as rs
        from backend.app.db import models as _m

        for model_name in rs._ENCRYPTED_COLUMNS:
            assert hasattr(_m, model_name), f"modelo cifrado sem classe: {model_name}"

    def test_migrates_email_config_smtp_password(self, tmp_path: Path) -> None:
        """Re-encrypt deve migrar smtp_password do EmailConfig."""
        db_file = tmp_path / "migrate-email.db"
        legacy = LocalFernetBackend()

        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(EmailConfig(smtp_password=legacy.encrypt("smtp-pass-value")))
            session.commit()
        engine.dispose()

        kms_key = str(tmp_path / "email.key")
        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )

        assert report.stats_by_model["EmailConfig"].migrated == 1

        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            config = session.query(EmailConfig).first()
            assert config is not None
            assert config.smtp_password.startswith("kmsenc::")
        engine.dispose()

    def test_migrates_threat_intel_api_key(self, tmp_path: Path) -> None:
        """Re-encrypt deve migrar api_key do ThreatIntelApiKey."""
        db_file = tmp_path / "migrate-ti.db"
        legacy = LocalFernetBackend()

        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(
                ThreatIntelApiKey(
                    provider="abuseipdb",
                    api_key=legacy.encrypt("abuseipdb-key-12345"),
                )
            )
            session.commit()
        engine.dispose()

        kms_key = str(tmp_path / "ti.key")
        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )

        assert report.stats_by_model["ThreatIntelApiKey"].migrated == 1

        engine = _make_engine(db_file)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            key = session.query(ThreatIntelApiKey).first()
            assert key is not None
            assert key.api_key.startswith("kmsenc::")
        engine.dispose()


class TestReencryptIdempotency:
    def test_idempotent_skips_already_migrated(self, tmp_path: Path) -> None:
        """Segunda execução deve pular registros já no formato kmsenc::."""
        db_file = tmp_path / "idempotent.db"
        legacy = LocalFernetBackend()
        kms_key = str(tmp_path / "idem.key")

        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        with Session() as session:
            session.add(EmailConfig(smtp_password=legacy.encrypt("smtp-pass")))
            session.commit()
        engine.dispose()

        # Primeira migração.
        report1 = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )
        assert report1.stats_by_model["EmailConfig"].migrated == 1
        assert report1.stats_by_model["EmailConfig"].skipped_already_migrated == 0

        # Segunda migração — deve pular tudo.
        report2 = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=kms_key,
        )
        assert report2.stats_by_model["EmailConfig"].migrated == 0
        assert report2.stats_by_model["EmailConfig"].skipped_already_migrated == 1


class TestReencryptNullHandling:
    def test_handles_null_columns(self, tmp_path: Path) -> None:
        """Colunas nulas devem ser contadas em skipped_null sem erro."""
        db_file = tmp_path / "null-cols.db"
        engine = _make_engine(db_file)
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)

        with Session() as session:
            org = Organization(name="Org Sem Creds", slug="org-sem-creds")
            session.add(org)
            session.flush()
            session.add(Integration(organization_id=org.id, name="Sem Credenciais", platform="sophos"))
            session.commit()
        engine.dispose()

        report = run_reencrypt(
            from_backend_name="local_fernet",
            to_backend_name="kms_wrapped_fernet",
            database_url=f"sqlite:///{db_file}",
            dry_run=False,
            kms_key_path=str(tmp_path / "null.key"),
        )

        assert report.total_failed() == 0
        # Todas as 9 colunas da Integration devem ser contadas como nulas.
        assert report.stats_by_model["Integration"].skipped_null == 9
        assert report.stats_by_model["Integration"].migrated == 0


class TestBuildBackend:
    def test_build_local_fernet(self) -> None:
        backend = build_backend("local_fernet")
        assert isinstance(backend, LocalFernetBackend)

    def test_build_kms_wrapped(self, tmp_path: Path) -> None:
        from backend.app.core.secrets.kms_wrapped_fernet import KmsWrappedFernetBackend

        backend = build_backend(
            "kms_wrapped_fernet",
            kms_key_path=str(tmp_path / "build-test.key"),
        )
        assert isinstance(backend, KmsWrappedFernetBackend)

    def test_build_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Backend desconhecido"):
            build_backend("inexistente")
