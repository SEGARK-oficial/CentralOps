"""Search-in-place no lake S3 (LakeProvider).

Lê o que os sinks escreveram, via filtro ESTRUTURADO (não SQL — sem
injeção), org-scoped por partição, LIMIT estrito (anti-OOM), seam S3 mockável.
Cobre: ndjson + parquet, filtro, LIMIT, isolamento por org=partição, janela,
statement inválido, capability, e o round-trip de config_json no create.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import gzip
import json

import pytest

from backend.app.collectors import registry
from backend.app.db import models
from backend.app.providers.errors import ProviderQueryError
from backend.app.providers.lake.provider import LakeProvider


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    """boto3 S3 client fake — list_objects_v2 (por prefixo) + get_object."""

    def __init__(self, store):
        self.store = store  # {key: bytes}
        self.listed_prefixes = []

    def list_objects_v2(self, Bucket, Prefix=None, MaxKeys=None, ContinuationToken=None):
        if Prefix is not None:
            self.listed_prefixes.append(Prefix)
        keys = [k for k in self.store if Prefix is None or k.startswith(Prefix)]
        return {"Contents": [{"Key": k, "Size": len(self.store[k])} for k in keys], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        return {"Body": _Body(self.store[Key])}


def _lake(monkeypatch, store, *, layout="s3_ndjson", org_id=7, **cfg):
    integ = models.Integration(
        name="lake", organization_id=org_id, kind="tenant", platform="lake",
        base_url="my-bucket", region="us-east-1", client_id="AKIA", tenant_id="123456789012",
        config_json=json.dumps({"layout": layout, "prefix": "centralops", "source": "centralops", **cfg}),
    )
    p = LakeProvider(integ)
    fake = _FakeS3(store)
    monkeypatch.setattr(p, "_s3_client", lambda conn: fake)
    return p, fake


def _ndjson(rows, gz=False):
    body = ("\n".join(json.dumps(r) for r in rows)).encode("utf-8")
    return gzip.compress(body, mtime=0) if gz else body


_WINDOW = ("2026-06-22T00:00:00Z", "2026-06-22T23:59:59Z")


# ── s3_ndjson layout ───────────────────────────────────────────────────


def test_lake_ndjson_filter_and_orgscope(monkeypatch):
    store = {
        "centralops/org=7/2026/06/22/a.ndjson": _ndjson([{"host": "A"}, {"host": "B"}, {"host": "A"}]),
        # outra org — NÃO deve ser lida (org=99)
        "centralops/org=99/2026/06/22/x.ndjson": _ndjson([{"host": "A"}]),
    }
    p, fake = _lake(monkeypatch, store, org_id=7)
    res = p.run_query(json.dumps({"filters": [{"field": "host", "op": "eq", "value": "A"}]}), *_WINDOW)
    assert res.total == 2  # só os 2 host=A da org 7 (a org 99 nunca foi listada)
    assert all(p_ == "centralops/org=7/2026/06/22/" for p_ in fake.listed_prefixes)


def test_lake_ndjson_gzip(monkeypatch):
    store = {"centralops/org=7/2026/06/22/a.ndjson.gz": _ndjson([{"host": "A"}], gz=True)}
    p, _ = _lake(monkeypatch, store)
    res = p.run_query("{}", *_WINDOW)  # sem filtro → tudo
    assert res.total == 1


def test_lake_limit_caps_rows(monkeypatch):
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, "QUERY_LAKE_MAX_ROWS", 3)
    store = {"centralops/org=7/2026/06/22/a.ndjson": _ndjson([{"i": i} for i in range(100)])}
    p, _ = _lake(monkeypatch, store)
    res = p.run_query("{}", *_WINDOW)
    assert res.total == 3  # teto duro respeitado (anti-OOM)


def test_lake_statement_limit_honored(monkeypatch):
    store = {"centralops/org=7/2026/06/22/a.ndjson": _ndjson([{"i": i} for i in range(50)])}
    p, _ = _lake(monkeypatch, store)
    res = p.run_query(json.dumps({"limit": 5}), *_WINDOW)
    assert res.total == 5


# ── security_lake_parquet layout ───────────────────────────────────────


def test_lake_parquet_layout(monkeypatch):
    key = "ext/centralops/region=us-east-1/accountId=123456789012/eventDay=20260622/h.parquet"
    store = {key: b"PARQUET-BYTES"}
    p, fake = _lake(monkeypatch, store, layout="security_lake_parquet")
    # patch o seam de parquet (sem pyarrow real)
    monkeypatch.setattr(p, "_read_parquet", lambda raw: [{"sev": 9}, {"sev": 1}])
    res = p.run_query(json.dumps({"filters": [{"field": "sev", "op": "gte", "value": 5}]}), *_WINDOW)
    assert res.total == 1
    assert fake.listed_prefixes[0].startswith("ext/centralops/region=us-east-1/accountId=123456789012/eventDay=20260622/")


# ── anti-OOM (revisão adversarial) ─────────────────────────────────────


def test_lake_skips_oversized_object(monkeypatch):
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, "QUERY_LAKE_MAX_OBJECT_BYTES", 50)  # teto minúsculo
    store = {"centralops/org=7/2026/06/22/big.ndjson": _ndjson([{"x": i} for i in range(100)])}
    p, _ = _lake(monkeypatch, store)
    res = p.run_query("{}", *_WINDOW)
    assert res.total == 0  # objeto gigante PULADO antes de baixar (não OOM)


def test_lake_gzip_bomb_skipped(monkeypatch):
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, "QUERY_LAKE_MAX_OBJECT_BYTES", 1000)
    # gzip pequeno (comprimido < teto) que descomprime ACIMA do teto
    bomb = gzip.compress(b'{"a":1}\n' * 5000, mtime=0)  # ~40KB descomprimido
    store = {"centralops/org=7/2026/06/22/bomb.ndjson.gz": bomb}
    p, _ = _lake(monkeypatch, store)
    res = p.run_query("{}", *_WINDOW)
    assert res.total == 0  # bomba detectada na descompressão → objeto pulado, sem crash


def test_lake_listing_capped_per_prefix(monkeypatch):
    """Partição patológica (sink em runaway) NÃO materializa todas as keys: a
    listagem é gerador e para no teto QUERY_LAKE_MAX_KEYS_PER_PREFIX."""
    from backend.app.core.config import settings
    monkeypatch.setattr(settings, "QUERY_LAKE_MAX_KEYS_PER_PREFIX", 2)
    store = {
        f"centralops/org=7/2026/06/22/obj{i}.ndjson": _ndjson([{"i": i}])
        for i in range(10)
    }
    p, _ = _lake(monkeypatch, store)
    res = p.run_query("{}", *_WINDOW)  # sem filtro/limit → varreria os 10
    assert res.total == 2  # só os 2 primeiros objetos foram listados/lidos (teto)


# ── erros + capability ─────────────────────────────────────────────────


def test_lake_bad_statement_raises(monkeypatch):
    p, _ = _lake(monkeypatch, {})
    with pytest.raises(ProviderQueryError):
        p.run_query("not json", *_WINDOW)


def test_lake_bad_window_raises(monkeypatch):
    p, _ = _lake(monkeypatch, {})
    with pytest.raises(ProviderQueryError):
        p.run_query("{}", "lixo", "lixo")


def test_lake_capability_declared():
    reg = registry.get_platform("lake")
    assert reg is not None and reg.provider_factory is not None
    assert {qc.dialect for qc in reg.query_capabilities} == {"lake_filter"}
    assert "query:lake_filter" in reg.capabilities


def test_lake_config_json_roundtrip_in_assign_credentials():
    """auth_fields não-coluna/não-secret (layout/prefix/source) caem
    no config_json no create (antes eram descartados)."""
    from types import SimpleNamespace

    from backend.app.routers.integrations import _assign_credentials

    plat_reg = registry.get_platform("lake")
    integ = models.Integration(name="l", organization_id=1, kind="tenant", platform="lake")
    data = SimpleNamespace(
        base_url="b", region="us-east-1", client_id="AKIA", layout="s3_ndjson",
        prefix="myprefix", source="mysrc", tenant_id="acct", model_extra={},
    )
    # secret é escrito no store (precisa do crypto real, já disponível em test)
    data.secret_access_key = "shh"
    _assign_credentials(integ, data, plat_reg, "tenant")
    cfg = json.loads(integ.config_json)
    assert cfg["layout"] == "s3_ndjson" and cfg["prefix"] == "myprefix" and cfg["source"] == "mysrc"
    # bucket/region/access-key foram p/ colunas; secret NÃO está no config_json
    assert integ.base_url == "b" and integ.client_id == "AKIA"
    assert "secret_access_key" not in cfg
