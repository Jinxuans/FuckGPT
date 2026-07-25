import { useEffect, useMemo, useState } from "react";
import {
  Link2,
  MailPlus,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  Unlink,
} from "lucide-react";
import { apiFetch, cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";

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

type MailboxPayload = {
  accounts: MailboxAccount[];
  addresses: MailboxAddress[];
  links: AccountMailboxLink[];
  paths?: Record<string, string>;
};

type AccountForm = {
  provider: string;
  email: string;
  login_account: string;
  status: string;
  credentialsText: string;
  metadataText: string;
};

const emptyForm: AccountForm = {
  provider: "local_ms_pool",
  email: "",
  login_account: "",
  status: "active",
  credentialsText: "{}",
  metadataText: "{}",
};

function parseJsonObject(text: string, label: string) {
  const trimmed = text.trim();
  if (!trimmed) return {};
  const parsed = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} 必须是 JSON object`);
  }
  return parsed;
}

function formatJson(value: any) {
  return JSON.stringify(value && typeof value === "object" ? value : {}, null, 2);
}

function statusVariant(status?: string) {
  const normalized = String(status || "").toLowerCase();
  if (normalized === "active") return "success" as const;
  if (normalized === "disabled" || normalized === "inactive") return "secondary" as const;
  return "default" as const;
}

function Section({
  title,
  desc,
  children,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-[15px] font-semibold text-[var(--text-primary)]">{title}</h2>
        {desc ? <p className="mt-0.5 text-[13px] text-[var(--text-muted)]">{desc}</p> : null}
      </div>
      {children}
    </section>
  );
}

function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("space-y-1.5 text-[12px] font-medium text-[var(--text-secondary)]", className)}>
      <span>{label}</span>
      {children}
    </label>
  );
}

export default function MailboxResources() {
  const [payload, setPayload] = useState<MailboxPayload>({
    accounts: [],
    addresses: [],
    links: [],
  });
  const [form, setForm] = useState<AccountForm>(emptyForm);
  const [editingId, setEditingId] = useState("");
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [addressForm, setAddressForm] = useState({
    mailbox_account_id: "",
    address: "",
    alias_index: "",
  });
  const [linkForm, setLinkForm] = useState({
    account_id: "",
    account_email: "",
    mailbox_address_id: "",
    purpose: "verification",
  });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const accountById = useMemo(() => {
    return new Map(payload.accounts.map((item) => [item.id, item]));
  }, [payload.accounts]);

  const addressById = useMemo(() => {
    return new Map(payload.addresses.map((item) => [item.id, item]));
  }, [payload.addresses]);

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const data = await apiFetch("/mailboxes");
      setPayload({
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

  const resetForm = () => {
    setForm(emptyForm);
    setEditingId("");
  };

  const editAccount = (account: MailboxAccount) => {
    setEditingId(account.id);
    setForm({
      provider: account.provider || "local_ms_pool",
      email: account.email || "",
      login_account: account.login_account || account.email || "",
      status: account.status || "active",
      credentialsText: formatJson(account.credentials),
      metadataText: formatJson(account.metadata),
    });
  };

  const saveAccount = async () => {
    setSaving(true);
    setError("");
    try {
      const body = {
        provider: form.provider,
        email: form.email,
        login_account: form.login_account,
        status: form.status,
        credentials: parseJsonObject(form.credentialsText, "credentials"),
        metadata: parseJsonObject(form.metadataText, "metadata"),
      };
      await apiFetch(editingId ? `/mailboxes/accounts/${editingId}` : "/mailboxes/accounts", {
        method: editingId ? "PATCH" : "POST",
        body: JSON.stringify(body),
      });
      resetForm();
      await load();
    } catch (exc: any) {
      setError(exc?.message || "保存邮箱账号失败");
    } finally {
      setSaving(false);
    }
  };

  const deleteAccount = async (accountId: string) => {
    if (!confirm("删除邮箱账号会同时删除该账号下的地址和绑定关系，继续吗？")) return;
    await apiFetch(`/mailboxes/accounts/${accountId}`, { method: "DELETE" });
    await load();
  };

  const reserveAddress = async () => {
    setError("");
    try {
      await apiFetch("/mailboxes/addresses/reserve", {
        method: "POST",
        body: JSON.stringify({
          mailbox_account_id: addressForm.mailbox_account_id || selectedAccountId,
          address: addressForm.address,
          alias_index: Number(addressForm.alias_index || 0),
        }),
      });
      setAddressForm({ mailbox_account_id: addressForm.mailbox_account_id, address: "", alias_index: "" });
      await load();
    } catch (exc: any) {
      setError(exc?.message || "预留邮箱地址失败");
    }
  };

  const releaseAddress = async (addressId: string) => {
    await apiFetch(`/mailboxes/addresses/${addressId}/release`, { method: "POST" });
    await load();
  };

  const linkAccount = async () => {
    setError("");
    try {
      await apiFetch("/mailboxes/account-link", {
        method: "POST",
        body: JSON.stringify({
          platform: "chatgpt",
          account_id: Number(linkForm.account_id || 0),
          account_email: linkForm.account_email,
          mailbox_address_id: linkForm.mailbox_address_id,
          purpose: linkForm.purpose || "verification",
        }),
      });
      setLinkForm({ account_id: "", account_email: "", mailbox_address_id: linkForm.mailbox_address_id, purpose: "verification" });
      await load();
    } catch (exc: any) {
      setError(exc?.message || "绑定账号失败");
    }
  };

  const unlinkAccount = async (link: AccountMailboxLink) => {
    await apiFetch(`/mailboxes/accounts/${link.account_id}/link?platform=${encodeURIComponent(link.platform)}&purpose=${encodeURIComponent(link.purpose || "verification")}`, {
      method: "DELETE",
    });
    await load();
  };

  const visibleAddresses = selectedAccountId
    ? payload.addresses.filter((item) => item.mailbox_account_id === selectedAccountId)
    : payload.addresses;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 text-[13px] text-[var(--text-muted)]">
          {payload.paths?.accounts ? (
            <span className="break-all">JSON: {payload.paths.accounts}</span>
          ) : (
            <span>邮箱账号、可用地址和账号绑定关系会保存到本地 JSON。</span>
          )}
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className="mr-2 h-4 w-4" />
          {loading ? "刷新中" : "刷新"}
        </Button>
      </div>

      {error ? (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">
          {error}
        </div>
      ) : null}

      <Section title="邮箱账号" desc="保存邮箱服务商、登录账号和明文凭据，注册成功后也会自动补充这里。">
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="grid gap-3 border-b border-[var(--border)] p-4 md:grid-cols-4">
            <Field label="Provider">
              <select
                className="control-surface control-surface-compact"
                value={form.provider}
                onChange={(event) => setForm((prev) => ({ ...prev, provider: event.target.value }))}
              >
                <option value="local_ms_pool">local_ms_pool</option>
                <option value="api_mailbox">api_mailbox</option>
                <option value="hotmail007">hotmail007</option>
              </select>
            </Field>
            <Field label="邮箱">
              <input
                className="control-surface control-surface-compact"
                value={form.email}
                onChange={(event) => setForm((prev) => ({ ...prev, email: event.target.value }))}
                placeholder="user@outlook.com"
              />
            </Field>
            <Field label="登录账号">
              <input
                className="control-surface control-surface-compact"
                value={form.login_account}
                onChange={(event) => setForm((prev) => ({ ...prev, login_account: event.target.value }))}
                placeholder="默认同邮箱"
              />
            </Field>
            <Field label="状态">
              <select
                className="control-surface control-surface-compact"
                value={form.status}
                onChange={(event) => setForm((prev) => ({ ...prev, status: event.target.value }))}
              >
                <option value="active">active</option>
                <option value="inactive">inactive</option>
                <option value="disabled">disabled</option>
              </select>
            </Field>
            <Field label="credentials JSON" className="md:col-span-2">
              <textarea
                className="control-surface control-surface-mono min-h-28"
                value={form.credentialsText}
                onChange={(event) => setForm((prev) => ({ ...prev, credentialsText: event.target.value }))}
              />
            </Field>
            <Field label="metadata JSON" className="md:col-span-2">
              <textarea
                className="control-surface control-surface-mono min-h-28"
                value={form.metadataText}
                onChange={(event) => setForm((prev) => ({ ...prev, metadataText: event.target.value }))}
              />
            </Field>
            <div className="flex gap-2 md:col-span-4">
              <Button onClick={saveAccount} disabled={saving || !form.email}>
                {editingId ? <Save className="mr-2 h-4 w-4" /> : <Plus className="mr-2 h-4 w-4" />}
                {editingId ? "保存邮箱账号" : "新增邮箱账号"}
              </Button>
              {editingId ? (
                <Button variant="outline" onClick={resetForm}>
                  取消编辑
                </Button>
              ) : null}
            </div>
          </div>

          {payload.accounts.length ? (
            <div className="glass-table-wrap">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="border-b border-[var(--border)] text-[var(--text-muted)]">
                  <tr>
                    <th className="px-4 py-3">邮箱</th>
                    <th className="px-4 py-3">Provider</th>
                    <th className="px-4 py-3">状态</th>
                    <th className="px-4 py-3">用量</th>
                    <th className="px-4 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-soft)]">
                  {payload.accounts.map((account) => (
                    <tr key={account.id} className="hover:bg-[var(--bg-hover)]">
                      <td className="px-4 py-3">
                        <div className="font-medium text-[var(--text-primary)]">{account.email}</div>
                        <div className="mt-0.5 text-xs text-[var(--text-muted)]">{account.login_account || account.id}</div>
                      </td>
                      <td className="px-4 py-3 text-[var(--text-secondary)]">{account.provider}</td>
                      <td className="px-4 py-3">
                        <Badge variant={statusVariant(account.status)}>{account.status || "active"}</Badge>
                      </td>
                      <td className="px-4 py-3 text-[var(--text-secondary)]">
                        {account.usage?.used_count ?? 0} / {account.usage?.capacity ?? "-"}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-2">
                          <button className="table-action-btn" onClick={() => {
                            setSelectedAccountId(account.id);
                            setAddressForm((prev) => ({ ...prev, mailbox_account_id: account.id }));
                            editAccount(account);
                          }}>
                            编辑
                          </button>
                          <button className="table-action-btn table-action-btn-danger" onClick={() => deleteAccount(account.id)}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state-panel m-4">暂无邮箱账号。新增或完成一次邮箱注册后会出现在这里。</div>
          )}
        </div>
      </Section>

      <Section title="邮箱地址" desc="主邮箱和别名地址都在这里，Codex OAuth 会优先使用账号绑定的验证邮箱。">
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="grid gap-3 border-b border-[var(--border)] p-4 md:grid-cols-[1.4fr_1.4fr_0.8fr_auto]">
            <Field label="邮箱账号">
              <select
                className="control-surface control-surface-compact"
                value={addressForm.mailbox_account_id || selectedAccountId}
                onChange={(event) => {
                  setSelectedAccountId(event.target.value);
                  setAddressForm((prev) => ({ ...prev, mailbox_account_id: event.target.value }));
                }}
              >
                <option value="">选择邮箱账号</option>
                {payload.accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.email}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="地址">
              <input
                className="control-surface control-surface-compact"
                value={addressForm.address}
                onChange={(event) => setAddressForm((prev) => ({ ...prev, address: event.target.value }))}
                placeholder="留空使用主邮箱"
              />
            </Field>
            <Field label="别名序号">
              <input
                className="control-surface control-surface-compact"
                value={addressForm.alias_index}
                onChange={(event) => setAddressForm((prev) => ({ ...prev, alias_index: event.target.value }))}
                placeholder="1"
              />
            </Field>
            <div className="flex items-end">
              <Button onClick={reserveAddress} disabled={!(addressForm.mailbox_account_id || selectedAccountId)}>
                <MailPlus className="mr-2 h-4 w-4" />
                预留地址
              </Button>
            </div>
          </div>

          {visibleAddresses.length ? (
            <div className="glass-table-wrap">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="border-b border-[var(--border)] text-[var(--text-muted)]">
                  <tr>
                    <th className="px-4 py-3">地址</th>
                    <th className="px-4 py-3">归属邮箱</th>
                    <th className="px-4 py-3">类型</th>
                    <th className="px-4 py-3">预留</th>
                    <th className="px-4 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-soft)]">
                  {visibleAddresses.map((address) => {
                    const account = accountById.get(address.mailbox_account_id);
                    return (
                      <tr key={address.id} className="hover:bg-[var(--bg-hover)]">
                        <td className="px-4 py-3 font-medium text-[var(--text-primary)]">{address.address}</td>
                        <td className="px-4 py-3 text-[var(--text-secondary)]">{account?.email || address.mailbox_account_id}</td>
                        <td className="px-4 py-3 text-[var(--text-secondary)]">{address.address_type || "primary"}</td>
                        <td className="px-4 py-3">
                          <Badge variant={address.reserved ? "warning" : "secondary"}>
                            {address.reserved ? "已预留" : "空闲"}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex justify-end gap-2">
                            <button
                              className="table-action-btn"
                              onClick={() => setLinkForm((prev) => ({ ...prev, mailbox_address_id: address.id }))}
                            >
                              绑定
                            </button>
                            <button className="table-action-btn" disabled={!address.reserved} onClick={() => releaseAddress(address.id)}>
                              释放
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state-panel m-4">暂无邮箱地址。选择邮箱账号后可以预留主邮箱或别名。</div>
          )}
        </div>
      </Section>

      <Section title="账号绑定" desc="把 ChatGPT 账号 id 绑定到一个验证邮箱地址，Codex OAuth 邮箱验证码会用这条绑定读取。">
        <div className="rounded-xl border border-[var(--border)] bg-[var(--bg-card)]">
          <div className="grid gap-3 border-b border-[var(--border)] p-4 md:grid-cols-[0.8fr_1.2fr_1.4fr_0.8fr_auto]">
            <Field label="账号 ID">
              <input
                className="control-surface control-surface-compact"
                value={linkForm.account_id}
                onChange={(event) => setLinkForm((prev) => ({ ...prev, account_id: event.target.value }))}
                placeholder="123"
              />
            </Field>
            <Field label="账号邮箱">
              <input
                className="control-surface control-surface-compact"
                value={linkForm.account_email}
                onChange={(event) => setLinkForm((prev) => ({ ...prev, account_email: event.target.value }))}
                placeholder="account@example.com"
              />
            </Field>
            <Field label="验证邮箱地址">
              <select
                className="control-surface control-surface-compact"
                value={linkForm.mailbox_address_id}
                onChange={(event) => setLinkForm((prev) => ({ ...prev, mailbox_address_id: event.target.value }))}
              >
                <option value="">选择地址</option>
                {payload.addresses.map((address) => (
                  <option key={address.id} value={address.id}>
                    {address.address}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="用途">
              <input
                className="control-surface control-surface-compact"
                value={linkForm.purpose}
                onChange={(event) => setLinkForm((prev) => ({ ...prev, purpose: event.target.value }))}
              />
            </Field>
            <div className="flex items-end">
              <Button onClick={linkAccount} disabled={!linkForm.account_id || !linkForm.mailbox_address_id}>
                <Link2 className="mr-2 h-4 w-4" />
                绑定账号
              </Button>
            </div>
          </div>

          {payload.links.length ? (
            <div className="glass-table-wrap">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="border-b border-[var(--border)] text-[var(--text-muted)]">
                  <tr>
                    <th className="px-4 py-3">平台账号</th>
                    <th className="px-4 py-3">验证邮箱</th>
                    <th className="px-4 py-3">用途</th>
                    <th className="px-4 py-3">状态</th>
                    <th className="px-4 py-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border-soft)]">
                  {payload.links.map((link) => {
                    const address = addressById.get(link.mailbox_address_id);
                    return (
                      <tr key={link.id} className="hover:bg-[var(--bg-hover)]">
                        <td className="px-4 py-3">
                          <div className="font-medium text-[var(--text-primary)]">{link.platform} #{link.account_id}</div>
                          <div className="mt-0.5 text-xs text-[var(--text-muted)]">{link.account_email || "未记录账号邮箱"}</div>
                        </td>
                        <td className="px-4 py-3 text-[var(--text-secondary)]">{address?.address || link.mailbox_address_id}</td>
                        <td className="px-4 py-3 text-[var(--text-secondary)]">{link.purpose || "verification"}</td>
                        <td className="px-4 py-3">
                          <Badge variant={statusVariant(link.status)}>{link.status || "active"}</Badge>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button className="table-action-btn" onClick={() => unlinkAccount(link)}>
                            <Unlink className="mr-1.5 h-3.5 w-3.5" />
                            解绑
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state-panel m-4">暂无账号绑定。注册成功后会自动创建，也可以在这里手工补绑。</div>
          )}
        </div>
      </Section>
    </div>
  );
}
