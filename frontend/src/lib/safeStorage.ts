/**
 * safeStorage
 *
 * `window.localStorage` lança em QUALQUER acesso — inclusive leitura — quando
 * o storage está bloqueado: modo privado estrito de alguns navegadores,
 * política corporativa (comum em SOC/ambiente gerenciado), cota estourada ou
 * `localStorage` desabilitado via flag do navegador. Código cru
 * (`localStorage.getItem(...)`) derruba o componente inteiro nesses casos —
 * e no shell (`AppLayout`, `PlatformContext`, `AuthContext`) isso acontece
 * ANTES de qualquer error boundary de rota, então a aplicação inteira não
 * monta (R3-6.2).
 *
 * Este wrapper nunca lança: falhas viram no-op (escrita) ou `null`/`0`
 * (leitura), e a persistência entre sessões vira best-effort em vez de
 * requisito de boot.
 */
export const safeStorage = {
  getItem(key: string): string | null {
    try {
      return window.localStorage.getItem(key)
    } catch {
      return null
    }
  },

  setItem(key: string, value: string): void {
    try {
      window.localStorage.setItem(key, value)
    } catch {
      // Persistência é best-effort — perder a preferência não pode derrubar
      // a ação que a originou (ex.: colapsar a sidebar, trocar de org).
    }
  },

  removeItem(key: string): void {
    try {
      window.localStorage.removeItem(key)
    } catch {
      // idem
    }
  },

  key(index: number): string | null {
    try {
      return window.localStorage.key(index)
    } catch {
      return null
    }
  },

  get length(): number {
    try {
      return window.localStorage.length
    } catch {
      return 0
    }
  },
}
