from datetime import datetime, timedelta
import json

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
    try:
        data = json.loads(result.result_json)
        items = data.get("items") or data.get("results") or []
    except Exception:
        items = []
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
    headers = list(items[0].keys())
    csv_lines = [",".join(headers)]
    for item in items:
        csv_lines.append(",".join(json.dumps(item.get(h, "")) for h in headers))
    csv_data = "\n".join(csv_lines)
    return Response(csv_data, media_type="text/csv")
