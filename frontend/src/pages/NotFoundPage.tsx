import type React from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { CompassIcon, ArrowLeftIcon, LayoutDashboardIcon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"

/**
 * Página 404 renderizada DENTRO do shell (mantém sidebar/header/breadcrumbs),
 * substituindo o antigo redirect silencioso para "/" — preserva contexto e dá
 * feedback claro de que a rota não existe (visibilidade de status, Nielsen #1).
 */
const NotFoundPage: React.FC = () => {
  const navigate = useNavigate()
  const { t } = useTranslation("ui")

  return (
    <EmptyState
      icon={<CompassIcon size={48} />}
      title={t("notFound.title")}
      description={t("notFound.description")}
      action={
        <div className="flex flex-wrap items-center justify-center gap-3">
          <Button variant="primary" size="sm" onClick={() => navigate("/dashboard")} leftIcon={<LayoutDashboardIcon size={14} />}>
            {t("notFound.goToDashboard")}
          </Button>
          <Button variant="outline" size="sm" onClick={() => navigate(-1)} leftIcon={<ArrowLeftIcon size={14} />}>
            {t("notFound.back")}
          </Button>
        </div>
      }
      className="py-20"
    />
  )
}

export default NotFoundPage
