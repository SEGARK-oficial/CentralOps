/**
 * dialogStack — pilha de Escape (ordem/topo) e trava de scroll com contador
 * (R2-8.5). Testado isoladamente (unidade) e via Modal/Drawer (integração).
 */
import {
  isTopmostDialog,
  lockBodyScroll,
  nextDialogOrder,
  registerOpenDialog,
  unlockBodyScroll,
  unregisterOpenDialog,
} from "@/components/ui/internal/dialogStack"

describe("dialogStack — pilha de ordem/topo (API retrocompatível)", () => {
  it("o segundo diálogo registrado é o topo; ao desregistrar, o primeiro volta a ser topo", () => {
    const orderA = nextDialogOrder()
    const orderB = nextDialogOrder()
    registerOpenDialog("a", orderA)
    registerOpenDialog("b", orderB)

    expect(isTopmostDialog(orderA)).toBe(false)
    expect(isTopmostDialog(orderB)).toBe(true)

    unregisterOpenDialog("b")
    expect(isTopmostDialog(orderA)).toBe(true)

    unregisterOpenDialog("a")
  })
})

describe("dialogStack — trava de scroll com contador (R2-8.5)", () => {
  afterEach(() => {
    document.body.style.overflow = ""
  })

  it("lock/unlock simples: trava e destrava o body", () => {
    expect(document.body.style.overflow).toBe("")
    lockBodyScroll()
    expect(document.body.style.overflow).toBe("hidden")
    unlockBodyScroll()
    expect(document.body.style.overflow).toBe("")
  })

  it("diálogo aninhado: fechar o de CIMA não destrava o scroll — só quando os DOIS fecham", () => {
    lockBodyScroll() // Modal de baixo abre
    lockBodyScroll() // ConfirmDialog de cima abre
    expect(document.body.style.overflow).toBe("hidden")

    unlockBodyScroll() // ConfirmDialog fecha
    // Bug original: isto zerava o overflow mesmo com o Modal de baixo aberto.
    expect(document.body.style.overflow).toBe("hidden")

    unlockBodyScroll() // Modal fecha
    expect(document.body.style.overflow).toBe("")
  })

  it("não destrava abaixo de zero (chamadas de unlock a mais não quebram o contador)", () => {
    lockBodyScroll()
    unlockBodyScroll()
    unlockBodyScroll()
    unlockBodyScroll()
    expect(document.body.style.overflow).toBe("")

    // Um lock novo depois disso ainda trava normalmente.
    lockBodyScroll()
    expect(document.body.style.overflow).toBe("hidden")
    unlockBodyScroll()
    expect(document.body.style.overflow).toBe("")
  })
})
