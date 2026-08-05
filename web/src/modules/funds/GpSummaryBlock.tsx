import { Link } from 'react-router-dom'
import { formatDateRange, recordLabel } from '../../lib/format'
import { registerProfileBlock } from '../../records/profileBlocks'
import type { ProfileBlockProps } from '../../records/profileBlocks'

/** Shown once a record's `role_summary` includes `gp_of` or `principal_of`
 * -- the GP-side counterpart to `LpSummaryBlock`. Both role labels can
 * appear on the same record (a Managing Partner is `principal_of` the GP
 * organization AND, if the GP itself invests, `gp_of` a fund), so this
 * merges both related-blocks groups into one list rather than showing two
 * near-identical "funds you're involved with" panels. */
function GpSummaryBlock({ related }: ProfileBlockProps) {
  const items = [...(related['GP of'] ?? []), ...(related['Principal of'] ?? [])]
  return (
    <div className="crm-profile-block">
      <div className="crm-profile-block-title">GP summary</div>
      <div className="crm-profile-block-body">
        {items.length === 0 ? (
          <div className="crm-profile-block-empty">No GP-side relationships on file.</div>
        ) : (
          <div className="crm-profile-block-list">
            {items.map((item) => (
              <div key={item.association_id}>
                <Link to={`/r/${item.entity}/${item.record.id}`} className="crm-reference-link">
                  {recordLabel(item.record)}
                </Link>
                {(item.valid_from || item.valid_to) && (
                  <span className="crm-related-item-dated"> {formatDateRange(item.valid_from, item.valid_to)}</span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

registerProfileBlock('funds.gp_summary', GpSummaryBlock)
