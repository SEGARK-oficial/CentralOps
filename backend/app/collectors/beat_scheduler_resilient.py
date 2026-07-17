"""Scheduler RedBeat endurecido para produção.

Duas melhorias sobre ``redbeat.RedBeatScheduler``, ambas críticas para a
resiliência da coleta agendada:

1. **Recuperação in-process do lock (leadership-safe).** O RedBeat foi desenhado
   para CRASHAR em ``LockNotOwnedError`` e depender do supervisor reiniciar o
   processo. No nosso deploy cada restart re-roda a migração de schema e para a
   coleta por segundos-a-minutos; sob uma perda TRANSITÓRIA do lock (restart/
   failover do Redis, blip de rede, evicção sob ``volatile-lru``, stall do
   processo > ``lock_timeout``) isso vira crash-loop e "vários collectors ficam
   sem coletar". Esta subclasse intercepta o ``LockNotOwnedError`` e tenta
   RE-ADQUIRIR o MESMO lock via ``SET NX`` (a mesma primitiva do RedBeat).

   Isto é **seguro contra double-scheduling**: a qualquer instante só um token
   pode segurar ``{prefix}lock``. Se outra réplica já assumiu a liderança, o
   ``acquire(blocking_timeout=...)`` falha rápido (o ``sleep`` do lock =
   ``max_interval`` >> ``blocking_timeout``) e nós PROPAGAMOS o erro — o processo
   cai e reinicia como hot-standby, preservando a exclusão mútua. Ou seja: é
   estritamente MAIS resiliente que o crash puro, com a MESMA garantia.

2. **Heartbeat por tick.** Grava o epoch em ``BEAT_HEARTBEAT_FILE`` no início de
   cada tick. Um beat vivo-mas-travado (I/O Redis pendurado dentro de
   ``super().tick()``) deixa o mtime envelhecer → o healthcheck do Docker / o
   ``livenessProbe`` do Kubernetes o reiniciam. Espelha o padrão já usado no
   kafka-dispatcher (``kafka_transport._touch_health``). Um crash puro se
   auto-cura via ``restart``/``restartPolicy``; o heartbeat cobre o buraco do
   beat TRAVADO, que hoje é invisível.

O caminho de import é resolvido só quando o Beat instancia o scheduler (Celery
faz ``symbol_by_name`` sobre a string ``beat_scheduler``), então importar
``celery_app`` NÃO importa este módulo — sem acoplamento de import no caminho da
API/worker.
"""

from __future__ import annotations

import logging
import os
import time

from redis.exceptions import LockError, LockNotOwnedError
from redbeat import RedBeatScheduler

logger = logging.getLogger(__name__)

#: Arquivo de heartbeat consumido pelo healthcheck (compose) / livenessProbe
#: (k8s). Mantido em sincronia com ``compose/docker-compose.yml`` e o Helm.
BEAT_HEARTBEAT_FILE = os.environ.get("BEAT_HEARTBEAT_FILE", "/tmp/beat-heartbeat")

#: Janela de bloqueio na re-aquisição do lock. Curta de propósito: se o lock
#: está livre (evictado/expirado) adquirimos de imediato; se está com OUTRA
#: réplica, falhamos rápido e propagamos (crash+restart → hot-standby).
_REACQUIRE_BLOCKING_TIMEOUT = float(
    os.environ.get("BEAT_LOCK_REACQUIRE_TIMEOUT", "5")
)


class ResilientRedBeatScheduler(RedBeatScheduler):
    """``RedBeatScheduler`` que sobrevive à perda transitória do lock e emite
    heartbeat por tick. Ver docstring do módulo."""

    def tick(self, *args, **kwargs):  # noqa: D401 — mesma assinatura da lib
        self._write_heartbeat()
        try:
            return super().tick(*args, **kwargs)
        except LockNotOwnedError:
            if self._reacquire_lock():
                logger.warning(
                    "beat: lock RedBeat perdido e RE-ADQUIRIDO in-process (sem "
                    "crash/re-migração) — a coleta continua sem janela morta"
                )
                # Não confia no intervalo antigo; agenda um tick logo em seguida
                # para reavaliar o schedule com o lock recém-renovado.
                return min(self.max_interval, 5.0)
            logger.error(
                "beat: re-aquisição do lock FALHOU (outro detentor ativo ou Redis "
                "indisponível) — propagando p/ crash+restart (exclusão preservada)"
            )
            raise

    # ── internos ─────────────────────────────────────────────────────────
    def _reacquire_lock(self) -> bool:
        """Re-adquire o mesmo lock via ``SET NX``. ``True`` se voltamos a ser o
        líder; ``False`` se outra réplica o detém ou o Redis está indisponível."""
        lock = getattr(self, "lock", None)
        if lock is None:
            # lock_key setado mas lock None ⇒ beat_init não conseguiu adquirir;
            # nada a re-adquirir aqui (o crash+restart resolve no boot).
            return False
        try:
            # O extend falho deixou ``self.local.token`` setado; zeramos para não
            # esbarrar em guardas de "já adquirido" de algumas versões do redis-py.
            try:
                lock.local.token = None
            except Exception:  # pragma: no cover — defensivo
                pass
            # blocking_timeout curto + ``sleep`` do lock (== max_interval) grande
            # ⇒ se o lock está com outra réplica, retorna False quase imediato.
            return bool(
                lock.acquire(blocking=True, blocking_timeout=_REACQUIRE_BLOCKING_TIMEOUT)
            )
        except LockError:
            return False

    def _write_heartbeat(self) -> None:
        """Escreve o epoch atual de forma atômica (best-effort). NUNCA levanta —
        o heartbeat jamais pode derrubar o tick."""
        try:
            tmp = f"{BEAT_HEARTBEAT_FILE}.tmp"
            with open(tmp, "w") as fh:
                fh.write(str(int(time.time())))
            os.replace(tmp, BEAT_HEARTBEAT_FILE)
        except Exception:  # pragma: no cover — /tmp pode ser read-only
            logger.debug("beat: falha ao gravar heartbeat", exc_info=True)
