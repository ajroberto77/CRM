import { Link } from 'react-router-dom'
import { formatDateRange, recordLabel } from '../../lib/format'
import { registerProfileBlock } from '../../records/profileBlocks'
import type { ProfileBlockProps } from '../../records/profileBlocks'

/** Shown on an organization once its `role_summary` includes `portfolio_of`
 * or `evaluating` -- an actual investment and a prospect it's still being
 * evaluated for are shown as two separate lists rather than merged, since
 * "invested" vs. "being looked at" is exactly the distinction these two
 * roles exist to keep apart (see modules/funds/manifest.py's `evaluating`
 * role docstring). */
function PortfolioSummaryBlock({ related }: ProfileBlockProps) {
  const invested = related['Portfolio of'] ?? []
  const evaluating = related['being evaluated by'] ?? []

  return (
    <div className="crm-profile-block">
      <div className="crm-profile-block-title">Portfolio summary</div>
      <div className="crm-profile-block-body">
        <div className="crm-profile-block-stat-row">
          <div className="crm-profile-block-stat">
            <div className="crm-profile-block-stat-value">{invested.length}</div>
            <div className="crm-profile-block-stat-label">invested</div>
          </div>
          <div className="crm-profile-block-stat">
            <div className="crm-profile-block-stat-value">{evaluating.length}</div>
            <div className="crm-profile-block-stat-label">evaluating</div>
          </div>
        </div>
        {[...invested, ...evaluating].length > 0 && (
          <div className="crm-profile-block-list">
            {invested.map((item) => (
              <div key={item.association_id}>
                <Link to={`/r/${item.entity}/${item.record.id}`} className="crm-reference-link">
                  {recordLabel(item.record)}
                </Link>
                {(item.valid_from || item.valid_to) && (
                  <span className="crm-related-item-dated"> {formatDateRange(item.valid_from, item.valid_to)}</span>
                )}
              </div>
            ))}
            {evaluating.map((item) => (
              <div key={item.association_id}>
                <Link to={`/r/${item.entity}/${item.record.id}`} className="crm-reference-link">
                  {recordLabel(item.record)}
                </Link>
                <span className="crm-related-item-dated"> (evaluating)</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

registerProfileBlock('funds.portfolio_summary', PortfolioSummaryBlock)
