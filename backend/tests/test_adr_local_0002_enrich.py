"""Enriquecimento em stream (ADR-LOCAL-0002).

Cobre as coisas que, se quebrarem, tornam a feature pior que inexistente: pureza do
caminho por-evento, recusa de PII, isolamento entre orgs, e a distinção entre "não
respondido" e "miss" (que se colapsada produz métrica que faz alguém desligar a
feature certa pelo motivo errado).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import ast
from pathlib import Path

import pytest

from backend.app.collectors.enrich import applier as applier_mod
from backend.app.collectors.enrich import dsl as dsl_mod
from backend.app.collectors.enrich import registry as registry_mod
from backend.app.collectors.enrich.applier import ApplyStats, apply, normalize_key
from backend.app.collectors.enrich.contract import (
    EnricherCapabilities,
    EnricherRegistration,
    EnrichmentConfigError,
    PiiEnricherRefused,
)
from backend.app.collectors.enrich.dsl import compile_policy
from backend.app.collectors.enrich.runtime import (
    BulkResolution,
    DictLookupTable,
    MonoTenantViolation,
    TableResolution,
    assert_mono_tenant,
)


# ── Pureza mecanizada: o caminho por-evento não pode tocar o mundo ──────────
#
# É o guard mais importante do arquivo, e a razão de ele existir está medida: o
# ``record_in`` fazia 4 pipelines Redis síncronos por evento (~0,8 ms/evento, ~8 s
# bloqueados num ciclo de 10k) até virar acumulador batched. Um ``import httpx``
# acrescentado ao applier daqui a seis meses seria invisível numa revisão de PR.

_FORBIDDEN_IMPORTS = (
    "redis", "httpx", "requests", "aiohttp", "sqlalchemy", "celery",
    "backend.app.db", "..db", "...db", "....db",
)


@pytest.mark.source_only
def test_applier_imports_nothing_that_touches_the_world():
    # ``source_only``: ``compose/cython-build.sh`` compila ``app/collectors``
    # inteiro, então na imagem de produção o ``.py`` é REMOVIDO e ``read_text()``
    # levantaria FileNotFoundError. O guard vale sobre a árvore de fontes, que é
    # onde o import proibido seria introduzido.
    src = Path(applier_mod.__file__).read_text()
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append("." * (node.level or 0) + (node.module or ""))
    offenders = [
        m for m in imported
        if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN_IMPORTS)
    ]
    assert not offenders, (
        f"applier.py importa módulo que toca o mundo: {offenders}. "
        "O aplicador roda POR EVENTO num laço serial — I/O aqui serializa a vazão."
    )


@pytest.mark.source_only
def test_applier_has_no_async_def():
    """Um ``async def`` no aplicador significaria await por evento."""
    tree = ast.parse(Path(applier_mod.__file__).read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


# ── Registry: as duas recusas de segurança acontecem no REGISTRO ────────────

def _caps(**kw):
    base = dict(key_kinds=frozenset({"ip"}), mode="local")
    base.update(kw)
    return EnricherCapabilities(**base)


def test_registry_refuses_pii_enricher():
    """PII é recusada no registro, não gated em runtime.

    A saída vive sob ``_centralops``, que ``pii_redaction.ALLOWED_ROOTS`` blinda —
    não existe rota que torne isso seguro, logo não existe configuração que
    justifique aceitar.
    """
    reg = EnricherRegistration(
        name="pii_leaker", factory=lambda cfg: None, caps=_caps(emits_pii=True)
    )
    with pytest.raises(PiiEnricherRefused, match="emits_pii"):
        registry_mod.register(reg)
    assert "pii_leaker" not in registry_mod.registered_names()


def test_registry_refuses_embedded_non_redistributable_dataset():
    reg = EnricherRegistration(
        name="geolite_baked", factory=lambda cfg: None,
        caps=_caps(license="MaxMind EULA", redistributable=False),
    )
    with pytest.raises(EnrichmentConfigError, match="redistribu"):
        registry_mod.register(reg, embedded_dataset=True)


def test_builtin_enrichers_are_registered():
    """opencti e virustotal se auto-registram no import do pacote."""
    from backend.app.collectors.enrich import enrichers  # noqa: F401

    names = registry_mod.registered_names()
    assert "opencti" in names and "virustotal" in names

    catalog = {c["name"]: c for c in registry_mod.describe_all()}
    # Egresso é consentimento de privacidade e precisa chegar à UI correto.
    assert catalog["opencti"]["egress"] == "internal"
    assert catalog["virustotal"]["egress"] == "third_party"
    assert catalog["opencti"]["mode"] == "local"
    assert catalog["virustotal"]["mode"] == "remote"


# ── DSL: rejeição explícita (o oposto do fail-open do compilador de mapping) ─

@pytest.fixture(autouse=True)
def _ensure_enrichers():
    from backend.app.collectors.enrich import enrichers  # noqa: F401


def _rule(**over):
    base = {
        "id": "r1",
        "enricher": "opencti",
        # opencti/virustotal declaram ``required_secrets``, e a DSL passou a exigir
        # ``source`` para eles: uma regra que precisa de credencial e não aponta uma
        # fonte configurada da org nunca poderia rodar, então é 422 no commit em vez
        # de no-op silencioso no ciclo.
        "source": "fonte-de-teste",
        "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
        "outputs": [{"from": "score", "target": "_centralops.enrichment.src.ti.score"}],
    }
    base.update(over)
    return base


def test_dsl_rejects_unknown_rule_key():
    with pytest.raises(EnrichmentConfigError, match="desconhecida"):
        compile_policy([_rule(nao_existe=1)])


def test_dsl_rejects_target_outside_centralops():
    """Escrever em ``normalized.*`` contornaria o gate OCSF de pipeline.py:1255."""
    with pytest.raises(EnrichmentConfigError, match="_centralops.enrichment"):
        compile_policy([
            _rule(outputs=[{"from": "score", "target": "normalized.src_endpoint.x"}])
        ])


def test_dsl_rejects_unknown_enricher_and_lists_valid_ones():
    with pytest.raises(EnrichmentConfigError) as exc:
        compile_policy([_rule(enricher="nao_existe")])
    assert "opencti" in str(exc.value)  # a mensagem carrega o vocabulário


def test_dsl_rejects_key_kind_the_enricher_cannot_resolve():
    with pytest.raises(EnrichmentConfigError, match="não resolve chave"):
        compile_policy([
            _rule(key={"source": "normalized.actor.user.name", "kind": "user"})
        ])


def test_dsl_rejects_output_field_the_enricher_never_returns():
    with pytest.raises(EnrichmentConfigError, match="não devolve o campo"):
        compile_policy([
            _rule(outputs=[{"from": "campo_inventado", "target": "_centralops.enrichment.a.b"}])
        ])


def test_dsl_rejects_duplicate_rule_id():
    with pytest.raises(EnrichmentConfigError, match="duplicado"):
        compile_policy([_rule(), _rule()])


def test_dsl_rejects_on_miss_default_without_any_default():
    """on_miss='default' sem default é 'skip' com aparência de configuração ativa."""
    with pytest.raises(EnrichmentConfigError, match="nenhum output declara"):
        compile_policy([_rule(on_miss="default")])


def test_dsl_rejects_remote_enricher_in_local_mode():
    with pytest.raises(EnrichmentConfigError, match="aplicador por evento é puro"):
        compile_policy([
            _rule(enricher="virustotal", mode="local",
                  outputs=[{"from": "malicious", "target": "_centralops.enrichment.a.b"}])
        ])


def test_dsl_compiles_and_classifies_modes():
    policy = compile_policy({
        "version": 1,
        "enrichment": [
            _rule(),
            _rule(id="r2", enricher="virustotal",
                  outputs=[{"from": "malicious_ratio",
                            "target": "_centralops.enrichment.src.vt.ratio"}]),
        ],
    })
    assert policy.has_local and policy.has_remote
    assert [r.rule_id for r in policy.local_rules()] == ["r1"]
    assert [r.rule_id for r in policy.remote_rules()] == ["r2"]


# ── Aplicador ───────────────────────────────────────────────────────────────

def _envelope(ip="8.8.8.8"):
    return {
        "_centralops": {"organization_id": 1, "vendor": "sophos"},
        "normalized": {"src_endpoint": {"ip": ip}},
        "raw": {},
    }


def test_apply_writes_under_centralops_only():
    policy = compile_policy([_rule(tags=["ti"])])
    table = DictLookupTable({"8.8.8.8": {"score": 80}})
    env = _envelope()
    stats = ApplyStats()

    touched = apply(env, policy.rules, TableResolution({"r1": table}), stats)

    assert touched is True
    assert env["_centralops"]["enrichment"]["src"]["ti"]["score"] == 80
    assert env["_centralops"]["enrichment_tags"] == ["ti"]
    # O corpo do evento é INTOCADO — é o que mantém `ocsf_valid` honesto.
    assert env["normalized"] == {"src_endpoint": {"ip": "8.8.8.8"}}
    assert stats.hits == {"r1": 1}


def test_apply_records_provenance():
    policy = compile_policy([_rule()])
    env = _envelope()
    apply(env, policy.rules, TableResolution({"r1": DictLookupTable({"8.8.8.8": {"score": 1}})}),
          ApplyStats())
    sources = env["_centralops"]["enrichment"]["_sources"]
    assert sources == [{"rule": "r1", "enricher": "opencti"}]


def test_unanswered_rule_is_skipped_not_counted_as_miss():
    """A distinção que evita 100% de miss_rate num enricher que funciona."""
    policy = compile_policy([_rule()])
    stats = ApplyStats()
    # Resolução vazia: nenhuma tabela para r1 (é o seam local vendo regra remota).
    apply(_envelope(), policy.rules, TableResolution({}), stats)
    assert stats.skipped == {"r1": 1}
    assert stats.misses == {}


def test_confirmed_miss_is_counted_and_can_tag():
    policy = compile_policy([_rule(on_miss="tag", tags=["asset_known"])])
    env = _envelope("1.1.1.1")
    stats = ApplyStats()
    apply(env, policy.rules, TableResolution({"r1": DictLookupTable({})}), stats)
    assert stats.misses == {"r1": 1}
    assert env["_centralops"]["enrichment_tags"] == ["asset_known_unknown"]


def test_vendor_data_wins_when_overwrite_is_false():
    """O default é o INVERSO do Elastic (`override: true`): o vendor nunca é apagado."""
    policy = compile_policy([_rule()])
    env = _envelope()
    env["_centralops"]["enrichment"] = {"src": {"ti": {"score": 999}}}
    apply(env, policy.rules,
          TableResolution({"r1": DictLookupTable({"8.8.8.8": {"score": 10}})}), ApplyStats())
    assert env["_centralops"]["enrichment"]["src"]["ti"]["score"] == 999


def test_when_false_writes_nothing_at_all():
    policy = compile_policy([
        _rule(when={"equals": {"source": "_centralops.vendor", "value": "outro"}}, tags=["ti"])
    ])
    env = _envelope()
    stats = ApplyStats()
    apply(env, policy.rules,
          TableResolution({"r1": DictLookupTable({"8.8.8.8": {"score": 1}})}), stats)
    assert "enrichment" not in env["_centralops"]
    assert stats.hits == {} and stats.misses == {} and stats.skipped == {}


def test_has_tag_gate_enables_rule_chaining():
    """O mecanismo de composição do ADR §4: regra A marca, regra B consome.

    Regressão de um furo REAL encontrado rodando o pipeline de demonstração: o
    predicado herdado ``in`` é escalar∈lista (``predicates.py:200``), então sobre
    ``enrichment_tags`` (uma LISTA) avaliava sempre False — a condição "só consulte
    a API paga para o que o feed local já marcou" era inexprimível.
    """
    policy = compile_policy([
        _rule(id="a", tags=["ti_known"]),
        _rule(id="b", when={"has_tag": "ti_known"},
              outputs=[{"from": "score", "target": "_centralops.enrichment.b.score"}]),
        _rule(id="c", when={"has_tag": "nunca_marcada"},
              outputs=[{"from": "score", "target": "_centralops.enrichment.c.score"}]),
    ])
    table = DictLookupTable({"8.8.8.8": {"score": 7}})
    env = _envelope()
    apply(env, policy.rules,
          TableResolution({r.rule_id: table for r in policy.rules}), ApplyStats())

    enr = env["_centralops"]["enrichment"]
    assert enr["src"]["ti"]["score"] == 7      # regra a marcou
    assert enr["b"]["score"] == 7              # b viu a tag de a
    assert "c" not in enr                      # c foi barrada


def test_lacks_tag_gate():
    policy = compile_policy([
        _rule(id="a", tags=["asset_known"]),
        _rule(id="b", when={"lacks_tag": "asset_known"},
              outputs=[{"from": "score", "target": "_centralops.enrichment.b.score"}]),
    ])
    table = DictLookupTable({"8.8.8.8": {"score": 7}})
    env = _envelope()
    apply(env, policy.rules,
          TableResolution({r.rule_id: table for r in policy.rules}), ApplyStats())
    assert "b" not in env["_centralops"]["enrichment"]


def test_when_rejects_multiple_discriminators():
    with pytest.raises(EnrichmentConfigError, match="múltiplas chaves"):
        compile_policy([_rule(when={"has_tag": "x", "exists": "normalized.a"})])


def test_on_multi_first_and_array():
    rows = [{"score": 10}, {"score": 20}]
    for on_multi, expected in (("first", 10), ("array", [10, 20])):
        policy = compile_policy([_rule(on_multi=on_multi)])
        env = _envelope()
        apply(env, policy.rules,
              BulkResolution({("r1", "8.8.8.8"): rows}), ApplyStats())
        assert env["_centralops"]["enrichment"]["src"]["ti"]["score"] == expected


def test_apply_never_raises_on_hostile_envelope():
    """Enriquecimento é observador, nunca porteiro."""
    policy = compile_policy([_rule()])
    for env in ({}, {"normalized": None}, {"normalized": {"src_endpoint": "nao-e-dict"}},
                {"_centralops": "escalar", "normalized": {"src_endpoint": {"ip": "8.8.8.8"}}}):
        apply(env, policy.rules,
              TableResolution({"r1": DictLookupTable({"8.8.8.8": {"score": 1}})}), ApplyStats())


def test_set_path_refuses_to_clobber_a_scalar():
    """Sobrescrever escalar por dict corromperia dado do vendor em silêncio."""
    policy = compile_policy([_rule()])
    env = _envelope()
    env["_centralops"]["enrichment"] = "ja-era-string"
    apply(env, policy.rules,
          TableResolution({"r1": DictLookupTable({"8.8.8.8": {"score": 1}})}), ApplyStats())
    assert env["_centralops"]["enrichment"] == "ja-era-string"


# ── Chave ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,norms,expected", [
    ("1.2.3[.]4", ("defang",), "1.2.3.4"),
    ("hxxps://mau.com", ("defang",), "https://mau.com"),
    ("  AbC.CoM  ", ("strip", "lower"), "abc.com"),
    ("1.2.3.4:443", ("strip_port",), "1.2.3.4"),
    ("[2001:db8::1]:443", ("strip_port",), "2001:db8::1"),
    # IPv6 SEM colchetes e sem porta não pode ser truncado no primeiro ':'.
    ("2001:db8::1", ("strip_port",), "2001:db8::1"),
])
def test_normalize_key(raw, norms, expected):
    assert normalize_key(raw, norms) == expected


# ── Invariante mono-tenant: toda a conta de memória depende dela ────────────

def test_mono_tenant_invariant_is_enforced_not_assumed():
    assert_mono_tenant(None, 1)
    assert_mono_tenant(1, 1)
    with pytest.raises(MonoTenantViolation, match="mono-tenant"):
        assert_mono_tenant(1, 2)


# ── Integração com o pipeline ───────────────────────────────────────────────

@pytest.mark.source_only
def test_pipeline_wires_both_seams_in_the_right_places():
    """Os dois seams existem, e o remoto cobre o flush TERMINAL.

    O flush terminal é o esquecido clássico: uma página que encerra a coleta nunca
    atinge ``collector_batch_size`` nem o timeout, então um estágio ligado só no
    flush condicional deixaria de rodar no último lote de TODO ciclo.
    """
    src = Path(
        Path(applier_mod.__file__).parent.parent / "pipeline.py"
    ).read_text()
    # E1 — antes da classificação em voo, para o campo alimentar a detecção.
    i_apply = src.index("_enrich_apply(")
    i_ruleset = src.index("evaluate_ruleset(")
    assert i_apply < i_ruleset, "E1 precisa vir ANTES de evaluate_ruleset"
    # E2 — nos DOIS flushes.
    assert src.count("await _enrich_remote_batch(") == 2


@pytest.mark.source_only
def test_enrichment_flag_off_deixa_o_hot_path_intocado():
    """Flag OFF ⇒ nenhum objeto instanciado e nenhuma chamada nova no laço.

    Este teste chamava-se ``..._is_off_by_default_and_guarded_in_the_hot_path``
    e afirmava ``ENRICHMENT_ENABLED is False``. O default virou True em ago/2026,
    porque o console ganhou página de enriquecimento e a flag OFF fazia o
    operador publicar política que não rodava, em silêncio.

    O que o teste protegia de verdade era a SEGUNDA metade do nome, e ela segue
    intacta: a flag desligada tem que zerar o custo. O default era o meio, não o
    fim, e travá-lo passou a impedir a correção em vez de proteger o produto.

    A verificação do desligamento saiu do default e virou COMPORTAMENTAL (ver
    ``test_flag_off_desliga_o_subsistema_de_verdade`` abaixo), o que tem mais
    dentes: antes bastava a constante estar False, agora o off precisa mesmo
    desligar.
    """
    src = Path(Path(applier_mod.__file__).parent.parent / "pipeline.py").read_text()
    # O call-site por evento é guardado por `is not None`, não pela flag: ler
    # settings por evento seria um atributo a mais no caminho quente.
    assert "if _enrich_local_res is not None:" in src
    assert "if settings.ENRICHMENT_ENABLED and organization_id is not None:" in src


def test_flag_off_desliga_o_subsistema_ANTES_de_tocar_o_banco(monkeypatch):
    """O off-switch funciona, independente de qual seja o default.

    É o que a asserção do default garantia por procuração, agora medido.

    O sinal NÃO pode ser "devolveu None": esta função devolve None também
    quando não há política, quando a org é None e quando o banco falha (ela é
    fail-closed em tudo). A primeira versão deste teste afirmava só isso, e
    passou numa mutação que REMOVIA o gate da flag. Teste verde pelo motivo
    errado é pior que teste ausente.

    O sinal com dentes é o curto-circuito: com a flag desligada, a função
    retorna sem NENHUMA sessão de banco.

    E o espião REGISTRA em vez de levantar. A segunda versão deste teste usava
    uma ``SessionLocal`` que lançava ``AssertionError``, e continuou passando
    sob mutação: a função tem um ``except Exception`` amplo (fail-closed de
    propósito, um problema de banco não pode derrubar a coleta) que engolia o
    próprio sentinela e devolvia None. Contador não é engolível.
    """
    from backend.app.collectors.enrich import runtime as enrich_runtime
    from backend.app.core.config import settings
    from backend.app.db import database as db_module

    chamadas = []

    def _espiao(*_a, **_k):
        chamadas.append(1)
        raise RuntimeError("não deveria chegar aqui")

    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", False, raising=False)
    monkeypatch.setattr(db_module, "SessionLocal", _espiao)

    # Org VÁLIDA de propósito: assim quem barra é a flag, não o fail-closed de
    # org ausente.
    resultado = enrich_runtime.load_policy_for_org(1)

    assert chamadas == [], (
        "abriu sessão de banco com ENRICHMENT_ENABLED=False — o gate da flag sumiu"
    )
    assert resultado is None


@pytest.mark.source_only
def test_enrich_names_are_initialized_before_the_try_block():
    """``finally`` referencia estes nomes — UnboundLocalError ali MASCARA o erro real.

    Regressão de um bug que eu mesmo introduzi e que a suíte pegou: inicializar
    dentro do ``try`` fez ``test_pipeline_early_failure_reraise`` falhar com
    ``UnboundLocalError: _enrich_stats`` em vez do ``_Boom: db down`` original —
    exatamente o modo de falha que o comentário de ``_inflight_*`` já descrevia.

    Este teste compara ÍNDICES, não presença: a primeira versão só verificava que
    a string existia no arquivo, e passava com a inicialização no lugar errado.
    """
    src = Path(Path(applier_mod.__file__).parent.parent / "pipeline.py").read_text()
    # Âncora: o ``try`` cujo ``finally`` faz os flushes finais. É o mesmo que
    # protege ``_metering_in``, então usamos a inicialização dele como marco.
    anchor = src.index("_metering_in = metering.InVolumeAccumulator()")
    body = src.index("# ── 1. Carrega Integration", anchor)
    for name in ("_enrich_apply = None", "_emit_enrich_stats = None",
                 "_enrich_runtime = None", "_enrich_stats = None",
                 "_enrich_policy = None", "_enrich_local_res = None"):
        pos = src.find(name, anchor)
        assert pos != -1, f"faltou inicializar {name!r}"
        assert pos < body, (
            f"{name!r} é inicializado DENTRO do try — uma falha antes dele "
            "deixaria o nome unbound e o finally mascararia o erro original"
        )


@pytest.mark.source_only
def test_enrichment_bytes_are_accounted_exactly_once():
    """O acréscimo é contabilizado no FLUSH, não nos dois seams.

    Creditar no seam local e de novo no remoto somaria a parte local duas vezes e
    inflaria ``bytes_added`` — que é o termo do KPI que o produto vende. O teste
    ancora a decisão na estrutura: o helper de contabilidade aparece UMA vez no
    caminho de dados, dentro do ``finally`` de ``_enrich_remote_batch``.
    """
    src = Path(Path(applier_mod.__file__).parent.parent / "pipeline.py").read_text()
    tree = ast.parse(src)

    # 1 definição + 1 chamada. Mais que isso = risco de dupla contagem.
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_account_enrichment_bytes"
    ]
    assert len(calls) == 1, (
        f"{len(calls)} chamadas a _account_enrichment_bytes — creditar em mais de "
        "um ponto soma a parte local duas vezes e infla bytes_added"
    )

    # A única chamada tem de viver no ``finally`` de ``_enrich_remote_batch``, que
    # roda no FLUSH: depois do seam local (por evento) E do remoto (por lote).
    # Ordenação textual não serve como prova — a função é DEFINIDA antes do laço.
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_enrich_remote_batch"
    )
    in_finally = [
        c for t in ast.walk(fn) if isinstance(t, ast.Try)
        for stmt in t.finalbody for c in ast.walk(stmt)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id == "_account_enrichment_bytes"
    ]
    assert in_finally, (
        "a contabilidade precisa estar no `finally` de _enrich_remote_batch: um "
        "lote já enriquecido localmente tem de ser contabilizado mesmo quando a "
        "resolução remota falha"
    )
    # E o acumulador é flushado no finally do ciclo, como o irmão do metering IN.
    assert "_metering_added.flush()" in src


def test_added_volume_accumulator_batches_and_is_noop_when_disabled():
    from backend.app.collectors.reduction.metering import (
        ADDING_REASONS,
        AddedVolumeAccumulator,
    )

    assert ADDING_REASONS == ("enrichment",)
    acc = AddedVolumeAccumulator(flush_events=2)
    # COST_METERING_ENABLED é False por default ⇒ add é no-op imediato.
    acc.add(1, "enrichment", 100.0)
    acc.add(None, "enrichment", 100.0)
    acc.flush()  # não pode levantar


def test_unenriched_batches_are_marked_never_silent(monkeypatch):
    """Produtor síncrono não enriquece — mas o evento DIZ isso ao chegar no destino.

    Sem a marca, um evento sem contexto é indistinguível de um que foi enriquecido e
    não casou; é o que faz o analista desconfiar da feature inteira. Fecha o antídoto
    nº 2 de §9.1 do ADR ("nunca omissão silenciosa"), que estava documentado como
    critério de aceite e não existia no código.
    """
    from backend.app.collectors.enrich.runtime import SKIP_REASONS, note_unenriched
    from backend.app.core.config import settings

    batch = [
        {"_centralops": {"event_id": "a"}},
        {"_centralops": {"event_id": "b", "enrichment": {"src": {"x": 1}}}},
        {"sem_centralops": True},
    ]
    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", False, raising=False)
    assert note_unenriched(batch, "producer_unsupported") == 0  # flag OFF ⇒ no-op

    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", True, raising=False)
    assert note_unenriched(batch, "producer_unsupported") == 1
    assert batch[0]["_centralops"]["enrichment_skipped"] == "producer_unsupported"
    # Já enriquecido no laço de coleta ⇒ NÃO é marcado (evita rotular re-despacho).
    assert "enrichment_skipped" not in batch[1]["_centralops"]
    # Envelope torto não derruba o despacho.
    assert "enrichment_skipped" not in batch[2]

    # Razão fora do vocabulário fechado cai no default, nunca vira string livre no
    # envelope entregue (cardinalidade no destino).
    fresh = [{"_centralops": {}}]
    note_unenriched(fresh, "razao-inventada")
    assert fresh[0]["_centralops"]["enrichment_skipped"] in SKIP_REASONS


@pytest.mark.source_only
def test_enqueue_dispatch_marks_unenriched_batches():
    """A marcação está no chokepoint dos 7 produtores, não espalhada."""
    src = Path(Path(applier_mod.__file__).parent.parent / "pipeline.py").read_text()
    i_def = src.index("def _enqueue_dispatch(")
    i_route = src.index("_enqueue_routed(batch, routes)", i_def)
    assert "note_unenriched(batch," in src[i_def:i_route], (
        "_enqueue_dispatch precisa marcar o lote ANTES de rotear"
    )


def test_load_policy_is_fail_closed_without_org():
    """Nunca enriquecer sem saber de quem é o evento (chave de cache é por org)."""
    from backend.app.collectors.enrich.runtime import load_policy_for_org

    assert load_policy_for_org(None) is None


# ── Radix / longest-prefix: o diferencial competitivo ───────────────────────

def test_radix_returns_the_most_specific_prefix():
    """Com /16 e /24 na tabela, o IP dentro do /24 recebe o /24.

    É a promessa que nem Vector nem Cribl cumprem nativamente. Um dict de igualdade
    devolveria nada; um 'primeiro que casar' devolveria o que viesse antes na lista.
    """
    from backend.app.collectors.enrich.radix import RadixTree

    tree, invalid = RadixTree.from_rows({
        "10.0.0.0/16": {"site": "matriz"},
        "10.0.5.0/24": {"site": "filial-sp"},
        "10.0.5.7/32": {"site": "servidor-critico"},
    })
    assert invalid == 0
    assert tree.search("10.0.9.1")["site"] == "matriz"
    assert tree.search("10.0.5.99")["site"] == "filial-sp"
    assert tree.search("10.0.5.7")["site"] == "servidor-critico"
    assert tree.search("192.168.1.1") is None


def test_radix_default_route_and_ipv6_are_separate_trees():
    """0.0.0.0/0 não pode capturar IPv6, e ::/0 não pode capturar IPv4.

    Unificar as árvores (mapeando IPv4 em ::ffff:a.b.c.d) faria um prefixo IPv6
    amplo capturar tráfego IPv4 sem ninguém perceber.
    """
    from backend.app.collectors.enrich.radix import RadixTree

    tree, _ = RadixTree.from_rows({
        "0.0.0.0/0": {"scope": "v4-default"},
        "2001:db8::/32": {"scope": "v6-doc"},
    })
    assert tree.search("8.8.8.8")["scope"] == "v4-default"
    assert tree.search("2001:db8::1")["scope"] == "v6-doc"
    # IPv6 fora do /32 não cai na default v4.
    assert tree.search("2001:dead::1") is None


def test_radix_counts_invalid_rows_instead_of_rejecting_the_file():
    """Três erros de digitação não podem derrubar 49.997 linhas boas."""
    from backend.app.collectors.enrich.radix import RadixTree

    tree, invalid = RadixTree.from_rows({
        "10.0.0.0/16": {"ok": 1},
        "nao-e-cidr": {"ok": 2},
        "999.999.999.0/24": {"ok": 3},
    })
    assert tree.entry_count == 1
    assert invalid == 2
    assert tree.search("10.0.0.1")["ok"] == 1


def test_radix_last_row_wins_on_duplicate_prefix():
    """Semântica de planilha: a última linha vale, não a primeira."""
    from backend.app.collectors.enrich.radix import RadixTree

    tree = RadixTree()
    tree.insert("10.0.0.0/8", {"v": "primeira"})
    tree.insert("10.0.0.0/8", {"v": "ultima"})
    assert tree.search("10.1.2.3")["v"] == "ultima"
    assert tree.entry_count == 1


def test_radix_bare_ip_is_treated_as_host_route():
    from backend.app.collectors.enrich.radix import RadixTree

    tree, invalid = RadixTree.from_rows({"10.0.5.7": {"host": True}})
    assert invalid == 0
    assert tree.search("10.0.5.7") == {"host": True}
    assert tree.search("10.0.5.8") is None


def test_customer_table_enrichers_are_registered_and_org_scoped():
    from backend.app.collectors.enrich import enrichers  # noqa: F401

    catalog = {c["name"]: c for c in registry_mod.describe_all()}
    assert catalog["table_cidr"]["key_kinds"] == ["ip"]
    assert catalog["table_exact"]["mode"] == "local"
    # Tabela do cliente NÃO faz egresso — é o que a torna CE e sem opt-in.
    assert catalog["table_cidr"]["egress"] == "none"
    assert catalog["table_exact"]["egress"] == "none"


def test_table_rule_requires_the_table_field():
    """Regra de tabela sem `table` falha no LOAD com mensagem acionável."""
    import asyncio

    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext

    reg = registry_mod.require("table_cidr")
    inst = reg.factory({})
    with pytest.raises(ValueError, match="exige o campo 'table'"):
        asyncio.run(inst.load(EnrichContext(organization_id=1)))


# ── Tabela do cliente contra banco REAL: isolamento e ponto-no-tempo ────────

@pytest.fixture()
def enrich_db(tmp_path):
    """SQLite em ARQUIVO com conexão por thread.

    Arquivo e não ``:memory:``+StaticPool porque ``_load_rows`` roda em
    ``asyncio.to_thread``: uma única conexão compartilhada entre threads produz
    linha "inexistente" e coluna vazia de forma intermitente — foi a causa raiz de
    dois flakes da suíte (ver ``conftest.make_threadsafe_sqlite_engine``).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import backend.app.db.database as db_module
    from backend.app.db import models as m
    from backend.app.db.database import Base

    engine = create_engine(
        f"sqlite:///{tmp_path/'enrich.db'}",
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    original = db_module.SessionLocal
    db_module.SessionLocal = Session
    try:
        yield Session, m
    finally:
        db_module.SessionLocal = original
        engine.dispose()


def _seed_table(Session, m, org_id: int, name: str, rows: dict, *, created_at=None):
    import json as _json
    from datetime import datetime

    with Session() as db:
        db.add(m.Organization(id=org_id, name=f"org-{org_id}", slug=f"org-{org_id}"))
        db.flush()
        table = m.EnrichmentTable(
            organization_id=org_id, name=name, match_mode="cidr", key_kind="ip"
        )
        db.add(table)
        db.flush()
        version = m.EnrichmentTableVersion(
            table_id=table.id,
            version_number=1,
            rows=_json.dumps(rows),
            entry_count=len(rows),
            commit_message="seed",
            created_at=created_at or datetime.utcnow(),
        )
        db.add(version)
        db.flush()
        table.current_version_id = version.id
        db.commit()
        return table.id


def test_customer_table_is_isolated_between_organizations(enrich_db):
    """Org A e Org B com tabelas de MESMO NOME não podem se enxergar.

    É a classe dos 11 gaps cross-org já fechados no projeto, e aqui o risco é maior:
    a tabela é o inventário do cliente. O teste usa o mesmo `name` de propósito —
    resolver por nome sem org é exatamente o bug que ele existe para impedir.
    """
    import asyncio

    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext

    Session, m = enrich_db
    _seed_table(Session, m, 1, "rede-corp", {"10.0.0.0/8": {"site": "org-A"}})
    _seed_table(Session, m, 2, "rede-corp", {"10.0.0.0/8": {"site": "org-B"}})

    reg = registry_mod.require("table_cidr")

    def load(org: int):
        return asyncio.run(
            reg.factory({}).load(EnrichContext(organization_id=org, table="rede-corp"))
        )

    assert load(1).lookup("10.1.2.3")["site"] == "org-A"
    assert load(2).lookup("10.1.2.3")["site"] == "org-B"


def test_customer_table_longest_prefix_end_to_end(enrich_db):
    """O caso do ADR: /16 e /24 na mesma tabela, o IP recebe o /24."""
    import asyncio

    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext

    Session, m = enrich_db
    _seed_table(Session, m, 7, "plano-rede", {
        "10.0.0.0/16": {"site": "matriz", "criticality": "normal"},
        "10.0.5.0/24": {"site": "filial-sp", "criticality": "alta"},
    })
    reg = registry_mod.require("table_cidr")
    table = asyncio.run(
        reg.factory({}).load(EnrichContext(organization_id=7, table="plano-rede"))
    )
    assert table.lookup("10.0.5.7")["site"] == "filial-sp"
    assert table.lookup("10.0.9.9")["site"] == "matriz"
    assert table.entry_count == 2


def test_point_in_time_resolves_the_version_that_was_current_then(enrich_db):
    """Backfill não pode enriquecer evento de maio com o inventário de agosto.

    Enriquecimento é time-dependent (um IP muda de dono). Sem ``as_of``, reprocessar
    90 dias produz atribuição errada — e, como a tag vira label de roteamento, o
    MESMO evento roteia diferente no live e no backfill.
    """
    import asyncio
    import json as _json
    from datetime import datetime, timedelta

    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext

    Session, m = enrich_db
    antigo = datetime(2026, 5, 1)
    table_id = _seed_table(
        Session, m, 9, "ativos", {"10.0.0.0/8": {"owner": "equipe-antiga"}},
        created_at=antigo,
    )
    # Versão nova, publicada em agosto.
    novo = datetime(2026, 8, 1)
    with Session() as db:
        v2 = m.EnrichmentTableVersion(
            table_id=table_id, version_number=2,
            rows=_json.dumps({"10.0.0.0/8": {"owner": "equipe-nova"}}),
            entry_count=1, commit_message="reorg", created_at=novo,
        )
        db.add(v2)
        db.flush()
        db.query(m.EnrichmentTable).filter(m.EnrichmentTable.id == table_id).update(
            {"current_version_id": v2.id}
        )
        db.commit()

    reg = registry_mod.require("table_cidr")

    def load(as_of):
        return asyncio.run(
            reg.factory({}).load(
                EnrichContext(organization_id=9, table="ativos", as_of=as_of)
            )
        )

    def epoch_ms(dt):
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)

    from datetime import timezone

    # Ao vivo (as_of=None) → versão corrente.
    assert load(None).lookup("10.1.1.1")["owner"] == "equipe-nova"
    # Evento de junho → a versão que valia em junho.
    assert load(epoch_ms(datetime(2026, 6, 15))).lookup("10.1.1.1")["owner"] == "equipe-antiga"
    # Evento de setembro → a versão nova.
    assert load(epoch_ms(datetime(2026, 9, 1))).lookup("10.1.1.1")["owner"] == "equipe-nova"
    # Evento ANTERIOR à primeira versão → vazio, não a versão corrente.
    assert load(epoch_ms(datetime(2026, 1, 1))).entry_count == 0


def test_missing_table_raises_actionable_error(enrich_db):
    import asyncio

    from backend.app.collectors.enrich import enrichers  # noqa: F401
    from backend.app.collectors.enrich.contract import EnrichContext

    reg = registry_mod.require("table_exact")
    with pytest.raises(LookupError, match="não existe na org"):
        asyncio.run(
            reg.factory({}).load(EnrichContext(organization_id=99, table="fantasma"))
        )


def test_bulk_resolution_distinguishes_unresolved_from_miss():
    res = BulkResolution({("r1", "a"): {"score": 1}, ("r1", "b"): None})
    assert res.get("r1", "a") == (True, {"score": 1})
    assert res.get("r1", "b") == (True, None)      # miss confirmado
    assert res.get("r1", "c") == (False, None)     # não resolvida (cota/erro)


# ── Regressões da revisão adversarial (ago/2026) ─────────────────────────────
# Os quatro defeitos abaixo passaram pela suíte original porque cada UNIDADE
# estava correta e o erro morava no CHAMADOR. `test_bulk_resolution_distinguishes
# _unresolved_from_miss`, logo acima, é o exemplo: a estrutura sempre soube
# separar UNKNOWN de MISS — quem preenchia é que não sabia.


def test_distinct_keys_respects_the_when_gate():
    """Chave gatada por ``when`` NÃO pode ser enviada ao enricher remoto.

    No seam remoto o ``when`` é o controle de EGRESSO, não só de escrita
    (ADR-LOCAL-0002 §4.1): avaliá-lo apenas no applier deixaria a chamada externa
    já ter acontecido — o indicador do cliente sairia para o terceiro e a cota
    seria gasta com o que a política proibia consultar, sem nada no payload
    denunciando isso.
    """
    from backend.app.collectors.enrich.runtime import _distinct_keys

    policy = compile_policy([
        _rule(
            id="vt",
            enricher="virustotal",
            mode="remote",
            when={"lacks_tag": ["asset_known"]},
            outputs=[{"from": "malicious", "target": "_centralops.enrichment.vt.m"}],
        )
    ])
    rule = policy.remote_rules()[0]

    gated = _envelope("10.0.0.1")
    gated["_centralops"]["enrichment_tags"] = ["asset_known"]  # gate FALSO
    allowed = _envelope("10.0.0.2")                            # gate VERDADEIRO

    assert _distinct_keys([gated], rule) == []
    assert _distinct_keys([allowed], rule) == ["10.0.0.2"]
    # Dedup não pode descartar a chave de quem passa por causa de quem não passa.
    same_key_gated = _envelope("10.0.0.2")
    same_key_gated["_centralops"]["enrichment_tags"] = ["asset_known"]
    assert _distinct_keys([same_key_gated, allowed], rule) == ["10.0.0.2"]


def test_key_omitted_by_provider_stays_unknown_not_confirmed_miss():
    """Chave que o provedor OMITIU não pode virar miss confirmado.

    Omissão = cota 429, erro por chave, corte por ``max_keys_per_batch``. Gravá-la
    como ``None`` faria o applier rodar ``on_miss`` e FABRICAR veredito limpo com
    proveniência falsa — um "0 detecções" que o analista leria como consultado.
    """
    from backend.app.collectors.enrich.runtime import BulkResolution

    owned = ["a", "b", "c"]
    resolved = {"a": {"malicious": 3}, "b": None}  # "c" omitida pelo provedor

    rows = {}
    for key in owned:
        if key in resolved:            # ← o predicado corrigido
            rows[("vt", key)] = resolved[key]

    res = BulkResolution(rows)
    assert res.get("vt", "a") == (True, {"malicious": 3})  # hit
    assert res.get("vt", "b") == (True, None)              # miss CONFIRMADO
    assert res.get("vt", "c") == (False, None)             # UNKNOWN — applier pula


def test_written_value_is_copied_not_aliased_to_the_resident_table():
    """Escrever a referência da linha da tabela corrompe o estado residente.

    Consequências reais, todas reproduzidas: a linha da tabela é mutada; o evento
    seguinte herda o campo mesmo com o gate FALSO (bypass silencioso de ``when``,
    com ``_sources`` mentindo sobre a origem); e dois outputs com o mesmo ``from``
    produzem envelope auto-referente, cujo dump derruba o lote inteiro no despacho.
    """
    rows = {"10.0.0.1": {"asset": {"site": "matriz"}, "zone": "dmz"}}
    table = DictLookupTable(rows)

    policy = compile_policy([
        _rule(
            id="a",
            enricher="table_exact",
            table="t",
            outputs=[{"from": "asset", "target": "_centralops.enrichment.asset"}],
        )
    ])
    stats = ApplyStats()
    env = _envelope("10.0.0.1")
    apply(env, policy.local_rules(), TableResolution({"a": table}), stats)

    written = env["_centralops"]["enrichment"]["asset"]
    assert written == {"site": "matriz"}
    # O valor escrito NÃO é o objeto da tabela.
    assert written is not rows["10.0.0.1"]["asset"]
    written["contaminado"] = True
    assert "contaminado" not in rows["10.0.0.1"]["asset"], (
        "mutar o valor escrito no evento alterou a tabela residente"
    )


def test_dsl_rejects_unknown_key_inside_when():
    """O gate era o único objeto da DSL sem recusa de chave desconhecida.

    Sem ela, ``lacks_tags`` (plural, digitado por engano) compilava com o gate
    reduzido a ``exists`` e a condição escrita pelo operador sumia sem 422 nem log
    — fail-OPEN no campo que decide egresso, numa DSL vendida como fail-closed.
    """
    with pytest.raises(EnrichmentConfigError, match="desconhecida"):
        compile_policy([
            _rule(when={"exists": "normalized.src_endpoint.ip", "lacks_tags": ["x"]})
        ])


def test_shared_source_resolves_for_child_org_only(enrich_db):
    """A fonte da matriz resolve para a filha na lista, e não para uma org de fora.

    Prova que o compartilhamento MSP não afrouxa o isolamento: o join do runtime
    é pela lista de orgs, então quem não está nela continua recebendo LookupError.
    """
    from backend.app.collectors.enrich.runtime import EnrichRuntime

    Session, m = enrich_db
    MATRIZ, FILHA, DE_FORA = 1, 2, 3
    with Session() as db:
        src = m.EnrichmentSource(
            id="src-msp",
            organization_id=MATRIZ,
            name="vt-compartilhada",
            enricher="virustotal",
            config='{"max_keys_per_batch": 10}',
            secret_ref="ciphertext-fake",
            enabled=True,
        )
        db.add(src)
        for org in (MATRIZ, FILHA):
            db.add(m.EnrichmentSourceOrg(source_id="src-msp", organization_id=org))
        db.commit()

    rt = EnrichRuntime(max_table_bytes=1_000_000, lru_bytes=1_000_000)
    for org in (MATRIZ, FILHA):
        cfg, ref = rt._resolve_source(org, "vt-compartilhada")
        assert ref == "ciphertext-fake"
        assert cfg["max_keys_per_batch"] == 10
        rt._sources_this_cycle = {}  # o cache é por ciclo

    with pytest.raises(LookupError, match="não existe"):
        rt._resolve_source(DE_FORA, "vt-compartilhada")


def test_source_cache_is_cleared_between_cycles(enrich_db):
    """Credencial resolvida não pode atravessar ciclo.

    O ciclo é mono-tenant, então uma entrada sobrevivente seria a credencial de
    uma org servida no ciclo da próxima.
    """
    from backend.app.collectors.enrich.runtime import EnrichRuntime

    Session, m = enrich_db
    with Session() as db:
        db.add(m.EnrichmentSource(
            id="src-1", organization_id=1, name="f", enricher="virustotal",
            config="{}", secret_ref="ref-1", enabled=True,
        ))
        db.add(m.EnrichmentSourceOrg(source_id="src-1", organization_id=1))
        db.commit()

    rt = EnrichRuntime(max_table_bytes=1_000, lru_bytes=1_000)
    rt.begin_cycle(1)
    rt._resolve_source(1, "f")
    assert rt._sources_this_cycle, "deveria ter cacheado dentro do ciclo"
    rt.end_cycle()
    assert rt._sources_this_cycle == {}
    rt.begin_cycle(1)
    assert rt._sources_this_cycle == {}
