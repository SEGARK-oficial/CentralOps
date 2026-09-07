import type React from "react"
import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { FileUpIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import {
  approxBytes,
  buildRows,
  parseCsv,
  type BuildResult,
  type ParsedCsv,
} from "./csv"

/**
 * Importa o conteúdo de uma tabela a partir de CSV, com pré-visualização.
 *
 * A entrada anterior era um `{chave: {campo: valor}}` colado numa textarea, e
 * CMDB, plano de endereçamento e allowlist nascem em planilha. A conversão
 * acontecia fora do produto, à mão, e o erro só aparecia depois de publicar —
 * como "N linhas inválidas descartadas", sem dizer quais.
 *
 * Três coisas que a tela mostra ANTES de publicar, porque depois é tarde:
 *
 * 1. **Quais linhas estão erradas**, com o número da linha do arquivo e o
 *    motivo. Contagem sem identificação não permite corrigir a planilha.
 * 2. **O diff contra a versão vigente.** Publicar substitui a versão inteira;
 *    "12 novas, 2 removidas" é como se percebe que o arquivo exportado é o
 *    errado antes de a regra parar de casar em produção.
 * 3. **O tamanho contra o teto por tabela**, porque estourar é recusa no
 *    servidor e o operador descobriria só no envio.
 */

interface Props {
  matchMode: "exact" | "cidr"
  /** Corpo da versão vigente, para o diff. Vazio na primeira publicação. */
  currentRows?: Record<string, Record<string, unknown>>
  /** Teto por tabela vindo da configuração da instalação. */
  maxBytes?: number
  /** Entrega o corpo pronto ao formulário que publica. */
  onChange: (rows: Record<string, Record<string, string>> | null) => void
}

const DEFAULT_MAX_BYTES = 32 * 1024 * 1024
const PREVIEW_LIMIT = 8

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`
  return `${(n / (1024 * 1024)).toFixed(1)} MiB`
}

export const CsvImportPanel: React.FC<Props> = ({
  matchMode,
  currentRows = {},
  maxBytes = DEFAULT_MAX_BYTES,
  onChange,
}) => {
  const { t } = useTranslation("enrichment")
  const fileRef = useRef<HTMLInputElement>(null)

  const [text, setText] = useState("")
  const [fileName, setFileName] = useState<string | null>(null)
  const [keyColumn, setKeyColumn] = useState("")
  const [valueColumns, setValueColumns] = useState<string[]>([])
  const [onlyProblems, setOnlyProblems] = useState(false)

  const parsed: ParsedCsv | null = useMemo(() => {
    if (!text.trim()) return null
    return parseCsv(text)
  }, [text])

  /** Reconcilia a seleção quando o arquivo troca e as colunas mudam. */
  function adopt(next: ParsedCsv) {
    const headers = next.headers
    const key = headers.includes(keyColumn) ? keyColumn : (headers[0] ?? "")
    setKeyColumn(key)
    // Todas as colunas menos a chave, por padrão: é o que o operador quer na
    // esmagadora maioria dos casos, e desmarcar é mais rápido que marcar sete.
    setValueColumns(headers.filter((h) => h !== key))
  }

  function handleText(value: string) {
    setText(value)
    if (!value.trim()) {
      setKeyColumn("")
      setValueColumns([])
      onChange(null)
      return
    }
    adopt(parseCsv(value))
  }

  async function handleFile(file: File) {
    const content = await file.text()
    setFileName(file.name)
    handleText(content)
  }

  const result: BuildResult | null = useMemo(() => {
    if (!parsed || !keyColumn) return null
    return buildRows(parsed, keyColumn, valueColumns, matchMode, currentRows)
  }, [parsed, keyColumn, valueColumns, matchMode, currentRows])

  const bytes = useMemo(() => (result ? approxBytes(result.rows) : 0), [result])
  const overLimit = bytes > maxBytes

  // Entrega ao pai a cada mudança de resultado. Sem isto o botão de publicar
  // ficaria com o corpo de uma seleção anterior.
  //
  // Em EFEITO, não durante a renderização: chamar o `setState` do pai enquanto
  // o filho renderiza é a origem do aviso "Cannot update a component while
  // rendering a different component" e, no React 18, de uma renderização a mais
  // por tecla digitada. A primeira versão deste componente fazia exatamente
  // isso.
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  useEffect(() => {
    onChangeRef.current(result && !overLimit ? result.rows : null)
    // `result` é memoizado sobre as entradas reais; depender dele evita
    // recalcular a assinatura à mão e sair de sincronia com o memo.
  }, [result, overLimit])

  const issuesByLine = useMemo(() => {
    const map = new Map<number, string>()
    for (const i of result?.issues ?? []) map.set(i.line, i.reason)
    return map
  }, [result])

  const previewRows = useMemo(() => {
    if (!parsed) return []
    const withLine = parsed.rows.map((row, idx) => ({ row, line: idx + 2 }))
    const filtered = onlyProblems
      ? withLine.filter(({ line, row }) => {
          if (issuesByLine.has(line)) return true
          const key = (row[keyColumn] ?? "").trim()
          return result?.changed.includes(key) || result?.added.includes(key)
        })
      : withLine
    return filtered.slice(0, PREVIEW_LIMIT)
  }, [parsed, onlyProblems, issuesByLine, keyColumn, result])

  return (
    <div className="space-y-3" data-testid="csv-import">
      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={fileRef}
          type="file"
          accept=".csv,.tsv,.txt,text/csv"
          className="hidden"
          data-testid="csv-file-input"
          onChange={(e) => {
            const file = e.target.files?.[0]
            if (file) void handleFile(file)
          }}
        />
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => fileRef.current?.click()}
          leftIcon={<FileUpIcon size={14} />}
        >
          {t("tables.csv.choose")}
        </Button>
        {fileName && (
          <span className="text-xs text-muted">
            {fileName}
            {parsed ? ` · ${t("tables.csv.lines", { count: parsed.rows.length })}` : ""}
            {parsed ? ` · ${t("tables.csv.delimiter", { value: parsed.delimiter === "\t" ? "tab" : parsed.delimiter })}` : ""}
          </span>
        )}
      </div>

      <Textarea
        label={t("tables.csv.pasteLabel")}
        value={text}
        onChange={(e) => handleText(e.target.value)}
        rows={5}
        className="font-mono text-xs"
        placeholder={
          matchMode === "cidr"
            ? "cidr;site;criticidade\n10.0.0.0/16;matriz;media"
            : "chave;dono\nservidor-01;infra"
        }
        helperText={t("tables.csv.pasteHint")}
      />

      {parsed && parsed.headers.length > 0 && (
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <Select
              label={t("tables.csv.keyColumn")}
              value={keyColumn}
              onValueChange={(v) => {
                const next = String(v)
                setKeyColumn(next)
                setValueColumns((prev) => prev.filter((c) => c !== next))
              }}
              options={parsed.headers.map((h) => ({ value: h, label: h }))}
              size="sm"
              helperText={
                matchMode === "cidr"
                  ? t("tables.csv.keyColumnHintCidr")
                  : t("tables.csv.keyColumnHintExact")
              }
            />
            <div>
              <span className="mb-1.5 block text-sm font-medium text-text">
                {t("tables.csv.valueColumns")}
              </span>
              <div className="flex flex-wrap gap-2">
                {parsed.headers
                  .filter((h) => h !== keyColumn)
                  .map((h) => {
                    const on = valueColumns.includes(h)
                    return (
                      <label key={h} className="flex items-center gap-1.5 text-xs">
                        <input
                          type="checkbox"
                          checked={on}
                          onChange={(e) =>
                            setValueColumns((prev) =>
                              e.target.checked
                                ? [...prev, h]
                                : prev.filter((c) => c !== h),
                            )
                          }
                        />
                        <span className={on ? "text-text" : "text-muted"}>{h}</span>
                      </label>
                    )
                  })}
              </div>
              <p className="mt-1 text-xs text-muted">{t("tables.csv.valueColumnsHint")}</p>
            </div>
          </div>

          {result && (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="success">
                  {t("tables.csv.added", { count: result.added.length })}
                </Badge>
                <Badge variant="warning">
                  {t("tables.csv.changed", { count: result.changed.length })}
                </Badge>
                <Badge variant="danger">
                  {t("tables.csv.removed", { count: result.removed.length })}
                </Badge>
                {result.issues.length > 0 && (
                  <Badge variant="danger">
                    {t("tables.csv.invalid", { count: result.issues.length })}
                  </Badge>
                )}
                <Badge variant={overLimit ? "danger" : "default"}>
                  {fmtBytes(bytes)} / {fmtBytes(maxBytes)}
                </Badge>
                <Button
                  type="button"
                  variant={onlyProblems ? "primary" : "ghost"}
                  size="xs"
                  onClick={() => setOnlyProblems((v) => !v)}
                >
                  {t("tables.csv.onlyProblems")}
                </Button>
              </div>

              {overLimit && (
                <Notice variant="danger" title={t("tables.csv.overLimitTitle")}>
                  {t("tables.csv.overLimitBody")}
                </Notice>
              )}

              {result.removed.length > 0 && (
                // Publicar substitui a versão inteira. Remoção em massa é o
                // sintoma de ter exportado o arquivo errado, e é silenciosa.
                <Notice variant="warning" title={t("tables.csv.removalTitle")}>
                  {t("tables.csv.removalBody", {
                    count: result.removed.length,
                    sample: result.removed.slice(0, 3).join(", "),
                  })}
                </Notice>
              )}

              <div className="overflow-x-auto rounded-md border border-border">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-muted">
                      <th className="p-2 font-medium">#</th>
                      <th className="p-2 font-medium">{keyColumn}</th>
                      {valueColumns.map((c) => (
                        <th key={c} className="p-2 font-medium">
                          {c}
                        </th>
                      ))}
                      <th className="p-2 font-medium">{t("tables.csv.state")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {previewRows.map(({ row, line }) => {
                      const issue = issuesByLine.get(line)
                      const key = (row[keyColumn] ?? "").trim()
                      return (
                        <tr
                          key={line}
                          className="border-b border-border last:border-0"
                          data-testid={`csv-row-${line}`}
                        >
                          <td className="p-2 text-muted">{line}</td>
                          <td
                            className={`p-2 font-mono ${issue ? "text-danger-500" : ""}`}
                          >
                            {key || "—"}
                          </td>
                          {valueColumns.map((c) => (
                            <td key={c} className="p-2">
                              {row[c]}
                            </td>
                          ))}
                          <td className="p-2">
                            {issue ? (
                              <Badge variant="danger">
                                {t(`tables.csv.issue.${issue}`)}
                              </Badge>
                            ) : result.added.includes(key) ? (
                              <Badge variant="success">{t("tables.csv.new")}</Badge>
                            ) : result.changed.includes(key) ? (
                              <Badge variant="warning">{t("tables.csv.updated")}</Badge>
                            ) : (
                              <Badge variant="default">{t("tables.csv.same")}</Badge>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              {parsed.rows.length > previewRows.length && (
                <p className="text-xs text-muted">
                  {t("tables.csv.more", {
                    count: parsed.rows.length - previewRows.length,
                  })}
                </p>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

export default CsvImportPanel
