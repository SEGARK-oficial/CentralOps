import type {ReactNode} from 'react';
import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';

type FeatureItem = {
  title: string;
  Svg: React.ComponentType<React.ComponentProps<'svg'>>;
  description: ReactNode;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Multi-vendor por design',
    Svg: require('@site/static/img/feature-multivendor.svg').default,
    description: (
      <>
        Registry pluggable de providers — Sophos Central/XDR, Microsoft Defender,
        Wazuh e NinjaOne de fábrica. Adicionar um vendor novo é registrar uma
        classe Python. Saída pluggable também: Wazuh hoje, qualquer SIEM amanhã
        via <code>_Target</code> Protocol.
      </>
    ),
  },
  {
    title: 'Normalização versionada (CML)',
    Svg: require('@site/static/img/feature-normalization.svg').default,
    description: (
      <>
        <strong>CML — CentralOps Mapping Language</strong> é uma DSL JSON
        declarativa pra normalizar vendor → OCSF. Versionada, com diff entre
        versões, dry-run contra sample reservoir, rollback append-only, drift
        detection automática e <code>array_builder</code> pra observáveis OCSF.
      </>
    ),
  },
  {
    title: 'MSSP-grade multi-tenant',
    Svg: require('@site/static/img/feature-multitenant.svg').default,
    description: (
      <>
        Auto-discovery de tenants Sophos Partner com workflow de aprovação
        humana (pending → approved → excluded → stale), isolamento por
        organização, audit trail completo, quarentena formal com reprocesso
        idempotente e RBAC por papel (Viewer / Operator / Engineer / Admin).
      </>
    ),
  },
];

function Feature({title, Svg, description}: FeatureItem) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        {/* Decorative: the heading and copy beside it carry the meaning, and
            SVGR strips the <title> out of the source file anyway, so role="img"
            would announce an unnamed graphic. */}
        <Svg className={styles.featureSvg} aria-hidden />
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <section className={styles.features}>
      <div className="container">
        <div className="row">
          {FeatureList.map((props, idx) => (
            <Feature key={idx} {...props} />
          ))}
        </div>
      </div>
    </section>
  );
}
