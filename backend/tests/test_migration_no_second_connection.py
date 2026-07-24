"""Guard: NENHUMA segunda conexão dentro de uma transação de migração.

O incidente (produção, upgrade 2.2.0 → 2.3.0)
---------------------------------------------
O container da API pendurava para SEMPRE na migração de schema. Último log:
``start-api: migração de schema...``. Nenhum erro, nenhum traceback — o
healthcheck estourava, o Docker reiniciava, e o processo novo entrava na mesma
fila. O ``pg_stat_activity`` do cliente mostrou o mecanismo::

    pid 42 | idle in transaction | UPDATE integrations ... | wait_event=ClientRead
    pid 50 | active              | SELECT pg_catalog.pg_attribute... | Lock/relation
    pid 51 | active              | SELECT pg_advisory_lock(...)      | Lock/advisory

pid 42 e pid 50 eram o **MESMO processo Python**:

1. Dentro de ``with engine.begin() as conn:`` (uma transação de ~1200 linhas em
   ``_run_lightweight_migrations``) rodou ``ALTER TABLE integrations ADD COLUMN
   collection_filters TEXT`` → pegou **ACCESS EXCLUSIVE** em ``integrations``.
2. Rodou o ``UPDATE integrations`` seguinte. A conexão ficou ``idle in
   transaction``, esperando o CLIENTE mandar o próximo comando.
3. O código então chamou ``inspect(engine).get_columns("integrations")`` — que
   abre uma **SEGUNDA conexão** e vai ler o catálogo daquela tabela. Bloqueada
   pelo ACCESS EXCLUSIVE que a PRIMEIRA conexão segura.
4. O processo Python travou na segunda conexão; a primeira esperava o processo.

**O detector de deadlock do Postgres NÃO desfaz isso.** Ele só enxerga ciclos de
espera-por-*lock*, e a pid 42 não espera lock — espera o cliente. Sem
``lock_timeout``, pendura para sempre. pid 51 é outro container, vítima inocente
esperando o advisory lock do init.

Por que o CI não pegou
----------------------
O ``inspect(engine)`` dentro da transação existia havia várias versões — bug
LATENTE. O que o armou foi ``collection_filters`` ser a primeira coluna nova em
``integrations`` naquele bloco em muito tempo: num banco já migrado era o ÚNICO
``ALTER`` que ainda disparava. Instalação nova não sofre (``create_all`` cria a
coluna, nenhum ``ALTER`` roda) — e os testes rodam em SQLite e/ou banco limpo.
Ou seja: **nem revisão de código nem suíte verde detectam essa classe de bug.**
Daí este guard ser ESTRUTURAL, sobre o AST do fonte.

O que este arquivo proíbe
-------------------------
Dentro de um bloco ``with engine.begin()/connect() as X:``:

1. ``inspect(engine)`` — abre uma segunda conexão;
2. usar um inspector ligado ao ENGINE (``inspector.get_columns(...)``, onde
   ``inspector`` veio de ``inspect(engine)`` e não foi religado a
   ``inspect(X)``) — cada método dele abre uma segunda conexão;
3. ``engine.begin()``/``engine.connect()`` ANINHADO — mesma classe de erro,
   sem intermediário;
4. abrir uma SESSÃO nova — ``SessionLocal()``, ``Session(bind=engine)``,
   ``sessionmaker(...)``, ``scoped_session(...)`` — ou pegar a conexão crua com
   ``engine.raw_connection()``. Uma Session sem ``bind`` explícito nasce ligada
   ao ENGINE, e o primeiro ``execute()`` dela puxa uma conexão NOVA do pool:
   é o mesmo bug da forma 1, só que com mais camadas por cima.

A correção é sempre a mesma: **``inspect(<conn>)``** — o inspector ligado à
CONEXÃO da transação reusa a conexão que já segura os locks. Ganho secundário:
ligado à conexão, a inspeção **enxerga o DDL não-commitado do mesmo bloco** (DDL
é transacional no Postgres), que é justamente o que os comentários
"re-inspect to pick up columns added above in this same block" sempre
pressupuseram e nunca obtiveram.

Para a forma 4 a correção é a mesma ideia com outro nome: **``bind=<conn>``**
(``SessionLocal(bind=conn)``, ``Session(bind=conn)``) — a Session passa a rodar
DENTRO da transação que já está aberta, em vez de disputar lock com ela.
Alternativa igualmente válida: mover a Session para FORA do ``with``, que é
exatamente o que ``_backfill_org_hierarchy`` faz hoje.

Ler o catálogo com um inspector do ENGINE **antes** de abrir o ``with`` continua
válido e é o padrão majoritário do arquivo — o guard só olha para DENTRO do
bloco. Do mesmo modo, ``with SessionLocal() as session:`` no topo de uma função,
fora de qualquer transação de migração, é correto e não é denunciado.

Como o guard funciona
---------------------
Análise de AST (``ast.parse``), não regex: comentário, docstring e literal de
string não são código e portanto não podem produzir falso positivo — eles nem
viram nós de chamada. Os testes de mutação abaixo provam que cada uma das
formas proibidas QUEBRA o guard e que a forma correta passa; sem eles, um guard
que só sabe dizer "ok" é indistinguível de um guard quebrado.

Calibração contra o bug REAL
----------------------------
Rodado sobre a versão de ``database.py`` ANTERIOR à correção, o analisador
aponta **31 ocorrências**: 9 ``inspect(engine)`` e 22 reusos de um inspector
ligado ao engine — exatamente o que a correção mudou, achado por achado. Sobre
a versão corrigida: **0**. É essa a régua; os literais sintéticos abaixo apenas
a congelam sem depender de um SHA do git.

A forma 4 (Session/raw connection) pontua **0 nas duas versões** — ela é
PREVENTIVA, não retrospectiva. Não confunda esse zero com cobertura: o guard
nasceu cego para ela, e ``_backfill_org_hierarchy`` só não estourou porque abre
a ``SessionLocal()`` FORA de qualquer ``_migration_txn()``. Mover aquele bloco
para dentro da transação — refactor de uma linha, plausível em qualquer revisão
— reproduz o travamento contra Postgres 16 real. Os mutantes das formas 5 a 8
existem justamente porque não há dívida histórica para calibrar aqui.

Defesa em profundidade
----------------------
``_migration_txn()`` carimba ``lock_timeout`` na transação, de modo que uma
espera anormal vire ``OperationalError`` em vez de container pendurado. Isso
transforma o pendurado-para-sempre num boot que falha rápido e com log — mas
NÃO torna o bug aceitável: com ``lock_timeout``, a mesma migração passa a
abortar o boot em vez de completar. O guard é que impede o defeito de entrar.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Set

import pytest

#: Alvo do guard. ``app/db/`` está em ``EXCLUDE_PREFIXES`` do
#: ``compose/cython-build.sh``, então hoje o .py sobrevive na imagem compilada.
#: Ainda assim o teste degrada para skip se o arquivo sumir: um guard que
#: estoura ``OSError`` em tempo de import derruba a COLETA da suíte inteira, o
#: que é infinitamente pior do que um guard que não roda.
_DATABASE_PY = Path(__file__).resolve().parents[1] / "app" / "db" / "database.py"

#: Métodos de ``sqlalchemy.engine.Inspector`` que disparam I/O no catálogo. Num
#: inspector ligado ao ENGINE, cada um destes = uma conexão NOVA do pool.
_INSPECTOR_METHODS = frozenset(
    {
        "get_columns",
        "get_table_names",
        "get_view_names",
        "get_materialized_view_names",
        "get_temp_table_names",
        "get_temp_view_names",
        "get_schema_names",
        "get_indexes",
        "get_pk_constraint",
        "get_foreign_keys",
        "get_unique_constraints",
        "get_check_constraints",
        "get_table_comment",
        "get_table_options",
        "get_sequence_names",
        "get_multi_columns",
        "get_multi_indexes",
        "has_table",
        "has_index",
        "has_sequence",
        "has_schema",
        "reflect_table",
        "reflect",
    }
)

#: Métodos que ADQUIREM uma conexão nova do pool a partir do engine.
_CONN_ACQUIRE = frozenset({"begin", "connect"})

#: Fábricas de Session conhecidas. Uma Session criada sem ``bind`` explícito
#: herda o bind do ENGINE: o primeiro ``execute()`` puxa uma conexão NOVA do
#: pool — mesma segunda-conexão da forma 1, com ORM por cima. ``sessionmaker`` e
#: ``scoped_session`` entram porque construir a fábrica DENTRO da transação é o
#: mesmo defeito adiado por uma linha.
_SESSION_FACTORY_NAMES = frozenset(
    {
        "Session",
        "SessionLocal",
        "AsyncSession",
        "AsyncSessionLocal",
        "sessionmaker",
        "async_sessionmaker",
        "scoped_session",
    }
)

#: Chamadas que CRIAM uma fábrica de sessão — usadas para descobrir os nomes
#: locais do módulo (``SessionLocal = sessionmaker(bind=engine)``) em vez de
#: depender de uma lista fixa.
_SESSION_FACTORY_BUILDERS = frozenset(
    {"sessionmaker", "async_sessionmaker", "scoped_session"}
)

#: ``Session`` é um nome popular fora do SQLAlchemy. Um cliente HTTP não abre
#: conexão de banco nenhuma, então denunciá-lo seria ruído puro.
_NON_DB_SESSION_OWNERS = frozenset({"requests", "httpx", "aiohttp", "boto3"})

#: Conexão DBAPI crua, direto do pool — não passa por ``begin()``/``connect()``,
#: então nenhuma das checagens anteriores a enxerga.
_RAW_CONN_METHODS = frozenset({"raw_connection"})

KIND_INSPECT_ENGINE = "inspect-engine-dentro-da-transacao"
KIND_ENGINE_INSPECTOR = "inspector-do-engine-dentro-da-transacao"
KIND_NESTED_CONN = "conexao-aninhada"
KIND_NEW_SESSION = "sessao-nova-dentro-da-transacao"
KIND_RAW_CONN = "raw-connection-dentro-da-transacao"
KIND_METADATA_BIND = "metadata-ligada-ao-engine-dentro-da-transacao"
#: Idiomas de metadata/reflexão que puxam conexão do ``bind``. Hoje ``create_all``
#: roda FORA de qualquer transação (em ``_run_schema_init``) — esta checagem é
#: PREVENTIVA: mover uma delas para dentro de um bloco seria o mesmo defeito de
#: segunda conexão, e nada avisaria.
_METADATA_METHODS = frozenset({"create_all", "drop_all", "reflect"})


@dataclass(frozen=True)
class Finding:
    """Uma segunda conexão aberta dentro de uma transação já aberta."""

    lineno: int
    kind: str
    snippet: str
    conn_var: str

    def render(self, filename: str) -> str:
        if self.kind == KIND_INSPECT_ENGINE:
            what = (
                f"`{self.snippet}` DENTRO do bloco `with ... as {self.conn_var}:` "
                f"— abre uma SEGUNDA conexão para ler o catálogo."
            )
            fix = f"troque por `inspect({self.conn_var})`."
        elif self.kind == KIND_ENGINE_INSPECTOR:
            insp = self.snippet.split(".", 1)[0]
            what = (
                f"`{self.snippet}` usa um inspector ligado ao ENGINE dentro do "
                f"bloco `with ... as {self.conn_var}:` — cada método dele abre "
                f"uma SEGUNDA conexão."
            )
            fix = (
                f"religue o inspector à conexão no topo do bloco "
                f"(`{insp} = inspect({self.conn_var})`), ou leia o catálogo "
                f"ANTES de abrir o `with`."
            )
        elif self.kind == KIND_NEW_SESSION:
            what = (
                f"`{self.snippet}` DENTRO do bloco `with ... as {self.conn_var}:` "
                f"— sem `bind` (ou com `bind=engine`) a Session fica ligada ao "
                f"ENGINE, e o primeiro `execute()` dela puxa uma SEGUNDA conexão "
                f"do pool."
            )
            fix = (
                f"passe a conexão do bloco (`bind={self.conn_var}`), ou mova a "
                f"Session para FORA da transação."
            )
        elif self.kind == KIND_METADATA_BIND:
            what = (
                f"`{self.snippet}` DENTRO do bloco `with ... as {self.conn_var}:` "
                f"— o `bind` aponta para o ENGINE, então a reflexão/DDL puxa uma "
                f"SEGUNDA conexão do pool no meio da transação aberta."
            )
            fix = (
                f"passe a conexão do bloco (`bind={self.conn_var}` ou "
                f"`autoload_with={self.conn_var}`), ou mova esta chamada para FORA "
                f"do `with`."
            )
        elif self.kind == KIND_RAW_CONN:
            what = (
                f"`{self.snippet}` DENTRO do bloco `with ... as {self.conn_var}:` "
                f"— pega uma conexão DBAPI crua NOVA do pool, sem passar por "
                f"`begin()`/`connect()`."
            )
            fix = (
                f"reuse `{self.conn_var}` (`{self.conn_var}.connection` expõe o "
                f"DBAPI da conexão que já está na transação), ou mova esta "
                f"chamada para FORA do `with`."
            )
        else:
            what = (
                f"`{self.snippet}` ANINHADO dentro do bloco "
                f"`with ... as {self.conn_var}:` — é uma SEGUNDA conexão no meio "
                f"de uma transação aberta, sem intermediário."
            )
            fix = (
                f"reuse `{self.conn_var}`, ou mova este bloco para FORA da "
                f"transação externa."
            )
        return f"{filename}:{self.lineno}\n      {what}\n      CORREÇÃO: {fix}"


# ── primitivas de AST ────────────────────────────────────────────────────────


def _dotted(node: ast.AST) -> Optional[str]:
    """``Name``/``Attribute`` → ``"a.b.c"``; qualquer outra coisa → ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _connection_acquisition(
    node: ast.expr, txn_factories: Set[str] = frozenset()
) -> Optional[str]:
    """Devolve o nome do que adquire a conexão, ou ``None``.

    Reconhece três formas:

    * ``engine.begin()`` — a transação de migração;
    * ``engine.connect().execution_options(...)`` — o advisory lock do init
      (desce por chamadas encadeadas até achar o ``connect()``);
    * ``_migration_txn()`` e primos — qualquer ``@contextmanager`` do módulo que
      abra ``engine.begin()`` e faça ``yield`` da conexão. Descobertos em
      ``_discover_txn_factories``, e não por nome fixo: o dia em que a
      transação de migração ganhou um wrapper (para carimbar ``lock_timeout``),
      um guard que só conhecesse ``engine.begin()`` teria ficado CEGO nos exatos
      blocos que importam — e verde.

    Exige "engine" no nome do receptor nas duas primeiras formas: ``conn.begin()``
    é savepoint na MESMA conexão e não é este bug.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in txn_factories
    ):
        return node.func.id
    while isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        if func.attr in _CONN_ACQUIRE:
            owner = _dotted(func.value)
            return owner if owner and "engine" in owner.lower() else None
        node = func.value
    return None


def _discover_txn_factories(tree: ast.Module) -> Set[str]:
    """``@contextmanager``s do módulo que fazem ``yield`` de uma conexão nova.

    Um ``with _migration_txn() as conn:`` abre exatamente a mesma transação que
    um ``with engine.begin() as conn:`` — e portanto retém os mesmos locks. Se o
    guard não os resolvesse, todo o corpo dessas dezenas de blocos ficaria fora
    da análise.
    """
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            (_dotted(dec) or "").split(".")[-1]
            in ("contextmanager", "asynccontextmanager")
            for dec in node.decorator_list
        )
        if not decorated:
            continue
        opens = any(
            isinstance(inner, (ast.With, ast.AsyncWith))
            and any(_connection_acquisition(i.context_expr) for i in inner.items)
            for inner in ast.walk(node)
        )
        yields = any(
            isinstance(inner, (ast.Yield, ast.YieldFrom)) for inner in ast.walk(node)
        )
        if opens and yields:
            names.add(node.name)
    return names


def _discover_session_factories(tree: ast.Module) -> Set[str]:
    """Nomes locais que são fábrica de Session, além dos nomes conhecidos.

    ``SessionLocal = sessionmaker(bind=engine)`` batiza a fábrica no módulo. Uma
    lista fixa de nomes ficaria cega no dia em que alguém criasse
    ``ReadOnlySession = sessionmaker(...)``, então descobrimos por atribuição —
    a mesma estratégia de ``_discover_txn_factories``, e pela mesma razão: o
    guard não pode depender de o próximo autor escolher o nome que a gente
    adivinhou.
    """
    names: Set[str] = set(_SESSION_FACTORY_NAMES)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        called = (_dotted(value.func) or "").split(".")[-1]
        if called not in _SESSION_FACTORY_BUILDERS:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _bound_to_connection(call: ast.Call, conn_vars: Sequence[str]) -> bool:
    """A Session foi ligada à conexão DO BLOCO?

    ``Session(bind=conn)`` / ``SessionLocal(bind=conn)`` são CORRETOS: a Session
    roda dentro da transação já aberta, reusando a conexão que segura os locks —
    nenhuma segunda conexão é pedida ao pool. ``bind=engine`` (ou ``bind``
    ausente) é o defeito.

    O primeiro posicional também conta: ``Session(conn)`` é o mesmo bind, já que
    ``bind`` é o primeiro parâmetro de ``Session.__init__``.
    """
    for keyword in call.keywords:
        if keyword.arg == "bind":
            return _dotted(keyword.value) in conn_vars
    if call.args:
        return _dotted(call.args[0]) in conn_vars
    return False


def _inspect_arg(node: ast.AST) -> Optional[ast.expr]:
    """Devolve o argumento de ``inspect(X)`` / ``sa.inspect(X)``, senão ``None``.

    ``inspect.getsource(x)`` (stdlib) não casa: lá ``inspect`` é o RECEPTOR, não
    a função chamada.
    """
    if not isinstance(node, ast.Call) or len(node.args) != 1:
        return None
    func = node.func
    if isinstance(func, ast.Attribute):
        called = func.attr
    elif isinstance(func, ast.Name):
        called = func.id
    else:
        return None
    return node.args[0] if called == "inspect" else None


def _own_expressions(stmt: ast.stmt) -> Sequence[ast.AST]:
    """Expressões que pertencem ao PRÓPRIO statement, não aos corpos filhos.

    Separar isso evita relatar a mesma linha duas vezes e mantém o controle de
    profundidade (``depth``) exato quando o statement é um ``with``.
    """
    if isinstance(stmt, ast.If) or isinstance(stmt, ast.While):
        return [stmt.test]
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return [stmt.iter]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return [item.context_expr for item in stmt.items]
    if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
        return []
    return [stmt]


def _sub_bodies(stmt: ast.stmt) -> Iterator[List[ast.stmt]]:
    """Corpos de statement aninhados (if/else/for/while/try/except/finally)."""
    for attr in ("body", "orelse", "finalbody"):
        value = getattr(stmt, attr, None)
        if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
            yield value
    for handler in getattr(stmt, "handlers", None) or []:
        if handler.body:
            yield handler.body


def _inspector_params(fn: ast.AST) -> Set[str]:
    """Parâmetros que RECEBEM um inspector (``_heal_fk_ondelete_rules(inspector_obj)``).

    A procedência é desconhecida no corpo da função, então tratamos como
    ligado-ao-engine: é a hipótese pessimista, e a correta — todos os call sites
    de hoje passam ``inspect(engine)``.
    """
    args = getattr(fn, "args", None)
    if args is None:
        return set()
    names: Set[str] = set()
    for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
        if "insp" in arg.arg.lower():
            names.add(arg.arg)
    return names


# ── o analisador ─────────────────────────────────────────────────────────────


def _check_expressions(
    exprs: Iterable[ast.AST],
    *,
    conn_vars: Sequence[str],
    engine_inspectors: Set[str],
    session_factories: Set[str],
    findings: List[Finding],
) -> None:
    if not conn_vars:  # fora de qualquer transação: tudo permitido
        return
    innermost = conn_vars[-1]
    for expr in exprs:
        for node in ast.walk(expr):
            if not isinstance(node, ast.Call):
                continue

            dotted = _dotted(node.func) or ""
            called = dotted.split(".")[-1]
            owner = dotted.rsplit(".", 1)[0] if "." in dotted else ""

            if called in session_factories and owner not in _NON_DB_SESSION_OWNERS:
                if not _bound_to_connection(node, conn_vars):
                    findings.append(
                        Finding(
                            lineno=node.lineno,
                            kind=KIND_NEW_SESSION,
                            snippet=f"{dotted}(...)",
                            conn_var=innermost,
                        )
                    )
                continue

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _METADATA_METHODS
                and not _bound_to_connection(node, conn_vars)
            ):
                findings.append(
                    Finding(
                        lineno=node.lineno,
                        kind=KIND_METADATA_BIND,
                        snippet=f"{dotted}(...)",
                        conn_var=innermost,
                    )
                )
                continue

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in _RAW_CONN_METHODS
                and "engine" in (_dotted(node.func.value) or "").lower()
            ):
                findings.append(
                    Finding(
                        lineno=node.lineno,
                        kind=KIND_RAW_CONN,
                        snippet=f"{dotted}()",
                        conn_var=innermost,
                    )
                )
                continue

            arg = _inspect_arg(node)
            if arg is not None:
                target = _dotted(arg)
                if target not in conn_vars:
                    findings.append(
                        Finding(
                            lineno=node.lineno,
                            kind=KIND_INSPECT_ENGINE,
                            snippet=f"inspect({target or '...'})",
                            conn_var=innermost,
                        )
                    )
                continue

            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in _INSPECTOR_METHODS
                and isinstance(func.value, ast.Name)
                and func.value.id in engine_inspectors
            ):
                findings.append(
                    Finding(
                        lineno=node.lineno,
                        kind=KIND_ENGINE_INSPECTOR,
                        snippet=f"{func.value.id}.{func.attr}(...)",
                        conn_var=innermost,
                    )
                )


def _analyze_body(
    stmts: Sequence[ast.stmt],
    *,
    conn_vars: Sequence[str],
    engine_inspectors: Set[str],
    findings: List[Finding],
    txn_factories: Set[str],
    session_factories: Set[str],
) -> None:
    # Cópias: um religamento dentro de um ramo ``if`` vale para o resto DAQUELE
    # ramo, e não vaza para os statements irmãos — o oposto produziria falso
    # negativo silencioso, que é o pior defeito possível num guard.
    engine_inspectors = set(engine_inspectors)

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Função nova = escopo novo. O ``with`` do CHAMADOR não é uma
            # transação sintaticamente aberta aqui (é assim que
            # ``initialize_database`` chama ``_do_init`` sob o advisory lock sem
            # ser falso positivo — o lock roda em AUTOCOMMIT e não segura lock
            # de tabela).
            _analyze_body(
                stmt.body,
                conn_vars=(),
                engine_inspectors=_inspector_params(stmt),
                findings=findings,
                txn_factories=txn_factories,
                session_factories=session_factories,
            )
            continue
        if isinstance(stmt, ast.ClassDef):
            _analyze_body(
                stmt.body,
                conn_vars=(),
                engine_inspectors=set(),
                findings=findings,
                txn_factories=txn_factories,
                session_factories=session_factories,
            )
            continue

        _check_expressions(
            _own_expressions(stmt),
            conn_vars=conn_vars,
            engine_inspectors=engine_inspectors,
            session_factories=session_factories,
            findings=findings,
        )

        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            acquired = [
                (item, _connection_acquisition(item.context_expr, txn_factories))
                for item in stmt.items
            ]
            opens_connection = [item for item, engine in acquired if engine]
            if opens_connection:
                if conn_vars:
                    engine_name = next(e for _, e in acquired if e)
                    findings.append(
                        Finding(
                            lineno=stmt.lineno,
                            kind=KIND_NESTED_CONN,
                            snippet=f"{engine_name}.begin()/connect()",
                            conn_var=conn_vars[-1],
                        )
                    )
                inner = list(conn_vars)
                for item in opens_connection:
                    if isinstance(item.optional_vars, ast.Name):
                        inner.append(item.optional_vars.id)
                    else:
                        # ``with engine.begin():`` sem ``as`` — ainda abre a
                        # transação; um nome sintético mantém o rastreio.
                        inner.append(f"<conn@L{stmt.lineno}>")
                _analyze_body(
                    stmt.body,
                    conn_vars=tuple(inner),
                    engine_inspectors=engine_inspectors,
                    findings=findings,
                    txn_factories=txn_factories,
                    session_factories=session_factories,
                )
            else:
                _analyze_body(
                    stmt.body,
                    conn_vars=conn_vars,
                    engine_inspectors=engine_inspectors,
                    findings=findings,
                    txn_factories=txn_factories,
                    session_factories=session_factories,
                )
            continue

        # Religamento: ``X = inspect(<algo>)`` decide se ``X`` passa a ser
        # seguro (ligado à conexão) ou perigoso (ligado ao engine) daqui pra
        # frente. Roda DEPOIS da checagem acima, então um ``inspect(engine)``
        # feito dentro da transação já foi denunciado.
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            value = stmt.value
            arg = _inspect_arg(value) if value is not None else None
            if arg is not None:
                bound_to_conn = _dotted(arg) in conn_vars
                targets = (
                    stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                )
                for target in targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if bound_to_conn:
                        engine_inspectors.discard(target.id)
                    else:
                        engine_inspectors.add(target.id)

        for body in _sub_bodies(stmt):
            _analyze_body(
                body,
                conn_vars=conn_vars,
                engine_inspectors=engine_inspectors,
                findings=findings,
                txn_factories=txn_factories,
                session_factories=session_factories,
            )


def find_second_connections(source: str, filename: str = "<source>") -> List[Finding]:
    """Todas as segundas-conexões abertas dentro de uma transação já aberta."""
    tree = ast.parse(source, filename=filename)
    findings: List[Finding] = []
    _analyze_body(
        tree.body,
        conn_vars=(),
        engine_inspectors=set(),
        findings=findings,
        txn_factories=_discover_txn_factories(tree),
        session_factories=_discover_session_factories(tree),
    )
    return sorted(findings, key=lambda f: (f.lineno, f.kind))


def render_failure(findings: Sequence[Finding], filename: str) -> str:
    """Mensagem de falha auto-suficiente: quem quebrar isto daqui a um ano não
    vai conhecer o incidente, então a mensagem tem de contá-lo."""
    lines = [
        "",
        "SEGUNDA CONEXÃO dentro de uma transação de migração — isto PENDURA o boot.",
        "",
        f"{len(findings)} ocorrência(s):",
        "",
    ]
    lines += [f"  • {f.render(filename)}\n" for f in findings]
    lines += [
        "POR QUÊ (incidente de produção, upgrade 2.2.0 → 2.3.0):",
        "  Um `ALTER TABLE` dentro de `with engine.begin()` pega ACCESS EXCLUSIVE",
        "  na tabela. A conexão então fica `idle in transaction`, esperando o",
        "  CLIENTE. Se o MESMO processo pede uma SEGUNDA conexão e ela precisa",
        "  daquela tabela (ler o catálogo já basta), ela bloqueia no lock que a",
        "  primeira conexão segura — e a primeira só continua quando o processo,",
        "  travado na segunda, mandar o próximo comando.",
        "",
        "  O detector de deadlock do Postgres NÃO desfaz isso: ele só enxerga",
        "  ciclos de espera-por-LOCK, e a primeira conexão não espera lock —",
        "  espera o cliente. Sem `lock_timeout`, pendura para SEMPRE. O sintoma é",
        "  um container reiniciando em loop com um único log, sem traceback.",
        "",
        "  Só acontece em Postgres com dado real: instalação nova não roda ALTER",
        "  (o `create_all` já cria as colunas) e a suíte roda em SQLite/banco",
        "  limpo. Por isso este guard é estrutural — o CI não pega de outro jeito.",
        "",
        "CORREÇÃO: use `inspect(<conn>)`, a conexão do próprio `with`. Ela reusa a",
        "  conexão que já segura os locks e, de quebra, ENXERGA o DDL não-commitado",
        "  do mesmo bloco (DDL é transacional no Postgres) — que é o que os",
        "  comentários 're-inspect ... in this same block' sempre pressupuseram.",
        "",
        "  Ler o catálogo com `inspect(engine)` ANTES de abrir o `with` continua",
        "  correto e é o padrão majoritário do arquivo.",
        "",
        "SESSÃO: uma Session vale por uma conexão. `SessionLocal()` sem `bind` nasce",
        "  ligada ao ENGINE, então o primeiro `execute()` dela puxa uma conexão NOVA",
        "  do pool e trava do mesmo jeito. Use `bind=<conn>` para rodar DENTRO da",
        "  transação já aberta, ou mova a Session para FORA do `with`.",
        "",
    ]
    return "\n".join(lines)


# ── o guard sobre o fonte real ───────────────────────────────────────────────


def _load_database_source() -> str:
    if not _DATABASE_PY.is_file():
        pytest.skip(
            "database.py não está em disco (imagem Cython compila para .so); "
            "o guard roda no CI não-compilado."
        )
    try:
        return _DATABASE_PY.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defesa em profundidade
        pytest.skip(f"database.py ilegível ({exc}); guard degradado para no-op.")


@pytest.mark.source_only
def test_no_second_connection_inside_migration_transaction() -> None:
    source = _load_database_source()
    findings = find_second_connections(source, filename=str(_DATABASE_PY))
    assert not findings, render_failure(findings, "backend/app/db/database.py")


@pytest.mark.source_only
def test_guard_actually_sees_the_migration_transactions() -> None:
    """O guard só vale se estiver ENXERGANDO os blocos que importam.

    Sem esta âncora, um refactor que renomeie ``engine`` ou troque o
    ``with engine.begin()`` por outro idioma transforma o guard num teste que
    passa sempre — verde e inútil.
    """
    source = _load_database_source()
    tree = ast.parse(source, filename=str(_DATABASE_PY))
    factories = _discover_txn_factories(tree)

    blocks = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and any(_connection_acquisition(i.context_expr, factories) for i in node.items)
    ]
    assert len(blocks) >= 15, (
        f"o guard achou apenas {len(blocks)} bloco(s) de transação em "
        "database.py (fábricas reconhecidas: "
        f"{sorted(factories) or 'nenhuma'}). As migrações leves usam dezenas "
        "deles — se o número despencou, o reconhecimento de bloco quebrou e o "
        "guard virou um teste que passa sempre.\n"
        "Isto JÁ aconteceu uma vez: quando `engine.begin()` ganhou o wrapper "
        "`_migration_txn()`, um guard que só conhecia `engine.begin()` ficaria "
        "cego nos exatos blocos que importam. Ajuste `_connection_acquisition` / "
        "`_discover_txn_factories` ANTES de baixar este piso."
    )

    # E que os blocos gigantes religam o inspector à conexão — a correção do
    # incidente. Sem isto, alguém pode "consertar" o guard removendo a religação
    # e a análise não teria mais nada que provar.
    rebinds = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and _inspect_arg(node.value) is not None
        and _dotted(_inspect_arg(node.value)) != "engine"
    ]
    assert len(rebinds) >= 2, (
        "nenhum `inspector = inspect(<conn>)` sobrou em database.py: os blocos "
        "de `_drop_legacy_client_tables` e `_run_lightweight_migrations` "
        "dependem dele para inspecionar o catálogo sem abrir segunda conexão."
    )


@pytest.mark.source_only
def test_guard_knows_the_real_session_factory() -> None:
    """``SessionLocal`` de database.py TEM de ser reconhecida como fábrica.

    Ela é criada por ``SessionLocal = sessionmaker(bind=engine)``, e é o idioma
    de ``_backfill_org_hierarchy`` — o caminho de boot que a verificação apontou
    como ALTO risco e que foi reproduzido travando contra Postgres 16 real. Se a
    descoberta parar de achá-la (renome, import indireto, fábrica montada por
    outra função), o guard fica cego exatamente onde dói, e VERDE.
    """
    source = _load_database_source()
    tree = ast.parse(source, filename=str(_DATABASE_PY))
    factories = _discover_session_factories(tree)
    assert "SessionLocal" in factories, (
        "`SessionLocal` não foi reconhecida como fábrica de Session em "
        f"database.py (achadas: {sorted(factories - _SESSION_FACTORY_NAMES)}). "
        "Ajuste `_discover_session_factories` ANTES de mudar como a fábrica é "
        "construída — sem ela, `with SessionLocal() as s:` dentro de uma "
        "transação de migração volta a passar despercebido."
    )


# ── testes de MUTAÇÃO: provam que o guard falha quando deve ──────────────────
#
# Não dependem de disco: rodam também na imagem compilada.

_PROLOGUE = "from sqlalchemy import inspect, text\n\n"

#: Forma 1 — exatamente o que travou a produção na 2.3.0.
_MUT_INSPECT_ENGINE = _PROLOGUE + '''
def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        conn.execute(text("UPDATE integrations SET collection_filters = '{}'"))
        # Re-inspect to pick up columns added above in this same block.
        cols = {c["name"] for c in inspect(engine).get_columns("integrations")}
        if "api_host" not in cols:
            conn.execute(text("ALTER TABLE integrations ADD COLUMN api_host TEXT"))
'''

#: Forma 2 — inspector ligado ao engine ANTES do bloco, usado DENTRO dele.
_MUT_ENGINE_BOUND_INSPECTOR = _PROLOGUE + '''
def _run_lightweight_migrations():
    inspector = inspect(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        cols = {c["name"] for c in inspector.get_columns("integrations")}
        if "api_host" not in cols:
            conn.execute(text("ALTER TABLE integrations ADD COLUMN api_host TEXT"))
'''

#: Forma 3 — segunda conexão sem intermediário nenhum.
_MUT_NESTED_CONNECTION = _PROLOGUE + '''
def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        with engine.connect() as probe:
            probe.execute(text("SELECT COUNT(*) FROM integrations"))
'''

#: Forma 4 — a mesma forma 1, mas atrás do wrapper ``@contextmanager``.
#: É o idioma REAL de database.py hoje (``_migration_txn`` carimba
#: ``lock_timeout``). Um guard que só conhecesse ``engine.begin()`` daria verde
#: aqui — cego exatamente onde o incidente aconteceu.
_MUT_BEHIND_FACTORY = _PROLOGUE + '''
from contextlib import contextmanager

@contextmanager
def _migration_txn():
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        yield conn

def _run_lightweight_migrations():
    with _migration_txn() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        cols = {c["name"] for c in inspect(engine).get_columns("integrations")}
'''

#: Forma 5 — ``SessionLocal()`` dentro da transação. É a forma que quase passou:
#: ``_backfill_org_hierarchy`` usa exatamente este idioma (hoje FORA de qualquer
#: transação, que é o correto) e foi reproduzida travando contra Postgres 16 real
#: quando movida para dentro. Nenhuma das checagens anteriores a enxergava —
#: não é ``inspect``, não é inspector, e ``SessionLocal()`` não é
#: ``engine.begin()``.
_MUT_SESSION_LOCAL = _PROLOGUE + '''
from contextlib import contextmanager

@contextmanager
def _migration_txn():
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '15s'"))
        yield conn

def _run_lightweight_migrations():
    with _migration_txn() as conn:
        conn.execute(text("ALTER TABLE organizations ADD COLUMN root_id INTEGER"))
        with SessionLocal() as session:
            session.execute(text("UPDATE organizations SET root_id = id"))
            session.commit()
'''

#: Forma 6 — ``Session(bind=engine)``: o bind é EXPLÍCITO e aponta para o lugar
#: errado. Ler ``bind=`` e parar por aí seria o bug óbvio de um guard ingênuo.
_MUT_SESSION_BOUND_TO_ENGINE = _PROLOGUE + '''
from sqlalchemy.orm import Session

def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        with Session(bind=engine) as session:
            session.execute(text("UPDATE integrations SET collection_filters = '{}'"))
'''

#: Forma 7 — construir a fábrica DENTRO da transação. A chamada em si não abre
#: conexão; a sessão que sai dela abre. É o mesmo defeito adiado uma linha.
_MUT_SESSIONMAKER_INSIDE = _PROLOGUE + '''
from sqlalchemy.orm import sessionmaker

def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        Local = sessionmaker(bind=engine)
        Local().execute(text("SELECT COUNT(*) FROM integrations"))
'''

#: Forma 8 — conexão DBAPI crua. Não passa por ``begin()``/``connect()``, então
#: a checagem de aninhamento não a via.
_MUT_RAW_CONNECTION = _PROLOGUE + '''
def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            cur.execute("SELECT COUNT(*) FROM integrations")
        finally:
            raw.close()
'''

#: Reflexão/DDL de metadata ligada ao ENGINE dentro da transação. Hoje o
#: ``create_all`` roda FORA de qualquer bloco (em ``_run_schema_init``); este
#: mutante existe para que MOVÊ-LO para dentro quebre o CI, e não a produção.
_MUT_METADATA_BIND_ENGINE = _PROLOGUE + """
def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        Base.metadata.create_all(bind=engine)
"""

#: A forma CORRETA — é o que database.py faz hoje.
_CORRECT = _PROLOGUE + '''
def _run_lightweight_migrations():
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    with engine.begin() as conn:
        inspector = inspect(conn)
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
        cols = {c["name"] for c in inspector.get_columns("integrations")}
        if "integrations" in table_names:
            for col in ("watermark_at", "last_run_capped"):
                if col not in cols:
                    conn.execute(text("ALTER TABLE integrations ADD COLUMN " + col))
            more = {c["name"] for c in inspect(conn).get_columns("api_tokens")}
            if "revoked_at" not in more:
                conn.execute(text("ALTER TABLE api_tokens ADD COLUMN revoked_at TIMESTAMP"))
'''


@pytest.mark.parametrize(
    ("source", "expected_kind", "label"),
    [
        (_MUT_INSPECT_ENGINE, KIND_INSPECT_ENGINE, "inspect(engine) dentro do with"),
        (
            _MUT_ENGINE_BOUND_INSPECTOR,
            KIND_ENGINE_INSPECTOR,
            "inspector do engine usado dentro do with",
        ),
        (
            _MUT_NESTED_CONNECTION,
            KIND_NESTED_CONN,
            "engine.connect() aninhado no with",
        ),
        (
            _MUT_BEHIND_FACTORY,
            KIND_INSPECT_ENGINE,
            "inspect(engine) atrás do wrapper @contextmanager",
        ),
        (
            _MUT_SESSION_LOCAL,
            KIND_NEW_SESSION,
            "SessionLocal() dentro de _migration_txn()",
        ),
        (
            _MUT_SESSION_BOUND_TO_ENGINE,
            KIND_NEW_SESSION,
            "Session(bind=engine) dentro do with",
        ),
        (
            _MUT_SESSIONMAKER_INSIDE,
            KIND_NEW_SESSION,
            "sessionmaker() construído dentro do with",
        ),
        (
            _MUT_RAW_CONNECTION,
            KIND_RAW_CONN,
            "engine.raw_connection() dentro do with",
        ),
        (
            _MUT_METADATA_BIND_ENGINE,
            KIND_METADATA_BIND,
            "metadata.create_all(bind=engine) dentro do with",
        ),
    ],
    ids=[
        "forma-1-inspect-engine",
        "forma-2-inspector-do-engine",
        "forma-3-aninhado",
        "forma-4-atras-do-contextmanager",
        "forma-5-sessionlocal",
        "forma-6-session-bind-engine",
        "forma-7-sessionmaker-dentro",
        "forma-8-raw-connection",
        "forma-9-metadata-bind-engine",
    ],
)
def test_mutation_each_forbidden_form_is_caught(
    source: str, expected_kind: str, label: str
) -> None:
    """Mutação: cada forma proibida TEM de quebrar o guard.

    Um guard só testado pelo caminho feliz não se distingue de um guard morto.
    """
    findings = find_second_connections(source, filename="<mutante>")
    assert findings, f"guard NÃO pegou a mutação: {label}"
    kinds = {f.kind for f in findings}
    assert expected_kind in kinds, f"esperava {expected_kind!r}, veio {kinds!r}"

    # E a mensagem tem de ENSINAR a correção, não só apontar o dedo.
    message = render_failure(findings, "backend/app/db/database.py")
    assert "inspect(<conn>)" in message
    assert "ACCESS EXCLUSIVE" in message
    assert str(findings[0].lineno) in message


def test_mutation_correct_form_passes() -> None:
    """A forma correta (a de hoje) não pode ser denunciada."""
    findings = find_second_connections(_CORRECT, filename="<correto>")
    assert not findings, render_failure(findings, "<correto>")


# ── falsos positivos ─────────────────────────────────────────────────────────


def test_comments_docstrings_and_strings_are_not_code() -> None:
    """Menção textual a ``inspect(engine)`` não é uma chamada.

    A análise é de AST, então isto é estrutural — mas o teste trava a decisão:
    quem trocar o analisador por regex quebra AQUI, com a razão escrita.
    """
    source = _PROLOGUE + '''
def _run_lightweight_migrations():
    """Nunca chame inspect(engine) aqui dentro.

    Nem `with engine.connect() as x:` aninhado.
    """
    with engine.begin() as conn:
        # NÃO faça inspect(engine).get_columns("integrations") aqui.
        conn.execute(text("SELECT 'inspect(engine).get_table_names()'"))
        note = "with engine.begin() as nested:"
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
'''
    assert not find_second_connections(source, filename="<comentarios>")


def test_engine_inspector_before_the_block_is_allowed() -> None:
    """Ler o catálogo ANTES de abrir a transação é o padrão majoritário do
    arquivo e continua correto — nenhum lock está retido ainda."""
    source = _PROLOGUE + '''
def _migrate():
    _s5_inspector = inspect(engine)
    _s5_tables = set(_s5_inspector.get_table_names())
    if "destinations" in _s5_tables:
        _s5_cols = {c["name"] for c in _s5_inspector.get_columns("destinations")}
        with engine.begin() as _s5_conn:
            if "secret_version" not in _s5_cols:
                _s5_conn.execute(text("ALTER TABLE destinations ADD COLUMN secret_version INTEGER"))
'''
    assert not find_second_connections(source, filename="<antes-do-bloco>")


def test_sibling_blocks_are_not_nested() -> None:
    """Blocos IRMÃOS (um fecha, o outro abre) são a forma recomendada de isolar
    uma migração não-idempotente — não podem ser confundidos com aninhamento."""
    source = _PROLOGUE + '''
def _migrate():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
    with engine.begin() as heal_conn:
        heal_conn.execute(text("UPDATE integrations SET api_host = NULL"))
'''
    assert not find_second_connections(source, filename="<irmaos>")


def test_helper_called_from_inside_the_block_is_not_syntactic_nesting() -> None:
    """``initialize_database`` segura um advisory lock em AUTOCOMMIT e chama
    ``_do_init()``, que abre a sua própria conexão. É correto (a conexão do lock
    não segura lock de TABELA) e o guard não pode denunciá-lo: o aninhamento
    proibido é SINTÁTICO, dentro da mesma função."""
    source = _PROLOGUE + '''
def _do_init():
    with engine.connect() as _probe:
        _insp = inspect(_probe)
        had_app = _insp.has_table("app_users")

def initialize_database():
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_conn:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": 1})
        try:
            _do_init()
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": 1})
'''
    assert not find_second_connections(source, filename="<advisory-lock>")


def test_conn_begin_savepoint_is_not_a_second_connection() -> None:
    """``conn.begin()`` é savepoint na MESMA conexão — não abre nada."""
    source = _PROLOGUE + '''
def _migrate():
    with engine.begin() as conn:
        with conn.begin_nested() as savepoint:
            conn.execute(text("ALTER TABLE integrations ADD COLUMN collection_filters TEXT"))
'''
    assert not find_second_connections(source, filename="<savepoint>")


def test_stdlib_inspect_getsource_is_not_sqlalchemy_inspect() -> None:
    """``inspect.getsource`` (stdlib) tem ``inspect`` como RECEPTOR, não como
    função chamada — não pode virar falso positivo."""
    source = "import inspect\n" + '''
def _migrate():
    with engine.begin() as conn:
        src = inspect.getsource(_migrate)
        conn.execute(text("SELECT 1"))
'''
    assert not find_second_connections(source, filename="<stdlib-inspect>")


@pytest.mark.parametrize(
    ("call", "label"),
    [
        ("Session(bind=conn)", "Session com bind nomeado"),
        ("SessionLocal(bind=conn)", "fábrica do módulo com bind nomeado"),
        ("Session(conn)", "bind posicional (é o 1º parâmetro de Session)"),
    ],
    ids=["session-bind-conn", "sessionlocal-bind-conn", "session-posicional"],
)
def test_session_bound_to_the_block_connection_is_allowed(
    call: str, label: str
) -> None:
    """A Session LIGADA à conexão do bloco é a CORREÇÃO, não o defeito.

    Com ``bind=<conn>`` a Session roda dentro da transação que já está aberta e
    reusa a conexão que segura os locks — nenhuma segunda conexão é pedida ao
    pool. Denunciar isto empurraria quem quer consertar de volta para o defeito.
    """
    source = _PROLOGUE + f'''
from sqlalchemy.orm import Session

def _run_lightweight_migrations():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE organizations ADD COLUMN root_id INTEGER"))
        with {call} as session:
            session.execute(text("UPDATE organizations SET root_id = id"))
'''
    findings = find_second_connections(source, filename="<bind-na-conn>")
    assert not findings, f"falso positivo em {label}: {render_failure(findings, '<x>')}"


def test_session_outside_the_transaction_is_allowed() -> None:
    """``_backfill_org_hierarchy`` abre ``with SessionLocal() as session:`` no
    topo da função, fora de qualquer ``_migration_txn()``. É correto — nenhum
    lock de tabela está retido — e é o idioma REAL de database.py hoje. Se o
    guard denunciasse isto, o arquivo alvo ficaria vermelho sem defeito nenhum."""
    source = _PROLOGUE + '''
def _backfill_org_hierarchy():
    with SessionLocal() as session:
        _apply_migration_lock_timeout(session.connection())
        if hierarchy.needs_backfill(session):
            hierarchy.backfill_hierarchy(session)
            session.commit()
'''
    assert not find_second_connections(source, filename="<fora-da-transacao>")


def test_http_client_session_is_not_a_database_session() -> None:
    """``requests.Session()`` não abre conexão de banco nenhuma.

    ``Session`` é um nome popular fora do SQLAlchemy; casar só pelo nome faria o
    guard gritar em código irrelevante, e um guard que grita à toa é um guard
    que alguém desliga."""
    source = "import requests\n" + '''
def _migrate():
    with engine.begin() as conn:
        with requests.Session() as http:
            http.get("https://example.invalid/health")
        conn.execute(text("SELECT 1"))
'''
    assert not find_second_connections(source, filename="<http-session>")


def test_rebind_inside_a_branch_does_not_leak_to_siblings() -> None:
    """Um religamento dentro de um ramo ``if`` não torna o inspector seguro
    para o código IRMÃO — senão o guard produziria falso negativo silencioso,
    que é o pior defeito possível aqui."""
    source = _PROLOGUE + '''
def _migrate():
    inspector = inspect(engine)
    with engine.begin() as conn:
        if True:
            inspector = inspect(conn)
            cols = {c["name"] for c in inspector.get_columns("integrations")}
        later = {c["name"] for c in inspector.get_columns("api_tokens")}
'''
    findings = find_second_connections(source, filename="<ramo>")
    assert [f.kind for f in findings] == [KIND_ENGINE_INSPECTOR]
    assert findings[0].snippet == "inspector.get_columns(...)"
