"""Quem SOBREVIVE ao teto de regras em voo passa a ser decisão do operador.

O QUE ESTAVA ERRADO. ``list_inflight_for_org`` cortava em
``INFLIGHT_MAX_RULES_PER_CYCLE`` sobre ``ORDER BY id ASC``. Com 200 regras
criadas (``CORRELATION_MAX_RULES_PER_ORG``) e teto 50, rodavam as 50 mais
ANTIGAS — e a regra 51, recém-escrita, era exatamente a que o operador estava
testando. Ele a via habilitada na lista e nada acontecia.

POR QUE NÃO ``id DESC``. O ``id ASC`` não era arbitrário: a ordem determinística
existe para que duas réplicas de worker avaliem as regras na MESMA ordem —
ordens diferentes produzem Detections com ``first_seen`` divergente sob
concorrência. Inverter para ``id DESC`` preservaria o determinismo e trocaria a
vítima: passaria a matar as regras ANTIGAS e estáveis, que é pior — as novas
pelo menos alguém está olhando.

A ESCOLHA. Coluna nova ``correlation_rules.eval_priority`` (INTEGER NOT NULL
DEFAULT 0) e ordem ``eval_priority DESC, id ASC``:

* DETERMINÍSTICA e TOTAL — ``id`` é único, então não existe par de linhas cuja
  ordem relativa o banco possa escolher;
* DESEMPATE ESTÁVEL — mesma prioridade cai no ``id ASC``, que não alterna;
* DEFAULT NEUTRO — empatadas em 0 (toda linha, nova ou migrada), a expressão
  degenera em ``id ASC``, byte-idêntica ao comportamento anterior. Nenhuma
  instalação muda de conjunto avaliado sem alguém digitar um número;
* OBSERVÁVEL — quem foi cortado continua contado (``count_inflight_for_org`` e
  ``rules_rejected{reason="truncated"}``) e agora é NOMEADO no aviso, lido pela
  MESMA cláusula de ordenação que escolheu as sobreviventes.

O QUE FOI RECUSADO, e por quê:

* reusar ``severity_id`` — descreve a SAÍDA, não a prioridade de execução; seu
  domínio é OCSF, onde ``99`` (Other) e ``0`` (Unknown) ordenam ACIMA de ``6``
  (Fatal), logo um ``ORDER BY`` numérico erraria justamente nos dois valores
  que significam "não sei". E acoplaria "quão alto o alerta grita" a "a regra
  chega a rodar": baixar a severidade de uma regra barulhenta a tiraria da
  avaliação em silêncio — o mesmo modo de falha mudo, com outra roupa;
* reusar ``updated_at`` — não é único (duas escritas no mesmo tick empatam sem
  desempate) e faria o conjunto avaliado mudar só porque alguém editou o
  ``description`` de uma regra;
* recusar a CRIAÇÃO acima do teto — o teto de avaliação é do WORKER
  (``INFLIGHT_MAX_RULES_PER_CYCLE`` no ``.env`` dele) e a criação é da API,
  outro processo, outro ``.env``: a API recusaria com 409 por um número que o
  worker pode não ter. Além de proibir o fluxo legítimo "escrevo desabilitada
  hoje, habilito depois" e de não ajudar em nada quem JÁ tem 200 regras.

Nada aqui lê fonte: todo teste executa a consulta real, a carga real ou a
migração real. Sem ``source_only`` de propósito — na imagem Cython o fonte não
existe, e a ordem de avaliação existe igual.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import logging

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight.runtime import (
    CUT_RULES_NAMED_IN_LOG,
    load_inflight_rules_for_org,
)
from backend.app.core.config import settings
from backend.app.db import database as _db_module
from backend.app.db import models, repository
from backend.app.db.models import Base

ORG_ID = 1
WHERE_OK = '[{"field":"a","op":"eq","value":"x"}]'

GAUGE = "collector_inflight_rules_loaded"
CONTADOR = "collector_inflight_rules_rejected_total"


# ── infra ───────────────────────────────────────────────────────────────────


@pytest.fixture
def sessao():
    """SQLite em memória com o schema real. Devolve o ``sessionmaker``."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _db_module.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as db:
        db.add(models.Organization(id=ORG_ID, name="Org", slug="org"))
        db.commit()
    yield Session
    engine.dispose()


def _semear(Session, especificacoes: list[tuple[int, int]], **extra) -> None:
    """Insere regras ``(id, eval_priority)`` NA ORDEM DADA.

    ATENÇÃO — uma versão anterior deste docstring afirmava que semear ids fora
    de ordem fazia a ordem física divergir da esperada, e que isso protegia
    contra um ``ORDER BY`` quebrado passar por acidente. **É falso, e foi
    medido.**

    ``Detection.id`` é ``INTEGER PRIMARY KEY``, que no SQLite é *alias de
    ``rowid``* — a ordem física é a ordem do id, não a de inserção. Inserindo
    ``90, 30, 70, 10, 50``, um ``SELECT`` sem ``ORDER BY`` devolve
    ``10, 30, 50, 70, 90``. O desempate por ``id ASC`` pode ser REMOVIDO do
    ``_inflight_eval_order()`` e três testes deste arquivo continuam verdes,
    inclusive o que existe só para prová-lo.

    Quem realmente guarda o desempate aqui é
    ``test_survivors_and_cut_are_ordered_by_the_same_clause``, que lê o SQL
    compilado, mais o teste marcado ``pg`` no fim do arquivo — e é no Postgres
    que a ausência do desempate morde de verdade, porque lá um seq scan não dá
    garantia nenhuma de ordem e duas réplicas produziriam ``first_seen``
    divergente.

    Semear fora de ordem continua sendo bom hábito; só não é a proteção que o
    texto anterior prometia.
    """
    with Session() as db:
        for rule_id, prioridade in especificacoes:
            db.add(
                models.CorrelationRule(
                    id=rule_id,
                    organization_id=ORG_ID,
                    name=f"regra-{rule_id}",
                    enabled=extra.get("enabled", True),
                    eval_mode=extra.get("eval_mode", "inflight"),
                    eval_priority=prioridade,
                    where_json=WHERE_OK,
                )
            )
        db.commit()


def _ids(regras) -> list[int]:
    return [r.id for r in regras]


def _ids_compiladas(ruleset) -> list[int]:
    """A regra COMPILADA guarda o id em ``rule_id`` (``id`` seria ambíguo numa
    dataclass que também carrega ``name``)."""
    return [r.rule_id for r in ruleset.rules]


def _sobreviventes(Session, cap: int) -> list[int]:
    with Session() as db:
        return _ids(repository.CorrelationRuleRepository(db).list_inflight_for_org(
            ORG_ID, limit=cap
        ))


def _cortadas(Session, cap: int, max_rows: int = 500) -> list[int]:
    with Session() as db:
        return _ids(repository.CorrelationRuleRepository(db).list_inflight_cut_for_org(
            ORG_ID, limit=cap, max_rows=max_rows
        ))


@pytest.fixture
def emitidos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict, float]]:
    """``(nome, attrs, valor)`` de tudo que a carga empurra para o OTel.

    ARMADILHA herdada dos vizinhos: ``OTEL_ENABLED`` é ``False`` por default e
    ``count``/``set_gauge`` viram no-op ANTES de tocar o instrumento. A espionagem
    é sobre estas funções, nunca sobre o instrumento.
    """
    capturado: list[tuple[str, dict, float]] = []

    def _count(name: str, value: float = 1, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {}), float(value)))

    def _set_gauge(name: str, value: float, attrs: dict | None = None) -> None:
        capturado.append((name, dict(attrs or {}), float(value)))

    monkeypatch.setattr(otel_metrics, "count", _count)
    monkeypatch.setattr(otel_metrics, "set_gauge", _set_gauge)
    return capturado


def _truncadas(emitidos) -> list[float]:
    return [
        v for n, a, v in emitidos
        if n == CONTADOR and a.get("reason") == "truncated"
    ]


# ── 1. NÃO-REGRESSÃO: o default não muda o que roda hoje ───────────────────


def test_default_priority_reproduces_the_historical_id_asc_cut(sessao):
    """Com todas em 0 — o estado de QUALQUER instalação após a migração — o
    conjunto avaliado é exatamente o de antes: as ``cap`` de MENOR id.

    Este é o teste que autoriza a mudança a ser lançada sem que ninguém peça
    nada. Se ele cair, alguma instalação passou a avaliar outro conjunto de
    regras só por subir de versão.
    """
    _semear(sessao, [(50, 0), (10, 0), (40, 0), (20, 0), (30, 0)])

    assert _sobreviventes(sessao, cap=3) == [10, 20, 30], (
        "o corte com prioridades empatadas deixou de ser o histórico id ASC"
    )
    assert _cortadas(sessao, cap=3) == [40, 50]


def test_a_rule_created_without_priority_lands_on_zero(sessao):
    """A não-regressão acima só vale se o default for do MODELO, não do teste.

    Uma linha inserida sem tocar em ``eval_priority`` tem de nascer 0 — senão
    ``None`` chegaria ao ``ORDER BY ... DESC``, que põe NULL no TOPO no Postgres
    e no FIM no SQLite: as regras legadas rodariam primeiro em produção e por
    último aqui, divergência que só aparece no deploy.
    """
    with sessao() as db:
        db.add(models.CorrelationRule(
            id=7, organization_id=ORG_ID, name="sem-prioridade",
            eval_mode="inflight", where_json=WHERE_OK,
        ))
        db.commit()
        regra = db.query(models.CorrelationRule).filter_by(id=7).one()
        assert regra.eval_priority == 0, (
            f"nasceu {regra.eval_priority!r} — o default saiu do modelo"
        )


# ── 2. O BUG: a regra recém-escrita passa a poder sobreviver ───────────────


def test_the_newest_rule_can_be_pinned_above_the_cap(sessao):
    """O cenário do ticket: 5 regras antigas, teto 3, e a 6ª acabou de nascer.

    Sem prioridade ela é a primeira a morrer. Com prioridade, ela roda e quem
    sai é a antiga de MENOR prioridade — e o par (antes/depois) é o que prova
    que a alavanca funciona, e não que o teste montou o resultado.
    """
    _semear(sessao, [(10, 0), (20, 0), (30, 0), (40, 0), (50, 0)])
    _semear(sessao, [(60, 0)])  # a recém-escrita, ainda sem prioridade

    assert 60 not in _sobreviventes(sessao, cap=3), (
        "o cenário do bug não foi reproduzido: a mais recente já sobrevivia"
    )

    with sessao() as db:
        db.query(models.CorrelationRule).filter_by(id=60).one().eval_priority = 10
        db.commit()

    sobreviventes = _sobreviventes(sessao, cap=3)
    assert sobreviventes == [60, 10, 20], (
        f"prioridade não mudou o corte: {sobreviventes}"
    )
    assert 60 not in _cortadas(sessao, cap=3), (
        "a regra fixada aparece como cortada — o selo 'Não avaliada' mentiria"
    )


def test_priority_beats_id_even_when_the_pinned_rule_is_the_oldest(sessao):
    """A alavanca não é "novo ganha": é o número que o operador escreveu.

    Sem este caso, ``eval_priority DESC`` seria indistinguível de um
    ``id DESC`` disfarçado no teste acima (lá a fixada também era a mais nova).
    """
    _semear(sessao, [(30, 0), (10, 5), (20, 0), (40, 0)])

    assert _sobreviventes(sessao, cap=2) == [10, 20]


# ── 3. ESTABILIDADE entre execuções (o motivo original do id ASC) ──────────


def test_the_selection_is_identical_across_two_executions(sessao):
    """Duas execuções = duas réplicas de worker. Sequências diferentes
    produziriam Detections com ``first_seen`` divergente sob concorrência.

    A comparação é entre as duas execuções E contra a sequência esperada: só
    ``primeira == segunda`` passaria por vacuidade se as duas viessem vazias.
    """
    _semear(sessao, [(50, 1), (10, 1), (40, 9), (20, 1), (30, 9)])

    primeira = _sobreviventes(sessao, cap=4)
    segunda = _sobreviventes(sessao, cap=4)

    assert primeira == [30, 40, 10, 20], f"ordem inesperada: {primeira}"
    assert primeira == segunda, (
        f"a seleção alternou entre execuções: {primeira} vs {segunda}"
    )
    assert _cortadas(sessao, cap=4) == _cortadas(sessao, cap=4) == [50]


def test_equal_priorities_break_the_tie_by_id_and_never_alternate(sessao):
    """Desempate ESTÁVEL: mesma prioridade não pode alternar entre ciclos.

    Semeado com ids fora de ordem e prioridades TODAS iguais — o único critério
    que resta é o ``id ASC``. Sem o desempate a ordem seria a que o plano do
    banco quisesse, e o valor da coluna sozinho não garantiria nada.
    """
    _semear(sessao, [(90, 7), (30, 7), (70, 7), (10, 7), (50, 7)])

    execucoes = [_sobreviventes(sessao, cap=5) for _ in range(2)]

    assert execucoes[0] == [10, 30, 50, 70, 90], (
        f"o desempate não é id ASC: {execucoes[0]}"
    )
    assert execucoes[0] == execucoes[1]


def test_survivors_and_cut_are_ordered_by_the_same_clause(sessao):
    """As duas consultas TÊM de ordenar igual — é o que impede o aviso (e o
    selo "Não avaliada" da tela) de responder por uma política de corte
    diferente da que o motor aplica.

    Compara o SQL COMPILADO, não o fonte: funciona na imagem Cython.
    """
    with sessao() as db:
        repo = repository.CorrelationRuleRepository(db)
        sobrevivem = str(
            repo._inflight_query(ORG_ID).order_by(
                *repository._inflight_eval_order()
            )
        )
    ordem = sobrevivem[sobrevivem.index("ORDER BY"):]

    assert "eval_priority DESC" in ordem, (
        "a prioridade saiu da cláusula de ordenação"
    )
    assert ordem.index("eval_priority DESC") < ordem.index("correlation_rules.id ASC"), (
        "o id passou à frente da prioridade — o campo vira decorativo"
    )
    assert "correlation_rules.id ASC" in ordem, (
        "sem o desempate por id a ordem deixa de ser TOTAL e duas réplicas "
        "podem divergir em regras de mesma prioridade"
    )


# ── 4. O CORTADO continua contado, e agora nomeado ─────────────────────────


def test_cut_is_the_exact_complement_of_the_survivors(sessao):
    """``sobreviventes + cortadas`` é a população inteira, sem sobreposição.

    É a propriedade que sustenta a observabilidade: se as duas listas pudessem
    se sobrepor ou deixar buraco, "quantas ficaram de fora" e "quais" contariam
    histórias diferentes.
    """
    _semear(sessao, [(50, 0), (10, 3), (40, 0), (20, 3), (30, 1)])
    # Ruído que NÃO é população: desabilitada e batch. Sem elas, o complemento
    # passaria mesmo que o filtro de ``list_inflight_cut_for_org`` divergisse.
    _semear(sessao, [(60, 99)], enabled=False)
    _semear(sessao, [(70, 99)], eval_mode="batch")

    cap = 2
    sobreviventes = _sobreviventes(sessao, cap)
    cortadas = _cortadas(sessao, cap)

    with sessao() as db:
        total = repository.CorrelationRuleRepository(db).count_inflight_for_org(ORG_ID)

    assert total == 5, f"a população em voo virou {total} — o ruído vazou"
    assert sobreviventes == [10, 20]
    assert set(sobreviventes) & set(cortadas) == set(), "listas se sobrepõem"
    assert sorted(sobreviventes + cortadas) == [10, 20, 30, 40, 50]
    assert len(cortadas) == total - cap


def test_cut_listing_honours_its_read_ceiling(sessao):
    """``max_rows`` é teto de LEITURA — nada é decidido por ele, mas ele existe
    para que um diagnóstico não paginue a org inteira para dentro de um log."""
    _semear(sessao, [(i * 10, 0) for i in range(1, 8)])

    assert _cortadas(sessao, cap=2, max_rows=3) == [30, 40, 50], (
        "o teto de leitura não recortou o PREFIXO da lista ordenada"
    )
    assert len(_cortadas(sessao, cap=2, max_rows=100)) == 5, (
        "sem teto apertado a lista tem de vir inteira — senão o recorte acima "
        "passou por a lista já ser curta"
    )


def test_truncation_still_counts_every_cut_rule(sessao, emitidos, monkeypatch):
    """O aviso de truncamento sobrevive à mudança de ordem: mesma métrica,
    mesmo delta ``total - cap``, mesmo gauge."""
    cap = 3
    monkeypatch.setattr(settings, "INFLIGHT_MAX_RULES_PER_CYCLE", cap)
    monkeypatch.setattr(_db_module, "SessionLocal", sessao)
    _semear(sessao, [(i * 10, 0) for i in range(1, 8)])  # 7 regras

    ruleset = load_inflight_rules_for_org(ORG_ID)

    assert _ids_compiladas(ruleset) == [10, 20, 30], (
        "a carga não devolveu as sobreviventes da política — o gauge abaixo "
        "seria sobre outro conjunto"
    )
    assert _truncadas(emitidos) == [4.0], (
        f"o delta de cortadas não é total-cap: {_truncadas(emitidos)}"
    )
    assert [v for n, _a, v in emitidos if n == GAUGE] == [3.0]


def test_the_warning_names_the_cut_rules_and_bounds_the_list(
    sessao, emitidos, monkeypatch, caplog
):
    """O aviso NOMEIA quem ficou de fora, capado em ``CUT_RULES_NAMED_IN_LOG``.

    Antes ele afirmava que as descartadas "são as mais RECENTES" — frase que
    vira mentira na primeira org que usar ``eval_priority``. O nome vem da
    consulta complementar, não de uma dedução paralela.
    """
    cap = 2
    monkeypatch.setattr(settings, "INFLIGHT_MAX_RULES_PER_CYCLE", cap)
    monkeypatch.setattr(_db_module, "SessionLocal", sessao)
    cortadas_esperadas = CUT_RULES_NAMED_IN_LOG + 3
    _semear(sessao, [(i * 10, 0) for i in range(1, cap + cortadas_esperadas + 1)])

    with caplog.at_level(logging.WARNING):
        load_inflight_rules_for_org(ORG_ID)

    aviso = "\n".join(
        r.getMessage() for r in caplog.records if "regras em voo" in r.getMessage()
    )
    assert aviso, "o truncamento voltou a ser silencioso"
    assert "eval_priority DESC, id ASC" in aviso, (
        "o aviso precisa dizer qual é a política de sobrevivência — sem isso o "
        "operador não sabe qual alavanca puxar"
    )
    assert "mais RECENTES" not in aviso, (
        "a frase antiga voltou: ela é falsa assim que alguém usa eval_priority"
    )
    # A PRIMEIRA cortada é nomeada; a última NÃO, porque passou do teto de log.
    assert f"{(cap + 1) * 10} (regra-{(cap + 1) * 10})" in aviso
    ultima = (cap + cortadas_esperadas) * 10
    assert f"{ultima} (regra-{ultima})" not in aviso, (
        "o teto de nomes não foi aplicado — 150 ids numa linha não são lidos"
    )
    assert "e mais 3" in aviso, "o resto omitido tem de ser confessado"
    assert f"{cortadas_esperadas} regra(s)" in aviso, (
        "o TOTAL de cortadas não pode desaparecer junto com os nomes"
    )


def test_named_cut_ceiling_is_a_log_ceiling_not_a_policy(sessao):
    """Invariante do único número novo deste lote.

    Zero apagaria a razão de a função existir sem quebrar nada; acima do teto
    de avaliação ele nunca morde (a lista de cortadas não é limitada por ele) e
    a linha de log volta a ser ilegível.
    """
    assert 1 <= CUT_RULES_NAMED_IN_LOG <= int(settings.INFLIGHT_MAX_RULES_PER_CYCLE), (
        f"CUT_RULES_NAMED_IN_LOG={CUT_RULES_NAMED_IN_LOG} saiu da faixa útil "
        f"[1, {settings.INFLIGHT_MAX_RULES_PER_CYCLE}]"
    )


# ── 5. O diagnóstico NÃO pode derrubar a detecção ──────────────────────────


def test_a_broken_cut_listing_degrades_the_log_and_nothing_else(
    sessao, emitidos, monkeypatch, caplog
):
    """A consulta das cortadas roda DENTRO do ``try`` que protege a coleta,
    cujo ``except`` devolve ruleset VAZIO. Um diagnóstico que desligasse a
    avaliação do ciclo inteiro seria uma troca terrível.
    """
    cap = 2
    monkeypatch.setattr(settings, "INFLIGHT_MAX_RULES_PER_CYCLE", cap)
    monkeypatch.setattr(_db_module, "SessionLocal", sessao)
    _semear(sessao, [(i * 10, 0) for i in range(1, 6)])

    def _explode(*_a, **_kw):
        raise RuntimeError("consulta das cortadas fora do ar")

    monkeypatch.setattr(
        repository.CorrelationRuleRepository, "list_inflight_cut_for_org", _explode
    )

    with caplog.at_level(logging.WARNING):
        ruleset = load_inflight_rules_for_org(ORG_ID)

    assert _ids_compiladas(ruleset) == [10, 20], (
        "o diagnóstico derrubou a avaliação do ciclo — exatamente o que o "
        "fail-safe da carga existe para impedir"
    )
    assert _truncadas(emitidos) == [3.0], (
        "o total de cortadas depende da consulta que quebrou; ele vem do "
        "count e tem de sobreviver"
    )
    assert any(
        "não foi possível listar" in r.getMessage() for r in caplog.records
    ), "a degradação do aviso ficou muda"


def test_the_cut_listing_is_not_queried_when_nothing_was_cut(
    sessao, emitidos, monkeypatch
):
    """Consulta extra só no estado quebrado. CONTA chamadas — levantar exceção
    dentro do dublê provaria menos: o ``except`` amplo da carga a engoliria e o
    teste passaria por vacuidade.
    """
    cap = 5
    monkeypatch.setattr(settings, "INFLIGHT_MAX_RULES_PER_CYCLE", cap)
    monkeypatch.setattr(_db_module, "SessionLocal", sessao)

    chamadas: list[int] = []
    original = repository.CorrelationRuleRepository.list_inflight_cut_for_org

    def _contando(self, organization_id, limit, max_rows):
        chamadas.append(limit)
        return original(self, organization_id, limit=limit, max_rows=max_rows)

    monkeypatch.setattr(
        repository.CorrelationRuleRepository, "list_inflight_cut_for_org", _contando
    )

    _semear(sessao, [(10, 0), (20, 0)])  # abaixo do teto
    load_inflight_rules_for_org(ORG_ID)
    assert chamadas == [], f"consultou as cortadas sem truncamento: {chamadas}"
    assert _truncadas(emitidos) == []

    _semear(sessao, [(i * 10, 0) for i in range(3, 9)])  # agora 8 > cap
    load_inflight_rules_for_org(ORG_ID)
    assert chamadas == [cap], (
        "no estado truncado a consulta TEM de acontecer — sem este lado "
        "positivo o assert vazio acima passaria com a função morta"
    )


# ── 6. A migração ──────────────────────────────────────────────────────────


@pytest.fixture
def engine_legado(monkeypatch, tmp_path):
    """Engine com o schema real, mas ``correlation_rules`` SEM a coluna nova.

    ``_run_lightweight_migrations`` lê ``inspect(engine)`` do objeto importado
    de ``database.py``, então o módulo-level ``engine`` tem de ser trocado —
    não basta o ``SessionLocal``.
    """
    url = f"sqlite:///{tmp_path / 'legado.db'}"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE correlation_rules"))
        conn.execute(text(
            "CREATE TABLE correlation_rules ("
            "  id INTEGER PRIMARY KEY,"
            "  organization_id INTEGER NOT NULL,"
            "  name VARCHAR NOT NULL,"
            "  enabled BOOLEAN NOT NULL DEFAULT 1,"
            "  eval_mode VARCHAR NOT NULL DEFAULT 'batch'"
            ")"
        ))
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(
        _db_module, "SessionLocal", sessionmaker(bind=engine)
    )
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine
    engine.dispose()


def test_migration_adds_the_column_with_zero_on_every_legacy_row(engine_legado):
    """A regra legada tem de acordar em 0 — é o que torna a migração invisível.

    Uma linha gravada ANTES do ALTER prova o DEFAULT do banco; a checagem do
    modelo (teste lá em cima) prova o do ORM. São dois caminhos de escrita
    distintos e o NULL só apareceria por um deles.
    """
    with engine_legado.begin() as conn:
        conn.execute(text(
            "INSERT INTO correlation_rules(id, organization_id, name, eval_mode) "
            "VALUES (1, 1, 'legada', 'inflight')"
        ))

    assert "eval_priority" not in {
        c["name"] for c in inspect(engine_legado).get_columns("correlation_rules")
    }, "o cenário legado não foi montado: a coluna já existia"

    _db_module._run_lightweight_migrations()

    colunas = {
        c["name"]: c for c in inspect(engine_legado).get_columns("correlation_rules")
    }
    assert "eval_priority" in colunas, "a migração leve não adicionou a coluna"
    assert colunas["eval_priority"]["nullable"] is False, (
        "coluna anulável: ORDER BY ... DESC põe NULL no topo no Postgres e no "
        "fim no SQLite — a prioridade das regras legadas divergiria por dialeto"
    )

    with engine_legado.begin() as conn:
        valor = conn.execute(
            text("SELECT eval_priority FROM correlation_rules WHERE id = 1")
        ).scalar()
    assert valor == 0, f"a linha legada acordou em {valor!r} e não em 0"


def test_migration_is_idempotent(engine_legado):
    """Segundo boot não pode estourar no ``ADD COLUMN`` já aplicado."""
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()

    assert "eval_priority" in {
        c["name"] for c in inspect(engine_legado).get_columns("correlation_rules")
    }


# ── O desempate, guardado onde ele de fato importa ──────────────────────────


@pytest.mark.pg
@pytest.mark.skipif(
    not os.environ.get("CENTRALOPS_TEST_PG_DSN"),
    reason="CENTRALOPS_TEST_PG_DSN não definido (sem Postgres real)",
)
def test_o_desempate_por_id_sobrevive_num_postgres_de_verdade() -> None:
    """O desempate `id ASC` só é verificável de verdade no Postgres.

    Os testes acima rodam em SQLite, onde `id INTEGER PRIMARY KEY` é alias de
    `rowid`: a ordem física é a ordem do id, e o banco devolve a sequência certa
    mesmo sem ordenar. Medido — removendo `id.asc()` de `_inflight_eval_order()`,
    três testes deste arquivo continuam VERDES, inclusive o que existe só para
    provar o desempate.

    No Postgres não há essa coincidência: sem `ORDER BY` explícito, um seq scan
    pode devolver qualquer ordem, e ela muda com o plano. É exatamente ali que
    a perda do desempate produz o dano que o `id ASC` original existia para
    evitar — duas réplicas de worker avaliando as mesmas regras em ordens
    diferentes, gerando Detections com `first_seen` divergente sob concorrência.

    Por isso o cenário aqui é o adversário certo: prioridades TODAS IGUAIS, para
    que `eval_priority DESC` não decida nada e só o desempate separe as linhas.
    """
    from sqlalchemy import create_engine as _create_engine

    engine = _create_engine(os.environ["CENTRALOPS_TEST_PG_DSN"])
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    try:
        with Session() as db:
            db.query(models.CorrelationRule).delete()
            db.query(models.Organization).filter_by(id=ORG_ID).delete()
            db.commit()
            # A org tem de existir ANTES das regras: no Postgres a FK é aplicada
            # de verdade. O fixture de SQLite deste arquivo já semeia a org, e
            # este teste usa engine próprio — foi por isso que ele quebrou no
            # gate `pg` e passou despercebido localmente, onde o marker o pula.
            db.add(models.Organization(id=ORG_ID, name="Org", slug="org-pg"))
            db.commit()

        _semear(Session, [(90, 0), (30, 0), (70, 0), (10, 0), (50, 0)])

        with Session() as db:
            repo = repository.CorrelationRuleRepository(db)
            primeira = [r.id for r in repo.list_inflight_for_org(ORG_ID, limit=3)]
            segunda = [r.id for r in repo.list_inflight_for_org(ORG_ID, limit=3)]

        assert primeira == [10, 30, 50], (
            f"com prioridades iguais, o desempate por id ASC devia dar "
            f"[10, 30, 50] e deu {primeira}. Sem ele, o Postgres é livre para "
            "devolver qualquer ordem — e duas réplicas divergem."
        )
        # Anti-vacuidade: duas listas vazias também seriam "idênticas".
        assert len(primeira) == 3
        assert primeira == segunda, "a ordem mudou entre duas execuções idênticas"
    finally:
        with Session() as db:
            db.query(models.CorrelationRule).delete()
            db.query(models.Organization).filter_by(id=ORG_ID).delete()
            db.commit()
        engine.dispose()
