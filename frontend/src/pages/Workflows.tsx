import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  Clock3,
  ExternalLink,
  GitBranch,
  Loader2,
  Play,
  RefreshCw,
  RotateCcw,
  Square,
  XCircle,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { formatDateTime, type Language } from '@/lib/i18n'
import { useI18n } from '@/lib/i18n-context'
import { cn } from '@/lib/utils'
import {
  CANCELLABLE_WORKFLOW_STATUSES,
  RETRYABLE_STEP_STATUSES,
  WORKFLOW_STATUS_VARIANTS,
  cancelWorkflowRun,
  createWorkflowRun,
  fetchWorkflowDefinitions,
  fetchWorkflowEvents,
  fetchWorkflowRun,
  fetchWorkflowRuns,
  getWorkflowStatusText,
  retryWorkflowStep,
  updateWorkflowStepInput,
  type WorkflowDefinition,
  type WorkflowEvent,
  type WorkflowRun,
  type WorkflowStepRun,
} from '@/lib/workflows'

const PAGE_SIZE = 20

const STATUS_OPTIONS = [
  '',
  'pending',
  'running',
  'waiting_external',
  'retry_scheduled',
  'needs_attention',
  'succeeded',
  'failed',
  'cancelled',
]

const DEFAULT_REGISTER_CODEX_PUSH_INPUT = {
  registration: {
    count: 1,
    concurrency: 1,
    executor_type: 'headless',
    extra: {
      identity_provider: 'mailbox',
    },
  },
  codex: {
    browser_mode: 'headless',
    keep_browser_open: 'false',
  },
  push: {
    target_key: 'nvtokens',
    payload_format: 'codex',
  },
}

function formatMaybeDate(value: string | undefined, language: Language) {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '-'
  return formatDateTime(date, language, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatJson(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2)
}

function parseJsonObject(text: string) {
  const parsed = JSON.parse(text || '{}')
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('JSON must be an object')
  }
  return parsed as Record<string, unknown>
}

function workflowInputForDefinition(definition?: WorkflowDefinition) {
  if (definition?.key === 'register_codex_push') {
    return DEFAULT_REGISTER_CODEX_PUSH_INPUT
  }
  return {}
}

function stepIcon(status: string) {
  if (status === 'succeeded') return CheckCircle2
  if (status === 'failed') return XCircle
  if (status === 'needs_attention') return AlertTriangle
  if (status === 'running') return Loader2
  return Clock3
}

function shortError(error: Record<string, unknown>) {
  return String(error?.message || error?.code || '')
}

function externalTaskUrl(step: WorkflowStepRun) {
  const taskId = step.external_ref || String(step.output?.task_id || '')
  return taskId ? '/tasks' : ''
}

export default function Workflows() {
  const { t, language } = useI18n()
  const [definitions, setDefinitions] = useState<WorkflowDefinition[]>([])
  const [definitionKey, setDefinitionKey] = useState('')
  const [definitionFilter, setDefinitionFilter] = useState('')
  const [status, setStatus] = useState('')
  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [total, setTotal] = useState(0)
  const [running, setRunning] = useState(0)
  const [offset, setOffset] = useState(0)
  const [selectedRunId, setSelectedRunId] = useState('')
  const [selectedRun, setSelectedRun] = useState<WorkflowRun | null>(null)
  const [events, setEvents] = useState<WorkflowEvent[]>([])
  const [inputText, setInputText] = useState(formatJson(DEFAULT_REGISTER_CODEX_PUSH_INPUT))
  const [inputError, setInputError] = useState('')
  const [stepInputText, setStepInputText] = useState('{}')
  const [stepInputError, setStepInputError] = useState('')
  const [editingStepId, setEditingStepId] = useState('')
  const [loading, setLoading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [actionId, setActionId] = useState('')
  const [error, setError] = useState('')

  const selectedDefinition = useMemo(
    () => definitions.find((item) => item.key === definitionKey) || definitions[0],
    [definitionKey, definitions],
  )

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1

  const loadDefinitions = useCallback(async () => {
    const items = await fetchWorkflowDefinitions()
    setDefinitions(items)
    setDefinitionKey((current) => current || items[0]?.key || '')
  }, [])

  const loadRuns = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const data = await fetchWorkflowRuns({
        limit: PAGE_SIZE,
        offset,
        status,
        definition_key: definitionFilter,
      })
      setRuns(data.items || [])
      setTotal(Number(data.total || 0))
      setRunning(Number(data.running || 0))
      setSelectedRunId((current) => current || data.items?.[0]?.id || '')
    } catch (exc: unknown) {
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    } finally {
      setLoading(false)
    }
  }, [definitionFilter, offset, status, t])

  const loadSelectedRun = useCallback(async () => {
    if (!selectedRunId) {
      setSelectedRun(null)
      setEvents([])
      return
    }
    try {
      const [run, eventData] = await Promise.all([
        fetchWorkflowRun(selectedRunId),
        fetchWorkflowEvents(selectedRunId),
      ])
      setSelectedRun(run)
      setEvents(eventData.items || [])
    } catch (exc: unknown) {
      setSelectedRun(null)
      setEvents([])
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    }
  }, [selectedRunId, t])

  useEffect(() => {
    loadDefinitions().catch((exc: unknown) => {
      setError(exc instanceof Error ? exc.message : t('workflows.loadFailed'))
    })
  }, [loadDefinitions, t])

  useEffect(() => {
    loadRuns()
  }, [loadRuns])

  useEffect(() => {
    loadSelectedRun()
  }, [loadSelectedRun])

  useEffect(() => {
    if (!selectedDefinition) return
    setInputText(formatJson(workflowInputForDefinition(selectedDefinition)))
  }, [selectedDefinition])

  useEffect(() => {
    const hasActiveRun =
      running > 0 ||
      runs.some((item) => CANCELLABLE_WORKFLOW_STATUSES.has(item.status)) ||
      (selectedRun && CANCELLABLE_WORKFLOW_STATUSES.has(selectedRun.status))
    if (!hasActiveRun) return
    const timer = window.setInterval(() => {
      loadRuns()
      loadSelectedRun()
    }, 2500)
    return () => window.clearInterval(timer)
  }, [loadRuns, loadSelectedRun, running, runs, selectedRun])

  const startRun = async () => {
    if (!selectedDefinition) return
    setInputError('')
    setSubmitting(true)
    try {
      const parsedInput = parseJsonObject(inputText)
      const run = await createWorkflowRun({
        definition_key: selectedDefinition.key,
        version: selectedDefinition.version,
        input: parsedInput,
      })
      setSelectedRunId(run.id)
      setSelectedRun(run)
      setOffset(0)
      await loadRuns()
    } catch (exc: unknown) {
      setInputError(exc instanceof Error ? exc.message : t('workflows.invalidJson'))
    } finally {
      setSubmitting(false)
    }
  }

  const cancelRun = async (runId: string) => {
    setActionId(runId)
    try {
      const run = await cancelWorkflowRun(runId)
      setSelectedRun(run)
      await loadRuns()
    } finally {
      setActionId('')
    }
  }

  const beginStepEdit = (step: WorkflowStepRun) => {
    setEditingStepId(step.step_id)
    setStepInputText(formatJson(step.input || {}))
    setStepInputError('')
  }

  const retryStep = async (step: WorkflowStepRun, overrideInput?: Record<string, unknown>) => {
    if (!selectedRun) return
    setActionId(`${selectedRun.id}:${step.step_id}`)
    try {
      let updatedRun = selectedRun
      if (overrideInput) {
        updatedRun = await updateWorkflowStepInput(selectedRun.id, step.step_id, overrideInput)
      }
      updatedRun = await retryWorkflowStep(updatedRun.id, step.step_id)
      setSelectedRun(updatedRun)
      setEditingStepId('')
      await loadRuns()
    } finally {
      setActionId('')
    }
  }

  const saveInputAndRetry = async (step: WorkflowStepRun) => {
    setStepInputError('')
    try {
      await retryStep(step, parseJsonObject(stepInputText))
    } catch (exc: unknown) {
      setStepInputError(exc instanceof Error ? exc.message : t('workflows.invalidJson'))
    }
  }

  const resetFilters = () => {
    setStatus('')
    setDefinitionFilter('')
    setOffset(0)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-[var(--text-primary)]">
            {t('workflows.title')}
          </h1>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            {t('workflows.subtitle')}
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={loadRuns} disabled={loading}>
          <RefreshCw className={cn('mr-1.5 h-4 w-4', loading && 'animate-spin')} />
          {t('common.refresh')}
        </Button>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3">
          <p className="text-[11px] font-medium text-[var(--text-muted)]">
            {t('workflows.metric.definitions')}
          </p>
          <p className="mt-1 text-xl font-semibold text-[var(--text-primary)]">
            {definitions.length}
          </p>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3">
          <p className="text-[11px] font-medium text-[var(--text-muted)]">
            {t('workflows.metric.running')}
          </p>
          <p className="mt-1 text-xl font-semibold text-[var(--text-primary)]">
            {running}
          </p>
        </div>
        <div className="rounded-lg border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-3 sm:col-span-2">
          <p className="text-[11px] font-medium text-[var(--text-muted)]">
            {t('workflows.metric.selected')}
          </p>
          <p className="mt-1 truncate text-sm font-medium text-[var(--text-primary)]">
            {selectedDefinition?.name || '-'}
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-[minmax(420px,0.9fr)_minmax(520px,1.1fr)]">
        <section className="space-y-4">
          <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
            <div className="border-b border-[var(--border)] px-3 py-3">
              <h2 className="text-sm font-semibold text-[var(--text-primary)]">
                {t('workflows.startTitle')}
              </h2>
            </div>
            <div className="space-y-3 p-3">
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.definition')}
                </span>
                <select
                  className="control-surface"
                  value={selectedDefinition?.key || ''}
                  onChange={(event) => setDefinitionKey(event.target.value)}
                >
                  {definitions.map((definition) => (
                    <option key={`${definition.key}:${definition.version}`} value={definition.key}>
                      {definition.name} v{definition.version}
                    </option>
                  ))}
                </select>
              </label>
              {selectedDefinition?.description && (
                <p className="text-xs leading-5 text-[var(--text-muted)]">
                  {selectedDefinition.description}
                </p>
              )}
              <label className="block">
                <span className="mb-1.5 block text-xs font-medium text-[var(--text-muted)]">
                  {t('workflows.inputJson')}
                </span>
                <textarea
                  className="control-surface control-surface-mono min-h-56 resize-y"
                  value={inputText}
                  onChange={(event) => setInputText(event.target.value)}
                  spellCheck={false}
                />
              </label>
              {inputError && <p className="text-xs text-red-400">{inputError}</p>}
              <div className="flex items-center justify-between gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setInputText(formatJson(workflowInputForDefinition(selectedDefinition)))}
                >
                  <RotateCcw className="mr-1.5 h-4 w-4" />
                  {t('workflows.resetInput')}
                </Button>
                <Button onClick={startRun} disabled={!selectedDefinition || submitting}>
                  <Play className="mr-1.5 h-4 w-4" />
                  {submitting ? t('workflows.starting') : t('workflows.startRun')}
                </Button>
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
            <div className="flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-3 py-3">
              <select
                className="control-surface control-surface-compact w-auto min-w-40"
                value={definitionFilter}
                onChange={(event) => {
                  setDefinitionFilter(event.target.value)
                  setOffset(0)
                }}
              >
                <option value="">{t('workflows.allDefinitions')}</option>
                {definitions.map((definition) => (
                  <option key={`${definition.key}:filter`} value={definition.key}>
                    {definition.name}
                  </option>
                ))}
              </select>
              <select
                className="control-surface control-surface-compact w-auto min-w-40"
                value={status}
                onChange={(event) => {
                  setStatus(event.target.value)
                  setOffset(0)
                }}
              >
                {STATUS_OPTIONS.map((value) => (
                  <option key={value || 'all'} value={value}>
                    {value ? getWorkflowStatusText(value, language) : t('workflows.allStatuses')}
                  </option>
                ))}
              </select>
              {(status || definitionFilter) && (
                <Button variant="ghost" size="sm" onClick={resetFilters}>
                  {t('common.clear')}
                </Button>
              )}
            </div>

            <div className="glass-table-wrap">
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b border-[var(--border)] text-[var(--text-muted)]">
                    <th className="px-3 py-2 text-left">{t('workflows.run')}</th>
                    <th className="px-3 py-2 text-left">{t('common.status')}</th>
                    <th className="px-3 py-2 text-left">{t('workflows.currentStep')}</th>
                    <th className="px-3 py-2 text-left">{t('common.date')}</th>
                    <th className="px-3 py-2 text-right">{t('common.actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => (
                    <tr
                      key={run.id}
                      className={cn(
                        'border-b border-[var(--border-soft)] hover:bg-[var(--bg-hover)]',
                        selectedRunId === run.id && 'bg-[var(--accent-soft)]',
                      )}
                    >
                      <td className="px-3 py-3 align-top">
                        <button
                          className="max-w-[260px] truncate text-left text-sm font-medium text-[var(--text-primary)] hover:text-[var(--accent-strong)]"
                          onClick={() => setSelectedRunId(run.id)}
                        >
                          {run.name || run.definition_key}
                        </button>
                        <div className="mt-1 max-w-[260px] truncate font-mono text-xs text-[var(--text-muted)]">
                          {run.id}
                        </div>
                      </td>
                      <td className="px-3 py-3 align-top">
                        <Badge variant={WORKFLOW_STATUS_VARIANTS[run.status] || 'secondary'}>
                          {getWorkflowStatusText(run.status, language)}
                        </Badge>
                      </td>
                      <td className="px-3 py-3 align-top text-[var(--text-secondary)]">
                        {run.current_step_id || '-'}
                      </td>
                      <td className="px-3 py-3 align-top text-xs text-[var(--text-muted)]">
                        {formatMaybeDate(run.created_at, language)}
                      </td>
                      <td className="px-3 py-3 align-top">
                        <div className="flex justify-end gap-2">
                          <Button variant="outline" size="sm" onClick={() => setSelectedRunId(run.id)}>
                            {t('workflows.viewRun')}
                          </Button>
                          {CANCELLABLE_WORKFLOW_STATUSES.has(run.status) && (
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => cancelRun(run.id)}
                              disabled={actionId === run.id}
                              className="text-amber-500 hover:text-amber-400"
                            >
                              <Square className="mr-1 h-3.5 w-3.5" />
                              {actionId === run.id ? t('workflows.cancelling') : t('common.cancel')}
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!loading && runs.length === 0 && (
              <div className="empty-state-panel m-3">{t('workflows.emptyRuns')}</div>
            )}
            {loading && runs.length === 0 && (
              <div className="empty-state-panel m-3">{t('common.loading')}</div>
            )}
            <div className="flex items-center justify-between gap-3 border-t border-[var(--border)] px-3 py-3 text-xs text-[var(--text-muted)]">
              <span>{t('workflows.page', { current: currentPage, total: pages })}</span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset <= 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  {t('taskHistory.prevPage')}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  {t('taskHistory.nextPage')}
                </Button>
              </div>
            </div>
          </div>
        </section>

        <section className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-3 py-3">
            <div className="min-w-0">
              <h2 className="truncate text-sm font-semibold text-[var(--text-primary)]">
                {selectedRun?.name || t('workflows.runDetail')}
              </h2>
              <p className="mt-0.5 truncate font-mono text-xs text-[var(--text-muted)]">
                {selectedRun?.id || t('workflows.noRunSelected')}
              </p>
            </div>
            {selectedRun && (
              <Badge variant={WORKFLOW_STATUS_VARIANTS[selectedRun.status] || 'secondary'}>
                {getWorkflowStatusText(selectedRun.status, language)}
              </Badge>
            )}
          </div>

          {!selectedRun && (
            <div className="empty-state-panel m-3">{t('workflows.selectRun')}</div>
          )}

          {selectedRun && (
            <div className="space-y-4 p-3">
              {selectedRun.error && (
                <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
                  {selectedRun.error}
                </div>
              )}
              <div className="grid gap-2 sm:grid-cols-3">
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.createdAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.created_at, language)}
                  </p>
                </div>
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.startedAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.started_at, language)}
                  </p>
                </div>
                <div className="rounded-md border border-[var(--border-soft)] bg-[var(--bg-pane)]/45 px-3 py-2">
                  <p className="text-[11px] text-[var(--text-muted)]">{t('workflows.finishedAt')}</p>
                  <p className="mt-1 text-xs text-[var(--text-secondary)]">
                    {formatMaybeDate(selectedRun.finished_at, language)}
                  </p>
                </div>
              </div>

              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                    {t('workflows.steps')}
                  </h3>
                  {CANCELLABLE_WORKFLOW_STATUSES.has(selectedRun.status) && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => cancelRun(selectedRun.id)}
                      disabled={actionId === selectedRun.id}
                      className="text-amber-500 hover:text-amber-400"
                    >
                      <Square className="mr-1 h-3.5 w-3.5" />
                      {actionId === selectedRun.id ? t('workflows.cancelling') : t('common.cancel')}
                    </Button>
                  )}
                </div>
                <div className="overflow-hidden rounded-md border border-[var(--border-soft)]">
                  {(selectedRun.steps || []).map((step, index) => {
                    const Icon = stepIcon(step.status)
                    const taskUrl = externalTaskUrl(step)
                    const editing = editingStepId === step.step_id
                    const busy = actionId === `${selectedRun.id}:${step.step_id}`
                    return (
                      <div
                        key={step.id}
                        className={cn(
                          'border-b border-[var(--border-soft)] px-3 py-3 last:border-b-0',
                          step.status === 'needs_attention' && 'bg-amber-500/5',
                        )}
                      >
                        <div className="flex gap-3">
                          <div className="flex flex-col items-center">
                            <div className="flex h-8 w-8 items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-pane)]">
                              <Icon
                                className={cn(
                                  'h-4 w-4 text-[var(--text-muted)]',
                                  step.status === 'running' && 'animate-spin text-[var(--accent-strong)]',
                                  step.status === 'succeeded' && 'text-emerald-500',
                                  step.status === 'failed' && 'text-red-500',
                                  step.status === 'needs_attention' && 'text-amber-500',
                                )}
                              />
                            </div>
                            {index < (selectedRun.steps || []).length - 1 && (
                              <div className="my-1 h-8 w-px bg-[var(--border)]" />
                            )}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-medium text-[var(--text-primary)]">
                                {step.name || step.step_id}
                              </span>
                              <Badge variant={WORKFLOW_STATUS_VARIANTS[step.status] || 'secondary'}>
                                {getWorkflowStatusText(step.status, language)}
                              </Badge>
                              <span className="text-xs text-[var(--text-muted)]">
                                {t('workflows.attempt', {
                                  attempt: step.attempt,
                                  total: step.max_attempts,
                                })}
                              </span>
                            </div>
                            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--text-muted)]">
                              <span>{step.adapter_key}</span>
                              {step.next_run_at && (
                                <span>
                                  {t('workflows.nextRunAt')}: {formatMaybeDate(step.next_run_at, language)}
                                </span>
                              )}
                              {step.timeout_at && (
                                <span>
                                  {t('workflows.timeoutAt')}: {formatMaybeDate(step.timeout_at, language)}
                                </span>
                              )}
                              {taskUrl && (
                                <a
                                  href={taskUrl}
                                  className="inline-flex items-center gap-1 text-[var(--text-accent)] hover:underline"
                                >
                                  {t('workflows.childTask')}
                                  <ExternalLink className="h-3 w-3" />
                                </a>
                              )}
                            </div>
                            {shortError(step.error) && (
                              <p className="mt-2 text-xs text-red-400">{shortError(step.error)}</p>
                            )}
                            <details className="mt-2">
                              <summary className="cursor-pointer text-xs text-[var(--text-accent)]">
                                {t('workflows.stepData')}
                              </summary>
                              <pre className="mt-2 max-h-52 overflow-auto rounded-md bg-[var(--bg-input)] p-2 text-xs text-[var(--text-secondary)]">
                                {formatJson({ input: step.input, output: step.output, error: step.error })}
                              </pre>
                            </details>
                            {RETRYABLE_STEP_STATUSES.has(step.status) && (
                              <div className="mt-3 space-y-2">
                                {editing ? (
                                  <>
                                    <textarea
                                      className="control-surface control-surface-mono min-h-32 resize-y"
                                      value={stepInputText}
                                      onChange={(event) => setStepInputText(event.target.value)}
                                      spellCheck={false}
                                    />
                                    {stepInputError && (
                                      <p className="text-xs text-red-400">{stepInputError}</p>
                                    )}
                                    <div className="flex flex-wrap gap-2">
                                      <Button size="sm" onClick={() => saveInputAndRetry(step)} disabled={busy}>
                                        <RotateCcw className="mr-1.5 h-4 w-4" />
                                        {busy ? t('workflows.retrying') : t('workflows.saveInputAndRetry')}
                                      </Button>
                                      <Button variant="outline" size="sm" onClick={() => setEditingStepId('')}>
                                        {t('common.cancel')}
                                      </Button>
                                    </div>
                                  </>
                                ) : (
                                  <div className="flex flex-wrap gap-2">
                                    <Button variant="outline" size="sm" onClick={() => beginStepEdit(step)}>
                                      {t('workflows.editInput')}
                                    </Button>
                                    <Button
                                      variant="outline"
                                      size="sm"
                                      onClick={() => retryStep(step)}
                                      disabled={busy}
                                    >
                                      <RotateCcw className="mr-1.5 h-4 w-4" />
                                      {busy ? t('workflows.retrying') : t('workflows.retryStep')}
                                    </Button>
                                  </div>
                                )}
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>

              <div>
                <div className="mb-2 flex items-center gap-2">
                  <GitBranch className="h-4 w-4 text-[var(--text-muted)]" />
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">
                    {t('workflows.events')}
                  </h3>
                </div>
                <div className="max-h-64 overflow-auto rounded-md border border-[var(--border-soft)] bg-[var(--bg-input)]">
                  {events.length === 0 && (
                    <div className="px-3 py-6 text-center text-sm text-[var(--text-muted)]">
                      {t('workflows.emptyEvents')}
                    </div>
                  )}
                  {events.map((event) => (
                    <div
                      key={event.id}
                      className="border-b border-[var(--border-soft)] px-3 py-2 text-xs last:border-b-0"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-[var(--text-muted)]">
                          {formatMaybeDate(event.created_at, language)}
                        </span>
                        {event.step_id && (
                          <span className="rounded bg-[var(--chip-bg)] px-1.5 py-0.5 text-[var(--text-secondary)]">
                            {event.step_id}
                          </span>
                        )}
                        <span
                          className={cn(
                            'text-[var(--text-secondary)]',
                            event.level === 'error' && 'text-red-400',
                            event.level === 'warning' && 'text-amber-400',
                          )}
                        >
                          {event.message}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  )
}
