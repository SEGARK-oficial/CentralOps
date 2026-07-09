"""Backend de KMS via HashiCorp Vault **Transit** secrets engine.

Implementa :class:`~backend.app.core.secrets.kms.KmsBackend` (wrap/unwrap de
DEKs) usando o *Transit engine* do Vault — **encryption-as-a-service**: a
master key **nunca** sai do Vault; a aplicação só pede "cifra/decifra esta
DEK". É o caminho enterprise para o envelope encryption do
:class:`~backend.app.core.secrets.kms_wrapped_fernet.KmsWrappedFernetBackend`.

Compatibilidade: o Transit engine é **open-source** — funciona em **Vault
Community Edition** (CE) self-hosted, sem HCP nem Enterprise. Apenas
*Namespaces* e *auto-unseal por HSM* exigem Enterprise, e este backend não
depende deles (o isolamento por tenant, quando desejado, é via *templated
policy*, que é CE). ``VAULT_NAMESPACE`` é aceito como forward-compat (no-op em
CE).

Resiliência de boot: o ``__init__`` faz **só validação de config —
ZERO rede**. A autenticação e a leitura da versão da chave acontecem de forma
**preguiçosa** (``_ensure_authed``) na 1ª operação de wrap/unwrap. Assim, um
Vault indisponível no boot NÃO derruba o import da app (que é amplo, via
``crypto.py``) — degrada e re-tenta, como o ``_wait_for_db`` do Postgres.

Concorrência: o backend é um singleton por processo usado por N threads de
worker. A autenticação (inicial e o re-login pós-403) é serializada E
**deduplicada** por um lock + contador de geração (double-checked) — N threads
que expiram juntas disparam **um** login, não N.

Autenticação: ``token`` (estático) ou ``approle`` (recomendado p/ serviços —
role_id + secret_id; o token de sessão expira e o re-login é automático em 403).

Síncrono por contrato (``hvac`` é síncrono), coerente com ``SecretsBackend``.
O ``KmsWrappedFernetBackend`` cacheia as DEKs desempacotadas (TTL), amortizando
o round-trip ao Vault nos paths quentes.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Callable, Optional, Tuple, TypeVar

from .kms import KmsBackend

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_TIMEOUT_SECONDS = 5.0


class VaultTransitBackend(KmsBackend):
    """KMS backend usando o Vault Transit engine (``wrap``/``unwrap`` de DEK).

    Args:
        addr: URL do Vault (ex.: ``https://vault.interno:8200``).
        key_name: nome da chave Transit (ex.: ``centralops``). Deve existir
            (``vault write -f transit/keys/<name>``).
        mount_point: mount do engine Transit (default ``transit``).
        auth_method: ``"token"`` ou ``"approle"``.
        token: token estático (auth_method=token).
        role_id / secret_id: credenciais AppRole (auth_method=approle).
        approle_mount: mount do auth AppRole (default ``approle``).
        namespace: Vault Enterprise namespace (no-op em CE; forward-compat).
        verify_tls: validar o certificado TLS do Vault (default True).
        timeout_seconds: timeout (connect+read) das chamadas ao Vault.
    """

    def __init__(
        self,
        *,
        addr: str,
        key_name: str,
        mount_point: str = "transit",
        auth_method: str = "token",
        token: Optional[str] = None,
        role_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        approle_mount: str = "approle",
        namespace: Optional[str] = None,
        verify_tls: bool = True,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # ── Validação de config — PURA, sem rede (resiliência de boot) ──
        if not addr:
            raise ValueError("VaultTransitBackend exige VAULT_ADDR.")
        if not key_name:
            raise ValueError("VaultTransitBackend exige VAULT_TRANSIT_KEY_NAME.")

        self._auth_method = auth_method.strip().lower()
        if self._auth_method == "token":
            if not token:
                raise ValueError(
                    "VaultTransitBackend auth_method=token exige VAULT_TOKEN."
                )
        elif self._auth_method == "approle":
            if not (role_id and secret_id):
                raise ValueError(
                    "VaultTransitBackend auth_method=approle exige "
                    "VAULT_ROLE_ID e VAULT_SECRET_ID."
                )
        else:
            raise ValueError(
                f"VAULT_AUTH_METHOD inválido: '{self._auth_method}'. "
                "Use 'token' ou 'approle'."
            )

        self._key_name = key_name
        self._mount = mount_point
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._approle_mount = approle_mount

        # Estado de auth preguiçosa. _auth_generation muda a cada (re)login —
        # o double-check no re-auth usa isso p/ deduplicar logins concorrentes.
        self._auth_lock = threading.Lock()
        self._authed = False
        self._auth_generation = 0
        self._key_version = "?"  # populado no 1º uso (lazy)

        # Import e construção do client NÃO fazem rede (só login/encrypt fazem).
        try:
            import hvac
        except ModuleNotFoundError as exc:  # pragma: no cover - ambiente sem hvac
            raise RuntimeError(
                "VaultTransitBackend requer o pacote 'hvac' (não instalado). "
                "Instale: pip install -r requirements-vault.txt."
            ) from exc

        self._hvac = hvac
        # Exceções de transporte (ConnectionError/ReadTimeout) NÃO são
        # hvac.exceptions.VaultError — capturamos as duas famílias p/ normalizar
        # qualquer falha do Vault em ValueError (sem vazar stack/path). requests
        # é dependência transitiva do hvac; resiliente se ausente.
        transport_excs: Tuple[type, ...] = ()
        try:
            import requests

            transport_excs = (requests.exceptions.RequestException,)
        except ModuleNotFoundError:  # pragma: no cover
            pass
        self._vault_excs: Tuple[type, ...] = (
            hvac.exceptions.VaultError,
            *transport_excs,
        )
        self._client = hvac.Client(
            url=addr,
            namespace=namespace or None,
            verify=verify_tls,
            timeout=timeout_seconds,
        )
        logger.info(
            "SecretsBackend KMS: VaultTransitBackend configurado (addr=%s, "
            "mount=%s, key=%s, auth=%s) — auth preguiçosa no 1º uso.",
            addr, self._mount, self._key_name, self._auth_method,
        )

    # ── Auth preguiçosa + double-checked ──────────────────────────────

    def _ensure_authed(self) -> None:
        """Autentica na 1ª chamada (lazy). Idempotente e thread-safe.

        Sob outage do Vault, levanta (a exceção é normalizada a ValueError
        pelos callers wrap/unwrap); ``_authed`` fica False → re-tenta no
        próximo uso (resiliente, sem travar o boot).
        """
        if self._authed:
            return
        with self._auth_lock:
            if self._authed:
                return
            self._authenticate()
            self._key_version = self._read_key_version()
            self._authed = True
            self._auth_generation += 1

    def _authenticate(self) -> None:
        """(Re)autentica o client. Token: sem rede. AppRole: login (rede)."""
        if self._auth_method == "token":
            self._client.token = self._token
        else:  # approle (validado no __init__)
            resp = self._client.auth.approle.login(
                role_id=self._role_id,
                secret_id=self._secret_id,
                mount_point=self._approle_mount,
            )
            self._client.token = resp["auth"]["client_token"]

    def _call(self, fn: Callable[[], _T]) -> _T:
        """Garante auth (lazy) e executa ``fn``; re-loga 1x em 403 (AppRole).

        Re-auth é **double-checked** por geração: se outra thread já re-logou
        enquanto esperávamos o lock, NÃO re-logamos de novo — um login serve as
        N threads que expiraram juntas.
        """
        self._ensure_authed()
        gen = self._auth_generation
        try:
            return fn()
        except self._hvac.exceptions.Forbidden:
            if self._auth_method != "approle":
                raise
            with self._auth_lock:
                if self._auth_generation == gen:
                    logger.info(
                        "VaultTransitBackend: token expirado — re-autenticando (AppRole)."
                    )
                    self._authenticate()
                    self._auth_generation += 1
            return fn()

    # ── API KmsBackend ────────────────────────────────────────────────

    def wrap(self, plaintext_dek: bytes) -> bytes:
        """Cifra a DEK via Transit (``transit/encrypt/<key>``).

        Returns:
            O ciphertext do Vault (``vault:v1:...``) como bytes — opaco.

        Raises:
            ValueError: se o Vault recusar/estiver inacessível (erro
                normalizado — não vaza path/policy/stack do Vault).
        """
        b64_plaintext = base64.b64encode(plaintext_dek).decode("ascii")

        def _do() -> str:
            resp = self._client.secrets.transit.encrypt_data(
                name=self._key_name,
                plaintext=b64_plaintext,
                mount_point=self._mount,
            )
            return resp["data"]["ciphertext"]

        try:
            ciphertext = self._call(_do)
        except self._vault_excs as exc:
            raise ValueError(
                f"Vault Transit recusou o wrap da DEK ({type(exc).__name__})."
            ) from exc
        return ciphertext.encode("ascii")

    def unwrap(self, wrapped_dek: bytes) -> bytes:
        """Decifra um wrapped DEK via Transit (``transit/decrypt/<key>``).

        Raises:
            ValueError: ciphertext não-ASCII (corrompido/de outro provedor) ou
                Vault recusou/inacessível (erro normalizado, sem vazar detalhe).
        """
        try:
            ciphertext = wrapped_dek.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "wrapped_dek não-ASCII — corrompido ou cifrado por outro "
                "provedor de KMS (ex.: troca de KMS_PROVIDER sem re-encrypt)."
            ) from exc

        def _do() -> str:
            resp = self._client.secrets.transit.decrypt_data(
                name=self._key_name,
                ciphertext=ciphertext,
                mount_point=self._mount,
            )
            return resp["data"]["plaintext"]

        try:
            b64_plaintext = self._call(_do)
        except self._vault_excs as exc:
            # Não vaza detalhe do Vault (pode conter path/policy) — só o tipo.
            raise ValueError(
                "Vault Transit recusou o unwrap da DEK "
                f"({type(exc).__name__}) — chave incorreta ou Vault inacessível."
            ) from exc
        return base64.b64decode(b64_plaintext)

    def key_id(self) -> str:
        """Identificador da chave: ``vault-transit:<mount>/<key>:v<versão>``.

        I/O-free: retorna a versão CACHEADA (``v?`` até a 1ª operação real, que
        a popula via ``_ensure_authed``). Não dispara rede — para o boot e o log
        do factory nunca dependerem do Vault estar de pé.
        """
        return f"vault-transit:{self._mount}/{self._key_name}:v{self._key_version}"

    # ── Helpers ───────────────────────────────────────────────────────

    def _read_key_version(self) -> str:
        """Lê a versão corrente da chave (sob o lock de _ensure_authed)."""
        try:
            resp = self._client.secrets.transit.read_key(
                name=self._key_name, mount_point=self._mount
            )
            return str(resp["data"].get("latest_version", "?"))
        except Exception as exc:  # pragma: no cover - leitura é best-effort
            logger.warning(
                "VaultTransitBackend: não foi possível ler a versão da chave "
                "'%s' (%s) — key_id usará '?'.",
                self._key_name, type(exc).__name__,
            )
            return "?"
