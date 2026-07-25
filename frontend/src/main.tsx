import React from "react"
import ReactDOM from "react-dom/client"
import "./styles/globals.css"
import "./i18n" // initialize react-i18next (locale detection + catalogs) before render
import App from "./App"

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
