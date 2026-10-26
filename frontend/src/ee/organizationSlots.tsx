import type { ReactElement } from "react"

import type { Organization } from "@/types"

/**
 * Seam Enterprise das ações por linha da lista de Organizações.
 *
 * Este é o **stub da Community**: vincular/desvincular uma organização do
 * parceiro Sophos (ADR-0017) é gestão de revenda multi-tenant — Enterprise — e a
 * UI real vive no overlay `@centralops/web-ee`. O build Enterprise substitui este
 * módulo por alias (`@/ee/organizationSlots` -> `@centralops/web-ee/organizationSlots`),
 * então o botão e o diálogo existem SÓ no pacote Enterprise; o artefato Community
 * não carrega o código.
 *
 * Diferente de `integrationDetailSlots`, o stub renderiza `null` e não um
 * signpost: a linha de organização já é densa, e o upsell do multi-tenant mora na
 * tela da integração parceiro, onde o operador de fato descobre tenants.
 *
 * A página só monta o slot dentro da rota de administração (`RoleGuard admin`).
 * O backend reforça de qualquer forma (org.manage + integration.write + escopo
 * global + licença `multi_tenant`).
 *
 * CONTRATO: o export `OrganizationRowExtraActions` e o formato das props
 * (`OrganizationRowSlotProps`) precisam permanecer idênticos aos do override
 * Enterprise — o build troca o módulo inteiro por alias. (O override redeclara o
 * tipo localmente em vez de exportá-lo, para não importar nada do Core além de
 * tipos; só o export do componente faz parte do contrato.)
 */
export interface OrganizationRowSlotProps {
  organization: Organization
  /** Recarrega a lista depois de uma ação concluída no slot. */
  onChanged: () => void | Promise<void>
}

export function OrganizationRowExtraActions(
  props: OrganizationRowSlotProps,
): ReactElement | null {
  void props
  return null
}
