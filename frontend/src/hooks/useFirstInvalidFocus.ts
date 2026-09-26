import { useCallback, useRef, useState } from "react"

/**
 * R2-8.3 — generaliza o padrão "failField" que o `NewUserModal` já usava
 * (A11Y-26): antes disto, cada form com `noValidate` reinventava o próprio
 * `useRef` por campo + `errorField` state + `focus()` manual no `catch`, e a
 * maioria dos forms REPETIA só a metade fácil (banner genérico de erro) e
 * pulava a metade que importa para teclado/leitor de tela — apontar qual
 * campo falhou (`aria-invalid`/`aria-describedby`) e mover o foco até ele.
 * Sem isso, o operador que usa teclado/leitor de tela lê "algo deu errado" no
 * topo do form e precisa caçar manualmente qual dos N campos é o culpado.
 *
 * Diferença do `NewUserModal` original: em vez de um `useRef` NOMEADO por
 * campo (não escala para os campos DINÂMICOS do `IntegrationForm`, cuja
 * chave só existe em runtime — `auth_fields[i].key`), os elementos entram
 * num `Map` via `registerField(key)`, o mesmo padrão de `ref` por chave já
 * usado no `Select` (R2-8.4, `optionButtonRefs`).
 */

export interface UseFirstInvalidFocusResult<TField extends string = string> {
  /** Mensagem do banner genérico (topo do form). `null` quando não há erro. */
  error: string | null
  /** Qual campo causou o erro atual — `null` quando o erro não é de um campo
   *  específico (ex.: falha de rede no submit). Liga `aria-invalid`. */
  errorField: TField | null
  /**
   * Ref callback a passar em `ref={registerField("nome_do_campo")}` no
   * `Input`/`Select`/elemento nativo — registra o nó DOM para poder focá-lo
   * depois. Funciona com qualquer alvo que aceite `ref` (a maioria dos
   * primitivos do design system encaminha para o elemento nativo).
   */
  registerField: (field: TField) => (el: HTMLElement | null) => void
  /** Marca o erro, aponta o campo culpado e foca nele — validação local. */
  failField: (field: TField, message: string) => void
  /** Marca o erro SEM campo específico — ex.: erro do backend no `catch`. */
  failGeneral: (message: string) => void
  /** Limpa erro/campo — chamar no início do `handleSubmit`. */
  clearError: () => void
  /**
   * Props prontas para espalhar num campo controlado: `aria-invalid` +
   * `aria-describedby` apontando para o banner (`errorId`), só quando ESTE
   * campo é o culpado do erro atual.
   */
  fieldErrorProps: (field: TField, errorId: string) => { "aria-invalid"?: "true"; "aria-describedby"?: string }
}

export function useFirstInvalidFocus<TField extends string = string>(): UseFirstInvalidFocusResult<TField> {
  const [error, setError] = useState<string | null>(null)
  const [errorField, setErrorField] = useState<TField | null>(null)
  const fieldEls = useRef(new Map<TField, HTMLElement>())

  const registerField = useCallback(
    (field: TField) => (el: HTMLElement | null) => {
      if (el) fieldEls.current.set(field, el)
      else fieldEls.current.delete(field)
    },
    [],
  )

  const failField = useCallback((field: TField, message: string) => {
    setError(message)
    setErrorField(field)
    fieldEls.current.get(field)?.focus()
  }, [])

  const failGeneral = useCallback((message: string) => {
    setError(message)
    setErrorField(null)
  }, [])

  const clearError = useCallback(() => {
    setError(null)
    setErrorField(null)
  }, [])

  const fieldErrorProps = useCallback(
    (field: TField, errorId: string): { "aria-invalid"?: "true"; "aria-describedby"?: string } =>
      errorField === field ? { "aria-invalid": "true", "aria-describedby": errorId } : {},
    [errorField],
  )

  return { error, errorField, registerField, failField, failGeneral, clearError, fieldErrorProps }
}

export default useFirstInvalidFocus
