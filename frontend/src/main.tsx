import React from "react"
import ReactDOM from "react-dom/client"
import "./styles/globals.css"
import "./i18n" // initialize react-i18next (locale detection + catalogs) before render
import App from "./App"
import { ThemeProvider } from "./contexts/ThemeContext"

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </React.StrictMode>,
)
