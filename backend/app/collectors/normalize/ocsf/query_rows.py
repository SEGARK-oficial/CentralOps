"""Da tabela de uma scheduled query para ``Detection Finding`` (OCSF 1.8).

Uma scheduled query que devolvia linhas produzia um único evento ``Scheduled
Job Activity`` (1006) dizendo *quantas* linhas apareceram — e as linhas viajavam
só no bloco ``raw``. Duas coisas rotineiras apagam o ``raw`` antes do analista:
uma rota com ``drop_raw`` (redução de custo) e um destino com ``payload="ocsf"``
(que entrega apenas ``envelope["normalized"]``). O resultado era um alerta
crítico dizendo "encontrei 1 resultado" sem dizer qual — verde nos dois lados,
inútil na triagem.

Este módulo põe a tabela onde ela sobrevive: dentro do ``normalized``, na única
classe do OCSF 1.8 que comporta os artefatos de uma detecção — ``Detection
Finding`` (2004), via ``evidences[]``. A 1006 não tem onde guardar isso: os
campos obrigatórios dela são ``device`` e ``job``, e nada ali comporta uma
linha de telemetria.

**O schema da tabela não existe.** O statement é SQL livre escrito pelo
analista, então os nomes das colunas são aliases arbitrários (``"Cmdline
(Any)"``, ``"Endpoint ID"``) e mudam a cada query. Pior: o vendor **omite a
chave quando o valor é nulo** — duas linhas do mesmo run não têm
necessariamente as mesmas colunas. Por isso o mapeamento é por *nome
canonizado* contra um dicionário de sinônimos, e **toda coluna que não casa é
preservada** em ``evidences[].data`` com o nome original. Descartar o
desconhecido repetiria, um nível acima, o defeito que este módulo corrige.

**Orçamento de bytes, não contagem de linhas.** O ``analysisd`` do Wazuh trunca
em silêncio acima de ``OS_MAXSTR`` (65536). Um corte por número de linhas não
protege nada: 50 linhas com ``cmd_line`` de PowerShell encoded passam do teto e
o evento chega cortado no meio do JSON — uma perda pior do que a que este
módulo veio consertar, porque agora parece que o dado foi entregue. O corte é
por bytes serializados, com o que ficou de fora declarado no evento.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .classes import CLASS_UID_DETECTION_FINDING

__all__ = [
    "canonical_column",
    "row_to_evidence",
    "build_finding_normalized",
    "MAX_EVIDENCES_BYTES",
]


# ── Orçamento ────────────────────────────────────────────────────────

#: Teto do bloco ``evidences[]`` serializado. Conservador de propósito: os
#: 65536 do ``OS_MAXSTR`` são o limite do EVENTO INTEIRO, e o resto do envelope
#: (``_centralops``, ``finding_info``, ``unmapped``) também ocupa espaço.
MAX_EVIDENCES_BYTES = 48 * 1024

#: Teto por string individual. Existe para garantir que **uma** evidência
#: sempre cabe: sem ele, uma única ``cmd_line`` gigante estouraria o orçamento
#: na primeira linha e o evento sairia com zero evidências — o mesmo silêncio,
#: com outro nome.
MAX_FIELD_BYTES = 4096


# ── Canonização de nome de coluna ────────────────────────────────────

#: Sufixo agregador do SQL: ``ANY_VALUE(pa.cmdline) AS "Cmdline (Any)"``. O
#: parêntese descreve a agregação, não o campo — ``"Cmdline (Any)"`` e
#: ``"Cmdline"`` são a mesma coluna para efeito de mapeamento.
_PARENS_RE = re.compile(r"\([^)]*\)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_PATH_SEP_RE = re.compile(r"[\\/]")


def canonical_column(column: str) -> str:
    """``"Cmdline (Any)"`` → ``"cmdline"``; ``"Endpoint ID"`` → ``"endpoint_id"``.

    Puramente sintático — não consulta o dicionário de sinônimos. Separado para
    ser testável de fora e para que um alias novo seja um teste de uma linha.
    """
    lowered = column.strip().lower()
    without_parens = _PARENS_RE.sub(" ", lowered)
    return _NON_ALNUM_RE.sub("_", without_parens).strip("_")


# ── Dicionário de sinônimos → caminho OCSF dentro da evidência ───────

#: Coluna canonizada → dot-path no ``Evidence Artifacts``. Mantido explícito
#: (e não derivado por regra) porque um mapeamento errado aqui é silencioso: o
#: campo simplesmente aparece no lugar errado e ninguém erra em vermelho.
_EVIDENCE_PATHS: Mapping[str, str] = {
    # device
    "endpoint_id": "device.uid",
    "device_id": "device.uid",
    "agent_id": "device.uid",
    "hostname": "device.hostname",
    "host": "device.hostname",
    "host_name": "device.hostname",
    "computer_name": "device.hostname",
    "device_name": "device.hostname",
    "public_ip": "device.ip",
    "ip": "device.ip",
    "ip_address": "device.ip",
    "os": "device.os.name",
    "os_name": "device.os.name",
    # actor
    "username": "actor.user.name",
    "user": "actor.user.name",
    "user_name": "actor.user.name",
    "account_name": "actor.user.name",
    # process
    "process_name": "process.name",
    "process_path": "process.file.path",
    "image_path": "process.file.path",
    "cmdline": "process.cmd_line",
    "command_line": "process.cmd_line",
    "process_cmdline": "process.cmd_line",
    "pid": "process.pid",
    "process_id": "process.pid",
    "parent_name": "process.parent_process.name",
    "parent_process_name": "process.parent_process.name",
    "parent_cmdline": "process.parent_process.cmd_line",
    "parent_command_line": "process.parent_process.cmd_line",
    "parent_process_cmdline": "process.parent_process.cmd_line",
    "parent_path": "process.parent_process.file.path",
}

#: Hash → ``algorithm_id`` do OCSF (objeto ``hash``). Vira um item de
#: ``process.file.hashes[]``, que é um array justamente para caber mais de um.
_HASH_ALGORITHM_IDS: Mapping[str, int] = {
    "md5": 1,
    "sha1": 2,
    "sha_1": 2,
    "sha256": 3,
    "sha_256": 3,
    "sha512": 4,
    "sha_512": 4,
}

#: Colunas que descrevem a plataforma e viram ``device.os.type_id``.
_OS_TYPE_COLUMNS = frozenset({"os_platform", "platform", "os_type"})

#: Substring do valor → ``os.type_id`` do OCSF 1.8. Casado por substring
#: porque o vendor manda ``"Windows 10 Pro"``, não ``"windows"``.
_OS_TYPE_IDS: Sequence[Tuple[str, int]] = (
    ("windows", 100),
    ("android", 201),
    ("ubuntu", 200),
    ("debian", 200),
    ("centos", 200),
    ("rhel", 200),
    ("red hat", 200),
    ("linux", 200),
    ("ipados", 302),
    ("ios", 301),
    ("macos", 300),
    ("mac os", 300),
    ("darwin", 300),
    ("osx", 300),
    ("solaris", 400),
    ("aix", 401),
)

# Colunas agregadas: descrevem o achado inteiro, não um artefato. Vão para
# ``finding_info``/``count`` E permanecem na linha (em ``data``, com o nome
# original) — a agregação não pode custar a granularidade de quem lê a linha.
_FIRST_SEEN_COLUMNS = frozenset({"first_seen", "first_seen_time", "first_event_time"})
_LAST_SEEN_COLUMNS = frozenset({"last_seen", "last_seen_time", "last_event_time"})
_COUNT_COLUMNS = frozenset({"event_count", "count", "hits", "occurrences"})
_REASON_COLUMNS = frozenset({"match_reason", "reason", "matched_on", "detection_reason"})

#: Separador do ``ARRAY_JOIN`` que as hunts usam para concatenar motivos. Um
#: ``finding_info.types[]`` com os motivos separados vale mais que uma string
#: única que o consumidor teria de reparsear.
_REASON_SPLIT_RE = re.compile(r"\s*\|\s*")


# ── Conversões ───────────────────────────────────────────────────────


def _clip(value: str) -> str:
    """Corta em :data:`MAX_FIELD_BYTES` deixando a marca do corte no valor.

    O sufixo é deliberado: um valor cortado em silêncio é indistinguível de um
    valor que o vendor mandou curto.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_FIELD_BYTES:
        return value
    kept = encoded[:MAX_FIELD_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}…[+{len(encoded) - MAX_FIELD_BYTES} bytes]"


def _to_epoch_ms(value: Any) -> Optional[int]:
    """Timestamp do vendor → epoch **em milissegundos**.

    Milissegundos porque é o que o OCSF pede. Segundos aqui seria o erro de
    1000x que este repo já pagou uma vez, em 16 mappings de uma vez só.
    Devolve ``None`` quando não dá para converter — o caller mantém o valor
    original em ``data`` em vez de emitir um tempo inventado.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # < 1e11 só pode ser segundos: em milissegundos isso seria 1973, e em
        # segundos seria o ano 5138. Nenhum dos dois é ambíguo na prática.
        return int(value * 1000) if value < 1e11 else int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _os_type_id(value: Any) -> int:
    """Texto de plataforma → ``os.type_id``. ``0`` (Unknown) quando não casa."""
    if not isinstance(value, str):
        return 0
    lowered = value.strip().lower()
    for needle, type_id in _OS_TYPE_IDS:
        if needle in lowered:
            return type_id
    return 0


def _set_path(target: Dict[str, Any], dot_path: str, value: Any) -> None:
    """Escreve ``value`` em ``dot_path``, criando os dicts intermediários."""
    parts = dot_path.split(".")
    cursor = target
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _is_empty(value: Any) -> bool:
    """Vazio = ausente. O vendor já omite nulos; strings vazias são o mesmo
    fato escrito de outro jeito e não merecem um campo OCSF."""
    return value is None or (isinstance(value, str) and not value.strip())


# ── Linha → Evidence Artifact ────────────────────────────────────────


def row_to_evidence(
    row: Mapping[str, Any], *, build_evidence: bool = True
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Uma linha da tabela → (``Evidence Artifacts``, agregados dessa linha).

    O segundo elemento carrega o que é do ACHADO e não do artefato
    (``first_seen``/``last_seen``/``count``/``types``), para o caller consolidar
    entre as linhas. A linha continua com esses valores em ``data``.

    ``build_evidence=False`` colhe **só** os agregados. É o caminho usado depois
    que o orçamento de bytes fechou: os totais precisam cobrir a tabela inteira,
    mas montar milhares de evidências que já se sabe que não vão sair seria
    trabalho para o lixo — e num hunt que devolve dezenas de milhares de linhas,
    memória do worker. Mesma função (e não uma segunda) para que a
    classificação de coluna não tenha duas implementações livres para divergir.
    """
    evidence: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}
    aggregates: Dict[str, Any] = {}

    for column, value in row.items():
        if _is_empty(value):
            continue
        key = canonical_column(str(column))
        clipped = _clip(value) if isinstance(value, str) else value

        # 1) agregados do achado — também preservados na linha, em ``data``.
        if key in _FIRST_SEEN_COLUMNS:
            aggregates["first_seen"] = _to_epoch_ms(value)
            extra[str(column)] = clipped
            continue
        if key in _LAST_SEEN_COLUMNS:
            aggregates["last_seen"] = _to_epoch_ms(value)
            extra[str(column)] = clipped
            continue
        if key in _COUNT_COLUMNS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                aggregates["count"] = int(value)
            extra[str(column)] = clipped
            continue
        if key in _REASON_COLUMNS and isinstance(value, str):
            aggregates["types"] = [
                part for part in _REASON_SPLIT_RE.split(value.strip()) if part
            ]
            extra[str(column)] = clipped
            continue

        if not build_evidence:
            continue

        # 2) hash → item de ``process.file.hashes[]``.
        algorithm_id = _HASH_ALGORITHM_IDS.get(key)
        if algorithm_id is not None and isinstance(clipped, str):
            hashes = (
                evidence.setdefault("process", {})
                .setdefault("file", {})
                .setdefault("hashes", [])
            )
            hashes.append({"algorithm_id": algorithm_id, "value": clipped})
            continue

        # 3) plataforma → ``device.os.type_id``.
        if key in _OS_TYPE_COLUMNS:
            _set_path(evidence, "device.os.type_id", _os_type_id(value))
            extra[str(column)] = clipped
            continue

        # 4) caminho OCSF conhecido.
        path = _EVIDENCE_PATHS.get(key)
        if path is not None:
            _set_path(evidence, path, clipped)
            continue

        # 5) desconhecida — preservada com o nome ORIGINAL. É o ponto do
        #    módulo: nada da tabela do analista se perde por não ter sinônimo.
        extra[str(column)] = clipped

    if not build_evidence:
        return {}, aggregates

    _derive_file_name(evidence)
    if extra:
        evidence["data"] = extra
    return evidence, aggregates


def _derive_file_name(evidence: Dict[str, Any]) -> None:
    """``process.file.name`` a partir do basename de ``process.file.path``.

    Derivação, não invenção: o ``file`` do OCSF pede ``name``, e o basename do
    caminho é exatamente isso. Aceita separador Windows e POSIX porque a mesma
    query roda sobre frotas mistas.
    """
    file_obj = (evidence.get("process") or {}).get("file")
    if not isinstance(file_obj, dict):
        return
    path = file_obj.get("path")
    if isinstance(path, str) and path and not file_obj.get("name"):
        basename = _PATH_SEP_RE.split(path)[-1]
        if basename:
            file_obj["name"] = basename


# ── Tabela → bloco ``normalized`` do Detection Finding ───────────────


def _serialized_size(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"))


def build_finding_normalized(
    *,
    rows: Sequence[Mapping[str, Any]],
    finding_uid: str,
    title: str,
    description: Optional[str],
    severity_id: int,
    query_id: Optional[int],
    occurred_ms: int,
    unmapped: Optional[Mapping[str, Any]] = None,
    max_bytes: int = MAX_EVIDENCES_BYTES,
) -> Dict[str, Any]:
    """Monta o ``normalized`` de um ``Detection Finding`` (2004) com a tabela.

    ``activity_id=1`` (Create) e ``status_id=1`` (New): cada execução que
    devolve linhas é um achado novo. O 1006 continua descrevendo a EXECUÇÃO do
    job — as duas coisas são eventos diferentes e não se substituem.

    Os agregados (``count``, janela, ``types``) cobrem **todas** as linhas,
    inclusive as que o orçamento cortar: um ``count`` que só contasse o que
    coube contradiria o ``result_count`` do ``SearchResult``, e a discordância
    entre os dois números seria descoberta na triagem, no pior momento.
    """
    evidences: List[Dict[str, Any]] = []
    first_seen: Optional[int] = None
    last_seen: Optional[int] = None
    total_count = 0
    types: List[str] = []
    dropped = 0

    unlimited = max_bytes <= 0
    budget_used = 2  # os colchetes do array
    budget_closed = False

    for row in rows:
        evidence, aggregates = row_to_evidence(row, build_evidence=not budget_closed)

        row_first = aggregates.get("first_seen")
        if row_first is not None and (first_seen is None or row_first < first_seen):
            first_seen = row_first
        row_last = aggregates.get("last_seen")
        if row_last is not None and (last_seen is None or row_last > last_seen):
            last_seen = row_last
        total_count += int(aggregates.get("count") or 1)
        for reason in aggregates.get("types") or []:
            if reason not in types:
                types.append(reason)

        if budget_closed:
            dropped += 1
            continue
        if not evidence:
            continue
        if unlimited:
            evidences.append(evidence)
            continue

        size = _serialized_size(evidence) + (1 if evidences else 0)  # vírgula
        # A primeira evidência entra mesmo estourando: um achado com zero
        # evidências é exatamente o silêncio que este módulo veio remover, e o
        # ``_clip`` por campo já mantém esse caso raro dentro do razoável.
        if evidences and budget_used + size > max_bytes:
            budget_closed = True
            dropped += 1
            continue
        evidences.append(evidence)
        budget_used += size

    finding_info: Dict[str, Any] = {"uid": finding_uid, "title": title}
    if description:
        finding_info["desc"] = description
    if types:
        finding_info["types"] = types
    if first_seen is not None:
        finding_info["first_seen_time"] = first_seen
    if last_seen is not None:
        finding_info["last_seen_time"] = last_seen
    # A query agendada É a analítica que produziu o achado — ``type_id=1``
    # (Rule). Aqui vai a IDENTIDADE dela (nome + uid), não o texto: o statement
    # de uma hunt real passa de 4 KiB e já viaja no 1006 e no ``SearchResult``.
    # Filtrar o statement que venha herdado em ``unmapped`` é responsabilidade
    # de quem chama — este módulo não conhece as chaves do produtor.
    analytic: Dict[str, Any] = {"name": title, "type_id": 1}
    if query_id is not None:
        analytic["uid"] = str(query_id)
    finding_info["analytic"] = analytic

    activity_id = 1  # Create
    normalized: Dict[str, Any] = {
        "class_uid": CLASS_UID_DETECTION_FINDING,
        "category_uid": 2,
        "activity_id": activity_id,
        "type_uid": CLASS_UID_DETECTION_FINDING * 100 + activity_id,
        "time": occurred_ms,
        "status_id": 1,  # New
        "severity_id": severity_id,
        "count": total_count,
        "metadata": {
            "version": "1.8.0",
            "product": {"name": "CentralOps", "vendor_name": "CentralOps"},
            "logged_time": occurred_ms,
        },
        "finding_info": finding_info,
        "evidences": evidences,
    }

    merged_unmapped: Dict[str, Any] = dict(unmapped or {})
    merged_unmapped["evidences_total"] = len(evidences) + dropped
    merged_unmapped["evidences_included"] = len(evidences)
    # Sempre presente, mesmo ``False``: um consumidor que precise saber se viu
    # tudo não deveria ter de distinguir "não truncou" de "campo ausente".
    merged_unmapped["evidences_truncated"] = dropped > 0
    if dropped:
        merged_unmapped["evidences_dropped"] = dropped
    normalized["unmapped"] = merged_unmapped
    return normalized
