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
  label: string
  /** The registered entity a `uuid` field's value points at, when fixed --
   * mirrors server/core/registry.py's `FieldSpec.references`. */
  references: string | null
  /** For a polymorphic reference, the name of the sibling field holding the
   * target entity name for this row -- mirrors `FieldSpec.references_type_field`. */
  references_type_field: string | null
  /** This field's valid filter operators, straight from query.py's own
   * `operators_for(kind)` -- empty for an unfilterable field (e.g. a
   * `compute` field like `deal.rotting`). */
  operators: string[]
}

export interface CustomFieldSchema {
  key: string
  kind: FieldKind
  label: string
  options: string[]
  indexed: boolean
  writable: boolean
  /** Same vocabulary as `FieldSchema.operators` -- a custom field carries no
   * `filterable` flag of its own (it's always reachable through the jsonb
   * path), so `operators_for(kind)` alone decides it. */
  operators: string[]
}

/** One entry in `EntitySchema.profile_blocks` -- mirrors
 * `server/core/registry.py`'s `ProfileBlock` as exposed by
 * `GET /records/{entity}/schema`. `RecordPage.tsx` filters these against a
 * record's own `role_summary`/field values at render time; the schema only
 * ever describes what COULD apply to this entity type. */
export interface ProfileBlockSchema {
  key: string
  region: 'left' | 'center' | 'right'
  title: string
  order: number
  roles: string[]
  show_if_field: string | null
  show_if_equals: unknown
}

export interface EntitySchema {
  name: string
  label: string
  label_field: string
  default_sort: { field: string; direction: 'asc' | 'desc' }[]
  searchable: string[]
  supports_custom_fields: boolean
  admin_only: boolean
  list_columns: string[]
  profile_blocks: ProfileBlockSchema[]
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
  nav: 'primary' | 'settings' | 'none'
  nav_group: string
  nav_order: number
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
  | { not: FilterNode }
  /** No `field` -- an EXISTS against `core.associations` instead of a
   * comparison on the entity's own row (server/core/query.py's
   * `_compile_role_clause`). A seeded saved view (e.g. "Portfolio companies")
   * is the first real producer of this shape; the UI only ever passes it
   * through opaquely (FilterBuilder never constructs one by hand). */
  | { role: string; direction: 'from' | 'to'; as_of?: string; include_history?: boolean }

/** One entry in an organization's/person's `role_summary` computed field
 * (server/core/registry.py's `_role_summary` + `associations.
 * role_summary_for()`) -- the record's live, emergent "type," never
 * persisted. Shown as pills next to the record title in `RecordPage.tsx`
 * and used there to gate which `ProfileBlockSchema` entries apply. */
export interface RoleSummaryEntry {
  role: string
  direction: 'in' | 'out'
  label: string
  group: string
  group_order: number
}

export interface RelatedItem {
  association_id: string
  role: string
  direction: 'in' | 'out'
  attributes: Record<string, unknown>
  valid_from: string | null
  valid_to: string | null
  /** The related record's own entity type -- what a link to it routes to. */
  entity: string
  record: RecordRow
  /** Section this role's edges render under (server/core/registry.py's
   * `AssociationRole.group`, resolved through `role_presentation()`). */
  group: string
  group_order: number
  [key: string]: unknown
}

export type RelatedBlocks = Record<string, RelatedItem[]>

/** One direction of one association role `entity` can take part in --
 * mirrors `GET /records/{entity}/roles`' payload shape. */
export interface AssociationRoleOption {
  role: string
  direction: 'from' | 'to'
  label: string
  target_types: string[]
  group: string
  group_order: number
  hierarchical: boolean
}

export interface HierarchyStep {
  entity_type: string
  entity_id: string
  depth: number
  record: RecordRow
}

/** Records naming this one as their parent through a real FK field --
 * mirrors `GET /records/{entity}/{id}/children`'s payload shape. The
 * FK-based counterpart to `RelatedItem`/`RelatedBlocks` (association
 * edges): a fund's commitments, a person's tasks. `records` is capped
 * (`server/core/repository.py`'s `CHILDREN_BLOCK_LIMIT`); `total` may
 * exceed its length -- the block is truncated, never the count. */
export interface ChildrenBlock {
  entity: string
  field: string
  records: RecordRow[]
  total: number
}

/** One entry in a record's merged activity feed -- mirrors
 * `GET /records/{entity}/{id}/timeline`'s payload shape. `children_of()`'s
 * blocks (notes/tasks/documents/...) flattened to individual records, plus
 * -- for a person -- their interactions, all merged and sorted
 * newest-first by `occurred_at`. */
export interface TimelineEntry {
  entity: string
  record: RecordRow
  occurred_at: string | null
}

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
