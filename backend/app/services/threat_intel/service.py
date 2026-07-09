"""Orquestração dos 3 tiers do Threat Intel (blacklist → cache → APIs)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from ...db import database, models
from .cache import cache as default_cache, ThreatIntelCache
from .clients.abuseipdb import (
    AbuseIPDBAuthError,
    AbuseIPDBClient,
    AbuseIPDBQuotaExceeded,
    AbuseIPDBResult,
)
from .clients.otx import OTXAuthError, OTXClient, OTXQuotaExceeded, OTXResult
from .consensus import (
    ConsensusInput,
    THREAT_CRITICAL,
    THREAT_SAFE,
    calculate_threat_level,
)
from .key_manager import (
    NoApiKeyAvailableError,
    PROVIDER_ABUSEIPDB,
    PROVIDER_OTX,
    key_manager,
)

logger = logging.getLogger(__name__)


TIER_PRIVATE = "private"
TIER_DISABLED = "disabled"
TIER_BLACKLIST = "tier0"
TIER_CACHE = "tier1"
TIER_EXTERNAL = "tier2"


class ThreatIntelService:
    """Serviço principal — fluxo descrito em RF-1..RF-8."""

    def __init__(
        self,
        cache: Optional[ThreatIntelCache] = None,
        abuse_client: Optional[AbuseIPDBClient] = None,
        otx_client: Optional[OTXClient] = None,
    ) -> None:
        self._cache = cache or default_cache
        self._abuse_client = abuse_client
        self._otx_client = otx_client

    # ── Helpers ──────────────────────────────────────────────────────

    def _get_config(self, db: Session) -> models.ThreatIntelConfig:
        config = db.get(models.ThreatIntelConfig, 1)
        if config:
            return config
        # Garante que o singleton exista mesmo se a migration tiver sido pulada.
        config = models.ThreatIntelConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
        return config

    @staticmethod
    def _is_private(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    @staticmethod
    def _flatten_response(
        ip: str,
        *,
        threat_level: str,
        tier: str,
        cached: bool,
        consensus: ConsensusInput,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ip_address": ip,
            "cached": cached,
            "tier": tier,
            "threat_level": threat_level,
            "otx_pulse_count": consensus.otx_pulse_count or 0,
            "abuse_score": consensus.abuse_score or 0,
            "abuse_country": consensus.abuse_country,
            "abuse_usage_type": consensus.abuse_usage_type,
        }
        if note:
            payload["note"] = note
        if consensus.quota_exceeded:
            payload["quota_exceeded_warning"] = True
        if consensus.otx_failed:
            payload["otx_failed"] = True
        if consensus.abuse_failed:
            payload["abuse_failed"] = True
        return payload

    # ── Persistência de auditoria ────────────────────────────────────

    def _persist_query(
        self,
        db: Session,
        *,
        ip: str,
        tier: str,
        threat_level: str,
        consensus: ConsensusInput,
        response_time_ms: int,
        token_id: Optional[int],
        source_ip: Optional[str],
    ) -> None:
        record = models.ThreatIntelQuery(
            ip_address=ip,
            tier=tier,
            threat_level=threat_level,
            otx_pulse_count=consensus.otx_pulse_count,
            abuse_score=consensus.abuse_score,
            abuse_country=consensus.abuse_country,
            abuse_usage_type=consensus.abuse_usage_type,
            response_time_ms=response_time_ms,
            quota_exceeded=consensus.quota_exceeded,
            token_id=token_id,
            source_ip=source_ip,
        )
        db.add(record)
        db.commit()

    # ── Tier 2 helpers ───────────────────────────────────────────────

    def _abuse_client_instance(self, timeout: int) -> AbuseIPDBClient:
        return self._abuse_client or AbuseIPDBClient(timeout_seconds=timeout)

    def _otx_client_instance(self, timeout: int) -> OTXClient:
        return self._otx_client or OTXClient(timeout_seconds=timeout)

    async def _query_abuseipdb(
        self,
        db: Session,
        ip: str,
        config: models.ThreatIntelConfig,
    ) -> AbuseIPDBResult:
        attempts = max(key_manager.keys_available(db, PROVIDER_ABUSEIPDB), 1)
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                key_record, plaintext = key_manager.get_next_key(db, PROVIDER_ABUSEIPDB)
            except NoApiKeyAvailableError as exc:
                raise exc
            try:
                client = self._abuse_client_instance(config.external_timeout_seconds)
                result = await client.check_ip(
                    ip,
                    plaintext,
                    max_age_days=config.abuseipdb_max_age_days,
                )
                key_manager.record_success(db, key_record.id)
                return result
            except AbuseIPDBQuotaExceeded as exc:
                key_manager.mark_exhausted(db, key_record.id, reason=str(exc))
                last_error = exc
                continue
            except AbuseIPDBAuthError as exc:
                key_manager.mark_exhausted(
                    db,
                    key_record.id,
                    cooldown=timedelta(days=7),
                    reason=str(exc),
                )
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise NoApiKeyAvailableError("Nenhuma chave AbuseIPDB disponível.")

    async def _query_otx(
        self,
        db: Session,
        ip: str,
        config: models.ThreatIntelConfig,
    ) -> OTXResult:
        attempts = max(key_manager.keys_available(db, PROVIDER_OTX), 1)
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                key_record, plaintext = key_manager.get_next_key(db, PROVIDER_OTX)
            except NoApiKeyAvailableError as exc:
                raise exc
            try:
                client = self._otx_client_instance(config.external_timeout_seconds)
                result = await client.lookup_ip(ip, plaintext)
                key_manager.record_success(db, key_record.id)
                return result
            except OTXQuotaExceeded as exc:
                key_manager.mark_exhausted(db, key_record.id, reason=str(exc))
                last_error = exc
                continue
            except OTXAuthError as exc:
                key_manager.mark_exhausted(
                    db,
                    key_record.id,
                    cooldown=timedelta(days=7),
                    reason=str(exc),
                )
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise NoApiKeyAvailableError("Nenhuma chave OTX disponível.")

    # ── Fluxo público ────────────────────────────────────────────────

    async def analyze(
        self,
        ip: str,
        *,
        token_id: Optional[int] = None,
        source_ip: Optional[str] = None,
        db: Optional[Session] = None,
    ) -> dict[str, Any]:
        owns_session = db is None
        session = db or database.SessionLocal()
        started_at = time.perf_counter()
        try:
            return await self._analyze_inner(
                session,
                ip,
                token_id=token_id,
                source_ip=source_ip,
                started_at=started_at,
            )
        finally:
            if owns_session:
                session.close()

    async def _analyze_inner(
        self,
        db: Session,
        raw_ip: str,
        *,
        token_id: Optional[int],
        source_ip: Optional[str],
        started_at: float,
    ) -> dict[str, Any]:
        config = self._get_config(db)

        try:
            ip_obj = ipaddress.ip_address(raw_ip.strip())
        except ValueError:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            consensus = ConsensusInput()
            response = self._flatten_response(
                raw_ip,
                threat_level=THREAT_SAFE,
                tier=TIER_PRIVATE,
                cached=False,
                consensus=consensus,
                note="Invalid IP address",
            )
            response["error"] = "invalid_ip"
            return response

        ip = str(ip_obj)

        # Disabled — devolve resposta degradada sem persistir auditoria.
        if not config.enabled:
            consensus = ConsensusInput()
            response = self._flatten_response(
                ip,
                threat_level=THREAT_SAFE,
                tier=TIER_DISABLED,
                cached=False,
                consensus=consensus,
                note="Threat Intel disabled",
            )
            return response

        # RF-2 — IPs privados/loopback/etc.
        if self._is_private(ip_obj):
            consensus = ConsensusInput()
            response = self._flatten_response(
                ip,
                threat_level=THREAT_SAFE,
                tier=TIER_PRIVATE,
                cached=False,
                consensus=consensus,
                note="Private IP ignored",
            )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            self._persist_query(
                db,
                ip=ip,
                tier=TIER_PRIVATE,
                threat_level=THREAT_SAFE,
                consensus=consensus,
                response_time_ms=elapsed_ms,
                token_id=token_id,
                source_ip=source_ip,
            )
            return response

        # RF-3 — Tier 0 blacklist global
        if await self._cache.is_blacklisted(ip):
            consensus = ConsensusInput(abuse_score=100)
            response = self._flatten_response(
                ip,
                threat_level=THREAT_CRITICAL,
                tier=TIER_BLACKLIST,
                cached=True,
                consensus=consensus,
                note="Blocked by Global Blacklist",
            )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            self._persist_query(
                db,
                ip=ip,
                tier=TIER_BLACKLIST,
                threat_level=THREAT_CRITICAL,
                consensus=consensus,
                response_time_ms=elapsed_ms,
                token_id=token_id,
                source_ip=source_ip,
            )
            return response

        # RF-4 — Tier 1 cache
        cached_payload = await self._cache.get_ip(ip)
        if cached_payload:
            cached_payload["cached"] = True
            cached_payload["tier"] = TIER_CACHE
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            consensus = ConsensusInput(
                otx_pulse_count=cached_payload.get("otx_pulse_count"),
                abuse_score=cached_payload.get("abuse_score"),
                abuse_country=cached_payload.get("abuse_country"),
                abuse_usage_type=cached_payload.get("abuse_usage_type"),
            )
            self._persist_query(
                db,
                ip=ip,
                tier=TIER_CACHE,
                threat_level=cached_payload.get("threat_level", THREAT_SAFE),
                consensus=consensus,
                response_time_ms=elapsed_ms,
                token_id=token_id,
                source_ip=source_ip,
            )
            return cached_payload

        # RF-5 — Tier 2 (chamadas paralelas, RNF-3 com return_exceptions)
        otx_task = asyncio.create_task(self._query_otx(db, ip, config))
        abuse_task = asyncio.create_task(self._query_abuseipdb(db, ip, config))
        otx_outcome, abuse_outcome = await asyncio.gather(
            otx_task, abuse_task, return_exceptions=True
        )

        consensus = ConsensusInput()

        if isinstance(otx_outcome, OTXResult):
            consensus.otx_pulse_count = otx_outcome.pulse_count
        else:
            consensus.otx_failed = True
            if isinstance(otx_outcome, (OTXQuotaExceeded, NoApiKeyAvailableError)):
                consensus.quota_exceeded = True
            if isinstance(otx_outcome, Exception):
                logger.warning("OTX falhou para %s: %s", ip, otx_outcome)

        if isinstance(abuse_outcome, AbuseIPDBResult):
            consensus.abuse_score = abuse_outcome.abuse_confidence_score
            consensus.abuse_country = abuse_outcome.country_code
            consensus.abuse_usage_type = abuse_outcome.usage_type
        else:
            consensus.abuse_failed = True
            if isinstance(abuse_outcome, (AbuseIPDBQuotaExceeded, NoApiKeyAvailableError)):
                consensus.quota_exceeded = True
            if isinstance(abuse_outcome, Exception):
                logger.warning("AbuseIPDB falhou para %s: %s", ip, abuse_outcome)

        threat_level = calculate_threat_level(consensus, config)
        response = self._flatten_response(
            ip,
            threat_level=threat_level,
            tier=TIER_EXTERNAL,
            cached=False,
            consensus=consensus,
        )

        ttl_seconds = max(int(config.cache_ttl_days) * 86400, 60)
        await self._cache.set_ip(
            ip,
            {**response, "tier": TIER_CACHE, "cached": True},
            ttl_seconds,
        )

        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        self._persist_query(
            db,
            ip=ip,
            tier=TIER_EXTERNAL,
            threat_level=threat_level,
            consensus=consensus,
            response_time_ms=elapsed_ms,
            token_id=token_id,
            source_ip=source_ip,
        )
        return response


_default_service: Optional[ThreatIntelService] = None


def get_threat_intel_service() -> ThreatIntelService:
    global _default_service
    if _default_service is None:
        _default_service = ThreatIntelService()
    return _default_service


__all__ = [
    "ThreatIntelService",
    "get_threat_intel_service",
    "TIER_PRIVATE",
    "TIER_BLACKLIST",
    "TIER_CACHE",
    "TIER_EXTERNAL",
    "TIER_DISABLED",
]
