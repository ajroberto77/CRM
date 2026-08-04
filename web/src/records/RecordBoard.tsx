import { useEffect, useMemo, useState } from 'react'
import { apiGet, ApiError } from '../lib/api'
import { formatCurrency, formatValue } from '../lib/format'
import { useRecordList } from './useRecordList'
import { useSaveField } from './useSaveField'
import type { EntitySchema, FilterNode, RecordRow, SortSpec } from './types'

/**
 * The board/kanban view type -- the same list `RecordTable` renders, grouped
 * into lanes by one field instead of rows in a table (shares `useRecordList`
 * with RecordTable rather than issuing a parallel fetch, per ui-reviewer's
 * own stated rule for this exact shape of duplication).
 *
 * Lane sourcing is generic (a `select`-kind field's declared `options`
 * become lanes) with one deliberate, narrow exception: `deal.stage` has no
 * `options` -- its vocabulary is per-pipeline (`core.pipelines.stages`),
 * since different pipelines legitimately have different stages. That one
 * case is special-cased below rather than generalized for a single caller.
 */

interface RecordBoardProps {
  entity: string
  schema: EntitySchema
  filters: FilterNode | null
  sort: SortSpec[] | null
  groupBy: string
  onOpenRecord: (id: string) => void
  refreshToken: number
}

interface Lane {
  value: string | null
  label: string
}

const NONE_LANE_LABEL = '(none)'

export function RecordBoard({ entity, schema, filters, sort, groupBy, onOpenRecord, refreshToken }: RecordBoardProps) {
  const isDealStage = entity === 'deal' && groupBy === 'stage'

  // ── The one named exception: deal.stage's lanes come from a pipeline,
  // not from FieldSpec.options (deal.stage has none -- see registry.py). ──
  const [pipelines, setPipelines] = useState<{ id: string; name: string }[]>([])
  const [pipelinesLoading, setPipelinesLoading] = useState(isDealStage)
  const [selectedPipelineId, setSelectedPipelineId] = useState<string | null>(null)
  const [pipelineStages, setPipelineStages] = useState<string[] | null>(null)
  const [pipelineError, setPipelineError] = useState<string | null>(null)

  useEffect(() => {
    if (!isDealStage) return
    setPipelinesLoading(true)
    setPipelineError(null)
    apiGet<{ records: { id: string; name: string }[] }>('/records/pipeline')
      .then((res) => {
        setPipelines(res.records)
        if (res.records.length === 1) setSelectedPipelineId(res.records[0].id)
      })
      .catch((err) => setPipelineError(err instanceof ApiError ? err.detail : String(err)))
      .finally(() => setPipelinesLoading(false))
    // Only ever needs to run once per mount -- the board's own pipeline
    // picker, not something that reacts to filters/sort/refreshToken.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isDealStage])

  const [stagesLoading, setStagesLoading] = useState(false)

  useEffect(() => {
    if (!isDealStage || !selectedPipelineId) {
      setPipelineStages(null)
      return
    }
    setStagesLoading(true)
    apiGet<{ record: { stages: unknown } }>(`/records/pipeline/${selectedPipelineId}`)
      .then((res) => {
        const raw = res.record.stages
        setPipelineStages(Array.isArray(raw) ? raw.map((s) => String(s)) : [])
      })
      .catch((err) => setPipelineError(err instanceof ApiError ? err.detail : String(err)))
      .finally(() => setStagesLoading(false))
  }, [isDealStage, selectedPipelineId])

  const effectiveFilters = useMemo<FilterNode | null>(() => {
    if (!isDealStage || !selectedPipelineId) return filters
    const pipelineFilter: FilterNode = { field: 'pipeline_id', op: 'eq', value: selectedPipelineId }
    return filters ? { and: [filters, pipelineFilter] } : pipelineFilter
  }, [filters, isDealStage, selectedPipelineId])

  const { result, loading, error, setResult } = useRecordList(entity, effectiveFilters, sort, refreshToken)

  const groupFieldSchema = schema.fields[groupBy]
  const labelKind = schema.fields[schema.label_field]?.kind ?? 'text'
  const hasAmount = Boolean(schema.fields.amount && schema.fields.currency)

  const lanes = useMemo<Lane[]>(() => {
    if (isDealStage) {
      if (!pipelineStages) return []
      return [...pipelineStages.map((s) => ({ value: s, label: s })), { value: null, label: NONE_LANE_LABEL }]
    }
    if (groupFieldSchema && groupFieldSchema.options.length > 0) {
      return [
        ...groupFieldSchema.options.map((o) => ({ value: o, label: o })),
        { value: null, label: NONE_LANE_LABEL },
      ]
    }
    // Generic fallback: distinct values actually present in the fetched
    // page. Weaker than the two cases above (an empty lane with zero
    // current rows won't show), but keeps the board usable on any field.
    const seen = new Set<string>()
    const values: string[] = []
    for (const row of result?.records ?? []) {
      const v = row[groupBy]
      if (v === null || v === undefined || v === '') continue
      const s = String(v)
      if (!seen.has(s)) {
        seen.add(s)
        values.push(s)
      }
    }
    values.sort()
    return [...values.map((v) => ({ value: v, label: v })), { value: null, label: NONE_LANE_LABEL }]
  }, [isDealStage, pipelineStages, groupFieldSchema, result, groupBy])

  const [dragOverLane, setDragOverLane] = useState<string | null>(null)
  const [movingId, setMovingId] = useState<string | null>(null)

  const { saveField } = useSaveField({
    entity,
    schema,
    onSaved: (recordId, updated) =>
      setResult((prev) =>
        prev ? { ...prev, records: prev.records.map((r) => (r.id === recordId ? updated : r)) } : prev,
      ),
  })

  async function moveRecord(row: RecordRow, laneValue: string | null) {
    const currentValue = row[groupBy]
    const normalizedCurrent = currentValue === undefined || currentValue === '' ? null : (currentValue as string | null)
    if (normalizedCurrent === laneValue) return
    setMovingId(row.id)
    // Optimistic: the lane change shows immediately, before the round trip
    // -- board drag-and-drop is the one caller of useSaveField that needs
    // this, so it stays local rather than folded into the shared hook.
    setResult((prev) =>
      prev
        ? { ...prev, records: prev.records.map((r) => (r.id === row.id ? { ...r, [groupBy]: laneValue } : r)) }
        : prev,
    )
    const ok = await saveField(String(row.id), groupBy, laneValue, groupFieldSchema?.kind ?? 'text')
    if (!ok) {
      setResult((prev) => (prev ? { ...prev, records: prev.records.map((r) => (r.id === row.id ? row : r)) } : prev))
    }
    setMovingId(null)
  }

  if (pipelineError) return <div className="crm-table-status crm-table-status-error">{pipelineError}</div>
  if (isDealStage && pipelinesLoading) return <div className="crm-table-status">Loading…</div>
  if (isDealStage && pipelines.length === 0) {
    return <div className="crm-table-status">No pipelines exist yet — create one to use the stage board.</div>
  }
  if (isDealStage && !selectedPipelineId && pipelines.length > 1) {
    return (
      <div className="crm-record-board">
        <PipelinePicker pipelines={pipelines} selectedId={selectedPipelineId} onSelect={setSelectedPipelineId} />
        <div className="crm-table-status">Choose a pipeline to see its stages.</div>
      </div>
    )
  }

  if (loading || stagesLoading) return <div className="crm-table-status">Loading…</div>
  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>

  const allRows = result?.records ?? []

  return (
    <div className="crm-record-board">
      {isDealStage && pipelines.length > 1 && (
        <PipelinePicker pipelines={pipelines} selectedId={selectedPipelineId} onSelect={setSelectedPipelineId} />
      )}
      {/* Lane counts below are counts within the fetched page (useRecordList's
          limit, same cap RecordTable's own footer total is scoped by) -- this
          note is the board's equivalent of RecordTable's "N records" footer,
          so per-lane counts don't read as authoritative totals past that cap. */}
      {result && (
        <div className="crm-record-board-total">
          {result.total} record{result.total === 1 ? '' : 's'}
        </div>
      )}
      <div className="crm-record-board-lanes">
        {lanes.map((lane) => {
          const rows = allRows.filter((r) => {
            const v = r[groupBy]
            const normalized = v === null || v === undefined || v === '' ? null : String(v)
            return normalized === lane.value
          })
          return (
            <div
              key={lane.label}
              className={
                dragOverLane === lane.label ? 'crm-record-board-lane crm-record-board-lane-over' : 'crm-record-board-lane'
              }
              onDragOver={(e) => {
                e.preventDefault()
                setDragOverLane(lane.label)
              }}
              onDragLeave={() => setDragOverLane((cur) => (cur === lane.label ? null : cur))}
              onDrop={(e) => {
                e.preventDefault()
                setDragOverLane(null)
                const id = e.dataTransfer.getData('text/plain')
                const row = allRows.find((r) => r.id === id)
                if (row) moveRecord(row, lane.value)
              }}
            >
              <div className="crm-record-board-lane-header">
                <span>{lane.label}</span>
                <span className="crm-record-board-lane-count">{rows.length}</span>
              </div>
              <div className="crm-record-board-lane-cards">
                {rows.map((row) => (
                  <div
                    key={row.id}
                    className={
                      movingId === row.id ? 'crm-record-board-card crm-record-board-card-moving' : 'crm-record-board-card'
                    }
                    draggable
                    role="button"
                    tabIndex={0}
                    onDragStart={(e) => e.dataTransfer.setData('text/plain', row.id)}
                    onClick={() => onOpenRecord(row.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault()
                        onOpenRecord(row.id)
                      }
                    }}
                  >
                    <div className="crm-record-board-card-title">
                      {formatValue(labelKind, row[schema.label_field]) || '(untitled)'}
                    </div>
                    {/* Card meta is schema-driven by field NAME convention, same as the
                        deal.stage lane exception above -- an amount/currency pair renders
                        when both fields exist, and a "Rotting" badge renders for any
                        boolean field literally named `rotting` (today: deal's own computed
                        FieldSpec, server/core/registry.py's _deal_rotting). Not entity-name
                        branching, but still a convention worth a future FieldSpec-level
                        "badge" flag if a second computed warning field ever needs this. */}
                    {hasAmount && (
                      <div className="crm-record-board-card-meta">
                        {formatCurrency(row.amount, row.currency as string | undefined)}
                      </div>
                    )}
                    {row.rotting === true && <span className="crm-record-board-card-rotting">Rotting</span>}
                  </div>
                ))}
                {rows.length === 0 && <div className="crm-record-board-lane-empty">—</div>}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function PipelinePicker({
  pipelines,
  selectedId,
  onSelect,
}: {
  pipelines: { id: string; name: string }[]
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  return (
    <div className="crm-record-board-toolbar">
      <label>
        Pipeline
        <select value={selectedId ?? ''} onChange={(e) => onSelect(e.target.value)}>
          <option value="" disabled>
            Choose a pipeline…
          </option>
          {pipelines.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
    </div>
  )
}
