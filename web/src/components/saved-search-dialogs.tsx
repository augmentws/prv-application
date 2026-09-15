"use client";

import { BookmarkPlus, FolderSearch, Globe2, LoaderCircle, LockKeyhole, Play, Trash2, UsersRound } from "lucide-react";
import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import type { MatterSavedSearchCreate, MatterSavedSearchRead, MatterSearchRequest, UserRead } from "@/generated/models";
import { formatDate } from "@/lib/format";

type Visibility = "PRIVATE" | "PUBLIC" | "SHARED";

export function SaveSearchDialog({ search, users, currentUserId, onCreate }: {
  search: MatterSearchRequest;
  users: UserRead[];
  currentUserId?: string;
  onCreate: (payload: MatterSavedSearchCreate) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("PRIVATE");
  const [sharedUserIds, setSharedUserIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const shareCandidates = users.filter((user) => user.id !== currentUserId && user.status === "ACTIVE");

  const reset = () => {
    setName("");
    setDescription("");
    setVisibility("PRIVATE");
    setSharedUserIds([]);
    setError("");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await onCreate({
        name,
        description: description.trim() || null,
        visibility,
        shared_user_ids: visibility === "SHARED" ? sharedUserIds : [],
        search: { ...search, offset: 0 },
      });
      reset();
      setOpen(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The search could not be saved.");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleUser = (userId: string) => {
    setSharedUserIds((current) => current.includes(userId)
      ? current.filter((value) => value !== userId)
      : [...current, userId]);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (!next) reset(); }}>
      <DialogTrigger asChild><Button variant="outline" size="sm"><BookmarkPlus />Save search</Button></DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Save this search</DialogTitle>
          <DialogDescription>Save the query and filters. Opening it later searches the matter&apos;s current documents.</DialogDescription>
        </DialogHeader>
        <form className="space-y-5" onSubmit={submit}>
          <div className="space-y-2">
            <Label htmlFor="saved-search-name">Name</Label>
            <Input id="saved-search-name" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} required autoFocus />
          </div>
          <div className="space-y-2">
            <Label htmlFor="saved-search-description">Description <span className="font-normal text-muted-foreground">(optional)</span></Label>
            <Textarea id="saved-search-description" value={description} onChange={(event) => setDescription(event.target.value)} maxLength={1000} rows={3} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="saved-search-visibility">Access</Label>
            <Select value={visibility} onValueChange={(value) => { setVisibility(value as Visibility); setSharedUserIds([]); }}>
              <SelectTrigger id="saved-search-visibility"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="PRIVATE">Private — only me</SelectItem>
                <SelectItem value="PUBLIC">Public — everyone in the matter</SelectItem>
                <SelectItem value="SHARED">Shared — selected users</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {visibility === "SHARED" ? (
            <fieldset className="space-y-2">
              <legend className="text-sm font-medium">Share with</legend>
              <div className="max-h-52 space-y-1 overflow-y-auto rounded-lg border p-2">
                {shareCandidates.map((user) => (
                  <label key={user.id} className="flex cursor-pointer items-center gap-3 rounded-md px-2 py-2 hover:bg-muted">
                    <input type="checkbox" className="size-4 accent-primary" checked={sharedUserIds.includes(user.id)} onChange={() => toggleUser(user.id)} />
                    <span className="min-w-0"><span className="block truncate text-sm font-medium">{user.display_name}</span><span className="block truncate text-xs text-muted-foreground">{user.email}</span></span>
                  </label>
                ))}
                {!shareCandidates.length ? <p className="px-2 py-3 text-sm text-muted-foreground">No other active tenant users are available.</p> : null}
              </div>
            </fieldset>
          ) : null}
          {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" disabled={submitting || !name.trim() || (visibility === "SHARED" && !sharedUserIds.length)}>
              {submitting ? <LoaderCircle className="animate-spin" /> : <BookmarkPlus />}{submitting ? "Saving…" : "Save search"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function SavedSearchesDialog({ searches, loading, onRun, onDelete }: {
  searches: MatterSavedSearchRead[];
  loading: boolean;
  onRun: (saved: MatterSavedSearchRead) => void;
  onDelete: (saved: MatterSavedSearchRead) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [deletingId, setDeletingId] = useState("");

  const remove = async (saved: MatterSavedSearchRead) => {
    setDeletingId(saved.id);
    try {
      await onDelete(saved);
    } finally {
      setDeletingId("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button variant="outline" size="sm"><FolderSearch />Saved searches</Button></DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Saved searches</DialogTitle>
          <DialogDescription>Run a saved query again against the matter&apos;s current search index.</DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] space-y-2 overflow-y-auto">
          {searches.map((saved) => {
            const AccessIcon = saved.visibility === "PRIVATE" ? LockKeyhole : saved.visibility === "PUBLIC" ? Globe2 : UsersRound;
            const query = saved.search.query?.trim();
            return (
              <article key={saved.id} className="rounded-lg border p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate font-semibold">{saved.name}</h3>
                      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground"><AccessIcon className="size-3.5" />{saved.visibility.toLowerCase()}</span>
                    </div>
                    {saved.description ? <p className="mt-1 text-sm text-muted-foreground">{saved.description}</p> : null}
                    <p className="mt-2 truncate text-sm">{query ? `“${query}”` : "All matter documents"}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{saved.search.search_mode?.toLowerCase()} · {saved.search.filters?.length ?? 0} filters · {saved.owner.display_name} · Updated {formatDate(saved.updated_at)}</p>
                  </div>
                  <div className="flex shrink-0 gap-1">
                    {saved.is_owner ? <Button type="button" variant="ghost" size="icon" aria-label={`Delete ${saved.name}`} disabled={deletingId === saved.id} onClick={() => void remove(saved)}>{deletingId === saved.id ? <LoaderCircle className="animate-spin" /> : <Trash2 />}</Button> : null}
                    <Button type="button" size="sm" onClick={() => { onRun(saved); setOpen(false); }}><Play />Run</Button>
                  </div>
                </div>
              </article>
            );
          })}
          {!loading && !searches.length ? <div className="rounded-lg border border-dashed p-8 text-center"><FolderSearch className="mx-auto size-8 text-muted-foreground" /><p className="mt-3 font-semibold">No saved searches yet</p><p className="mt-1 text-sm text-muted-foreground">Save the current query and filters to find it here.</p></div> : null}
          {loading ? <div className="grid place-items-center p-8"><LoaderCircle className="size-6 animate-spin text-muted-foreground" /><span className="sr-only">Loading saved searches</span></div> : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
