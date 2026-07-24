import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  gettingStartedSidebar: [
    'intro',
    {
      type: 'category',
      label: 'Começar',
      collapsed: false,
      items: [
        'getting-started/overview',
        'getting-started/first-login',
        'getting-started/quickstart',
      ],
    },
    {
      type: 'category',
      label: 'Instalação & Deploy',
      collapsed: false,
      items: [
        'deployment/docker-compose',
        'deployment/kubernetes',
        'deployment/configuration',
        'deployment/upgrading',
      ],
    },
    {
      type: 'category',
      label: 'Edições & Upgrade',
      collapsed: false,
      items: [
        'editions/community-vs-enterprise',
        'editions/upgrade',
      ],
    },
    {
      type: 'category',
      label: 'Conceitos',
      collapsed: false,
      items: [
        'concepts/architecture',
        'concepts/rbac',
        'concepts/data-model',
      ],
    },
  ],

  operationsSidebar: [
    {
      type: 'category',
      label: 'Operação diária',
      collapsed: false,
      items: [
        'operations/dashboard',
        'operations/fluxo-de-dados',
        'operations/quarantine',
        'operations/search',
        'operations/federated-search',
        'operations/detections',
        'operations/correlation-rules',
        'operations/pipeline-health',
        'operations/live-capture',
        'operations/observability',
        'operations/history-audit',
      ],
    },
  ],

  administrationSidebar: [
    {
      type: 'category',
      label: 'Administração',
      collapsed: false,
      items: [
        'administration/users-and-roles',
        'administration/sso-entra',
        'administration/organizations',
        'administration/tenant-hierarchy',
        'administration/platform-config',
        'administration/secrets-and-master-key',
      ],
    },
    {
      type: 'category',
      label: 'Integrações',
      collapsed: false,
      items: [
        'integrations/overview',
        'integrations/sophos',
        'integrations/wazuh',
        'integrations/crowdstrike',
        'integrations/microsoft-defender',
        'integrations/dfir-iris',
        'integrations/push-ingestion',
        'integrations/fortinet-fortigate',
        'integrations/windows-event-log',
        'integrations/adding-new-vendor',
      ],
    },
  ],

  normalizationSidebar: [
    {
      type: 'category',
      label: 'Mappings (CML)',
      collapsed: false,
      items: [
        'normalization/overview',
        'normalization/cml',
        'normalization/dsl-spec',
        'normalization/cookbook',
        'normalization/operators-reference',
        'normalization/use-cases',
        'normalization/troubleshooting',
        'normalization/migration-v1-to-v2',
      ],
    },
    {
      type: 'category',
      label: 'Pipelines',
      collapsed: false,
      items: [
        'pipelines/collectors',
        'pipelines/collection-filters',
        'pipelines/scheduler',
        'pipelines/drift',
        'pipelines/backfill',
      ],
    },
  ],

  outputsSidebar: [
    {
      type: 'category',
      label: 'Saídas & Roteamento',
      collapsed: false,
      items: [
        'outputs/overview',
        {
          type: 'category',
          label: 'Destinos',
          collapsed: false,
          items: [
            'outputs/destinations',
            'outputs/destination-wazuh-syslog',
            'outputs/destination-splunk-hec',
            'outputs/destination-elastic',
            'outputs/destination-clickhouse',
            'outputs/destination-crowdstrike-ngsiem',
            'outputs/destination-crowdstrike-logscale',
            'outputs/destination-s3',
            'outputs/destination-sentinel',
            'outputs/destination-kafka',
            'outputs/destination-otlp',
            'outputs/destination-operations',
            'outputs/destination-webhook',
            'outputs/destination-datadog',
            'outputs/destination-chronicle',
            'outputs/destination-security-lake',
            'outputs/adding-a-destination',
          ],
        },
        {
          type: 'category',
          label: 'Roteamento',
          collapsed: false,
          items: [
            'outputs/routing',
            'outputs/routing-canary',
            'outputs/routing-dry-run',
            'outputs/reducao-de-volume',
            'outputs/pii-redaction',
          ],
        },
        'outputs/observability',
      ],
    },
  ],

  runbooksSidebar: [
    {
      type: 'category',
      label: 'Runbooks (SRE)',
      collapsed: false,
      items: [
        'runbooks/dispatcher',
        'runbooks/scheduler-stuck',
        'runbooks/collection-lag-backlog',
        'runbooks/high-quarantine-rate',
        'runbooks/slo-burn',
        'runbooks/redis-capacity',
        'runbooks/dlq-and-destination-delivery',
        'runbooks/destination-down',
        'runbooks/routing-misroute',
        'runbooks/pii-redaction-blocked',
        'runbooks/iris-handoff-customer-mapping',
        'runbooks/migration-and-boot',
      ],
    },
    {
      type: 'category',
      label: 'Compliance',
      collapsed: false,
      items: [
        'compliance/retention',
        'compliance/lgpd-gdpr',
      ],
    },
  ],
};

export default sidebars;
