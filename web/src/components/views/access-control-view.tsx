"use client";

import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, KeyRound, LoaderCircle, Plus, ShieldCheck, Trash2, UserRound, UserRoundCog, UsersRound } from "lucide-react";
import { useCallback, useMemo, useState } from "react";
import { toast } from "sonner";

import { CreateUserDialog, type CreateUserValues } from "@/components/forms/create-user-dialog";
import { PageHeader } from "@/components/page-header";
import { QueryError, TableLoading } from "@/components/query-state";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { useWorkspace } from "@/components/workspace-context";
import type {
  AccessGroupRead,
  AuthorizationGrantRead,
  ClientRead,
  CollectionRead,
  MatterRead,
  PermissionRead,
  RoleRead,
  UserRead,
} from "@/generated/models";
import { coreApi } from "@/lib/api-client";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

type Tab = "users" | "roles" | "groups" | "grants";
type SubjectType = "USER" | "ACCESS_GROUP";
type ScopeType = "TENANT" | "CLIENT" | "COLLECTION" | "MATTER";

const tabs: { id: Tab; label: string; icon: typeof ShieldCheck }[] = [
  { id: "users", label: "Users", icon: UserRound },
  { id: "roles", label: "Roles", icon: ShieldCheck },
  { id: "groups", label: "Groups", icon: UsersRound },
  { id: "grants", label: "Assignments", icon: KeyRound },
];

function permissionCategories(permissions: PermissionRead[]) {
  const result = new Map<string, PermissionRead[]>();
  for (const permission of permissions) {
    const category = result.get(permission.category) ?? [];
    category.push(permission);
    result.set(permission.category, category);
  }
  return [...result.entries()].sort(([left], [right]) => left.localeCompare(right));
}

function roleType(role: RoleRead) {
  return role.role_kind === "SYSTEM" ? "System" : "Custom";
}

function scopeLabel(grant: AuthorizationGrantRead) {
  return `${grant.scope_display_name} · ${grant.scope_type.toLowerCase()}`;
}

export function AccessControlView() {
  const { tenant, selectedTenantId, user, permissionKeys = [] } = useWorkspace();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<Tab>("users");
  const [roleDialogOpen, setRoleDialogOpen] = useState(false);
  const [editingRole, setEditingRole] = useState<RoleRead | null>(null);
  const [groupDialogOpen, setGroupDialogOpen] = useState(false);
  const [editingGroup, setEditingGroup] = useState<AccessGroupRead | null>(null);
  const [grantDialogOpen, setGrantDialogOpen] = useState(false);

  const legacyAdministrator = user.is_superuser || user.tenant_role === "ADMIN";
  const canManageUsers = legacyAdministrator || permissionKeys.includes("tenant.manage_users");
  const canManageRoles = legacyAdministrator || permissionKeys.includes("tenant.manage_roles");
  const canManageGroups = legacyAdministrator || permissionKeys.includes("tenant.manage_groups");
  const authorized = canManageUsers || canManageRoles || canManageGroups;
  const accessAdministrationEnabled = canManageRoles || canManageGroups;
  const availableTabs = tabs.filter(({ id }) => (
    id === "users" ? canManageUsers
      : id === "roles" || id === "grants" ? canManageRoles
        : canManageGroups
  ));
  const activeTab = availableTabs.some(({ id }) => id === tab) ? tab : availableTabs[0]?.id;
  const permissions = useQuery({
    queryKey: ["permissions", selectedTenantId],
    queryFn: () => coreApi<PermissionRead[]>("/v1/permissions"),
    enabled: canManageRoles,
  });
  const roles = useQuery({
    queryKey: ["roles", selectedTenantId],
    queryFn: () => coreApi<RoleRead[]>(`/v1/tenants/${selectedTenantId}/roles`),
    enabled: canManageRoles,
  });
  const groups = useQuery({
    queryKey: ["access-groups", selectedTenantId],
    queryFn: () => coreApi<AccessGroupRead[]>(`/v1/tenants/${selectedTenantId}/access-groups`),
    enabled: accessAdministrationEnabled,
  });
  const grants = useQuery({
    queryKey: ["authorization-grants", selectedTenantId],
    queryFn: () => coreApi<AuthorizationGrantRead[]>(`/v1/tenants/${selectedTenantId}/authorization-grants`),
    enabled: canManageRoles,
  });
  const users = useQuery({
    queryKey: ["users", selectedTenantId],
    queryFn: () => coreApi<UserRead[]>(`/v1/tenants/${selectedTenantId}/users`),
    enabled: authorized,
  });
  const clients = useQuery({
    queryKey: ["clients", selectedTenantId],
    queryFn: () => coreApi<ClientRead[]>(`/v1/tenants/${selectedTenantId}/clients`),
    enabled: canManageRoles,
  });
  const matterQueries = useQueries({
    queries: (clients.data ?? []).map((client) => ({
      queryKey: ["matters", client.id],
      queryFn: () => coreApi<MatterRead[]>(`/v1/clients/${client.id}/matters`),
      enabled: canManageRoles,
    })),
  });
  const collectionQueries = useQueries({
    queries: (clients.data ?? []).map((client) => ({
      queryKey: ["collections", selectedTenantId, client.id],
      queryFn: () => coreApi<CollectionRead[]>(`/v1/tenants/${selectedTenantId}/clients/${client.id}/collections`),
      enabled: canManageRoles,
    })),
  });
  const matters = useMemo(() => matterQueries.flatMap((query) => query.data ?? []), [matterQueries]);
  const collections = useMemo(() => collectionQueries.flatMap((query) => query.data ?? []), [collectionQueries]);

  const refreshSecurity = () => {
    void queryClient.invalidateQueries({ queryKey: ["roles", selectedTenantId] });
    void queryClient.invalidateQueries({ queryKey: ["access-groups", selectedTenantId] });
    void queryClient.invalidateQueries({ queryKey: ["authorization-grants", selectedTenantId] });
    void queryClient.invalidateQueries({ queryKey: ["effective-permissions"] });
  };
  const archiveRole = useMutation({
    mutationFn: (role: RoleRead) => coreApi<RoleRead>(`/v1/tenants/${selectedTenantId}/roles/${role.id}/archive`, { method: "POST" }),
    onSuccess: () => { refreshSecurity(); toast.success("Role archived."); },
  });
  const archiveGroup = useMutation({
    mutationFn: (group: AccessGroupRead) => coreApi<AccessGroupRead>(`/v1/tenants/${selectedTenantId}/access-groups/${group.id}/archive`, { method: "POST" }),
    onSuccess: () => { refreshSecurity(); toast.success("Group archived."); },
  });
  const removeGrant = useMutation({
    mutationFn: (grant: AuthorizationGrantRead) => coreApi<void>(`/v1/tenants/${selectedTenantId}/authorization-grants/${grant.id}`, { method: "DELETE" }),
    onSuccess: () => { refreshSecurity(); toast.success("Assignment removed."); },
  });
  const createUserMutation = useMutation({
    mutationFn: (values: CreateUserValues) => coreApi<UserRead>(`/v1/tenants/${selectedTenantId}/users`, { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (createdUser) => {
      void queryClient.invalidateQueries({ queryKey: ["users", selectedTenantId] });
      toast.success(`${createdUser.display_name} was added.`);
    },
  });
  const createUser = useCallback(
    (values: CreateUserValues) => createUserMutation.mutateAsync(values).then(() => undefined),
    [createUserMutation],
  );

  if (!authorized) return <QueryError message="Access control administration is required." />;
  const queryError = permissions.error ?? roles.error ?? groups.error ?? grants.error ?? users.error ?? clients.error;
  const pending = users.isPending
    || (canManageRoles && (permissions.isPending || roles.isPending || grants.isPending || clients.isPending))
    || (accessAdministrationEnabled && groups.isPending);

  return (
    <>
      <PageHeader
        eyebrow={tenant.name}
        title="Access control"
        description="Manage tenant users, define reusable permission roles, organize users into groups, and assign scoped access."
      />
      <div className="mb-5 flex flex-wrap gap-2 border-b pb-3" role="tablist" aria-label="Access control sections">
        {availableTabs.map(({ id, label, icon: Icon }) => (
          <Button key={id} type="button" role="tab" aria-selected={activeTab === id} variant={activeTab === id ? "default" : "ghost"} onClick={() => setTab(id)}>
            <Icon />{label}
          </Button>
        ))}
      </div>

      {pending ? <TableLoading /> : queryError ? <QueryError message={queryError.message} /> : activeTab === "users" ? (
        <UsersPanel users={users.data ?? []} onCreate={createUser} />
      ) : activeTab === "roles" ? (
        <RolesPanel
          roles={roles.data ?? []}
          onCreate={() => { setEditingRole(null); setRoleDialogOpen(true); }}
          onEdit={(role) => { setEditingRole(role); setRoleDialogOpen(true); }}
          onArchive={(role) => archiveRole.mutate(role)}
        />
      ) : activeTab === "groups" ? (
        <GroupsPanel
          groups={groups.data ?? []}
          users={users.data ?? []}
          onCreate={() => { setEditingGroup(null); setGroupDialogOpen(true); }}
          onManage={(group) => { setEditingGroup(group); setGroupDialogOpen(true); }}
          onArchive={(group) => archiveGroup.mutate(group)}
        />
      ) : (
        <GrantsPanel grants={grants.data ?? []} onCreate={() => setGrantDialogOpen(true)} onRemove={(grant) => removeGrant.mutate(grant)} />
      )}

      <RoleDialog
        key={`${editingRole?.id ?? "new-role"}-${roleDialogOpen ? "open" : "closed"}`}
        open={roleDialogOpen}
        onOpenChange={setRoleDialogOpen}
        tenantId={selectedTenantId}
        role={editingRole}
        permissions={permissions.data ?? []}
        onSaved={refreshSecurity}
      />
      <GroupDialog
        key={`${editingGroup?.id ?? "new-group"}-${groupDialogOpen ? "open" : "closed"}`}
        open={groupDialogOpen}
        onOpenChange={setGroupDialogOpen}
        tenantId={selectedTenantId}
        group={editingGroup}
        users={users.data ?? []}
        onSaved={refreshSecurity}
      />
      <GrantDialog
        key={grantDialogOpen ? "grant-open" : "grant-closed"}
        open={grantDialogOpen}
        onOpenChange={setGrantDialogOpen}
        tenantId={selectedTenantId}
        tenantName={tenant.name}
        users={users.data ?? []}
        groups={(groups.data ?? []).filter((group) => group.status === "ACTIVE")}
        roles={(roles.data ?? []).filter((role) => role.status === "ACTIVE")}
        clients={clients.data ?? []}
        matters={matters}
        collections={collections}
        onSaved={refreshSecurity}
      />
    </>
  );
}

function UsersPanel({ users, onCreate }: { users: UserRead[]; onCreate: (values: CreateUserValues) => Promise<void> }) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>Users</CardTitle><CardDescription>Create tenant users, then assign their access directly or through groups.</CardDescription></div>
        <CreateUserDialog onCreate={onCreate} />
      </CardHeader>
      <CardContent>
        <div className="overflow-hidden rounded-lg border">
          <Table>
            <TableHeader><TableRow><TableHead>User</TableHead><TableHead>Legacy access</TableHead><TableHead>Status</TableHead><TableHead>Added</TableHead></TableRow></TableHeader>
            <TableBody>{users.length ? users.map((tenantUser) => (
              <TableRow key={tenantUser.id}>
                <TableCell><p className="font-semibold">{tenantUser.display_name}</p><p className="text-xs text-muted-foreground">{tenantUser.email}</p></TableCell>
                <TableCell><Badge>{tenantUser.tenant_role === "ADMIN" ? "Administrator" : "RBAC"}</Badge></TableCell>
                <TableCell><StatusBadge status={tenantUser.status} /></TableCell>
                <TableCell><span className="text-muted-foreground">{formatDate(tenantUser.created_at)}</span></TableCell>
              </TableRow>
            )) : <TableRow><TableCell colSpan={4} className="h-32 text-center text-muted-foreground">No users are assigned to this tenant.</TableCell></TableRow>}</TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function RolesPanel({ roles, onCreate, onEdit, onArchive }: { roles: RoleRead[]; onCreate: () => void; onEdit: (role: RoleRead) => void; onArchive: (role: RoleRead) => void }) {
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div><CardTitle>Roles</CardTitle><CardDescription>Roles bundle permissions; assigning a role determines where that bundle applies.</CardDescription></div>
        <Button onClick={onCreate}><Plus />New role</Button>
      </CardHeader>
      <CardContent>
        <div className="overflow-hidden rounded-lg border">
          <Table><TableHeader><TableRow><TableHead>Role</TableHead><TableHead>Type</TableHead><TableHead>Permissions</TableHead><TableHead>Assignments</TableHead><TableHead className="text-right">Actions</TableHead></TableRow></TableHeader>
            <TableBody>{roles.map((role) => (
              <TableRow key={role.id}>
                <TableCell><p className="font-semibold">{role.display_name}</p><p className="text-xs text-muted-foreground">{role.description ?? role.key}</p></TableCell>
                <TableCell><Badge variant={role.role_kind === "SYSTEM" ? "accent" : "outline"}>{roleType(role)}</Badge>{role.status === "ARCHIVED" ? <Badge className="ml-2" variant="outline">Archived</Badge> : null}</TableCell>
                <TableCell className="tabular-nums">{role.permission_keys.length}</TableCell>
                <TableCell className="tabular-nums">{role.grant_count}</TableCell>
                <TableCell><div className="flex justify-end gap-2">{role.role_kind === "CUSTOM" && role.status === "ACTIVE" ? <><Button size="sm" variant="outline" onClick={() => onEdit(role)}>Edit permissions</Button><Button size="icon" variant="ghost" aria-label={`Archive ${role.display_name}`} onClick={() => onArchive(role)}><Archive /></Button></> : null}</div></TableCell>
              </TableRow>
            ))}</TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function GroupsPanel({ groups, users, onCreate, onManage, onArchive }: { groups: AccessGroupRead[]; users: UserRead[]; onCreate: () => void; onManage: (group: AccessGroupRead) => void; onArchive: (group: AccessGroupRead) => void }) {
  const userNames = new Map(users.map((user) => [user.id, user.display_name]));
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4"><div><CardTitle>Groups</CardTitle><CardDescription>Group users who should receive the same scoped assignments.</CardDescription></div><Button onClick={onCreate}><Plus />New group</Button></CardHeader>
      <CardContent><div className="overflow-hidden rounded-lg border"><Table><TableHeader><TableRow><TableHead>Group</TableHead><TableHead>Members</TableHead><TableHead>Assignments</TableHead><TableHead className="text-right">Actions</TableHead></TableRow></TableHeader>
        <TableBody>{groups.length ? groups.map((group) => (
          <TableRow key={group.id}><TableCell><p className="font-semibold">{group.display_name}</p><p className="text-xs text-muted-foreground">{group.description ?? group.key}</p>{group.status === "ARCHIVED" ? <Badge className="mt-1" variant="outline">Archived</Badge> : null}</TableCell><TableCell><p className="tabular-nums">{group.member_ids.length}</p>{group.member_ids.length ? <p className="max-w-sm truncate text-xs text-muted-foreground">{group.member_ids.map((id) => userNames.get(id) ?? id).join(", ")}</p> : null}</TableCell><TableCell className="tabular-nums">{group.grant_count}</TableCell><TableCell><div className="flex justify-end gap-2">{group.status === "ACTIVE" ? <><Button size="sm" variant="outline" onClick={() => onManage(group)}><UserRoundCog />Manage members</Button><Button size="icon" variant="ghost" aria-label={`Archive ${group.display_name}`} onClick={() => onArchive(group)}><Archive /></Button></> : null}</div></TableCell></TableRow>
        )) : <TableRow><TableCell colSpan={4} className="h-32 text-center text-muted-foreground">No access groups have been created.</TableCell></TableRow>}</TableBody>
      </Table></div></CardContent>
    </Card>
  );
}

function GrantsPanel({ grants, onCreate, onRemove }: { grants: AuthorizationGrantRead[]; onCreate: () => void; onRemove: (grant: AuthorizationGrantRead) => void }) {
  return (
    <Card><CardHeader className="flex-row items-start justify-between gap-4"><div><CardTitle>Assignments</CardTitle><CardDescription>Bind a role to a user or group at a resource scope.</CardDescription></div><Button onClick={onCreate}><Plus />New assignment</Button></CardHeader>
      <CardContent><div className="overflow-hidden rounded-lg border"><Table><TableHeader><TableRow><TableHead>User or group</TableHead><TableHead>Role</TableHead><TableHead>Scope</TableHead><TableHead className="text-right">Action</TableHead></TableRow></TableHeader><TableBody>{grants.length ? grants.map((grant) => (
        <TableRow key={grant.id}><TableCell><p className="font-semibold">{grant.subject_display_name}</p><p className="text-xs text-muted-foreground">{grant.subject_type === "ACCESS_GROUP" ? "Group" : "User"}</p></TableCell><TableCell>{grant.role_display_name}</TableCell><TableCell><p>{grant.scope_display_name}</p><p className="text-xs text-muted-foreground">{scopeLabel(grant)}</p></TableCell><TableCell className="text-right"><Button size="icon" variant="ghost" aria-label={`Remove ${grant.role_display_name} assignment`} onClick={() => onRemove(grant)}><Trash2 /></Button></TableCell></TableRow>
      )) : <TableRow><TableCell colSpan={4} className="h-32 text-center text-muted-foreground">No assignments exist.</TableCell></TableRow>}</TableBody></Table></div></CardContent>
    </Card>
  );
}

function RoleDialog({ open, onOpenChange, tenantId, role, permissions, onSaved }: { open: boolean; onOpenChange: (open: boolean) => void; tenantId: string; role: RoleRead | null; permissions: PermissionRead[]; onSaved: () => void }) {
  const [key, setKey] = useState(role?.key ?? "");
  const [name, setName] = useState(role?.display_name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [selected, setSelected] = useState<string[]>(role?.permission_keys ?? []);
  const [error, setError] = useState<string | null>(null);
  const categories = useMemo(() => permissionCategories(permissions), [permissions]);
  const save = useMutation({
    mutationFn: async () => {
      if (role) return coreApi<RoleRead>(`/v1/tenants/${tenantId}/roles/${role.id}/permissions`, { method: "PUT", body: JSON.stringify({ permission_keys: selected, row_version: role.row_version }) });
      return coreApi<RoleRead>(`/v1/tenants/${tenantId}/roles`, { method: "POST", body: JSON.stringify({ key, display_name: name, description: description || null, permission_keys: selected }) });
    },
    onSuccess: () => { onSaved(); onOpenChange(false); toast.success(role ? "Role permissions updated." : "Role created."); },
    onError: (failure) => setError(failure.message),
  });
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent className="max-w-3xl"><DialogHeader><DialogTitle>{role ? `Edit ${role.display_name}` : "Create custom role"}</DialogTitle><DialogDescription>Select the exact actions this role permits. A role has no effect until it is assigned.</DialogDescription></DialogHeader>
    <div className="space-y-5">{!role ? <div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="role-name">Name</Label><Input id="role-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="role-key">Key</Label><Input id="role-key" placeholder="CASE_ANALYST" value={key} onChange={(event) => setKey(event.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} /></div><div className="space-y-2 sm:col-span-2"><Label htmlFor="role-description">Description</Label><Textarea id="role-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div></div> : null}
      <div><div className="mb-3 flex items-center justify-between"><Label>Permissions</Label><span className="text-xs text-muted-foreground">{selected.length} selected</span></div><div className="max-h-[42vh] space-y-4 overflow-y-auto rounded-lg border p-4">{categories.map(([category, items]) => <fieldset key={category}><legend className="mb-2 text-sm font-semibold">{category}</legend><div className="grid gap-2 sm:grid-cols-2">{items.map((permission) => <label key={permission.key} className={cn("flex cursor-pointer gap-3 rounded-lg border p-3 text-sm", selected.includes(permission.key) && "border-primary bg-primary/5")}><input type="checkbox" className="mt-1" checked={selected.includes(permission.key)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, permission.key] : current.filter((key) => key !== permission.key))} /><span><span className="font-medium">{permission.display_name}</span><code className="block text-xs text-muted-foreground">{permission.key}</code>{permission.dangerous ? <span className="mt-1 block text-xs text-destructive">Sensitive action</span> : null}</span></label>)}</div></fieldset>)}</div></div>
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
    </div><DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={save.isPending || (!role && (!name.trim() || !key.trim()))} onClick={() => save.mutate()}>{save.isPending ? <LoaderCircle className="animate-spin" /> : null}{role ? "Save permissions" : "Create role"}</Button></DialogFooter>
  </DialogContent></Dialog>;
}

function GroupDialog({ open, onOpenChange, tenantId, group, users, onSaved }: { open: boolean; onOpenChange: (open: boolean) => void; tenantId: string; group: AccessGroupRead | null; users: UserRead[]; onSaved: () => void }) {
  const [key, setKey] = useState(group?.key ?? ""); const [name, setName] = useState(group?.display_name ?? ""); const [description, setDescription] = useState(group?.description ?? ""); const [members, setMembers] = useState<string[]>(group?.member_ids ?? []); const [error, setError] = useState<string | null>(null);
  const save = useMutation({ mutationFn: async () => {
    if (group) return coreApi<AccessGroupRead>(`/v1/tenants/${tenantId}/access-groups/${group.id}/members`, { method: "PUT", body: JSON.stringify({ user_ids: members, row_version: group.row_version }) });
    return coreApi<AccessGroupRead>(`/v1/tenants/${tenantId}/access-groups`, { method: "POST", body: JSON.stringify({ key, display_name: name, description: description || null }) });
  }, onSuccess: () => { onSaved(); onOpenChange(false); toast.success(group ? "Group members updated." : "Group created."); }, onError: (failure) => setError(failure.message) });
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle>{group ? `Manage ${group.display_name}` : "Create access group"}</DialogTitle><DialogDescription>{group ? "Choose the users who inherit this group's assignments." : "Create a reusable authorization subject. Add members after creation."}</DialogDescription></DialogHeader>
    {!group ? <div className="space-y-4"><div className="space-y-2"><Label htmlFor="group-name">Name</Label><Input id="group-name" value={name} onChange={(event) => setName(event.target.value)} /></div><div className="space-y-2"><Label htmlFor="group-key">Key</Label><Input id="group-key" value={key} placeholder="review-team" onChange={(event) => setKey(event.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, "-"))} /></div><div className="space-y-2"><Label htmlFor="group-description">Description</Label><Textarea id="group-description" value={description} onChange={(event) => setDescription(event.target.value)} /></div></div> : <div className="max-h-[50vh] space-y-2 overflow-y-auto rounded-lg border p-3">{users.map((tenantUser) => <label key={tenantUser.id} className="flex cursor-pointer items-center gap-3 rounded-md p-2 hover:bg-muted"><input type="checkbox" checked={members.includes(tenantUser.id)} onChange={(event) => setMembers((current) => event.target.checked ? [...current, tenantUser.id] : current.filter((id) => id !== tenantUser.id))} /><span><span className="block text-sm font-medium">{tenantUser.display_name}</span><span className="block text-xs text-muted-foreground">{tenantUser.email}</span></span></label>)}</div>}
    {error ? <p role="alert" className="mt-4 text-sm text-destructive">{error}</p> : null}<DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={save.isPending || (!group && (!name.trim() || !key.trim()))} onClick={() => save.mutate()}>{save.isPending ? <LoaderCircle className="animate-spin" /> : null}{group ? "Save members" : "Create group"}</Button></DialogFooter>
  </DialogContent></Dialog>;
}

function GrantDialog({ open, onOpenChange, tenantId, tenantName, users, groups, roles, clients, matters, collections, onSaved }: { open: boolean; onOpenChange: (open: boolean) => void; tenantId: string; tenantName: string; users: UserRead[]; groups: AccessGroupRead[]; roles: RoleRead[]; clients: ClientRead[]; matters: MatterRead[]; collections: CollectionRead[]; onSaved: () => void }) {
  const [subjectType, setSubjectType] = useState<SubjectType>("USER"); const [subjectId, setSubjectId] = useState(""); const [roleId, setRoleId] = useState(""); const [scopeType, setScopeType] = useState<ScopeType>("MATTER"); const [scopeId, setScopeId] = useState(""); const [error, setError] = useState<string | null>(null);
  const scopeItems = scopeType === "TENANT" ? [{ id: tenantId, name: tenantName }] : scopeType === "CLIENT" ? clients : scopeType === "MATTER" ? matters : collections;
  const save = useMutation({ mutationFn: () => coreApi<AuthorizationGrantRead>(`/v1/tenants/${tenantId}/authorization-grants`, { method: "POST", body: JSON.stringify({ subject: { type: subjectType, id: subjectId }, role_id: roleId, scope: { type: scopeType, id: scopeId } }) }), onSuccess: () => { onSaved(); onOpenChange(false); toast.success("Assignment created."); }, onError: (failure) => setError(failure.message) });
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle>New assignment</DialogTitle><DialogDescription>Assign one role to a user or group at one scope. Ancestor scopes flow down to supported descendants.</DialogDescription></DialogHeader><div className="space-y-4">
    <div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label>Subject type</Label><Select value={subjectType} onValueChange={(value: SubjectType) => { setSubjectType(value); setSubjectId(""); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="USER">User</SelectItem><SelectItem value="ACCESS_GROUP">Group</SelectItem></SelectContent></Select></div><div className="space-y-2"><Label>User or group</Label><Select value={subjectId} onValueChange={setSubjectId}><SelectTrigger><SelectValue placeholder="Select subject" /></SelectTrigger><SelectContent>{(subjectType === "USER" ? users : groups).map((subject) => <SelectItem key={subject.id} value={subject.id}>{subject.display_name}</SelectItem>)}</SelectContent></Select></div></div>
    <div className="space-y-2"><Label>Role</Label><Select value={roleId} onValueChange={setRoleId}><SelectTrigger><SelectValue placeholder="Select role" /></SelectTrigger><SelectContent>{roles.filter((role) => role.key !== "ROOT_ADMINISTRATOR").map((role) => <SelectItem key={role.id} value={role.id}>{role.display_name}</SelectItem>)}</SelectContent></Select></div>
    <div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2"><Label>Scope type</Label><Select value={scopeType} onValueChange={(value: ScopeType) => { setScopeType(value); setScopeId(value === "TENANT" ? tenantId : ""); }}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent><SelectItem value="TENANT">Tenant</SelectItem><SelectItem value="CLIENT">Client</SelectItem><SelectItem value="COLLECTION">Collection</SelectItem><SelectItem value="MATTER">Matter</SelectItem></SelectContent></Select></div><div className="space-y-2"><Label>Scope</Label><Select value={scopeId} onValueChange={setScopeId}><SelectTrigger><SelectValue placeholder="Select scope" /></SelectTrigger><SelectContent>{scopeItems.map((scope) => <SelectItem key={scope.id} value={scope.id}>{scope.name}</SelectItem>)}</SelectContent></Select></div></div>
    {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}</div><DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button><Button disabled={save.isPending || !subjectId || !roleId || !scopeId} onClick={() => save.mutate()}>{save.isPending ? <LoaderCircle className="animate-spin" /> : null}Create assignment</Button></DialogFooter>
  </DialogContent></Dialog>;
}
