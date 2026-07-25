import type React from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"

/**
 * Selo de status de uma fonte degradada.
 *
 * `status` chega como o enum cru do backend (`routers/dashboard.py` só empurra
 * `degraded` e `error` para `degraded_items`, mas o campo é string livre e enum
 * novo já vazou antes). Traduzir aqui evita a UI em português exibindo
 * "degraded"; enum desconhecido cai no valor cru, que é honesto — inventar um
 * rótulo esconderia o drift em vez de mostrá-lo.
 */
const STATUS_LABEL_KEY: Record<string, string> = {
  degraded: "dashboardPage.sourcesHealth.status.degraded",
  error: "dashboardPage.sourcesHealth.status.error",
  unhealthy: "dashboardPage.sourcesHealth.status.unhealthy",
  unknown: "dashboardPage.sourcesHealth.status.unknown",
}

/**
 * A lista inteira já diz "isto precisa de você"; a matiz diferencia DENTRO dela.
 *
 * `error` é vermelhão porque parou de coletar; `degraded`/`unhealthy` são âmbar
 * porque ainda coletam. `unknown` fica sem matiz de propósito: é ausência de
 * sinal, não um grau de falha, e pintá-lo de âmbar seria inventar a severidade
 * que justamente não temos — mesmo motivo pelo qual o enum desconhecido cai no
 * valor cru em vez de ganhar rótulo. Sem `success` aqui: nada nesta lista está bem.
 */
const STATUS_VARIANT: Record<string, "danger" | "warning" | "outline"> = {
  error: "danger",
  degraded: "warning",
  unhealthy: "warning",
  unknown: "outline",
}

interface SourceStatusBadgeProps {
  status: string
}

export const SourceStatusBadge: React.FC<SourceStatusBadgeProps> = ({ status }) => {
  const { t } = useTranslation("dashboard")
  const normalized = status.toLowerCase()
  const key = STATUS_LABEL_KEY[normalized]

  return (
    <Badge variant={STATUS_VARIANT[normalized] ?? "outline"} size="sm">
      {key ? t(key) : status}
    </Badge>
  )
}
