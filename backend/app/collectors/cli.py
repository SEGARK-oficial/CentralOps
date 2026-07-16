"""CLI de debug e operação para o collector.

Uso:

    python -m backend.app.collectors.cli smoke --integration 42 --stream alerts
    python -m backend.app.collectors.cli list-state --integration 42
    python -m backend.app.collectors.cli reset-cursor --integration 42 --stream alerts

    python -m backend.app.collectors.cli list-selections --partner-id 42 --state pending
    python -m backend.app.collectors.cli bulk-approve --partner-id 42 --all-pending --dry-run
    python -m backend.app.collectors.cli bulk-approve --partner-id 42 --csv ids.csv --apply
    python -m backend.app.collectors.cli bulk-approve --partner-id 42 --csv ids.csv --state excluded --apply

Os comandos ``bulk-approve`` e ``list-selections`` operam direto contra o banco
(bypass HTTP/auth) e são reservados para administração. Para automação externa,
prefira a API ``POST /api/integrations/{id}/tenants/select``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from typing import Iterable

import redis.asyncio as redis_async

from ..core import ee_hooks
from ..core.config import settings
from ..db import database, models
from ..db.repository import (
    CollectionStateRepository,
    IntegrationRepository,
    IntegrationTenantSelectionRepository,
)
from .pipeline import run_collection_once
from .state.cursor import HOT_KEY

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


# ─── existing collector commands ──────────────────────────────────────


async def _cmd_smoke(args: argparse.Namespace) -> int:
    await run_collection_once(args.integration, args.stream)
    return 0


async def _cmd_reset_cursor(args: argparse.Namespace) -> int:
    redis = redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0", decode_responses=True
    )
    try:
        await redis.delete(
            HOT_KEY.format(integration_id=args.integration, stream=args.stream)
        )
    finally:
        await redis.aclose()

    with database.SessionLocal() as db:
        repo = CollectionStateRepository(db)
        row = repo.get(args.integration, args.stream)
        if row:
            db.delete(row)
            db.commit()
    print("cursor reset OK")
    return 0


def _cmd_list_state(args: argparse.Namespace) -> int:
    with database.SessionLocal() as db:
        repo = CollectionStateRepository(db)
        rows = repo.list_for_integration(args.integration)
        for r in rows:
            print(
                json.dumps(
                    {
                        "integration_id": r.integration_id,
                        "stream": r.stream,
                        "last_success_at": str(r.last_success_at),
                        "consecutive_failures": r.consecutive_failures,
                        "events_collected_total": r.events_collected_total,
                        "last_error": r.last_error,
                    },
                    indent=2,
                )
            )
    return 0


# ─── tenant selection commands (Sophos Partner) ───────────────────────


def _read_csv_external_ids(path: str) -> list[str]:
    """Lê uma external_id por linha de um CSV. Trim + dedup preservando ordem."""
    seen: set[str] = set()
    ordered: list[str] = []
    with open(path, "r", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            for cell in row:
                value = (cell or "").strip()
                if not value or value in seen:
                    continue
                # Pula header trivial (linha "external_id" sozinha).
                if value.lower() == "external_id":
                    continue
                seen.add(value)
                ordered.append(value)
    return ordered


def _resolve_partner_or_exit(
    db, partner_id: int
) -> models.Integration:
    """Carrega a integração mãe ou aborta com mensagem amigável."""
    int_repo = IntegrationRepository(db)
    partner = int_repo.get(partner_id)
    if partner is None:
        print(f"[ERROR] integration id={partner_id} not found", file=sys.stderr)
        sys.exit(2)
    if partner.kind not in ("partner", "organization"):
        print(
            f"[ERROR] integration id={partner_id} is kind={partner.kind!r} — "
            f"expected partner|organization",
            file=sys.stderr,
        )
        sys.exit(2)
    return partner


def _resolve_external_ids(
    sel_repo: IntegrationTenantSelectionRepository,
    partner_id: int,
    *,
    csv_path: str | None,
    all_pending: bool,
) -> list[str]:
    """Aplica regra de origem dos external_ids (CSV ou --all-pending)."""
    if csv_path and all_pending:
        raise ValueError("use --csv OR --all-pending, não ambos")
    if not csv_path and not all_pending:
        raise ValueError("informe --csv <path> ou --all-pending")
    if csv_path:
        return _read_csv_external_ids(csv_path)
    pending_ids = sel_repo.list_external_ids(partner_id, state="pending")
    return sorted(pending_ids)


def _cmd_list_selections(args: argparse.Namespace) -> int:
    with database.SessionLocal() as db:
        partner = _resolve_partner_or_exit(db, args.partner_id)
        sel_repo = IntegrationTenantSelectionRepository(db)
        state_filter = None if args.state == "all" else args.state
        rows = sel_repo.list(partner.id, state=state_filter, limit=args.limit)
        for r in rows:
            print(
                json.dumps(
                    {
                        "external_id": r.external_id,
                        "name": r.name_snapshot,
                        "state": r.state,
                        "region": r.region_snapshot,
                        "data_geography": r.data_geography_snapshot,
                        "api_host": r.api_host_snapshot,
                        "decided_by_user_id": r.decided_by_user_id,
                        "decided_at": str(r.decided_at) if r.decided_at else None,
                        "last_seen_at": str(r.last_seen_at) if r.last_seen_at else None,
                    },
                    indent=2,
                )
            )
        print(
            f"[INFO] {len(rows)} selection(s) listed (state={args.state})",
            file=sys.stderr,
        )
    return 0


def _print_dry_run_summary(
    *,
    state: str,
    target_ids: Iterable[str],
    found: dict[str, models.IntegrationTenantSelection],
) -> None:
    target_list = list(target_ids)
    missing = [eid for eid in target_list if eid not in found]
    same_state = [
        eid for eid in target_list if eid in found and found[eid].state == state
    ]
    will_change = [
        eid for eid in target_list if eid in found and found[eid].state != state
    ]
    print("[DRY-RUN] resumo da operação:", file=sys.stderr)
    print(f"  total alvos    : {len(target_list)}", file=sys.stderr)
    print(f"  já em '{state}'   : {len(same_state)}", file=sys.stderr)
    print(f"  mudará de estado: {len(will_change)}", file=sys.stderr)
    print(
        f"  não descobertos: {len(missing)} (necessitam sync antes)",
        file=sys.stderr,
    )
    for eid in will_change[:25]:
        from_state = found[eid].state
        name = found[eid].name_snapshot or "(sem nome)"
        print(f"   - {eid} {name!r}: {from_state} -> {state}", file=sys.stderr)
    if len(will_change) > 25:
        print(f"   ... +{len(will_change) - 25} more", file=sys.stderr)


def _cmd_bulk_approve(args: argparse.Namespace) -> int:
    """Bulk approve/exclude tenant selections.

    Usa caminho via DB direto (mesmas funções do endpoint
    ``POST /tenants/select``). ``--apply`` é necessário pra efetivar — sem ele
    é dry-run (default seguro).
    """
    state = args.state
    if state not in ("approved", "excluded"):
        print(f"[ERROR] --state inválido: {state!r}", file=sys.stderr)
        return 2

    try:
        with database.SessionLocal() as db:
            partner = _resolve_partner_or_exit(db, args.partner_id)
            sel_repo = IntegrationTenantSelectionRepository(db)

            try:
                target_ids = _resolve_external_ids(
                    sel_repo,
                    partner.id,
                    csv_path=args.csv,
                    all_pending=args.all_pending,
                )
            except ValueError as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 2

            if not target_ids:
                print("[INFO] nenhum external_id encontrado para processar", file=sys.stderr)
                return 0

            # Carrega as selections que JÁ EXISTEM (descobertas pelo sync).
            existing_rows = (
                db.query(models.IntegrationTenantSelection)
                .filter(
                    models.IntegrationTenantSelection.parent_integration_id == partner.id,
                    models.IntegrationTenantSelection.external_id.in_(target_ids),
                )
                .all()
            )
            found = {r.external_id: r for r in existing_rows}

            if not args.apply:
                _print_dry_run_summary(
                    state=state, target_ids=target_ids, found=found
                )
                print(
                    "[DRY-RUN] nada foi gravado. Use --apply para efetivar.",
                    file=sys.stderr,
                )
                return 0

            # Apply path.
            valid_ids = [eid for eid in target_ids if eid in found]
            missing = [eid for eid in target_ids if eid not in found]

            if not valid_ids:
                print(
                    f"[ERROR] nenhum tenant alvo está descoberto. "
                    f"{len(missing)} ids não estão em integration_tenant_selections. "
                    f"Rode POST /sync-tenants antes.",
                    file=sys.stderr,
                )
                return 1

            # Child materialization is an Enterprise feature. Check
            # the applier BEFORE mutating state so Community --apply is ATOMIC — it does
            # not partially change selection state and then bail. (Dry-run still works:
            # it is DB read-only.)
            applier = ee_hooks.get_tenant_selection_applier()
            if applier is None:
                print(
                    "[ERROR] bulk-approve --apply requer a edição Enterprise "
                    "(gestão de tenants-filho de reseller). O dry-run funciona na "
                    "Community.",
                    file=sys.stderr,
                )
                return 1

            updated = sel_repo.set_state(
                parent_id=partner.id,
                external_ids=valid_ids,
                state=state,
                decided_by_user_id=args.actor_user_id,
            )

            try:
                result = applier(db, partner, updated, state)
            except ee_hooks.LicenseRequiredError as exc:
                # EE presente, mas a licença ativa não concede a feature: o applier
                # recusou ANTES de materializar (decisões persistidas, zero children).
                print(
                    f"[ERROR] license_required: a licença ativa não concede a feature "
                    f"{exc.feature!r} — bulk-approve --apply requer uma licença "
                    f"Enterprise com multi_tenant. As decisões de seleção foram "
                    f"persistidas; nenhum child foi materializado.",
                    file=sys.stderr,
                )
                return 1
            materialized = int(result.get("materialized", 0))
            deactivated = int(result.get("deactivated", 0))
            errors: list[tuple[str, str]] = [
                (e["external_id"], e["reason"]) for e in result.get("errors", [])
            ]

            summary = {
                "partner_id": partner.id,
                "state": state,
                "processed": len(updated),
                "materialized": materialized,
                "deactivated": deactivated,
                "missing": len(missing),
                "errors": [{"external_id": eid, "reason": r} for eid, r in errors],
            }
            print(json.dumps(summary, indent=2))
            return 0 if not errors else 1
    finally:
        pass


# ─── dispatcher ───────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="centralops-collector")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_smoke = sub.add_parser("smoke", help="Executa um ciclo de coleta ad-hoc")
    p_smoke.add_argument("--integration", type=int, required=True)
    p_smoke.add_argument("--stream", required=True)

    p_reset = sub.add_parser("reset-cursor", help="Zera cursor de (integration, stream)")
    p_reset.add_argument("--integration", type=int, required=True)
    p_reset.add_argument("--stream", required=True)

    p_list = sub.add_parser("list-state", help="Lista estados de coleta de uma integração")
    p_list.add_argument("--integration", type=int, required=True)

    p_list_sel = sub.add_parser(
        "list-selections",
        help="Lista seleções de tenants Sophos de um Partner",
    )
    p_list_sel.add_argument("--partner-id", type=int, required=True)
    p_list_sel.add_argument(
        "--state",
        choices=["pending", "approved", "excluded", "all"],
        default="pending",
    )
    p_list_sel.add_argument("--limit", type=int, default=None)

    p_bulk = sub.add_parser(
        "bulk-approve",
        help="Aprovação/exclusão em massa de tenants Sophos via DB direto",
    )
    p_bulk.add_argument("--partner-id", type=int, required=True)
    src = p_bulk.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--csv",
        help="Caminho do CSV com uma external_id por linha",
    )
    src.add_argument(
        "--all-pending",
        action="store_true",
        help="Pega todos os tenants em state=pending do Partner",
    )
    p_bulk.add_argument(
        "--state",
        choices=["approved", "excluded"],
        default="approved",
        help="Estado destino (default: approved)",
    )
    p_bulk.add_argument(
        "--apply",
        action="store_true",
        help="Efetivar mudanças. Sem essa flag o comando é dry-run.",
    )
    p_bulk.add_argument(
        "--actor-user-id",
        type=int,
        default=None,
        help="ID do AppUser pra auditoria (decided_by_user_id). Default: NULL.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "smoke":
        return asyncio.run(_cmd_smoke(args))
    if args.cmd == "reset-cursor":
        return asyncio.run(_cmd_reset_cursor(args))
    if args.cmd == "list-state":
        return _cmd_list_state(args)
    if args.cmd == "list-selections":
        return _cmd_list_selections(args)
    if args.cmd == "bulk-approve":
        return _cmd_bulk_approve(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
