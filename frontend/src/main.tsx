import React from "react"
import ReactDOM from "react-dom/client"
import "./styles/globals.css"
import { i18nReady } from "./i18n" // locale detection + carrega o catálogo do idioma resolvido
import App from "./App"

// PERF-01: o catálogo do idioma inicial é baixado sob demanda (não mais
// embutido no chunk de entrada). Renderizar ANTES de `i18nReady` resolver
// piscaria chave crua (`common:actions.save`) até o catálogo chegar.
void i18nReady.then(() => {
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  )
})
