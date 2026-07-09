/**
 * PiiRuleEditor — editor estruturado para regras de redação de PII.
 *
 * Espelha RouteConditionEditor: cada regra tem path + Select de action
 * + campos condicionais (mask_char, fixed_len, salt, keep_prefix,
 * keep_suffix, octets).
 *
 * Modos:
 *   structured — UI path+action (default)
 *   json       — textarea JSON avançado (toggle)
 *
 * Valida on-blur do JSON com preview do resultado parseado.
 * Produz PiiRedactionRule[] normalizado para o form pai.
 *
 * A11y: fieldset/legend, aria-label em botões de remoção, labels
 * explícitos em todos os inputs.
 */

import type React from "react"
import { useState, useCallback, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { PlusIcon, Trash2Icon, CodeIcon, LayoutListIcon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Notice } from "@/components/ui/Notice/Notice"
import type { PiiRedactionRule, PiiRedactionAction } from "@/types"

const EMPTY_RULE: PiiRedactionRule = { path: "", action: "mask" }

// ── Helpers ───────────────────────────────────────────────────────────────

function rulesToJson(rules: PiiRedactionRule[]): string {
  return JSON.stringify(rules, null, 2)
}

function parseJson(text: string): PiiRedactionRule[] | null {
  try {
    const parsed: unknown = JSON.parse(text)
    if (Array.isArray(parsed)) return parsed as PiiRedactionRule[]
    // Aceita também o formato spec {version, rules}
    if (parsed && typeof parsed === "object" && "rules" in parsed && Array.isArray((parsed as { rules: unknown }).rules)) {
      return (parsed as { rules: PiiRedactionRule[] }).rules
    }
    return null
  } catch {
    return null
  }
}

// ── Componente ────────────────────────────────────────────────────────────

interface PiiRuleEditorProps {
  rules: PiiRedactionRule[]
  onChange: (rules: PiiRedactionRule[]) => void
  disabled?: boolean
}

export const PiiRuleEditor: React.FC<PiiRuleEditorProps> = ({ rules, onChange, disabled }) => {
  const { t } = useTranslation("routing")
  const [mode, setMode] = useState<"structured" | "json">("structured")
  const [jsonText, setJsonText] = useState(() => rulesToJson(rules))
  const [jsonError, setJsonError] = useState<string | null>(null)

  const ACTION_OPTIONS: { value: PiiRedactionAction; label: string; description: string }[] = useMemo(
    () => [
      { value: "mask", label: t("piiRuleEditor.actions.mask.label"), description: t("piiRuleEditor.actions.mask.description") },
      { value: "hash", label: t("piiRuleEditor.actions.hash.label"), description: t("piiRuleEditor.actions.hash.description") },
      { value: "partial", label: t("piiRuleEditor.actions.partial.label"), description: t("piiRuleEditor.actions.partial.description") },
      { value: "drop_field", label: t("piiRuleEditor.actions.drop_field.label"), description: t("piiRuleEditor.actions.drop_field.description") },
    ],
    [t],
  )

  // Sincroniza ao alternar para JSON
  const switchToJson = useCallback(() => {
    setJsonText(rulesToJson(rules))
    setJsonError(null)
    setMode("json")
  }, [rules])

  // Sincroniza ao alternar para estruturado
  const switchToStructured = useCallback(() => {
    const parsed = parseJson(jsonText)
    if (parsed === null) {
      setJsonError(t("piiRuleEditor.jsonInvalidSwitch"))
      return
    }
    onChange(parsed)
    setJsonError(null)
    setMode("structured")
  }, [jsonText, onChange, t])

  const handleJsonBlur = useCallback(() => {
    if (jsonText.trim() === "" || jsonText.trim() === "[]") {
      setJsonError(null)
      onChange([])
      return
    }
    const parsed = parseJson(jsonText)
    if (parsed === null) {
      setJsonError(t("piiRuleEditor.jsonInvalid"))
    } else {
      setJsonError(null)
      onChange(parsed)
    }
  }, [jsonText, onChange, t])

  const setRule = (i: number, patch: Partial<PiiRedactionRule>) =>
    onChange(rules.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))

  const addRule = () => onChange([...rules, { ...EMPTY_RULE }])

  const removeRule = (i: number) => onChange(rules.filter((_, idx) => idx !== i))

  return (
    <div className="space-y-3">
      {/* Alternador de modo */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-text-secondary">
          {rules.length === 0
            ? t("piiRuleEditor.noRules")
            : t("piiRuleEditor.rulesConfigured", { count: rules.length })}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={mode === "structured" ? switchToJson : switchToStructured}
          disabled={disabled}
          leftIcon={mode === "structured" ? <CodeIcon size={14} /> : <LayoutListIcon size={14} />}
          aria-label={mode === "structured" ? t("piiRuleEditor.switchToJsonAria") : t("piiRuleEditor.switchToStructuredAria")}
        >
          {mode === "structured" ? t("piiRuleEditor.switchToJson") : t("piiRuleEditor.switchToStructured")}
        </Button>
      </div>

      {/* Modo estruturado */}
      {mode === "structured" && (
        <div className="space-y-2">
          {rules.length === 0 && (
            <p className="text-xs text-text-secondary">
              {t("piiRuleEditor.structuredHintPrefix")}<code className="font-mono">raw.user.email</code>{t("piiRuleEditor.structuredHintMiddle")}<code className="font-mono">normalized.src_ip</code>{t("piiRuleEditor.structuredHintSuffix")}
            </p>
          )}
          {rules.map((rule, i) => (
            <fieldset
              key={i}
              className="space-y-2 rounded-md border border-border p-3"
              aria-label={t("piiRuleEditor.ruleFieldsetAria", { index: i + 1 })}
            >
              <div className="flex flex-wrap items-end gap-2">
                <div className="min-w-[200px] flex-1">
                  <Input
                    label={i === 0 ? t("piiRuleEditor.fieldPathLabel") : undefined}
                    value={rule.path}
                    placeholder={t("piiRuleEditor.fieldPathPlaceholder")}
                    disabled={disabled}
                    onChange={(e) => setRule(i, { path: e.target.value })}
                    aria-label={t("piiRuleEditor.fieldPathAria", { index: i + 1 })}
                  />
                </div>
                <div className="w-44">
                  <Select
                    label={i === 0 ? t("piiRuleEditor.actionLabel") : undefined}
                    value={rule.action}
                    options={ACTION_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
                    disabled={disabled}
                    onValueChange={(v) =>
                      setRule(i, {
                        action: v as PiiRedactionAction,
                        // Limpa campos condicionais ao trocar ação
                        fixed_len: undefined,
                        mask_char: undefined,
                        salt: undefined,
                        keep_prefix: undefined,
                        keep_suffix: undefined,
                        octets: undefined,
                      })
                    }
                    aria-label={t("piiRuleEditor.actionAria", { index: i + 1 })}
                  />
                </div>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => removeRule(i)}
                  disabled={disabled}
                  leftIcon={<Trash2Icon size={14} />}
                  aria-label={t("piiRuleEditor.removeRuleAria", { index: i + 1, path: rule.path || t("piiRuleEditor.removeRuleNoPath") })}
                />
              </div>

              {/* Campos condicionais por ação */}
              {rule.action === "mask" && (
                <div className="flex flex-wrap gap-2">
                  <div className="w-32">
                    <Input
                      label={t("piiRuleEditor.maskCharLabel")}
                      value={rule.mask_char ?? ""}
                      placeholder="*"
                      maxLength={1}
                      disabled={disabled}
                      onChange={(e) => setRule(i, { mask_char: e.target.value || undefined })}
                      aria-label={t("piiRuleEditor.maskCharAria")}
                    />
                  </div>
                  <div className="w-32">
                    <Input
                      label={t("piiRuleEditor.fixedLenLabel")}
                      type="number"
                      min={0}
                      value={rule.fixed_len !== undefined ? String(rule.fixed_len) : ""}
                      placeholder={t("piiRuleEditor.fixedLenPlaceholder")}
                      disabled={disabled}
                      onChange={(e) =>
                        setRule(i, { fixed_len: e.target.value ? Number(e.target.value) : undefined })
                      }
                      aria-label={t("piiRuleEditor.fixedLenAria")}
                    />
                  </div>
                </div>
              )}

              {rule.action === "hash" && (
                <div className="w-64">
                  <Input
                    label={t("piiRuleEditor.saltLabel")}
                    value={rule.salt ?? ""}
                    placeholder={t("piiRuleEditor.saltPlaceholder")}
                    disabled={disabled}
                    onChange={(e) => setRule(i, { salt: e.target.value || undefined })}
                    aria-label={t("piiRuleEditor.saltAria")}
                  />
                </div>
              )}

              {rule.action === "partial" && (
                <div className="flex flex-wrap gap-2">
                  <div className="w-32">
                    <Input
                      label={t("piiRuleEditor.keepPrefixLabel")}
                      type="number"
                      min={0}
                      value={rule.keep_prefix !== undefined ? String(rule.keep_prefix) : ""}
                      placeholder="0"
                      disabled={disabled}
                      onChange={(e) =>
                        setRule(i, { keep_prefix: e.target.value ? Number(e.target.value) : undefined })
                      }
                      aria-label={t("piiRuleEditor.keepPrefixAria")}
                    />
                  </div>
                  <div className="w-32">
                    <Input
                      label={t("piiRuleEditor.keepSuffixLabel")}
                      type="number"
                      min={0}
                      value={rule.keep_suffix !== undefined ? String(rule.keep_suffix) : ""}
                      placeholder="0"
                      disabled={disabled}
                      onChange={(e) =>
                        setRule(i, { keep_suffix: e.target.value ? Number(e.target.value) : undefined })
                      }
                      aria-label={t("piiRuleEditor.keepSuffixAria")}
                    />
                  </div>
                  <div className="w-32">
                    <Input
                      label={t("piiRuleEditor.octetsLabel")}
                      type="number"
                      min={0}
                      max={4}
                      value={rule.octets !== undefined ? String(rule.octets) : ""}
                      placeholder={t("piiRuleEditor.octetsPlaceholder")}
                      disabled={disabled}
                      onChange={(e) =>
                        setRule(i, { octets: e.target.value ? Number(e.target.value) : undefined })
                      }
                      aria-label={t("piiRuleEditor.octetsAria")}
                    />
                  </div>
                </div>
              )}

              {/* Preview da regra */}
              {rule.path && (
                <p className="text-xs text-text-tertiary">
                  {t("piiRuleEditor.resultPreview")}{" "}
                  <code className="font-mono">
                    {rule.path} → {rule.action}
                    {rule.action === "partial" && rule.keep_prefix != null && ` ${t("piiRuleEditor.resultPrefixSuffix", { value: rule.keep_prefix })}`}
                    {rule.action === "partial" && rule.keep_suffix != null && ` ${t("piiRuleEditor.resultSuffixSuffix", { value: rule.keep_suffix })}`}
                    {rule.action === "mask" && rule.mask_char && ` ${t("piiRuleEditor.resultMaskChar", { value: rule.mask_char })}`}
                    {rule.action === "hash" && rule.salt && ` ${t("piiRuleEditor.resultSalt")}`}
                  </code>
                </p>
              )}
            </fieldset>
          ))}

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={addRule}
            disabled={disabled}
            leftIcon={<PlusIcon size={14} />}
          >
            {t("piiRuleEditor.addRule")}
          </Button>
        </div>
      )}

      {/* Modo JSON avançado */}
      {mode === "json" && (
        <div className="space-y-2">
          <textarea
            className="w-full rounded-md border border-border bg-surface p-2 font-mono text-xs text-text focus-ring"
            rows={8}
            spellCheck={false}
            placeholder={t("piiRuleEditor.jsonPlaceholder")}
            value={jsonText}
            disabled={disabled}
            onChange={(e) => setJsonText(e.target.value)}
            onBlur={handleJsonBlur}
            aria-label={t("piiRuleEditor.jsonTextareaAria")}
          />
          {jsonError && (
            <Notice variant="danger" title={t("piiRuleEditor.jsonInvalidTitle")}>
              {jsonError}
            </Notice>
          )}
          {!jsonError && jsonText.trim() && (
            <p className="text-xs text-text-secondary">
              {t("piiRuleEditor.jsonValid", { count: rules.length })}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
