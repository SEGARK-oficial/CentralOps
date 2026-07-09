import type React from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"

interface LoadingSpinnerProps {
  size?: "sm" | "md" | "lg"
  className?: string
  text?: string
}

const sizeMap = {
  sm: "h-4 w-4",
  md: "h-6 w-6",
  lg: "h-10 w-10",
}

export const LoadingSpinner: React.FC<LoadingSpinnerProps> = ({ size = "md", className, text }) => {
  const { t } = useTranslation("ui")
  return (
    <div
      className={cn("flex flex-col items-center justify-center gap-3", className)}
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <svg className={cn("animate-spin text-primary-500", sizeMap[size])} viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" className="opacity-25" />
        <path
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
          fill="currentColor"
          className="opacity-75"
        />
      </svg>
      {/* Texto visível é anunciado via role=status; sem texto, sr-only garante anúncio. */}
      {text ? <p className="text-sm text-text-secondary">{text}</p> : <span className="sr-only">{t("loadingSpinner.loading")}</span>}
    </div>
  )
}

export default LoadingSpinner
