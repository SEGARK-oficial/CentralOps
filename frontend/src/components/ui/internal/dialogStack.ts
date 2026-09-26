/**
 * Pilha compartilhada de "diálogos" empilháveis (Modal, Drawer, ConfirmDialog
 * — tudo que trapeia foco e reage a Escape via listener no `document`).
 *
 * A11Y-02 / A11Y-17: com um listener por instância no `document`, um
 * ConfirmDialog (ou Drawer) aberto por cima de um Modal reagiria ao MESMO
 * Escape que o de baixo — os dois fechariam junto. Só o do TOPO deve reagir.
 *
 * Importante: a ordem NÃO pode vir da ordem de execução do `useEffect` — React
 * roda efeitos de baixo pra cima (filho antes do pai), então um diálogo
 * aninhado (filho) registraria ANTES do diálogo por baixo (pai), invertendo a
 * pilha. Em vez disso, cada instância recebe um número de ordem no PRIMEIRO
 * render (a função de um componente filho só é chamada durante a
 * renderização do pai, então a ordem de render bate com o aninhamento
 * visual), e o "topo" é o maior número entre os diálogos ATUALMENTE abertos.
 */
let orderSeq = 0
const openOrders = new Map<string, number>()

/** Chamar uma única vez por instância, via `useState(() => nextDialogOrder())`. */
export function nextDialogOrder(): number {
  return ++orderSeq
}

export function registerOpenDialog(id: string, order: number): void {
  openOrders.set(id, order)
}

export function unregisterOpenDialog(id: string): void {
  openOrders.delete(id)
}

/** `true` quando `order` é o maior entre todos os diálogos abertos agora. */
export function isTopmostDialog(order: number): boolean {
  const top = Math.max(-1, ...openOrders.values())
  return order === top
}

/**
 * R2-8.5: trava de scroll do `<body>` com CONTADOR. Antes, Modal e Drawer
 * setavam `document.body.style.overflow = "hidden"` no open e `= ""` no
 * cleanup, cada um por conta própria — fechar um ConfirmDialog aninhado
 * (o de CIMA) zerava o overflow mesmo com o Modal de BAIXO ainda aberto,
 * destravando o scroll da página por trás dele. `lockBodyScroll`/
 * `unlockBodyScroll` só mexem no DOM na transição 0→1 / 1→0 do contador —
 * a pilha inteira precisa esvaziar pra destravar.
 */
let scrollLockCount = 0
let previousBodyOverflow: string | null = null

export function lockBodyScroll(): void {
  if (scrollLockCount === 0) {
    previousBodyOverflow = document.body.style.overflow
    document.body.style.overflow = "hidden"
  }
  scrollLockCount += 1
}

export function unlockBodyScroll(): void {
  scrollLockCount = Math.max(0, scrollLockCount - 1)
  if (scrollLockCount === 0) {
    document.body.style.overflow = previousBodyOverflow ?? ""
    previousBodyOverflow = null
  }
}
