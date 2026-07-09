"""Open-core boundary gate.

Enforce, as tests that run in the normal Community CI suite, the invariants that
keep the **Community (AGPL) artifact clean** of proprietary material:

1. The proprietary ``centralops_ee`` package is NOT vendored into this repo.
2. The frontend ``web-ee`` workspace is NOT vendored either.
3. The ONLY ``import centralops_ee`` in the Core is the single guarded discovery
   hook in ``core/edition.py`` (``activate_enterprise``) — never a hard import.
4. With the EE absent (the Community case), ``activate_enterprise`` resolves to
   Community (returns ``False``) and importing the EE raises ``ImportError``.
5. No Stripe dependency or import leaks into the Community backend (billing lives
   only in the separate commercial repo).
6. No Ed25519/RSA **private** key material is committed anywhere in the tree, and
   the bundled license keyring ships with only public material (README).

These mirror the CI ``openness-gate.yml`` (defense-in-depth across the whole tree)
but run inline so a regression fails fast, locally and in PR.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# backend/tests/test_open_core_boundary.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_APP = _REPO_ROOT / "backend" / "app"
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"

# Importação REAL (não comentário) de um símbolo/caminho que só existe no overlay EE
# (web-ee): o painel partner/reseller. Casa `import ... from "...web-ee..."`,
# `from "@centralops/web-ee..."`, e `import { PartnerTenantsPanel | AutoApprovePolicyModal }`.
_EE_FRONTEND_IMPORT_RE = re.compile(
    r"""(?x)
    ^\s*(?:import|export)\b .* \bfrom\b \s* ['"][^'"]*
        (?:@centralops/web-ee | /web-ee/ | web-ee)        # caminho do overlay
        [^'"]* ['"]
    | ^\s*import\b [^;\n]* \b(?:PartnerTenantsPanel|AutoApprovePolicyModal)\b  # símbolo EE
    """
)

# PEM header for ANY private key flavor (RSA/EC/OpenSSH/PKCS8/DSA) — Python regex.
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
# POSIX-ERE equivalent for ``git grep -E`` (no non-capturing groups in ERE).
_PRIVATE_KEY_ERE = r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"


def _git(*args: str) -> "subprocess.CompletedProcess[str] | None":
    """Roda git; ``None`` quando o binário ``git`` não existe (ex.: imagem Cython
    compilada do CI, que não embarca git nem o .git) — o chamador então pula. A
    varredura autoritativa de chave-privada roda no workflow openness-gate (runner
    com git)."""
    try:
        return subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None


def test_ee_package_not_vendored() -> None:
    """O artefato Community NÃO pode conter o pacote proprietário centralops_ee."""
    candidates = [
        _REPO_ROOT / "centralops_ee",
        _REPO_ROOT / "backend" / "centralops_ee",
        _BACKEND_APP / "centralops_ee",
    ]
    leaked = [str(p.relative_to(_REPO_ROOT)) for p in candidates if p.exists()]
    assert not leaked, f"pacote EE vendorado no artefato Community: {leaked}"


def test_web_ee_not_vendored() -> None:
    """O workspace de frontend EE (web-ee) NÃO pode estar no artefato Community."""
    candidates = [
        _REPO_ROOT / "frontend" / "web-ee",
        _REPO_ROOT / "web-ee",
        _REPO_ROOT / "frontend" / "src" / "web-ee",
    ]
    leaked = [str(p.relative_to(_REPO_ROOT)) for p in candidates if p.exists()]
    assert not leaked, f"workspace web-ee vendorado no artefato Community: {leaked}"


def test_only_guarded_ee_import() -> None:
    """A ÚNICA referência de import a centralops_ee no Core é o hook guardado em
    edition.py (activate_enterprise). Qualquer import direto em outro módulo
    quebraria o arrow de dependência EE→Core e falharia o boot Community."""
    import_re = re.compile(r"^\s*(?:import|from)\s+centralops_ee\b")
    offenders: list[str] = []
    for py in _BACKEND_APP.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if import_re.match(line):
                rel = py.relative_to(_REPO_ROOT)
                if py.name != "edition.py" or "core" not in py.parts:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "import de centralops_ee fora do hook guardado em core/edition.py:\n"
        + "\n".join(offenders)
    )


def test_ee_absent_resolves_to_community() -> None:
    """Sem o pacote EE instalado (caso Community), activate_enterprise → False e
    importar o EE levanta ImportError (fail-closed-to-Community)."""
    import sys

    from backend.app.core import edition

    # Order-independent: descarta qualquer módulo-fake injetado por outro teste
    # (ex.: test_edition_activation) — queremos provar a AUSÊNCIA real do pacote.
    sys.modules.pop("centralops_ee", None)

    with pytest.raises(ImportError):
        import centralops_ee  # type: ignore  # noqa: F401

    class _FakeApp:  # nunca usado: o import falha antes de activate(app)
        pass

    assert edition.activate_enterprise(_FakeApp()) is False


def test_no_stripe_dependency_or_import() -> None:
    """Billing (Stripe) vive só no repo comercial — não pode vazar no Community."""
    for req_name in ("requirements.txt", "requirements.lock"):
        req = _REPO_ROOT / "backend" / req_name
        if not req.exists():
            continue
        for raw in req.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[=<>!~\[ ]", line, 1)[0].strip().lower()
            assert pkg != "stripe", f"dependência Stripe em backend/{req_name}: {raw!r}"

    import_re = re.compile(r"^\s*(?:import|from)\s+stripe\b")
    offenders = [
        f"{py.relative_to(_REPO_ROOT)}"
        for py in _BACKEND_APP.rglob("*.py")
        if any(import_re.match(l) for l in py.read_text(encoding="utf-8").splitlines())
    ]
    assert not offenders, f"import de stripe no backend Community: {offenders}"


def test_no_private_keys_committed() -> None:
    """Nenhuma chave PRIVADA (Ed25519/RSA/EC/OpenSSH) pode estar commitada — a
    chave de assinatura de licença vive SÓ no billing-plane (KMS/Vault)."""
    # ``-e`` é obrigatório: o padrão começa com '-----' (senão git lê como flag).
    res = _git("grep", "-lI", "-E", "-e", _PRIVATE_KEY_ERE, "HEAD")
    if res is None or res.returncode not in (0, 1):
        # git ausente (imagem compilada) ou não-repo → o gate de CI cobre isto.
        pytest.skip("git indisponível ou não é repositório git")
    # git grep HEAD prefixa "HEAD:<path>". Exclui ESTE arquivo, que contém os
    # padrões PEM como strings de regex (não material de chave real).
    hits = [
        ln for ln in res.stdout.splitlines()
        if ln.strip() and not ln.endswith("test_open_core_boundary.py")
    ]
    assert not hits, f"material de chave PRIVADA commitado: {hits}"


def test_license_keyring_ships_without_private_keys() -> None:
    """O keyring embutido (license_keys/) só carrega material PÚBLICO; em repo
    ele ship vazio (só README) → Community."""
    keys_dir = _BACKEND_APP / "core" / "license_keys"
    assert keys_dir.is_dir(), "diretório license_keys/ ausente"
    for f in keys_dir.iterdir():
        if f.is_file() and f.suffix in {".pem", ".key", ".txt"}:
            text = f.read_text(encoding="utf-8", errors="ignore")
            assert not _PRIVATE_KEY_RE.search(text), (
                f"chave PRIVADA no keyring embutido: {f.name}"
            )


# ── Carve-out: o motor de busca federada é uma trava Enterprise ───────
# O engine de query federada async (orquestração, tasks, routers,
# tradução Sigma, quota) foi movido para ``centralops_ee``. Só o MOTOR saiu; ficam no
# core Community os modelos ORM (QueryJob/CorrelationRule/Detection — formas de dado
# entrelaçadas via ``SearchResult.query_job_id``), os repositórios, a execução
# por-provider, a fila ``collect.query`` e a TRIAGEM de Detection (o scheduler
# Community também emite Detection — triagem é SOC base, não trava paga).
_CARVED_OUT_ENGINE = (
    "services/query_service.py",
    "services/query_quota.py",
    "services/query_sigma.py",
    "services/correlation_service.py",
    "collectors/query_tasks.py",
    "routers/query_jobs.py",
    "routers/correlation_rules.py",
)


def test_federated_query_engine_not_in_community() -> None:
    """O motor de query federada (orquestração + tasks + routers) NÃO pode estar no
    artefato Community — vive só no ``centralops_ee``. Forkar o gate de
    runtime não reconstrói o motor: ele simplesmente não está no artefato AGPL."""
    leaked = [p for p in _CARVED_OUT_ENGINE if (_BACKEND_APP / p).exists()]
    assert not leaked, f"motor de query federada vazado no Community: {leaked}"


def test_query_service_orchestrator_not_importable_in_community() -> None:
    """A classe orquestradora ``QueryService`` (o IP da federação) não existe no core."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.app.services.query_service")


def test_core_app_keeps_detections_but_not_federated_query() -> None:
    """O app Community monta ``/api/detections`` (triagem = SOC base; o scheduler
    Community emite Detection) mas NÃO ``/api/query-jobs`` nem ``/api/correlation-rules``
    (trava EE — montados só por ``centralops_ee.activate``)."""
    from backend.app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert any(p.startswith("/api/detections") for p in paths), (
        "detections deveria permanecer no Community (triagem base)"
    )
    assert not any(p.startswith("/api/query-jobs") for p in paths), "/api/query-jobs vazou no Community"
    assert not any(p.startswith("/api/correlation-rules") for p in paths), (
        "/api/correlation-rules vazou no Community"
    )


def test_community_frontend_imports_no_ee_component() -> None:
    """O bundle Community (frontend) não pode conter o painel partner/reseller do EE.

    Garantia do lado do front: nenhuma fonte Community IMPORTA um símbolo/caminho do
    overlay web-ee (PartnerTenantsPanel/AutoApprovePolicyModal/@centralops/web-ee) — se
    nenhuma fonte importa, o Vite não pode empacotá-lo. (Comentários/docstrings que
    citam os nomes são ignorados — só linhas de import contam.)"""
    if not _FRONTEND_SRC.is_dir():
        pytest.skip("frontend/src ausente")
    offenders: list[str] = []
    for ext in ("*.ts", "*.tsx"):
        for src in _FRONTEND_SRC.rglob(ext):
            for i, line in enumerate(src.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if _EE_FRONTEND_IMPORT_RE.match(line):
                    offenders.append(f"{src.relative_to(_REPO_ROOT)}:{i}: {line.strip()}")
    assert not offenders, "import de componente EE na fonte Community do frontend:\n" + "\n".join(offenders)


def test_community_frontend_ee_seams_are_stubs() -> None:
    """As fachadas @/ee/* do Community devem ser STUBS: routes vazio + slot que não
    importa o PartnerTenantsPanel (o overlay EE as sobrescreve via alias no build EE)."""
    if not _FRONTEND_SRC.is_dir():
        pytest.skip("frontend/src ausente")
    slot = _FRONTEND_SRC / "ee" / "integrationDetailSlots.tsx"
    routes = _FRONTEND_SRC / "ee" / "routes.tsx"
    assert slot.is_file() and routes.is_file(), "stubs @/ee/* ausentes"
    slot_imports = [
        l for l in slot.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*import\b", l) and "PartnerTenantsPanel" in l
    ]
    assert not slot_imports, f"o stub Community do slot importa o painel EE: {slot_imports}"
    assert re.search(r"eeRoutes\s*[:=].*\[\s*\]", routes.read_text(encoding="utf-8")), (
        "o stub Community @/ee/routes deve exportar eeRoutes vazio"
    )
