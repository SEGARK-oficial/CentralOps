# Changelog

## [2.3.0](https://github.com/SEGARK-oficial/CentralOps/compare/v2.2.0...v2.3.0) (2026-07-24)


### Features

* **api:** expor filtro de coleta e atraso real na API ([d22da15](https://github.com/SEGARK-oficial/CentralOps/commit/d22da1567a043e726ad6fa931c271ddb7beaf404))
* **brand:** dar identidade própria ao CentralOps e corrigir o que os docs quebravam ([a3d84ae](https://github.com/SEGARK-oficial/CentralOps/commit/a3d84ae1727057a35e5833eeb66d28ef9a86a604))
* **capture:** exportar eventos capturados em CSV/NDJSON com streaming ([bcdbb44](https://github.com/SEGARK-oficial/CentralOps/commit/bcdbb44629d6d60145955f5ce8bdc0c37c4dfdc4))
* **capture:** rota estruturada no desfecho e antes/depois no inspetor ([6ba9b29](https://github.com/SEGARK-oficial/CentralOps/commit/6ba9b29b824b10522aaf1b6a2d1f2d4f00fbd142))
* **collect:** filtro de coleta declarado pelo plugin, por integração ([a7e6e10](https://github.com/SEGARK-oficial/CentralOps/commit/a7e6e1010e33e6a9b8a45aa49d8314f366ed7d24))
* **dedupe:** TTL em segundos (piso 4h) e fórmula de capacidade do Redis ([b829856](https://github.com/SEGARK-oficial/CentralOps/commit/b829856e4599db40b193149a9092ed822301be6b))
* **destinations:** campo de preço por GB (FinOps) na UI do destino ([acc3bbe](https://github.com/SEGARK-oficial/CentralOps/commit/acc3bbea3a938c58c87cf95362c09ab4ab5fdaac))
* filtro de coleta plugin-driven, trava de ciclo e medição do atraso real ([bf1d3aa](https://github.com/SEGARK-oficial/CentralOps/commit/bf1d3aa1de98274858cfc090602c84b45c83f1ed))
* **helm:** expor as flags de PII, redução, metering e drift no chart ([492c7bb](https://github.com/SEGARK-oficial/CentralOps/commit/492c7bbd709061cebf84ff26e544e8e30ac7bd70))
* **mappings:** podar o raw nos seeds — drop dos blobs Sophos e drop_nulls ([d3d2616](https://github.com/SEGARK-oficial/CentralOps/commit/d3d2616a9165fa71c46ec481e9943e5b9f9b3c64))
* **metering:** decompor bytes_saved por causa e sinalizar mistura de unidades ([1a585c9](https://github.com/SEGARK-oficial/CentralOps/commit/1a585c9ff0981f84fb90a8bf61eaeceaa29724b7))
* **reduction:** primitiva de drop do raw + Route.drop_raw, e o bug que apagava raw_reduction a cada edição ([7ad8a10](https://github.com/SEGARK-oficial/CentralOps/commit/7ad8a10f301768a281eb5af00c059b85ba791fe0))
* **reduction:** primitiva de drop no raw_reduction (drop/keep_only/drop_nulls) ([3418799](https://github.com/SEGARK-oficial/CentralOps/commit/3418799bd4218b7b0969f373d66c8bc42abea19c))
* **routes-ui:** expor "descartar raw" no formulário de rota ([17dfc90](https://github.com/SEGARK-oficial/CentralOps/commit/17dfc900003f27455637a04f844edea1e7f2f21a))
* **routing:** Route.drop_raw — descartar o bloco raw por destino ([2546781](https://github.com/SEGARK-oficial/CentralOps/commit/25467818ac253972512c169cf131e2f841791d10))
* **ui:** configurar o filtro de coleta e distinguir os dois atrasos ([45b56b0](https://github.com/SEGARK-oficial/CentralOps/commit/45b56b093046032715bcd463d6b219b15f892983))


### Bug Fixes

* **capture:** scrubbar segredos embutidos em valores no ring de captura ([6578601](https://github.com/SEGARK-oficial/CentralOps/commit/657860112f9e6d0d8d9112b7065d093904fab289))
* **collect:** trava por (integração, stream) e medição do atraso real ([a52de5c](https://github.com/SEGARK-oficial/CentralOps/commit/a52de5c63beffdb339903ecfa33b0558400d1645))
* **config-bundle:** preservar drop_raw no round-trip de exportação ([96e263e](https://github.com/SEGARK-oficial/CentralOps/commit/96e263e29ff94be1faeb4a903bcb103a836c9af6))
* **destinations:** não exibir o bucket parcial como se fosse a taxa do destino ([416e8e7](https://github.com/SEGARK-oficial/CentralOps/commit/416e8e71314a6b6166257ea867c14e77abb3c735))
* **drift-ui:** tornar drift sem mapping filtrável e não engolir erro de carga ([0a906e1](https://github.com/SEGARK-oficial/CentralOps/commit/0a906e1280a240d5e6dbab92b484178d4a0a6ac6))
* **drift,capture,metering:** detecção por path, captura com rota/antes-depois/export e metering honesto ([71aae41](https://github.com/SEGARK-oficial/CentralOps/commit/71aae41289e8c8e605a3d45c1eb073970d7e8ab9))
* **drift,dedupe:** escopo subtree em drift/samples e TTL de dedupe em horas (piso 4h) ([ab33027](https://github.com/SEGARK-oficial/CentralOps/commit/ab33027508d22e0839e64ad70130dd60b8259bcf))
* **drift,mappings:** escopo subtree-aware em drift, discover-fields e samples ([ff4a1f7](https://github.com/SEGARK-oficial/CentralOps/commit/ff4a1f7afd629ba78ae2dd636f24b33f8100cb28))
* **drift:** detectar campos por PATH, não por chave de topo ([4c295b1](https://github.com/SEGARK-oficial/CentralOps/commit/4c295b17f644a22aab0c3a01b140d724328613ed))
* **drift:** mascarar o valor de amostra persistido (PII fail-closed) ([42976ba](https://github.com/SEGARK-oficial/CentralOps/commit/42976baf25f3707a4b77e1bebe2ecdd4c42d3ed7))
* **drift:** upsert atômico e fail-closed em organização nula ([cf66dff](https://github.com/SEGARK-oficial/CentralOps/commit/cf66dfffa18f2d667aff5539fb7bf02a45cd95ba))
* **flow:** creditar volume da fonte no minuto real, não no fim do ciclo ([a92a547](https://github.com/SEGARK-oficial/CentralOps/commit/a92a5471ebdc4402119e2b606d2d5f4a3126f8fe))
* **flow:** parar de recalcular a redução e rotular as bases de medição do card ([f801b51](https://github.com/SEGARK-oficial/CentralOps/commit/f801b5137f771f0dd58a86180fdefbaa73b0cba0))
* **mappings:** parar de apagar raw_reduction a cada edição de mapping ([22ee873](https://github.com/SEGARK-oficial/CentralOps/commit/22ee873a949139892486360336b50ac89f72e147))
* **observability:** gravar a latência de entrega real (a série era sempre vazia) ([afc3434](https://github.com/SEGARK-oficial/CentralOps/commit/afc343414d4210392309b7aec9809b40e606ed45))
* **routes-ui:** corrigir textos da UI que descreviam o produto errado ([e4bedba](https://github.com/SEGARK-oficial/CentralOps/commit/e4bedbac086476763f76c85b2aed864d11a729fa))
* **routes:** escopo de rotas subtree-aware, alinhado com fontes e integrações ([0b2d2fb](https://github.com/SEGARK-oficial/CentralOps/commit/0b2d2fb89f92208a82e11b6359d0389cdf32914a))
* **routes:** revalidar condição e chave de supressão ao restaurar versão ([d6f0096](https://github.com/SEGARK-oficial/CentralOps/commit/d6f00961cac5b0666bc77e2f5170755cf5ab216c))
* **routes:** validar suppress_key com a mesma allowlist da condition ([34f719f](https://github.com/SEGARK-oficial/CentralOps/commit/34f719f92ab977eeb8983fa074da14d99acc742b))
* **routing:** corrigir aviso que dizia alavancas de redução desligadas por padrão ([2d45e18](https://github.com/SEGARK-oficial/CentralOps/commit/2d45e182d14c9f61c847ccb3c3d1f46d4e038b27))
* **routing:** não carimbar sample_rate em rota protegida ([3b00539](https://github.com/SEGARK-oficial/CentralOps/commit/3b00539648fea6a17cc72dae1ab89cff92b5fb4f))
* **suppress,routes,observability:** supressão que descartava tudo, latência nunca gravada e contadores do /flow inflados ([cb8c0c8](https://github.com/SEGARK-oficial/CentralOps/commit/cb8c0c81230781b8fd5bbf3f6872624d2a32fb3f))
* **suppress:** não descartar tráfego com assinatura degenerada nem em rota protegida ([159f8d4](https://github.com/SEGARK-oficial/CentralOps/commit/159f8d4be47cbcde8b171a2641098293d7d56ee3))

## [2.2.0](https://github.com/SEGARK-oficial/CentralOps/compare/v2.1.0...v2.2.0) (2026-07-19)


### Features

* **auth:** add correlation.preview, never granted to read-only roles ([925e971](https://github.com/SEGARK-oficial/CentralOps/commit/925e971f05856dd8cb00ff65b13c7aece54a2a79))
* **capture:** record every event outcome, not just successful deliveries ([d6136e9](https://github.com/SEGARK-oficial/CentralOps/commit/d6136e9687d7729026e26e220811929fb5b40e83))
* **i18n:** add the correlation namespace in pt, en and es ([253fc56](https://github.com/SEGARK-oficial/CentralOps/commit/253fc56a2a5e2a1568cf588c63e2725065321f24))
* **inflight:** classify events in the pipeline, before they reach the SIEM ([428e337](https://github.com/SEGARK-oficial/CentralOps/commit/428e337199f5311a033e757062ac17c8b0d71e91))
* **inflight:** preview a rule against real samples without persisting anything ([184efd9](https://github.com/SEGARK-oficial/CentralOps/commit/184efd9ddf655760d80d8d676344b8a8bb96444a))
* **observability:** per-rule 24h counters, which the 3h TTL made impossible ([66ab079](https://github.com/SEGARK-oficial/CentralOps/commit/66ab079839bcef5e8242c83c5edd36c520d671d9))
* **routes:** expose the volume-reduction levers end to end ([fcf6044](https://github.com/SEGARK-oficial/CentralOps/commit/fcf60446dd3f845dee2ede819a2423aa47a7f6c1))


### Bug Fixes

* **capture-ui:** align the outcome vocabulary with the backend enum ([9104a60](https://github.com/SEGARK-oficial/CentralOps/commit/9104a60863989ad15a9b144df2a06997428cbdbb))
* **contract:** expose eval_mode to the client and name inflight as a Detection source ([4c7f0f8](https://github.com/SEGARK-oficial/CentralOps/commit/4c7f0f87a5fffb4682c3f0771512591b85af7640))
* **correlation:** normalise timestamps to UTC and fail closed on a broken where ([440a69e](https://github.com/SEGARK-oficial/CentralOps/commit/440a69e468b5be8a3117c16523f193273ad602d3))
* **dedupe:** cut the TTL to 1 day, unify its default, and surface Redis eviction ([ddaf54d](https://github.com/SEGARK-oficial/CentralOps/commit/ddaf54d2589c52abe13714d6330f28d5cf3d696b))
* **dedupe:** release unsettled claims on every data plane, not just kafka ([ac1a246](https://github.com/SEGARK-oficial/CentralOps/commit/ac1a2466f586ec0724169ccc4b7eb35e48de688f))
* **flow:** attach the wheel-zoom listener as non-passive ([fb0817a](https://github.com/SEGARK-oficial/CentralOps/commit/fb0817a87ed8b72f6d94ce11f3f69630e56f146c))
* **inflight:** implement contains, report rule truncation, and unbreak the cython sweep ([a31b9c0](https://github.com/SEGARK-oficial/CentralOps/commit/a31b9c05b63bbe421a67cf8c5bacc78a3804350b))
* **providers:** advertise spec_kinds by runtime availability, not static catalog ([af00344](https://github.com/SEGARK-oficial/CentralOps/commit/af0034418c6fc3ca3f179939fb4828b245c53cd5))
* **quarantine:** cap writes per reason per cycle and keep the metric faithful ([7c13cfa](https://github.com/SEGARK-oficial/CentralOps/commit/7c13cfa05215a21680f7fad4b02a39ced4cd8412))
* **threat-intel:** drop the dead re-export that made the package unimportable ([84afcf8](https://github.com/SEGARK-oficial/CentralOps/commit/84afcf82aae0b88794b09eec89ce3b1669bddacb))


### Performance Improvements

* **normalize:** resolve simple dot-paths without the jmespath interpreter ([2034852](https://github.com/SEGARK-oficial/CentralOps/commit/20348527890f4f415569da643836595ed4a667b2))

## [2.1.0](https://github.com/SEGARK-oficial/CentralOps/compare/v2.0.0...v2.1.0) (2026-07-18)


### Features

* add manual release trigger to build-and-publish and update release-please to build both API and frontend images with provenance and signing ([a41c7ba](https://github.com/SEGARK-oficial/CentralOps/commit/a41c7ba01ab076e996e55f333df4780b35c8f65b))


### Bug Fixes

* **collectors:** bound per-cycle work in all paginating collectors to stop the soft-timeout poison-loop ([4ecddaa](https://github.com/SEGARK-oficial/CentralOps/commit/4ecddaa07961f93a8f0e2c91c7b2ed71a8ba5395))
* dispose DB engine pool on worker process initialization to ensure fork safety ([8489f54](https://github.com/SEGARK-oficial/CentralOps/commit/8489f5414e7c448a557cb5d8758e1cca62062548))
* **flow:** record per-route route/drop counters unconditionally, not only under sampling ([db492f0](https://github.com/SEGARK-oficial/CentralOps/commit/db492f082c8612d6b5373944bcb1e340462e7932))
* **metering:** credit bytes_saved for sampling and suppression reduction levers ([1a3cf9b](https://github.com/SEGARK-oficial/CentralOps/commit/1a3cf9ba2fd25ece297d19223545224ec23063a4))
* **ocsf:** emit timestamp_t in milliseconds, map Veeam to 1006 and CloudWatch to Base Event ([659c110](https://github.com/SEGARK-oficial/CentralOps/commit/659c11077f6a6cc500eaf98b887bfce0ce197965))
* update correlation engine to support millisecond timestamps and add Veeam brand icon ([21710bb](https://github.com/SEGARK-oficial/CentralOps/commit/21710bbe420362aedf4f76b461d72da3a4a7e320))


### Performance Improvements

* **flow:** memoize FlowCanvas node/edge visuals, throttle pan to rAF, dedupe gradients, cap SMIL particles ([87d8126](https://github.com/SEGARK-oficial/CentralOps/commit/87d8126047b1cdc539b9a8eedf555bd4011202eb))

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
