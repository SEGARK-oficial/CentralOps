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
