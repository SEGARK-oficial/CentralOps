/**
 * Leitura de CSV para tabelas de enriquecimento.
 *
 * Cada teste aqui corresponde a uma forma de o fluxo falhar SEM ERRO — que é a
 * classe de defeito que esta feature inteira persegue. Uma tabela publicada com
 * a chave errada não dá 422: ela publica com 201 e simplesmente nunca casa, e
 * nos painéis aparece com zero em tudo, indistinguível de "nenhum evento tinha
 * o campo".
 */

import { describe, it, expect } from "vitest"
import {
  approxBytes,
  buildRows,
  detectDelimiter,
  isValidCidrOrIp,
  parseCsv,
  splitLine,
} from "@/components/enrichment/csv"

describe("detectDelimiter", () => {
  it("elege o separador pela consistência, não pela frequência", () => {
    // Arquivo pt-BR: a vírgula aparece MAIS vezes (decimais), mas quem separa
    // colunas é o ponto e vírgula. Contar ocorrências escolheria errado e o
    // arquivo inteiro viraria uma coluna só.
    const csv = [
      "cidr;site;custo",
      "10.0.0.0/16;matriz;1,50",
      "10.0.5.0/24;filial-sp;2,75",
    ].join("\n")
    expect(detectDelimiter(csv)).toBe(";")
  })

  it("prefere o separador consistente mesmo quando o outro produz mais colunas", () => {
    // Este caso é sintético de propósito. O teste acima passa mesmo com a
    // regra de consistência REMOVIDA (descobri por mutação): lá a vírgula é
    // descartada antes, por produzir uma coluna só no cabeçalho. Sem um caso
    // em que as duas candidatas produzem ≥2 colunas, a regra ficaria sem
    // guarda e um refactor a apagaria em silêncio.
    //
    // Com ",": 3 colunas na 1ª linha, 2 na 2ª — inconsistente.
    // Com ";": 2 colunas em ambas — consistente, e é o separador real.
    const csv = "a,b;c,d\n1,2;3"
    expect(detectDelimiter(csv)).toBe(";")
  })

  it("reconhece vírgula, tabulação e barra vertical", () => {
    expect(detectDelimiter("a,b\n1,2")).toBe(",")
    expect(detectDelimiter("a\tb\n1\t2")).toBe("\t")
    expect(detectDelimiter("a|b\n1|2")).toBe("|")
  })
})

describe("splitLine", () => {
  it("respeita aspas e o escape de aspas do RFC 4180", () => {
    expect(splitLine('a,"b,c",d', ",")).toEqual(["a", "b,c", "d"])
    expect(splitLine('a,"diz ""oi""",c', ",")).toEqual(["a", 'diz "oi"', "c"])
  })

  it("preserva campo vazio no meio da linha", () => {
    // Descartar o vazio desalinharia TODAS as colunas seguintes, e o resultado
    // seria uma tabela publicada com os valores trocados de coluna.
    expect(splitLine("a,,c", ",")).toEqual(["a", "", "c"])
  })
})

describe("parseCsv", () => {
  it("remove o BOM que o Excel escreve no primeiro cabeçalho", () => {
    // Com o BOM colado, o cabeçalho vira "﻿cidr", some da lista de colunas
    // e a coluna-chave fica inselecionável — sem mensagem de erro nenhuma.
    const parsed = parseCsv("﻿cidr,site\n10.0.0.0/8,matriz")
    expect(parsed.headers).toEqual(["cidr", "site"])
  })

  it("ignora linhas em branco e aceita CRLF", () => {
    const parsed = parseCsv("cidr,site\r\n10.0.0.0/8,matriz\r\n\r\n")
    expect(parsed.rows).toHaveLength(1)
    expect(parsed.rows[0]).toEqual({ cidr: "10.0.0.0/8", site: "matriz" })
  })

  it("preenche com vazio quando a linha tem menos colunas que o cabeçalho", () => {
    const parsed = parseCsv("cidr,site,dono\n10.0.0.0/8,matriz")
    expect(parsed.rows[0]).toEqual({ cidr: "10.0.0.0/8", site: "matriz", dono: "" })
  })
})

describe("isValidCidrOrIp", () => {
  it("recusa octeto acima de 255 e prefixo fora da faixa", () => {
    // São os dois erros de planilha exportada que o backend recusaria DEPOIS
    // do upload, com a contagem mas sem dizer quais linhas.
    expect(isValidCidrOrIp("10.0.300.0/24")).toBe(false)
    expect(isValidCidrOrIp("192.168.1.0/33")).toBe(false)
    expect(isValidCidrOrIp("10.0.5.0/24")).toBe(true)
    expect(isValidCidrOrIp("10.0.5.7")).toBe(true)
  })

  it("aceita IPv6 com e sem prefixo", () => {
    expect(isValidCidrOrIp("2001:db8::/48")).toBe(true)
    expect(isValidCidrOrIp("2001:db8::1")).toBe(true)
    expect(isValidCidrOrIp("2001:db8::/129")).toBe(false)
  })

  it("recusa texto que não é endereço", () => {
    expect(isValidCidrOrIp("matriz")).toBe(false)
    expect(isValidCidrOrIp("")).toBe(false)
    expect(isValidCidrOrIp("10.0.0.0/24/8")).toBe(false)
  })
})

describe("buildRows", () => {
  const parsed = parseCsv(
    [
      "cidr,site,criticidade,vlan",
      "10.0.0.0/16,matriz,media,100",
      "10.0.5.0/24,filial-sp,alta,105",
      "10.0.300.0/24,filial-bh,baixa,130",
      ",orfa,baixa,0",
      "10.0.5.0/24,duplicada,alta,999",
    ].join("\n"),
  )

  it("monta o corpo só com as colunas escolhidas", () => {
    const out = buildRows(parsed, "cidr", ["site", "criticidade"], "cidr")
    expect(out.rows["10.0.0.0/16"]).toEqual({ site: "matriz", criticidade: "media" })
    // `vlan` não foi escolhida e não entra: publicar campo a mais infla a
    // tabela residente, que tem teto por processo.
    expect(out.rows["10.0.0.0/16"]).not.toHaveProperty("vlan")
  })

  it("reporta cada linha problemática com o número da linha do arquivo", () => {
    const out = buildRows(parsed, "cidr", ["site"], "cidr")
    const porMotivo = Object.fromEntries(out.issues.map((i) => [i.reason, i]))

    expect(porMotivo.invalid_key.line).toBe(4)
    expect(porMotivo.invalid_key.key).toBe("10.0.300.0/24")
    expect(porMotivo.empty_key.line).toBe(5)
    // A duplicada venceria em silêncio: a última ocorrência sobrescreveria a
    // primeira e o operador publicaria "alta" achando que publicou "filial-sp".
    expect(porMotivo.duplicate_key.line).toBe(6)
    expect(out.rows["10.0.5.0/24"]).toEqual({ site: "filial-sp" })
  })

  it("não valida formato de chave quando o casamento é exato", () => {
    // Uma tabela `exact` casa hostname, usuário, hash — exigir CIDR ali
    // recusaria dado legítimo.
    const p = parseCsv("chave,dono\nservidor-01,infra\nfulano@ex.com,ti")
    const out = buildRows(p, "chave", ["dono"], "exact")
    expect(out.issues).toHaveLength(0)
    expect(Object.keys(out.rows)).toHaveLength(2)
  })

  it("calcula o diff contra a versão vigente", () => {
    // Publicar SUBSTITUI a versão inteira. O diff é a única chance de perceber
    // que o arquivo exportado está errado antes de a regra parar de casar.
    const p = parseCsv(
      ["cidr,site", "10.0.0.0/16,matriz", "10.0.9.0/24,filial-rj"].join("\n"),
    )
    const atual = {
      "10.0.0.0/16": { site: "matriz-antiga" },
      "10.0.7.0/24": { site: "some" },
    }
    const out = buildRows(p, "cidr", ["site"], "cidr", atual)

    expect(out.added).toEqual(["10.0.9.0/24"])
    expect(out.removed).toEqual(["10.0.7.0/24"])
    expect(out.changed).toEqual(["10.0.0.0/16"])
  })

  it("chave inalterada não entra no diff", () => {
    const p = parseCsv("cidr,site\n10.0.0.0/16,matriz")
    const out = buildRows(p, "cidr", ["site"], "cidr", {
      "10.0.0.0/16": { site: "matriz" },
    })
    expect(out.changed).toEqual([])
    expect(out.added).toEqual([])
    expect(out.removed).toEqual([])
  })
})

describe("approxBytes", () => {
  it("mede o corpo serializado, que é o que conta contra o teto", () => {
    const bytes = approxBytes({ "10.0.0.0/8": { site: "matriz" } })
    expect(bytes).toBeGreaterThan(0)
    expect(bytes).toBe(JSON.stringify({ "10.0.0.0/8": { site: "matriz" } }).length)
  })
})
