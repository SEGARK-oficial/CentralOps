/**
 * Catálogo de permissões — categoria e descrição legível de cada scope.
 *
 * **Fonte única.** Antes, `ScopeSelector` (criar token) tinha sua própria
 * cópia hardcoded e a matriz de `/admin/users` não tinha nenhuma: mostrava
 * `internal.tenant.read` cru, que é fiel e ilegível ao mesmo tempo. Duas
 * cópias divergiram na prática — `query.run`, `query.save` e
 * `correlation.preview` existem no backend há tempo e caíam em "Outros" no
 * seletor de scopes, sem descrição.
 *
 * A LISTA de permissões e quem as tem continua vindo do backend
 * (`GET /auth/permissions`, derivado de `ROLE_PERMISSIONS`) — este módulo só
 * acrescenta como explicá-las a um humano. O teste
 * `permissions.catalog.test.ts` falha se o backend ganhar uma permissão que
 * não esteja aqui, que é o que impede a divergência de voltar.
 */

export type PermissionCategory =
  | "integrations"
  | "mappings"
  | "quarantineDrift"
  | "detection"
  | "administration"
  | "internal"

/** Ordem de exibição. Do mais operacional ao mais privilegiado. */
export const PERMISSION_CATEGORY_ORDER: PermissionCategory[] = [
  "integrations",
  "mappings",
  "quarantineDrift",
  "detection",
  "administration",
  "internal",
]

/**
 * Toda permissão do `Permission` StrEnum do backend
 * (`backend/app/core/auth.py`). Manter em sincronia é obrigação verificada
 * por teste, não por disciplina.
 */
export const PERMISSION_CATALOG: Record<string, PermissionCategory> = {
  "integration.read": "integrations",
  "integration.write": "integrations",
  "integration.pause": "integrations",

  "mapping.read": "mappings",
  "mapping.write": "mappings",
  "mapping.rollback": "mappings",

  "quarantine.read": "quarantineDrift",
  "quarantine.discard": "quarantineDrift",
  "drift.read": "quarantineDrift",
  "drift.ignore": "quarantineDrift",
  "drift.mark_mapped": "quarantineDrift",
  "drift.delete": "quarantineDrift",

  "query.run": "detection",
  "query.save": "detection",
  "correlation.preview": "detection",

  "user.manage": "administration",
  "org.manage": "administration",
  "secret.read": "administration",
  "audit.read": "administration",

  "internal.tenant.read": "internal",
}

/** Categoria de uma permissão; desconhecida cai em "internal" (não some). */
export function categoryOf(permission: string): PermissionCategory {
  return PERMISSION_CATALOG[permission] ?? "internal"
}

/** Chave i18n da descrição. O texto vive no namespace `admin`. */
export function descriptionKeyOf(permission: string): string {
  return `permissions.descriptions.${permission}`
}

/** Chave i18n do rótulo de uma categoria. */
export function categoryLabelKeyOf(category: PermissionCategory): string {
  return `permissions.categories.${category}`
}

/** Agrupa permissões por categoria, preservando `PERMISSION_CATEGORY_ORDER`. */
export function groupByCategory(
  permissions: string[],
): Array<{ category: PermissionCategory; permissions: string[] }> {
  const buckets = new Map<PermissionCategory, string[]>()
  for (const category of PERMISSION_CATEGORY_ORDER) buckets.set(category, [])
  for (const permission of permissions) {
    buckets.get(categoryOf(permission))?.push(permission)
  }
  return PERMISSION_CATEGORY_ORDER.map((category) => ({
    category,
    permissions: (buckets.get(category) ?? []).sort(),
  })).filter((group) => group.permissions.length > 0)
}
