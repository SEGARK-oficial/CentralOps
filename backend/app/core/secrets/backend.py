"""Contrato base para backends de secrets."""

from __future__ import annotations

import abc


class SecretsBackend(abc.ABC):
    """Backend que cifra e decifra secrets armazenados pelo app.

    Implementações devem ser idempotentes em ``encrypt``: chamar duas
    vezes sobre o mesmo plaintext produz tokens diferentes (Fernet usa
    nonce), mas ambos descriptografam para o mesmo valor.

    O contrato é deliberadamente síncrono — todas as chamadas hoje são
    feitas dentro de handlers FastAPI ou em paths quentes do collector
    onde a latência adicional de I/O assíncrono não compensa. Backends
    remotos (Vault, AWS Secrets Manager) devem usar cliente síncrono
    com cache local para amortizar.
    """

    @abc.abstractmethod
    def encrypt(self, plaintext: str) -> str:
        """Devolve o ciphertext serializado como string opaca.

        Convenção: o backend é responsável por anexar qualquer prefixo
        que precise para reconhecer seus próprios tokens depois (ex:
        ``enc::`` no LocalFernet). ``decrypt`` deve aceitar tanto o
        formato com prefixo quanto plaintext puro (compatibilidade com
        valores legados em DB).
        """

    @abc.abstractmethod
    def decrypt(self, ciphertext: str) -> str:
        """Devolve o plaintext correspondente.

        Se ``ciphertext`` parecer plaintext legado (sem prefixo nem
        formato esperado), devolve como veio — preserva compatibilidade
        com valores cadastrados antes da introdução da cifragem.

        Levanta ``ValueError`` se o token tiver formato esperado mas
        não decifrar com a chave atual (mascarar isso silenciosamente
        corromperia integrações em produção).
        """
