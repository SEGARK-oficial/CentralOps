/**
 * SyslogSourcesPanel — fontes do receptor syslog nativo, por integração push.
 *
 * Auto-oculta para fontes pull (mesmo critério do IngestSourcePanel: 422 em
 * getIngestInfo). Cadastra QUEM pode falar syslog conosco (CIDR de origem,
 * porta/transporte) e PARA QUAL stream, por conteúdo (classificador com
 * detectores de fábrica ou JMESPath). Traz um testador de linha: cola o que o
 * aparelho manda e vê o stream escolhido antes de salvar.
 */
import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { PlusIcon, RadioIcon, Trash2Icon, FlaskConicalIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { SyslogClassifierRule, SyslogSource } from "@/services/api"
import { Card } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import { Select } from "@/components/ui/Select/Select"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { useFirstInvalidFocus } from "@/hooks/useFirstInvalidFocus"

interface Props {
  integrationId: number
  canManage?: boolean
}

const CIDR_RE = /^[0-9a-fA-F:.]+\/\d{1,3}$|^[0-9.]+$|^[0-9a-fA-F:]+$/

export const SyslogSourcesPanel: React.FC<Props> = ({ integrationId, canManage = true }) => {
  const { t } = useTranslation("integrations")
  const [hidden, setHidden] = useState(false)
  const [streams, setStreams] = useState<string[]>([])
  const [sources, setSources] = useState<SyslogSource[]>([])
  const errorId = "syslog-sources-error"
  // R2-8.3: o botão "Adicionar" já fica desabilitado enquanto o form é
  // inválido (guarda a prova via helper text sempre visível em cada campo —
  // uma alternativa válida ao banner pós-submit). O que faltava mesmo era o
  // banner de erro do SERVIDOR (criar/excluir/testar) não ter `role`/`live`
  // nenhum — sumia sem anúncio nenhum para leitor de tela. O hook também cobre
  // o caso defensivo de Enter disparar o submit nativo do form mesmo com o
  // botão desabilitado (alguns navegadores permitem).
  const { error, errorField, registerField, failField, failGeneral, clearError } = useFirstInvalidFocus<
    "name" | "cidr" | "defaultStream"
  >()
  const [notice, setNotice] = useState<string | null>(null)

  const [name, setName] = useState("")
  const [cidr, setCidr] = useState("")
  const [port, setPort] = useState("")
  const [transport, setTransport] = useState<SyslogSource["transport"]>("any")
  const [defaultStream, setDefaultStream] = useState("")
  const [rules, setRules] = useState<SyslogClassifierRule[]>([])
  const [creating, setCreating] = useState(false)

  const [testLine, setTestLine] = useState("")
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<api.SyslogClassifyTest | null>(null)

  const reload = useCallback(async () => {
    try {
      const list = await api.listSyslogSources(integrationId)
      setSources(Array.isArray(list) ? list : [])
    } catch (e) {
      failGeneral(e instanceof Error ? e.message : t("syslog.loadError"))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [integrationId, t])

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const info = await api.getIngestInfo(integrationId)
        if (cancelled) return
        if (!info || info.transport !== "push") {
          setHidden(true)
          return
        }
        setStreams(info.streams ?? [])
        setDefaultStream((prev) => prev || info.streams?.[0] || "")
        await reload()
      } catch {
        if (!cancelled) setHidden(true)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [integrationId, reload])

  const cidrValid = CIDR_RE.test(cidr.trim())
  const canCreate = name.trim().length > 0 && cidrValid && defaultStream.length > 0 && rules.every((r) => r.when.trim() && r.stream)

  const handleCreate = useCallback(async () => {
    // Defensivo: o botão fica desabilitado enquanto `!canCreate`, mas o
    // submit NATIVO do form (Enter num campo de texto) pode disparar mesmo
    // assim em alguns navegadores — sem isto o clique silenciosamente não
    // fazia nada e o operador não tinha pista do motivo.
    if (!name.trim()) {
      failField("name", t("syslog.form.nameRequiredError"))
      return
    }
    if (!cidrValid) {
      failField("cidr", t("syslog.form.cidrInvalidError"))
      return
    }
    if (!defaultStream) {
      failField("defaultStream", t("syslog.form.defaultStreamRequiredError"))
      // Select não encaminha ref — foca o trigger pelo id.
      document.getElementById("syslog-default-stream-select")?.focus()
      return
    }
    if (!canCreate) return
    setCreating(true)
    clearError()
    setNotice(null)
    try {
      const created = await api.createSyslogSource({
        integration_id: integrationId,
        name: name.trim(),
        source_cidr: cidr.trim(),
        listen_port: port.trim() ? Number(port) : null,
        transport,
        default_stream: defaultStream,
        classifier: rules.length ? { rules: rules.map((r) => ({ when: r.when.trim(), stream: r.stream })) } : undefined,
      })
      setSources((prev) => [...prev, created])
      setName("")
      setCidr("")
      setPort("")
      setRules([])
      setNotice(t("syslog.form.created", { name: created.name }))
    } catch (e) {
      failGeneral(e instanceof Error ? e.message : t("syslog.form.createError"))
    } finally {
      setCreating(false)
    }
  }, [canCreate, integrationId, name, cidr, cidrValid, port, transport, defaultStream, rules, t, failField, failGeneral, clearError])

  const handleDelete = useCallback(
    async (src: SyslogSource) => {
      if (typeof window !== "undefined" && !window.confirm(t("syslog.form.deleteConfirm", { name: src.name }))) return
      clearError()
      try {
        await api.deleteSyslogSource(src.id)
        setSources((prev) => prev.filter((s) => s.id !== src.id))
      } catch (e) {
        failGeneral(e instanceof Error ? e.message : t("syslog.form.deleteError"))
      }
    },
    [t, clearError, failGeneral],
  )

  const handleTest = useCallback(async () => {
    if (!testLine.trim()) return
    setTesting(true)
    setTestResult(null)
    try {
      const res = await api.testSyslogClassifier({
        line: testLine,
        classifier: rules.length ? { rules } : undefined,
        default_stream: defaultStream || undefined,
      })
      setTestResult(res)
    } catch (e) {
      failGeneral(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }, [testLine, rules, defaultStream, failGeneral])

  if (hidden) return null

  const streamOptions = streams.map((s) => ({ value: s, label: s }))
  const transportOptions = [
    { value: "any", label: t("syslog.form.transportAny") },
    { value: "udp", label: "UDP" },
    { value: "tcp", label: "TCP" },
    { value: "tls", label: "TLS" },
  ]

  return (
    <Card className="space-y-4 p-5" data-testid="syslog-sources">
      <div className="flex items-center gap-3">
        <RadioIcon size={18} className="text-primary-600" aria-hidden />
        <div>
          <h3 className="text-sm font-semibold text-text">{t("syslog.title")}</h3>
          <p className="text-xs text-text-secondary">{t("syslog.help")}</p>
        </div>
      </div>

      {sources.length === 0 ? (
        <Notice variant="warning">{t("syslog.empty")}</Notice>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-left text-text-tertiary">
              <tr>
                <th className="py-1 pr-3">{t("syslog.col.name")}</th>
                <th className="py-1 pr-3">{t("syslog.col.cidr")}</th>
                <th className="py-1 pr-3">{t("syslog.col.port")}</th>
                <th className="py-1 pr-3">{t("syslog.col.transport")}</th>
                <th className="py-1 pr-3">{t("syslog.col.defaultStream")}</th>
                <th className="py-1 pr-3">{t("syslog.col.rules")}</th>
                <th className="py-1 pr-3" />
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id} className="border-t border-border" data-testid={`syslog-source-${s.id}`}>
                  <td className="py-1.5 pr-3 font-medium text-text">{s.name}</td>
                  <td className="py-1.5 pr-3"><code>{s.source_cidr}</code></td>
                  <td className="py-1.5 pr-3">{s.listen_port ?? t("syslog.col.anyPort")}</td>
                  <td className="py-1.5 pr-3 uppercase">{s.transport === "any" ? t("syslog.col.anyPort") : s.transport}</td>
                  <td className="py-1.5 pr-3"><Badge variant="default" size="sm">{s.default_stream}</Badge></td>
                  <td className="py-1.5 pr-3">{s.classifier.rules.length}</td>
                  <td className="py-1.5 text-right">
                    <Badge variant={s.enabled ? "success" : "default"} size="sm">
                      {s.enabled ? t("syslog.col.enabled") : t("syslog.col.disabled")}
                    </Badge>
                    {canManage && (
                      <Button type="button" size="sm" variant="ghost" aria-label={t("syslog.form.removeRule")} onClick={() => void handleDelete(s)}>
                        <Trash2Icon size={14} />
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {canManage && (
        <form
          className="space-y-3 rounded-md border border-border p-3"
          noValidate
          onSubmit={(e) => {
            e.preventDefault()
            void handleCreate()
          }}
        >
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
            <Input
              ref={registerField("name")}
              label={t("syslog.form.name")}
              value={name}
              onChange={(e) => setName(e.target.value)}
              data-testid="syslog-name"
              aria-invalid={errorField === "name" ? "true" : undefined}
              aria-describedby={errorField === "name" ? errorId : undefined}
            />
            <Input
              ref={registerField("cidr")}
              label={t("syslog.form.cidr")}
              placeholder={t("syslog.form.cidrPlaceholder")}
              helperText={t("syslog.form.cidrHelper")}
              value={cidr}
              onChange={(e) => setCidr(e.target.value)}
              data-testid="syslog-cidr"
              aria-invalid={errorField === "cidr" ? "true" : undefined}
              aria-describedby={errorField === "cidr" ? errorId : undefined}
            />
            <Input label={t("syslog.form.port")} helperText={t("syslog.form.portHelper")} value={port} onChange={(e) => setPort(e.target.value)} inputMode="numeric" />
            <Select label={t("syslog.form.transport")} options={transportOptions} value={transport} onChange={(v) => setTransport(String(v) as SyslogSource["transport"])} />
            <Select
              id="syslog-default-stream-select"
              label={t("syslog.form.defaultStream")}
              options={streamOptions}
              value={defaultStream}
              onChange={(v) => setDefaultStream(String(v))}
              data-testid="syslog-default-stream"
              error={errorField === "defaultStream" ? error ?? undefined : undefined}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-text-secondary">{t("syslog.form.rulesTitle")}</span>
              <Button type="button" size="sm" variant="outline" onClick={() => setRules((r) => [...r, { when: "", stream: defaultStream }])} data-testid="syslog-add-rule">
                <PlusIcon size={14} /> {t("syslog.form.addRule")}
              </Button>
            </div>
            <p className="text-xs text-text-tertiary">{t("syslog.form.rulesHelp")}</p>
            {rules.map((r, i) => (
              <div key={i} className="grid gap-2 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
                <Input
                  placeholder={t("syslog.form.whenPlaceholder")}
                  value={r.when}
                  onChange={(e) => setRules((rs) => rs.map((x, j) => (j === i ? { ...x, when: e.target.value } : x)))}
                  aria-label={`when-${i}`}
                />
                <Select options={streamOptions} value={r.stream} onChange={(v) => setRules((rs) => rs.map((x, j) => (j === i ? { ...x, stream: String(v) } : x)))} aria-label={`stream-${i}`} />
                <Button type="button" size="sm" variant="ghost" onClick={() => setRules((rs) => rs.filter((_, j) => j !== i))}>
                  {t("syslog.form.removeRule")}
                </Button>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2">
            <Button type="submit" size="sm" loading={creating} disabled={!canCreate} data-testid="syslog-create">
              <PlusIcon size={14} /> {t("syslog.form.create")}
            </Button>
            {notice && <span className="text-xs text-text-secondary">{notice}</span>}
          </div>
        </form>
      )}

      {/* Testador de linha: vê o stream antes de salvar. */}
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <FlaskConicalIcon size={14} className="text-primary-600" aria-hidden />
          <span className="text-xs font-medium text-text-secondary">{t("syslog.test.title")}</span>
        </div>
        <Textarea rows={2} placeholder={t("syslog.test.placeholder")} value={testLine} onChange={(e) => setTestLine(e.target.value)} data-testid="syslog-test-line" />
        <div className="flex items-center gap-3">
          <Button type="button" size="sm" variant="outline" loading={testing} disabled={!testLine.trim()} onClick={() => void handleTest()} data-testid="syslog-test-run">
            {t("syslog.test.run")}
          </Button>
          {testResult && (
            <span className="text-xs text-text" data-testid="syslog-test-result">
              {t("syslog.test.stream")}: <Badge variant="default" size="sm">{testResult.stream ?? "—"}</Badge>{" "}
              ({testResult.matched_rule ? t("syslog.test.matched") : t("syslog.test.noMatch")}) · {t("syslog.test.format")}:{" "}
              {String(testResult.parsed.format)} · {t("syslog.test.host")}: {String(testResult.parsed.host ?? "—")} · {t("syslog.test.app")}:{" "}
              {String(testResult.parsed.app ?? "—")}
            </span>
          )}
        </div>
      </div>

      {/* R2-8.3: antes era um <p> sem role/aria-live nenhum — erro de
          criar/excluir/testar sumia sem anúncio nenhum pro leitor de tela. */}
      {error && (
        <p id={errorId} className="text-xs text-danger-600" role="alert" aria-live="assertive">
          {error}
        </p>
      )}
    </Card>
  )
}
