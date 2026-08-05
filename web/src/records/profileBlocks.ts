import type { ComponentType } from 'react'
import type { RecordRow, RelatedBlocks } from './types'

/** What every profile block component receives -- the record itself plus
 * its already-loaded related-records data (`RecordPage.tsx`'s own `related`
 * state, the same data `RelatedPanel` renders), so a block never fires a
 * second fetch for information the page already has in hand. */
export interface ProfileBlockProps {
  entity: string
  record: RecordRow
  related: RelatedBlocks
}

/** String-keyed registry, populated by each module's own block components at
 * import time (`registerProfileBlock('funds.portfolio_summary', ...)`) --
 * core (this file) never learns a module's block keys or what they render
 * (R6). `RecordPage.tsx` looks up a key from `schema.profile_blocks`
 * (server/core/registry.py's `ProfileBlock`) and renders whatever is
 * registered here, or nothing if the module's frontend bundle isn't loaded
 * -- the same fail-safe-empty contract `getProfileBlock` documents below. */
const blocks: Record<string, ComponentType<ProfileBlockProps>> = {}

export function registerProfileBlock(key: string, component: ComponentType<ProfileBlockProps>): void {
  blocks[key] = component
}

/** Undefined for an unregistered key -- deliberately not an error. A block
 * the schema names but whose module bundle hasn't registered a component
 * (not yet built, or a module disabled after the fact) simply doesn't
 * render, the same "hidden, not broken" contract the rest of this platform
 * gives an entity/role the current principal can't read. */
export function getProfileBlock(key: string): ComponentType<ProfileBlockProps> | undefined {
  return blocks[key]
}
