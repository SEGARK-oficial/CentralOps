import type React from "react"

/**
 * R2-5.2 — tela mínima para quando o PRÓPRIO carregamento do i18n falha antes
 * do 1º render (ex.: o chunk do catálogo cai por rede instável). `main.tsx`
 * não tinha `.catch()` no `i18nReady.then(render)`: uma falha aqui deixava a
 * tela em branco pra sempre, sem log nem saída pro usuário.
 *
 * Sem `t()` de propósito — o catálogo é exatamente o que não carregou, e
 * chamar `useTranslation`/`t()` aqui arriscaria repetir a mesma falha (ou
 * suspender de novo, sem namespace nenhum pronto). Texto inline fixo nos
 * dois idiomas mais prováveis de quem acessa (pt/en), sem depender de
 * detecção de idioma nem de nenhum recurso de rede. As classes usam tokens
 * do design system — `globals.css` já carregou antes deste ponto (import
 * estático em `main.tsx`, fora da cadeia de promises do i18n), então os
 * tokens existem independente do i18n ter falhado.
 */
export const BootFailureScreen: React.FC = () => (
  <div
    role="alert"
    className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-sidebar px-6 text-center"
  >
    <div className="max-w-sm rounded-3xl border border-border bg-surface p-8 shadow-xl">
      <p className="text-sm text-text">
        Não foi possível carregar o idioma da interface.
        <br />
        Could not load the interface language.
      </p>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="mt-5 inline-flex items-center justify-center rounded-md border border-border-hover bg-primary-600 px-4 py-2 text-sm font-medium text-text-inverse hover:bg-primary-500"
      >
        Recarregar / Reload
      </button>
    </div>
  </div>
)

export default BootFailureScreen
