export interface LiteralFinding {
  signature: string
  file: string
  line: number
  snippet: string
}

export declare const PT_WORDS: RegExp

export declare function looksLikePortuguese(text: string): boolean

export declare function stripComments(source: string): string

export declare function findingsInText(rawSource: string, relPath: string): LiteralFinding[]

export declare function walkTsFiles(dir: string): Generator<string, void, void>

export declare function countBySignature(findings: LiteralFinding[]): Map<string, number>

export interface FindingOverage {
  signature: string
  current: number
  baseline: number
  extra: number
}

export declare function findOverages(
  currentCounts: Map<string, number>,
  baselineCounts: Map<string, number>,
): FindingOverage[]

export declare function countsToBaselineObject(counts: Map<string, number>): Record<string, number>

export declare function baselineToCounts(baselineRaw: unknown): Map<string, number>
