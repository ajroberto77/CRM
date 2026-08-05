/** Registers every funds-module profile block by import side effect --
 * imported once from main.tsx, so core (`records/profileBlocks.ts`) never
 * has to know these components exist (R6), the same seam
 * `modules/funds/manifest.py` gives the backend registry. */
import './LpSummaryBlock'
import './GpSummaryBlock'
import './PortfolioSummaryBlock'
