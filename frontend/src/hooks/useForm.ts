"use client"

import type React from "react"

/**
 * useForm Hook
 * Hook para gerenciamento de formulários com validação
 */

import { useCallback, useEffect, useRef, useState } from "react"

interface UseFormOptions<T> {
  initialValues: T
  validate?: (values: T) => Partial<Record<keyof T, string>>
  onSubmit: (values: T) => Promise<void> | void
}

interface UseFormReturn<T> {
  values: T
  errors: Partial<Record<keyof T, string>>
  touched: Partial<Record<keyof T, boolean>>
  isSubmitting: boolean
  /** Erro do `onSubmit` (rejeitado) — antes só ia pro `console.error` e
   *  sumia; a maioria dos chamadores já trata o próprio erro (try/catch
   *  local com `setFeedback`), mas isto é a rede de segurança para quem
   *  não trata: sem isto, um erro inesperado deixava o usuário olhando pro
   *  formulário sem nenhum sinal do que aconteceu. */
  submitError: string | null
  handleChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => void
  handleBlur: (event: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => void
  handleSubmit: (event: React.FormEvent) => void
  setFieldValue: (field: keyof T, value: any) => void
  setFieldError: (field: keyof T, error: string) => void
  resetForm: () => void
}

export function useForm<T extends Record<string, any>>({
  initialValues,
  validate,
  onSubmit,
}: UseFormOptions<T>): UseFormReturn<T> {
  const initialValuesRef = useRef(initialValues)
  const [values, setValues] = useState<T>(initialValues)
  const [errors, setErrors] = useState<Partial<Record<keyof T, string>>>({})
  const [touched, setTouched] = useState<Partial<Record<keyof T, boolean>>>({})
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  useEffect(() => {
    initialValuesRef.current = initialValues
  }, [initialValues])

  const handleChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
      const { name, value, type } = event.target
      const fieldName = name as keyof T

      let fieldValue: any = value

      // Handle different input types
      if (type === "checkbox") {
        fieldValue = (event.target as HTMLInputElement).checked
      } else if (type === "number") {
        fieldValue = value === "" ? "" : Number(value)
      }

      setValues((prev) => ({
        ...prev,
        [fieldName]: fieldValue,
      }))

      // Clear error when user starts typing
      if (errors[fieldName]) {
        setErrors((prev) => ({
          ...prev,
          [fieldName]: undefined,
        }))
      }
    },
    [errors],
  )

  const handleBlur = useCallback(
    (event: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
      const { name } = event.target
      const fieldName = name as keyof T

      setTouched((prev) => ({
        ...prev,
        [fieldName]: true,
      }))

      // Validate field on blur
      if (validate) {
        const fieldErrors = validate(values)
        if (fieldErrors[fieldName]) {
          setErrors((prev) => ({
            ...prev,
            [fieldName]: fieldErrors[fieldName],
          }))
        }
      }
    },
    [values, validate],
  )

  const handleSubmit = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault()

      if (isSubmitting) return

      setSubmitError(null)

      // Mark all fields as touched
      const allTouched = Object.keys(values).reduce(
        (acc, key) => ({
          ...acc,
          [key]: true,
        }),
        {} as Partial<Record<keyof T, boolean>>,
      )

      setTouched(allTouched)

      // Validate all fields
      let formErrors: Partial<Record<keyof T, string>> = {}
      if (validate) {
        formErrors = validate(values)
        setErrors(formErrors)
      }

      // Check if form has errors
      const hasErrors = Object.values(formErrors).some((error) => error)
      if (hasErrors) return

      setIsSubmitting(true)

      try {
        await onSubmit(values)
      } catch (error) {
        console.error("Form submission error:", error)
        setSubmitError(error instanceof Error ? error.message : String(error))
      } finally {
        setIsSubmitting(false)
      }
    },
    [values, validate, onSubmit, isSubmitting],
  )

  const setFieldValue = useCallback((field: keyof T, value: any) => {
    setValues((prev) => ({
      ...prev,
      [field]: value,
    }))
  }, [])

  const setFieldError = useCallback((field: keyof T, error: string) => {
    setErrors((prev) => ({
      ...prev,
      [field]: error,
    }))
  }, [])

  const resetForm = useCallback(() => {
    setValues(initialValuesRef.current)
    setErrors({})
    setTouched({})
    setIsSubmitting(false)
    setSubmitError(null)
  }, [])

  return {
    values,
    errors,
    touched,
    isSubmitting,
    submitError,
    handleChange,
    handleBlur,
    handleSubmit,
    setFieldValue,
    setFieldError,
    resetForm,
  }
}
