import React from "react"
import ReactDOM from "react-dom/client"
import "./styles/globals.css"
import { i18nReady } from "./i18n" // locale detection + carrega o catálogo do idioma resolvido
import { BootFailureScreen } from "./i18n/BootFailureScreen"
import App from "./App"

// PERF-01: o catálogo do idioma inicial é baixado sob demanda (não mais
// embutido no chunk de entrada). Renderizar ANTES de `i18nReady` resolver
// piscaria chave crua (`common:actions.save`) até o catálogo chegar.
//
// R2-5.2: `i18nReady` é uma promise de rede (dynamic import do catálogo) —
// sem `.catch()`, uma falha (chunk caiu, offline no 1º load) nunca chamava
// `render()` e a tela ficava em branco pra sempre, sem log nem saída pro
// usuário. `BootFailureScreen` é standalone (sem depender de `t()`/i18n
// pronto) e devolve o controle: recarregar tenta a rede de novo.
void i18nReady
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    )
  })
  .catch((error: unknown) => {
    console.error("i18n boot failed", error)
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <BootFailureScreen />
      </React.StrictMode>,
    )
  })
