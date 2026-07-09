import type React from "react"
import { useTranslation } from "react-i18next"
import { Notice } from "@/components/ui/Notice/Notice"

interface LastErrorPanelProps {
  lastError: string | null
}

export const LastErrorPanel: React.FC<LastErrorPanelProps> = ({ lastError }) => {
  const { t } = useTranslation("dashboard")
  if (!lastError) return null

  return (
    <Notice variant="warning" title={t("health.lastError.title")}>
      <code className="block mt-2 whitespace-pre-wrap break-all font-mono text-xs text-warning-700">
        {lastError}
      </code>
    </Notice>
  )
}
