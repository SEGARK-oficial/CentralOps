"""Smoke E2E: Rfc3164JsonClient + validação JSON_Decoder Wazuh.

Estratégia de três camadas:

  1. Servidor TCP local (asyncio.start_server) — captura linhas reais sem
     depender de rede externa. Cobre framing LF, formato RFC 3164, e
     compatibilidade com o prematch ``^{`` do JSON_Decoder do Wazuh.

  2. Simulação do parser Wazuh — extrai o MSG após ``APP[pid]: `` e verifica
     que o JSON_Decoder (prematch ``^{``) casaria em todos os eventos.

  3. Smoke condicional contra Wazuh real (192.168.3.211:514) — só roda se o
     host estiver acessível via TCP dentro de 2s; caso contrário, pula com
     mensagem explicativa. Não exige acesso root no Wazuh remoto.

Por que esses 5 eventos?
  Cobrem os quatro vendors integrados (Sophos, Microsoft Defender, NinjaOne)
  e a faixa de severity_id (1 = info, 5 = critical) que importa para as
  regras Wazuh em local_rules.xml. Cada um usa build_envelope para garantir
  que o envelope canônico real seja formatado — não stubs inventados.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from typing import Any, Dict, List, Optional

import pytest

from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope
from backend.app.collectors.output.rfc3164_sender import (
    PRI_INFO,
    Rfc3164JsonClient,
    format_rfc3164,
)

# ── Padrão RFC 3164 esperado ────────────────────────────────────────────────

# Prefixo obrigatório: <PRI>Mmm dd HH:MM:SS hostname centralops[pid]:
# Grupos: (timestamp)(hostname)(app)(pid)(msg)
# PRI varia por severity_id (info=134, critical=130, ...) — o decoder Wazuh
# só exige ``<NNN>``, não um valor fixo. Casar ``<134>`` literal quebrava
# para qualquer evento não-info (ex.: sophos.alert critical → <130>).
_RFC3164_RE = re.compile(
    r"^<\d{1,3}>"
    r"([A-Z][a-z]{2})\s+(\d{1,2})\s(\d{2}:\d{2}:\d{2})"   # timestamp
    r"\s(\S+)"                                                  # hostname
    r"\s(centralops)\[(\d+)\]:\s"                              # app[pid]:
    r"(.*)$"                                                    # MSG
)

# Prematch usado pelo Wazuh JSON_Decoder nativo (0006-json_decoders.xml).
_WAZUH_JSON_PREMATCH = re.compile(r"^\{")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_envelope(
    vendor: str,
    event_type: str,
    stream: str,
    raw: Dict[str, Any],
    normalized: Dict[str, Any],
    customer_id: int = 7,
    integration_id: int = 1,
) -> Dict[str, Any]:
    """Constrói envelope canônico via build_envelope (caminho real de produção)."""
    ctx = EnvelopeContext(
        vendor=vendor,
        integration_id=integration_id,
        customer_id=customer_id,
        stream=stream,
        event_type=event_type,
        mapping_version_id="v1-smoke",
        collector_host=socket.gethostname() or "centralops-test",
    )
    return build_envelope(
        raw=raw,
        normalized=normalized,
        ctx=ctx,
        vendor_msg_id=raw.get("id"),
    )


def _extract_wazuh_msg(line: str) -> str:
    """Simula como o Wazuh isola o MSG depois do header syslog.

    O Wazuh remove o cabeçalho RFC 3164 antes de aplicar decoders.
    Em RFC 3164 tudo após ``AppName[pid]: `` é o MSG que o JSON_Decoder vê.
    """
    m = re.match(
        r"^<\d+>\w+\s+\d+\s\d+:\d+:\d+\s\S+\s\S+\[\d+\]:\s*(.*)$",
        line,
    )
    return m.group(1) if m else ""


def _assert_valid_rfc3164_line(line: str, event_label: str) -> Dict[str, Any]:
    """Valida estrutura RFC 3164 completa e retorna o JSON parseado.

    O que valida:
      - Prefixo <134> (facility=local0, severity=info)
      - Timestamp "Mmm dd HH:MM:SS" incluindo dia com espaço para 1 dígito
      - Hostname presente
      - APP[pid]: centralops[<int>]
      - MSG começa com { (prematch Wazuh JSON_Decoder)
      - MSG é JSON válido
      - JSON contém _centralops.vendor, _centralops.customer_id,
        normalized.class_uid, normalized.severity_id

    O que NÃO valida (fora do escopo deste smoke):
      - Que o timestamp está correto em relação ao horário real
      - Que o hostname bate com o host de produção
      - Que o Wazuh realmente recebeu e indexou o evento (ver camada 3)
    """
    m = _RFC3164_RE.match(line)
    assert m is not None, (
        f"[{event_label}] Linha não casa com RFC 3164: {line[:100]!r}"
    )

    msg = m.group(7)
    assert msg.startswith("{"), (
        f"[{event_label}] MSG não começa com '{{' — Wazuh JSON_Decoder não casaria. "
        f"msg_start={msg[:40]!r}"
    )

    try:
        payload = json.loads(msg)
    except json.JSONDecodeError as exc:
        pytest.fail(f"[{event_label}] MSG não é JSON válido: {exc} | msg={msg[:80]!r}")

    # Campos obrigatórios para regras Wazuh em local_rules.xml
    meta = payload.get("_centralops", {})
    assert meta.get("vendor"), f"[{event_label}] _centralops.vendor ausente"
    assert meta.get("customer_id") is not None, (
        f"[{event_label}] _centralops.customer_id ausente"
    )

    norm = payload.get("normalized", {})
    assert norm.get("class_uid") is not None, (
        f"[{event_label}] normalized.class_uid ausente"
    )
    assert norm.get("severity_id") is not None, (
        f"[{event_label}] normalized.severity_id ausente"
    )

    return payload


# ── Fixtures dos 5 eventos representativos ──────────────────────────────────

def _events_fixture() -> List[Dict[str, Any]]:
    """5 eventos OCSF representativos dos vendors integrados.

    Escolhidos por cobrir:
      - Todos os vendors ativos em prod (sophos, microsoft_defender, ninjaone)
      - Faixa de severity_id que aciona regras Wazuh (1=info, 3=medium,
        4=high, 5=critical)
      - Diferentes event_types que mapeiam para rules distintas
    """
    return [
        # 1. Sophos alert crítico — aciona rule 100199 (critical catch-all)
        _make_envelope(
            vendor="sophos",
            event_type="sophos.alert",
            stream="alerts",
            raw={"id": "smoke-sophos-alert-1", "severity": "Critical",
                 "type": "malware"},
            normalized={"class_uid": 2004, "severity_id": 5,
                         "finding_info": {"uid": "smoke-sophos-alert-1",
                                          "title": "Malware detectado"}},
        ),
        # 2. Sophos case (moderado) — não aciona critical, mas indexa
        _make_envelope(
            vendor="sophos",
            event_type="sophos.case",
            stream="cases",
            raw={"id": "smoke-sophos-case-1", "status": "open"},
            normalized={"class_uid": 2001, "severity_id": 3,
                         "finding_info": {"uid": "smoke-sophos-case-1",
                                          "title": "Case aberto"}},
        ),
        # 3. Defender alert (high) — aciona rule 100100 área Defender
        _make_envelope(
            vendor="microsoft_defender",
            event_type="defender.alert",
            stream="alerts",
            raw={"id": "smoke-defender-alert-1", "severity": "High"},
            normalized={"class_uid": 2004, "severity_id": 4,
                         "finding_info": {"uid": "smoke-defender-alert-1",
                                          "title": "Defender high alert"}},
        ),
        # 4. Defender incident resolvido — aciona rule 100110
        _make_envelope(
            vendor="microsoft_defender",
            event_type="defender.incident",
            stream="incidents",
            raw={"id": "smoke-defender-inc-1", "status": "resolved"},
            normalized={"class_uid": 2001, "severity_id": 4,
                         "finding_info": {"uid": "smoke-defender-inc-1",
                                          "title": "Incident resolvido"}},
        ),
        # 5. NinjaOne activity (info) — indexa, não aciona alertas
        _make_envelope(
            vendor="ninjaone",
            event_type="ninjaone.activity",
            stream="activities",
            raw={"id": "smoke-ninja-1", "activityType": "DEVICE_REBOOT"},
            normalized={"class_uid": 3002, "severity_id": 1,
                         "finding_info": {"uid": "smoke-ninja-1",
                                          "title": "Device reboot"}},
        ),
    ]


# ── Camada 1: servidor TCP local ─────────────────────────────────────────────

async def _run_tcp_capture(events: List[Dict[str, Any]]) -> List[str]:
    """Sobe servidor TCP efêmero, envia batch via Rfc3164JsonClient, captura linhas."""
    received_data: List[bytes] = []
    server_ready = asyncio.Event()

    async def handle_connection(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            data = await asyncio.wait_for(
                reader.read(65536),
                timeout=5.0,
            )
            received_data.append(data)
        finally:
            writer.close()

    # Porta 0 = OS escolhe porta efêmera livre
    server = await asyncio.start_server(
        handle_connection,
        host="127.0.0.1",
        port=0,
    )
    port = server.sockets[0].getsockname()[1]

    async with server:
        client = Rfc3164JsonClient(host="127.0.0.1", port=port)
        try:
            await client.send_batch(events)
        finally:
            await client.close()
        # Aguarda o handler processar
        await asyncio.sleep(0.1)

    # Decodifica e split por LF
    raw_bytes = b"".join(received_data)
    lines = [
        ln for ln in raw_bytes.decode("utf-8").split("\n")
        if ln.strip()
    ]
    return lines


@pytest.mark.asyncio
async def test_rfc3164_smoke_local_tcp_all_events() -> None:
    """Smoke E2E — envia 5 eventos reais via socket TCP local e valida cada linha.

    Valida: framing LF, formato RFC 3164 completo, prematch Wazuh (^{),
    JSON válido, campos _centralops.vendor/.customer_id e
    normalized.class_uid/.severity_id presentes em todos.

    NÃO valida: que o Wazuh remoto recebeu ou indexou; regras Wazuh;
    comportamento de TLS (coberto em test_rfc3164_sender.py).
    """
    events = _events_fixture()
    lines = await _run_tcp_capture(events)

    assert len(lines) == len(events), (
        f"Esperado {len(events)} linhas, recebido {len(lines)}. "
        f"Linhas: {lines!r}"
    )

    labels = [
        "sophos.alert(critical)",
        "sophos.case(medium)",
        "defender.alert(high)",
        "defender.incident(resolved)",
        "ninjaone.activity(info)",
    ]
    for line, label in zip(lines, labels):
        _assert_valid_rfc3164_line(line, label)


@pytest.mark.asyncio
async def test_rfc3164_lf_framing_between_events() -> None:
    """Cada evento deve ser separado por LF — Wazuh syslog input depende disso.

    Valida: exatamente N eventos em N linhas (split por \\n).
    NÃO valida: conteúdo dos eventos — coberto por test_*_all_events.
    """
    events = _events_fixture()
    lines = await _run_tcp_capture(events)
    assert len(lines) == 5, (
        f"Framing LF incorreto — esperado 5 linhas, obtido {len(lines)}"
    )


# ── Camada 2: simulação Wazuh JSON_Decoder ───────────────────────────────────

def test_wazuh_json_decoder_would_match_all_events() -> None:
    """Simula o prematch do JSON_Decoder do Wazuh (^{) para todos os eventos.

    Wazuh remove o header syslog e aplica ``prematch: ^{`` antes de tentar
    decodificar como JSON. Este teste garante que nenhum evento produz um
    MSG que quebraria esse prematch.

    Valida: MSG extraído via regex RFC 3164 começa com '{' para todos os
    5 eventos representativos.
    NÃO valida: que o Wazuh está de fato rodando; que a regra específica
    casa; que os campos OCSF estão corretos (coberto em camada 1).
    """
    events = _events_fixture()
    labels = [
        "sophos.alert", "sophos.case", "defender.alert",
        "defender.incident", "ninjaone.activity",
    ]
    for event, label in zip(events, labels):
        line = format_rfc3164(event)
        msg = _extract_wazuh_msg(line)

        assert msg, (
            f"[{label}] _extract_wazuh_msg retornou vazio — "
            f"regex não casou na linha: {line[:80]!r}"
        )
        assert _WAZUH_JSON_PREMATCH.match(msg), (
            f"[{label}] Wazuh JSON_Decoder NÃO casaria (prematch ^{{): "
            f"msg_start={msg[:40]!r}"
        )

        # Confirma que é JSON parseável (não apenas começa com {)
        try:
            json.loads(msg)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"[{label}] MSG não é JSON válido após extração: {exc}"
            )


def test_wazuh_json_decoder_extracts_msg_correctly() -> None:
    """Verifica que _extract_wazuh_msg isola corretamente o MSG para decoder.

    Valida: a função retorna tudo após "app[pid]: ", sem o header syslog.
    NÃO valida: conteúdo do JSON — coberto em outros testes.
    """
    # Linha RFC 3164 sintética com conteúdo controlado
    synthetic_line = (
        '<134>Apr 25 14:32:01 worker-1 centralops[42]: '
        '{"_centralops":{"vendor":"smoke"},"normalized":{},"raw":{}}'
    )
    msg = _extract_wazuh_msg(synthetic_line)
    assert msg == '{"_centralops":{"vendor":"smoke"},"normalized":{},"raw":{}}', (
        f"MSG extraído incorreto: {msg!r}"
    )


# ── Camada 3: smoke condicional contra Wazuh real ────────────────────────────

async def _can_reach_wazuh(
    host: str = "192.168.3.211",
    port: int = 514,
    timeout: float = 2.0,
) -> bool:
    """Testa conectividade TCP ao Wazuh. Retorna False se unreachable."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except (asyncio.TimeoutError, OSError):
        return False


@pytest.mark.asyncio
async def test_smoke_against_real_wazuh_if_reachable() -> None:
    """Smoke real contra Wazuh prod (192.168.3.211:514) — skip se unreachable.

    Envia 1 evento sintético com event_type='centralops.smoke.rfc3164.test'.
    Para verificar recebimento no Wazuh após rodar:

        ssh wazuh-host "tail -200 /var/ossec/logs/archives/archives.json \
          | grep centralops.smoke"

    Valida: conexão TCP e envio sem exceção.
    NÃO valida: que o Wazuh indexou ou gerou alerta (requer acesso remoto).
    """
    wazuh_host = "192.168.3.211"
    wazuh_port = 514

    reachable = await _can_reach_wazuh(wazuh_host, wazuh_port)
    if not reachable:
        pytest.skip(
            f"Wazuh {wazuh_host}:{wazuh_port} unreachable — "
            "rodar manualmente quando staging estiver up. "
            "Ver docs/collector/testing/wazuh-smoke.md para instruções."
        )

    # Evento sintético de smoke — grep-ável nos archives
    smoke_event = _make_envelope(
        vendor="centralops",
        event_type="centralops.smoke.rfc3164.test",
        stream="smoke",
        raw={"id": "smoke-real-wazuh-1", "note": "smoke test"},
        normalized={"class_uid": 9999, "severity_id": 1,
                     "finding_info": {"uid": "smoke-real-wazuh-1",
                                      "title": "CentralOps smoke test"}},
        customer_id=0,
        integration_id=0,
    )

    client = Rfc3164JsonClient(host=wazuh_host, port=wazuh_port)
    try:
        await client.send_batch([smoke_event])
    finally:
        await client.close()

    # Linha de exemplo para o operador
    example_line = format_rfc3164(smoke_event)
    print(
        f"\n[smoke-real-wazuh] Evento enviado. "
        f"Para verificar no Wazuh:\n"
        f"  ssh wazuh-host \"tail -200 /var/ossec/logs/archives/archives.json "
        f"| grep centralops.smoke\"\n"
        f"\nLinha enviada (truncada):\n  {example_line[:120]}..."
    )
