# Changelog

## [2.0.0](https://github.com/SEGARK-oficial/CentralOps/compare/v1.1.0...v2.0.0) (2026-07-17)


### ⚠ BREAKING CHANGES

* the Wazuh-only alerts surface was removed (route /alerts, alerts API endpoints, the /dashboard/summary v1 Accept path, and the MCP list_integration_alerts tool). Use federated search (Investigations) + Detections instead. Sophos/Wazuh alert *ingestion* is unchanged.

### Features

* add Wazuh detection mapping validation and fix missing seed definition ([567ff40](https://github.com/SEGARK-oficial/CentralOps/commit/567ff408bff832a9bed9f0cd672780b7214b4a9d))
* implement node aggregation with +N expand functionality and update FlowCanvas layout constants ([736f1f1](https://github.com/SEGARK-oficial/CentralOps/commit/736f1f10b0adf81db123a5130765af2de95cf4dc))
* implement robust CSV export for federated search results and add i18n support for federated search labels ([48699e1](https://github.com/SEGARK-oficial/CentralOps/commit/48699e15d640cee0d1c846c7831779f79798c2ec))
* replace raw condition operator labels with localized, human-friendly definitions in RouteConditionEditor ([a6538e7](https://github.com/SEGARK-oficial/CentralOps/commit/a6538e7799d808297e4e7dd93c4915a0ad04bdc0))


### Bug Fixes

* dispose DB pool on soft-timeout and initialize local variables early to prevent UnboundLocalError during pipeline failures ([4dc3b33](https://github.com/SEGARK-oficial/CentralOps/commit/4dc3b33aae3208fca5e107079bb360cede50159e))
* let global admins scope live capture to a tenant via org selector ([baaadae](https://github.com/SEGARK-oficial/CentralOps/commit/baaadae2498031b71aabe6ce58925c36df859e74))
* prevent normalization engine from applying pre_cast and value_map to default values to avoid errors with non-string defaults ([6f7c675](https://github.com/SEGARK-oficial/CentralOps/commit/6f7c6750c70b4ed4ae5214c28517de35d4663842))
* prevent session cookie boot crashes by handling empty environment variables and fixing OCSF resource path anchoring. ([329b37a](https://github.com/SEGARK-oficial/CentralOps/commit/329b37a1dbd3edb169da41b96aebabc15b881c23))
* resolve RedBeat starvation and crash-loops by increasing lock timeout, setting max loop interval, and enforcing idempotent scheduler registration. ([29038a8](https://github.com/SEGARK-oficial/CentralOps/commit/29038a8faed3747fb97577303fc78604e2beca6b))
* sanitize service account shim IDs to prevent foreign key violations in audit and mapping logs ([cf43df8](https://github.com/SEGARK-oficial/CentralOps/commit/cf43df811db796d239dec3914ef9e08d0918fb71))


### Performance Improvements

* batch ingestion metering with InVolumeAccumulator to reduce Redis I/O latency in pipeline ([13e4cbc](https://github.com/SEGARK-oficial/CentralOps/commit/13e4cbce09b84b716610568b31c72e1ffe402518))


### Code Refactoring

* document alerts surface removal as breaking ([f424f36](https://github.com/SEGARK-oficial/CentralOps/commit/f424f368bf1560299f8e9fb455d54b14424ff4b4))

## [1.1.0](https://github.com/SEGARK-oficial/CentralOps/compare/v1.0.1...v1.1.0) (2026-07-16)


### Features

* implement license gating for tenant sync and reduce license keyring log noise ([a9bbccb](https://github.com/SEGARK-oficial/CentralOps/commit/a9bbccbed91c8447890820df4afd66752607acb6))

## [1.0.1](https://github.com/SEGARK-oficial/CentralOps/compare/v1.0.0...v1.0.1) (2026-07-10)


### Bug Fixes

* **ci:** runners GitHub-hosted; restaura () removidos na limpeza; tira detalhes de repo privado dos docs públicos ([a2f3bb1](https://github.com/SEGARK-oficial/CentralOps/commit/a2f3bb11b4c775ca456e11ded8abd6c7978f6006))
* **frontend:** apk upgrade patches c-ares CVE-2026-33630 (Trivy HIGH gate) ([ff96b3c](https://github.com/SEGARK-oficial/CentralOps/commit/ff96b3cf41c5c39087021b207f0add3e7846ed7d))

## Changelog

All notable changes to CentralOps are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/) and this project
adheres to [Semantic Versioning](https://semver.org/). Releases are managed automatically
by [release-please](https://github.com/googleapis/release-please); entries below are
generated from Conventional Commits.

<!-- Public release history starts at v1.0.0. -->
