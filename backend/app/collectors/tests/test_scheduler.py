"""Testes unitários do módulo collectors.scheduler (Fase 1.3).

Cobre:
- register_integration_in_beat é idempotente (upsert via .save()).
- deregister_integration_from_beat é idempotente (KeyError = no-op).
- Falha silenciosa quando Redis/DB indisponível (sem raise).

Estratégia de patch:
- ``database`` e ``models`` e ``iter_for_platform`` são importados no topo
  do módulo scheduler, então são patcháveis via ``backend.app.collectors.scheduler.*``.
- ``RedBeatSchedulerEntry`` e ``celery_app`` são importados lazy dentro das
  funções; patchamos via seus módulos de origem.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────

class _FakeIntegration:
    def __init__(self, id: int, platform: str, kind: str = "tenant"):
        self.id = id
        self.platform = platform
        self.is_active = True
        self.kind = kind


class _FakeRegistration:
    def __init__(
        self,
        platform: str,
        stream: str,
        task_name: str = "collectors.collect_vendor_logs_bulk",
        queue: str = "collect.bulk",
        schedule: timedelta = timedelta(minutes=5),
    ):
        self.platform = platform
        self.stream = stream
        self.task_name = task_name
        self.queue = queue
        self.schedule = schedule

    @property
    def beat_key(self) -> str:
        return f"{self.platform}-{self.stream}"


_MOD = "backend.app.collectors.scheduler"
_CELERY_MOD = "backend.app.collectors.celery_app"


def _mock_session_returning(integration):
    """SessionLocal context manager que retorna a integração configurada."""
    mock_db = MagicMock()
    mock_filter = MagicMock()
    mock_filter.first.return_value = integration
    mock_db.query.return_value.filter.return_value = mock_filter
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_db)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ── register_integration_in_beat ──────────────────────────────────────

class TestRegisterIntegrationInBeat:

    def _run(self, integration, regs, mock_entry_cls):
        with (
            patch(f"{_MOD}.database") as mock_db_module,
            patch(f"{_MOD}.models"),
            patch(f"{_MOD}.iter_for_platform", return_value=iter(regs)),
            patch("redbeat.RedBeatSchedulerEntry", mock_entry_cls),
            patch(f"{_CELERY_MOD}.celery_app"),
        ):
            mock_db_module.SessionLocal.return_value = _mock_session_returning(integration)
            from ..scheduler import _register_integration_in_beat_unsafe
            _register_integration_in_beat_unsafe(integration.id if integration else 99)

    def test_calls_save_for_each_stream(self):
        """Para 2 streams, .save() é chamado 2x."""
        integration = _FakeIntegration(id=42, platform="sophos")
        regs = [
            _FakeRegistration("sophos", "alerts", queue="collect.priority"),
            _FakeRegistration("sophos", "detections", queue="collect.bulk"),
        ]
        mock_entry_cls = MagicMock()
        mock_entry_instance = MagicMock()
        mock_entry_cls.return_value = mock_entry_instance

        self._run(integration, regs, mock_entry_cls)

        assert mock_entry_instance.save.call_count == 2

    def test_idempotent_second_call_also_saves(self):
        """Segunda chamada com mesmo integration_id faz upsert (.save() novamente)."""
        integration = _FakeIntegration(id=7, platform="sophos")
        mock_entry_cls = MagicMock()
        mock_entry_instance = MagicMock()
        mock_entry_cls.return_value = mock_entry_instance

        regs = [_FakeRegistration("sophos", "alerts")]
        self._run(integration, regs, mock_entry_cls)
        assert mock_entry_instance.save.call_count == 1

        mock_entry_instance.save.reset_mock()
        regs2 = [_FakeRegistration("sophos", "alerts")]
        self._run(integration, regs2, mock_entry_cls)
        assert mock_entry_instance.save.call_count == 1  # segunda chamada também salva

    def test_skips_when_integration_not_found(self):
        """Integração inexistente ou inativa não cria entries."""
        mock_entry_cls = MagicMock()
        self._run(None, [], mock_entry_cls)
        mock_entry_cls.assert_not_called()

    def test_public_wrapper_does_not_raise_on_error(self):
        """register_integration_in_beat não propaga exceções (fire-and-forget)."""
        with patch(
            f"{_MOD}._register_integration_in_beat_unsafe",
            side_effect=ConnectionError("Redis down"),
        ):
            from ..scheduler import register_integration_in_beat
            register_integration_in_beat(1)  # não deve levantar


# ── deregister_integration_from_beat ─────────────────────────────────

class TestDeregisterIntegrationFromBeat:

    def _run(self, integration, regs, mock_entry_cls):
        mock_celery_app = MagicMock()
        mock_celery_app.conf.redbeat_key_prefix = "redbeat::"

        with (
            patch(f"{_MOD}.database") as mock_db_module,
            patch(f"{_MOD}.models"),
            patch(f"{_MOD}.iter_for_platform", return_value=iter(regs)),
            patch("redbeat.RedBeatSchedulerEntry", mock_entry_cls),
            patch(f"{_CELERY_MOD}.celery_app", mock_celery_app),
        ):
            mock_db_module.SessionLocal.return_value = _mock_session_returning(integration)
            from ..scheduler import _deregister_integration_from_beat_unsafe
            _deregister_integration_from_beat_unsafe(integration.id if integration else 99)

    def test_calls_delete_for_each_stream(self):
        """Para 2 streams, .delete() é chamado 2x."""
        integration = _FakeIntegration(id=42, platform="sophos")
        regs = [
            _FakeRegistration("sophos", "alerts"),
            _FakeRegistration("sophos", "detections"),
        ]
        mock_entry_instance = MagicMock()
        mock_entry_cls = MagicMock()
        mock_entry_cls.from_key.return_value = mock_entry_instance

        self._run(integration, regs, mock_entry_cls)

        assert mock_entry_instance.delete.call_count == 2

    def test_idempotent_when_entry_not_found(self):
        """KeyError em from_key é tratado como no-op (entry já removida)."""
        integration = _FakeIntegration(id=42, platform="sophos")
        regs = [_FakeRegistration("sophos", "alerts")]

        mock_entry_cls = MagicMock()
        mock_entry_cls.from_key.side_effect = KeyError("not found")

        self._run(integration, regs, mock_entry_cls)  # não deve levantar

    def test_public_wrapper_does_not_raise_on_error(self):
        """deregister_integration_from_beat não propaga exceções."""
        with patch(
            f"{_MOD}._deregister_integration_from_beat_unsafe",
            side_effect=ConnectionError("Redis down"),
        ):
            from ..scheduler import deregister_integration_from_beat
            deregister_integration_from_beat(1)  # não deve levantar
