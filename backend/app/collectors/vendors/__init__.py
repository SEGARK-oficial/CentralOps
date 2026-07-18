"""Collectors assíncronos por vendor.

Cada módulo se auto-registra no ``collectors.registry`` via um ``_register()``
executado no import. Para adicionar um vendor novo:

1. Crie ``vendors/<vendor>.py`` com a classe ``<Vendor>Collector(BaseCollector)``.
2. Implemente o refresher async em ``auth/refreshers.py`` (ou inline).
3. Chame ``register(CollectorRegistration(...))`` no final do módulo.
4. Importe o módulo aqui (``from . import <vendor>``).

Zero mudanças em ``pipeline``, ``beat_schedule`` ou roteamento Celery.
"""

from __future__ import annotations

# Ordem não importa — cada módulo é idempotente.
from . import wazuh  # noqa: F401           — wazuh (catálogo + provider)
from . import sophos  # noqa: F401          — sophos/alerts
from . import sophos_cases  # noqa: F401    — sophos/cases
from . import defender  # noqa: F401        — microsoft_defender/incidents
from . import defender_alerts  # noqa: F401 — microsoft_defender/alerts (v2)
from . import ninjaone  # noqa: F401        — ninjaone/activities
from . import sophos_detections  # noqa: F401 — sophos/detections (XDR async runs)
from . import wazuh_detections  # noqa: F401  — wazuh/detections (pull do Indexer)
from . import crowdstrike  # noqa: F401      — crowdstrike/detections (Alerts API v2)
from . import entra_id  # noqa: F401         — entra_id/signins+audit (Graph)
from . import okta  # noqa: F401             — okta/system_log (SSWS)
from . import aws_cloudtrail  # noqa: F401    — aws_cloudtrail/events (S3 poll)
from . import aws_cloudwatch  # noqa: F401    — aws_cloudwatch/events (Logs FilterLogEvents)
from . import veeam  # noqa: F401             — veeam/sessions (VBR REST, OAuth2 password)
from . import lake  # noqa: F401             — lake (search-in-place no S3)
from . import push_ingest  # noqa: F401      — fortigate/windows_event_log (PUSH/ingest)
