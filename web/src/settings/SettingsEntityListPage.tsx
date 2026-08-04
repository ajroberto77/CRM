import { EntityListPage } from '../records/EntityListPage'

/** Wrapper for entity list pages mounted under settings. EntityListPage is now
 * smart enough to normalize entity names from URL params (kebab-case to snake_case),
 * so we just pass it through. */
export function SettingsEntityListPage() {
  return <EntityListPage />
}
