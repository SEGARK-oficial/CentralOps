export declare function flatten(obj: Record<string, unknown>, prefix?: string, out?: Set<string>): Set<string>

export declare function extractDefaultNamespaces(sourceText: string): string[]

export declare function keyResolvesInAnyNamespace(
  nsList: string[],
  key: string,
  getCatalogKeys: (ns: string) => Set<string> | undefined,
): boolean

export interface NamespaceCandidates {
  nsList: string[]
  key: string
}

export declare function resolveNamespaceCandidates(
  raw: string,
  defaultNamespaces: string[],
): NamespaceCandidates
