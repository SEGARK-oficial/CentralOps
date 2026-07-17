import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core.config import settings
from ..core.errors import ApiError
from ..db import models
from ..db import database, repository
from ..services.search_results import SearchResultRetentionService

router = APIRouter(prefix="/search", tags=["results"])


def _csv_expired(result: models.SearchResult) -> bool:
    if not result.created_at:
        return False
    retention_window = timedelta(days=settings.SEARCH_HISTORY_CSV_RETENTION_DAYS)
    return result.created_at < datetime.utcnow() - retention_window


# Chave de coluna sintética para itens escalares (não-dict) — busca federada normal
# devolve uma lista de dicts, mas mantemos robustez para shapes heterogêneos.
_SCALAR_COLUMN = "value"


def _extract_items(result_json: str) -> list:
    """Normaliza ``result_json`` para uma lista de itens.

    Aceita duas formas de payload persistido:
    - LISTA no topo (busca federada / opensearch_dsl): ``[ {...}, {...} ]``.
    - dict com ``items`` ou ``results``: ``{"items": [...]}`` / ``{"results": [...]}``.

    Só o *parse* de JSON é tolerado a falha (payload corrompido → sem dados). O
    tratamento de shape NÃO é embrulhado em ``try/except`` de propósito: um shape
    novo/inesperado deve propagar (500) em vez de virar um 404 silencioso e enganoso.
    """
    try:
        data = json.loads(result_json)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get("items")
        if items is None:
            items = data.get("results")
        return items if isinstance(items, list) else []
    return []


def _csv_cell(value: object) -> str:
    """Serializa uma célula. Valores aninhados (dict/list) → JSON compacto; o
    ``csv.writer`` cuida do quoting/escape (vírgulas, aspas, quebras de linha)."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _rows_to_csv(items: list) -> str:
    """Monta o CSV com cabeçalho = UNIÃO estável das chaves de todos os itens
    (ordem de primeira aparição), não apenas as do primeiro item — resultados
    heterogêneos não perdem colunas."""
    headers: list[str] = []
    seen: set[str] = set()
    has_scalars = False
    for item in items:
        if isinstance(item, dict):
            for key in item.keys():
                if key not in seen:
                    seen.add(key)
                    headers.append(key)
        else:
            has_scalars = True
    if has_scalars and _SCALAR_COLUMN not in seen:
        seen.add(_SCALAR_COLUMN)
        headers.append(_SCALAR_COLUMN)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for item in items:
        if isinstance(item, dict):
            writer.writerow([_csv_cell(item.get(h)) for h in headers])
        else:
            writer.writerow([_csv_cell(item) if h == _SCALAR_COLUMN else "" for h in headers])
    return output.getvalue()


def get_results_repo(
    db: Session = Depends(database.get_session),
) -> repository.SearchResultRepository:
    SearchResultRetentionService(db).prune_expired_entries(force=True)
    return repository.SearchResultRepository(db)


@router.get("/history", response_model=list[schemas.SearchResultRead])
def list_search_history(
    client_id: int | None = None,
    schedule_id: int | None = None,
    results_repo: repository.SearchResultRepository = Depends(get_results_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    return results_repo.list(client_id, schedule_id, viewer=current_user)


@router.get("/history/result/{search_id}", response_model=schemas.SearchResultRead)
def get_search_history_item(
    search_id: str,
    results_repo: repository.SearchResultRepository = Depends(get_results_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    result = results_repo.get_by_search_id(search_id, viewer=current_user)
    if not result:
        raise ApiError(
            "search.not_found",
            404,
            messages={
                "pt": "Busca não encontrada.",
                "en": "Search not found.",
                "es": "Búsqueda no encontrada.",
            },
        )
    return result


@router.get("/history/result/{search_id}/csv")
def download_history_csv(
    search_id: str,
    results_repo: repository.SearchResultRepository = Depends(get_results_repo),
    current_user: models.AppUser = Depends(app_auth.require_authenticated_user),
):
    result = results_repo.get_by_search_id(search_id, viewer=current_user)
    if not result or not result.result_json:
        raise ApiError(
            "search.results_not_available",
            404,
            messages={
                "pt": "Resultados não disponíveis.",
                "en": "Results not available.",
                "es": "Resultados no disponibles.",
            },
        )
    if _csv_expired(result):
        raise ApiError(
            "search.csv_expired",
            410,
            messages={
                "pt": "Download de CSV expirado para este resultado de busca.",
                "en": "CSV download expired for this search result.",
                "es": "Descarga de CSV vencida para este resultado de búsqueda.",
            },
        )
    items = _extract_items(result.result_json)
    if not items:
        raise ApiError(
            "search.no_data",
            404,
            messages={
                "pt": "Nenhum dado disponível.",
                "en": "No data available.",
                "es": "No hay datos disponibles.",
            },
        )
    csv_data = _rows_to_csv(items)
    return Response(
        csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{result.search_id}.csv"'},
    )
