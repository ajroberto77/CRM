import { Link } from 'react-router-dom'
import { formatDateRange, recordLabel } from '../../lib/format'
import { registerProfileBlock } from '../../records/profileBlocks'
import type { ProfileBlockProps } from '../../records/profileBlocks'

/** Shown on an organization's or person's record page once its live
 * `role_summary` includes `lp_in` (registered in modules/funds/manifest.py's
 * `install()`). Reads `related["LP in"]` -- data `RecordPage.tsx` already
 * loaded for the Related panel -- rather than a second fetch. */
function LpSummaryBlock({ related }: ProfileBlockProps) {
  const commitments = related['LP in'] ?? []
  return (
    <div className="crm-profile-block">
      <div className="crm-profile-block-title">LP summary</div>
      <div className="crm-profile-block-body">
        <div className="crm-profile-block-stat-row">
          <div className="crm-profile-block-stat">
            <div className="crm-profile-block-stat-value">{commitments.length}</div>
            <div className="crm-profile-block-stat-label">
              {commitments.length === 1 ? 'fund' : 'funds'}
            </div>
          </div>
        </div>
        {commitments.length > 0 && (
          <div className="crm-profile-block-list">
            {commitments.map((item) => (
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

registerProfileBlock('funds.lp_summary', LpSummaryBlock)
