import "@testing-library/jest-dom/vitest"

class ResizeObserverMock {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
})

Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  value: ResizeObserverMock,
})

// jsdom não implementa scrollIntoView; vários componentes o usam ao focar/rolar.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}

// O i18n nunca era inicializado aqui: `useTranslation` devolvia a chave crua (ou
// o defaultValue em inglês embutido em alguns componentes), então toda asserção
// em português falhava. Fixar "pt" também tira o teste da mão do detector, que
// lia navigator.language do jsdom ("en-US") e deixava o resultado dependente do
// ambiente.
import i18n from "@/i18n"
await i18n.changeLanguage("pt")
