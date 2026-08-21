"""A tabela de uma scheduled query chega ao destino, mapeada em OCSF 1.8.

O defeito coberto aqui: as linhas do resultado viajavam só no bloco ``raw``, e
o ``raw`` some em dois caminhos rotineiros — uma rota com ``drop_raw`` e um
destino com ``payload="ocsf"`` (que entrega apenas ``normalized``). O alerta
crítico chegava dizendo quantas linhas apareceram, sem dizer quais.

Os nomes de coluna aqui são os de uma hunt de ``process_activity`` real (com os
sufixos de agregação do SQL, ``"Cmdline (Any)"``); os VALORES são sintéticos.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from unittest.mock import patch

from backend.app.collectors.normalize.ocsf.query_rows import (
    MAX_EVIDENCES_BYTES,
    build_finding_normalized,
    canonical_column,
    row_to_evidence,
)

# Uma linha no formato que o data lake devolve: alias do SELECT como chave.
_ROW = {
    "Endpoint ID": "11111111-2222-3333-4444-555555555555",
    "Hostname": "HOST-0001",
    "Public IP": "203.0.113.10",
    "OS": "Windows 10 Pro 22H2",
    "OS Platform": "windows",
    "Username": "svc_test",
    "Process Name": "anydesk.exe",
    "Process Path": "C:\\Program Files\\Test\\anydesk.exe",
    "SHA256": "a" * 64,
    "Cmdline (Any)": "anydesk.exe --start-service",
    "Parent Name (Any)": "explorer.exe",
    "Parent Cmdline (Any)": "C:\\Windows\\explorer.exe",
    "First Seen": "2026-08-19T20:31:23Z",
    "Last Seen": "2026-08-19T21:04:12Z",
    "Event Count": 2,
    "Match Reason": "PROC_NAME_IOC | C2_DOMAIN_IN_CMDLINE",
}


def _finding(rows, **kwargs):
    params = dict(
        rows=rows,
        finding_uid="sched:2:integ:9:539",
        title="Hunt de teste",
        description="descricao da hunt",
        severity_id=5,
        query_id=1,
        occurred_ms=1787252507558,
    )
    params.update(kwargs)
    return build_finding_normalized(**params)


class TestCanonicalColumn:
    """O alias do SQL vira uma chave estável antes de procurar sinônimo."""

    def test_strips_sql_aggregate_suffix(self) -> None:
        # ``ANY_VALUE(pa.cmdline) AS "Cmdline (Any)"`` — o parêntese descreve a
        # agregação, não o campo.
        assert canonical_column("Cmdline (Any)") == "cmdline"
        assert canonical_column("Parent Cmdline (Any)") == "parent_cmdline"

    def test_normalizes_spaces_and_case(self) -> None:
        assert canonical_column("Endpoint ID") == "endpoint_id"
        assert canonical_column("SHA256") == "sha256"
        assert canonical_column("  Public IP  ") == "public_ip"


class TestRowToEvidence:
    """Cada linha vira um ``Evidence Artifacts`` do OCSF 1.8."""

    def test_maps_device_actor_and_process(self) -> None:
        evidence, _ = row_to_evidence(_ROW)

        assert evidence["device"]["uid"] == _ROW["Endpoint ID"]
        assert evidence["device"]["hostname"] == "HOST-0001"
        assert evidence["device"]["ip"] == "203.0.113.10"
        assert evidence["device"]["os"]["name"] == "Windows 10 Pro 22H2"
        assert evidence["actor"]["user"]["name"] == "svc_test"
        assert evidence["process"]["name"] == "anydesk.exe"
        assert evidence["process"]["cmd_line"] == "anydesk.exe --start-service"
        assert evidence["process"]["parent_process"]["name"] == "explorer.exe"
        assert (
            evidence["process"]["parent_process"]["cmd_line"]
            == "C:\\Windows\\explorer.exe"
        )

    def test_sha256_becomes_a_hash_object(self) -> None:
        # Um hash solto numa string perde o algoritmo; o OCSF pede o par.
        evidence, _ = row_to_evidence(_ROW)
        hashes = evidence["process"]["file"]["hashes"]
        assert hashes == [{"algorithm_id": 3, "value": "a" * 64}]  # 3 = SHA-256

    def test_os_platform_becomes_type_id(self) -> None:
        evidence, _ = row_to_evidence(_ROW)
        assert evidence["device"]["os"]["type_id"] == 100  # Windows

    def test_file_name_derived_from_path(self) -> None:
        # Basename do caminho, com separador Windows — a mesma query roda sobre
        # frota mista.
        evidence, _ = row_to_evidence(_ROW)
        assert evidence["process"]["file"]["name"] == "anydesk.exe"

    def test_unknown_column_is_preserved_not_dropped(self) -> None:
        # O ponto do módulo: o statement é SQL livre, então sempre haverá coluna
        # sem sinônimo. Descartá-la repetiria o defeito que isto veio corrigir.
        evidence, _ = row_to_evidence({**_ROW, "Coluna Inventada": "valor-x"})
        assert evidence["data"]["Coluna Inventada"] == "valor-x"

    def test_missing_columns_do_not_break(self) -> None:
        # O vendor OMITE a chave quando o valor é nulo: duas linhas do mesmo run
        # não têm necessariamente as mesmas colunas.
        evidence, _ = row_to_evidence({"Hostname": "HOST-0002"})
        assert evidence["device"]["hostname"] == "HOST-0002"
        assert "process" not in evidence

    def test_empty_string_is_treated_as_absent(self) -> None:
        evidence, _ = row_to_evidence({"Hostname": "HOST-0003", "Username": "   "})
        assert "actor" not in evidence


class TestFindingAggregates:
    """O que descreve o achado inteiro sai de ``finding_info``, não da linha."""

    def test_timestamps_are_milliseconds(self) -> None:
        # Segundos aqui seria o erro de 1000x que este repo já pagou em 16
        # mappings de uma vez. 13 dígitos = ms; 10 = segundos.
        finding = _finding([_ROW])
        first = finding["finding_info"]["first_seen_time"]
        assert first == 1787171483000
        assert len(str(first)) == 13

    def test_window_spans_all_rows(self) -> None:
        early = {**_ROW, "First Seen": "2026-08-01T00:00:00Z"}
        late = {**_ROW, "Last Seen": "2026-08-20T23:59:59Z"}
        finding = _finding([early, late])
        assert finding["finding_info"]["first_seen_time"] < finding["finding_info"][
            "last_seen_time"
        ]

    def test_match_reason_becomes_finding_types(self) -> None:
        # O ``ARRAY_JOIN(..., ' | ')`` da hunt vira uma lista, para o consumidor
        # não ter de reparsear a string.
        finding = _finding([_ROW])
        assert finding["finding_info"]["types"] == [
            "PROC_NAME_IOC",
            "C2_DOMAIN_IN_CMDLINE",
        ]

    def test_count_sums_event_counts(self) -> None:
        finding = _finding([_ROW, _ROW])
        assert finding["count"] == 4  # 2 + 2

    def test_query_becomes_the_analytic(self) -> None:
        finding = _finding([_ROW])
        analytic = finding["finding_info"]["analytic"]
        assert analytic["name"] == "Hunt de teste"
        assert analytic["type_id"] == 1  # Rule
        assert analytic["uid"] == "1"

    def test_analytic_carries_the_query_identity_not_its_text(self) -> None:
        # O ``finding_info`` identifica a analítica (nome + uid); o SQL inteiro
        # não cabe aqui. Quem filtra o statement herdado é o produtor — ver
        # ``test_statement_not_repeated_in_the_finding_event``.
        finding = _finding([_ROW])
        assert "statement" not in finding["finding_info"]
        assert finding["finding_info"]["analytic"]["uid"] == "1"


class TestByteBudget:
    """O ``analysisd`` trunca em silêncio acima de OS_MAXSTR (65536)."""

    def test_truncation_is_declared_in_the_event(self) -> None:
        rows = [{**_ROW, "Cmdline (Any)": f"x{i}" + "y" * 3000} for i in range(80)]
        finding = _finding(rows)

        unmapped = finding["unmapped"]
        assert unmapped["evidences_truncated"] is True
        assert unmapped["evidences_included"] < unmapped["evidences_total"]
        assert unmapped["evidences_dropped"] > 0
        assert (
            unmapped["evidences_included"] + unmapped["evidences_dropped"]
            == unmapped["evidences_total"]
        )

    def test_evidences_block_stays_under_budget(self) -> None:
        rows = [{**_ROW, "Cmdline (Any)": f"x{i}" + "y" * 3000} for i in range(80)]
        finding = _finding(rows)
        size = len(
            json.dumps(finding["evidences"], separators=(",", ":")).encode("utf-8")
        )
        assert size <= MAX_EVIDENCES_BYTES

    def test_aggregates_still_cover_dropped_rows(self) -> None:
        # Um ``count`` que só contasse o que coube contradiria o result_count do
        # SearchResult — e a discordância apareceria na triagem.
        rows = [{**_ROW, "Cmdline (Any)": f"x{i}" + "y" * 3000} for i in range(80)]
        finding = _finding(rows)
        assert finding["count"] == 160  # 80 linhas × Event Count 2
        assert len(finding["evidences"]) < 80

    def test_truncation_flag_present_even_when_false(self) -> None:
        # Ausência do campo obrigaria o consumidor a distinguir "não truncou" de
        # "produtor não informou".
        finding = _finding([_ROW])
        assert finding["unmapped"]["evidences_truncated"] is False

    def test_one_oversized_row_still_yields_one_evidence(self) -> None:
        # Zero evidências seria exatamente o silêncio que isto veio remover.
        finding = _finding([{**_ROW, "Cmdline (Any)": "z" * (MAX_EVIDENCES_BYTES * 2)}])
        assert len(finding["evidences"]) == 1

    def test_field_clip_marks_the_cut(self) -> None:
        evidence, _ = row_to_evidence({"Cmdline (Any)": "z" * 20000})
        cmd = evidence["process"]["cmd_line"]
        assert cmd.endswith("bytes]")
        assert len(cmd.encode("utf-8")) < 20000


class TestFindingIsValidOcsf:
    """Validado pelo gate do próprio repo, não por inspeção visual."""

    def test_class_identity(self) -> None:
        finding = _finding([_ROW])
        assert finding["class_uid"] == 2004  # Detection Finding
        assert finding["category_uid"] == 2  # Findings
        assert finding["activity_id"] == 1  # Create
        assert finding["type_uid"] == 200401  # class_uid * 100 + activity_id
        assert finding["status_id"] == 1  # New

    def test_passes_the_repo_structural_validator(self) -> None:
        from backend.app.collectors.normalize.ocsf.validator import (
            validate_normalized,
        )

        result = validate_normalized(_finding([_ROW]))
        assert result.valid is True, result.reason
        # ``missing_required`` é advisory (GATE-6) e não invalida o evento, mas
        # um achado sem os campos da classe não serviria a ninguém.
        assert result.reason == "ok", result.reason
        assert result.in_scope is True
        assert result.class_name == "Detection Finding"

    def test_carries_every_field_the_class_requires(self) -> None:
        finding = _finding([_ROW])
        for field in (
            "activity_id",
            "category_uid",
            "class_uid",
            "finding_info",
            "metadata",
            "severity_id",
            "time",
            "type_uid",
        ):
            assert field in finding, field


class TestBothEventsAreDispatched:
    """O 2004 é um evento A MAIS — o 1006 continua igual."""

    def _dispatch(self, items):
        from backend.app.collectors.tests.test_scheduled_query_alert import (
            _FakeIntegration,
            _FakePredefinedQuery,
            _FakeScheduledQuery,
            _FakeSearchResult,
        )

        captured: list[dict] = []

        def fake_enqueue(batch, *a, **k):
            captured.extend(batch)

        with patch(
            "backend.app.collectors.pipeline._enqueue_dispatch",
            fake_enqueue,
        ):
            from backend.app.collectors.scheduler_tasks import (
                _dispatch_scheduled_query_alert,
            )

            _dispatch_scheduled_query_alert(
                integration=_FakeIntegration(id=90, org_id=42),
                sched=_FakeScheduledQuery(id=2),
                query_def=_FakePredefinedQuery(id=1, title="Hunt de teste"),
                items=items,
                from_ts="2026-08-19T19:01:40Z",
                to_ts="2026-08-20T19:01:40Z",
                record=_FakeSearchResult(id=539),
            )
        return captured

    def test_job_event_and_finding_event(self) -> None:
        job, finding = self._dispatch([_ROW])

        assert job["normalized"]["class_uid"] == 1006
        assert job["_centralops"]["event_type"] == "centralops.scheduled_query.match"
        assert finding["normalized"]["class_uid"] == 2004
        assert (
            finding["_centralops"]["event_type"] == "centralops.scheduled_query.finding"
        )

    def test_finding_carries_the_table(self) -> None:
        _, finding = self._dispatch([_ROW])
        evidence = finding["normalized"]["evidences"][0]
        # O que faltava no evento entregue: quem, onde, e a linha de comando.
        assert evidence["process"]["cmd_line"] == "anydesk.exe --start-service"
        assert evidence["device"]["hostname"] == "HOST-0001"
        assert evidence["actor"]["user"]["name"] == "svc_test"

    def test_finding_survives_drop_raw(self) -> None:
        # A rota que entregou o evento do incidente tinha drop_raw ligado. O
        # detalhe tem de sobreviver a isso, que é o motivo de estar no
        # ``normalized`` e não no ``raw``.
        from backend.app.collectors.routing.engine import _without_raw

        _, finding = self._dispatch([_ROW])
        reduced = _without_raw(finding)
        assert reduced["_centralops"]["raw_dropped"] is True
        assert reduced["normalized"]["evidences"][0]["process"]["cmd_line"]

    def test_finding_survives_ocsf_only_payload(self) -> None:
        # Destino com payload="ocsf" entrega SÓ o normalized.
        _, finding = self._dispatch([_ROW])
        ocsf_only = finding["normalized"]
        assert ocsf_only["evidences"][0]["device"]["hostname"] == "HOST-0001"

    def test_distinct_event_ids_so_dedup_keeps_both(self) -> None:
        # Um id compartilhado faria o destino descartar o segundo como repetição
        # do primeiro — justamente o que carrega a tabela.
        job, finding = self._dispatch([_ROW])
        assert job["_centralops"]["event_id"] != finding["_centralops"]["event_id"]
        assert finding["_centralops"]["event_id"].endswith("-finding")

    def test_finding_inherits_tenant_labels(self) -> None:
        job, finding = self._dispatch([_ROW])
        for label in ("organization_id", "customer_id", "platform", "stream"):
            assert finding["_centralops"][label] == job["_centralops"][label]

    def test_statement_not_repeated_in_the_finding_event(self) -> None:
        # O 1006 carrega o statement inteiro (uma hunt real passa de 4 KiB). O
        # 2004 herdava o mesmo ``unmapped`` e repetia o texto no fio, sem
        # informação nova, comendo a margem sob o OS_MAXSTR do Wazuh.
        job, finding = self._dispatch([_ROW])
        assert "statement" in job["normalized"]["unmapped"]
        assert "statement" not in finding["normalized"]["unmapped"]
        assert "SELECT" not in json.dumps(finding)
        # A trilha até o SQL continua no evento.
        assert finding["normalized"]["unmapped"]["search_result_id"] == 539
        assert finding["normalized"]["finding_info"]["analytic"]["uid"] == "1"

    def test_job_event_still_carries_raw_items(self) -> None:
        # Compatibilidade: quem consome o 1006 hoje não vê diferença.
        job, _ = self._dispatch([_ROW])
        assert job["raw"]["items"] == [_ROW]
