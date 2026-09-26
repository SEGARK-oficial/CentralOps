import { useEffect, type RefObject } from "react"

const APP_NAME = "CentralOps"

/**
 * useDocumentTitle — deriva `document.title` do `<h1>` real da página atual.
 *
 * A11Y-07: não havia `document.title` por rota (o navegador/leitor de tela
 * sempre anunciava só "CentralOps" ao navegar). Em vez de manter um SEGUNDO
 * mapa rota→título (que teria que ficar sincronizado com o `PageHeader`,
 * posse da Sub 8, e com o mapa já existente em `Breadcrumbs`), observamos o
 * `<h1>` que cada página já renderiza — inclusive quando o título só fica
 * disponível depois de um fetch assíncrono (ex.: nome da integração).
 *
 * `containerRef` deve envolver a área onde o `<Outlet>` da rota é renderizado.
 */
export function useDocumentTitle(containerRef: RefObject<HTMLElement | null>, routeKey: string): void {
  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const applyTitle = () => {
      const heading = container.querySelector("h1")
      const text = heading?.textContent?.trim()
      document.title = text ? `${text} — ${APP_NAME}` : APP_NAME
    }

    applyTitle()

    // Várias páginas só sabem o h1 definitivo depois de um fetch (ex.: nome
    // real da integração/mapeamento); observamos o subtree até ele existir.
    const observer = new MutationObserver(applyTitle)
    observer.observe(container, { childList: true, subtree: true, characterData: true })

    return () => observer.disconnect()
  }, [containerRef, routeKey])
}
