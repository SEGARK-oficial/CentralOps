"""Todo pacote sob ``app/services`` DEVE ser importável.

Guard de integridade estrutural. Um pacote que não importa é indistinguível, para
o resto do sistema, de um pacote que não existe — e o modo de falha é silencioso:
nenhum consumidor aparece, ninguém reclama, e a capacidade fica "na prateleira"
por meses parecendo pronta.

Motivo concreto (ADR-0015, Fase 0): ``app/services/threat_intel/__init__.py`` fazia
``from .blacklist import BlacklistManager, get_blacklist_manager`` e o módulo
``blacklist.py`` NÃO EXISTIA no pacote. ``import ...threat_intel`` levantava
``ModuleNotFoundError``. As ~1.839 linhas do subsistema (3 tiers de cache, AbuseIPDB,
OTX, consensus, rotação de chaves) eram inalcançáveis — o que explica os zero
consumidores fora do próprio pacote.

Este teste custa milissegundos e fecha a classe inteira: qualquer ``__init__`` que
reexporte um símbolo inexistente, qualquer módulo removido sem limpar o import,
qualquer dependência opcional importada no topo sem guarda passa a reprovar o CI em
vez de morrer em silêncio.

NB: importar é diferente de exercitar. Este guard NÃO afirma que o pacote funciona —
afirma apenas que ele é alcançável, que é a pré-condição de tudo mais.
"""

from __future__ import annotations

import importlib
import os
import pkgutil

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

# `backend.app.*` e NUNCA `app.*`: o gate compilado tem dual-root e o primeiro
# import do .so congela a resolução — usar `app.*` passa aqui e quebra na imagem.
import backend.app.services as services_pkg

# Dependências de terceiros que são OPCIONAIS por design (extras não instalados no
# ambiente base). Um ImportError cujo nome-raiz esteja aqui é esperado, não regressão.
# Comparação pela raiz do nome do módulo faltante — ver ``_missing_root``.
_OPTIONAL_THIRD_PARTY: frozenset[str] = frozenset(
    {
        "hvac",          # requirements-vault.txt  (KMS Vault Transit)
        "sigma",         # requirements-query-abstractions.txt (pySigma)
        "pysigma",
        "opentelemetry",  # requirements-otel.txt
    }
)


def _missing_root(exc: ImportError) -> str:
    """Raiz do módulo ausente ('a.b.c' -> 'a'). '' quando o nome não vier no erro."""
    name = getattr(exc, "name", None) or ""
    return name.split(".", 1)[0]


def _iter_service_modules() -> list[str]:
    """Nomes totalmente qualificados de todo módulo/pacote sob ``app/services``."""
    return [
        name
        for _, name, _ in pkgutil.walk_packages(
            services_pkg.__path__, prefix=f"{services_pkg.__name__}."
        )
    ]


def test_services_package_is_not_empty():
    """Sanidade do próprio guard: se a varredura voltar vazia, ele não guarda nada."""
    modules = _iter_service_modules()
    assert modules, (
        "walk_packages não encontrou nenhum módulo sob app/services — o guard estaria "
        "passando por vacuidade. Verifique __path__ do pacote."
    )


@pytest.mark.source_only
@pytest.mark.parametrize("module_name", _iter_service_modules())
def test_service_module_imports(module_name: str):
    """Importa cada módulo sob ``app/services``; falha ruidosamente no que quebrar.

    ``source_only``: na imagem Cython os módulos viram .so e ``walk_packages`` não
    os enumera da mesma forma — o guard vale sobre a árvore de fontes, que é onde o
    erro é introduzido.
    """
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        root = _missing_root(exc)
        if root in _OPTIONAL_THIRD_PARTY:
            pytest.skip(f"dependência opcional ausente ({root}): {exc}")
        pytest.fail(
            f"{module_name} não é importável: {type(exc).__name__}: {exc}\n"
            "Um pacote que não importa é inalcançável pelo resto do sistema. "
            "Corrija o import quebrado ou remova o módulo — não deixe a casca."
        )
