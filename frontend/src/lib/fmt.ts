/**
 * fmt — formatadores compartilhados de métricas para a UI.
 *
 * Centraliza a formatação para que a MESMA métrica tenha o MESMO formato em
 * toda a aplicação (ex.: EPS na topologia de roteamento e na lista de destinos).
 */

/**
 * Formata uma taxa (eventos/min, EPS, …) de forma compacta.
 *
 * Regras (estáveis — usadas por FlowCanvas, RoutingTopology e DestinationsPage):
 * - valores não-finitos ou <= 0  → "0"        (sem tráfego)
 * - >= 10000                      → "{n/1000}k" sem casas decimais (ex.: 12000 → "12k")
 * - >= 1000                       → "{n/1000}k" com 1 casa decimal     (ex.: 1500 → "1.5k")
 * - >= 100                        → inteiro arredondado                (ex.: 120 → "120")
 * - >= 10                         → inteiro                            (ex.: 42 → "42")
 * - >= 1                          → 1 casa decimal                     (ex.: 2.5 → "2.5")
 * - >= 0.01                       → 2 casas decimais                   (ex.: 0.04 → "0.04")
 * - 0 < n < 0.01                  → "<0.01"     (tráfego real, baixo demais p/ mostrar)
 *
 * O piso `<0.01` distingue tráfego BAIXO de tráfego NULO: antes, taxas pequenas
 * (ex.: 0.04 EPS) eram arredondadas para "0.0" e "sumiam" como se fossem zero.
 */
export function fmtRate(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0"
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`
  if (n >= 100) return String(Math.round(n))
  if (n >= 10) return n.toFixed(0)
  if (n >= 1) return n.toFixed(1)
  if (n >= 0.01) return n.toFixed(2)
  return "<0.01"
}
