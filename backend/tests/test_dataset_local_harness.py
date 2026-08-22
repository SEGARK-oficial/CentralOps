"""Harness opt-in: exercita o detector in-flight contra eventos REAIS.

O que este arquivo acrescenta ao que já existe
----------------------------------------------
As fixtures de benchmark versionadas (``app/collectors/tests/benchmarks/fixtures``)
são **um evento sintético** por (vendor, event_type, tamanho), escolhido a dedo.
Elas provam corretude e servem de baseline estável — e continuam sendo a única
coisa commitada.

O que elas não conseguem provar é **distribuição**: qual a cara do tráfego real,
quantos eventos estouram um teto, quantas chaves distintas um ``group_by`` produz
num ciclo. Isso só existe em volume, e volume real não pode ser versionado (ver
``dataset_local``). Daí este harness: mede a distribuição na máquina de quem tem
o dataset, e é pulado em todo lugar onde ele não existe.

Nenhum assert aqui pina EPS absoluto. Esse repo já registrou o falso-positivo:
o baseline de ``benchmark.yml`` foi medido em arm64 e o runner de CI é x86_64
compartilhado, 2-4x mais lento, e por isso aquele job é ``continue-on-error``.
Os tetos abaixo miram **regressão estrutural** — um ``frozenset`` virando lista,
o short-circuit se perdendo, um ``split`` de dot-path voltando para o caminho por
evento — e por isso são deliberadamente frouxos.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend.app.collectors.inflight.matcher import (
    CompiledClause,
    CompiledInflightRule,
    CompiledRuleSet,
    evaluate_ruleset,
)
from backend.app.core.config import settings

# ── Localizador do dataset (inline: ``tests/`` não é pacote, então um módulo
# irmão exigiria import por sys.path — frágil e sem ganho para um só consumidor)
#: Nome da variável de ambiente. Absoluta de propósito: derivar o caminho de
#: ``Path(__file__)`` quebraria na imagem Cython (o ``.py`` não existe) e
#: apontaria para dentro do repo, que é exatamente onde o dado não pode estar.
ENV_VAR = "CENTRALOPS_DATASET_DIR"

#: Layout escrito pelo coletor local: ``org-<id>/<event_type>/<AAAAMMDD-HH>.ndjson``.
_SHARD_GLOB = "org-*/{stream}/*.ndjson"

SKIP_REASON = (
    f"{ENV_VAR} não definida — este teste precisa do dataset local de eventos "
    f"reais, que por construção não é versionado. Aponte a variável para o "
    f"diretório 'ds-bruto' (ex.: export {ENV_VAR}=~/dataset-local/ds-bruto)."
)


def dataset_root() -> Optional[Path]:
    """Raiz do dataset, ou ``None`` quando não configurado/ausente.

    ``None`` cobre os três casos que devem virar *skip* e não falha: variável
    ausente, variável vazia, e caminho que não existe mais (dataset movido ou
    apagado — ele é descartável de propósito).
    """
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    return root if root.is_dir() else None


def requires_dataset() -> Path:
    """Devolve a raiz ou **pula** o teste. Use no corpo, não no import."""
    root = dataset_root()
    if root is None:
        pytest.skip(SKIP_REASON)
    return root


def shards(stream: str, *, root: Optional[Path] = None) -> list[Path]:
    """Arquivos do ``stream``, em ordem cronológica (o nome já ordena)."""
    base = root or requires_dataset()
    return sorted(base.glob(_SHARD_GLOB.format(stream=stream)))


def iter_events(
    stream: str,
    *,
    limit: int,
    root: Optional[Path] = None,
    skip_malformed: bool = True,
) -> Iterator[dict[str, Any]]:
    """Percorre até ``limit`` eventos do ``stream``, **linha a linha**.

    Streaming não é preciosismo aqui: o maior shard do dataset de referência tem
    41 MB e o registry de fixtures dos benchmarks (``benchmarks/conftest.py``) é
    *session-scoped* e carrega tudo em memória. Copiar aquele padrão para cá
    faria uma sessão de teste segurar centenas de MB por nada — os testes
    consomem os eventos em sequência e descartam.

    ``limit`` é obrigatório e sem default: um percurso ilimitado sobre 120k
    eventos num teste é um travamento disfarçado de lentidão.
    """
    if limit <= 0:
        return
    seen = 0
    for shard in shards(stream, root=root):
        with shard.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Um shard pode ter sido cortado no meio se o coletor foi
                    # morto durante a escrita. Isso é ruído esperado do dataset,
                    # não defeito do produto — mas quem estuda robustez de parse
                    # pode querer ver, por isso é opção e não regra.
                    if skip_malformed:
                        continue
                    raise
                yield event
                seen += 1
                if seen >= limit:
                    return


pytestmark = pytest.mark.dataset

#: Stream de maior volume do dataset de referência. Escolhido por ser o pior caso
#: de cardinalidade de ``group_by`` medido (ver o teste de cardinalidade abaixo).
STREAM = "sophos.siem_event"

#: Tamanho de lote do pipeline. O teto de chaves por regra é POR CICLO, então
#: medir cardinalidade sobre o dataset inteiro responderia a pergunta errada.
CYCLE = 200


def _ruleset(clauses_por_regra: int, n_regras: int, *, group_by=None) -> CompiledRuleSet:
    """Ruleset no ORÇAMENTO MÁXIMO permitido pelo guard de boot.

    Não é cenário pessimista inventado: é literalmente o teto que
    ``INFLIGHT_MAX_RULES_PER_CYCLE * INFLIGHT_MAX_WHERE_CLAUSES <= 500``
    autoriza. Medir abaixo dele mediria um sistema que o operador pode
    legitimamente sair da configuração.
    """
    rules = tuple(
        CompiledInflightRule(
            rule_id=i,
            name=f"r{i}",
            severity_id=4,
            suppression_window_seconds=300,
            group_by_path=group_by,
            # Primeira cláusula compartilhada entre todas as regras — é o padrão
            # real de SOC (discriminar por vendor/event_type antes de tudo) e é o
            # que liga o cache de path do ``evaluate_ruleset``.
            clauses=tuple(
                [CompiledClause(("raw", "type"), "eq", "__nunca_casa__")]
                + [
                    CompiledClause(("raw", f"campo_{j}"), "eq", f"v{j}")
                    for j in range(clauses_por_regra - 1)
                ]
            ),
        )
        for i in range(n_regras)
    )
    return CompiledRuleSet(rules=rules, share_paths=True)


def test_matcher_throughput_on_real_events():
    """Custo por evento sobre a FORMA real, no orçamento máximo de regras.

    A forma importa e é o motivo de este teste existir: o evento sintético de
    fixture tem ~20 chaves de topo rasas, enquanto o evento real carrega um
    sub-dicionário de dezenas de campos. ``_resolve`` navega dicts, então a
    profundidade e a largura reais são exatamente a variável que a fixture
    versionada não representa.
    """
    requires_dataset()
    eventos = [{"raw": e} for e in iter_events(STREAM, limit=2000)]
    if len(eventos) < 100:
        pytest.skip(f"dataset tem só {len(eventos)} eventos de {STREAM}; mínimo 100")

    rs = _ruleset(
        clauses_por_regra=int(settings.INFLIGHT_MAX_WHERE_CLAUSES),
        n_regras=int(settings.INFLIGHT_MAX_RULES_PER_CYCLE),
    )

    inicio = time.perf_counter()
    for ev in eventos:
        evaluate_ruleset(ev, rs)
    us = (time.perf_counter() - inicio) / len(eventos) * 1e6

    print(f"\n[dataset] {STREAM}: {us:.1f} µs/evento no orçamento máximo "
          f"({len(rs.rules)} regras × {len(rs.rules[0].clauses)} cláusulas), "
          f"n={len(eventos)}")

    # Teto ~5x sobre o medido no par sintético equivalente, na mesma disciplina do
    # teste de orçamento já existente. Estourar aqui é sinal de regressão
    # ESTRUTURAL — ou de máquina ocupada; confira localmente antes de acreditar.
    assert us < 600, (
        f"{us:.1f} µs/evento excede o teto frouxo de 600 µs. Ou uma regressão "
        f"estrutural entrou no avaliador (short-circuit perdido, path voltando a "
        f"ser split por evento, frozenset virando lista), ou esta máquina está "
        f"sob carga. Rode de novo em repouso antes de tratar como regressão."
    )


def test_empty_ruleset_stays_free_on_real_events():
    """Par NEGATIVO obrigatório do teste acima.

    Sem ele, o teto de 600 µs passaria igualmente se o avaliador não estivesse
    fazendo trabalho nenhum. Este prova que o caminho "sem regra ativa" é de fato
    gratuito (R2: custo zero quando desligado) e, por contraste, que o número
    acima mediu trabalho real.
    """
    requires_dataset()
    eventos = [{"raw": e} for e in iter_events(STREAM, limit=1000)]
    if len(eventos) < 100:
        pytest.skip("dataset insuficiente")

    vazio = CompiledRuleSet(rules=(), share_paths=False)
    inicio = time.perf_counter()
    for ev in eventos:
        evaluate_ruleset(ev, vazio)
    us = (time.perf_counter() - inicio) / len(eventos) * 1e6

    assert us < 5, f"ruleset vazio custa {us:.2f} µs/evento — R2 diz que deve ser ~0"


def test_group_by_cardinality_against_the_per_cycle_cap():
    """Mede o que só o volume real responde: o teto de chaves por ciclo aperta?

    Este teste NÃO falha quando o teto estoura — estourar é comportamento
    projetado (o teto existe justamente para conter cardinalidade explosiva) e a
    taxa depende do par (stream, group_by) que o cliente escolher. Ele existe
    para tornar o número VISÍVEL, porque a consequência de estourar é perder
    chaves silenciosamente, e ninguém deveria descobrir isso em produção.
    """
    requires_dataset()
    cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)

    ciclos = estouros = maior = 0
    atual: set[str] = set()
    n = 0
    for ev in iter_events(STREAM, limit=20_000):
        valor = ev.get("endpoint_id")
        if valor is not None:
            atual.add(str(valor))
        n += 1
        if n >= CYCLE:
            ciclos += 1
            maior = max(maior, len(atual))
            if len(atual) > cap:
                estouros += 1
            atual, n = set(), 0

    if ciclos == 0:
        pytest.skip("dataset insuficiente para um ciclo completo")

    pct = 100.0 * estouros / ciclos
    print(f"\n[dataset] {STREAM} group_by=endpoint_id: {ciclos} ciclos de {CYCLE} "
          f"eventos, máx {maior} chaves/ciclo, {estouros} ciclos ({pct:.1f}%) "
          f"acima do teto de {cap}")

    # Anti-vacuidade: se o campo sumir do stream, o laço acima roda inteiro sem
    # medir nada e o teste ficaria verde sem ter observado uma única chave.
    assert maior > 0, (
        "nenhuma chave de group_by observada — o campo mudou de nome no vendor "
        "ou o dataset é de outro stream. O teste não mediu nada."
    )


def test_group_value_truncation_rate_on_real_events():
    """Quantos valores reais de ``group_by`` passam do teto de comprimento.

    É o número que motivou o digest na chave de dedup: acima do teto, o token
    truncado deixa de ser injetivo e dois valores distintos colapsam na mesma
    Detection. Aqui a taxa é medida na fonte, não estimada.
    """
    requires_dataset()
    cap = int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)

    total = acima = 0
    prefixos: dict[str, str] = {}
    colisoes = 0
    for ev in iter_events(STREAM, limit=20_000):
        for campo in ("endpoint_id", "name", "location"):
            valor = ev.get(campo)
            if not isinstance(valor, str):
                continue
            total += 1
            if len(valor) > cap:
                acima += 1
                pref = valor[:cap]
                anterior = prefixos.get(pref)
                if anterior is not None and anterior != valor:
                    colisoes += 1
                prefixos[pref] = valor

    if total == 0:
        pytest.skip("nenhum campo string candidato a group_by neste stream")

    print(f"\n[dataset] {STREAM}: {total} valores candidatos, {acima} acima de "
          f"{cap} chars, {colisoes} colisões de prefixo")

    # O produto não promete taxa nenhuma; o que ele promete é que colisão de
    # prefixo NÃO vira Detection fundida. Se houver colisão medida aqui, ela tem
    # de estar coberta pelo digest — o teste de injetividade em
    # test_adr0015_inflight_matcher.py é quem prova isso.
    assert total > 0


# ── Meta-teste: o harness precisa ser exercitado, não só pulado ───────────────

def test_harness_actually_runs_when_pointed_at_a_directory(tmp_path, monkeypatch):
    """Prova que o caminho FELIZ funciona, não só o skip.

    Sem isto, a suíte inteira deste arquivo poderia estar quebrada e ninguém
    saberia: em CI ela é 100% pulada, e um teste pulado é verde. Este caso monta
    um dataset mínimo sintético no ``tmp_path`` — dois eventos inventados, zero
    dado real — e prova que o carregador encontra, percorre e devolve.
    """
    shard = tmp_path / "org-1" / "stream.x"
    shard.mkdir(parents=True)
    (shard / "20260101-00.ndjson").write_text(
        '{"id":"a","v":1}\n{"id":"b","v":2}\n', encoding="utf-8"
    )
    monkeypatch.setenv(ENV_VAR, str(tmp_path))

    assert dataset_root() == tmp_path
    eventos = list(iter_events("stream.x", limit=10))
    assert [e["id"] for e in eventos] == ["a", "b"]

    # ``limit`` é teto de verdade, não sugestão.
    assert len(list(iter_events("stream.x", limit=1))) == 1


def test_harness_skips_cleanly_without_the_env_var(monkeypatch):
    """O par negativo: ausência do dataset é skip legível, nunca falha nem
    passagem vazia."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert dataset_root() is None

    with pytest.raises(BaseException) as exc:  # pytest.skip levanta Skipped
        requires_dataset()
    assert "dataset" in str(exc.value).lower()


def test_harness_treats_a_missing_directory_as_absent(tmp_path, monkeypatch):
    """Dataset apagado/movido não pode virar erro críptico de FileNotFound no
    meio de um laço — o dataset é descartável por natureza."""
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "nao-existe"))
    assert dataset_root() is None
