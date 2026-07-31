import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  Copy,
  ExternalLink,
  FileText,
  KeyRound,
  Link2,
  LoaderCircle,
  QrCode,
  RefreshCw,
  RotateCcw,
  ScanLine,
  Search,
  Settings2,
  X,
} from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { apiFetch, cn } from '@/lib/utils'

type SettingKind = 'supplier' | 'scanner' | 'scanner_546789'

type KakaoSetting = {
  id: number | null
  kind: SettingKind
  display_name: string
  base_url: string
  has_cdk: boolean
  cdk_count: number
  cdk_keys: string[]
  cdk_preview: string
  driver_type: string
}

type ScannerKind = 'scanner' | 'scanner_546789'

type KakaoSettings = Record<SettingKind, KakaoSetting> & {
  default_scanner_kind: ScannerKind
  auto_upload_after_extract: boolean
}

type Pipeline = {
  id?: number
  account_id?: number
  state: string
  payment_method?: string
  supplier_name?: string
  supplier_status: string
  supplier_order_id?: string
  supplier_stage?: number
  supplier_stage_total?: number
  supplier_stage_name?: string
  payment_url?: string
  scanner_driver?: string
  scanner_name?: string
  scanner_status: string
  scanner_order_id?: string
  scanner_subscription_status?: string
  scan_url?: string
  scan_expires_at?: string
  plus_status: string
  final_result: string
  last_error_code: string
  last_error_message: string
  events?: Array<{ time: string; level: string; message: string }>
  supplier_response?: Record<string, unknown>
  scanner_response?: Record<string, unknown>
}

type KakaoAccount = {
  id: number
  email: string
  plan: string
  plan_state: string
  validity: string
  checked_at?: string | null
  pipeline: Pipeline
}

type SettingDraft = {
  display_name: string
  base_url: string
  cdk_keys: string
}

type CdkCheckResult = {
  cdk_key: string
  status: 'valid' | 'depleted' | 'invalid'
  message: string
  product_type?: string
  cdk_status?: string
  total_count?: number
  used_count?: number
  frozen_count?: number
  available_count?: number
  expires_at?: string | null
  remark?: string
  unlimited?: boolean
}

const SETTING_KINDS: SettingKind[] = ['supplier', 'scanner', 'scanner_546789']
const SETTING_TITLES: Record<SettingKind, string> = {
  supplier: '提链供应商',
  scanner: 'I7wap 扫码',
  scanner_546789: '546789 扫码',
}

const PAGE_SIZE = 20
const ACTIVE_PIPELINE_STATES = new Set([
  'supplier_processing',
  'scanner_processing',
  'scanner_succeeded',
  'plus_checking',
  'plus_pending',
])

function parseError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || '操作失败')
  try {
    const payload = JSON.parse(message)
    const detail = payload?.detail
    if (typeof detail === 'string') return detail
    if (detail && typeof detail === 'object') return detail.message || detail.code || message
  } catch {
    // Keep the original response text.
  }
  return message
}

async function copyText(value: string) {
  if (!value) throw new Error('没有可复制的内容')
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(value)
      return
    } catch {
      // Fall back to a temporary textarea for restricted clipboard contexts.
    }
  }
  const element = document.createElement('textarea')
  element.value = value
  element.setAttribute('readonly', '')
  element.style.position = 'fixed'
  element.style.opacity = '0'
  document.body.appendChild(element)
  element.select()
  const copied = document.execCommand('copy')
  document.body.removeChild(element)
  if (!copied) throw new Error('复制失败，请手动复制')
}

function sessionTokenFromCookies(value: string) {
  const raw = String(value || '').trim()
  if (!raw) return ''
  try {
    const parsed = JSON.parse(raw)
    const cookies = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.cookies) ? parsed.cookies : [])
    const match = cookies.find((item: { name?: string; value?: string }) => (
      ['__Secure-next-auth.session-token', '__Secure-authjs.session-token'].includes(String(item?.name || ''))
    ))
    if (match?.value) return String(match.value)
  } catch {
    // Raw Cookie headers are handled below.
  }
  const match = raw.match(/(?:^|[;\s])(?:__Secure-next-auth\.session-token|__Secure-authjs\.session-token)=([^;\s]+)/)
  return match?.[1] ? decodeURIComponent(match[1]) : ''
}

function planBadge(account: KakaoAccount) {
  const plan = String(account.plan || '').toLowerCase()
  const subscribed = account.plan_state === 'subscribed' || ['plus', 'pro', 'team', 'business'].some(item => plan.includes(item))
  if (subscribed) return <Badge variant="success">PLUS</Badge>
  if (plan === 'free' || account.plan_state === 'free') return <Badge variant="secondary">FREE</Badge>
  return <Badge variant="secondary">{account.plan || '未检测'}</Badge>
}

function accountIsPlus(account: KakaoAccount) {
  const plan = String(account.plan || '').toLowerCase()
  return account.plan_state === 'subscribed' || ['plus', 'pro', 'team', 'business'].some(item => plan.includes(item))
}

function AccountMoreMenu({
  accountId,
  onCopyCredential,
  onForceReset,
  onCheckPlus,
  actionDisabled = false,
  onShowLog,
}: {
  accountId: number
  onCopyCredential: (accountId: number, credentialType: 'session' | 'access') => void
  onForceReset: () => void
  onCheckPlus: () => void
  actionDisabled?: boolean
  onShowLog?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 })
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const menuRef = useRef<HTMLDivElement | null>(null)
  const menuItemCount = 4 + (onShowLog ? 1 : 0)

  useEffect(() => {
    if (!open) return

    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect()
      if (!rect) return
      const width = 136
      const height = 20 + menuItemCount * 34
      const gap = 6
      const openUp = window.innerHeight - rect.bottom < height + 16 && rect.top > height
      setMenuPosition({
        top: openUp ? rect.top - height - gap : rect.bottom + gap,
        left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
      })
    }
    const closeOnOutsideClick = (event: MouseEvent) => {
      const target = event.target as Node
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return
      setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    document.addEventListener('mousedown', closeOnOutsideClick)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
      document.removeEventListener('mousedown', closeOnOutsideClick)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [menuItemCount, open])

  const copyCredential = (credentialType: 'session' | 'access') => {
    setOpen(false)
    onCopyCredential(accountId, credentialType)
  }

  return (
    <>
      <Button
        ref={triggerRef}
        variant="ghost"
        size="sm"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        更多 <ChevronDown className="ml-1 h-3.5 w-3.5" />
      </Button>
      {open && typeof document !== 'undefined' && createPortal(
        <div
          ref={menuRef}
          role="menu"
          className="fixed z-[60] w-[136px] overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-card)] py-1 shadow-[var(--shadow-soft)]"
          style={{ top: menuPosition.top, left: menuPosition.left }}
        >
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-hover)] focus-visible:bg-[var(--bg-hover)] focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onCheckPlus()
            }}
          >
            <Check className="mr-2 h-3.5 w-3.5" /> 检测 Plus
          </button>
          <button
            type="button"
            role="menuitem"
            disabled={actionDisabled}
            className="flex w-full items-center px-3 py-2 text-left text-xs font-medium text-red-500 transition-colors hover:bg-red-500/10 focus-visible:bg-red-500/10 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-40"
            onClick={() => {
              setOpen(false)
              onForceReset()
            }}
          >
            <RotateCcw className="mr-2 h-3.5 w-3.5" /> 重置记录
          </button>
          <div className="my-1 h-px bg-[var(--border)]" />
          <button
            type="button"
            role="menuitem"
            className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
            onClick={() => copyCredential('access')}
          >
            <Copy className="mr-2 h-3.5 w-3.5" /> 复制 AT
          </button>
          <button
            type="button"
            role="menuitem"
            className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
            onClick={() => copyCredential('session')}
          >
            <KeyRound className="mr-2 h-3.5 w-3.5" /> 复制 ST
          </button>
          {onShowLog ? (
            <button
              type="button"
              role="menuitem"
              className="flex w-full items-center px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:bg-[var(--bg-hover)]"
              onClick={() => {
                setOpen(false)
                onShowLog()
              }}
            >
              <FileText className="mr-2 h-3.5 w-3.5" /> 查看日志
            </button>
          ) : null}
        </div>,
        document.body,
      )}
    </>
  )
}

type StepState = 'waiting' | 'active' | 'complete' | 'error'

function PipelineProgress({ account }: { account: KakaoAccount }) {
  const pipeline = account.pipeline
  const alreadyPlus = accountIsPlus(account)
  const scannerComplete = ['scanner_succeeded', 'plus_checking', 'plus_pending', 'plus_check_failed', 'completed'].includes(pipeline.state)
  const steps: Array<{ label: string; state: StepState }> = [
    {
      label: '提链',
      state: pipeline.state === 'supplier_failed'
        ? 'error'
        : pipeline.payment_url
          ? 'complete'
          : ['supplier_submitting', 'supplier_processing'].includes(pipeline.state)
            ? 'active'
            : 'waiting',
    },
    {
      label: '扫码',
      state: pipeline.state === 'scanner_failed'
        ? 'error'
        : scannerComplete
          ? 'complete'
          : ['scanner_submitting', 'scanner_processing'].includes(pipeline.state)
            ? 'active'
            : 'waiting',
    },
    {
      label: 'Plus',
      state: pipeline.state === 'plus_check_failed'
        ? 'error'
        : pipeline.state === 'completed' || pipeline.final_result === 'plus' || alreadyPlus
          ? 'complete'
          : pipeline.state === 'plus_checking'
            ? 'active'
            : 'waiting',
    },
  ]

  const stateText = (() => {
    if (pipeline.state === 'completed' || pipeline.final_result === 'plus') return '已确认升级 Plus'
    if (alreadyPlus) return '账号当前已是 Plus'
    if (pipeline.state === 'supplier_processing') {
      const stage = pipeline.supplier_stage_total ? `${pipeline.supplier_stage || 0}/${pipeline.supplier_stage_total} ` : ''
      return `${stage}${pipeline.supplier_stage_name || pipeline.supplier_status || '供应商处理中'}`
    }
    if (pipeline.state === 'scanner_processing') return `${pipeline.scanner_name || '扫码平台'}处理中`
    if (pipeline.state === 'link_ready') return '长链已就绪，等待上传扫码'
    if (pipeline.state === 'scanner_succeeded' || pipeline.state === 'plus_pending') return '扫码成功，等待 Plus 同步'
    if (pipeline.state === 'plus_checking') return '正在复检 Plus 状态'
    if (['supplier_failed', 'scanner_failed', 'plus_check_failed'].includes(pipeline.state)) {
      return pipeline.last_error_message || '本次操作失败'
    }
    return '等待提取 Kakao 长链'
  })()

  return (
    <div className="min-w-[320px] max-w-[460px]">
      <div className="flex items-start" aria-label={`流水线状态：${stateText}`}>
        {steps.map((step, index) => (
          <div key={step.label} className="relative flex flex-1 flex-col items-center gap-1">
            {index > 0 ? (
              <div className={cn(
                'absolute right-1/2 top-[6px] h-px w-full',
                steps[index - 1].state === 'complete' ? 'bg-emerald-500/60' : 'bg-[var(--border)]',
              )} />
            ) : null}
            <span className={cn(
              'relative z-10 h-3 w-3 rounded-full ring-4 ring-[var(--bg-surface)]',
              step.state === 'complete' && 'bg-emerald-500',
              step.state === 'active' && 'bg-[var(--accent)]',
              step.state === 'error' && 'bg-red-500',
              step.state === 'waiting' && 'bg-[var(--border)]',
            )} />
            <span className={cn(
              'text-[11px]',
              step.state === 'complete' && 'text-emerald-500',
              step.state === 'active' && 'font-medium text-[var(--accent)]',
              step.state === 'error' && 'font-medium text-red-500',
              step.state === 'waiting' && 'text-[var(--text-muted)]',
            )}>{step.label}</span>
          </div>
        ))}
      </div>
      <p className={cn(
        'mt-2 truncate text-center text-xs',
        ['supplier_failed', 'scanner_failed', 'plus_check_failed'].includes(pipeline.state)
          ? 'text-red-500'
          : 'text-[var(--text-secondary)]',
      )} title={stateText}>{stateText}</p>
    </div>
  )
}

function PipelineLogDrawer({
  account,
  detail,
  onClose,
  onCopyLink,
}: {
  account: KakaoAccount
  detail?: Pipeline
  onClose: () => void
  onCopyLink: (value: string) => void
}) {
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  if (typeof document === 'undefined') return null

  return createPortal(
    <div className="fixed inset-0 z-[50]">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label="关闭日志"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="kakao-log-title"
        className="absolute inset-y-0 right-0 flex w-full max-w-3xl flex-col border-l border-[var(--border)] bg-[var(--bg-surface)] shadow-[-4px_0_8px_rgba(0,0,0,0.2)]"
      >
        <header className="flex shrink-0 items-start justify-between gap-4 border-b border-[var(--border)] px-5 py-4">
          <div className="min-w-0">
            <h2 id="kakao-log-title" className="text-base font-semibold text-[var(--text-primary)]">流水线日志</h2>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
              <span className="max-w-md truncate text-[var(--text-secondary)]" title={account.email}>{account.email}</span>
              <span className="font-mono">#{account.id}</span>
              {planBadge(account)}
            </div>
          </div>
          <Button variant="ghost" size="icon" autoFocus onClick={onClose} aria-label="关闭日志">
            <X className="h-4 w-4" />
          </Button>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          {!detail ? (
            <div className="space-y-3" aria-label="正在读取日志">
              <div className="h-4 w-32 animate-pulse rounded bg-[var(--chip-bg)]" />
              {Array.from({ length: 6 }).map((_, index) => (
                <div key={index} className="h-9 animate-pulse rounded-md bg-[var(--bg-hover)]" />
              ))}
            </div>
          ) : (
            <div className="space-y-6">
              <section>
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h3 className="text-sm font-semibold text-[var(--text-primary)]">操作记录</h3>
                  <span className="text-xs text-[var(--text-muted)]">{(detail.events || []).length} 条</span>
                </div>
                {(detail.events || []).length === 0 ? (
                  <p className="rounded-lg bg-[var(--bg-hover)] px-4 py-6 text-center text-sm text-[var(--text-muted)]">暂无日志</p>
                ) : (
                  <div className="divide-y divide-[var(--border-soft)] border-y border-[var(--border)]">
                    {[...(detail.events || [])].reverse().map((event, index) => (
                      <div key={`${event.time}-${index}`} className="grid grid-cols-[76px_minmax(0,1fr)] gap-3 py-3 text-xs">
                        <time className="font-mono text-[var(--text-muted)]">
                          {new Date(event.time).toLocaleTimeString()}
                        </time>
                        <span className={cn(
                          'leading-5',
                          event.level === 'error'
                            ? 'text-red-500'
                            : event.level === 'warning'
                              ? 'text-amber-500'
                              : 'text-[var(--text-secondary)]',
                        )}>{event.message}</span>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              {detail.payment_url ? (
                <section>
                  <h3 className="mb-2 text-sm font-semibold text-[var(--text-primary)]">Kakao 长链</h3>
                  <div className="flex min-w-0 items-center gap-2 rounded-lg bg-[var(--bg-input)] p-2">
                    <code className="min-w-0 flex-1 truncate px-1 text-xs text-[var(--text-secondary)]">{detail.payment_url}</code>
                    <Button variant="outline" size="sm" onClick={() => onCopyLink(detail.payment_url || '')}>
                      <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => window.open(detail.payment_url, '_blank', 'noopener,noreferrer')}>
                      <ExternalLink className="mr-1.5 h-3.5 w-3.5" /> 打开
                    </Button>
                  </div>
                </section>
              ) : null}

              <details className="border-t border-[var(--border)] pt-4">
                <summary className="cursor-pointer text-sm font-medium text-[var(--text-secondary)] hover:text-[var(--text-primary)]">
                  查看脱敏接口响应
                </summary>
                <div className="mt-3 grid gap-3 lg:grid-cols-2">
                  <div className="min-w-0">
                    <div className="mb-1 text-xs text-[var(--text-muted)]">供应商</div>
                    <pre className="max-h-80 overflow-auto rounded-md bg-[var(--bg-input)] p-3 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">
                      {JSON.stringify(detail.supplier_response || {}, null, 2)}
                    </pre>
                  </div>
                  <div className="min-w-0">
                    <div className="mb-1 text-xs text-[var(--text-muted)]">扫码平台</div>
                    <pre className="max-h-80 overflow-auto rounded-md bg-[var(--bg-input)] p-3 font-mono text-[11px] leading-5 text-[var(--text-secondary)]">
                      {JSON.stringify(detail.scanner_response || {}, null, 2)}
                    </pre>
                  </div>
                </div>
              </details>
            </div>
          )}
        </div>
      </aside>
    </div>,
    document.body,
  )
}

function SettingsPanel({
  settings,
  onClose,
  onSaved,
}: {
  settings: KakaoSettings
  onClose: () => void
  onSaved: () => Promise<void>
}) {
  const [drafts, setDrafts] = useState<Record<SettingKind, SettingDraft>>({
    supplier: {
      display_name: settings.supplier.display_name,
      base_url: settings.supplier.base_url,
      cdk_keys: settings.supplier.cdk_keys.join('\n'),
    },
    scanner: {
      display_name: settings.scanner.display_name,
      base_url: settings.scanner.base_url,
      cdk_keys: settings.scanner.cdk_keys.join('\n'),
    },
    scanner_546789: {
      display_name: settings.scanner_546789.display_name,
      base_url: settings.scanner_546789.base_url,
      cdk_keys: settings.scanner_546789.cdk_keys.join('\n'),
    },
  })
  const [busy, setBusy] = useState('')
  const [activeKind, setActiveKind] = useState<SettingKind>('supplier')
  const [feedback, setFeedback] = useState<Record<SettingKind, string>>({ supplier: '', scanner: '', scanner_546789: '' })
  const [checkResults, setCheckResults] = useState<Record<SettingKind, CdkCheckResult[]>>({ supplier: [], scanner: [], scanner_546789: [] })

  useEffect(() => {
    setDrafts({
      supplier: {
        display_name: settings.supplier.display_name,
        base_url: settings.supplier.base_url,
        cdk_keys: settings.supplier.cdk_keys.join('\n'),
      },
      scanner: {
        display_name: settings.scanner.display_name,
        base_url: settings.scanner.base_url,
        cdk_keys: settings.scanner.cdk_keys.join('\n'),
      },
      scanner_546789: {
        display_name: settings.scanner_546789.display_name,
        base_url: settings.scanner_546789.base_url,
        cdk_keys: settings.scanner_546789.cdk_keys.join('\n'),
      },
    })
  }, [settings])

  const update = (kind: SettingKind, key: keyof SettingDraft, value: string) => {
    setDrafts(current => ({ ...current, [kind]: { ...current[kind], [key]: value } }))
    if (key === 'cdk_keys') setCheckResults(current => ({ ...current, [kind]: [] }))
  }

  const selectDefaultScanner = async (scannerKind: ScannerKind) => {
    setBusy('select-default-scanner')
    try {
      await apiFetch('/kakao-pipeline/settings/default-scanner/select', {
        method: 'PUT',
        body: JSON.stringify({ scanner_kind: scannerKind }),
      })
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [scannerKind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const setAutoUpload = async (enabled: boolean) => {
    setBusy('set-auto-upload')
    try {
      await apiFetch('/kakao-pipeline/settings/options/auto-upload', {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
      })
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [settings.default_scanner_kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const save = async (kind: SettingKind) => {
    setBusy(`save-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      await apiFetch(`/kakao-pipeline/settings/${kind}`, {
        method: 'PUT',
        body: JSON.stringify(drafts[kind]),
      })
      setFeedback(current => ({ ...current, [kind]: '配置已保存' }))
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const checkCdks = async (kind: SettingKind) => {
    setBusy(`check-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      await apiFetch(`/kakao-pipeline/settings/${kind}`, {
        method: 'PUT',
        body: JSON.stringify(drafts[kind]),
      })
      const result = await apiFetch(`/kakao-pipeline/settings/${kind}/check-cdks`, {
        method: 'POST',
        body: JSON.stringify(drafts[kind]),
      })
      const removed = new Set<string>(Array.isArray(result.removed) ? result.removed : [])
      if (removed.size) {
        setDrafts(current => ({
          ...current,
          [kind]: {
            ...current[kind],
            cdk_keys: current[kind].cdk_keys
              .split(/\r?\n/)
              .map(item => item.trim())
              .filter(item => item && !removed.has(item))
              .join('\n'),
          },
        }))
      }
      const items = Array.isArray(result.items) ? result.items : []
      setCheckResults(current => ({ ...current, [kind]: items }))
      const valid = items.filter((item: { status: string }) => item.status === 'valid').length
      const invalid = items.length - valid - removed.size
      setFeedback(current => ({
        ...current,
        [kind]: kind !== 'supplier'
          ? `已保存并校验额度：可用 ${valid}，已用完并删除 ${removed.size}，无效/异常 ${invalid}。`
          : `已保存并校验：有效 ${valid}，已用完并删除 ${removed.size}，无效/异常 ${invalid}。提链接口不提供精确剩余次数。`,
      }))
      await onSaved()
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  const test = async (kind: SettingKind) => {
    setBusy(`test-${kind}`)
    setFeedback(current => ({ ...current, [kind]: '' }))
    try {
      const result = await apiFetch(`/kakao-pipeline/settings/${kind}/test`, {
        method: 'POST',
        body: JSON.stringify(drafts[kind]),
      })
      setFeedback(current => ({ ...current, [kind]: result.message || '连接成功' }))
    } catch (error) {
      setFeedback(current => ({ ...current, [kind]: parseError(error) }))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="mb-5 border border-[var(--border)] bg-[var(--bg-surface)]">
      <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-[var(--text-primary)]">Kakao 接口配置</h2>
          <p className="mt-0.5 text-xs text-[var(--text-muted)]">每行一个 CDK，配置在本机明文显示；确认用完后会自动从池中删除。</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-3">
          <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={settings.auto_upload_after_extract}
              disabled={Boolean(busy)}
              onChange={event => void setAutoUpload(event.target.checked)}
            />
            <span>提链成功后自动上传</span>
          </label>
          <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
            <span>默认扫码供应商</span>
            <select
              className="control-surface control-surface-compact min-w-36"
              value={settings.default_scanner_kind}
              disabled={Boolean(busy)}
              onChange={event => void selectDefaultScanner(event.target.value as ScannerKind)}
            >
              <option value="scanner">I7wap</option>
              <option value="scanner_546789">546789</option>
            </select>
          </label>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="关闭配置">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>
      <div className="grid lg:grid-cols-[220px_minmax(0,1fr)]">
        <nav className="flex gap-1 overflow-x-auto border-b border-[var(--border)] p-2 lg:flex-col lg:border-b-0 lg:border-r" aria-label="接口类型">
          {SETTING_KINDS.map(kind => {
            const setting = settings[kind]
            const selected = activeKind === kind
            return (
              <button
                key={kind}
                type="button"
                aria-current={selected ? 'page' : undefined}
                onClick={() => setActiveKind(kind)}
                className={cn(
                  'flex min-w-44 items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-left text-sm transition-colors lg:min-w-0',
                  selected
                    ? 'bg-[var(--bg-active)] font-medium text-[var(--text-primary)]'
                    : 'text-[var(--text-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]',
                )}
              >
                <span>{SETTING_TITLES[kind]}</span>
                <span className={cn(
                  'h-2 w-2 shrink-0 rounded-full',
                  setting.has_cdk ? 'bg-emerald-500' : 'bg-amber-500',
                )} aria-label={setting.has_cdk ? `${setting.cdk_count} 个 CDK` : 'CDK 未配置'} />
              </button>
            )
          })}
        </nav>
        {SETTING_KINDS.filter(kind => kind === activeKind).map(kind => {
          const title = SETTING_TITLES[kind]
          const setting = settings[kind]
          return (
            <section key={kind} className="min-w-0 p-4 lg:p-5">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h3 className="text-sm font-medium text-[var(--text-primary)]">{title}</h3>
                <Badge variant={setting.has_cdk ? 'success' : 'warning'}>
                  {setting.has_cdk ? `${setting.cdk_count} 个 CDK` : 'CDK 未配置'}
                </Badge>
              </div>
              <div className="space-y-3">
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">显示名称</span>
                  <input
                    className="control-surface control-surface-compact"
                    value={drafts[kind].display_name}
                    onChange={event => update(kind, 'display_name', event.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">接口地址</span>
                  <input
                    className="control-surface control-surface-compact control-surface-mono"
                    value={drafts[kind].base_url}
                    onChange={event => update(kind, 'base_url', event.target.value)}
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs text-[var(--text-secondary)]">{kind === 'scanner_546789' ? 'submit_cdk' : 'X-CDK-Key'}（每行一个，明文）</span>
                  <textarea
                    rows={5}
                    className="control-surface control-surface-mono min-h-28 resize-y px-3 py-2 text-xs"
                    value={drafts[kind].cdk_keys}
                    placeholder={'cdk-1\ncdk-2\ncdk-3'}
                    onChange={event => update(kind, 'cdk_keys', event.target.value)}
                    spellCheck={false}
                  />
                </label>
                {feedback[kind] && (
                  <p className={cn(
                    'text-xs',
                    feedback[kind].includes('成功') || feedback[kind].includes('保存') || feedback[kind].includes('校验完成')
                      ? 'text-emerald-500'
                      : 'text-red-500',
                  )}>
                    {feedback[kind]}
                  </p>
                )}
                {checkResults[kind].length > 0 && (
                  <div className="max-h-44 space-y-1 overflow-y-auto border border-[var(--border)] p-2">
                    {checkResults[kind].map(item => (
                      <div key={item.cdk_key} className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-2 text-[11px]">
                        <Badge variant={item.status === 'valid' ? 'success' : item.status === 'depleted' ? 'danger' : 'warning'}>
                          {item.status === 'valid' ? '有效' : item.status === 'depleted' ? '已用完' : '无效'}
                        </Badge>
                        <div className="min-w-0">
                          <div className="break-all font-mono text-[var(--text-primary)]">{item.cdk_key}</div>
                          <div className="mt-0.5 text-[var(--text-muted)]">{item.message}</div>
                          {typeof item.available_count === 'number' && (
                            <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[var(--text-secondary)]">
                              <span>可用 {item.available_count}</span>
                              <span>已用 {item.used_count ?? 0}</span>
                              <span>冻结 {item.frozen_count ?? 0}</span>
                              <span>总计 {item.total_count ?? 0}</span>
                              {item.product_type ? <span>{item.product_type}</span> : null}
                              {item.cdk_status ? <span>{item.cdk_status}</span> : null}
                              {item.unlimited ? <span>无限额度</span> : null}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => checkCdks(kind)}>
                    {busy === `check-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    校验 CDK
                  </Button>
                  <Button variant="outline" size="sm" disabled={Boolean(busy)} onClick={() => test(kind)}>
                    {busy === `test-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    测试连接
                  </Button>
                  <Button size="sm" disabled={Boolean(busy)} onClick={() => save(kind)}>
                    {busy === `save-${kind}` ? <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : null}
                    保存配置
                  </Button>
                </div>
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}

export default function KakaoPipeline() {
  const [settings, setSettings] = useState<KakaoSettings | null>(null)
  const [accounts, setAccounts] = useState<KakaoAccount[]>([])
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [pageLoading, setPageLoading] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [operations, setOperations] = useState<Record<number, string>>({})
  const [expanded, setExpanded] = useState<number | null>(null)
  const [details, setDetails] = useState<Record<number, Pipeline>>({})
  const [toast, setToast] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const activeRequests = useRef(new Set<number>())
  const loadRequestRef = useRef(0)
  const accountLoadInFlightRef = useRef(false)
  const hasLoadedAccountsRef = useRef(false)

  const loadSettings = useCallback(async () => {
    const result = await apiFetch('/kakao-pipeline/settings')
    setSettings(result)
  }, [])

  const loadAccounts = useCallback(async (showLoading = true) => {
    const requestId = ++loadRequestRef.current
    accountLoadInFlightRef.current = true
    const showSkeleton = showLoading && !hasLoadedAccountsRef.current
    if (showSkeleton) setLoading(true)
    if (showLoading && !showSkeleton) setPageLoading(true)
    try {
      const params = new URLSearchParams({
        page: String(page),
        pageSize: String(PAGE_SIZE),
      })
      if (debouncedSearch) params.set('search', debouncedSearch)
      const result = await apiFetch(`/kakao-pipeline/accounts?${params.toString()}`)
      if (requestId !== loadRequestRef.current) return
      setAccounts(Array.isArray(result.items) ? result.items : [])
      setTotal(Number(result.total || 0))
      hasLoadedAccountsRef.current = true
    } catch (error) {
      if (requestId !== loadRequestRef.current) return
      setToast({ type: 'error', text: parseError(error) })
    } finally {
      if (requestId === loadRequestRef.current && showLoading) {
        setLoading(false)
        setPageLoading(false)
      }
      if (requestId === loadRequestRef.current) accountLoadInFlightRef.current = false
    }
  }, [debouncedSearch, page])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1)
      setDebouncedSearch(search.trim())
    }, 350)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4500)
    return () => window.clearTimeout(timer)
  }, [toast])

  useEffect(() => {
    loadSettings().catch(error => {
      setToast({ type: 'error', text: parseError(error) })
    })
  }, [loadSettings])

  useEffect(() => {
    void loadAccounts()
  }, [loadAccounts])

  const run = useCallback(async (
    accountId: number,
    label: string,
    path: string,
    body: Record<string, unknown> = {},
    options?: { quiet?: boolean; checkPlusAfter?: boolean; refreshAfter?: boolean },
  ) => {
    if (activeRequests.current.has(accountId)) return null
    activeRequests.current.add(accountId)
    setOperations(current => ({ ...current, [accountId]: label }))
    try {
      const result = await apiFetch(path, { method: 'POST', body: JSON.stringify(body) })
      if (options?.checkPlusAfter && result?.state === 'scanner_succeeded') {
        await apiFetch(`/kakao-pipeline/accounts/${accountId}/plus/check`, {
          method: 'POST',
          body: JSON.stringify({ advance_pipeline: true }),
        })
      }
      if (options?.refreshAfter !== false) await loadAccounts(false)
      if (!options?.quiet) setToast({ type: 'success', text: `${label}完成` })
      return result
    } catch (error) {
      if (!options?.quiet) setToast({ type: 'error', text: parseError(error) })
      await Promise.all([
        options?.refreshAfter !== false ? loadAccounts(false) : Promise.resolve(),
        path.endsWith('/scanner') ? loadSettings() : Promise.resolve(),
      ])
      return null
    } finally {
      activeRequests.current.delete(accountId)
      if (!options?.quiet) {
        setDetails(current => {
          if (!(accountId in current)) return current
          const next = { ...current }
          delete next[accountId]
          return next
        })
      }
      setOperations(current => {
        const next = { ...current }
        delete next[accountId]
        return next
      })
    }
  }, [loadAccounts, loadSettings])

  useEffect(() => {
    const refreshVisiblePage = () => {
      if (document.visibilityState !== 'visible' || accountLoadInFlightRef.current) return
      void loadAccounts(false)
    }
    const timer = window.setInterval(refreshVisiblePage, 3000)
    document.addEventListener('visibilitychange', refreshVisiblePage)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshVisiblePage)
    }
  }, [loadAccounts])

  const toggleDetail = async (accountId: number) => {
    if (expanded === accountId) {
      setExpanded(null)
      return
    }
    setExpanded(accountId)
    if (details[accountId]) return
    try {
      const detail = await apiFetch(`/kakao-pipeline/accounts/${accountId}`)
      setDetails(current => ({ ...current, [accountId]: detail }))
    } catch (error) {
      setExpanded(null)
      setToast({ type: 'error', text: parseError(error) })
    }
  }

  const copyLink = async (value: string) => {
    try {
      await copyText(value)
      setToast({ type: 'success', text: 'Kakao 长链已复制' })
    } catch {
      setToast({ type: 'error', text: '复制失败，请打开详情手动复制' })
    }
  }

  const copyAccountCredential = async (accountId: number, credentialType: 'session' | 'access') => {
    try {
      const response = await apiFetch(`/accounts/${accountId}/credentials?scope=platform`)
      const items = Array.isArray(response?.items) ? response.items : []
      const keys = credentialType === 'session'
        ? ['session_token', 'sessionToken', 'session_cookie']
        : ['access_token', 'accessToken']
      const credential = items.find((item: { key?: string; value?: string }) => keys.includes(String(item.key || '')) && item.value)
      const cookieCredential = credentialType === 'session'
        ? items.find((item: { key?: string; value?: string }) => ['cookies', 'cookie'].includes(String(item.key || '')) && item.value)
        : null
      const value = credential?.value || sessionTokenFromCookies(cookieCredential?.value || '')
      if (!value) throw new Error(credentialType === 'session' ? '该账号没有 Session Token' : '该账号没有 Access Token')
      await copyText(value)
      setToast({ type: 'success', text: credentialType === 'session' ? 'Session Token 已复制' : 'Access Token 已复制' })
    } catch (error) {
      setToast({ type: 'error', text: parseError(error) })
    }
  }

  const renderActions = (account: KakaoAccount) => {
    const pipeline = account.pipeline
    const busy = operations[account.id]
    const supplierReady = Boolean(settings?.supplier.has_cdk)
    const scannerKind = settings?.default_scanner_kind || 'scanner'
    const selectedScanner = settings?.[scannerKind]
    const scannerReady = Boolean(selectedScanner?.has_cdk)

    const checkPlus = () => {
      void run(account.id, '检测 Plus', `/kakao-pipeline/accounts/${account.id}/plus/check`)
    }

    const forceReset = () => {
      const confirmed = window.confirm(
        `确认强制重置 ${account.email} 的 Kakao 流水线？正在执行或卡住的任务记录也会被清除。`,
      )
      if (!confirmed) return
      void run(
        account.id,
        '重置记录',
        `/kakao-pipeline/accounts/${account.id}/reset`,
        { force: true },
      )
    }

    if (busy) {
      return (
        <div className="flex items-center justify-end gap-1.5">
          <Button variant="outline" size="sm" disabled>
            <LoaderCircle className="mr-1.5 h-3.5 w-3.5 animate-spin" /> {busy}
          </Button>
          <AccountMoreMenu
            accountId={account.id}
            onCopyCredential={copyAccountCredential}
            onForceReset={forceReset}
            onCheckPlus={checkPlus}
            actionDisabled
          />
        </div>
      )
    }
    return (
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        {(pipeline.state === 'idle' || pipeline.state === 'supplier_failed') && !accountIsPlus(account) && (
          <Button
            size="sm"
            disabled={!supplierReady}
            title={supplierReady ? '调用供应商提取 Kakao 长链' : '请先配置供应商 CDK'}
            onClick={() => run(account.id, '提取链接', `/kakao-pipeline/accounts/${account.id}/extract`, {
              supplier_setting_id: settings?.supplier.id,
              payment_method: 'kakao_pay',
            })}
          >
            <Link2 className="mr-1.5 h-3.5 w-3.5" /> 提取 Kakao 链接
          </Button>
        )}
        {pipeline.state === 'supplier_processing' && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => run(account.id, '刷新提链', `/kakao-pipeline/accounts/${account.id}/supplier/poll`)}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新
          </Button>
        )}
        {['link_ready', 'scanner_failed'].includes(pipeline.state) && pipeline.payment_url && (
          <>
            <Button variant="outline" size="sm" onClick={() => copyLink(pipeline.payment_url || '')}>
              <Copy className="mr-1.5 h-3.5 w-3.5" /> 复制链接
            </Button>
            <Button
              size="sm"
              disabled={!scannerReady}
              title={scannerReady ? `上传到 ${selectedScanner?.display_name}` : `请先配置 ${selectedScanner?.display_name || '默认扫码供应商'} CDK`}
              onClick={() => {
                if (!window.confirm(`确认将 ${account.email} 的 Kakao 长链上传到 ${selectedScanner?.display_name}？`)) return
                void run(account.id, '上传扫码', `/kakao-pipeline/accounts/${account.id}/scanner`, {
                  scanner_setting_id: selectedScanner?.id,
                  scanner_kind: scannerKind,
                })
              }}
            >
              <ScanLine className="mr-1.5 h-3.5 w-3.5" /> 上传扫码
            </Button>
          </>
        )}
        {pipeline.state === 'scanner_processing' && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => run(
              account.id,
              '刷新扫码',
              `/kakao-pipeline/accounts/${account.id}/scanner/poll`,
              {},
              { checkPlusAfter: true },
            )}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新扫码
          </Button>
        )}
        {pipeline.scan_url && (
          <Button variant="outline" size="sm" onClick={() => window.open(pipeline.scan_url, '_blank', 'noopener,noreferrer')}>
            <QrCode className="mr-1.5 h-3.5 w-3.5" /> 扫码信息
          </Button>
        )}
        {['scanner_succeeded', 'plus_pending', 'plus_check_failed', 'completed'].includes(pipeline.state) || accountIsPlus(account) ? (
          <Button
            variant="outline"
            size="sm"
            onClick={() => run(
              account.id,
              '检测 Plus',
              `/kakao-pipeline/accounts/${account.id}/plus/check`,
              { advance_pipeline: true },
            )}
          >
            <Check className="mr-1.5 h-3.5 w-3.5" /> 检测 Plus
          </Button>
        ) : null}
        <AccountMoreMenu
          accountId={account.id}
          onCopyCredential={copyAccountCredential}
          onForceReset={forceReset}
          onCheckPlus={checkPlus}
          onShowLog={pipeline.state !== 'idle' ? () => toggleDetail(account.id) : undefined}
        />
      </div>
    )
  }

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const activeCount = accounts.filter(account => ACTIVE_PIPELINE_STATES.has(account.pipeline.state)).length
  const readyCount = accounts.filter(account => account.pipeline.state === 'link_ready').length
  const errorCount = accounts.filter(account => ['supplier_failed', 'scanner_failed', 'plus_check_failed'].includes(account.pipeline.state)).length
  const completedCount = accounts.filter(account => account.pipeline.state === 'completed' || accountIsPlus(account)).length
  const expandedAccount = expanded === null ? null : accounts.find(account => account.id === expanded) || null

  return (
    <div className="space-y-5">
      {expandedAccount ? (
        <PipelineLogDrawer
          account={expandedAccount}
          detail={details[expandedAccount.id]}
          onClose={() => setExpanded(null)}
          onCopyLink={copyLink}
        />
      ) : null}
      {toast && (
        <button
          type="button"
          aria-live="polite"
          onClick={() => setToast(null)}
          className={cn(
            'fixed right-5 top-5 z-[70] flex max-w-md items-center gap-2 rounded-lg border px-4 py-3 text-left text-sm shadow-[var(--shadow-hard)]',
            toast.type === 'success'
              ? 'border-emerald-500/30 bg-[var(--bg-card)] text-emerald-500'
              : 'border-red-500/30 bg-[var(--bg-card)] text-red-500',
          )}
        >
          {toast.type === 'success' ? <CheckCircle2 className="h-4 w-4 shrink-0" /> : <AlertTriangle className="h-4 w-4 shrink-0" />}
          {toast.text}
        </button>
      )}

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2.5">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-[var(--accent-soft)] text-[var(--accent)]">
              <ScanLine className="h-5 w-5" />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-[var(--text-primary)]">Kakao 流水线</h1>
              <p className="mt-0.5 text-sm text-[var(--text-muted)]">按账号完成提链、扫码与 Plus 复检。</p>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => loadAccounts()}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 刷新账号
          </Button>
          <Button variant={settingsOpen ? 'default' : 'outline'} size="sm" onClick={() => setSettingsOpen(value => !value)}>
            <Settings2 className="mr-1.5 h-3.5 w-3.5" /> 配置供应商
          </Button>
        </div>
      </header>

      {settings && settingsOpen && (
        <SettingsPanel settings={settings} onClose={() => setSettingsOpen(false)} onSaved={loadSettings} />
      )}

      {settings && (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-y border-[var(--border)] py-3 text-xs">
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', settings.supplier.has_cdk ? 'bg-emerald-500' : 'bg-amber-500')} />
            <span className="text-[var(--text-muted)]">提链</span>
            <span className="font-medium text-[var(--text-primary)]">{settings.supplier.display_name}</span>
            <span className="text-[var(--text-secondary)]">{settings.supplier.has_cdk ? `${settings.supplier.cdk_count} 个 CDK` : '未配置 CDK'}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className={cn('h-2 w-2 rounded-full', settings[settings.default_scanner_kind].has_cdk ? 'bg-emerald-500' : 'bg-amber-500')} />
            <span className="text-[var(--text-muted)]">扫码</span>
            <span className="font-medium text-[var(--text-primary)]">{settings[settings.default_scanner_kind].display_name}</span>
            <span className="text-[var(--text-secondary)]">
              {settings[settings.default_scanner_kind].has_cdk ? `${settings[settings.default_scanner_kind].cdk_count} 个 CDK` : '未配置 CDK'}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[var(--text-muted)]">提链后</span>
            <span className="font-medium text-[var(--text-primary)]">{settings.auto_upload_after_extract ? '自动上传扫码' : '人工确认上传'}</span>
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--text-muted)]" />
          <input
            className="control-surface pl-9"
            placeholder="搜索本地 ChatGPT 账号"
            value={search}
            onChange={event => setSearch(event.target.value)}
          />
        </div>
        <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-xs">
          <span className="text-[var(--text-muted)]">本页</span>
          {activeCount > 0 ? <span className="text-[var(--accent)]">处理中 {activeCount}</span> : null}
          {readyCount > 0 ? <span className="text-amber-500">待上传 {readyCount}</span> : null}
          {errorCount > 0 ? <span className="text-red-500">异常 {errorCount}</span> : null}
          {completedCount > 0 ? <span className="text-emerald-500">已完成 {completedCount}</span> : null}
          <span className="text-[var(--text-muted)]">共 {total} 个账号</span>
        </div>
      </div>

      <div className="overflow-hidden border border-[var(--border)] bg-[var(--bg-surface)]">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px] table-fixed text-left" aria-busy={loading || pageLoading}>
            <colgroup>
              <col className="w-[30%]" />
              <col className="w-[38%]" />
              <col className="w-[32%]" />
            </colgroup>
            <thead className="border-b border-[var(--border)] bg-[var(--bg-pane)] text-[var(--text-muted)]">
              <tr>
                <th className="px-4 py-3">账号</th>
                <th className="px-4 py-3 text-center">流程进度</th>
                <th className="px-4 py-3 text-right">下一步</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border-soft)]">
              {loading ? (
                Array.from({ length: 6 }).map((_, index) => (
                  <tr key={index}>
                    {Array.from({ length: 3 }).map((__, cell) => (
                      <td key={cell} className="px-4 py-4">
                        <div className="h-4 animate-pulse rounded bg-[var(--chip-bg)]" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : accounts.length === 0 ? (
                <tr>
                  <td colSpan={3} className="px-4 py-14 text-center text-sm text-[var(--text-muted)]">
                    没有找到本地 ChatGPT 账号。
                  </td>
                </tr>
              ) : accounts.map(account => (
                    <tr key={account.id} className={cn(
                      'align-top transition-colors hover:bg-[var(--bg-hover)]',
                      account.pipeline.state === 'supplier_failed' || account.pipeline.state === 'scanner_failed'
                        ? 'bg-red-500/[0.025]'
                        : account.pipeline.state === 'completed'
                          ? 'bg-emerald-500/[0.025]'
                          : '',
                    )}>
                      <td className="px-4 py-4">
                        <div className="max-w-full truncate text-sm font-medium text-[var(--text-primary)]" title={account.email}>
                          {account.email}
                        </div>
                        <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px] text-[var(--text-muted)]">
                          <span className="font-mono">#{account.id}</span>
                          {planBadge(account)}
                          <span>{account.validity === 'valid' ? '账号有效' : `有效性：${account.validity || '未检测'}`}</span>
                        </div>
                      </td>
                      <td className="px-4 py-4"><PipelineProgress account={account} /></td>
                      <td className="px-4 py-4">{renderActions(account)}</td>
                    </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center justify-between border-t border-[var(--border)] px-4 py-3">
          <span className="flex items-center gap-1.5 text-xs text-[var(--text-muted)]" role="status" aria-live="polite">
            {pageLoading ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : null}
            {pageLoading ? `正在加载第 ${page} 页` : `第 ${page} / ${totalPages} 页`}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={pageLoading || page <= 1} onClick={() => setPage(value => value - 1)}>上一页</Button>
            <Button variant="outline" size="sm" disabled={pageLoading || page >= totalPages} onClick={() => setPage(value => value + 1)}>下一页</Button>
          </div>
        </div>
      </div>
    </div>
  )
}
