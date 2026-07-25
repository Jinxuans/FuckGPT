import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { RefreshCw, X } from "lucide-react";
import { apiFetch } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type MailboxAccount = {
  id: string;
  provider: string;
  email: string;
  login_account?: string;
  status?: string;
  credentials?: Record<string, any>;
  capabilities?: Record<string, any>;
  usage?: Record<string, any>;
  metadata?: Record<string, any>;
  updated_at?: string;
};

type MailboxAddress = {
  id: string;
  mailbox_account_id: string;
  address: string;
  address_type?: string;
  status?: string;
  reserved?: boolean;
  reserved_for?: Record<string, any>;
  metadata?: Record<string, any>;
  updated_at?: string;
};

type AccountMailboxLink = {
  id: string;
  platform: string;
  account_id: number;
  account_email?: string;
  mailbox_address_id: string;
  mailbox_account_id: string;
  purpose?: string;
  status?: string;
  created_at?: string;
};

type MailboxResource = {
  id: string;
  resource_kind: "address";
  mailbox_account_id: string;
  mailbox_address_id: string;
  address: string;
  address_type?: string;
  provider: string;
  parent_email?: string;
  login_account?: string;
  status: string;
  mailbox_status?: string;
  reserved?: boolean;
  reserved_for?: Record<string, any>;
  usage?: Record<string, any>;
  chatgpt_account_id?: number | null;
  chatgpt_account_email?: string;
  link_id?: string;
  updated_at?: string;
  created_at?: string;
};

type MailMessage = {
  id?: string;
  subject?: string;
  from?: string;
  to?: string[];
  received_at?: string;
  preview?: string;
  code?: string;
  link?: string;
  folder?: string;
  provider?: string;
};

type MailboxPayload = {
  resources: MailboxResource[];
  accounts: MailboxAccount[];
  addresses: MailboxAddress[];
  links: AccountMailboxLink[];
  paths?: Record<string, string>;
};

const statusOptions = [
  { value: "all", label: "全部状态" },
  { value: "available", label: "空闲" },
  { value: "allocated", label: "已分配" },
  { value: "registered", label: "已注册" },
  { value: "disabled", label: "禁用" },
];

function statusLabel(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "registered") return "已注册";
  if (normalized === "allocated") return "已分配";
  if (normalized === "available") return "空闲";
  if (normalized === "disabled" || normalized === "inactive") return "禁用";
  if (normalized === "error") return "异常";
  return status || "未知";
}

function statusPillClass(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "registered") return "bg-emerald-500/10 text-emerald-500 ring-emerald-500/20";
  if (normalized === "allocated") return "bg-amber-500/10 text-amber-500 ring-amber-500/20";
  if (normalized === "disabled" || normalized === "inactive" || normalized === "error") return "bg-red-500/10 text-red-500 ring-red-500/20";
  if (normalized === "available") return "bg-blue-500/10 text-blue-500 ring-blue-500/20";
  return "bg-[var(--text-primary)]/5 text-[var(--text-secondary)] ring-[var(--border)]";
}

function statusDotClass(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "registered") return "bg-emerald-500 shadow-[0_0_4px_rgba(16,185,129,0.6)]";
  if (normalized === "allocated") return "bg-amber-500 shadow-[0_0_4px_rgba(245,158,11,0.6)]";
  if (normalized === "disabled" || normalized === "inactive" || normalized === "error") return "bg-red-500 shadow-[0_0_4px_rgba(239,68,68,0.6)]";
  if (normalized === "available") return "bg-blue-500";
  return "bg-gray-400";
}

function providerLabel(provider?: string) {
  if (provider === "local_ms_pool") return "local_ms";
  return provider || "-";
}

function formatTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatCompactTime(value?: string) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function jsonBlock(value: unknown) {
  return JSON.stringify(value ?? {}, null, 2);
}

function StatusPill({ status }: { status?: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${statusPillClass(status)}`}>
      <span className={`mr-1 h-1 w-1 rounded-full ${statusDotClass(status)}`} />
      {statusLabel(status)}
    </span>
  );
}

function ActionMenu({
  resource,
  onDetail,
  onAction,
}: {
  resource: MailboxResource;
  onDetail: () => void;
  onAction: (resource: MailboxResource, action: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0, maxHeight: 320 });
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const update = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const width = 180;
      const gap = 6;
      const desiredHeight = 260;
      const spaceBelow = window.innerHeight - rect.bottom - 16;
      const spaceAbove = rect.top - 16;
      const openUp = spaceBelow < desiredHeight && spaceAbove > spaceBelow;
      const maxHeight = Math.max(160, Math.min(desiredHeight, openUp ? spaceAbove : spaceBelow));
      setMenuPosition({
        top: openUp ? Math.max(8, rect.top - maxHeight - gap) : rect.bottom + gap,
        left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
        maxHeight,
      });
    };
    const close = (event: MouseEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    document.addEventListener("mousedown", close);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      document.removeEventListener("mousedown", close);
    };
  }, [open]);

  const run = (action: string) => {
    setOpen(false);
    onAction(resource, action);
  };

  return (
    <div className="flex items-center justify-end gap-2">
      <button onClick={onDetail} className="table-action-btn">详情</button>
      <button ref={triggerRef} onClick={() => setOpen((value) => !value)} className="table-action-btn">更多 ▾</button>
      {open && typeof document !== "undefined" && createPortal(
        <div
          ref={menuRef}
          className="fixed z-[9999] w-[180px] overflow-y-auto rounded-2xl border border-[var(--border)] bg-[var(--bg-card)]/96 py-1.5 shadow-[var(--shadow-soft)]"
          style={{ top: menuPosition.top, left: menuPosition.left, maxHeight: menuPosition.maxHeight }}
        >
          <button onClick={() => run("copy")} className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
            复制邮箱
          </button>
          <button onClick={() => run("messages")} className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
            获取邮件
          </button>
          <button
            onClick={() => run("bind")}
            disabled={Boolean(resource.chatgpt_account_id)}
            className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-40"
          >
            绑定账号
          </button>
          <button
            onClick={() => run("unlink")}
            disabled={!resource.chatgpt_account_id}
            className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-40"
          >
            解绑账号
          </button>
          <button
            onClick={() => run("release")}
            disabled={!resource.reserved || resource.status === "registered"}
            className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)] disabled:opacity-40"
          >
            释放邮箱
          </button>
          <button onClick={() => run("disable")} className="w-full px-3 py-2 text-left text-xs text-[var(--text-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--text-primary)]">
            禁用邮箱
          </button>
          <div className="my-1 border-t border-[var(--border)]/70" />
          <button onClick={() => run("delete")} className="w-full px-3 py-2 text-left text-xs text-[#f0b0b0] transition-colors hover:bg-[rgba(239,68,68,0.08)] hover:text-[#ffd5d5]">
            删除记录
          </button>
        </div>,
        document.body,
      )}
    </div>
  );
}

function MailMessagesModal({
  resource,
  messages,
  loading,
  error,
  onRefresh,
  onClose,
}: {
  resource: MailboxResource;
  messages: MailMessage[];
  loading: boolean;
  error: string;
  onRefresh: () => void;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-lg flex flex-col" style={{ maxHeight: "90vh" }} onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4 shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">邮件列表</h2>
            <p className="mt-0.5 break-all text-xs text-[var(--text-muted)]">{resource.address}</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}>
              {loading ? "获取中" : "重新获取"}
            </Button>
            <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          {error ? (
            <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
              {error}
            </div>
          ) : null}
          {!error && loading && messages.length === 0 ? (
            <div className="empty-state-panel">正在获取邮件...</div>
          ) : null}
          {!error && !loading && messages.length === 0 ? (
            <div className="empty-state-panel">没有获取到邮件。</div>
          ) : null}
          {messages.length > 0 ? (
            <div className="space-y-3">
              {messages.map((message, index) => (
                <div key={message.id || index} className="rounded-lg border border-[var(--border)] bg-[var(--bg-hover)] p-3">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium text-[var(--text-primary)]" title={message.subject || ""}>
                        {message.subject || "(无主题)"}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-2 text-xs text-[var(--text-muted)]">
                        <span>{message.from || "未知发件人"}</span>
                        <span>{formatTime(message.received_at)}</span>
                        {message.folder ? <span>{message.folder}</span> : null}
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {message.code ? <span className="rounded border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-500">code {message.code}</span> : null}
                      {message.link ? <span className="rounded border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-xs text-blue-500">link</span> : null}
                    </div>
                  </div>
                  {message.to?.length ? (
                    <div className="mt-2 truncate text-xs text-[var(--text-muted)]" title={message.to.join(", ")}>
                      To: {message.to.join(", ")}
                    </div>
                  ) : null}
                  <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-[var(--border)] bg-[var(--bg-input)] p-2 text-xs text-[var(--text-secondary)]">
                    {message.preview || ""}
                  </pre>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function DetailModal({
  resource,
  account,
  address,
  link,
  onClose,
}: {
  resource: MailboxResource;
  account: MailboxAccount | null;
  address: MailboxAddress | null;
  link: AccountMailboxLink | null;
  onClose: () => void;
}) {
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <div className="dialog-panel dialog-panel-lg flex flex-col" style={{ maxHeight: "90vh" }} onClick={(event) => event.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-[var(--border)] px-6 py-4 shrink-0">
          <div>
            <h2 className="text-base font-semibold text-[var(--text-primary)]">邮箱详情</h2>
            <p className="mt-0.5 break-all text-xs text-[var(--text-muted)]">{resource.address}</p>
          </div>
          <button onClick={onClose} className="text-[var(--text-muted)] hover:text-[var(--text-primary)]">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
          <div className="grid gap-4 lg:grid-cols-[0.75fr_1.25fr]">
            <div className="space-y-4">
              <div className="rounded-lg border border-[var(--border)] bg-[var(--bg-hover)] p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <StatusPill status={resource.status} />
                  <span className="text-xs text-[var(--text-muted)]">{providerLabel(resource.provider)}</span>
                </div>
                <div className="grid gap-3 text-sm">
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">邮箱号</div>
                    <div className="mt-1 break-all font-mono text-[var(--text-primary)]">{resource.address}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">来源账号</div>
                    <div className="mt-1 break-all text-[var(--text-primary)]">{resource.parent_email || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">登录账号</div>
                    <div className="mt-1 break-all text-[var(--text-primary)]">{resource.login_account || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-[var(--text-muted)]">ChatGPT 账号</div>
                    <div className="mt-1 text-[var(--text-primary)]">
                      {resource.chatgpt_account_id ? `#${resource.chatgpt_account_id}` : "未绑定"}
                    </div>
                    {resource.chatgpt_account_email ? (
                      <div className="mt-1 break-all text-xs text-[var(--text-muted)]">{resource.chatgpt_account_email}</div>
                    ) : null}
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <div className="text-xs text-[var(--text-muted)]">地址类型</div>
                      <div className="mt-1 text-[var(--text-primary)]">{resource.address_type || "primary"}</div>
                    </div>
                    <div>
                      <div className="text-xs text-[var(--text-muted)]">更新时间</div>
                      <div className="mt-1 text-[var(--text-primary)]">{formatTime(resource.updated_at || resource.created_at)}</div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <details open className="rounded-lg border border-[var(--border)] bg-[var(--bg-card)]">
              <summary className="cursor-pointer px-3 py-2 text-sm font-medium text-[var(--text-primary)]">原始数据</summary>
              <div className="grid gap-3 border-t border-[var(--border)] p-3 md:grid-cols-3">
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">account</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(account)}</pre>
                </div>
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">address</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(address)}</pre>
                </div>
                <div>
                  <div className="mb-1 text-xs text-[var(--text-muted)]">link</div>
                  <pre className="control-surface control-surface-mono max-h-80 overflow-auto whitespace-pre-wrap">{jsonBlock(link)}</pre>
                </div>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function MailboxResources() {
  const [payload, setPayload] = useState<MailboxPayload>({
    resources: [],
    accounts: [],
    addresses: [],
    links: [],
  });
  const [statusFilter, setStatusFilter] = useState("all");
  const [providerFilter, setProviderFilter] = useState("all");
  const [detail, setDetail] = useState<MailboxResource | null>(null);
  const [mailDialog, setMailDialog] = useState<{ resource: MailboxResource; messages: MailMessage[]; loading: boolean; error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const accountById = useMemo(() => new Map(payload.accounts.map((item) => [item.id, item])), [payload.accounts]);
  const addressById = useMemo(() => new Map(payload.addresses.map((item) => [item.id, item])), [payload.addresses]);
  const linkByAddressId = useMemo(() => new Map(payload.links.map((item) => [item.mailbox_address_id, item])), [payload.links]);

  const providers = useMemo(() => {
    return Array.from(new Set(payload.resources.map((item) => item.provider).filter(Boolean))).sort();
  }, [payload.resources]);

  const counts = useMemo(() => {
    return payload.resources.reduce<Record<string, number>>((acc, item) => {
      acc[item.status] = (acc[item.status] || 0) + 1;
      return acc;
    }, {});
  }, [payload.resources]);

  const visibleResources = useMemo(() => {
    return payload.resources.filter((item) => {
      if (statusFilter !== "all" && item.status !== statusFilter) return false;
      if (providerFilter !== "all" && item.provider !== providerFilter) return false;
      return true;
    });
  }, [payload.resources, providerFilter, statusFilter]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch("/mailboxes");
      setPayload({
        resources: Array.isArray(data?.resources) ? data.resources : [],
        accounts: Array.isArray(data?.accounts) ? data.accounts : [],
        addresses: Array.isArray(data?.addresses) ? data.addresses : [],
        links: Array.isArray(data?.links) ? data.links : [],
        paths: data?.paths || {},
      });
    } catch (exc: any) {
      setError(exc?.message || "加载邮箱资源失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const copyAddress = async (address: string) => {
    if (!address || !navigator?.clipboard) return;
    await navigator.clipboard.writeText(address);
  };

  const releaseAddress = async (resource: MailboxResource) => {
    if (!resource.mailbox_address_id) return;
    if (!confirm(`释放邮箱 ${resource.address}？`)) return;
    await apiFetch(`/mailboxes/addresses/${resource.mailbox_address_id}/release`, { method: "POST" });
    await load();
  };

  const unlinkResource = async (resource: MailboxResource) => {
    const link = linkByAddressId.get(resource.mailbox_address_id);
    if (!link) return;
    if (!confirm(`解绑 ${resource.address} 与 ChatGPT #${link.account_id}？`)) return;
    await apiFetch(`/mailboxes/accounts/${link.account_id}/link?platform=${encodeURIComponent(link.platform)}&purpose=${encodeURIComponent(link.purpose || "verification")}`, {
      method: "DELETE",
    });
    await load();
  };

  const bindResource = async (resource: MailboxResource) => {
    if (!resource.mailbox_address_id) return;
    const accountId = window.prompt("ChatGPT 账号 ID");
    if (!accountId) return;
    const accountEmail = window.prompt("ChatGPT 账号邮箱，可留空") || "";
    await apiFetch("/mailboxes/account-link", {
      method: "POST",
      body: JSON.stringify({
        platform: "chatgpt",
        account_id: Number(accountId),
        account_email: accountEmail,
        mailbox_address_id: resource.mailbox_address_id,
        purpose: "verification",
      }),
    });
    await load();
  };

  const disableMailbox = async (resource: MailboxResource) => {
    if (!confirm(`禁用邮箱来源 ${resource.parent_email || resource.address}？`)) return;
    await apiFetch(`/mailboxes/accounts/${resource.mailbox_account_id}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "disabled" }),
    });
    await load();
  };

  const deleteMailbox = async (resource: MailboxResource) => {
    if (!confirm(`删除邮箱来源 ${resource.parent_email || resource.address}？该来源下地址和绑定都会删除。`)) return;
    await apiFetch(`/mailboxes/accounts/${resource.mailbox_account_id}`, { method: "DELETE" });
    if (detail?.id === resource.id) setDetail(null);
    await load();
  };

  const fetchMessages = async (resource: MailboxResource) => {
    setMailDialog({ resource, messages: [], loading: true, error: "" });
    try {
      const data = await apiFetch(`/mailboxes/addresses/${resource.mailbox_address_id}/messages?limit=10`);
      setMailDialog({ resource, messages: Array.isArray(data?.items) ? data.items : [], loading: false, error: "" });
    } catch (exc: any) {
      setMailDialog({ resource, messages: [], loading: false, error: exc?.message || "获取邮件失败" });
    }
  };

  const handleAction = async (resource: MailboxResource, action: string) => {
    setError("");
    try {
      if (action === "copy") await copyAddress(resource.address);
      if (action === "messages") await fetchMessages(resource);
      if (action === "bind") await bindResource(resource);
      if (action === "unlink") await unlinkResource(resource);
      if (action === "release") await releaseAddress(resource);
      if (action === "disable") await disableMailbox(resource);
      if (action === "delete") await deleteMailbox(resource);
    } catch (exc: any) {
      setError(exc?.message || "操作失败");
    }
  };

  const detailAccount = detail ? accountById.get(detail.mailbox_account_id) || null : null;
  const detailAddress = detail ? addressById.get(detail.mailbox_address_id) || null : null;
  const detailLink = detail ? linkByAddressId.get(detail.mailbox_address_id) || null : null;

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-[var(--text-primary)]">邮箱资源</h1>
          <div className="mt-1 text-[12px] text-[var(--text-muted)]">
            {payload.paths?.accounts ? <span className="break-all">JSON: {payload.paths.accounts}</span> : "本地邮箱资源"}
          </div>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className="mr-2 h-4 w-4" />
          {loading ? "刷新中" : "刷新列表"}
        </Button>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      ) : null}

      <Card className="shrink-0 bg-[var(--bg-pane)]/40 border border-[var(--border)] shadow-sm">
        <div className="flex flex-wrap items-center gap-2 p-3">
          <select className="control-surface control-surface-compact w-auto min-w-28" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            {statusOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select className="control-surface control-surface-compact w-auto min-w-32" value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)}>
            <option value="all">全部 Provider</option>
            {providers.map((provider) => (
              <option key={provider} value={provider}>{provider}</option>
            ))}
          </select>
          <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-muted)]">
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">共 {payload.resources.length}</span>
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">已注册 {counts.registered || 0}</span>
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">已分配 {counts.allocated || 0}</span>
            <span className="rounded border border-[var(--border)] bg-[var(--bg-card)] px-2 py-1">空闲 {counts.available || 0}</span>
          </div>
          <div className="ml-auto text-xs text-[var(--text-muted)]">当前 {visibleResources.length}</div>
        </div>
      </Card>

      <Card className="min-h-0 flex-1 overflow-hidden p-0 border border-[var(--border)] shadow-sm">
        <div className="glass-table-wrap h-full min-h-0 overflow-auto">
          <table className="table-fixed w-full min-w-[900px] text-sm">
            <colgroup>
              <col className="w-[38%]" />
              <col className="w-[11%]" />
              <col className="w-[11%]" />
              <col className="w-[18%]" />
              <col className="w-[10%]" />
              <col className="w-[12%]" />
            </colgroup>
            <thead className="sticky top-0 z-10 bg-[var(--bg-pane)]/80">
              <tr className="border-b border-[var(--border)] text-xs font-medium uppercase tracking-wider text-[var(--text-muted)]">
                <th className="px-3 py-2 text-left">邮箱号</th>
                <th className="px-3 py-2 text-left">状态</th>
                <th className="px-3 py-2 text-left">Provider</th>
                <th className="px-3 py-2 text-left">ChatGPT 账号</th>
                <th className="px-3 py-2 text-left">更新时间</th>
                <th className="px-3 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {visibleResources.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-24 text-center">
                    <div className="flex flex-col items-center justify-center space-y-3">
                      <div className="flex h-12 w-12 items-center justify-center rounded-full border border-[var(--border)] bg-[var(--bg-pane)] shadow-sm">
                        <span className="text-lg text-[var(--text-muted)]">@</span>
                      </div>
                      <h3 className="text-sm font-medium text-[var(--text-primary)]">暂无邮箱资源</h3>
                      <p className="max-w-sm text-xs text-[var(--text-muted)]">获取到邮箱后会出现在这里。</p>
                    </div>
                  </td>
                </tr>
              )}
              {visibleResources.map((resource) => (
                <tr
                  key={resource.id}
                  className="group cursor-pointer border-b border-[var(--border)]/30 transition-colors hover:bg-[var(--text-primary)]/[0.02]"
                  onClick={() => setDetail(resource)}
                >
                  <td className="px-3 py-2.5 align-top font-mono text-sm text-[var(--text-primary)]">
                    <div className="min-w-0">
                      <div className="truncate tracking-tight" title={resource.address}>{resource.address}</div>
                      <div className="mt-1 truncate text-xs text-[var(--text-muted)]">
                        {resource.address_type || "primary"}
                      </div>
                    </div>
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    <StatusPill status={resource.status} />
                  </td>
                  <td className="px-3 py-2.5 align-top text-[var(--text-secondary)]">
                    <div className="truncate" title={resource.provider}>{providerLabel(resource.provider)}</div>
                  </td>
                  <td className="px-3 py-2.5 align-top">
                    {resource.chatgpt_account_id ? (
                      <div className="min-w-0">
                        <div className="font-medium text-[var(--text-primary)]">#{resource.chatgpt_account_id}</div>
                        <div className="mt-1 truncate text-xs text-[var(--text-muted)]" title={resource.chatgpt_account_email || ""}>
                          {resource.chatgpt_account_email || "未记录邮箱"}
                        </div>
                      </div>
                    ) : (
                      <span className="text-xs text-[var(--text-muted)]">未绑定</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 align-top font-mono text-xs text-[var(--text-muted)]" title={formatTime(resource.updated_at || resource.created_at)}>
                    {formatCompactTime(resource.updated_at || resource.created_at)}
                  </td>
                  <td className="px-3 py-2.5 align-top" onClick={(event) => event.stopPropagation()}>
                    <div className="flex items-center justify-end opacity-60 transition-opacity group-hover:opacity-100">
                      <ActionMenu
                        resource={resource}
                        onDetail={() => setDetail(resource)}
                        onAction={handleAction}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {detail ? (
        <DetailModal
          resource={detail}
          account={detailAccount}
          address={detailAddress}
          link={detailLink}
          onClose={() => setDetail(null)}
        />
      ) : null}

      {mailDialog ? (
        <MailMessagesModal
          resource={mailDialog.resource}
          messages={mailDialog.messages}
          loading={mailDialog.loading}
          error={mailDialog.error}
          onRefresh={() => fetchMessages(mailDialog.resource)}
          onClose={() => setMailDialog(null)}
        />
      ) : null}
    </div>
  );
}
