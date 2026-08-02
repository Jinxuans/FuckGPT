import { translate, type Language } from '@/lib/i18n'
import { apiFetch } from '@/lib/utils'

export type WorkflowDefinition = {
  id: number
  key: string
  version: number
  name: string
  description: string
  enabled: boolean
  definition: {
    steps?: WorkflowDefinitionStep[]
    [key: string]: unknown
  }
  created_at?: string
  updated_at?: string
}

export type WorkflowDefinitionStep = {
  id: string
  name?: string
  uses: string
  needs?: string[]
  timeout?: string
  max_attempts?: number
}

export type WorkflowStepRun = {
  id: string
  workflow_run_id: string
  step_id: string
  name: string
  adapter_key: string
  status: string
  attempt: number
  max_attempts: number
  input: Record<string, unknown>
  output: Record<string, unknown>
  error: Record<string, unknown>
  external_ref: string
  idempotency_key: string
  next_run_at?: string
  timeout_at?: string
  started_at?: string
  finished_at?: string
  created_at?: string
  updated_at?: string
}

export type WorkflowRun = {
  id: string
  definition_key: string
  definition_version: number
  name: string
  status: string
  terminal: boolean
  input: Record<string, unknown>
  context?: Record<string, unknown>
  output?: Record<string, unknown>
  definition?: Record<string, unknown>
  current_step_id: string
  error: string
  cancellation_requested_at?: string
  started_at?: string
  finished_at?: string
  created_at?: string
  updated_at?: string
  steps?: WorkflowStepRun[]
}

export type WorkflowEvent = {
  id: number
  workflow_run_id: string
  step_id: string
  type: string
  level: string
  message: string
  line: string
  detail: Record<string, unknown>
  created_at?: string
}

export const WORKFLOW_STATUS_VARIANTS: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'secondary'> = {
  pending: 'secondary',
  ready: 'secondary',
  running: 'default',
  waiting_external: 'warning',
  retry_scheduled: 'warning',
  needs_attention: 'warning',
  cancel_requested: 'warning',
  succeeded: 'success',
  failed: 'danger',
  skipped: 'secondary',
  cancelled: 'warning',
}

export const CANCELLABLE_WORKFLOW_STATUSES = new Set([
  'pending',
  'running',
  'waiting_external',
  'retry_scheduled',
  'needs_attention',
  'cancel_requested',
])

export const RETRYABLE_STEP_STATUSES = new Set([
  'failed',
  'needs_attention',
  'cancelled',
])

export function getWorkflowStatusText(status: string, language?: Language) {
  switch (status) {
    case 'pending':
      return translate('workflowStatus.pending', language)
    case 'ready':
      return translate('workflowStatus.ready', language)
    case 'running':
      return translate('workflowStatus.running', language)
    case 'waiting_external':
      return translate('workflowStatus.waiting_external', language)
    case 'retry_scheduled':
      return translate('workflowStatus.retry_scheduled', language)
    case 'needs_attention':
      return translate('workflowStatus.needs_attention', language)
    case 'cancel_requested':
      return translate('workflowStatus.cancel_requested', language)
    case 'succeeded':
      return translate('workflowStatus.succeeded', language)
    case 'failed':
      return translate('workflowStatus.failed', language)
    case 'skipped':
      return translate('workflowStatus.skipped', language)
    case 'cancelled':
      return translate('workflowStatus.cancelled', language)
    default:
      return status || '-'
  }
}

export async function fetchWorkflowDefinitions() {
  const data = await apiFetch('/workflows/definitions')
  return (data.items || []) as WorkflowDefinition[]
}

export async function fetchWorkflowRuns(params: {
  limit: number
  offset: number
  status?: string
  definition_key?: string
}) {
  const search = new URLSearchParams({
    limit: String(params.limit),
    offset: String(params.offset),
  })
  if (params.status) search.set('status', params.status)
  if (params.definition_key) search.set('definition_key', params.definition_key)
  return apiFetch(`/workflows/runs?${search.toString()}`) as Promise<{
    items: WorkflowRun[]
    total: number
    running: number
    limit: number
    offset: number
  }>
}

export async function createWorkflowRun(payload: {
  definition_key: string
  version?: number
  name?: string
  input: Record<string, unknown>
}) {
  return apiFetch('/workflows/runs', {
    method: 'POST',
    body: JSON.stringify(payload),
  }) as Promise<WorkflowRun>
}

export async function fetchWorkflowRun(runId: string) {
  return apiFetch(`/workflows/runs/${runId}`) as Promise<WorkflowRun>
}

export async function fetchWorkflowEvents(runId: string, since = 0) {
  const search = new URLSearchParams({ since: String(since), limit: '200' })
  const data = await apiFetch(`/workflows/runs/${runId}/events?${search.toString()}`)
  return data as { items: WorkflowEvent[]; cursor: number }
}

export async function cancelWorkflowRun(runId: string) {
  return apiFetch(`/workflows/runs/${runId}/cancel`, { method: 'POST' }) as Promise<WorkflowRun>
}

export async function retryWorkflowStep(runId: string, stepId: string) {
  return apiFetch(`/workflows/runs/${runId}/steps/${stepId}/retry`, { method: 'POST' }) as Promise<WorkflowRun>
}

export async function updateWorkflowStepInput(
  runId: string,
  stepId: string,
  input: Record<string, unknown>,
) {
  return apiFetch(`/workflows/runs/${runId}/steps/${stepId}/input`, {
    method: 'PATCH',
    body: JSON.stringify({ input }),
  }) as Promise<WorkflowRun>
}
