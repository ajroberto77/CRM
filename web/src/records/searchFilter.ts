import type { EntitySchema, FilterNode } from './types'

/** The one search-to-filter translation (R1) -- OR-of-`contains` across an
 * entity's `searchable` fields, falling back to `label_field` when none are
 * declared. Shared by `EntityListPage`'s search box and `useRecordSearch`'s
 * reference-picker search, which were previously two byte-identical
 * inline copies. */
export function buildSearchFilter(schema: EntitySchema, text: string): FilterNode {
  const searchable = schema.searchable.length > 0 ? schema.searchable : [schema.label_field]
  return { or: searchable.map((field) => ({ field, op: 'contains', value: text })) }
}
