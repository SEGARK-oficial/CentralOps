/**
 * portal-positioning.ts
 *
 * Utilitário para calcular posição de portais flutuantes (dropdowns, pickers)
 * a partir do bounding rect do elemento trigger.
 *
 * Usa `position: fixed` para escapar de qualquer ancestral com overflow clipping.
 */

export interface PortalRect {
  top: number
  left: number
  width: number
  /** true quando o popup foi invertido para cima (viewport sem espaço abaixo) */
  flipped: boolean
}

/**
 * Calcula top/left/width para um portal flutuante alinhado ao `triggerEl`.
 *
 * @param triggerEl  - O elemento trigger (button, input, etc.)
 * @param dropdownHeight - Altura estimada do popup (usado para checar se cabe abaixo).
 *                         Passe 0 se não souber; o flip não ocorrerá.
 * @param gap - Espaço em pixels entre trigger e popup (default 4).
 */
export function getPortalPosition(
  triggerEl: HTMLElement,
  dropdownHeight = 0,
  gap = 4,
): PortalRect {
  const rect = triggerEl.getBoundingClientRect()
  const viewportHeight = window.innerHeight

  const spaceBelow = viewportHeight - rect.bottom
  const flipped = dropdownHeight > 0 && spaceBelow < dropdownHeight && rect.top > spaceBelow

  const top = flipped
    ? rect.top - dropdownHeight - gap
    : rect.bottom + gap

  return {
    top,
    left: rect.left,
    width: rect.width,
    flipped,
  }
}
