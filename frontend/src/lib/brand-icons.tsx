/**
 * brand-icons.tsx — Vendor brand SVG icons for the integrations/destinations catalog.
 *
 * CONTRACT:
 *   brandIconFor(id?, opts?) → React.ReactNode
 *     Returns a real brand SVG for known ids, or falls back to iconFor() from ./icons.
 *   BRAND_IDS: ReadonlySet<string>
 *     Set of ids that have a real brand SVG.
 *
 * Resolution is 4-tier (first match wins): local raster asset (CrowdStrike PNG) →
 * react-icons/si (Simple Icons, scalable) → inline hand-drawn SVG → lucide glyph.
 *
 * Design notes:
 *   - A new vendor whose slug exists in Simple Icons needs ONE line in SI_MARKS —
 *     no hand-drawn SVG. Only si-excluded brands (Microsoft/AWS/Amazon/Google/
 *     CrowdStrike/Sophos) need a local asset (Tier 1) or inline glyph (Tier 3).
 *   - Every node is sized + aria-hidden="true". The node is usually <svg>, but a
 *     raster vendored asset (CrowdStrike) renders <img> — consumers must not
 *     assume <svg>.
 *   - Generic marks (syslog/webhook/jsonl) use currentColor; brand marks fix color.
 *   - No remote fetches; local assets are bundled.
 *   - Named exports (e.g. SplunkIcon) remain for direct use / back-compat.
 */

import type React from "react"
import {
  SiApachekafka,
  SiSplunk,
  SiDatadog,
  SiOpentelemetry,
  SiOkta,
  SiFortinet,
  SiVeeam,
} from "react-icons/si"
import { iconFor } from "./icons"

// CrowdStrike é o único asset vendado localmente: o Simple Icons não carrega a
// marca (restrição de trademark) e o logo oficial é fornecido como PNG (raster).
import crowdstrikePng from "@/assets/crowdstrike.png"

// ── Shared SVG wrapper type ───────────────────────────────────────────────────

interface BrandSvgProps {
  size?: number
  className?: string
}

// ── Brand icon components ─────────────────────────────────────────────────────

export function SplunkIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Splunk: green arrow-chevron mark */}
      <path d="M2 7l10 5-10 5V7z" fill="#65A637" />
      <path d="M8 4l10 5-10 5V4z" fill="#3CB34A" />
      <path d="M14 1l8 5-8 5V1z" fill="#00A651" />
    </svg>
  )
}

export function ElasticIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Elastic: stacked colored bars (simplified mark) */}
      <rect x="3" y="3" width="18" height="3.5" rx="1.75" fill="#F04E98" />
      <rect x="3" y="8.25" width="18" height="3.5" rx="1.75" fill="#FEC514" />
      <rect x="3" y="13.5" width="18" height="3.5" rx="1.75" fill="#00BFB3" />
      <rect x="3" y="18.75" width="11" height="3" rx="1.5" fill="#0077CC" />
    </svg>
  )
}

export function ClickHouseIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* ClickHouse: yellow/red vertical bar chart */}
      <rect x="2" y="14" width="3" height="8" rx="1" fill="#FACC14" />
      <rect x="7" y="9" width="3" height="13" rx="1" fill="#FACC14" />
      <rect x="12" y="4" width="3" height="18" rx="1" fill="#E42528" />
      <rect x="17" y="9" width="3" height="13" rx="1" fill="#FACC14" />
      <rect x="22" y="14" width="0" height="8" rx="0" fill="transparent" />
    </svg>
  )
}

export function CrowdStrikeIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* CrowdStrike: simplified red falcon silhouette */}
      <path
        d="M12 2C9 2 6.5 4 5.5 6.5L4 11l4-1.5C8.5 8 10.5 7 12 7c2.5 0 4.5 1.5 5 3.5L18 12l2-5C18.5 4 15.5 2 12 2z"
        fill="#E8001D"
      />
      <path
        d="M18.5 13l-2-.5C16 14.5 14.5 16 12 16c-1.5 0-3-1-3.5-2.5L5 12l1.5 5C7.5 19.5 9.5 22 12 22c3 0 5.5-2.5 6.5-5l.5-1.5-.5-2.5z"
        fill="#C2000A"
      />
    </svg>
  )
}

export function DatadogIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Datadog: purple dog-ear / geometric mark */}
      <rect x="3" y="3" width="8" height="8" rx="2" fill="#632CA6" />
      <rect x="13" y="3" width="8" height="8" rx="2" fill="#774AA4" />
      <rect x="3" y="13" width="8" height="8" rx="2" fill="#774AA4" />
      <rect x="13" y="13" width="8" height="8" rx="2" fill="#632CA6" />
      {/* Center dog-bone cutout feel */}
      <circle cx="12" cy="12" r="2.5" fill="white" opacity="0.85" />
    </svg>
  )
}

export function OpenTelemetryIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* OpenTelemetry: colored horizontal signal bars with orange/blue */}
      <rect x="2" y="5" width="20" height="2.5" rx="1.25" fill="#F5A623" />
      <rect x="2" y="10.75" width="14" height="2.5" rx="1.25" fill="#4F8EF7" />
      <rect x="2" y="16.5" width="8" height="2.5" rx="1.25" fill="#F5A623" />
      <circle cx="20" cy="17.75" r="3" fill="#4F8EF7" />
    </svg>
  )
}

export function ApacheKafkaIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Kafka: abstract node-broker topology */}
      <circle cx="12" cy="12" r="3" fill="#231F20" />
      <circle cx="4" cy="7" r="2.5" fill="#231F20" />
      <circle cx="20" cy="7" r="2.5" fill="#231F20" />
      <circle cx="4" cy="17" r="2.5" fill="#231F20" />
      <circle cx="20" cy="17" r="2.5" fill="#231F20" />
      <line x1="9" y1="10.5" x2="6" y2="8.5" stroke="#231F20" strokeWidth="1.5" />
      <line x1="15" y1="10.5" x2="18" y2="8.5" stroke="#231F20" strokeWidth="1.5" />
      <line x1="9" y1="13.5" x2="6" y2="15.5" stroke="#231F20" strokeWidth="1.5" />
      <line x1="15" y1="13.5" x2="18" y2="15.5" stroke="#231F20" strokeWidth="1.5" />
    </svg>
  )
}

export function AmazonS3Icon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* AWS S3: simplified bucket with AWS orange smile-arrow */}
      <ellipse cx="12" cy="6" rx="8" ry="3" fill="#FF9900" />
      <path d="M4 6v12c0 1.65 3.58 3 8 3s8-1.35 8-3V6" fill="none" stroke="#FF9900" strokeWidth="1.75" />
      <ellipse cx="12" cy="6" rx="8" ry="3" fill="#FF9900" opacity="0.7" />
      {/* AWS smile arrow at bottom */}
      <path
        d="M7.5 17.5 Q12 20 16.5 17.5"
        fill="none"
        stroke="#232F3E"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  )
}

// AmazonSecurityLake shares the AWS S3 visual with a subtle shield overlay
export function AmazonSecurityLakeIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      <ellipse cx="12" cy="6" rx="8" ry="3" fill="#FF9900" />
      <path d="M4 6v10c0 1.65 3.58 3 8 3s8-1.35 8-3V6" fill="none" stroke="#FF9900" strokeWidth="1.75" />
      <ellipse cx="12" cy="6" rx="8" ry="3" fill="#FF9900" opacity="0.7" />
      {/* Shield overlay for "Security Lake" */}
      <path
        d="M12 9.5 L9 11 v3 c0 1.5 1.5 2.5 3 3 1.5-.5 3-1.5 3-3 v-3 L12 9.5z"
        fill="#232F3E"
        opacity="0.85"
      />
    </svg>
  )
}

export function MicrosoftSentinelIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Microsoft 4-square + sentinel shield overlay */}
      <rect x="2.5" y="2.5" width="8.5" height="8.5" rx="0.5" fill="#F25022" />
      <rect x="13" y="2.5" width="8.5" height="8.5" rx="0.5" fill="#7FBA00" />
      <rect x="2.5" y="13" width="8.5" height="8.5" rx="0.5" fill="#00A4EF" />
      <rect x="13" y="13" width="8.5" height="8.5" rx="0.5" fill="#FFB900" />
      {/* Sentinel shield centered */}
      <path
        d="M12 5.5 L8.5 7.5 v4.5 c0 2.5 1.75 4.5 3.5 5.5 1.75-1 3.5-3 3.5-5.5 v-4.5 L12 5.5z"
        fill="white"
        opacity="0.9"
      />
    </svg>
  )
}

export function ChronicleIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Google Chronicle: blue arc/ring with G-style mark */}
      <circle cx="12" cy="12" r="9" fill="none" stroke="#4285F4" strokeWidth="2.5" />
      <path d="M12 12 h5.5" stroke="#4285F4" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="12" cy="12" r="2.25" fill="#4285F4" />
    </svg>
  )
}

export function SyslogIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Generic: network stack lines */}
      <rect x="2" y="5" width="20" height="2" rx="1" />
      <rect x="2" y="11" width="16" height="2" rx="1" />
      <rect x="2" y="17" width="10" height="2" rx="1" />
    </svg>
  )
}

export function WebhookIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Generic: chain-link / hook mark */}
      <path
        d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function JsonlIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Generic: file with curly-brace lines */}
      <rect x="4" y="2" width="16" height="20" rx="2" stroke="currentColor" strokeWidth="1.75" />
      <path d="M8 8h3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M8 12h8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      <path d="M8 16h5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

export function FortinetIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Fortinet: red shield with F-like horizontal bars */}
      <path d="M12 2 L3 6 v7 c0 4.5 4 8 9 9 5-1 9-4.5 9-9 V6 L12 2z" fill="#EE3124" />
      <rect x="8" y="8" width="8" height="2" rx="1" fill="white" />
      <rect x="8" y="12" width="5" height="2" rx="1" fill="white" />
    </svg>
  )
}

export function WindowsIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Windows: classic 4-color window panes */}
      <path d="M2 4.5l9-1.3v9H2V4.5z" fill="#0078D4" />
      <path d="M13 3l9-1.3v9.8H13V3z" fill="#0078D4" />
      <path d="M2 13.8h9v8.7l-9-1.3v-7.4z" fill="#0078D4" />
      <path d="M13 13.8h9v8.3L13 21v-7.2z" fill="#0078D4" />
    </svg>
  )
}

export function SophosIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 65 65"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Sophos: official mark (blue tile + two swooshes) */}
      <path
        fill="#2006f7"
        d="M.24,10.78v25.07c0,4.25,2.3,8.16,6.02,10.22l26.1,14.48.17.09,26.22-14.57c3.71-2.06,6.01-5.97,6.01-10.22V10.78H.24Z"
      />
      <path
        fill="#fff"
        d="M55.43,20.52l-13.57,7.57c-1.31.73-2.79,1.12-4.29,1.12l-25.62.08,12.91-7.2c1.94-1.08,4.14-1.65,6.36-1.64l24.2.07ZM11.67,42.05l13.57-7.57c1.31-.73,2.79-1.12,4.29-1.12l25.62-.08-12.91,7.2c-1.94,1.08-4.14,1.65-6.36,1.64l-24.2-.07Z"
      />
    </svg>
  )
}

export function WazuhIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Wazuh: teal/blue shield with W */}
      <path d="M12 2 L3 6 v7 c0 4.5 4 8 9 9 5-1 9-4.5 9-9 V6 L12 2z" fill="#00A9C7" />
      <path
        d="M7.5 9 L9.5 15 L12 11 L14.5 15 L16.5 9"
        fill="none"
        stroke="white"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function OktaIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Okta: blue circle with inner dot (O-mark) */}
      <circle cx="12" cy="12" r="9.5" fill="#007DC1" />
      <circle cx="12" cy="12" r="4" fill="white" />
    </svg>
  )
}

export function NinjaOneIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* NinjaOne: dark star/shuriken mark */}
      <path
        d="M12 2 L14.5 9 L22 9 L16 13.5 L18.5 21 L12 16.5 L5.5 21 L8 13.5 L2 9 L9.5 9 Z"
        fill="#1C1E21"
      />
    </svg>
  )
}

export function MicrosoftIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Microsoft: 4-square flag */}
      <rect x="2.5" y="2.5" width="8.5" height="8.5" rx="0.5" fill="#F25022" />
      <rect x="13" y="2.5" width="8.5" height="8.5" rx="0.5" fill="#7FBA00" />
      <rect x="2.5" y="13" width="8.5" height="8.5" rx="0.5" fill="#00A4EF" />
      <rect x="13" y="13" width="8.5" height="8.5" rx="0.5" fill="#FFB900" />
    </svg>
  )
}

export function AwsIcon({ size = 28, className }: BrandSvgProps): React.ReactElement {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      aria-hidden="true"
      className={className}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* AWS: orange smile arrow */}
      <path
        d="M6.5 13.5 C5 12 4.5 9 6 7 C7.5 5 10 4 12 4 C14 4 16.5 5 18 7 C19.5 9 19 12 17.5 13.5"
        fill="none"
        stroke="#232F3E"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <path
        d="M5.5 17 Q12 21 18.5 17"
        fill="none"
        stroke="#FF9900"
        strokeWidth="2.5"
        strokeLinecap="round"
      />
      <path d="M17 15.5 L18.5 17 L16.5 18" stroke="#FF9900" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

// ── Brand ID registry ─────────────────────────────────────────────────────────

export const BRAND_IDS: ReadonlySet<string> = new Set([
  // Destinations
  "splunk",
  "elastic",
  "clickhouse",
  "crowdstrike",
  "datadog",
  "opentelemetry",
  "apachekafka",
  "amazons3",
  "amazonsecuritylake",
  "microsoftsentinel",
  "chronicle",
  "syslog",
  "webhook",
  "jsonl",
  // Sources / integrations
  "fortinet",
  "windows",
  "sophos",
  "wazuh",
  "okta",
  "ninjaone",
  "microsoft",
  "aws",
])

// ── Renderer map ──────────────────────────────────────────────────────────────

type BrandRenderer = (size: number, className: string | undefined) => React.ReactElement

const BRAND_RENDERERS: Record<string, BrandRenderer> = {
  splunk: (s, c) => <SplunkIcon size={s} className={c} />,
  elastic: (s, c) => <ElasticIcon size={s} className={c} />,
  clickhouse: (s, c) => <ClickHouseIcon size={s} className={c} />,
  crowdstrike: (s, c) => <CrowdStrikeIcon size={s} className={c} />,
  datadog: (s, c) => <DatadogIcon size={s} className={c} />,
  opentelemetry: (s, c) => <OpenTelemetryIcon size={s} className={c} />,
  apachekafka: (s, c) => <ApacheKafkaIcon size={s} className={c} />,
  amazons3: (s, c) => <AmazonS3Icon size={s} className={c} />,
  amazonsecuritylake: (s, c) => <AmazonSecurityLakeIcon size={s} className={c} />,
  microsoftsentinel: (s, c) => <MicrosoftSentinelIcon size={s} className={c} />,
  chronicle: (s, c) => <ChronicleIcon size={s} className={c} />,
  syslog: (s, c) => <SyslogIcon size={s} className={c} />,
  webhook: (s, c) => <WebhookIcon size={s} className={c} />,
  jsonl: (s, c) => <JsonlIcon size={s} className={c} />,
  fortinet: (s, c) => <FortinetIcon size={s} className={c} />,
  windows: (s, c) => <WindowsIcon size={s} className={c} />,
  sophos: (s, c) => <SophosIcon size={s} className={c} />,
  wazuh: (s, c) => <WazuhIcon size={s} className={c} />,
  okta: (s, c) => <OktaIcon size={s} className={c} />,
  ninjaone: (s, c) => <NinjaOneIcon size={s} className={c} />,
  microsoft: (s, c) => <MicrosoftIcon size={s} className={c} />,
  aws: (s, c) => <AwsIcon size={s} className={c} />,
}

// ── Tier 1: marca vendada localmente (raster) ─────────────────────────────────
// Apenas a CrowdStrike: PNG → <img>, dimensionado + aria-hidden como os demais.

function rasterMark(src: string): BrandRenderer {
  return (s, c) => (
    <img
      src={src}
      width={s}
      height={s}
      className={c}
      alt=""
      aria-hidden="true"
      draggable={false}
      style={{ objectFit: "contain" }}
    />
  )
}

const LOCAL_MARKS: Record<string, BrandRenderer> = {
  crowdstrike: rasterMark(crowdstrikePng),
}

// ── Tier 2: Simple Icons (react-icons/si) ─────────────────────────────────────
// Camada ESCALÁVEL: ~3000 logos de marca vetoriais. Um vendor novo cujo slug
// existe no Simple Icons ganha logo real adicionando UMA linha aqui — sem
// desenhar SVG à mão. `color` fixa a cor de marca (legível no chip branco em
// light + dark). Marcas que o Simple Icons não carrega (Microsoft/AWS/Amazon/
// Google/CrowdStrike/Sophos) ficam no Tier 1 (local) ou Tier 3 (inline).

type SiComponent = (props: { size?: number; color?: string; className?: string } & Record<string, unknown>) => React.ReactElement

function siMark(Comp: SiComponent, color: string): BrandRenderer {
  // react-icons aplica role="img" por padrão; nossos ícones são DECORATIVOS
  // (aria-hidden) — removemos o role para não anunciar duas vezes no leitor de tela.
  return (s, c) => (
    <Comp size={s} color={color} className={c} aria-hidden="true" role={undefined} focusable="false" />
  )
}

const SI_MARKS: Record<string, BrandRenderer> = {
  splunk: siMark(SiSplunk as SiComponent, "#000000"),
  datadog: siMark(SiDatadog as SiComponent, "#632CA6"),
  opentelemetry: siMark(SiOpentelemetry as SiComponent, "#425CC7"),
  okta: siMark(SiOkta as SiComponent, "#007DC1"),
  fortinet: siMark(SiFortinet as SiComponent, "#EE3124"),
  apachekafka: siMark(SiApachekafka as SiComponent, "#231F20"),
  veeam: siMark(SiVeeam as SiComponent, "#00B336"),
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Resolve um ícone de marca para `id`, em 4 camadas (a 1ª que casar vence):
 *   1. LOCAL_MARKS  — SVG/PNG vendado (logo oficial alta fidelidade)
 *   2. SI_MARKS     — react-icons/si (Simple Icons, escalável)
 *   3. BRAND_RENDERERS — SVG inline desenhado à mão (vendors que o si não tem)
 *   4. lucide iconFor — glifo genérico (id sem marca conhecida)
 * Sempre dimensionado a `size` (default 28) e com aria-hidden. O nó pode ser
 * <svg> (tiers 1-3 svg) ou <img> (CrowdStrike raster).
 */
export function brandIconFor(
  id?: string | null,
  opts?: { size?: number; className?: string },
): React.ReactNode {
  const size = opts?.size ?? 28
  const className = opts?.className
  const key = id?.toLowerCase() ?? ""

  const local = LOCAL_MARKS[key]
  if (local) return local(size, className)

  const si = SI_MARKS[key]
  if (si) return si(size, className)

  const renderer = BRAND_RENDERERS[key]
  if (renderer) return renderer(size, className)

  // Fallback: lucide generic glyph
  const Icon = iconFor(id)
  return <Icon size={size} className={className} aria-hidden />
}
