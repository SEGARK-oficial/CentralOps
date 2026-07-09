/**
 * ocsfTemplates — templates OCSF pré-construídos para o editor de mapping.
 *
 * Dados puramente estáticos: sem React, sem side effects.
 * Os valores de class_uid, category_uid e SEVERITY_ID foram lidos diretamente de
 * backend/app/collectors/normalize/ocsf/classes.py para garantir consistência.
 *
 * OCSF spec: https://schema.ocsf.io/1.3.0
 * classes.py:
 *   CLASS_UID_DETECTION_FINDING = 2004
 *   CLASS_UID_INCIDENT_FINDING  = 2005
 *   CATEGORY_UID_FINDINGS       = 2
 *   SEVERITY_ID (verbatim):
 *     unknown=0, informational=1, low=2, medium=3, high=4, critical=5, fatal=6, other=99
 *
 * Email Activity (4009) pertence à categoria 4 (Network/Application Activity)
 * conforme OCSF taxonomy; não está presente em classes.py porque esse vendor stream
 * ainda não foi implementado no backend — mas o class_uid e category_uid são os
 * valores oficiais do schema OCSF v1.3.0.
 */

import type { MappingRule } from "@/types"

export interface OcsfTemplate {
  id: string
  name: string
  description: string
  class_uid: number
  category_uid: number
  rules: MappingRule[]
}

// ── Mapa de severidade — verbatim de backend/app/collectors/normalize/ocsf/classes.py ──
const SEVERITY_VALUE_MAP: Record<string, unknown> = {
  unknown: 0,
  informational: 1,
  low: 2,
  medium: 3,
  high: 4,
  critical: 5,
  fatal: 6,
  other: 99,
}

// ── Detection Finding (2004) ──────────────────────────────────────────────────
// class_uid=2004, category_uid=2, type_uid=2004*100+1=200401 (activity_id=1=Create)

const DETECTION_FINDING_RULES: MappingRule[] = [
  {
    target: "normalized.class_uid",
    const: 2004,
    required: true,
  },
  {
    target: "normalized.category_uid",
    const: 2,
    required: true,
  },
  {
    target: "normalized.activity_id",
    const: 1,
    required: true,
    expected_always_default: false,
  },
  {
    target: "normalized.type_uid",
    const: 200401,
    required: true,
  },
  {
    target: "normalized.time",
    source: "",
    type_cast: "iso_to_epoch",
    default: 0,
    required: true,
    expected_always_default: true,
  },
  {
    target: "normalized.severity_id",
    source: "",
    value_map: SEVERITY_VALUE_MAP,
    required: true,
  },
  {
    target: "normalized.metadata.product.name",
    source: "",
    default: "Unknown",
  },
  {
    target: "normalized.metadata.version",
    const: "1.5.0",
  },
  {
    target: "normalized.finding_info.uid",
    source: "id",
  },
]

// ── Incident Finding (2005) ───────────────────────────────────────────────────
// class_uid=2005, category_uid=2, type_uid=2005*100+1=200501 (activity_id=1=Create)

const INCIDENT_FINDING_RULES: MappingRule[] = [
  {
    target: "normalized.class_uid",
    const: 2005,
    required: true,
  },
  {
    target: "normalized.category_uid",
    const: 2,
    required: true,
  },
  {
    target: "normalized.activity_id",
    const: 1,
    required: true,
    expected_always_default: false,
  },
  {
    target: "normalized.type_uid",
    const: 200501,
    required: true,
  },
  {
    target: "normalized.time",
    source: "",
    type_cast: "iso_to_epoch",
    default: 0,
    required: true,
    expected_always_default: true,
  },
  {
    target: "normalized.severity_id",
    source: "",
    value_map: SEVERITY_VALUE_MAP,
    required: true,
  },
  {
    target: "normalized.metadata.product.name",
    source: "",
    default: "Unknown",
  },
  {
    target: "normalized.metadata.version",
    const: "1.5.0",
  },
  {
    target: "normalized.incident.uid",
    source: "id",
  },
]

// ── Email Activity (4009) ─────────────────────────────────────────────────────
// class_uid=4009, category_uid=4, type_uid=4009*100+1=400901 (activity_id=1=Send)

const EMAIL_ACTIVITY_RULES: MappingRule[] = [
  {
    target: "normalized.class_uid",
    const: 4009,
    required: true,
  },
  {
    target: "normalized.category_uid",
    const: 4,
    required: true,
  },
  {
    target: "normalized.activity_id",
    const: 1,
    required: true,
    expected_always_default: false,
  },
  {
    target: "normalized.type_uid",
    const: 400901,
    required: true,
  },
  {
    target: "normalized.time",
    source: "",
    type_cast: "iso_to_epoch",
    default: 0,
    required: true,
    expected_always_default: true,
  },
  {
    target: "normalized.severity_id",
    source: "",
    value_map: SEVERITY_VALUE_MAP,
    required: true,
  },
  {
    target: "normalized.metadata.product.name",
    source: "",
    default: "Unknown",
  },
  {
    target: "normalized.metadata.version",
    const: "1.5.0",
  },
  {
    target: "normalized.email.from",
    source: "",
  },
  {
    target: "normalized.email.to",
    source: "",
  },
  {
    target: "normalized.email.subject",
    source: "",
  },
]

// ── Catálogo exportado ────────────────────────────────────────────────────────

export const OCSF_TEMPLATES: OcsfTemplate[] = [
  {
    id: "detection_finding_2004",
    name: "Detection Finding (2004)",
    description:
      "Para alertas e detecções de endpoint, email, identidade. Usa class_uid=2004, category=Findings.",
    class_uid: 2004,
    category_uid: 2,
    rules: DETECTION_FINDING_RULES,
  },
  {
    id: "incident_finding_2005",
    name: "Incident Finding (2005)",
    description:
      "Para casos e incidentes orquestrados que agregam múltiplas detecções. Usa class_uid=2005, category=Findings.",
    class_uid: 2005,
    category_uid: 2,
    rules: INCIDENT_FINDING_RULES,
  },
  {
    id: "email_activity_4009",
    name: "Email Activity (4009)",
    description:
      "Para eventos de email (delivery, quarantine, DLP). Usa class_uid=4009, category=Application Activity.",
    class_uid: 4009,
    category_uid: 4,
    rules: EMAIL_ACTIVITY_RULES,
  },
]
