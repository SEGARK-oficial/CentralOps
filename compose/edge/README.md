# Edge-collectors (ingestão push)

Configs prontas de coletores de borda que recebem fontes **push** e encaminham ao
endpoint de ingestão do CentralOps (`POST /api/ingest/<stream>`), autenticado por
token de ingestão.

| Arquivo | Fonte | Ferramenta | Stream |
|---------|-------|-----------|--------|
| `vector-fortigate.toml` | Fortinet FortiGate (syslog) | Vector | `traffic` |
| `fluent-bit-windows-wec.conf` | Windows Event Log (WEC/WEF) | Fluent Bit | `security` |
| `docker-compose.edge.yml` | sobe o Vector do FortiGate | Docker Compose | — |

Passo a passo completo (criar integração, emitir token, validar) na
[documentação](https://segark-oficial.github.io/CentralOps).

## Rápido (FortiGate)

```bash
CENTRALOPS_INGEST_URL="https://<host>/api/ingest/traffic" \
CENTRALOPS_INGEST_TOKEN="coi_<id>_xxxxx" \
docker compose -f compose/edge/docker-compose.edge.yml up -d
```

O token de ingestão é emitido na tela da integração (painel **Ingestão push**),
que também gera um snippet já preenchido. O Windows Event Log roda nativamente no
servidor coletor WEC (Fluent Bit), não em container.
