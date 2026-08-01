/** Mirrors server/core/registry.py's FieldSpec/EntitySpec shapes as returned
 * by GET /records/{entity}/schema (server/api/records.py's `entity_schema`).
 * The frontend never hardcodes a field list per entity (R4) -- every table
 * column, filter, and form field is built from this at runtime. */

/** Mirrors server/core/registry.py's KINDS (custom fields) plus the core
 * field kinds (`uuid`) that never appear as a custom-field kind but do
 * appear on core FieldSpecs. */
export type FieldKind =
  | 'text' | 'number' | 'date' | 'datetime' | 'boolean'
  | 'select' | 'multiselect' | 'currency' | 'url' | 'email' | 'phone' | 'uuid' | 'jsonb'

export interface FieldSchema {
  kind: FieldKind
  filterable: boolean
  sortable: boolean
  writable: boolean
  required: boolean
  options: string[]
}

export interface CustomFieldSchema {
  key: string
  kind: FieldKind
  label: string
  options: string[]
  indexed: boolean
  writable: boolean
}

export interface EntitySchema {
  name: string
  label: string
  label_field: string
  default_sort: { field: string; direction: 'asc' | 'desc' }[]
  searchable: string[]
  supports_custom_fields: boolean
  admin_only: boolean
  fields: Record<string, FieldSchema>
  custom_fields: CustomFieldSchema[]
  can_create: boolean
  read_level: string
  edit_level: string
  delete_level: string
}

export interface EntitySummary {
  name: string
  label: string
  label_field: string
  admin_only: boolean
  module: string
}

/** A record's shape is inherently dynamic -- see EntitySchema above. Custom
 * field values nest under `custom`, exactly as the API returns and accepts
 * them (server/core/repository.py's `_split_changes`). */
export type RecordRow = Record<string, unknown> & {
  id: string
  created_at: string
  updated_at: string
  custom?: Record<string, unknown>
}

export interface ListResult {
  records: RecordRow[]
  total: number
}

export interface SortSpec {
  field: string
  direction: 'asc' | 'desc'
}

export type FilterNode =
  | { field: string; op: string; value?: unknown }
  | { and: FilterNode[] }
  | { or: FilterNode[] }

export interface RelatedItem {
  association_id: string
  role: string
  record: RecordRow
  [key: string]: unknown
}

export type RelatedBlocks = Record<string, RelatedItem[]>

export interface SavedView {
  id: string
  entity: string
  name: string
  kind: 'table' | 'board' | 'calendar'
  filters: FilterNode | null
  sort: SortSpec[] | null
  columns: string[] | null
  group_by: string | null
  is_shared: boolean
  created_at: string
  updated_at: string
}
