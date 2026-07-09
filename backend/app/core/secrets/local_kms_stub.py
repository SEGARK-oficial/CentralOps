"""Stub de KMS para dev/test — master key armazenada em arquivo local.

**NÃO USAR EM PRODUÇÃO.** Em produção use ``AwsKmsBackend`` (boto3 +
``kms:Encrypt``/``kms:Decrypt``) ou ``HashicorpVaultBackend`` (Transit
Engine). Este stub existe apenas para viabilizar testes de integração do
fluxo completo de envelope encryption sem depender de serviços externos.

Diferenças em relação ao KMS real:
- A master key fica legível no disco (permissão 0600, mas ainda acessível
  ao usuário do processo).
- Não há log de auditoria de uso da chave.
- Não há rotação automática nem políticas de acesso.
- O ``key_id`` muda se o arquivo for deletado e recriado.

Segurança (HIGH 1 — F5-S5):
- ``master_key_path`` é **obrigatório** — sem default ``/tmp/...`` que seria
  world-writable e acessível por qualquer processo no container.
- ``_load_or_create`` usa ``O_CREAT | O_WRONLY | O_EXCL`` para criação atômica
  (evita race condition onde dois processos criam chaves diferentes).
- Em ``FileExistsError``: lê o arquivo existente (não sobrescreve — o outro
  processo criou primeiro).
- Permissão 0600 garantida via ``stat.S_IRUSR | stat.S_IWUSR``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .kms import KmsBackend

logger = logging.getLogger(__name__)


class LocalKmsStubBackend(KmsBackend):
    """Stub de KMS para dev/test. Master key em arquivo no disco.

    Na primeira chamada, gera uma chave Fernet aleatória e salva em
    ``master_key_path`` com permissão 0600 de forma atômica (O_EXCL).
    Nas chamadas seguintes, carrega do mesmo arquivo — garantindo que
    instâncias diferentes do processo usem a mesma master key enquanto
    o arquivo existir.

    Args:
        master_key_path: Caminho absoluto do arquivo de master key.
            **Obrigatório** — sem default para forçar configuração
            explícita e evitar uso inadvertido de /tmp (world-writable).
            Em dev/test, usar ``settings.KMS_LOCAL_STUB_MASTER_KEY_PATH``
            (default: ``/var/lib/centralops/kms-master.key``).
    """

    def __init__(self, master_key_path: str) -> None:
        self._path = Path(master_key_path)
        self._master_key: bytes = self._load_or_create()
        # Instância Fernet lazy — recriada somente se _master_key mudar.
        self._fernet = Fernet(self._master_key)

    # ── API pública ───────────────────────────────────────────────────

    def wrap(self, plaintext_dek: bytes) -> bytes:
        """Cifra a DEK com a master key (Fernet).

        Args:
            plaintext_dek: DEK em plaintext (ex: saída de Fernet.generate_key()).

        Returns:
            DEK cifrada (bytes).
        """
        return self._fernet.encrypt(plaintext_dek)

    def unwrap(self, wrapped_dek: bytes) -> bytes:
        """Decifra um wrapped DEK.

        Args:
            wrapped_dek: bytes produzidos por :meth:`wrap`.

        Returns:
            DEK em plaintext.

        Raises:
            ValueError: se não conseguir decifrar (chave errada ou dados corrompidos).
        """
        try:
            return self._fernet.decrypt(wrapped_dek)
        except InvalidToken as exc:
            raise ValueError(
                "Não foi possível desempacotar o wrapped DEK: token inválido ou "
                "master key diferente da que cifrou originalmente. "
                f"Arquivo de master key: {self._path}"
            ) from exc

    def key_id(self) -> str:
        """Identificador da master key atual.

        Formato: ``local-kms-stub:{primeiros 8 hex do sha256 da master key}``.
        Muda apenas se o arquivo de master key for substituído.
        """
        digest = hashlib.sha256(self._master_key).hexdigest()
        return f"local-kms-stub:{digest[:8]}"

    # ── Helpers privados ──────────────────────────────────────────────

    def _load_or_create(self) -> bytes:
        """Carrega master key do arquivo ou gera e persiste uma nova.

        Estratégia de criação atômica (HIGH 1 — F5-S5):
        - Usa ``O_CREAT | O_WRONLY | O_EXCL`` para garantir que apenas um
          processo cria o arquivo em condição de corrida simultânea.
        - Em ``FileExistsError``: outro processo criou o arquivo entre o
          nosso check e o open — lemos o arquivo existente.
        - Nunca sobrescreve um arquivo pré-existente (evita destruição de
          chave em uso por outros processos).
        """
        if self._path.exists():
            raw = self._path.read_bytes().strip()
            if raw:
                logger.debug(
                    "LocalKmsStubBackend: carregou master key de %s", self._path
                )
                return raw

        # Gera nova chave Fernet (32 bytes urlsafe-b64 = 44 chars).
        key = Fernet.generate_key()
        self._path.parent.mkdir(parents=True, exist_ok=True)

        # O_EXCL garante criação exclusiva (atomic create + fail-if-exists).
        # Se dois processos chegarem aqui simultaneamente, apenas um vence;
        # o outro recebe FileExistsError e lê a chave gerada pelo vencedor.
        try:
            fd = os.open(
                str(self._path),
                os.O_CREAT | os.O_WRONLY | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,  # 0o600
            )
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        except FileExistsError:
            # Outra instância criou o arquivo entre nosso check e o open.
            # Lemos a chave criada por ela — consistência garantida.
            logger.debug(
                "LocalKmsStubBackend: race condition detectada — usando chave "
                "criada concorrentemente em %s",
                self._path,
            )
            raw = self._path.read_bytes().strip()
            if not raw:
                raise RuntimeError(
                    f"Arquivo de master key em {self._path} existe mas está vazio "
                    "(race condition sem resolução). Remova o arquivo e tente novamente."
                )
            return raw

        logger.warning(
            "LocalKmsStubBackend: nova master key gerada em %s — "
            "NÃO USE EM PRODUÇÃO",
            self._path,
        )
        return key
