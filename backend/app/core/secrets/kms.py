"""Contrato base para backends de Key Management Service (KMS).

Define a interface de wrap/unwrap de DEKs (Data Encryption Keys).
Implementações concretas:
- :class:`LocalKmsStubBackend` — dev/test, master key em arquivo local.
- ``AwsKmsBackend`` — TODO Fase 4+ (boto3, KMS::Encrypt/Decrypt).
- ``HashicorpVaultBackend`` — TODO Fase 4+ (Transit Engine).

O backend é deliberadamente síncrono para manter coerência com a
interface :class:`~backend.app.core.secrets.backend.SecretsBackend`.
Implementações remotas devem usar cache local de DEK para amortizar
latência de round-trip.
"""

from __future__ import annotations

import abc


class KmsBackend(abc.ABC):
    """Backend de Key Management Service — wrap/unwrap de DEKs.

    O fluxo de envelope encryption funciona assim:
    1. Gera-se uma DEK (Data Encryption Key) Fernet aleatória.
    2. A DEK é cifrada com a master key do KMS → "wrapped DEK".
    3. O wrapped DEK é armazenado junto ao ciphertext.
    4. Para cifrar: KMS unwrap → DEK → Fernet.encrypt(plaintext).
    5. Para decifrar: KMS unwrap → DEK → Fernet.decrypt(ciphertext).

    Vantagem central: a master key **nunca** toca o processo da aplicação
    em implementações reais (AWS KMS, Vault). Aqui o :class:`LocalKmsStubBackend`
    simula esse comportamento via arquivo local — aceitável em dev/test,
    **proibido em produção**.
    """

    @abc.abstractmethod
    def wrap(self, plaintext_dek: bytes) -> bytes:
        """Cifra uma DEK com a master key.

        Args:
            plaintext_dek: bytes da DEK em claro (ex: resultado de
                ``Fernet.generate_key()``).

        Returns:
            Wrapped DEK (bytes opacos que só este KMS consegue desempacotar).
        """

    @abc.abstractmethod
    def unwrap(self, wrapped_dek: bytes) -> bytes:
        """Decifra um wrapped DEK.

        Args:
            wrapped_dek: bytes produzidos por :meth:`wrap`.

        Returns:
            DEK em plaintext.

        Raises:
            ValueError: se o wrapped DEK não puder ser decifrado com a
                master key atual (ex: rotação de chave sem re-encrypt).
        """

    @abc.abstractmethod
    def key_id(self) -> str:
        """Identificador da master key usada neste backend.

        Usado para audit trail e detecção de rotação de chave.
        Formato livre por implementação; deve ser estável enquanto a
        master key não rotacionar.
        """
