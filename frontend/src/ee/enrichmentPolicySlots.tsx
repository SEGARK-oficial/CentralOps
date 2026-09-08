import type { ReactElement } from "react"
import { CrownIcon, ExternalLinkIcon } from "lucide-react"
import { useTranslation } from "react-i18next"

import { Notice } from "@/components/ui/Notice/Notice"
import { DOCS } from "@/lib/docs"
import type { EnrichmentPolicy } from "@/services/api"

/**
 * Seam Enterprise do editor de política de enriquecimento.
 *
 * Este é o **stub da Community**. O painel do modelo da matriz — marcar a
 * política como modelo e aplicá-la às organizações filhas, com verificação
 * prévia por filha — é Enterprise e vive no overlay `@centralops/web-ee`. O
 * build Enterprise substitui este módulo por alias
 * (`@/ee/enrichmentPolicySlots`), então aquele painel existe SÓ no pacote
 * Enterprise; o artefato Community não carrega o código.
 *
 * Em vez de renderizar `null` — um muro invisível, em que o operador de um MSP
 * não descobre que existe uma forma de aplicar a mesma política a N clientes —,
 * o stub aponta o caminho que a Community de fato tem: copiar para outra
 * organização, uma de cada vez. É o mesmo mecanismo, manual.
 *
 * O aviso só aparece quando há mais de uma organização visível: numa
 * instalação de organização única ele seria propaganda sem função.
 *
 * CONTRATO: o export `EnrichmentPolicyTemplatePanel` e o formato das props
 * precisam permanecer idênticos aos do override Enterprise — o build troca o
 * módulo inteiro por alias.
 */
export interface EnrichmentPolicySlotProps {
  policy: EnrichmentPolicy
  organizations: Array<{ id: number; name: string }>
  /** Recarrega a política depois de uma ação do painel. */
  onChanged: () => void | Promise<void>
}

export function EnrichmentPolicyTemplatePanel({
  organizations,
}: EnrichmentPolicySlotProps): ReactElement | null {
  const { t } = useTranslation("enrichment")
  if (organizations.length <= 1) return null
  return (
    <Notice
      variant="info"
      icon={<CrownIcon size={16} />}
      title={t("policies.template.communityTitle")}
      data-testid="enterprise-template-signpost"
    >
      <p>{t("policies.template.communityBody")}</p>
      <a
        href={DOCS.editionsUpgrade}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-1 inline-flex items-center gap-1 font-medium underline"
      >
        {t("policies.template.communityLink")}
        <ExternalLinkIcon size={12} aria-hidden="true" />
      </a>
    </Notice>
  )
}
