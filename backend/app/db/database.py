from sqlalchemy import create_engine, event as sa_event, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, declarative_base
import logging
import os
import time
from datetime import datetime
from uuid import uuid4

from ..core.config import settings
from ..core.crypto import encrypt

logger = logging.getLogger(__name__)


DATABASE_URL = settings.DATABASE_URL


def _get_engine_kwargs() -> dict:
    # SQLite ``:memory:`` → StaticPool: UMA conexão compartilhada entre threads.
    # Sem isto, o pool default abre uma conexão NOVA por thread, e como cada
    # conexão a ``:memory:`` é um banco SEPARADO e VAZIO, qualquer trabalho em
    # thread (ex.: ``persist_rejected_to_dlq`` via ``asyncio.to_thread``) bate em
    # "no such table". Os fixtures de teste já usam StaticPool por isso; o engine
    # GLOBAL precisa também (a suíte da imagem compilada roda sob :memory: e os
    # monkeypatches do engine não alcançam o código .so). File-based e Postgres
    # NÃO usam isto — só o caminho :memory:.
    if ":memory:" in DATABASE_URL:
        from sqlalchemy.pool import StaticPool

        return {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    if DATABASE_URL.startswith("sqlite:///"):
        raw_path = DATABASE_URL.replace("sqlite:///", "", 1)
        absolute_path = os.path.abspath(raw_path)
        directory = os.path.dirname(absolute_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        return {
            "connect_args": {
                "check_same_thread": False,
                "timeout": 30,  # espera 30s antes de SQLITE_BUSY
            },
        }
    # pool_pre_ping descarta conexões mortas após idle; pool_recycle: 1h.
    return {
        "pool_size": 20,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 3600,
        "pool_pre_ping": True,
    }


engine = create_engine(DATABASE_URL, **_get_engine_kwargs())

# ── WAL mode para SQLite: elimina "database is locked" em writes concorrentes ──
# WAL (Write-Ahead Logging) permite leitores simultâneos sem bloquear writers.
# Sem isso, múltiplos workers Celery (drift, quarantine, cursor) batem em
# SQLITE_BUSY quando escrevem ao mesmo tempo.
if DATABASE_URL.startswith("sqlite:///"):
    @sa_event.listens_for(engine, "connect")
    def _enable_sqlite_wal(dbapi_conn: object, _conn_record: object) -> None:
        """Habilita WAL mode + foreign_keys + synchronous=NORMAL.

        WAL permite leitores concorrentes sem bloquear writers. Sem isso,
        múltiplos workers Celery escrevendo simultaneamente (drift,
        quarantine, cursor) batem em "database is locked".
        """
        cursor = dbapi_conn.cursor()  # type: ignore[union-attr]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _wait_for_db(max_wait_s: float | None = None, interval_s: float = 2.0) -> None:
    """Aguarda o banco ficar acessível antes de inicializar o schema.

    Em orquestradores (Docker Compose / Kubernetes) a rede do container costuma
    ser anexada LOGO APÓS o start; como o app conecta no banco no import (``initialize_database``)
    sem retry, ele falha com ``OperationalError`` transitório no boot — tipicamente
    ``could not translate host name ... Temporary failure in name resolution`` ou
    ``connection refused`` — e o container entra em crash-loop até a rede/DNS
    estabilizar. Aqui fazemos retry com backoff fixo até ``max_wait_s`` para
    absorver essa janela. SQLite (arquivo local) não precisa esperar.

    ``max_wait_s`` default 60s, configurável via ``APP_DB_WAIT_MAX_S``.
    """
    if DATABASE_URL.startswith("sqlite"):
        return
    if max_wait_s is None:
        try:
            max_wait_s = float(os.environ.get("APP_DB_WAIT_MAX_S", "60"))
        except ValueError:
            max_wait_s = 60.0
    deadline = time.monotonic() + max_wait_s
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("database: conexão OK na tentativa %d", attempt)
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                logger.error(
                    "database: inacessível após %.0fs (%d tentativas) — desistindo",
                    max_wait_s,
                    attempt,
                )
                raise
            logger.warning(
                "database: indisponível no boot (tentativa %d): %s — retry em %.1fs",
                attempt,
                exc.orig if hasattr(exc, "orig") else exc,
                interval_s,
            )
            time.sleep(interval_s)


# Chave fixa do advisory lock que serializa o init entre
# réplicas/processos concorrentes. Valor arbitrário porém estável (mudar quebra
# a exclusão mútua durante um rolling update com a versão anterior).
_MIGRATION_ADVISORY_LOCK_KEY = 0x0C0DE004


def _run_schema_init() -> None:
    """O DDL idempotente do schema (drop legado + create_all + migrações leves)."""
    # CRÍTICO: registra TODOS os models em ``Base.metadata`` antes do create_all.
    # A etapa standalone ``python -m app.db.migrate`` NÃO importa
    # ``app.main`` — sem este import, ``Base.metadata`` ficaria VAZIO e
    # ``create_all`` criaria ZERO tabela; a migração leve do ``destination_dlq``
    # então referencia ``organizations`` (inexistente) e o boot quebra no
    # Postgres (FK estrita) — o SQLite tolerava (FK lazy), por isso só explodia
    # no deploy real. Import RELATIVO (``from . import``), não ``app.db.models``:
    # sob o sweep compilado dual-root (``app`` vs ``backend.app``) o caminho
    # absoluto registraria num ``Base`` diferente do que o ``create_all`` usa.
    from . import models  # noqa: F401 — popula Base.metadata (side-effect)

    _drop_legacy_client_tables()
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    _backfill_org_hierarchy()


def _backfill_org_hierarchy() -> None:
    """Materializa a árvore de tenants (idempotente, guardado).

    Roda após o schema estar pronto (colunas + ``org_closure`` existem). Deriva o
    pai do ``partner_integration_id`` e popula ``root_id``/``depth``/closure. É
    ZERO-RUNTIME para o read path (nada lê a árvore ainda). Best-effort: uma falha
    aqui não derruba o boot — a árvore pode ser re-materializada no próximo boot ou
    via o serviço de hierarquia na próxima criação de org.
    """
    try:
        from . import hierarchy

        with SessionLocal() as session:
            if hierarchy.needs_backfill(session):
                n = hierarchy.backfill_hierarchy(session)
                session.commit()
                logger.info("hierarquia materializada para %s orgs", n)
    except Exception:  # pragma: no cover — boot resiliente
        logger.warning("backfill de hierarquia falhou (não-fatal)", exc_info=True)


# Alembic. A revisão BASELINE âncora a adoção: o schema legado é
# criado por _run_schema_init (acima) e o ponteiro de versão é carimbado aqui.
# Mudanças FUTURAS de schema viram revisions Alembic (autogenerate dos models).
_BASELINE_REVISION = "0001_baseline"


def _alembic_config():
    """Config programático do Alembic — script_location ABSOLUTO (resolve de
    qualquer cwd/root) + url do DATABASE_URL corrente (inclusive sob monkeypatch
    nos testes)."""
    from alembic.config import Config

    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    return cfg


def _sync_alembic_version(*, had_version: bool, had_app: bool) -> None:
    """Concilia o ponteiro de versão do Alembic com o schema recém-garantido por
    ``_run_schema_init``:

    - **versionado** (tem ``alembic_version``): ``upgrade head`` aplica revisions
      pendentes (o caminho normal pós-adoção).
    - **legado pré-Alembic** (tem tabelas do app, sem ``alembic_version``):
      ``stamp baseline`` (o schema == baseline) + ``upgrade head`` (na adoção
      head==baseline ⇒ no-op; revisions futuras aplicam nos próximos deploys).
    - **fresh** (DB vazio): ``_run_schema_init`` já criou o schema ATUAL (que é
      == head, pois os models são a fonte de verdade) ⇒ ``stamp head`` (não
      ``upgrade``, p/ não re-aplicar DDL já presente via create_all).
    """
    from alembic import command

    cfg = _alembic_config()
    if had_version:
        command.upgrade(cfg, "head")
    elif had_app:
        command.stamp(cfg, _BASELINE_REVISION)
        command.upgrade(cfg, "head")
    else:
        command.stamp(cfg, "head")


def _do_init() -> None:
    """Unidade de trabalho do init: garante o schema (idempotente) e concilia o
    ponteiro Alembic. Rodada sob o advisory lock no Postgres (ver baixo)."""
    # Estado ANTES do schema init — distingue fresh de legado (depois do
    # _run_schema_init as tabelas existem e a distinção se perde).
    with engine.connect() as _probe:
        _insp = inspect(_probe)
        had_version = _insp.has_table("alembic_version")
        had_app = _insp.has_table("app_users")

    _run_schema_init()
    _sync_alembic_version(had_version=had_version, had_app=had_app)


def initialize_database() -> None:
    """Inicializa o schema de forma concorrência-segura.

    Garante o schema (``_run_schema_init``, idempotente) e concilia o ponteiro de
    versão do Alembic (``_sync_alembic_version``). No Postgres, TODO o init roda
    sob um **advisory lock de sessão**: com ``api``/workers em ``replicas>1``,
    dois processos disparariam o MESMO DDL/stamp ao mesmo tempo e corriam — o
    lock serializa (só um detentor executa; os demais bloqueiam e veem o schema
    já aplicado). É o pré-requisito da escala horizontal.

    SQLite (instância única) dispensa o lock — e nem suporta ``pg_advisory_lock``.

    Deixou de rodar no IMPORT (``main.py``); agora é invocado como
    ETAPA explícita (``python -m app.db.migrate``) pelos entrypoints antes de
    subir a app — share-nothing, sem ordering entre serviços.
    """
    _wait_for_db()

    if not DATABASE_URL.startswith("postgresql"):
        _do_init()
        return

    # AUTOCOMMIT: garante que o pg_advisory_lock tome efeito de imediato em
    # nível de SESSÃO (não preso a uma transação) e siga retido enquanto esta
    # conexão viver. Liberado no finally (mesma conexão).
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_conn:
        lock_conn.execute(
            text("SELECT pg_advisory_lock(:k)"), {"k": _MIGRATION_ADVISORY_LOCK_KEY}
        )
        try:
            _do_init()
        finally:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"),
                {"k": _MIGRATION_ADVISORY_LOCK_KEY},
            )


# _mark_db_ready() removido. O sinal `.db_ready`
# em arquivo no volume `app-data` era acoplamento por filesystem — não funciona
# cross-node (k8s) e travava réplicas. Agora cada container GARANTE o schema ele
# mesmo via initialize_database() (idempotente + sob advisory lock), sem volume
# compartilhado nem ordering entre serviços (share-nothing). Ver start-collector.sh.


def _drop_legacy_client_tables() -> None:
    """Drop tables whose schema still references the removed ``clients`` table.

    SQLite cannot drop a column with a foreign key constraint, so we drop the
    whole table when we detect the legacy ``client_id`` FK. ``create_all`` will
    recreate it with the new schema immediately after.
    """
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    with engine.begin() as conn:
        for legacy_table in ("history", "search_results"):
            if legacy_table not in table_names:
                continue
            columns = {col["name"] for col in inspector.get_columns(legacy_table)}
            if "client_id" in columns:
                conn.execute(text(f"DROP TABLE {legacy_table}"))

        if "clients" in table_names:
            conn.execute(text("DROP TABLE clients"))


# ── ON DELETE rules expected (Postgres). Aligned with models.py. ────
# SQLAlchemy só emite a rule na criação inicial — declarar ``ondelete=``
# no model não recria a constraint num banco já existente. Esta tabela
# é a fonte da verdade pra reescrever as constraints em Postgres via
# ALTER idempotente quando o schema foi criado em uma versão anterior.
#
# Schema: (table_name, column_name, ref_table, ref_column, expected_rule)
#   expected_rule ∈ {"CASCADE", "SET NULL", "RESTRICT"}.
_EXPECTED_FK_ONDELETE_RULES: tuple[tuple[str, str, str, str, str], ...] = (
    # Operacionais — CASCADE quando parent some, child é lixo.
    ("integrations", "organization_id", "organizations", "id", "CASCADE"),
    ("integration_health_checks", "integration_id", "integrations", "id", "CASCADE"),
    ("user_sessions", "user_id", "app_users", "id", "CASCADE"),
    ("scheduled_queries", "query_id", "predefined_queries", "id", "CASCADE"),
    # Destinatário escopado por tenant; org some → e-mail é lixo.
    ("notification_emails", "organization_id", "organizations", "id", "CASCADE"),
    # Audit/histórico — SET NULL preserva a row pra forense.
    ("app_users", "organization_id", "organizations", "id", "SET NULL"),
    ("history", "integration_id", "integrations", "id", "SET NULL"),
    ("history", "user_id", "app_users", "id", "SET NULL"),
    ("audit_logs", "user_id", "app_users", "id", "SET NULL"),
    ("search_results", "integration_id", "integrations", "id", "SET NULL"),
    ("search_results", "user_id", "app_users", "id", "SET NULL"),
    ("search_results", "schedule_id", "scheduled_queries", "id", "SET NULL"),
    ("threat_intel_tokens", "created_by", "app_users", "id", "SET NULL"),
    ("threat_intel_queries", "token_id", "threat_intel_tokens", "id", "SET NULL"),
    ("mapping_versions", "author_user_id", "app_users", "id", "SET NULL"),
    ("quarantine_events", "mapping_version_id", "mapping_versions", "id", "SET NULL"),
    # Tenant da quarentena CASCADE com a org (erase-by-org).
    ("quarantine_events", "organization_id", "organizations", "id", "CASCADE"),
    ("backfill_jobs", "requested_by_user_id", "app_users", "id", "SET NULL"),
    ("data_deletion_jobs", "requested_by_user_id", "app_users", "id", "SET NULL"),
    ("mapping_audit_log", "mapping_definition_id", "mapping_definitions", "id", "SET NULL"),
    ("mapping_audit_log", "mapping_version_id", "mapping_versions", "id", "SET NULL"),
    ("mapping_audit_log", "integration_id", "integrations", "id", "SET NULL"),
    ("mapping_audit_log", "user_id", "app_users", "id", "SET NULL"),
    ("service_accounts", "organization_id", "organizations", "id", "SET NULL"),
    # Proteção explícita — bloqueia deleção do parent enquanto child existe.
    ("data_deletion_jobs", "organization_id", "organizations", "id", "RESTRICT"),
    # Hierarquia de tenants. parent RESTRICT (anti-órfão: não deleta
    # um pai com filhos); root SET NULL (não auto-bloqueia raiz-folha self-ref);
    # closure CASCADE (deletar a org limpa suas arestas).
    ("organizations", "parent_organization_id", "organizations", "id", "RESTRICT"),
    ("organizations", "root_id", "organizations", "id", "SET NULL"),
    ("org_closure", "ancestor_id", "organizations", "id", "CASCADE"),
    ("org_closure", "descendant_id", "organizations", "id", "CASCADE"),
    # Binding de papel escopado. Some com o usuário e com a org-escopo.
    ("org_role_bindings", "user_id", "app_users", "id", "CASCADE"),
    ("org_role_bindings", "scope_org_id", "organizations", "id", "CASCADE"),
    # Concessão de reseller + quota. Some com a org do reseller.
    ("partner_programs", "reseller_org_id", "organizations", "id", "CASCADE"),
)


def _heal_fk_ondelete_rules(inspector_obj) -> None:
    """Reescreve FKs em Postgres pra alinhar com ``_EXPECTED_FK_ONDELETE_RULES``.

    Idempotente: pula linhas já com a rule correta. Para cada FK errada,
    descobre o nome real da constraint em ``information_schema``, dropa
    e recria com a regra esperada. Roda em transação dedicada por FK
    pra evitar que um drop/add no meio do batch quebre os anteriores
    (apesar do trade-off em consistência atomic — um boot interrompido
    deixa as FKs já corrigidas e tenta as restantes no próximo boot).

    Mapeamento ``rule`` (texto) → SQL string que vai no ``ON DELETE``:
        "CASCADE"   → "CASCADE"
        "SET NULL"  → "SET NULL"
        "RESTRICT"  → "RESTRICT"

    SQLite: skip — ``ALTER TABLE ... DROP CONSTRAINT`` não existe no
    SQLite. Para SQLite, ``Base.metadata.create_all`` em DB virgem
    cria as constraints já com ``ondelete=`` corretas (vinda do model).
    Bancos SQLite legados em dev local sobrevivem porque o erro só
    aparece em DELETE com filhos órfãos — raro fora de prod.
    """
    table_names = set(inspector_obj.get_table_names())

    for table, column, ref_table, ref_column, expected in _EXPECTED_FK_ONDELETE_RULES:
        if table not in table_names or ref_table not in table_names:
            continue

        # Postgres reporta a rule em ``information_schema.referential_constraints``
        # como uma das strings: "CASCADE", "SET NULL", "RESTRICT", "NO ACTION",
        # "SET DEFAULT". "NO ACTION" é o default quando ``ON DELETE`` não foi
        # declarado — sintaticamente diferente de RESTRICT mas semanticamente
        # equivalente para deletes imediatos (RESTRICT não pode ser DEFERRED;
        # NO ACTION pode). Para o nosso caso (todas constraints são imediatas),
        # tratamos NO ACTION e RESTRICT como valor "default", que é justamente
        # o que queremos REESCREVER.
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT rc.constraint_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.key_column_usage kcu
                      ON rc.constraint_name = kcu.constraint_name
                     AND rc.constraint_schema = kcu.constraint_schema
                    WHERE kcu.table_name = :table
                      AND kcu.column_name = :column
                      AND kcu.constraint_schema = current_schema()
                    LIMIT 1
                    """
                ),
                {"table": table, "column": column},
            ).fetchone()

            if row is None:
                # FK ainda não existe no banco — provavelmente migration de
                # schema pendente (coluna recém-adicionada). Skip silencioso;
                # criar a FK aqui sem context é arriscado.
                continue

            constraint_name = row.constraint_name
            current_rule = (row.delete_rule or "").upper()

            # Normaliza pra comparação: "SET NULL" pode vir como "SET NULL"
            # (com espaço), idem "NO ACTION". Tudo já uppercase.
            if current_rule == expected:
                continue

            # Drop e recria. ``ON DELETE`` é declarativo no ``ADD CONSTRAINT``.
            # Mantemos o mesmo nome de constraint pra preservar referências
            # em logs/diagnóstico e pra que reruns sejam previsíveis.
            conn.execute(
                text(
                    f'ALTER TABLE "{table}" DROP CONSTRAINT "{constraint_name}"'
                )
            )
            conn.execute(
                text(
                    f'ALTER TABLE "{table}" '
                    f'ADD CONSTRAINT "{constraint_name}" '
                    f'FOREIGN KEY ("{column}") '
                    f'REFERENCES "{ref_table}" ("{ref_column}") '
                    f'ON DELETE {expected}'
                )
            )


def _run_lightweight_migrations() -> None:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    with engine.begin() as conn:
        # Canary: add routes.canary_percent if the table was
        # created before this column existed.
        if "routes" in table_names:
            route_columns = {c["name"] for c in inspector.get_columns("routes")}
            if "canary_percent" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN canary_percent INTEGER NOT NULL DEFAULT 100")
                )
            # Redação de PII por rota (JSON nullable).
            if "pii_redaction" not in route_columns:
                conn.execute(text("ALTER TABLE routes ADD COLUMN pii_redaction TEXT"))
            # Fail-safe: rotas que alimentam
            # detecção NUNCA são amostradas/agregadas. DEFAULT true = protege (o
            # operador faz opt-out por-rota). `DEFAULT true` é cross-dialect
            # (Postgres nativo; SQLite ≥3.23 aliasa true→1) — NÃO usar `DEFAULT 1`
            # (Postgres rejeita int em coluna BOOLEAN).
            if "protect_detection" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN protect_detection BOOLEAN NOT NULL DEFAULT true")
                )
            # Sampling de redução por-rota. DEFAULT 100 = sem
            # amostragem (back-compat byte-idêntico até o operador baixar o valor).
            if "sample_percent" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN sample_percent INTEGER NOT NULL DEFAULT 100")
                )
            # Suppression por assinatura. Defaults = desligado
            # (suppress_key NULL, allow 0) → back-compat byte-idêntico.
            if "suppress_key" not in route_columns:
                conn.execute(text("ALTER TABLE routes ADD COLUMN suppress_key TEXT"))
            if "suppress_allow" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN suppress_allow INTEGER NOT NULL DEFAULT 0")
                )
            if "suppress_window_s" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN suppress_window_s INTEGER NOT NULL DEFAULT 30")
                )
            # Descarte do bloco ``raw`` por-rota (decisão por-destino: lago
            # recebe o bruto, SIEM não). DEFAULT false = byte-idêntico até o
            # operador optar. Mesmo cuidado cross-dialect do protect_detection:
            # `DEFAULT false`, NUNCA `DEFAULT 0` (Postgres rejeita int em BOOLEAN).
            if "drop_raw" not in route_columns:
                conn.execute(
                    text("ALTER TABLE routes ADD COLUMN drop_raw BOOLEAN NOT NULL DEFAULT false")
                )

        # ADR-0015 Fase 1 — discriminador de execução da regra de correlação.
        # DEFAULT 'batch' = back-compat exato: toda regra existente continua
        # sendo avaliada apenas ao final de uma busca federada, e NENHUMA passa
        # a rodar no hot path de ingestão sem que o operador opte por isso.
        # VARCHAR e não ENUM: o domínio ainda pode crescer e um ENUM em Postgres
        # exigiria migração de tipo para cada valor novo.
        if "correlation_rules" in table_names:
            corr_rule_columns = {
                c["name"] for c in inspector.get_columns("correlation_rules")
            }
            if "eval_mode" not in corr_rule_columns:
                conn.execute(
                    text(
                        "ALTER TABLE correlation_rules "
                        "ADD COLUMN eval_mode VARCHAR NOT NULL DEFAULT 'batch'"
                    )
                )

        # Preferência de idioma da UI por usuário (nullable =
        # seguir o Accept-Language do navegador). Sincronizada pelo seletor do SPA.
        if "app_users" in table_names:
            app_user_columns = {c["name"] for c in inspector.get_columns("app_users")}
            if "locale" not in app_user_columns:
                conn.execute(text("ALTER TABLE app_users ADD COLUMN locale VARCHAR"))

        # Cache do resultado da validação OCSF no commit de mapping
        # (JSON nullable; versões legadas ficam NULL). ADD COLUMN bare (TEXT).
        if "mapping_versions" in table_names:
            _mv_cols = {c["name"] for c in inspector.get_columns("mapping_versions")}
            if "ocsf_validation_stats" not in _mv_cols:
                conn.execute(
                    text("ALTER TABLE mapping_versions ADD COLUMN ocsf_validation_stats TEXT")
                )

        # Backfill da política OCSF por-org. A tabela é criada por
        # create_all; aqui garantimos 1 linha 'tag_and_pass' por org existente (rollout
        # seguro: orgs legadas ficam warn-only, então um futuro flip do default GLOBAL
        # p/ 'quarantine' na GA NÃO as afeta retroativamente). Idempotente (NOT EXISTS);
        # CURRENT_TIMESTAMP é cross-dialect (Postgres/SQLite). O resolver cai no default
        # global quando NÃO há linha (orgs criadas entre boots).
        if "organization_ocsf_policy" in table_names and "organizations" in table_names:
            conn.execute(
                text(
                    "INSERT INTO organization_ocsf_policy "
                    "(organization_id, enforcement_mode, created_at, updated_at) "
                    "SELECT o.id, 'tag_and_pass', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                    "FROM organizations o WHERE NOT EXISTS ("
                    "  SELECT 1 FROM organization_ocsf_policy p WHERE p.organization_id = o.id)"
                )
            )

        # Hierarquia de tenants: colunas da árvore em organizations.
        # ADD COLUMN bare p/ as FKs self-ref (a regra ondelete é curada por
        # _heal_fk_ondelete_rules no Postgres quando a FK existe via create_all em
        # DB virgem); depth/kind com DEFAULT preenchem linhas existentes. Os índices
        # batem o auto-naming do SQLAlchemy (ix_organizations_<col>) → CREATE IF NOT
        # EXISTS é no-op no fresh (create_all já criou) e cura DBs legados.
        if "organizations" in table_names:
            _org_cols = {c["name"] for c in inspector.get_columns("organizations")}
            if "parent_organization_id" not in _org_cols:
                conn.execute(text("ALTER TABLE organizations ADD COLUMN parent_organization_id INTEGER"))
            if "root_id" not in _org_cols:
                conn.execute(text("ALTER TABLE organizations ADD COLUMN root_id INTEGER"))
            if "depth" not in _org_cols:
                conn.execute(text("ALTER TABLE organizations ADD COLUMN depth INTEGER NOT NULL DEFAULT 0"))
            if "kind" not in _org_cols:
                conn.execute(text("ALTER TABLE organizations ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'customer'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_organizations_parent_organization_id ON organizations (parent_organization_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_organizations_root_id ON organizations (root_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_organizations_kind ON organizations (kind)"))

        if "app_users" in table_names:
            app_user_columns = {column["name"] for column in inspector.get_columns("app_users")}
            if "uuid" not in app_user_columns:
                conn.execute(text("ALTER TABLE app_users ADD COLUMN uuid VARCHAR"))
            if "organization_id" not in app_user_columns:
                conn.execute(text("ALTER TABLE app_users ADD COLUMN organization_id INTEGER"))
            rows = conn.execute(
                text("SELECT id FROM app_users WHERE uuid IS NULL OR TRIM(uuid) = ''")
            ).fetchall()
            for row in rows:
                conn.execute(
                    text("UPDATE app_users SET uuid = :uuid WHERE id = :id"),
                    {"uuid": str(uuid4()), "id": row.id},
                )
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_app_users_uuid ON app_users (uuid)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_app_users_organization_id ON app_users (organization_id)"))

            # Migração de papel legado 'user' → 'viewer' (conservador).
            # Usuários com papel nos 4 papéis RBAC (viewer/operator/engineer/admin)
            # não são tocados. Promoção posterior é feita manualmente via UI.
            # Nota: constraint CHECK a nível DB entra com Alembic futuro (SQLite não
            # suporta ADD CONSTRAINT em tabelas existentes; Postgres sim, mas exige
            # migration explícita fora do escopo desta Fase).
            conn.execute(
                text(
                    "UPDATE app_users SET role = 'viewer' "
                    "WHERE role NOT IN ('viewer', 'operator', 'engineer', 'admin')"
                )
            )

        if "history" in table_names:
            history_columns = {column["name"] for column in inspector.get_columns("history")}
            if "user_id" not in history_columns:
                conn.execute(text("ALTER TABLE history ADD COLUMN user_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_history_user_id ON history (user_id)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_history_integration_id ON history (integration_id)")
            )

        # organization_id na quarentena (eixo de tenant que
        # faltava) + índice composto (organization_id, created_at) p/ pruning/erase
        # por tenant+tempo. ADD COLUMN bare (sem FK inline, como app_users.org_id):
        # a regra ondelete CASCADE é curada por _heal_fk_ondelete_rules no Postgres
        # quando a FK existe (fresh install via create_all); em DB legado o erase
        # por org segue coberto pelo CASCADE de integration_id. Idempotente.
        if "quarantine_events" in table_names:
            _q_cols = {c["name"] for c in inspector.get_columns("quarantine_events")}
            if "organization_id" not in _q_cols:
                conn.execute(
                    text("ALTER TABLE quarantine_events ADD COLUMN organization_id INTEGER")
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_quarantine_events_organization_id "
                    "ON quarantine_events (organization_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_quarantine_events_org_created "
                    "ON quarantine_events (organization_id, created_at)"
                )
            )

        if "search_results" in table_names:
            search_result_columns = {column["name"] for column in inspector.get_columns("search_results")}
            if "user_id" not in search_result_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN user_id INTEGER"))
            if "result_count" not in search_result_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN result_count INTEGER"))
            # Metadado OCSF + org_id fail-closed + link ao QueryJob.
            if "ocsf_mapping_version" not in search_result_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN ocsf_mapping_version VARCHAR"))
            if "organization_id" not in search_result_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN organization_id INTEGER"))
            if "query_job_id" not in search_result_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN query_job_id INTEGER"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_search_results_user_id ON search_results (user_id)")
            )
            # Índices p/ retenção por-org e lookup por job.
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_search_results_organization_id ON search_results (organization_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_search_results_query_job_id ON search_results (query_job_id)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_search_results_created_at ON search_results (created_at)")
            )

        # Provenance Sigma no QueryJob (spec_kind + statement original).
        if "query_jobs" in table_names:
            qj_columns = {c["name"] for c in inspector.get_columns("query_jobs")}
            if "spec_kind" not in qj_columns:
                conn.execute(text("ALTER TABLE query_jobs ADD COLUMN spec_kind VARCHAR DEFAULT 'passthrough'"))
            if "original_statement" not in qj_columns:
                conn.execute(text("ALTER TABLE query_jobs ADD COLUMN original_statement TEXT"))

        if "audit_logs" in table_names:
            audit_log_columns = {column["name"] for column in inspector.get_columns("audit_logs")}
            if "request_payload" not in audit_log_columns:
                conn.execute(text("ALTER TABLE audit_logs ADD COLUMN request_payload TEXT"))

        if "scheduled_queries" in table_names:
            scheduled_query_columns = {column["name"] for column in inspector.get_columns("scheduled_queries")}

            if "interval_value" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN interval_value INTEGER"))
            if "interval_unit" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN interval_unit VARCHAR"))
            if "lookback_value" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN lookback_value INTEGER"))
            if "lookback_unit" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN lookback_unit VARCHAR"))
            if "notify_on_results" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN notify_on_results BOOLEAN"))
            # ATENÇÃO: PostgreSQL NÃO reconhece DATETIME — usar TIMESTAMP (ANSI SQL);
            # SQLite aceita TIMESTAMP normalmente (ver NOTE em _ensure_*_columns).
            if "last_run_at" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN last_run_at TIMESTAMP"))
            if "created_at" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN created_at TIMESTAMP"))
            if "updated_at" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN updated_at TIMESTAMP"))
            # Org fail-closed + estado de saúde + índice do tick.
            # ``organization_id`` é convergente com a auditoria multi-tenant:
            # mesmo objetivo (escopo de leitura/delete por org). Nullable: linhas
            # antigas viram org NULL (visível só a admin/is_global).
            if "organization_id" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN organization_id INTEGER"))
            if "consecutive_failures" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN consecutive_failures INTEGER DEFAULT 0"))
            if "last_error" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN last_error TEXT"))
            if "last_error_at" not in scheduled_query_columns:
                # TIMESTAMP, não DATETIME — Postgres não reconhece DATETIME.
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN last_error_at TIMESTAMP"))
            if "status" not in scheduled_query_columns:
                conn.execute(text("ALTER TABLE scheduled_queries ADD COLUMN status VARCHAR DEFAULT 'healthy'"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_scheduled_queries_next_run ON scheduled_queries (next_run)")
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_scheduled_queries_organization_id ON scheduled_queries (organization_id)")
            )

            now = datetime.utcnow()
            # Postgres é estrito com tipos: literal `0` é INTEGER e não case
            # com BOOLEAN no COALESCE. Usar FALSE/TRUE — SQLite trata como 0/1
            # internamente, então é portável.
            conn.execute(
                text(
                    """
                    UPDATE scheduled_queries
                    SET interval_value = COALESCE(interval_value, interval_minutes, 60),
                        interval_unit = COALESCE(NULLIF(TRIM(interval_unit), ''), 'minutes'),
                        lookback_value = COALESCE(lookback_value, days_back, 1),
                        lookback_unit = COALESCE(NULLIF(TRIM(lookback_unit), ''), 'days'),
                        notify_on_results = COALESCE(notify_on_results, FALSE),
                        created_at = COALESCE(created_at, :now),
                        updated_at = COALESCE(updated_at, :now)
                    """
                ),
                {"now": now},
            )

        # Auditoria multi-tenant: predefined_queries.organization_id — dono da
        # query salva p/ escopo de leitura/edição/delete. Nullable: linhas antigas
        # viram org NULL (visível só a admin/is_global; fail-closed p/ escopados).
        if "predefined_queries" in table_names:
            pq_columns = {c["name"] for c in inspector.get_columns("predefined_queries")}
            if "organization_id" not in pq_columns:
                conn.execute(
                    text("ALTER TABLE predefined_queries ADD COLUMN organization_id INTEGER")
                )
            # Dialeto + forma do statement (passthrough|sigma|...).
            if "dialect" not in pq_columns:
                conn.execute(text("ALTER TABLE predefined_queries ADD COLUMN dialect VARCHAR"))
            if "spec_kind" not in pq_columns:
                conn.execute(
                    text("ALTER TABLE predefined_queries ADD COLUMN spec_kind VARCHAR DEFAULT 'passthrough'")
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_predefined_queries_organization_id "
                    "ON predefined_queries (organization_id)"
                )
            )
            # Title deixa de ser unique GLOBAL (colidia entre tenants)
            # → unique por (organization_id, title). Postgres: dropa a constraint
            # implícita legada do title; SQLite (dev/test) já recria pelo create_all.
            if engine.dialect.name == "postgresql":
                conn.execute(
                    text("ALTER TABLE predefined_queries DROP CONSTRAINT IF EXISTS predefined_queries_title_key")
                )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_predefined_queries_org_title "
                    "ON predefined_queries (organization_id, title)"
                )
            )

        # notification_emails.organization_id — escopo de
        # destinatário por tenant (fecha o leak cross-tenant do e-mail de
        # scheduled query). Nullable: linhas antigas viram org NULL (sistema).
        if "notification_emails" in table_names:
            notif_columns = {c["name"] for c in inspector.get_columns("notification_emails")}
            if "organization_id" not in notif_columns:
                conn.execute(
                    text("ALTER TABLE notification_emails ADD COLUMN organization_id INTEGER")
                )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_notification_emails_organization_id "
                    "ON notification_emails (organization_id)"
                )
            )

        if "email_config" in table_names:
            rows = conn.execute(
                text(
                    """
                    SELECT id, smtp_password
                    FROM email_config
                    WHERE smtp_password IS NOT NULL AND TRIM(smtp_password) != ''
                    """
                )
            ).fetchall()

            for row in rows:
                if row.smtp_password.startswith("enc::"):
                    continue
                conn.execute(
                    text("UPDATE email_config SET smtp_password = :smtp_password WHERE id = :id"),
                    {
                        "id": row.id,
                        "smtp_password": encrypt(row.smtp_password),
                    },
                )

        if "search_results" in table_names:
            sr_columns = {column["name"] for column in inspector.get_columns("search_results")}
            if "integration_id" not in sr_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN integration_id INTEGER"))
            if "platform" not in sr_columns:
                conn.execute(text("ALTER TABLE search_results ADD COLUMN platform VARCHAR"))

        # Observabilidade de ATRASO REAL da coleta. Até aqui a única medida era
        # ``last_success_at``, reescrito a cada ciclo que termina sem erro — mesmo
        # quando o ciclo processou o dia anterior. Resultado: um coletor 15h
        # atrasado reportava ``lag_seconds: 0`` e ``healthy`` (incidente jul/2026).
        # ``watermark_at`` guarda até onde o cursor chegou na linha do tempo do
        # FORNECEDOR; ``last_run_capped`` diz se sobrou backlog. Os dois juntos
        # distinguem "parado porque não há eventos" de "parado porque não dá conta".
        # NULL/false = desconhecido até o próximo ciclo preencher — nenhuma linha
        # existente é reinterpretada.
        if "collection_state" in table_names:
            cs_columns = {column["name"] for column in inspector.get_columns("collection_state")}
            if "watermark_at" not in cs_columns:
                # TIMESTAMP, não DATETIME — Postgres não reconhece DATETIME.
                conn.execute(text("ALTER TABLE collection_state ADD COLUMN watermark_at TIMESTAMP"))
            if "last_run_capped" not in cs_columns:
                # `DEFAULT false`, NUNCA `DEFAULT 0`: Postgres rejeita int em BOOLEAN.
                conn.execute(
                    text(
                        "ALTER TABLE collection_state "
                        "ADD COLUMN last_run_capped BOOLEAN NOT NULL DEFAULT false"
                    )
                )

        if "integrations" in table_names:
            integration_columns = {column["name"] for column in inspector.get_columns("integrations")}

            for column_name in (
                "manager_api_username",
                "manager_api_password",
                "indexer_username",
                "indexer_password",
                "auth_status",
                "last_checked_at",
                "last_successful_check_at",
                "last_error",
                "config_json",  # config não-secreta de vendor (JSON)
                # Filtros de coleta por stream (JSON). NULL = não filtra nada, que é
                # o que mantém a atualização byte-idêntica: quem nunca abriu a tela
                # continua coletando exatamente o mesmo volume de antes.
                "collection_filters",
            ):
                if column_name not in integration_columns:
                    column_type = "VARCHAR"
                    if column_name in {"last_checked_at", "last_successful_check_at"}:
                        # TIMESTAMP, não DATETIME — Postgres não reconhece DATETIME.
                        column_type = "TIMESTAMP"
                    elif column_name in ("last_error", "config_json", "collection_filters"):
                        column_type = "TEXT"
                    elif column_name == "auth_status":
                        conn.execute(text("ALTER TABLE integrations ADD COLUMN auth_status VARCHAR DEFAULT 'unknown'"))
                        continue

                    conn.execute(text(f"ALTER TABLE integrations ADD COLUMN {column_name} {column_type}"))

            conn.execute(
                text(
                    """
                    UPDATE integrations
                    SET manager_api_username = COALESCE(manager_api_username, api_username),
                        manager_api_password = COALESCE(manager_api_password, api_password),
                        indexer_username = COALESCE(indexer_username, api_username),
                        indexer_password = COALESCE(indexer_password, api_password),
                        auth_status = COALESCE(NULLIF(TRIM(auth_status), ''), 'unknown')
                    WHERE platform = 'wazuh'
                    """
                )
            )

            # ── Sophos Partner Mode — hierarchy + auto-onboarding columns ─
            # Re-inspect to pick up columns added above in this same block.
            integration_columns = {column["name"] for column in inspect(engine).get_columns("integrations")}
            partner_columns = (
                # (name, type_sql, default_sql_or_None)
                # NOTE: ``TIMESTAMP`` (ANSI SQL) instead of ``DATETIME`` —
                # PostgreSQL does NOT recognise ``DATETIME``; SQLite accepts
                # both. Using TIMESTAMP keeps the migration portable.
                ("kind", "VARCHAR", "'tenant'"),
                ("parent_integration_id", "INTEGER", None),
                ("external_id", "VARCHAR", None),
                ("id_type", "VARCHAR", None),
                ("data_geography", "VARCHAR", None),
                ("last_tenant_sync_at", "TIMESTAMP", None),
                ("tenant_sync_status", "VARCHAR", None),
                ("auto_managed", "BOOLEAN", "FALSE"),
                # ``api_host`` armazena o hostname Sophos (sem ``https://``)
                # retornado por ``/partner/v1/tenants``. Quando populado,
                # collectors usam direto em vez de derivar de ``region`` —
                # evita NXDOMAIN quando ``region`` é geo-code (``EU``/``US``)
                # em vez do slug do datacenter (``eu03``/``us02``).
                ("api_host", "VARCHAR", None),
                # Política de descoberta: quando ``True``, sync materializa
                # automaticamente todo tenant novo. Quando ``False`` (default
                # seguro) novos tenants ficam ``state='pending'`` aguardando
                # decisão manual via UI. Coluna nullable pra preservar
                # comportamento de rows existentes — backfill abaixo trata.
                ("auto_approve_new_tenants", "BOOLEAN", "FALSE"),
                # base_url genérico (ex.: NinjaOne). ``tenant_id`` já
                # existe (legado Sophos) e é reutilizado por vendors novos.
                ("base_url", "VARCHAR", None),
            )
            for col_name, col_type, default_sql in partner_columns:
                if col_name in integration_columns:
                    continue
                ddl = f"ALTER TABLE integrations ADD COLUMN {col_name} {col_type}"
                if default_sql is not None:
                    ddl += f" DEFAULT {default_sql}"
                conn.execute(text(ddl))

            # Backfill kind for existing rows so they don't break with NOT NULL semantics.
            conn.execute(
                text(
                    "UPDATE integrations "
                    "SET kind = COALESCE(NULLIF(TRIM(kind), ''), 'tenant') "
                    "WHERE kind IS NULL OR TRIM(kind) = ''"
                )
            )
            # Copy legacy tenant_id into external_id for existing Sophos rows
            # (only when external_id is empty — idempotent on re-runs).
            conn.execute(
                text(
                    """
                    UPDATE integrations
                    SET external_id = tenant_id
                    WHERE platform = 'sophos'
                      AND tenant_id IS NOT NULL
                      AND TRIM(tenant_id) <> ''
                      AND (external_id IS NULL OR TRIM(external_id) = '')
                    """
                )
            )
            # Indexes for hierarchy + lookup hot paths
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_integrations_kind ON integrations (kind)")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_integrations_parent_integration_id "
                    "ON integrations (parent_integration_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_integrations_external_id "
                    "ON integrations (external_id)"
                )
            )

        if "organizations" in table_names:
            organization_columns = {
                column["name"] for column in inspector.get_columns("organizations")
            }
            org_partner_columns = (
                ("external_provider", "VARCHAR", None),
                ("external_id", "VARCHAR", None),
                ("auto_managed", "BOOLEAN", "FALSE"),
                ("iris_customer_id", "INTEGER", None),
                ("partner_integration_id", "INTEGER", None),
            )
            for col_name, col_type, default_sql in org_partner_columns:
                if col_name in organization_columns:
                    continue
                ddl = f"ALTER TABLE organizations ADD COLUMN {col_name} {col_type}"
                if default_sql is not None:
                    ddl += f" DEFAULT {default_sql}"
                conn.execute(text(ddl))
            # Indexes + idempotent unique constraint for Sophos auto-onboarding.
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organizations_external_provider "
                    "ON organizations (external_provider)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organizations_external_id "
                    "ON organizations (external_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organizations_iris_customer_id "
                    "ON organizations (iris_customer_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_organizations_partner_integration_id "
                    "ON organizations (partner_integration_id)"
                )
            )
            # Unique constraint guarantees sync idempotency: same (provider, external_id)
            # never produces two Organization rows.
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_organization_provider_external "
                    "ON organizations (external_provider, external_id) "
                    "WHERE external_provider IS NOT NULL AND external_id IS NOT NULL"
                )
            )

        # ── Threat Intel singleton config seed ──────────────────────
        ti_table_names = set(inspect(engine).get_table_names())
        if "threat_intel_config" in ti_table_names:
            existing = conn.execute(
                text("SELECT COUNT(*) AS n FROM threat_intel_config")
            ).fetchone()
            if existing and existing.n == 0:
                now = datetime.utcnow()
                conn.execute(
                    text(
                        """
                        INSERT INTO threat_intel_config (
                            id, enabled, cache_ttl_days, blacklist_update_interval_seconds,
                            blacklist_confidence_minimum, blacklist_limit, abuseipdb_max_age_days,
                            threat_score_critical, threat_score_high, otx_pulse_high,
                            external_timeout_seconds, created_at, updated_at
                        ) VALUES (
                            1, TRUE, 7, 3600, 80, 10000, 30, 80, 40, 5, 5, :now, :now
                        )
                        """
                    ),
                    {"now": now},
                )

        # ── Mapping definitions + versions seed (normalização)
        # Catálogo dos 5 streams + MappingVersion v1 com regras OCSF
        # carregadas de ``backend.app.collectors.normalize.defaults``.
        # Idempotente em duas dimensões:
        #   - cria definição apenas se ainda não existe (vendor, event_type)
        #   - cria v1 apenas se a definição não tem ``current_version_id``
        # Saves manuais via UI geram v2/v3/... e o seed nunca
        # mais toca essa definição.
        md_table_names = set(inspect(engine).get_table_names())
        if "mapping_definitions" in md_table_names:
            import json as _json

            from ..collectors.normalize.defaults import (
                DEFAULT_MAPPING_FILES,
                load_default_rules,
            )

            seed_definitions = [
                # (vendor, event_type, ocsf_class_uid, description)
                ("sophos", "sophos.alert", 2004, "Sophos Central — alerts (Detection Finding)"),
                ("sophos", "sophos.case", 2005, "Sophos Central — cases (Incident Finding, MDR/XDR)"),
                ("sophos", "sophos.detection", 2004, "Sophos Central — detections (XDR async runs)"),
                ("microsoft_defender", "defender.incident", 2005, "Microsoft Defender — incidents (Incident Finding)"),
                ("microsoft_defender", "defender.alert", 2004, "Microsoft Defender — alerts (Detection Finding)"),
                ("ninjaone", "ninjaone.activity", 6003, "NinjaOne — activities (API Activity)"),
                # Wazuh como FONTE (pull do Indexer) — Detection Finding.
                ("wazuh", "wazuh.detection", 2004, "Wazuh — detections do Indexer (Detection Finding)"),
                # Fontes push/ingest — mapeamento OCSF baseline seedado.
                ("fortinet_fortigate", "fortinet_fortigate.traffic", 4001, "Fortinet FortiGate — traffic (Network Activity)"),
                ("windows_event_log", "windows_event_log.security", 3002, "Windows Event Log/WEC — security (Authentication)"),
                # Vendors com default em DEFAULT_MAPPING_FILES que ficaram FORA
                # do catálogo de seed (gap jul/2026): a 1ª integração ia 100%
                # p/ quarentena por missing_mapping, como no incidente wazuh.
                # class_uid extraído da regra const de cada JSON default.
                ("crowdstrike", "crowdstrike.detection", 2004, "CrowdStrike Falcon — detections (Detection Finding)"),
                ("entra_id", "entra_id.signin", 3002, "Microsoft Entra ID — sign-ins (Authentication)"),
                ("entra_id", "entra_id.audit", 3001, "Microsoft Entra ID — directory audit (Account Change)"),
                ("okta", "okta.system_log", 3002, "Okta — System Log (Authentication)"),
                ("aws_cloudtrail", "aws_cloudtrail.event", 6003, "AWS CloudTrail — management events (API Activity)"),
                ("aws_cloudwatch", "aws_cloudwatch.event", 0, "AWS CloudWatch Logs — log events de um log group (Base Event: transporte heterogêneo)"),
                ("veeam", "veeam.session", 1006, "Veeam Backup & Replication — sessões de job (Scheduled Job Activity)"),
            ]
            now = datetime.utcnow()

            for vendor, event_type, class_uid, description in seed_definitions:
                row = conn.execute(
                    text(
                        "SELECT id, current_version_id "
                        "FROM mapping_definitions "
                        "WHERE vendor = :v AND event_type = :e"
                    ),
                    {"v": vendor, "e": event_type},
                ).fetchone()

                if row is None:
                    definition_id = str(uuid4())
                    conn.execute(
                        text(
                            """
                            INSERT INTO mapping_definitions (
                                id, vendor, event_type, ocsf_class_uid,
                                description, current_version_id,
                                created_at, updated_at
                            ) VALUES (
                                :id, :v, :e, :cu, :d, NULL, :now, :now
                            )
                            """
                        ),
                        {
                            "id": definition_id,
                            "v": vendor,
                            "e": event_type,
                            "cu": class_uid,
                            "d": description,
                            "now": now,
                        },
                    )
                    current_version_id = None
                else:
                    definition_id = row.id
                    current_version_id = row.current_version_id

                # Cria seed só se a definição ainda não tem versão atual.
                if current_version_id is None and (vendor, event_type) in DEFAULT_MAPPING_FILES:
                    try:
                        rules = load_default_rules(vendor, event_type)
                    except (FileNotFoundError, ValueError):
                        # Sem mapping default disponível — definição fica
                        # com current=NULL e o pipeline manda em quarentena.
                        continue

                    # Backend padroniza shape v2 (dict). Mappings default em
                    # disco podem estar em formato list (legado): wrap.
                    if isinstance(rules, list):
                        rules_payload = {"preprocess": [], "rules": rules}
                    elif isinstance(rules, dict):
                        rules_payload = {
                            "preprocess": list(rules.get("preprocess") or []),
                            "rules": list(rules.get("rules") or []),
                        }
                    else:
                        continue

                    version_id = str(uuid4())
                    conn.execute(
                        text(
                            """
                            INSERT INTO mapping_versions (
                                id, definition_id, version_number,
                                rules, author_user_id, commit_message,
                                diff_from_previous, dry_run_stats,
                                dsl_version, created_at
                            ) VALUES (
                                :id, :def_id, 1,
                                :rules, NULL, :commit_msg,
                                NULL, NULL,
                                :dsl_version, :now
                            )
                            """
                        ),
                        {
                            "id": version_id,
                            "def_id": definition_id,
                            "rules": _json.dumps(rules_payload, separators=(",", ":")),
                            "dsl_version": 2,
                            "commit_msg": "Initial seed",
                            "now": now,
                        },
                    )
                    conn.execute(
                        text(
                            "UPDATE mapping_definitions "
                            "SET current_version_id = :vid, updated_at = :now "
                            "WHERE id = :def_id"
                        ),
                        {"vid": version_id, "def_id": definition_id, "now": now},
                    )

        # ── DSL v2 only — coluna dsl_version + normalização de rules ──────
        # Backend padroniza shape v2 (dict com preprocess+rules). Linhas
        # legadas persistidas como list pura são reescritas aqui de forma
        # idempotente. Coluna dsl_version segue existindo (zero-downtime),
        # mas todas as linhas viram 2 e novos writes hardcodam 2.
        if "mapping_versions" in table_names:
            import json as _json_mig

            mv_columns = {col["name"] for col in inspector.get_columns("mapping_versions")}
            if "dsl_version" not in mv_columns:
                conn.execute(
                    text(
                        "ALTER TABLE mapping_versions "
                        "ADD COLUMN dsl_version INTEGER NOT NULL DEFAULT 2"
                    )
                )

            mv_rows = conn.execute(
                text("SELECT id, rules FROM mapping_versions")
            ).fetchall()
            for mv_row in mv_rows:
                if not mv_row.rules:
                    continue
                try:
                    parsed = _json_mig.loads(mv_row.rules)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    if "rules" in parsed and "preprocess" in parsed:
                        continue
                    new_payload = {
                        "preprocess": list(parsed.get("preprocess") or []),
                        "rules": list(parsed.get("rules") or []),
                    }
                elif isinstance(parsed, list):
                    new_payload = {"preprocess": [], "rules": list(parsed)}
                else:
                    continue
                conn.execute(
                    text(
                        "UPDATE mapping_versions "
                        "SET rules = :rules, dsl_version = 2 "
                        "WHERE id = :id"
                    ),
                    {
                        "id": mv_row.id,
                        "rules": _json_mig.dumps(new_payload, separators=(",", ":")),
                    },
                )

        # ── mapping_audit_log: nova coluna integration_id e índices ──
        if "mapping_audit_log" in table_names:
            mal_columns = {col["name"] for col in inspector.get_columns("mapping_audit_log")}
            if "integration_id" not in mal_columns:
                conn.execute(
                    text("ALTER TABLE mapping_audit_log ADD COLUMN integration_id INTEGER")
                )
            # Índice composto para queries de auditoria por definição + data
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_mapping_audit_def_ts "
                    "ON mapping_audit_log (mapping_definition_id, created_at DESC)"
                )
            )

        # ── unknown_fields: índice de lookup ───────────────
        if "unknown_fields" in table_names:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_unknown_fields_lookup "
                    "ON unknown_fields (vendor, event_type, last_seen)"
                )
            )

        # unknown_fields.organization_id: ver bloco ISOLADO
        # fora deste ``with engine.begin() as conn:`` (logo após o heal de
        # api_host), porque o backfill é write NÃO idempotente e seria
        # descartado pelo ROLLBACK do inspect() interno deste bloco.

        # ── collector_config: adiciona wazuh_syslog_format ─
        # Linhas existentes (prod) recebem 'rfc5424' para preservar
        # comportamento atual. Novas linhas usam 'rfc3164' (default).
        if "collector_config" in table_names:
            cc_cols = {col["name"] for col in inspector.get_columns("collector_config")}
            if "wazuh_syslog_format" not in cc_cols:
                # 'rfc5424' para linhas existentes — preserva prod legado.
                conn.execute(
                    text(
                        "ALTER TABLE collector_config "
                        "ADD COLUMN wazuh_syslog_format VARCHAR "
                        "NOT NULL DEFAULT 'rfc5424'"
                    )
                )
            # TTL de dedupe em SEGUNDOS (canônico). NULLABLE de propósito: NULL
            # significa "deriva de dedupe_ttl_days", então uma linha
            # pré-migração continua valendo exatamente o que valia. O backfill
            # abaixo materializa o valor equivalente para que a UI mostre o
            # número certo já na primeira abertura.
            if "dedupe_ttl_seconds" not in cc_cols:
                conn.execute(
                    text("ALTER TABLE collector_config ADD COLUMN dedupe_ttl_seconds INTEGER")
                )
                conn.execute(
                    text(
                        "UPDATE collector_config "
                        "SET dedupe_ttl_seconds = dedupe_ttl_days * 86400 "
                        "WHERE dedupe_ttl_seconds IS NULL AND dedupe_ttl_days IS NOT NULL"
                    )
                )

        # ── api_tokens: PAT (Personal Access Tokens) ───────
        # Tabela criada via Base.metadata.create_all em initialize_database;
        # aqui só garantimos índices secundários e idempotência caso a
        # tabela tenha sido criada por uma migration anterior sem alguns
        # índices que viramos a definir depois.
        if "api_tokens" in table_names:
            api_tokens_columns = {
                column["name"] for column in inspect(engine).get_columns("api_tokens")
            }
            # Defesa em profundidade: caso a tabela exista de uma rev anterior,
            # adiciona colunas faltantes sem perder linhas existentes.
            #
            # ATENÇÃO: PostgreSQL exige TIMESTAMP, não DATETIME.
            #
            # Novas colunas: service_account_id, scopes_json, is_eternal.
            for col_name, col_type in (
                ("expires_at", "TIMESTAMP"),
                ("last_used_at", "TIMESTAMP"),
                ("last_used_ip", "VARCHAR"),
                ("use_count", "INTEGER NOT NULL DEFAULT 0"),
                ("revoked_at", "TIMESTAMP"),
                ("service_account_id", "INTEGER"),
                ("scopes_json", "TEXT"),
                # NOTE: ``DEFAULT FALSE`` (ANSI SQL) instead of ``DEFAULT 0``.
                # PostgreSQL é estrito com tipos — boolean = integer não tem
                # operador, e SQLite trata ambos como 0/1. Usar FALSE/TRUE
                # mantém a migration portável.
                ("is_eternal", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ):
                if col_name not in api_tokens_columns:
                    conn.execute(
                        text(f"ALTER TABLE api_tokens ADD COLUMN {col_name} {col_type}")
                    )

            # Relax NOT NULL em ``user_id``. O schema original
            # tinha ``user_id NOT NULL`` porque todo PAT pertencia a um
            # AppUser. Depois vieram Service Accounts: tokens podem ter
            # ``service_account_id`` setado e ``user_id=NULL`` (XOR enforced
            # por ``CheckConstraint`` no model). O model em SQLAlchemy já
            # declara ``nullable=True``, mas em DBs criados antes disso a
            # constraint NOT NULL ficou no Postgres.
            #
            # Sem este ALTER, ``POST /service-accounts/{id}/tokens`` retorna
            # 500 com ``NotNullViolation`` em prod. SQLite ignora silente
            # (constraint não-enforced no SQLAlchemy default), por isso o
            # bug só aparece em Postgres.
            #
            # Idempotente: checa ``information_schema.columns.is_nullable``
            # antes de alterar. SQLite skip (não suporta ALTER COLUMN
            # DROP NOT NULL; ``create_all`` em DB virgem já vem nullable).
            if engine.dialect.name == "postgresql":
                is_user_id_nullable = conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'api_tokens' "
                        "  AND column_name = 'user_id'"
                    )
                ).scalar()
                if is_user_id_nullable == "NO":
                    conn.execute(
                        text("ALTER TABLE api_tokens ALTER COLUMN user_id DROP NOT NULL")
                    )

            # Para tokens emitidos antes, marcar como eternos
            # quando expires_at IS NULL (preserva semântica original: NULL =
            # nunca expira). Idempotente. ``TRUE``/``FALSE`` por compat
            # Postgres+SQLite (vide nota acima sobre 0/1 vs boolean).
            conn.execute(
                text(
                    "UPDATE api_tokens SET is_eternal = TRUE "
                    "WHERE expires_at IS NULL AND is_eternal = FALSE"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_api_tokens_user_id "
                    "ON api_tokens (user_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_api_tokens_service_account_id "
                    "ON api_tokens (service_account_id)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_api_tokens_revoked_at "
                    "ON api_tokens (revoked_at)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_api_tokens_token_prefix "
                    "ON api_tokens (token_prefix)"
                )
            )

        # ── service_accounts: identidades non-human ────────
        # Tabela criada via Base.metadata.create_all; aqui só índices
        # secundários e idempotência caso já exista de rev anterior.
        if "service_accounts" in table_names:
            sa_columns = {
                column["name"] for column in inspect(engine).get_columns("service_accounts")
            }
            for col_name, col_type in (
                ("description", "TEXT"),
                ("organization_id", "INTEGER"),
                # DEFAULT TRUE (ANSI), não `1`: Postgres é estrito (boolean ≠
                # integer) e rejeitaria `DEFAULT 1` num ADD COLUMN de DB legado.
                # SQLite aceita ambos; só fere no upgrade de Postgres real.
                ("is_active", "BOOLEAN NOT NULL DEFAULT TRUE"),
                ("created_by_user_id", "INTEGER"),
                ("created_at", "TIMESTAMP"),
                ("updated_at", "TIMESTAMP"),
            ):
                if col_name not in sa_columns:
                    conn.execute(
                        text(f"ALTER TABLE service_accounts ADD COLUMN {col_name} {col_type}")
                    )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_service_accounts_name "
                    "ON service_accounts (name)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_service_accounts_organization_id "
                    "ON service_accounts (organization_id)"
                )
            )

        # ── Entra — app_users: identidade federada + escopo global ─
        # Colunas pra suportar login OIDC/SCIM e o "analista global"
        # do SOC interno. Idempotente: ADD COLUMN só se ausente; o DEFAULT
        # preenche linhas existentes (contas locais → 'local' / false).
        if "app_users" in table_names:
            app_users_columns = {
                column["name"] for column in inspect(engine).get_columns("app_users")
            }
            # ATENÇÃO: PostgreSQL exige TIMESTAMP, não DATETIME; e DEFAULT
            # FALSE (ANSI) em vez de 0 — booleano portável Postgres+SQLite.
            for col_name, col_type in (
                ("email", "VARCHAR"),
                ("auth_provider", "VARCHAR NOT NULL DEFAULT 'local'"),
                ("external_subject", "VARCHAR"),
                ("is_global", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ):
                if col_name not in app_users_columns:
                    conn.execute(
                        text(f"ALTER TABLE app_users ADD COLUMN {col_name} {col_type}")
                    )

            # Relax NOT NULL em password_hash: contas federadas não têm senha.
            # Mesmo padrão do api_tokens.user_id. SQLite skip — não
            # suporta ALTER COLUMN e DB virgem já vem nullable.
            if engine.dialect.name == "postgresql":
                pw_nullable = conn.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'app_users' "
                        "  AND column_name = 'password_hash'"
                    )
                ).scalar()
                if pw_nullable == "NO":
                    conn.execute(
                        text("ALTER TABLE app_users ALTER COLUMN password_hash DROP NOT NULL")
                    )

            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_app_users_email "
                    "ON app_users (email)"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_app_users_external_subject "
                    "ON app_users (external_subject)"
                )
            )
            # Unicidade (auth_provider, external_subject): 1 conta por sujeito
            # de IdP. NULLs (contas locais) não conflitam em ambos os bancos.
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_app_users_provider_subject "
                    "ON app_users (auth_provider, external_subject)"
                )
            )

        # ── Collector config singleton seed ─────────────────────────
        # Primeira subida popula a tabela com valores do ``.env`` atual
        # (via ``settings``). Deploy subsequentes: preserva o que o
        # operador editou na UI.
        cc_table_names = set(inspect(engine).get_table_names())
        if "collector_config" in cc_table_names:
            existing = conn.execute(
                text("SELECT COUNT(*) AS n FROM collector_config")
            ).fetchone()
            if existing and existing.n == 0:
                import json

                from ..core.config import settings as _settings

                now = datetime.utcnow()
                # Heurística de porta: se TLS não estiver pré-configurado,
                # usa 514 (TCP plaintext — Wazuh vanilla). Caso contrário,
                # respeita o que já estava no env (normalmente 6514).
                seed_port = (
                    int(_settings.WAZUH_SYSLOG_PORT)
                    if _settings.WAZUH_SYSLOG_PORT
                    else 514
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO collector_config (
                            id,
                            wazuh_syslog_host, wazuh_syslog_port,
                            wazuh_syslog_use_tls, wazuh_ca_bundle,
                            wazuh_dispatch_mode, collector_jsonl_dir,
                            collector_batch_size, collector_batch_flush_seconds,
                            dedupe_ttl_days,
                            domain_concurrency_limits, rate_limits_by_vendor,
                            created_at, updated_at
                        ) VALUES (
                            1,
                            :host, :port,
                            :use_tls, :ca,
                            :mode, :jsonl_dir,
                            :bs, :flush,
                            :ttl,
                            :dcl, :rlv,
                            :now, :now
                        )
                        """
                    ),
                    {
                        "host": _settings.WAZUH_SYSLOG_HOST or None,
                        "port": seed_port,
                        # Default TLS OFF (Wazuh vanilla não aceita TLS).
                        # Se o operador já tiver CA configurado no env,
                        # marca como TLS on — intenção explícita.
                        "use_tls": bool(_settings.WAZUH_CA_BUNDLE),
                        "ca": _settings.WAZUH_CA_BUNDLE or None,
                        "mode": _settings.WAZUH_DISPATCH_MODE or "syslog",
                        "jsonl_dir": (
                            _settings.COLLECTOR_JSONL_DIR
                            or "/var/log/centralops/collectors"
                        ),
                        "bs": _settings.COLLECTOR_BATCH_SIZE or 200,
                        "flush": _settings.COLLECTOR_BATCH_FLUSH_SECONDS or 5,
                        "ttl": _settings.DEDUPE_TTL_DAYS or 7,
                        "dcl": json.dumps(
                            dict(_settings.DOMAIN_CONCURRENCY_LIMITS or {}),
                            separators=(",", ":"),
                        ),
                        "rlv": json.dumps(
                            dict(_settings.RATE_LIMITS_BY_VENDOR or {}),
                            separators=(",", ":"),
                        ),
                        "now": now,
                    },
                )

        # ── Identity config singleton seed (Entra/SSO) ──────
        # Primeira subida popula a partir das env ENTRA_* (via settings).
        # Depois a UI (/config → Identidade & SSO) é a fonte de verdade.
        # O client_secret é cifrado antes de persistir.
        ic_table_names = set(inspect(engine).get_table_names())
        if "identity_config" in ic_table_names:
            ic_existing = conn.execute(
                text("SELECT COUNT(*) AS n FROM identity_config")
            ).fetchone()
            if ic_existing and ic_existing.n == 0:
                import json as _json_ic

                from ..core.config import settings as _s_ic

                now_ic = datetime.utcnow()
                ic_domains = _s_ic.ENTRA_ALLOWED_EMAIL_DOMAINS
                if isinstance(ic_domains, str):
                    ic_domains = [d.strip().lower() for d in ic_domains.split(",") if d.strip()]
                conn.execute(
                    text(
                        """
                        INSERT INTO identity_config (
                            id, entra_enabled, entra_tenant_id, entra_client_id,
                            entra_client_secret, entra_redirect_uri, entra_authority,
                            entra_scopes, entra_role_map, entra_default_role,
                            entra_default_is_global, entra_jit_provisioning,
                            entra_allowed_email_domains, entra_button_label,
                            entra_post_login_redirect, created_at, updated_at
                        ) VALUES (
                            1, :enabled, :tenant, :client, :secret, :redirect,
                            :authority, :scopes, :role_map, :default_role,
                            :default_global, :jit, :domains, :button, :post_login,
                            :now, :now
                        )
                        """
                    ),
                    {
                        "enabled": bool(_s_ic.ENTRA_ENABLED),
                        "tenant": _s_ic.ENTRA_TENANT_ID or None,
                        "client": _s_ic.ENTRA_CLIENT_ID or None,
                        "secret": (
                            encrypt(_s_ic.ENTRA_CLIENT_SECRET)
                            if _s_ic.ENTRA_CLIENT_SECRET
                            else None
                        ),
                        "redirect": _s_ic.ENTRA_REDIRECT_URI or None,
                        "authority": _s_ic.ENTRA_AUTHORITY,
                        "scopes": _s_ic.ENTRA_SCOPES,
                        "role_map": _json_ic.dumps(
                            dict(_s_ic.ENTRA_ROLE_MAP or {}), separators=(",", ":")
                        ),
                        "default_role": _s_ic.ENTRA_DEFAULT_ROLE,
                        "default_global": bool(_s_ic.ENTRA_DEFAULT_IS_GLOBAL),
                        "jit": bool(_s_ic.ENTRA_JIT_PROVISIONING),
                        "domains": _json_ic.dumps(ic_domains, separators=(",", ":")),
                        "button": _s_ic.ENTRA_BUTTON_LABEL,
                        "post_login": _s_ic.ENTRA_POST_LOGIN_REDIRECT,
                        "now": now_ic,
                    },
                )

            # Adiciona colunas novas se nao existirem (idempotente).
            _IC_NEW_COLS_2B = {
                "entra_sync_enabled":    "BOOLEAN NOT NULL DEFAULT 0",
                "entra_sync_deprovision": "BOOLEAN NOT NULL DEFAULT 1",
                "entra_last_sync_at":    "TIMESTAMP",
                "entra_last_sync_status": "VARCHAR",
                "entra_last_sync_summary": "TEXT",
            }
            ic_columns_existing = {
                col["name"]
                for col in inspect(engine).get_columns("identity_config")
            }
            for _col_name, _col_def in _IC_NEW_COLS_2B.items():
                if _col_name not in ic_columns_existing:
                    conn.execute(text(
                        f"ALTER TABLE identity_config ADD COLUMN {_col_name} {_col_def}"
                    ))

    # ── Sophos Partner — integration_tenant_selections + backfill ───────
    # IMPORTANTE: roda em sua própria transação isolada pela mesma razão
    # do bloco de heal abaixo: o pipeline acima intercala ``inspect(engine)``,
    # e o ROLLBACK implícito do inspect descarta writes pendentes da outer
    # transaction (vide nota detalhada no bloco seguinte). Aqui, em
    # particular, o INSERT do backfill é one-shot e NÃO é trivialmente
    # idempotente em re-runs (a query usa NOT EXISTS, então é safe re-rodar,
    # mas não queremos depender disso por sorte).
    inspector = inspect(engine)
    table_names_now = set(inspector.get_table_names())
    if "integrations" in table_names_now and "app_users" in table_names_now:
        with engine.begin() as ts_conn:
            # 1) Garante a tabela mesmo quando ``Base.metadata.create_all``
            #    não roda (testes isolados que invocam migrations sem ORM).
            # NOTA: dialect é checada via ``engine.dialect.name`` (não via
            # ``DATABASE_URL.startswith``) porque ``DATABASE_URL`` é resolvido
            # no import do módulo. Em testes que monkey-patcham ``engine`` pra
            # Postgres, o ``DATABASE_URL`` permanece SQLite — chequer com
            # ``DATABASE_URL`` quebra a migration nesses casos. Usar
            # ``engine.dialect.name`` reflete o engine ATUAL.
            _is_sqlite = engine.dialect.name == "sqlite"
            ts_conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS integration_tenant_selections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        parent_integration_id INTEGER NOT NULL,
                        external_id VARCHAR NOT NULL,
                        state VARCHAR NOT NULL,
                        decided_by_user_id INTEGER,
                        decided_at TIMESTAMP,
                        name_snapshot VARCHAR,
                        region_snapshot VARCHAR,
                        data_geography_snapshot VARCHAR,
                        api_host_snapshot VARCHAR,
                        last_seen_at TIMESTAMP,
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL,
                        FOREIGN KEY (parent_integration_id)
                            REFERENCES integrations(id) ON DELETE CASCADE,
                        FOREIGN KEY (decided_by_user_id)
                            REFERENCES app_users(id) ON DELETE SET NULL
                    )
                    """
                )
                if _is_sqlite
                else text(
                    """
                    CREATE TABLE IF NOT EXISTS integration_tenant_selections (
                        id SERIAL PRIMARY KEY,
                        parent_integration_id INTEGER NOT NULL
                            REFERENCES integrations(id) ON DELETE CASCADE,
                        external_id VARCHAR NOT NULL,
                        state VARCHAR NOT NULL,
                        decided_by_user_id INTEGER
                            REFERENCES app_users(id) ON DELETE SET NULL,
                        decided_at TIMESTAMP,
                        name_snapshot VARCHAR,
                        region_snapshot VARCHAR,
                        data_geography_snapshot VARCHAR,
                        api_host_snapshot VARCHAR,
                        last_seen_at TIMESTAMP,
                        created_at TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP NOT NULL
                    )
                    """
                )
            )
            ts_conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_selection_parent_external "
                    "ON integration_tenant_selections (parent_integration_id, external_id)"
                )
            )
            ts_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_tenant_selection_parent_state "
                    "ON integration_tenant_selections (parent_integration_id, state)"
                )
            )
            ts_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_tenant_selection_parent_id "
                    "ON integration_tenant_selections (parent_integration_id)"
                )
            )
            ts_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_tenant_selection_decided_by "
                    "ON integration_tenant_selections (decided_by_user_id)"
                )
            )

            # 2) Backfill: todo child com auto_managed=true e parent_integration_id
            #    NOT NULL que NÃO tem row de seleção vira approved retroativamente.
            #    Isso preserva o estado dos children legados — operadores não
            #    precisam re-aprovar tenants que já estavam funcionando.
            now_dt = datetime.utcnow()
            ts_conn.execute(
                text(
                    """
                    INSERT INTO integration_tenant_selections (
                        parent_integration_id, external_id, state,
                        decided_by_user_id, decided_at,
                        name_snapshot, region_snapshot, api_host_snapshot,
                        last_seen_at, created_at, updated_at
                    )
                    SELECT
                        i.parent_integration_id,
                        i.external_id,
                        'approved',
                        NULL,
                        :now,
                        i.name,
                        i.region,
                        i.api_host,
                        :now,
                        :now,
                        :now
                    FROM integrations i
                    WHERE i.auto_managed = TRUE
                      AND i.parent_integration_id IS NOT NULL
                      AND i.external_id IS NOT NULL
                      AND TRIM(i.external_id) <> ''
                      AND NOT EXISTS (
                          SELECT 1 FROM integration_tenant_selections s
                          WHERE s.parent_integration_id = i.parent_integration_id
                            AND s.external_id = i.external_id
                      )
                    """
                ),
                {"now": now_dt},
            )

    # ── ON DELETE rules: alinhar Postgres ao schema declarado em models ─
    # SQLAlchemy emite o ``ON DELETE`` rule só na criação inicial da
    # constraint; ALTER da column no metadata não recria o FK no banco.
    # Esta migration descobre o nome real da constraint via
    # ``information_schema`` e reescreve a rule, idempotente: se já
    # estiver com a regra esperada, nem toca.
    #
    # Política aplicada:
    #   SET NULL  → audit/histórico (preserva trilha forense).
    #   CASCADE   → operacional (sem parent, child é lixo).
    #   RESTRICT  → proteção explícita (jobs de retenção em andamento).
    if engine.dialect.name == "postgresql":
        _heal_fk_ondelete_rules(inspector)

    # ── Heal: NULL-out fantasma ``api_host`` derivado-errado ────────────
    # IMPORTANT: este bloco roda em sua PRÓPRIA transaction, FORA do
    # ``with engine.begin() as conn:`` acima. Isso porque o bloco acima
    # intercala chamadas a ``inspect(engine)``, e cada uma delas dispara
    # um ``BEGIN/ROLLBACK`` na conexão subjacente (StaticPool/SQLite usa
    # uma única conexão física). O ``ROLLBACK`` do inspect DESCARTA
    # writes pendentes da nossa outer transaction quando não há savepoint.
    # As demais UPDATEs do bloco acima são idempotentes; esta heal NÃO é,
    # logo precisa de transação isolada.
    #
    # Sophos Central usa slugs de datacenter (``eu01``/``us03``/...) — não
    # geo-codes (``EU``/``US``/...). Se um registro persistiu um host
    # derivado de geo-code, ele é inválido (NXDOMAIN). Anulamos para que o
    # próximo sync repopule a partir do payload canônico de ``/partner/v1``.
    inspector = inspect(engine)
    if "integrations" in set(inspector.get_table_names()):
        with engine.begin() as heal_conn:
            heal_conn.execute(
                text(
                    """
                    UPDATE integrations
                    SET api_host = NULL,
                        updated_at = :now
                    WHERE platform = 'sophos'
                      AND api_host IS NOT NULL
                      AND api_host IN (
                          'api-US.central.sophos.com',
                          'api-EU.central.sophos.com',
                          'api-DE.central.sophos.com',
                          'api-JP.central.sophos.com',
                          'api-CA.central.sophos.com',
                          'api-AU.central.sophos.com',
                          'api-BR.central.sophos.com',
                          'api-GB.central.sophos.com',
                          'api-IE.central.sophos.com',
                          'api-IN.central.sophos.com'
                      )
                    """
                ),
                {"now": datetime.utcnow()},
            )

    # ── unknown_fields.organization_id (bloco ISOLADO) ──
    # Mesma razão do heal acima: o backfill é write NÃO idempotente que o
    # ROLLBACK do inspect() no bloco principal descartaria (StaticPool/SQLite).
    # ``inspect`` roda ANTES de abrir a transação; o backfill (DML) vem por
    # ÚLTIMO, sem DDL após, para o commit do bloco persisti-lo. Idempotente:
    # roda só enquanto a coluna não existir (DB novo já a tem via create_all).
    inspector = inspect(engine)
    if "unknown_fields" in set(inspector.get_table_names()):
        _uf_cols = {c["name"] for c in inspector.get_columns("unknown_fields")}
        if "organization_id" not in _uf_cols:
            # ``engine.dialect.name`` (não DATABASE_URL) — testes monkey-patcham
            # o engine p/ Postgres mantendo a URL SQLite.
            _uf_sqlite = engine.dialect.name == "sqlite"
            with engine.begin() as uf_conn:
                if _uf_sqlite:
                    # SQLite não adiciona FK via ALTER — coluna pura; FK no ORM.
                    uf_conn.execute(
                        text("ALTER TABLE unknown_fields ADD COLUMN organization_id INTEGER")
                    )
                else:
                    uf_conn.execute(
                        text(
                            "ALTER TABLE unknown_fields ADD COLUMN organization_id INTEGER "
                            "REFERENCES organizations(id) ON DELETE CASCADE"
                        )
                    )
                uf_conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_unknown_fields_organization_id "
                        "ON unknown_fields (organization_id)"
                    )
                )
                # Amplia a unicidade (DDL). SQLite: índice único (DROP INDEX);
                # Postgres: constraint (DROP CONSTRAINT).
                if _uf_sqlite:
                    uf_conn.execute(text("DROP INDEX IF EXISTS uq_unknown_field_path"))
                else:
                    uf_conn.execute(
                        text(
                            "ALTER TABLE unknown_fields "
                            "DROP CONSTRAINT IF EXISTS uq_unknown_field_path"
                        )
                    )
                uf_conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_unknown_field_vendor_event_org "
                        "ON unknown_fields (vendor, event_type, field_path, organization_id)"
                    )
                )
                # Backfill por ÚLTIMO (DML, sem DDL após): integração mais ANTIGA
                # (created_at ASC, id ASC como desempate determinístico) que
                # possui o vendor "dona" das rows legadas.
                #
                # LIMITAÇÃO conhecida (baixa severidade): em deploy
                # MSSP PRÉ-EXISTENTE onde 2+ tenants já ingeriam o MESMO vendor,
                # as rows legadas (sem org) colapsam na org da integração mais
                # antiga — possível MISATRIBUIÇÃO de drift legado. Não afeta DB
                # novo nem deploy single-tenant-por-vendor. Rows cujo vendor não
                # tem integração ficam NULL (órfãs) — nunca servidas (leitura é
                # fail-closed por org) e não apagáveis por org (gap de erasure
                # documentado). Drift é regenerado no próximo ciclo de coleta.
                uf_conn.execute(
                    text(
                        """
                        UPDATE unknown_fields
                        SET organization_id = (
                            SELECT i.organization_id FROM integrations i
                            WHERE i.platform = unknown_fields.vendor
                            ORDER BY i.created_at ASC, i.id ASC
                            LIMIT 1
                        )
                        WHERE organization_id IS NULL
                        """
                    )
                )

    # ── Seed: destino wazuh-default (saída desacoplada) ──────
    # Bloco PRÓPRIO (mesma razão do heal acima): o seed é não-idempotente
    # (INSERT), e o ``inspect(engine)`` intercalado no bloco principal
    # descartaria o write sob StaticPool/SQLite (ROLLBACK do inspect). O
    # ``inspect`` aqui roda ANTES de abrir a transação; dentro só há
    # SELECT/INSERT. Materializa o destino Wazuh a partir das colunas
    # ``wazuh_*`` do collector_config — caminho Wazuh idêntico. Idempotente:
    # só insere se a linha id='wazuh-default' ainda não existe.
    inspector = inspect(engine)
    _dst_tables = set(inspector.get_table_names())
    if "destinations" in _dst_tables and "collector_config" in _dst_tables:
        with engine.begin() as dst_conn:
            dst_existing = dst_conn.execute(
                text("SELECT COUNT(*) AS n FROM destinations WHERE id = 'wazuh-default'")
            ).fetchone()
            cc_row = dst_conn.execute(
                text(
                    "SELECT wazuh_syslog_host, wazuh_syslog_port, "
                    "wazuh_syslog_use_tls, wazuh_ca_bundle, wazuh_dispatch_mode, "
                    "wazuh_syslog_format, collector_jsonl_dir "
                    "FROM collector_config WHERE id = 1"
                )
            ).fetchone()
            # Vendor-neutro: um SDPP NÃO presume um sink. Só
            # materializa o destino Wazuh legado quando o operador REALMENTE
            # configurou Wazuh — host syslog setado OU modo jsonl explícito (não-
            # default). Numa instalação NOVA, ``wazuh_syslog_host`` é NULL e o modo é
            # o default "syslog" → NENHUM destino é semeado (lista zerada, como deve
            # ser). Num upgrade de quem usava Wazuh, o host está setado → preserva a
            # entrega. Destinos já existentes (n>0) nunca são recriados.
            _wazuh_configured = cc_row is not None and (
                (cc_row.wazuh_dispatch_mode or "syslog") == "jsonl"
                or bool((cc_row.wazuh_syslog_host or "").strip())
            )
            if dst_existing and dst_existing.n == 0 and _wazuh_configured:
                import json as _json_dst

                from ..collectors.output.destinations.registry import (
                    compute_config_version as _compute_config_version,
                )

                # Derivar o kind vendor-neutro a partir da config legada:
                # jsonl  → kind=jsonl  (config tem apenas jsonl_dir)
                # rfc5424 → kind=syslog_rfc5424 (config tem host/port/use_tls/ca_bundle)
                # rfc3164 → kind=syslog_rfc3164 (idem — default Wazuh)
                _dispatch_mode = cc_row.wazuh_dispatch_mode or "syslog"
                _syslog_format = (
                    getattr(cc_row, "wazuh_syslog_format", None) or "rfc3164"
                )

                if _dispatch_mode == "jsonl":
                    dst_kind = "jsonl"
                    dst_config: dict = {
                        "jsonl_dir": (
                            cc_row.collector_jsonl_dir
                            or "/var/log/centralops/collectors"
                        ),
                    }
                elif _syslog_format == "rfc5424":
                    dst_kind = "syslog_rfc5424"
                    dst_config = {
                        "host": cc_row.wazuh_syslog_host,
                        "port": int(cc_row.wazuh_syslog_port or 514),
                        "use_tls": bool(cc_row.wazuh_syslog_use_tls),
                        "ca_bundle": cc_row.wazuh_ca_bundle,
                    }
                else:
                    # rfc3164 — default Wazuh (e fallback para modo "both",
                    # que uma etapa futura transformará em duas rotas separadas).
                    dst_kind = "syslog_rfc3164"
                    dst_config = {
                        "host": cc_row.wazuh_syslog_host,
                        "port": int(cc_row.wazuh_syslog_port or 514),
                        "use_tls": bool(cc_row.wazuh_syslog_use_tls),
                        "ca_bundle": cc_row.wazuh_ca_bundle,
                    }

                dst_delivery: dict = {}
                dst_conn.execute(
                    text(
                        """
                        INSERT INTO destinations (
                            id, name, kind, enabled, config, secret_ref,
                            delivery, config_version, organization_id,
                            created_at, updated_at
                        ) VALUES (
                            'wazuh-default', :name, :kind, :enabled,
                            :config, NULL, :delivery, :version, NULL,
                            :now, :now
                        )
                        """
                    ),
                    {
                        "name": "Wazuh (default)",
                        "kind": dst_kind,
                        "enabled": True,
                        "config": _json_dst.dumps(dst_config, separators=(",", ":")),
                        "delivery": _json_dst.dumps(dst_delivery, separators=(",", ":")),
                        "version": _compute_config_version(dst_config, dst_delivery),
                        "now": datetime.utcnow(),
                    },
                )

    # ── Vendor-neutro: SEM auto-catch-all hardcoded ───────────────
    # ANTES seedávamos uma rota ``{} → [wazuh-default]`` (prioridade mínima,
    # is_final, global) que FORÇAVA todo evento sem match para o Wazuh. Removido:
    # nem toda empresa usa Wazuh, e um SDPP vendor-neutro não deve presumir um sink.
    # O catch-all agora é DECISÃO do operador: uma rota ``condition={}`` OU um
    # ``Destination`` marcado ``is_default`` (resolvido por ``_load_fallback_
    # destination_id``). Sem nenhum configurado, eventos sem rota vão à DLQ/
    # quarentena (zero perda, visível, replayável — ``BatchRouting.unrouted``).
    # DBs já provisionados MANTÊM a rota ``wazuh-default-catchall`` existente
    # (o seed era idempotente; não apagamos dados — só paramos de criar nova).

    # ── Migration: destination_dlq table ─────────
    # ``Base.metadata.create_all`` handles fresh DBs. For upgrades on existing
    # DBs that were created before this table was added, we create it here.
    # Dialect-branched: SQLite uses TEXT primary key + INTEGER FK (no UUID type);
    # Postgres uses VARCHAR. Both use CREATE TABLE IF NOT EXISTS.
    _dlq_tables = set(inspect(engine).get_table_names())
    if "destination_dlq" not in _dlq_tables:
        _dlq_is_sqlite = DATABASE_URL.startswith("sqlite")
        with engine.begin() as _dlq_conn:
            if _dlq_is_sqlite:
                _dlq_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS destination_dlq (
                            id TEXT PRIMARY KEY,
                            destination_id TEXT NOT NULL,
                            event_id TEXT NOT NULL,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            error_kind TEXT NOT NULL,
                            error_detail TEXT,
                            payload TEXT,
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            else:
                _dlq_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS destination_dlq (
                            id VARCHAR PRIMARY KEY,
                            destination_id VARCHAR NOT NULL,
                            event_id VARCHAR NOT NULL,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            error_kind VARCHAR NOT NULL,
                            error_detail TEXT,
                            payload TEXT,
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            _dlq_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_dlq_destination_id "
                    "ON destination_dlq (destination_id)"
                )
            )
            _dlq_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_dlq_event_id "
                    "ON destination_dlq (event_id)"
                )
            )
            _dlq_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_dlq_organization_id "
                    "ON destination_dlq (organization_id)"
                )
            )

    # E1 dedup guard: UNIQUE (destination_id, event_id).
    # Created in its OWN idempotent block so an existing destination_dlq table
    # (created before this constraint existed) is healed too. Tolerant: if the
    # table already holds duplicate rows the CREATE UNIQUE INDEX fails — we log
    # and continue (the table is dormant/forensic; an operator can dedup later).
    try:
        with engine.begin() as _dlq_uq_conn:
            _dlq_uq_conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_dest_dlq_dest_event "
                    "ON destination_dlq (destination_id, event_id)"
                )
            )
    except Exception:  # pragma: no cover - pre-existing duplicates only
        logger.warning(
            "destination_dlq: não foi possível criar índice único "
            "uq_dest_dlq_dest_event (linhas duplicadas pré-existentes?) — "
            "dedup de E1 fica a cargo da aplicação até a limpeza manual",
            exc_info=True,
        )

    # Índice composto (organization_id, created_at) p/
    # pruning/erase por tenant+tempo. Bloco próprio idempotente para curar
    # tabelas destination_dlq pré-existentes (criadas antes deste índice).
    if "destination_dlq" in set(inspect(engine).get_table_names()):
        with engine.begin() as _dlq_idx_conn:
            _dlq_idx_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_dlq_org_created "
                    "ON destination_dlq (organization_id, created_at)"
                )
            )

    # ── Backfill iris_customer_id legado → destination_customer_mappings ──
    # A tabela é criada por create_all (é model). Aqui só migramos os ids do IRIS
    # da coluna DEPRECADA Organization.iris_customer_id para o mapping genérico
    # (kind='iris'), tornando o mapping a fonte da verdade. Idempotente: só insere
    # orgs ainda sem mapping. inspect() ANTES do engine.begin() (gotcha StaticPool/
    # SQLite: inspect DENTRO do bloco faz ROLLBACK e descarta os inserts).
    _dcm_inspector = inspect(engine)
    _dcm_names = set(_dcm_inspector.get_table_names())
    if {"destination_customer_mappings", "organizations"} <= _dcm_names:
        _org_cols = {c["name"] for c in _dcm_inspector.get_columns("organizations")}
        if "iris_customer_id" in _org_cols:
            with engine.begin() as _dcm_conn:
                # Dois NOT EXISTS: (1) org ainda sem mapping iris; (2) o id
                # externo não está reivindicado por OUTRA org (uq kind+extid) —
                # evita violar a unicidade global no boot se ids legados
                # colidirem (lição do bug de boot recente: nunca deixe a
                # migração leve quebrar o boot).
                _dcm_rows = _dcm_conn.execute(
                    text(
                        "SELECT o.id AS org_id, o.iris_customer_id AS iris_id "
                        "FROM organizations o "
                        "WHERE o.iris_customer_id IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM destination_customer_mappings m "
                        "WHERE m.organization_id = o.id AND m.destination_kind = 'iris') "
                        "AND NOT EXISTS (SELECT 1 FROM destination_customer_mappings m2 "
                        "WHERE m2.destination_kind = 'iris' "
                        "AND m2.external_customer_id = CAST(o.iris_customer_id AS VARCHAR))"
                    )
                ).fetchall()
                _dcm_now = datetime.utcnow()
                for _dcm_row in _dcm_rows:
                    _dcm_conn.execute(
                        text(
                            "INSERT INTO destination_customer_mappings "
                            "(organization_id, destination_kind, external_customer_id, "
                            "created_at, updated_at) "
                            "VALUES (:org, 'iris', :ext, :now, :now)"
                        ),
                        {"org": _dcm_row.org_id, "ext": str(_dcm_row.iris_id), "now": _dcm_now},
                    )
                if _dcm_rows:
                    logger.info(
                        "backfill de %d iris_customer_id legado(s) → "
                        "destination_customer_mappings (kind='iris').",
                        len(_dcm_rows),
                    )

    # ── Ciclo de vida de credencial (destinations) ─────────
    # Colunas adicionadas ao modelo Destination para rastrear versão/rotação/
    # expiração/revogação de credencial. Idempotente via inspect().
    _s5_inspector = inspect(engine)
    _s5_table_names = set(_s5_inspector.get_table_names())
    if "destinations" in _s5_table_names:
        _s5_cols = {c["name"] for c in _s5_inspector.get_columns("destinations")}
        _s5_is_sqlite = engine.dialect.name == "sqlite"
        with engine.begin() as _s5_conn:
            if "secret_version" not in _s5_cols:
                _s5_conn.execute(
                    text(
                        "ALTER TABLE destinations "
                        "ADD COLUMN secret_version INTEGER NOT NULL DEFAULT 1"
                    )
                )
            for _s5_col in ("secret_created_at", "secret_rotated_at",
                             "secret_expires_at", "secret_revoked_at"):
                if _s5_col not in _s5_cols:
                    # TIMESTAMP portável (Postgres+SQLite); nullable.
                    _s5_conn.execute(
                        text(f"ALTER TABLE destinations ADD COLUMN {_s5_col} TIMESTAMP")
                    )

    # ── credential_access_log (nova tabela) ────────────────
    # Tabela append-only para auditoria de acesso a credencial.
    # Dialect-branched para compatibilidade SQLite vs Postgres.
    _s6_inspector = inspect(engine)
    _s6_table_names = set(_s6_inspector.get_table_names())
    if "credential_access_log" not in _s6_table_names:
        _s6_is_sqlite = engine.dialect.name == "sqlite"
        with engine.begin() as _s6_conn:
            if _s6_is_sqlite:
                _s6_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS credential_access_log (
                            id TEXT PRIMARY KEY,
                            destination_id TEXT NOT NULL,
                            actor TEXT,
                            action TEXT NOT NULL,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            detail TEXT,
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            else:
                _s6_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS credential_access_log (
                            id VARCHAR PRIMARY KEY,
                            destination_id VARCHAR NOT NULL,
                            actor VARCHAR,
                            action VARCHAR NOT NULL,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            detail TEXT,
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            _s6_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_cred_access_log_destination_id "
                    "ON credential_access_log (destination_id)"
                )
            )
            _s6_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_cred_access_log_created_at "
                    "ON credential_access_log (created_at)"
                )
            )
            _s6_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_cred_access_log_organization_id "
                    "ON credential_access_log (organization_id)"
                )
            )

    # ── destination_audit_log (nova tabela) ──────────
    # Trilha append-only do CRUD de destinos (create/update/delete) com
    # snapshot scrubado (sem secret em claro). ``Base.metadata.create_all``
    # cobre DBs virgens (testes); este bloco cobre DBs existentes.
    # Dialect-branched para compatibilidade SQLite vs Postgres, espelhando
    # exatamente o bloco credential_access_log.
    _da_inspector = inspect(engine)
    _da_table_names = set(_da_inspector.get_table_names())
    if "destination_audit_log" not in _da_table_names:
        _da_is_sqlite = engine.dialect.name == "sqlite"
        with engine.begin() as _da_conn:
            if _da_is_sqlite:
                _da_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS destination_audit_log (
                            id TEXT PRIMARY KEY,
                            destination_id TEXT NOT NULL,
                            action TEXT NOT NULL,
                            actor TEXT,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            snapshot TEXT NOT NULL DEFAULT '{}',
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            else:
                _da_conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS destination_audit_log (
                            id VARCHAR PRIMARY KEY,
                            destination_id VARCHAR NOT NULL,
                            action VARCHAR NOT NULL,
                            actor VARCHAR,
                            organization_id INTEGER REFERENCES organizations(id)
                                ON DELETE CASCADE,
                            snapshot TEXT NOT NULL DEFAULT '{}',
                            created_at TIMESTAMP NOT NULL
                        )
                        """
                    )
                )
            _da_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_audit_log_destination_id "
                    "ON destination_audit_log (destination_id)"
                )
            )
            _da_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_audit_log_created_at "
                    "ON destination_audit_log (created_at)"
                )
            )
            _da_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_destination_audit_log_organization_id "
                    "ON destination_audit_log (organization_id)"
                )
            )

    # ── data_residency em destinations ──────────────────────
    # Coluna nullable VARCHAR; NULL = sem restrição de residência (default).
    # Enforcement no engine de roteamento (conservador, sem perda silenciosa).
    _s7_inspector = inspect(engine)
    _s7_table_names = set(_s7_inspector.get_table_names())
    if "destinations" in _s7_table_names:
        _s7_cols = {c["name"] for c in _s7_inspector.get_columns("destinations")}
        if "data_residency" not in _s7_cols:
            with engine.begin() as _s7_conn:
                _s7_conn.execute(
                    text("ALTER TABLE destinations ADD COLUMN data_residency VARCHAR")
                )
                _s7_conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_destinations_data_residency "
                        "ON destinations (data_residency)"
                    )
                )
        # Vendor-neutro: destino de FALLBACK (catch-all). ``false`` é
        # aceito por Postgres E SQLite (>=3.23). Unicidade "1 default por org" é
        # garantida na API (destinations router) — evita índice parcial cross-DB.
        if "is_default" not in _s7_cols:
            with engine.begin() as _s7_conn:
                _s7_conn.execute(
                    text(
                        "ALTER TABLE destinations ADD COLUMN is_default "
                        "BOOLEAN NOT NULL DEFAULT false"
                    )
                )
                _s7_conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_destinations_is_default "
                        "ON destinations (organization_id, is_default)"
                    )
                )
