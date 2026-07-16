"""Centralised application settings loaded from environment / .env file."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Union

# Importação local apenas no validator para evitar custo de import no módulo global
# (ipaddress é stdlib, mas o import é feito inline para sinalizar intenção).

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_INSECURE_MASTER_KEYS = {
    "change-me-in-production-use-a-32-char-random-key",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Security ──────────────────────────────────────────────────────
    APP_MASTER_KEY: str
    APP_ENV: str = "production"
    # Versão do app — exportada como ``service.version`` no Resource OTel
    # (correlação/deploy markers no backend de ops). Setar via env no build/CI.
    APP_VERSION: str = "unknown"
    ENABLE_API_DOCS: Optional[bool] = None
    APP_COMPANY_NAME: str = "Sua Empresa"
    APP_COMPANY_PORTAL_NAME: str = "Portal de Login"

    # ── Database ──────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./data/sophos.db"

    @field_validator("DATABASE_URL")
    @classmethod
    def forbid_sqlite_in_production(cls, value: str, info) -> str:
        """Postgres obrigatório em produção (fail-fast no boot).

        SQLite é o default histórico, apropriado só para dev/test. Em produção é
        um anti-pattern: arquivo único, sem concorrência real de escrita
        (``SQLITE_BUSY`` sob workers Celery), e sem o caminho de HA/réplica que
        produção exige (RDS multi-AZ + PgBouncer + réplica). Subir com
        ``APP_ENV=production`` + ``DATABASE_URL`` SQLite é um erro de configuração:
        falhamos aqui, alto e claro, em vez de degradar silenciosamente em runtime.

        ``APP_ENV`` já chega normalizado (lowercase) em ``info.data`` porque
        ``normalize_app_env`` roda antes — o campo ``APP_ENV`` (linha 29) é
        declarado ANTES de ``DATABASE_URL`` e validators Pydantic v2 rodam na
        ordem de declaração dos CAMPOS. Mesmo invariante de
        ``enforce_secure_cookie_in_production``.
        """
        app_env = info.data.get("APP_ENV", "production")
        if app_env == "production" and value.strip().lower().startswith("sqlite"):
            raise ValueError(
                "DATABASE_URL não pode usar SQLite em produção. Configure um "
                "Postgres (ex.: postgresql+psycopg://user:pass@host:5432/centralops) "
                "ou use APP_ENV != production para dev/test."
            )
        return value

    # ── Redis (Threat Intel cache + blacklist) ────────────────────────
    REDIS_URL: Optional[str] = None  # ex: redis://redis:6379/0; vazio = fallback in-memory
    THREAT_INTEL_QUERY_RETENTION_DAYS: int = 30

    # ── CORS ──────────────────────────────────────────────────────────
    CORS_ORIGINS: Union[List[str], str] = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Union[str, List[str]]) -> List[str]:
        if isinstance(value, str):
            return [o.strip() for o in value.split(",") if o.strip()]
        return value

    # ── App session / authentication ─────────────────────────────────
    SESSION_COOKIE_NAME: str = "sophos_session"
    SESSION_TTL_HOURS: int = 12
    SESSION_SECURE_COOKIE: bool = False
    SESSION_SAMESITE: str = "lax"
    AUTH_FAILURE_LIMIT: int = 5
    AUTH_FAILURE_WINDOW_SECONDS: int = 300
    AUTH_LOCKOUT_SECONDS: int = 300
    AUDIT_LOG_RETENTION_DAYS: int = 90
    OUTBOUND_URL_ALLOWED_HOSTS: str = ""
    OUTBOUND_URL_ALLOWED_CIDRS: str = ""

    # CIDR allowlist de proxies confiáveis para X-Forwarded-For.
    # Em prod com ALB privado: ["10.0.0.0/8"]. Em dev/local: vazio (ignora XFF).
    # JSON-encoded via env var: '["10.0.0.0/8"]' ou string CSV "10.0.0.0/8,172.16.0.0/12".
    TRUSTED_PROXIES_CIDRS: List[str] = []

    @field_validator("TRUSTED_PROXIES_CIDRS", mode="before")
    @classmethod
    def parse_trusted_proxies(cls, value: Any) -> Any:
        """Aceita JSON array ou string CSV de CIDRs."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            # Tenta JSON primeiro
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
            # Fallback: CSV
            return [c.strip() for c in stripped.split(",") if c.strip()]
        return value

    # ── Integration creation limits ──────────────────────────────────
    # Limite máximo de integrações ativas por organização.
    # POST /integrations retorna 400 se org já atingiu este limite.
    MAX_INTEGRATIONS_PER_ORG: int = 100

    # ── Sophos global rate-limits (per tenant) ────────────────────────
    RATE_LIMIT_PER_SECOND: int = 10
    RATE_LIMIT_PER_MINUTE: int = 100
    RATE_LIMIT_PER_HOUR: int = 1000
    RATE_LIMIT_PER_DAY: int = 50000

    # ── XDR Query specific limits ─────────────────────────────────────
    XDR_QUERY_MAX_RUNS_PER_MINUTE: int = 10
    XDR_QUERY_MAX_RUNS_PER_DAY: int = 500

    # ── Concurrency / polling ─────────────────────────────────────────
    MAX_CONCURRENT_CLIENTS: int = 5
    QUERY_POLL_INTERVAL: int = 3  # seconds between status polls
    QUERY_POLL_TIMEOUT: int = 120  # total seconds before timeout
    SEARCH_HISTORY_CSV_RETENTION_DAYS: int = 7

    # ── QueryService federada ─────────────────────────────────────────
    # Teto de fontes por job (sanity do fan-out; um job não vira DoS de worker).
    QUERY_MAX_INTEGRATIONS_PER_JOB: int = 25
    # Cap de bytes do result_json POR fonte.
    # Resultado acima disto é truncado com flag partial — evita inflar o DB.
    QUERY_MAX_RESULT_BYTES: int = 5_000_000
    # Limite default de linhas por fonte quando o dialeto não traz um (passthrough).
    QUERY_DEFAULT_ROW_LIMIT: int = 1000
    # Teto DURO de linhas por query ao lake — anti-OOM: leitura
    # incremental para de ler ao atingir; nunca carrega o parquet/bucket inteiro.
    QUERY_LAKE_MAX_ROWS: int = 10000
    # Teto de bytes POR objeto S3 (anti-OOM): objeto maior é PULADO (logado), e o
    # gzip é descomprimido com este limite (anti gzip-bomb). Um lote de sink é pequeno;
    # 64 MiB é folgado — objeto acima disso é suspeito.
    QUERY_LAKE_MAX_OBJECT_BYTES: int = 64 * 1024 * 1024
    # Teto de objetos listados por partição de data (anti-OOM do plano de METADADOS):
    # a listagem é stream (gerador) e para neste teto — uma partição patológica
    # (sink em runaway) não materializa milhões de keys na memória.
    QUERY_LAKE_MAX_KEYS_PER_PREFIX: int = 100_000
    # ── Quota / detecção ──────────────────────────────────────────────
    # Submissões de query ao vivo por org por minuto (token-bucket Redis; 0 = sem
    # limite). Um tenant noisy não monopoliza os workers de query.
    QUERY_RATE_LIMIT_PER_MIN: int = 60
    # Severidade OCSF default da Detection de scheduled query — NÃO mais fixa em
    # Critical(5); high(4) por default (configurável). 0..6/99 (ver SEVERITY_ID).
    QUERY_DETECTION_DEFAULT_SEVERITY_ID: int = 4
    # Janela de supressão (s): match repetido do mesmo schedule dentro dela só
    # BUMPA count em vez de criar novo alerta (anti-spam).
    QUERY_DETECTION_SUPPRESSION_SECONDS: int = 3600
    # Teto de regras de correlação por org — toda finalização de job avalia TODAS
    # as regras habilitadas da org (O(regras×eventos)); o cap evita fan-out ilimitado
    # no worker (rejeita criação acima disto + limita a avaliação).
    CORRELATION_MAX_RULES_PER_ORG: int = 200
    # ── Async worker-releasing ────────────────────────────────────────
    # Atraso (s) antes do 1º poll após submeter um run async (vendor leva tempo).
    QUERY_POLL_INITIAL_DELAY: int = 5
    # Atraso (s) entre polls subsequentes (o poll-task re-enfileira com countdown).
    QUERY_POLL_REENQUEUE_DELAY: int = 10
    # Cap de ciclos de poll por job — evita poll infinito se o vendor nunca
    # finaliza. ~180×10s = 30min (cobre a janela do Data Lake). Estourou ⇒ failed.
    QUERY_POLL_MAX_CYCLES: int = 180

    # ── Collector subsystem (Celery + Redis + Wazuh target) ──────────
    # Broker/backend usam DBs separados do cache geral (REDIS_URL = db 0).
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None
    # RedBeat usa o mesmo Redis do broker por padrão (CELERY_BROKER_URL).
    # Sobrescreva com REDBEAT_REDIS_URL para isolar namespace em Redis separado.
    REDBEAT_REDIS_URL: Optional[str] = None
    # HA do scheduler. Com 2+ réplicas de beat (hot-standby), só
    # o detentor do lock distribuído dispara; os demais assumem em segundos no
    # failover. Timeout do lock: refrescado a cada beat_sync_every (5s), então
    # 60s dá margem confortável sem permitir disparo duplo por slow-GC.
    REDBEAT_LOCK_TIMEOUT: int = 60

    # ── data-plane durável (control/data-plane split) ──
    # Backend de TRANSPORTE de evento (data-plane), separado do control-plane
    # (Celery+Redis: scheduling, OAuth, breaker, rate-limit, observability — que
    # PERMANECE no Celery+Redis). Valores:
    #   "celery" — legado: o lote viaja como payload de task no broker Redis
    #              (dev/test sem broker; é o DEFAULT só por ergonomia de teste).
    #   "kafka"  — durável: o lote é produzido num tópico Kafka/Redpanda e um
    #              consumer dedicado (role ``dispatcher``) despacha. É o caminho de
    #              DEPLOY — o compose e o Helm setam EVENT_DATAPLANE=kafka.
    EVENT_DATAPLANE: str = "celery"
    # Bootstrap do broker (Redpanda self-host, MSK, Redpanda Cloud, Confluent…).
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    # Prefixo de tópico — isola ambientes que compartilham um cluster.
    KAFKA_TOPIC_PREFIX: str = "centralops"
    # Tópico de entrega (sufixado ao prefixo): 1 tópico, N partições, key=dest_id
    # → mesma destinação sempre na mesma partição (ordem + HoL-blocking limitado,
    # espelha o DISPATCH_DEST_SHARDS do caminho Celery). Evita o anti-pattern de
    # "1 tópico por destino" (cardinalidade explode com N tenants × M destinos).
    KAFKA_DELIVER_TOPIC: str = "deliver"
    KAFKA_DELIVER_PARTITIONS: int = 8
    KAFKA_DELIVER_REPLICATION: int = 1  # 1 p/ self-host single-node; 3 em prod HA
    # Consumer group do role dispatcher (KEDA escala por lag deste group).
    KAFKA_CONSUMER_GROUP: str = "centralops-dispatcher"
    # Teto de bytes de UM registro produzido. DEVE ser == ao limite do broker
    # (`max.message.bytes` no Kafka / `kafka_batch_max_bytes` no Redpanda) — é a
    # ÚNICA fonte de verdade do tamanho. Se o cliente achar que pode mandar mais que
    # o broker aceita, ele não barra local e o broker rejeita com
    # MessageSizeTooLargeError (era o bug: cliente 8MiB vs broker 1MiB). Default 1MiB
    # = o default do broker (Kafka/Redpanda). O produtor faz split por tamanho p/
    # nunca emitir um registro acima disto e comprime (KAFKA_COMPRESSION_TYPE) p/
    # encolher o fio; um único evento que ainda estoure vai p/ DLQ (não-retryable).
    # Para mensagens maiores, suba ESTE valor E o limite do broker JUNTOS.
    KAFKA_MAX_REQUEST_BYTES: int = 1024 * 1024  # 1 MiB (== broker max.message.bytes)
    # Compressão do produtor — encolhe o tamanho no fio (evento JSON comprime ~5-10×),
    # a maior alavanca contra MessageSizeTooLarge. "gzip" é zero-dependência (zlib da
    # stdlib); "lz4"/"snappy"/"zstd" são mais rápidos SE a lib estiver instalada;
    # "none" desliga. Aplicado ao data-plane E aos destinos Kafka.
    KAFKA_COMPRESSION_TYPE: str = "gzip"
    # Segurança (managed brokers). Vazio/PLAINTEXT = self-host sem auth.
    KAFKA_SECURITY_PROTOCOL: str = "PLAINTEXT"  # PLAINTEXT|SASL_PLAINTEXT|SASL_SSL
    KAFKA_SASL_MECHANISM: Optional[str] = None  # PLAIN|SCRAM-SHA-256|SCRAM-SHA-512
    KAFKA_SASL_USERNAME: Optional[str] = None
    KAFKA_SASL_PASSWORD: Optional[str] = None
    # ── Resiliência do data-plane (hardening pós-review) ──────
    # Consume side: nº máx. de retentativas TRANSITÓRIAS por registro antes de
    # mandar o lote à DLQ e commitar (espelha o max_retries=10 da lane Celery).
    # Sem isto, um destino com falha transitória permanente prende a partição
    # (head-of-line blocking) em loop infinito de seek.
    KAFKA_MAX_DELIVERY_ATTEMPTS: int = 10
    # Producer: timeouts EXPLÍCITOS p/ falhar rápido quando o broker está
    # down/lento (senão aiokafka usa ~40s default e o produce bloqueia o
    # hot-path de coleta por sub-lote). KAFKA_PRODUCE_WAIT_S deve ser > o
    # request_timeout p/ o wait_for interno disparar primeiro.
    KAFKA_REQUEST_TIMEOUT_MS: int = 10000
    KAFKA_METADATA_MAX_AGE_MS: int = 30000
    KAFKA_PRODUCE_WAIT_S: float = 12.0
    # Consumer: folga de max_poll_interval acima do pior caso de dispatch (1 msg
    # = 1 sub-lote, multi-chunk). Evita rebalance espúrio por "consumer morto"
    # durante um dispatch lento. Default 15min cobre max_elapsed_ms × chunks.
    KAFKA_MAX_POLL_INTERVAL_MS: int = 900000
    # Intervalo do emissor de lag em BACKGROUND (independe de chegada de msg —
    # senão o gauge congela quando a partição zera, o pior momento p/ o SRE).
    KAFKA_LAG_REFRESH_SECONDS: int = 15

    @field_validator("EVENT_DATAPLANE")
    @classmethod
    def _validate_event_dataplane(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"celery", "kafka"}:
            raise ValueError("EVENT_DATAPLANE must be 'celery' or 'kafka'")
        return normalized

    @model_validator(mode="after")
    def _validate_kafka_sasl(self) -> "Settings":
        """SASL_* exige um mecanismo SASL — senão o aiokafka tenta conectar sem
        mecanismo e o ``.start()`` do producer/consumer falha no broker gerenciado.
        Falha-rápido no boot em vez de só estourar no connect."""
        proto = (self.KAFKA_SECURITY_PROTOCOL or "").upper()
        if proto.startswith("SASL_") and not (self.KAFKA_SASL_MECHANISM or "").strip():
            raise ValueError(
                "KAFKA_SECURITY_PROTOCOL=%s exige KAFKA_SASL_MECHANISM "
                "(PLAIN|SCRAM-SHA-256|SCRAM-SHA-512)" % self.KAFKA_SECURITY_PROTOCOL
            )
        return self

    # Wazuh destino — via Syslog TCP/TLS (RFC 5424/6587) ou JSONL fallback
    WAZUH_SYSLOG_HOST: Optional[str] = None
    WAZUH_SYSLOG_PORT: int = 6514
    WAZUH_CA_BUNDLE: Optional[str] = None
    WAZUH_DISPATCH_MODE: str = "syslog"  # "syslog" | "jsonl" | "both"
    COLLECTOR_JSONL_DIR: str = "/var/log/centralops/collectors"

    # ── Multi-destino — GA, sempre ativo ──────────────────────────────
    # A saída desacoplada (destinos + fan-out) é o comportamento PADRÃO e
    # incondicional. A lane dedicada do Wazuh foi removida: ``wazuh-default`` é
    # um destino como qualquer outro e
    # entrega via a lane uniforme ``dispatch_to_destination``. O catch-all é
    # vendor-neutro (``Destination.is_default``); sem fallback, lotes sem rota
    # vão para a DLQ como ``unrouted``. NÃO há mais flag de dormência:
    # ``MULTI_DESTINATION_ENABLED`` foi removida (app pré-lançamento, sem
    # necessidade de back-compat). O roteamento por regra é o modelo ÚNICO
    # sempre-ativo (``ROUTING_ENABLED`` também removida).

    # ── backpressure / load-shedding ──────────────────────────────────
    # OFF (default): nenhum descarte; um destino backed-up conta com a
    # durabilidade do broker + circuit breaker (que abre e fast-fail-a um sink
    # morto, drenando a fila). ON: destinos com ``backpressure="drop_newest"`` e
    # teto de profundidade (``delivery.queue_ceiling`` ou o teto global abaixo)
    # têm lotes NOVOS descartados quando a shard queue passa do teto — válvula
    # de segurança contra crescimento ilimitado do Redis (OOM multi-tenant).
    BACKPRESSURE_E6_ENABLED: bool = False
    # Teto global de profundidade por shard queue (0 = desabilitado). Usado como
    # default para destinos drop_newest que não definem ``queue_ceiling`` próprio.
    DISPATCH_QUEUE_CEILING: int = 0

    # ── motor de roteamento (GA, sempre ativo) ────────────────────────
    # O roteamento por regra (tabela ``routes``) é o ÚNICO modelo de despacho —
    # sem flag de liga/desliga (app pré-lançamento, sem back-compat; padrão
    # Cribl/Vector: roteamento É o pipeline). Cada evento é avaliado contra as
    # rotas (first-match, ``is_final`` stop vs clone+continue fan-out, ``drop``),
    # com um catch-all garantindo ZERO perda silenciosa: evento sem match →
    # ``wazuh-default`` (byte-idêntico ao histórico). Sem rotas extras → tudo cai
    # no catch-all (comportamento idêntico ao legado). Criar um destino gera
    # automaticamente uma rota ``{} → [destino]`` editável (broadcast por default,
    # visível na UI). NÃO há mais ``ROUTING_ENABLED``.

    # ── lineage por evento (event→destination) ────────────────────────
    # OFF (default): nenhum registro positivo de entrega por (evento, destino).
    # ON: ao despachar com sucesso para um destino (fan-out ou routing),
    # grava em Redis com TTL configurável (lineage RECENTE, NÃO arquivo de
    # compliance permanente — TTL default 7 dias). Gated apenas em
    # LINEAGE_ENABLED (multi-destino é sempre ativo agora). Fail-open: Redis
    # down não derruba entrega.
    LINEAGE_ENABLED: bool = False
    # TTL em segundos das entradas de lineage no Redis (default 7 dias).
    # Aumentar aumenta retenção mas consome mais memória Redis.
    LINEAGE_TTL_S: int = 7 * 24 * 3600  # 7 dias

    # ── redação de PII por rota (governança LGPD) ──
    # Kill-switch global da feature. ON → rotas com pii_redaction mascaram/
    # pseudonimizam/removem campos antes do destino daquela rota (mesma origem
    # íntegra no lago, mascarada no SIEM). OFF (default): no estado DEFAULT (sem
    # rota com spec) é byte-idêntico. Mas se uma rota TEM spec e a flag está OFF,
    # é FAIL-CLOSED — _compile_route_row levanta → _load_routes_for_org cai p/
    # wazuh-default interno, NUNCA entrega cleartext ao destino externo (a flag
    # nunca vira um caminho de vazamento). FAIL-CLOSED também na escrita: spec
    # ruim → 422 no CRUD.
    PII_REDACTION_ENABLED: bool = False

    # ── metering de custo/volume (medição, sem alavanca) ──
    # OFF (default) + byte-idêntico: quando False, os hooks de metering são no-op
    # IMEDIATO (zero serialização extra no hot path). ON: mede eventos/bytes
    # (lógicos, pré-compressão) que ENTRAM (por source/org) vs SAEM (por destino/org)
    # e expõe em GET /collectors/cost-summary + no catálogo OTel. NENHUMA alavanca de
    # redução (drop/sample/trim) é ativada aqui — só medição; logo nenhum evento
    # é descartado e a pré-condição Route.protect_detection (que gateia o
    # sampling) não se aplica. Custo em US$ é EE (seam ee_hooks.cost_pricer); o core
    # Community expõe só volume + razão adimensional. Toda futura flag REDUCTION_* será
    # no-op enquanto esta estiver False.
    COST_METERING_ENABLED: bool = False
    # Sampling estatístico de redução (consistent-hash por
    # event_id). Default OFF: sample_percent nas rotas é no-op até ligar. Só reduz
    # se COST_METERING_ENABLED também estiver on (não se reduz sem medir).
    REDUCTION_SAMPLE_ENABLED: bool = False
    # Kill-switch GLOBAL do fail-safe de detecção. True
    # (default) = rotas com Route.protect_detection=True NUNCA são amostradas/
    # agregadas, mesmo com sampling/aggregate on. False desliga a proteção
    # globalmente (perigoso; use só com replay de detecção validado). A proteção
    # real é por-rota (a coluna); esta flag é o override de emergência.
    REDUCTION_SAMPLE_PROTECT_DETECTION: bool = True
    # Trimming lossless: quando o raw_reduction (limites de tamanho
    # por-campo) dispara, mede os bytes evitados como bytes_saved{reason=trim}. Default
    # OFF: o trimming por max_bytes/max_items CONTINUA acontecendo (back-compat); esta
    # flag só liga a CONTABILIZAÇÃO da economia (não muda o que é entregue). Só reduz
    # se COST_METERING_ENABLED também estiver on.
    REDUCTION_TRIM_ENABLED: bool = False
    # Suppression durável por assinatura (rate-limit Number-to-Allow
    # via Redis INCR+EXPIRE). Default OFF: rotas com suppress_key/allow são no-op até
    # ligar. Só reduz se COST_METERING_ENABLED também estiver on. Fail-OPEN: erro de
    # Redis entrega o evento (supressão é otimização, não correção).
    REDUCTION_SUPPRESS_ENABLED: bool = False
    # Agregação/rollup log→métrica por destino (a mais coarse; opt-in
    # por-destino via delivery.aggregate.group_by). Default OFF: flush_ms/aggregate são
    # no-op. Só reduz se COST_METERING_ENABLED on. FAIL-OPEN anti-OOM: cardinalidade de
    # grupos acima do teto passa o lote intacto. Nunca agrega
    # detecção (é opt-in por-destino — quem alimenta detecção não recebe aggregate).
    REDUCTION_AGGREGATE_ENABLED: bool = False

    # ── OTel tracing distribuído (export vendor-neutro) ──
    # OFF (default) + byte-idêntico. ON: instrumenta collect→normalize→dispatch→
    # por-destino com OTel, propagando ``traceparent`` pelo boundary Celery, e
    # exporta via OTLP — o time interno conecta QUALQUER backend (Tempo/Datadog/
    # Jaeger/...). Requer os pacotes opentelemetry-* instalados; se ausentes, o
    # código degrada para no-op (sem tracing, sem quebrar nada).
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "centralops-collector"
    # Endpoint OTLP (ex.: http://otel-collector:4318/v1/traces). Vazio = usa os
    # envs padrão do SDK (OTEL_EXPORTER_OTLP_ENDPOINT).
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    # Superfície B (ops): MÉTRICAS via OTLP-PUSH. Cada filho
    # prefork EMPURRA suas séries de entrega a cada intervalo (default 60s). Isso
    # evita a complexidade do PROMETHEUS_MULTIPROC_DIR e atravessa NAT/egress-only
    # (híbrido SaaS/self-hosted). Vendor-neutro: o mesmo endpoint OTLP do tracing.
    OTEL_METRIC_EXPORT_INTERVAL_MS: int = 60000
    # Janela (minutos) da média móvel de taxa exibida na UI (EPS de fontes/destinos
    # e *_per_min de rotas em /flow e /destinations — Superfície A, store Redis
    # nativo). 5 min = mais "tempo real" (reage rápido a bursts/silêncio) ao custo
    # de mais ruído em volume baixo; 60 min = mais suave porém laggy. Os buckets são
    # por-minuto (TTL 3h), então a janela só muda a LEITURA, não o armazenamento.
    # NÃO afeta o endpoint de saúde de rota (matched_1h, fixo em 1h por contrato).
    OBS_RATE_WINDOW_MINUTES: int = 5
    # Head-sampling de TRACES (0..1). Default 1.0 (mantém tudo) deixando o
    # tail-sampling no Collector decidir (reter 100% de erro/DLQ, amostrar
    # sucesso). Baixe (ex.: 0.1) para reduzir egress na origem. ParentBased ⇒
    # decisão consistente ao longo do trace inteiro (boundary Celery).
    OTEL_TRACES_SAMPLER_RATIO: float = 1.0
    # Sinal de LOGS OTel (toggle SEPARADO de OTEL_ENABLED — volume alto). ON ⇒
    # faz a ponte do logging Python p/ LogRecord OTel correlacionado por
    # trace_id e exporta via OTLP. Requer OTEL_ENABLED=true também.
    OTEL_LOGS_ENABLED: bool = False

    # ── Quarentena — teto do raw_payload ARMAZENADO ───────────────────
    # Limite de bytes do raw_payload gravado em quarantine_events. NÃO é o
    # caminho de saída ao Wazuh (esse não tem teto no CentralOps; o limite
    # real downstream é o OS_MAXSTR=64KiB do Wazuh). Postgres Text/TOAST
    # suporta ~1GB, então este número é higiene de storage/UI, não restrição
    # de DB. Acima do limite, o writer poda campos grandes mantendo JSON
    # VÁLIDO + escalares de topo (inspeção e reprocesso parcial preservados).
    # Default subido de 64 KiB (histórico hardcoded) para 256 KiB.
    QUARANTINE_RAW_MAX_BYTES: int = 256 * 1024  # 256 KiB

    @field_validator("QUARANTINE_RAW_MAX_BYTES")
    @classmethod
    def _validate_quarantine_max_bytes(cls, value: int) -> int:
        # Piso defensivo: abaixo de 4 KiB nem os escalares de topo cabem.
        if value < 4 * 1024:
            raise ValueError("QUARANTINE_RAW_MAX_BYTES deve ser >= 4096 (4 KiB)")
        return value

    # ── Validação OCSF — forward-looking, default-OFF (à la OTEL_ENABLED) ──
    # OFF ⇒ o hook em pipeline.py é um no-op de custo zero (fail-open, comportamento
    # atual). ON ⇒ o structural gate (tier-1, ~µs, puro-Python) valida class_uid/
    # category/severity/type_uid/activity_id contra o manifest OCSF vendorado, emite
    # métricas de conformidade e ETIQUETA o envelope. A AÇÃO em evento inválido é
    # decidida pela política por-org, com fallback no default global abaixo.
    OCSF_VALIDATION_ENABLED: bool = False
    # Default GLOBAL de enforcement quando a org não tem linha em
    # ``organization_ocsf_policy``: tag_and_pass | quarantine | fail_closed.
    # Default SEGURO = tag_and_pass (nada é descartado). Vira ``quarantine`` na GA;
    # orgs existentes são backfilladas em tag_and_pass, então o flip não
    # as afeta. Validado no boot contra ocsf_policy.ENFORCEMENT_MODES (um teste garante).
    OCSF_DEFAULT_ENFORCEMENT: str = "tag_and_pass"
    # Teto de ESCRITAS de validate-quarentena por ciclo de coleta: sob um
    # mapping regredido (100% inválido) evita amplificação de escrita no DB. Acima do
    # teto os inválidos ainda NÃO são despachados (honra o modo quarantine/fail_closed)
    # e o counter de métrica segue SEM amostragem (fidelidade), mas a escrita é pulada.
    OCSF_QUARANTINE_MAX_PER_RUN: int = 100
    # Gate de commit: 422 em ``create_version`` quando o mapping emite OCSF
    # inválido. OFF = permissivo (só grava ``ocsf_validation_stats``, não bloqueia).
    OCSF_MAPPING_GATE_ENABLED: bool = False
    # Versão-alvo do schema OCSF vendorado (global; per-org fica para depois).
    OCSF_VALIDATION_VERSION: str = "1.8.0"

    @field_validator("OCSF_DEFAULT_ENFORCEMENT")
    @classmethod
    def _validate_ocsf_enforcement(cls, value: str) -> str:
        allowed = {"tag_and_pass", "quarantine", "fail_closed"}
        if value not in allowed:
            raise ValueError(
                f"OCSF_DEFAULT_ENFORCEMENT inválido: {value!r} (use um de {sorted(allowed)})"
            )
        return value

    @field_validator("OTEL_TRACES_SAMPLER_RATIO")
    @classmethod
    def _validate_otel_sampler_ratio(cls, value: float) -> float:
        # Fail-fast no boot em vez do clamp silencioso do tracing — config errada
        # (ex.: 2.0, -1) não deve virar 1.0 sem o operador saber.
        if not (0.0 <= value <= 1.0):
            raise ValueError("OTEL_TRACES_SAMPLER_RATIO deve estar em [0.0, 1.0]")
        return value

    # Concurrency por domínio do vendor
    # JSON-encoded via env var: '{"sophos":20,"microsoft_defender":30,"ninjaone":15}'
    DOMAIN_CONCURRENCY_LIMITS: Dict[str, int] = {
        "sophos": 20,
        "microsoft_defender": 30,
        "ninjaone": 15,
    }

    # Rate budget distribuído por (tenant, vendor) — valores derivam dos
    # limites reais que cada vendor documenta + margem de segurança.
    # Ajustes:
    # - sophos: docs mencionam ~500 req/min por client OAuth. 400/min
    #   mantém 20% de buffer; per_second=10 evita burst curto.
    # - microsoft_defender (Graph): throttling global ~15k req/10s por app
    #   + sub-limit por tenant. 600/min é seguro.
    # - ninjaone: docs não publicam limite firme; 300/min é padrão prático.
    RATE_LIMITS_BY_VENDOR: Dict[str, Dict[str, int]] = {
        "sophos": {"per_second": 10, "per_minute": 400, "per_hour": 20000},
        "microsoft_defender": {"per_second": 15, "per_minute": 600, "per_hour": 15000},
        "ninjaone": {"per_second": 5, "per_minute": 300, "per_hour": 5000},
    }

    # Idempotência: TTL da chave SET NX em dias
    DEDUPE_TTL_DAYS: int = 7

    # Coleta: flush de lote para o dispatch
    COLLECTOR_BATCH_SIZE: int = 200
    COLLECTOR_BATCH_FLUSH_SECONDS: int = 5

    # Detecção de drift: fração de eventos amostrados para
    # registrar campos desconhecidos. 0.1 = 1 em cada 10. 0 desliga.
    DRIFT_SAMPLE_RATE: float = 0.1
    # Janela de aprendizado (auto-discovery à la Cribl/Axoflow): os primeiros N
    # eventos de uma combinação NOVA (org, vendor, event_type) vista pelo processo
    # são amostrados a 100%, independentemente de DRIFT_SAMPLE_RATE. Faz um syslog
    # recém-apontado (ex.: FortiGate via edge-collector) aparecer IMEDIATAMENTE no
    # Drift Explorer com o schema completo, em vez de gotejar 1 campo a cada 10
    # eventos. Depois da janela, cai para a amostragem estacionária. 0 desliga o boost.
    DRIFT_LEARNING_EVENTS: int = 200

    @field_validator("DOMAIN_CONCURRENCY_LIMITS", "RATE_LIMITS_BY_VENDOR", mode="before")
    @classmethod
    def _parse_json_mapping(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON mapping: {exc}") from exc
        return value

    @field_validator("WAZUH_DISPATCH_MODE")
    @classmethod
    def _validate_dispatch_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"syslog", "jsonl", "both"}:
            raise ValueError("WAZUH_DISPATCH_MODE must be 'syslog', 'jsonl' or 'both'")
        return normalized

    @field_validator("DFIR_IRIS_URL", mode="before")
    @classmethod
    def _validate_iris_url(cls, value):
        """Fail-fast no boot se DFIR_IRIS_URL for malformada (era
        um 503 em runtime). Vazio/None = integração desabilitada (ok)."""
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        from urllib.parse import urlparse

        parsed = urlparse(s)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "DFIR_IRIS_URL inválida: precisa de scheme http(s) e host "
                f"(ex.: https://iris.exemplo.com) — recebido: {s!r}"
            )
        return s.rstrip("/")

    # ── Secrets backend ───────────────────────────────────────────────
    # Backend de secrets ativo. Valores aceitos:
    #   "local_fernet"         — default histórico (AES-128 + PBKDF2 local).
    #   "kms_wrapped_fernet"   — envelope encryption via KMS (recomendado prod).
    # Trocar para "kms_wrapped_fernet" requer configurar KMS_LOCAL_STUB_MASTER_KEY_PATH
    # (dev/test) ou uma implementação real de KmsBackend (prod).
    SECRETS_BACKEND: str = "local_fernet"

    # Caminho do arquivo de master key para LocalKmsStubBackend (dev/test apenas).
    # Em produção, esse arquivo não existe — o KMS real (AWS, Vault) fornece a chave.
    KMS_LOCAL_STUB_MASTER_KEY_PATH: str = "/var/lib/centralops/kms-master.key"

    # TTL em segundos do cache in-memory de DEKs desempacotadas.
    # Cache reduz round-trips ao KMS em listagens batch (ex: listar integrações).
    # Valor 0 desabilita o cache completamente.
    KMS_DEK_CACHE_TTL_SECONDS: int = 60

    # Provedor de KMS usado quando SECRETS_BACKEND="kms_wrapped_fernet".
    # Separa o MECANISMO (envelope encryption) do PROVEDOR — novos KMS plugam
    # aqui sem novo SECRETS_BACKEND. Valores:
    #   "local_stub"    — dev/test, master key em arquivo (NÃO usar em prod).
    #   "vault_transit" — HashiCorp Vault Transit (CE-compatível; recomendado prod).
    #   ("aws_kms"      — TODO: boto3 KMS::Encrypt/Decrypt.)
    KMS_PROVIDER: str = "local_stub"

    # ── HashiCorp Vault (Transit engine) — KMS_PROVIDER="vault_transit" ──
    # Transit é open-source: funciona em Vault Community Edition self-hosted.
    VAULT_ADDR: Optional[str] = None
    # Nome da chave Transit (deve existir: `vault write -f transit/keys/<name>`).
    VAULT_TRANSIT_KEY_NAME: str = "centralops"
    # Mount do engine Transit (default `transit`).
    VAULT_TRANSIT_MOUNT: str = "transit"
    # Auth: "token" (estático) ou "approle" (recomendado p/ serviços).
    VAULT_AUTH_METHOD: str = "token"
    VAULT_TOKEN: Optional[str] = None
    VAULT_ROLE_ID: Optional[str] = None
    VAULT_SECRET_ID: Optional[str] = None
    VAULT_APPROLE_MOUNT: str = "approle"
    # Namespace é Vault Enterprise (no-op em CE; forward-compat).
    VAULT_NAMESPACE: Optional[str] = None
    # Validar o cert TLS do Vault (False só p/ dev com self-signed).
    VAULT_VERIFY_TLS: bool = True
    # Timeout (connect+read, s) das chamadas ao Vault — bound do hot-path.
    VAULT_TIMEOUT_SECONDS: float = 5.0

    # ── Logging estruturado / Wazuh forwarding ────────────────────────
    # Habilitar handler de arquivo JSONL para consumo pelo agente Wazuh.
    LOGGING_WAZUH_JSONL_ENABLED: bool = False
    # Caminho do arquivo JSONL; rotation diária, retenção 7 dias.
    LOGGING_WAZUH_JSONL_PATH: str = "/var/log/centralops/app.jsonl"

    # ── Internal service-to-service API ─────────────────────────────
    # Shared secret consumed by /api/internal/* (TenantContext.bind() Level 0).
    # Empty disables the endpoints entirely (returns 503).
    CENTRALOPS_INTERNAL_API_KEY: Optional[str] = None

    # ── DFIR-IRIS integration (OPCIONAL) ──────────────────────────────
    # Provisionamento de customers no IRIS é uma integração de borda OPCIONAL
    # (não gateia a entrega de eventos). Vazio = desabilitado.
    DFIR_IRIS_URL: Optional[str] = None
    DFIR_IRIS_API_KEY: Optional[str] = None
    DFIR_IRIS_TLS_SKIP_VERIFY: bool = False

    # ── Sophos Partner Mode — sync policy ───────────────────────────
    # Cron expression for the periodic sync of all active Partner integrations.
    # Empty string disables the periodic sync (manual /sync-tenants still works).
    SOPHOS_PARTNER_SYNC_CRON: str = "0 4 * * *"  # 04:00 UTC daily

    # Auto-seed de tenant-admin no provisionamento partner.
    # OFF (default, fail-safe): materialize_child NÃO cria usuário (postura
    # histórica). ON: ao materializar um tenant APROVADO, semeia 1 admin-de-org
    # ESCOPADO (OrgRoleBinding role=admin/scope=org/inherit=self) como conta
    # PENDENTE (is_active=False, sem senha) — ativação via convite/set-password ou
    # SSO/SCIM JIT é fluxo separado. Idempotente (re-sync não duplica). Nunca
    # concede is_global; nunca seed silencioso (gated por esta flag + tenant aprovado).
    PARTNER_AUTO_SEED_TENANT_ADMIN: bool = False

    # O flag ENVELOPE_USE_IRIS_CUSTOMER_ID foi REMOVIDO. O
    # ``_centralops.customer_id`` do envelope agora é SEMPRE o ``Organization.id``
    # interno — a entrega de eventos não depende mais da identidade do IRIS. O
    # mapeamento Organization → customer id externo (IRIS/SOAR) vive em
    # ``destination_customer_mappings`` e é resolvido só na borda do connector.

    # ── Microsoft Entra ID — OIDC SSO ─────────────────────────────────
    # Login federado backend-driven (confidential client + PKCE). Quando
    # ENTRA_ENABLED=true E tenant/client/secret/redirect preenchidos, a tela
    # de login mostra "Entrar com Microsoft" e o fluxo Authorization Code roda
    # em /api/auth/sso/login → /api/auth/sso/callback. Single-tenant: o token
    # só é aceito se o claim ``tid`` == ENTRA_TENANT_ID.
    ENTRA_ENABLED: bool = False
    ENTRA_TENANT_ID: Optional[str] = None
    ENTRA_CLIENT_ID: Optional[str] = None
    ENTRA_CLIENT_SECRET: Optional[str] = None
    # URL absoluta de callback, idêntica à registrada no App Registration.
    # Ex: https://centralops.exemplo.com/api/auth/sso/callback
    ENTRA_REDIRECT_URI: Optional[str] = None
    # Authority base — troque apenas em clouds soberanas (US Gov / China).
    ENTRA_AUTHORITY: str = "https://login.microsoftonline.com"
    # Scopes OIDC (openid é obrigatório para receber id_token).
    ENTRA_SCOPES: str = "openid profile email"
    # Mapa App Role (claim ``roles``) → papel local. JSON via env:
    # '{"CentralOpsAdmin":"admin","CentralOpsEngineer":"engineer"}'.
    # Sem match, usa ENTRA_DEFAULT_ROLE. Múltiplos roles → o de maior nível.
    ENTRA_ROLE_MAP: Dict[str, str] = {}
    ENTRA_DEFAULT_ROLE: str = "viewer"
    # Concede escopo global (vê todas as orgs) às contas criadas via SSO —
    # caso típico do SOC interno (toda a equipe monitora todos os clientes).
    ENTRA_DEFAULT_IS_GLOBAL: bool = False
    # Just-in-time: cria a conta no 1º login. False = conta deve existir antes
    # (modo "provisioning governa" — SCIM/Graph-sync).
    ENTRA_JIT_PROVISIONING: bool = True
    # Allowlist opcional de domínios de e-mail (defense-in-depth). Vazio =
    # qualquer e-mail do tenant. CSV: "empresa.com,empresa.com.br".
    ENTRA_ALLOWED_EMAIL_DOMAINS: Union[List[str], str] = ""
    # Rótulo do botão de SSO (frontend lê via /api/auth/status).
    ENTRA_BUTTON_LABEL: str = "Entrar com Microsoft"
    # Caminho no frontend para onde redirecionar após login bem-sucedido.
    ENTRA_POST_LOGIN_REDIRECT: str = "/"

    # Cron de 5 campos para o sync periodico de usuarios do Entra via Graph.
    # Vazio = desabilitado (o beat nao registra a entry). Mudar o valor exige
    # restart do processo Beat para ter efeito (comportamento identico ao
    # SOPHOS_PARTNER_SYNC_CRON).
    # Exemplo: "0 */4 * * *" = a cada 4 horas.
    ENTRA_SYNC_CRON: str = ""

    @field_validator("ENTRA_ROLE_MAP", mode="before")
    @classmethod
    def _parse_entra_role_map(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid ENTRA_ROLE_MAP JSON: {exc}") from exc
        return value

    @field_validator("ENTRA_ALLOWED_EMAIL_DOMAINS", mode="before")
    @classmethod
    def _parse_entra_email_domains(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [d.strip().lower() for d in value.split(",") if d.strip()]
        return value

    # ── Debug ─────────────────────────────────────────────────────────
    DEBUG_REQUESTS: bool = False

    @field_validator("SESSION_SECURE_COOKIE", mode="before")
    @classmethod
    def empty_secure_cookie_means_unset(cls, value: object, info) -> object:
        """String vazia = "não configurado" (seguro em produção), nunca bool_parsing.

        O compose interpola ``SESSION_SECURE_COOKIE=${SESSION_SECURE_COOKIE:-...}``;
        um default vazio (ou operador exportando a var vazia) chegava aqui como ``''``
        e derrubava o BOOT com ``bool_parsing`` — foi o que deixou todos os collectors
        em crash-loop (jul/2026). Vazio vira o default seguro do ambiente: True em
        produção, False fora dela.
        """
        if isinstance(value, str) and not value.strip():
            return info.data.get("APP_ENV", "production") == "production"
        return value

    @field_validator("SESSION_SECURE_COOKIE")
    @classmethod
    def enforce_secure_cookie_in_production(cls, value: bool, info) -> bool:
        """Garante SESSION_SECURE_COOKIE=true em ambiente de produção.

        APP_ENV é normalizado para lowercase pelo validator normalize_app_env,
        que é declarado APÓS este validator na classe. Como validators Pydantic
        v2 rodam na ordem de declaração dos campos (não dos validators), e
        APP_ENV (linha 26) é declarado antes de SESSION_SECURE_COOKIE (linha 51),
        o valor normalizado já está disponível em info.data ao rodar este validator.
        """
        app_env = info.data.get("APP_ENV", "production")
        if app_env == "production" and value is False:
            raise ValueError(
                "SESSION_SECURE_COOKIE deve ser True em produção. "
                "Configure SESSION_SECURE_COOKIE=true no env ou use APP_ENV != production."
            )
        return value

    @field_validator("APP_MASTER_KEY")
    @classmethod
    def validate_master_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("APP_MASTER_KEY must be explicitly configured")
        if normalized in _INSECURE_MASTER_KEYS:
            raise ValueError("APP_MASTER_KEY is using an insecure placeholder value")
        if len(normalized) < 32:
            raise ValueError("APP_MASTER_KEY must contain at least 32 characters")
        return normalized

    @field_validator("APP_ENV")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("APP_ENV must not be empty")
        return normalized

    @property
    def api_docs_enabled(self) -> bool:
        if self.ENABLE_API_DOCS is not None:
            return self.ENABLE_API_DOCS
        return self.APP_ENV != "production"


settings = Settings()
