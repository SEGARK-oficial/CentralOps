import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'CentralOps',
  tagline: 'Security data pipeline para SOC multi-tenant e MSSPs — coleta, normalização (CML) e roteamento pluggable',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://segark-oficial.github.io',
  baseUrl: '/CentralOps/',

  organizationName: 'SEGARK-oficial',
  projectName: 'CentralOps',
  trailingSlash: false,

  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',

  // .md = CommonMark (não MDX): docs em prosa usam {chaves}/<colchetes> livremente
  // sem o parser tentar avaliá-los como JSX. Nenhum doc usa import/componente MDX.
  markdown: {
    format: 'detect',
  },

  i18n: {
    // Docs são escritas em pt-BR (default). en/es adicionados para o rollout de
    // i18n — a UI do tema é traduzida; a prosa das páginas cai no pt-BR até ser
    // traduzida no lançamento (conteúdo, não infra). Ver i18n/<locale>/.
    defaultLocale: 'pt-BR',
    locales: ['pt-BR', 'en', 'es'],
    localeConfigs: {
      'pt-BR': { label: 'Português' },
      en: { label: 'English' },
      es: { label: 'Español' },
    },
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          routeBasePath: '/',
          // Sem versionamento: só existe a doc ATUAL (2.0), servida em ``/``. A
          // versão 1.7 legada foi removida (produto ainda não lançado — não há
          // base instalada a suportar). O seletor de versão no navbar saiu junto.
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  // Lunr-search: busca client-side estática, sem necessidade de Algolia
  themes: ['docusaurus-lunr-search'],

  themeConfig: {
    image: 'img/centralops-social-card.jpg',
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'CentralOps',
      logo: {
        alt: 'CentralOps',
        src: 'img/logo.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'gettingStartedSidebar',
          position: 'left',
          label: 'Começar',
        },
        {
          type: 'docSidebar',
          sidebarId: 'outputsSidebar',
          position: 'left',
          label: 'Saídas & Rotas',
        },
        {
          type: 'docSidebar',
          sidebarId: 'operationsSidebar',
          position: 'left',
          label: 'Operação',
        },
        {
          type: 'docSidebar',
          sidebarId: 'administrationSidebar',
          position: 'left',
          label: 'Administração',
        },
        {
          type: 'docSidebar',
          sidebarId: 'normalizationSidebar',
          position: 'left',
          label: 'Mappings',
        },
        {
          type: 'docSidebar',
          sidebarId: 'runbooksSidebar',
          position: 'left',
          label: 'Runbooks',
        },
        // Seletor de idioma (pt-BR / en / es).
        {
          type: 'localeDropdown',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Documentação',
          items: [
            {label: 'Começar', to: '/getting-started/overview'},
            {label: 'Operação diária', to: '/operations/dashboard'},
            {label: 'Mappings (CML)', to: '/normalization/overview'},
            {label: 'Runbooks', to: '/runbooks/dispatcher'},
          ],
        },
        {
          title: 'Plataforma',
          items: [
            {label: 'Arquitetura', to: '/concepts/architecture'},
            {label: 'RBAC', to: '/concepts/rbac'},
            {label: 'Integrações', to: '/integrations/overview'},
            {label: 'Compliance', to: '/compliance/retention'},
          ],
        },
        {
          title: 'Recursos',
          items: [
            {label: 'Quickstart', to: '/getting-started/quickstart'},
            {label: 'DSL Cookbook', to: '/normalization/cookbook'},
            {label: 'Troubleshooting', to: '/normalization/troubleshooting'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} CentralOps. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'yaml', 'python', 'typescript', 'jsx', 'tsx'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
