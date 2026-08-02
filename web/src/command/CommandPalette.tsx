import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiPost, ApiError } from '../lib/api'
import { useEntityList } from '../records/useEntitySchema'

/**
 * M7's Cmd/Ctrl+K palette -- global keyboard listener mounted once in
 * Shell.tsx. Fuzzy-matches the sidebar's entity list first (a client-side
 * substring match against the entity names/labels from useEntityList(),
 * the same hook Shell.tsx and App.tsx already call -- this is its own
 * GET /records, not a reuse of the sidebar's in-memory state, since the
 * hook has no shared cache; the request is cheap and this follows existing
 * precedent rather than adding a new fetch path); once the query is long
 * enough AND matches no entity, falls back (debounced) to POST /search for
 * semantic results over interaction bodies and other embedded content.
 *
 * No new frontend dependency (no cmdk/kbar) -- reuses the existing
 * .crm-modal-overlay backdrop and design tokens, matching the app's
 * minimal-runtime-deps stance.
 */

const MIN_SEARCH_QUERY_LENGTH = 3
const SEARCH_DEBOUNCE_MS = 250

interface SearchResult {
  id: string
  owner_type: string
  owner_id: string
  chunk_kind: string
  content_text: string
  distance: number
}

export function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { entities } = useEntityList()
  const navigate = useNavigate()

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((prev) => !prev)
      } else if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  useEffect(() => {
    if (!open) {
      setQuery('')
      setResults([])
      setError(null)
    }
  }, [open])

  const trimmed = query.trim().toLowerCase()
  const entityMatches = trimmed
    ? entities.filter(
        (e) => e.name.toLowerCase().includes(trimmed) || e.label.toLowerCase().includes(trimmed)
      )
    : []

  useEffect(() => {
    if (trimmed.length < MIN_SEARCH_QUERY_LENGTH || entityMatches.length > 0) {
      setResults([])
      setSearching(false)
      setError(null)
      return
    }
    setSearching(true)
    // Clear any previous query's hits immediately -- otherwise they stay
    // rendered (mixed in with the "Searching…" hint) until the new fetch
    // resolves, since the results list below is only gated on
    // entityMatches.length, not on `searching`.
    setResults([])
    setError(null)
    let cancelled = false
    const handle = setTimeout(() => {
      apiPost<{ results: SearchResult[] }>('/search', { query: trimmed, limit: 8 })
        .then((r) => {
          if (!cancelled) setResults(r.results)
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof ApiError ? err.detail : 'Search failed')
        })
        .finally(() => {
          if (!cancelled) setSearching(false)
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      // Guards against out-of-order responses too: if the user keeps typing
      // before this request resolves, its (possibly late-arriving) result
      // must not overwrite a newer query's results.
      cancelled = true
      clearTimeout(handle)
    }
    // entityMatches.length, not the array itself, is the real dependency --
    // entities only ever loads once, so its filtered length is stable
    // across renders that don't change `query`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trimmed, entityMatches.length])

  function goToEntity(name: string) {
    navigate(`/e/${name}`)
    setOpen(false)
  }

  function goToResult(result: SearchResult) {
    if (entities.some((e) => e.name === result.owner_type)) {
      navigate(`/e/${result.owner_type}/${result.owner_id}`)
    }
    setOpen(false)
  }

  if (!open) return null

  return (
    <div className="crm-modal-overlay" onClick={() => setOpen(false)}>
      <div className="crm-command-palette" onClick={(e) => e.stopPropagation()}>
        <input
          autoFocus
          className="crm-command-palette-input"
          placeholder="Jump to an entity, or search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {error && <div className="crm-auth-error">{error}</div>}
        <div className="crm-command-palette-results">
          {entityMatches.map((e) => (
            <button
              key={e.name}
              className="crm-command-palette-result"
              onClick={() => goToEntity(e.name)}
            >
              {e.label}
            </button>
          ))}
          {entityMatches.length === 0 && searching && (
            <div className="crm-settings-hint">Searching…</div>
          )}
          {entityMatches.length === 0 && !searching && results.length === 0 && trimmed.length >= MIN_SEARCH_QUERY_LENGTH && (
            <div className="crm-table-status">No matches.</div>
          )}
          {entityMatches.length === 0 &&
            results.map((r) => (
              <button
                key={r.id}
                className="crm-command-palette-result"
                onClick={() => goToResult(r)}
              >
                <span className="crm-command-palette-result-kind">{r.owner_type}</span>
                <span className="crm-command-palette-result-snippet">
                  {r.content_text.slice(0, 120)}
                </span>
              </button>
            ))}
        </div>
      </div>
    </div>
  )
}
