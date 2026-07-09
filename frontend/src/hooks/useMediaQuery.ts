import { useEffect, useState } from "react"

/**
 * Reage a uma media query (ex.: "(min-width: 1280px)") de forma SSR-safe e
 * com cleanup do listener. Usado para alternar entre layouts (ex.: painéis
 * redimensionáveis no desktop vs. empilhados no mobile).
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false,
  )

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return
    const mql = window.matchMedia(query)
    const handler = () => setMatches(mql.matches)
    handler()
    mql.addEventListener("change", handler)
    return () => mql.removeEventListener("change", handler)
  }, [query])

  return matches
}
