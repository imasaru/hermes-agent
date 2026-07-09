/**
 * The Kanban board page — mounted at `/kanban` (a ROUTES_AREA contribution) in
 * the workspace pane. The desktop port of the dashboard board: one compact
 * header row (count, filter kebab, search, attention chip, nudge, settings,
 * new task — the board SWITCHER lives in the titlebar, see board-switcher.tsx),
 * columns in BOARD_COLUMNS order, drag-to-move (optimistic, workflow-checked),
 * right-click actions, and the detail drawer.
 */

import {
  Button,
  cn,
  Codicon,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  Contribute,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  ErrorState,
  host,
  Input,
  Loader,
  SearchField,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  Textarea,
  Tip,
  TITLEBAR_AREAS,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { type ReactNode, useEffect, useMemo, useState } from 'react'

import {
  $boardSlug,
  $introDismissed,
  boardKey,
  createTask,
  deleteTask,
  fetchBoard,
  fetchProfiles,
  nudgeDispatcher,
  patchTask,
  PROFILES_KEY
} from './api'
import { BoardSwitcher } from './board-switcher'
import { TaskDrawer } from './drawer'
import { OrchestrationPanel } from './orchestration'
import { columnMeta, type KanbanBoard, type KanbanTask } from './types'
import { ago, Avatar, errText, Freshness, isLockedTarget, LOCKED_COLUMNS, shortId } from './ui'

// ── optimistic board edits (reconciled by the follow-up refresh) ─────────────

function moveCard(board: KanbanBoard, id: string, toStatus: string): KanbanBoard {
  let moved: KanbanTask | undefined

  const columns = board.columns.map(col => ({
    ...col,
    tasks: col.tasks.filter(task => {
      if (task.id !== id) {
        return true
      }

      moved = { ...task, status: toStatus }

      return false
    })
  }))

  if (!moved) {
    return board
  }

  return {
    ...board,
    columns: columns.map(col => (col.name === toStatus ? { ...col, tasks: [moved!, ...col.tasks] } : col))
  }
}

function removeCard(board: KanbanBoard, id: string): KanbanBoard {
  return { ...board, columns: board.columns.map(col => ({ ...col, tasks: col.tasks.filter(t => t.id !== id) })) }
}

// ── card ─────────────────────────────────────────────────────────────────────

function Meta({ children, icon }: { children: ReactNode; icon: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <Codicon name={icon} size="0.7rem" />
      {children}
    </span>
  )
}

function CardFooter({ task }: { task: KanbanTask }) {
  const created = ago(task.created_at)
  const links = task.link_counts ? task.link_counts.parents + task.link_counts.children : 0
  // The board's #1 silent failure: Ready + no assignee = the dispatcher will
  // never claim it. Say so on the card instead of letting it sit mute.
  const stranded = task.status === 'ready' && !task.assignee

  return (
    <div className="flex items-center gap-2 whitespace-nowrap text-[0.625rem] text-(--ui-text-tertiary)">
      {task.assignee ? <Avatar name={task.assignee} size="1.125rem" /> : null}
      {stranded && (
        <Tip label="Ready cards only run once a profile is assigned. Open the card and set an assignee.">
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 text-amber-500">
            <Codicon name="debug-disconnect" size="0.7rem" />
            won't run
          </span>
        </Tip>
      )}
      <span className="min-w-0 truncate font-mono text-(--ui-text-quaternary)">{shortId(task.id)}</span>
      <div className="ml-auto flex shrink-0 items-center gap-2">
        {typeof task.priority === 'number' && task.priority > 0 && (
          <span className="inline-flex items-center gap-0.5 text-amber-500">
            <Codicon name="arrow-up" size="0.7rem" />
            {task.priority}
          </span>
        )}
        {task.progress && task.progress.total > 0 && (
          <Meta icon="checklist">
            {task.progress.done}/{task.progress.total}
          </Meta>
        )}
        {Boolean(task.comment_count) && <Meta icon="comment">{task.comment_count}</Meta>}
        {links > 0 && <Meta icon="references">{links}</Meta>}
        {task.warnings && task.warnings.count > 0 && (
          <span className="inline-flex items-center gap-0.5 text-destructive">
            <Codicon name="warning" size="0.7rem" />
            {task.warnings.count}
          </span>
        )}
        {created && !task.assignee && !stranded ? <span className="text-(--ui-text-quaternary)">{created}</span> : null}
      </div>
    </div>
  )
}

function Card({
  columns,
  onDelete,
  onMove,
  onOpen,
  task
}: {
  columns: string[]
  onDelete: (id: string) => void
  onMove: (id: string, status: string) => void
  onOpen: (id: string) => void
  task: KanbanTask
}) {
  const [dragging, setDragging] = useState(false)
  const meta = columnMeta(task.status)
  const summary = task.latest_summary || task.body

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div
          className={cn(
            'group flex cursor-grab flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) border-l-2 bg-(--ui-bg-elevated) p-2.5',
            'transition-colors hover:border-(--ui-stroke-secondary) hover:bg-(--ui-control-hover-background) active:cursor-grabbing',
            dragging && 'opacity-40'
          )}
          draggable
          onClick={() => onOpen(task.id)}
          onDragEnd={() => setDragging(false)}
          onDragStart={event => {
            event.dataTransfer.setData('text/plain', task.id)
            event.dataTransfer.effectAllowed = 'move'
            // Snapshot the drag image before dimming the source, so the ghost
            // stays a solid card (dimming first would bake 40% into it).
            event.dataTransfer.setDragImage(event.currentTarget, event.nativeEvent.offsetX, event.nativeEvent.offsetY)
            setDragging(true)
          }}
          style={{ borderLeftColor: meta.tone }}
        >
          <span className="line-clamp-2 text-[0.8125rem] font-medium leading-snug text-foreground">
            {task.title || task.id}
          </span>
          {summary && (
            <span className="line-clamp-2 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{summary}</span>
          )}
          <CardFooter task={task} />
        </div>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <ContextMenuItem onSelect={() => onOpen(task.id)}>
          <Codicon name="link-external" size="0.85rem" />
          Open
        </ContextMenuItem>
        <ContextMenuSeparator />
        {columns
          .filter(name => name !== task.status && !isLockedTarget(name))
          .map(name => (
            <ContextMenuItem key={name} onSelect={() => onMove(task.id, name)}>
              <span className="size-2 rounded-full" style={{ backgroundColor: columnMeta(name).tone }} />
              Move to {columnMeta(name).label}
            </ContextMenuItem>
          ))}
        <ContextMenuSeparator />
        <ContextMenuItem onSelect={() => onDelete(task.id)} variant="destructive">
          <Codicon name="trash" size="0.85rem" />
          Delete
        </ContextMenuItem>
      </ContextMenuContent>
    </ContextMenu>
  )
}

// ── column ───────────────────────────────────────────────────────────────────

function Column({
  column,
  columns,
  onAdd,
  onDelete,
  onDropTask,
  onMove,
  onOpen
}: {
  column: { name: string; tasks: KanbanTask[] }
  columns: string[]
  onAdd: (status: string) => void
  onDelete: (id: string) => void
  onDropTask: (id: string, status: string) => void
  onMove: (id: string, status: string) => void
  onOpen: (id: string) => void
}) {
  const [over, setOver] = useState(false)
  const meta = columnMeta(column.name)
  const locked = isLockedTarget(column.name)

  return (
    <div
      className={cn(
        'group/col flex h-full w-64 shrink-0 flex-col rounded-lg p-2 transition-colors',
        over && !locked
          ? 'bg-(--ui-bg-quinary)'
          : 'bg-[color-mix(in_srgb,var(--ui-bg-quinary)_50%,transparent)]'
      )}
      onDragLeave={() => setOver(false)}
      onDragOver={event => {
        // Locked lanes don't preventDefault → the OS shows the no-drop cursor
        // and the drop event never fires. The lane is honest about itself.
        if (locked) {
          event.dataTransfer.dropEffect = 'none'

          return
        }

        event.preventDefault()
        event.dataTransfer.dropEffect = 'move'
        setOver(true)
      }}
      onDrop={event => {
        event.preventDefault()
        setOver(false)
        const id = event.dataTransfer.getData('text/plain')

        if (id) {
          onDropTask(id, column.name)
        }
      }}
    >
      <header className="mb-1.5 flex items-center gap-1.5 px-1">
        <span className="size-1.5 rounded-full" style={{ backgroundColor: meta.tone }} />
        <Tip label={meta.help}>
          <span className="cursor-help text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)">
            {meta.label}
          </span>
        </Tip>
        <span className="text-[0.625rem] tabular-nums text-(--ui-text-quaternary)">{column.tasks.length}</span>
      </header>
      <div className="relative flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
        {column.tasks.map(task => (
          <Card columns={columns} key={task.id} onDelete={onDelete} onMove={onMove} onOpen={onOpen} task={task} />
        ))}
        {/* Jira-style lane add — dashed, faded in on lane hover. Opacity (not
            display) so it always holds its slot and never thrashes layout.
            Locked lanes get none: you can't create into a system state. */}
        {!locked && (
          <button
            aria-label={`New task in ${meta.label}`}
            className="flex shrink-0 items-center justify-center rounded-md border border-dashed border-(--ui-stroke-secondary) py-1.5 text-(--ui-text-tertiary) opacity-0 transition-[opacity,color,border-color] group-hover/col:opacity-100 hover:border-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-foreground focus-visible:opacity-100"
            onClick={() => onAdd(column.name)}
            type="button"
          >
            <Codicon name="add" size="0.8rem" />
          </button>
        )}
        {column.tasks.length === 0 && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center text-[0.6875rem] text-(--ui-text-quaternary)">
            Empty
          </div>
        )}
      </div>
    </div>
  )
}

// ── dialogs ──────────────────────────────────────────────────────────────────

const NO_PARENT = '__none__'
const WORKSPACE_KINDS = ['scratch', 'worktree', 'dir'] as const

function Field({ children, label }: { children: ReactNode; label: string }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[0.62rem] font-semibold uppercase tracking-[0.14em] text-(--ui-text-quaternary)">
        {label}
      </span>
      {children}
    </label>
  )
}

function NewTaskDialog({
  onClose,
  parents,
  target
}: {
  onClose: () => void
  parents: Array<{ id: string; title: string }>
  target: null | string
}) {
  const qc = useQueryClient()
  const { data: roster } = useQuery({ queryKey: PROFILES_KEY, queryFn: fetchProfiles, staleTime: 60_000 })
  const isTriage = target === 'triage'
  const [title, setTitle] = useState('')
  const [bodyText, setBodyText] = useState('')
  const [assignee, setAssignee] = useState('')
  const [priority, setPriority] = useState('0')
  const [skills, setSkills] = useState('')
  const [workspaceKind, setWorkspaceKind] = useState<string>('scratch')
  const [parent, setParent] = useState('')
  const [goalMode, setGoalMode] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<null | string>(null)

  // Reset per open — the dialog is externally controlled (open = target set),
  // so onOpenChange(true) never fires; key the reset off `target` instead.
  useEffect(() => {
    if (target) {
      setTitle('')
      setBodyText('')
      setAssignee('')
      setPriority('0')
      setSkills('')
      setWorkspaceKind('scratch')
      setParent('')
      setGoalMode(false)
      setError(null)
      setBusy(false)
    }
  }, [target])

  const submit = async () => {
    const trimmed = title.trim()

    if (!trimmed || !target || busy) {
      return
    }

    setBusy(true)
    setError(null)

    try {
      const skillList = skills.split(',').map(s => s.trim()).filter(Boolean)

      // create() derives status (triage flag → 'triage', else 'ready'); move to
      // the requested column when they differ, so a per-column add lands right.
      const { task, warning } = await createTask({
        assignee: assignee || undefined,
        body: bodyText.trim() || undefined,
        goal_mode: goalMode,
        parents: parent ? [parent] : undefined,
        priority: Number(priority) || 0,
        skills: skillList.length ? skillList : undefined,
        title: trimmed,
        triage: isTriage,
        workspace_kind: workspaceKind
      })

      if (task && task.status !== target) {
        await patchTask(task.id, { status: target })
      }

      // Dispatcher-presence warning ("this ready task will sit idle") — not an
      // error, but the user should know.
      if (warning) {
        host.notify({ kind: 'warning', message: warning })
      }

      await qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      onClose()
    } catch (err) {
      setError(errText(err))
      setBusy(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && onClose()} open={Boolean(target)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New task{target ? ` in ${columnMeta(target).label}` : ''}</DialogTitle>
        </DialogHeader>
        <div className="flex max-h-[60vh] flex-col gap-3 overflow-y-auto pr-0.5">
          <Input
            autoFocus
            onChange={event => setTitle(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              }
            }}
            placeholder={isTriage ? 'Rough idea — a specifier will flesh it out' : 'Title'}
            value={title}
          />
          <Textarea
            className="min-h-20"
            onChange={event => setBodyText(event.target.value)}
            placeholder="Description (optional)"
            value={bodyText}
          />

          <div className="grid grid-cols-2 gap-3">
            <Field label="Priority">
              <Input onChange={event => setPriority(event.target.value)} type="number" value={priority} />
            </Field>
            <Field label="Workspace">
              <Select onValueChange={setWorkspaceKind} value={workspaceKind}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {WORKSPACE_KINDS.map(kind => (
                    <SelectItem key={kind} value={kind}>
                      {kind}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          <Field label="Assignee">
            <Select onValueChange={v => setAssignee(v === NO_PARENT ? '' : v)} value={assignee || NO_PARENT}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_PARENT}>(auto / unassigned)</SelectItem>
                {(roster?.profiles ?? []).map(profile => (
                  <SelectItem key={profile.name} value={profile.name}>
                    {profile.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <Field label="Skills (comma-separated)">
            <Input onChange={event => setSkills(event.target.value)} placeholder="translation, github" value={skills} />
          </Field>

          {parents.length > 0 && (
            <Field label="Parent (blocks until it's done)">
              <Select onValueChange={v => setParent(v === NO_PARENT ? '' : v)} value={parent || NO_PARENT}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NO_PARENT}>— no parent —</SelectItem>
                  {parents.map(option => (
                    <SelectItem key={option.id} value={option.id}>
                      {option.title || option.id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          )}

          <label className="flex cursor-pointer items-center gap-2 text-[0.75rem] text-(--ui-text-secondary)">
            <Switch
              aria-label="Goal mode"
              checked={goalMode}
              onCheckedChange={setGoalMode}
              size="xs"
            />
            Goal mode (worker loops until a judge agrees it's done)
          </label>

          {error && <span className="text-[0.75rem] text-destructive">{error}</span>}
        </div>
        <DialogFooter>
          <Button onClick={onClose} variant="text">
            Cancel
          </Button>
          <Button disabled={!title.trim() || busy} onClick={() => void submit()}>
            {busy ? 'Creating…' : 'Create task'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── intro ────────────────────────────────────────────────────────────────────

// One-time explainer for the board's core gotcha: this is a dispatcher queue,
// not a todo list. Dismissal persists via plugin storage.
function Intro() {
  const dismissed = useValue($introDismissed)

  if (dismissed) {
    return null
  }

  return (
    <div
      className="mx-4 mb-2 flex flex-col items-start gap-1.5 rounded-lg bg-(--ui-bg-quinary) px-3 py-2.5 text-[0.75rem] leading-relaxed text-(--ui-text-secondary)"
      data-selectable-text="true"
    >
      <p className="min-w-0">
        You don't run the cards — agents do. Put a card in <b>Ready</b> with an assignee and an agent picks it up
        within a minute. No assignee, no run. <b>Triage</b>: an agent rewrites the idea into a proper task first.{' '}
        <b>Todo</b>: waiting on other cards. <b>Scheduled</b>: waiting on a timer. <b>Running</b> and <b>Review</b>:
        the agents' lanes, hands off. <b>Blocked</b>: it's waiting on you. Results come back on the card.
      </p>
      <Button onClick={() => $introDismissed.set(true)} size="inline" variant="textStrong">
        Got it
      </Button>
    </div>
  )
}

// ── filter kebab ─────────────────────────────────────────────────────────────

function FilterMenu({
  archived,
  assignee,
  board,
  onArchived,
  onAssignee,
  onTenant,
  tenant
}: {
  archived: boolean
  assignee: string
  board: KanbanBoard
  onArchived: (v: boolean) => void
  onAssignee: (v: string) => void
  onTenant: (v: string) => void
  tenant: string
}) {
  const active = Boolean(assignee || tenant || archived)

  const check = (on: boolean) => (on ? <Codicon className="ml-auto" name="check" size="0.8rem" /> : null)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label="Filters"
          className={cn(active && 'bg-(--ui-control-active-background) text-foreground')}
          size="icon-xs"
          variant="ghost"
        >
          <Codicon name="filter" size="0.85rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuItem onSelect={() => onAssignee('')}>All profiles{check(!assignee)}</DropdownMenuItem>
        {board.assignees.map(name => (
          <DropdownMenuItem key={name} onSelect={() => onAssignee(name)}>
            <Avatar name={name} size="0.875rem" />
            {name}
            {check(assignee === name)}
          </DropdownMenuItem>
        ))}
        {board.tenants.length > 0 && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => onTenant('')}>All tenants{check(!tenant)}</DropdownMenuItem>
            {board.tenants.map(name => (
              <DropdownMenuItem key={name} onSelect={() => onTenant(name)}>
                {name}
                {check(tenant === name)}
              </DropdownMenuItem>
            ))}
          </>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => onArchived(!archived)}>
          Show archived{check(archived)}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ── page ─────────────────────────────────────────────────────────────────────

export function KanbanBoardPage() {
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const [archived, setArchived] = useState(false)

  const { data: board, dataUpdatedAt, error, isFetching } = useQuery({
    queryFn: () => fetchBoard(archived),
    queryKey: boardKey(slug, archived),
    refetchInterval: 8_000
  })

  const [openId, setOpenId] = useState<null | string>(null)
  const [addStatus, setAddStatus] = useState<null | string>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [tenant, setTenant] = useState('')
  const [assignee, setAssignee] = useState('')
  const [attentionOnly, setAttentionOnly] = useState(false)

  const nudge = useMutation({
    mutationFn: nudgeDispatcher,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: () => {
      host.notify({ kind: 'info', message: 'Dispatcher nudged' })
      void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
    }
  })

  const columnNames = board?.columns.map(col => col.name) ?? []

  const parentOptions = useMemo(
    () => board?.columns.flatMap(col => col.tasks).map(task => ({ id: task.id, title: task.title })) ?? [],
    [board]
  )

  // Client-side filters, mirroring the dashboard (search over title/body/id).
  const filtered = useMemo(() => {
    if (!board) {
      return null
    }

    const q = search.trim().toLowerCase()

    const keep = (task: KanbanTask) =>
      (!q || `${task.title} ${task.body ?? ''} ${task.id}`.toLowerCase().includes(q)) &&
      (!tenant || task.tenant === tenant) &&
      (!assignee || task.assignee === assignee) &&
      (!attentionOnly || (task.warnings?.count ?? 0) > 0)

    return { ...board, columns: board.columns.map(col => ({ ...col, tasks: col.tasks.filter(keep) })) }
  }, [board, search, tenant, assignee, attentionOnly])

  const total = filtered?.columns.reduce((sum, col) => sum + col.tasks.length, 0) ?? 0

  // A completed card can still carry crash-history diagnostics (the backend
  // scans run history, and Done is not excluded) — but "needs attention" is a
  // to-do signal, and a done card has nothing left to do. Count active lanes only.
  const attention = board?.columns.reduce(
    (sum, col) =>
      col.name === 'done' || col.name === 'archived'
        ? sum
        : sum + col.tasks.filter(task => (task.warnings?.count ?? 0) > 0).length,
    0
  )

  const moveMut = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) => patchTask(id, { status }),
    onMutate: async ({ id, status }) => {
      await qc.cancelQueries({ queryKey: boardKey(slug, archived) })
      const previous = qc.getQueryData<KanbanBoard>(boardKey(slug, archived))

      if (previous) {
        qc.setQueryData(boardKey(slug, archived), moveCard(previous, id, status))
      }

      return { previous }
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        qc.setQueryData(boardKey(slug, archived), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: (_data, _err, vars) => {
      void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
      void qc.invalidateQueries({ queryKey: ['kanban', 'task', slug, vars.id] })
    }
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteTask(id),
    onMutate: async id => {
      await qc.cancelQueries({ queryKey: boardKey(slug, archived) })
      const previous = qc.getQueryData<KanbanBoard>(boardKey(slug, archived))

      if (previous) {
        qc.setQueryData(boardKey(slug, archived), removeCard(previous, id))
      }

      return { previous }
    },
    onError: (err, _id, context) => {
      if (context?.previous) {
        qc.setQueryData(boardKey(slug, archived), context.previous)
      }

      host.notify({ kind: 'error', message: errText(err) })
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ['kanban', 'board'] })
  })

  const onMove = (id: string, status: string) => {
    const task = board?.columns.flatMap(col => col.tasks).find(candidate => candidate.id === id)

    if (!task || task.status === status) {
      return
    }

    if (isLockedTarget(status)) {
      host.notify({ kind: 'info', message: LOCKED_COLUMNS[status] })

      return
    }

    moveMut.mutate({ id, status })
  }

  const errorMessage = error ? errText(error) : null

  return (
    <div className="relative flex h-full flex-col overflow-hidden bg-(--ui-surface-background)">
      {/* Page-owned titlebar chrome: exists exactly while this page is mounted. */}
      <Contribute area={TITLEBAR_AREAS.center} id="kanban:board-switcher">
        <BoardSwitcher />
      </Contribute>

      <header className="flex shrink-0 flex-wrap items-center gap-2 px-4 py-2">
        <h1 className="text-sm font-semibold text-foreground">Kanban</h1>
        <span className="rounded-full bg-(--ui-bg-quaternary) px-1.5 py-px text-[0.625rem] tabular-nums text-(--ui-text-tertiary)">
          {total}
        </span>
        {board && (
          <FilterMenu
            archived={archived}
            assignee={assignee}
            board={board}
            onArchived={setArchived}
            onAssignee={setAssignee}
            onTenant={setTenant}
            tenant={tenant}
          />
        )}
        <SearchField aria-label="Filter cards" onChange={setSearch} placeholder="Filter cards…" value={search} />
        {Boolean(attention) && (
          <Tip label={attentionOnly ? 'Show all tasks' : 'Show only tasks with active diagnostics'}>
            <button
              className={cn(
                'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[0.6875rem] font-medium text-destructive transition-colors',
                'bg-[color-mix(in_srgb,var(--destructive)_8%,transparent)] hover:bg-[color-mix(in_srgb,var(--destructive)_14%,transparent)]',
                attentionOnly && 'bg-[color-mix(in_srgb,var(--destructive)_16%,transparent)]'
              )}
              onClick={() => setAttentionOnly(!attentionOnly)}
              type="button"
            >
              <Codicon name="warning" size="0.7rem" />
              {attention} need{attention === 1 ? 's' : ''} attention
            </button>
          </Tip>
        )}
        <div className="ml-auto flex items-center gap-1">
          <Tip label="Run a dispatch tick now instead of waiting for the next one">
            <Button
              aria-label="Nudge dispatcher"
              disabled={nudge.isPending}
              onClick={() => nudge.mutate()}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="rocket" size="0.85rem" />
            </Button>
          </Tip>
          <Tip label="Orchestration settings">
            <Button
              aria-label="Orchestration settings"
              className={cn(settingsOpen && 'bg-(--ui-control-active-background) text-foreground')}
              onClick={() => setSettingsOpen(!settingsOpen)}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="organization" size="0.85rem" />
            </Button>
          </Tip>
          <Button onClick={() => setAddStatus('triage')} size="sm">
            <Codicon name="add" size="0.8rem" />
            New task
          </Button>
        </div>
      </header>

      {settingsOpen && <OrchestrationPanel />}

      {board && <Intro />}

      {errorMessage && !board ? (
        <div className="grid flex-1 place-items-center">
          <ErrorState title={errorMessage} />
        </div>
      ) : !filtered ? (
        <div className="grid flex-1 place-items-center">
          <Loader type="lemniscate-bloom" />
        </div>
      ) : total === 0 ? (
        <div className="grid flex-1 place-items-center px-4 text-center">
          <div className="flex flex-col items-center gap-2">
            <Codicon className="text-(--ui-text-quaternary)" name="project" size="1.25rem" />
            <p className="text-xs text-(--ui-text-tertiary)">
              {search || tenant || assignee || attentionOnly ? 'No tasks match the filters' : 'No tasks on this board'}
            </p>
            <Button className="mt-0.5" onClick={() => setAddStatus('triage')} size="sm" variant="outline">
              <Codicon name="add" size="0.75rem" />
              New task
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-1 gap-2 overflow-x-auto px-4 pt-1 pb-3">
          {filtered.columns.map(col => (
            <Column
              column={col}
              columns={columnNames}
              key={col.name}
              onAdd={setAddStatus}
              onDelete={id => deleteMut.mutate(id)}
              onDropTask={onMove}
              onMove={onMove}
              onOpen={setOpenId}
            />
          ))}
        </div>
      )}

      {board && (
        <div className="pointer-events-none absolute right-4 bottom-1.5 z-10 rounded bg-[color-mix(in_srgb,var(--ui-surface-background)_85%,transparent)] leading-none">
          <Freshness fetching={isFetching} updatedAt={dataUpdatedAt} />
        </div>
      )}

      <NewTaskDialog onClose={() => setAddStatus(null)} parents={parentOptions} target={addStatus} />
      <TaskDrawer columns={columnNames} id={openId} onClose={() => setOpenId(null)} onOpen={setOpenId} />
    </div>
  )
}
