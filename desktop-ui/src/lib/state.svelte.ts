import { api, ApiError, connect, openExternal, relayMode, setRelay } from './api';
import { parseDiff, type DiffFile } from './diff';
import { forgetKeys, loadKeys, pairFromText, RelayTransport, type LinkState } from './relay';
import type { AgentSpec, Chat, Message, Project, ServerEvent } from './types';

export const app = $state({
  ready: false,
  online: false,
  link: 'connecting' as LinkState,
  relayMode,
  needsPairing: false,
  mobile: false,
  mobileView: 'list' as 'list' | 'chat',
  phoneDialogOpen: false,
  importOpen: false,
  hasMore: {} as Record<number, boolean>,
  freshPairLink: '',
  agents: [] as AgentSpec[],
  projects: [] as Project[],
  chats: [] as Chat[],
  selectedId: null as number | null,
  messages: {} as Record<number, Message[]>,
  unread: {} as Record<number, boolean>,
  drafts: {} as Record<number, string>,
  diffOpen: false,
  newChatOpen: false,
  newChatProject: null as number | null,
  toast: null as null | { text: string; kind: 'error' | 'info'; action?: { label: string; run: () => void } },
  previewOpen: false,
  confirm: null as null | { title: string; body: string; okLabel: string; danger: boolean; resolve: (ok: boolean) => void },
  now: Date.now(),
});

export interface PreviewInfo { cap: string; port: number; chat_id: number; exp: number; url: string; kind: 'port' | 'file'; name: string; root?: string; missing?: string[] }
export interface HtmlFile { path: string; rel: string; size: number; mtime: number }
export interface PortInfo { port: number; command: string; cwd: string; kind: 'mine' | 'dev' | 'web' | 'other' }
export const previews = $state({ list: [] as PreviewInfo[], ports: [] as PortInfo[], files: [] as HtmlFile[], loading: false });

export const diff = $state({
  chatId: null as number | null,
  files: [] as DiffFile[],
  status: '',
  truncated: false,
  loading: false,
  error: '',
});

const store = {
  get(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key: string, value: string) {
    try { localStorage.setItem(key, value); } catch { /* storage may be blocked */ }
  },
};

// -- helpers ----------------------------------------------------------------

export const selectedChat = (): Chat | undefined => app.chats.find((c) => c.id === app.selectedId);
export const agentSpec = (id: string) => app.agents.find((a) => a.id === id);
export const agentLabel = (id: string | null) => (id ? (agentSpec(id)?.label ?? id) : '');
export function modelLabel(agent: string, model: string | null): string {
  if (!model) return 'по умолчанию';
  return agentSpec(agent)?.models.find((m) => m.id === model)?.label ?? model;
}

let toastTimer: number | undefined;
export function notify(text: string, kind: 'error' | 'info' = 'error', action?: { label: string; run: () => void }) {
  app.toast = { text, kind, action };
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (app.toast = null), action ? 15000 : kind === 'error' ? 7000 : 2500);
}

function fail(e: unknown) {
  notify(e instanceof ApiError ? e.message : 'Нет связи с сервером.');
}

export function ask(title: string, body: string, okLabel = 'OK', danger = false): Promise<boolean> {
  return new Promise((resolve) => {
    app.confirm = { title, body, okLabel, danger, resolve: (ok) => { app.confirm = null; resolve(ok); } };
  });
}

// -- loading -----------------------------------------------------------------

const PAGE = 60;
const loading = new Set<number>();
const buffered = new Map<number, ServerEvent[]>();

function upsertChat(chat: Chat) {
  const i = app.chats.findIndex((c) => c.id === chat.id);
  if (i >= 0) app.chats[i] = chat;
  else app.chats.push(chat);
}

export async function loadChat(id: number) {
  loading.add(id);
  buffered.set(id, []);
  try {
    // A page, not the whole history: imported sessions can hold thousands of messages.
    const data = await api.get<{ chat: Chat; messages: Message[]; has_more: boolean }>(`/api/chats/${id}?limit=${PAGE}`);
    upsertChat(data.chat);
    app.messages[id] = data.messages;
    app.hasMore[id] = data.has_more;
  } catch (e) {
    fail(e);
  } finally {
    loading.delete(id);
    // Events that arrived while the snapshot was in flight; stale ones are
    // dropped by the per-message rev check.
    for (const ev of buffered.get(id) ?? []) apply(ev);
    buffered.delete(id);
  }
}

export async function loadEarlier(id: number) {
  const first = app.messages[id]?.[0];
  if (!first) return;
  try {
    const data = await api.get<{ messages: Message[]; has_more: boolean }>(`/api/chats/${id}?limit=${PAGE}&before=${first.id}`);
    app.messages[id] = [...data.messages, ...(app.messages[id] ?? [])];
    app.hasMore[id] = data.has_more;
  } catch (e) {
    fail(e);
  }
}

async function bootstrap() {
  const data = await api.get<{ catalog: { agents: AgentSpec[] }; projects: Project[]; chats: Chat[] }>('/api/bootstrap');
  app.agents = data.catalog.agents;
  app.projects = data.projects;
  app.chats = data.chats;
  void loadImportList(true);
  void api.get<{ previews: PreviewInfo[] }>('/api/previews').then((r) => (previews.list = r.previews), () => {});
  const remembered = Number(store.get('agent8s-selected'));
  const target = app.chats.find((c) => c.id === (app.selectedId ?? remembered)) ?? sorted()[0];
  const showList = !app.ready || app.selectedId === null;
  if (target) await select(target.id);
  else app.selectedId = null;
  if (app.mobile && showList) app.mobileView = 'list';
  app.ready = true;
}

export function sorted(): Chat[] {
  return [...app.chats].sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id);
}

export interface Group { project_id: number; name: string; chats: Chat[] }

// Projects with recent activity first; projects without chats last, by name.
export function groups(): Group[] {
  const byProject = new Map<number, Chat[]>();
  for (const chat of sorted()) byProject.set(chat.project_id, [...(byProject.get(chat.project_id) ?? []), chat]);
  const result: Group[] = app.projects.map((p) => ({ project_id: p.id, name: p.name, chats: byProject.get(p.id) ?? [] }));
  return result.sort((a, b) => {
    if (!a.chats.length !== !b.chats.length) return a.chats.length ? -1 : 1;
    if (a.chats.length) return b.chats[0].updated_at.localeCompare(a.chats[0].updated_at);
    return a.name.localeCompare(b.name);
  });
}

export async function select(id: number) {
  app.selectedId = id;
  app.mobileView = 'chat';
  delete app.unread[id];
  store.set('agent8s-selected', String(id));
  if (diff.chatId !== id) { diff.files = []; diff.status = ''; diff.error = ''; diff.chatId = id; }
  await loadChat(id);
  if (app.diffOpen) void refreshDiff();
}

// -- live events ---------------------------------------------------------------

let queue: ServerEvent[] = [];
let scheduled = false;

function enqueue(ev: ServerEvent) {
  queue.push(ev);
  if (!scheduled) {
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      const batch = queue;
      queue = [];
      for (const e of batch) apply(e);
    });
  }
}

function upsertMessage(msg: Message) {
  const list = app.messages[msg.chat_id];
  if (!list) return;
  const i = list.findIndex((m) => m.id === msg.id);
  if (i < 0) list.push(msg);
  else if ((msg.rev ?? 0) > (list[i].rev ?? 0)) list[i] = msg;
}

let diffTimer: number | undefined;

function apply(ev: ServerEvent) {
  if ('chat_id' in ev && loading.has(ev.chat_id)) {
    buffered.get(ev.chat_id)?.push(ev);
    return;
  }
  switch (ev.t) {
    case 'chat':
      upsertChat(ev.chat);
      break;
    case 'chat_deleted': {
      app.chats = app.chats.filter((c) => c.id !== ev.chat_id);
      delete app.messages[ev.chat_id];
      if (app.selectedId === ev.chat_id) {
        const next = sorted()[0];
        if (next) void select(next.id);
        else app.selectedId = null;
      }
      break;
    }
    case 'msg_new':
      upsertMessage(ev.message);
      break;
    case 'msg_ops': {
      const msg = app.messages[ev.chat_id]?.find((m) => m.id === ev.msg_id);
      if (!msg || ev.rev <= (msg.rev ?? 0)) return;
      for (const op of ev.ops) {
        if (op.op === 'append') {
          const part = msg.parts[op.i];
          if (part && (part.type === 'text' || part.type === 'thinking')) part.text += op.text;
        } else {
          msg.parts[op.i] = op.part;
        }
      }
      msg.rev = ev.rev;
      break;
    }
    case 'msg_status': {
      const msg = app.messages[ev.chat_id]?.find((m) => m.id === ev.msg_id);
      if (msg) msg.status = ev.status;
      if (ev.chat_id !== app.selectedId) app.unread[ev.chat_id] = true;
      break;
    }
    case 'preview': {
      previews.list = previews.list.filter((p) => p.cap !== ev.cap);
      if (ev.op === 'add' && ev.url) {
        const info: PreviewInfo = { cap: ev.cap, port: ev.port, chat_id: ev.chat_id, exp: ev.exp, url: ev.url, kind: ev.kind, name: ev.name };
        previews.list.push(info);
        // Something just shared from the other device: offer to open it, wherever you are looking.
        if (!app.previewOpen) {
          const what = ev.kind === 'file' ? `Файл ${ev.name} готов к чтению` : `Превью сайта (порт ${ev.port}) готово`;
          notify(what, 'info', { label: 'Открыть', run: () => openPreview(info.url) });
        }
      }
      break;
    }
    case 'diff_changed':
      if (ev.chat_id === app.selectedId && app.diffOpen) {
        window.clearTimeout(diffTimer);
        diffTimer = window.setTimeout(() => void refreshDiff(), 300);
      }
      break;
  }
}

let transport: RelayTransport | null = null;

// (Re)synchronise: anything may have happened while we were away.
function resync() {
  for (const id of Object.keys(app.messages)) delete app.messages[Number(id)];
  void bootstrap().catch(fail);
}

export function start() {
  window.setInterval(() => (app.now = Date.now()), 1000);
  if (relayMode) {
    void startRelay();
    return;
  }
  connect(
    enqueue,
    () => {
      app.online = true;
      app.link = 'online';
      resync();
    },
    () => {
      app.online = false;
      app.link = 'connecting';
    },
  );
}

// Phone: pair from a link in the URL fragment, a pasted key, or the key stored earlier.
async function startRelay(pasted?: string) {
  let keys = null;
  try {
    if (pasted) keys = await pairFromText(pasted);
    else if (/[#&]k=/.test(location.hash)) {
      keys = await pairFromText(location.hash);
      app.freshPairLink = location.href; // shown once so the key can be handed to the home-screen app
      history.replaceState(null, '', location.pathname + location.search); // the key must not linger in the address bar
    } else keys = await loadKeys();
  } catch (e) {
    fail(e);
  }
  if (!keys) {
    app.needsPairing = true;
    return;
  }
  app.needsPairing = false;
  transport?.stop();
  transport = new RelayTransport(keys, {
    event: enqueue,
    open: resync,
    close: () => (app.online = false),
    state: (state) => {
      app.link = state;
      app.online = state === 'online';
    },
  });
  setRelay(transport);
  transport.start();
}

export const pairWith = (text: string) => startRelay(text);

export async function unpair() {
  transport?.stop();
  transport = null;
  setRelay(null);
  await forgetKeys();
  app.chats = [];
  app.selectedId = null;
  app.ready = false;
  app.needsPairing = true;
}

// -- actions ----------------------------------------------------------------------

export async function send(text: string): Promise<boolean> {
  const id = app.selectedId;
  if (id === null || !text.trim()) return false;
  try {
    const r = await api.post<{ user: Message; assistant: Message }>(`/api/chats/${id}/send`, { text });
    upsertMessage(r.user);
    upsertMessage(r.assistant);
    return true;
  } catch (e) {
    fail(e);
    return false;
  }
}

export async function stop() {
  if (app.selectedId === null) return;
  try { await api.post(`/api/chats/${app.selectedId}/stop`); } catch (e) { fail(e); }
}

export async function patchChat(id: number, body: Partial<Pick<Chat, 'title' | 'agent' | 'model' | 'effort'>>) {
  try {
    upsertChat(await api.patch<Chat>(`/api/chats/${id}`, body));
  } catch (e) {
    fail(e);
  }
}

export async function createChat(project_id: number, agent: string, model: string, effort: string, mode: string) {
  try {
    const chat = await api.post<Chat>('/api/chats', { project_id, agent, model, effort, mode });
    upsertChat(chat);
    await select(chat.id);
    return chat;
  } catch (e) {
    fail(e);
    return null;
  }
}

export async function deleteChat(chat: Chat) {
  const body =
    chat.mode === 'worktree'
      ? `Папка ${chat.worktree_path} и ветка ${chat.branch} будут удалены вместе с несохранёнными изменениями.`
      : 'Чат будет удалён. Файлы проекта не затрагиваются.';
  if (!(await ask(`Удалить чат «${chat.title}»?`, body, 'Удалить', true))) return;
  try { await api.del(`/api/chats/${chat.id}`); } catch (e) { fail(e); }
}

export async function addProject(name: string, path: string): Promise<Project | null> {
  try {
    const project = await api.post<Project>('/api/projects', { name, path });
    app.projects.push(project);
    return project;
  } catch (e) {
    fail(e);
    return null;
  }
}

export async function refreshDiff() {
  const id = app.selectedId;
  if (id === null) return;
  diff.loading = true;
  try {
    const r = await api.get<{ diff: string; truncated: boolean; status: string }>(`/api/chats/${id}/diff`);
    if (app.selectedId !== id) return;
    diff.chatId = id;
    diff.files = parseDiff(r.diff);
    diff.status = r.status;
    diff.truncated = r.truncated;
    diff.error = '';
  } catch (e) {
    diff.error = e instanceof ApiError ? e.message : 'Нет связи с сервером.';
  } finally {
    diff.loading = false;
  }
}

export function toggleDiff() {
  app.diffOpen = !app.diffOpen;
  if (app.diffOpen) void refreshDiff();
}

export async function gitAction(kind: 'commit' | 'merge', message: string): Promise<boolean> {
  const id = app.selectedId;
  if (id === null) return false;
  try {
    const r = await api.post<{ into?: string }>(`/api/chats/${id}/${kind}`, { message });
    notify(kind === 'merge' ? `Влито в ${r.into ?? 'основную ветку'}` : 'Закоммичено', 'info');
    await refreshDiff();
    return true;
  } catch (e) {
    fail(e);
    return false;
  }
}

export async function openFolder(target: 'finder' | 'terminal' | 'code') {
  if (app.selectedId === null) return;
  try { await api.post(`/api/chats/${app.selectedId}/open`, { target }); } catch (e) { fail(e); }
}

export async function addWritableDir(path: string) {
  if (app.selectedId === null) return;
  try {
    upsertChat(await api.post<Chat>(`/api/chats/${app.selectedId}/dirs`, { path }));
    notify(`Агент сможет писать в ${path}`, 'info');
  } catch (e) {
    fail(e);
  }
}

// -- phone pairing (desktop side) ---------------------------------------------------

export interface RemoteInfo {
  configured: boolean;
  relay: string | null;
  state: string;
  error: string;
  clients: number;
}

export const remote = $state({ info: null as RemoteInfo | null, url: '', svg: '', busy: false });

export async function refreshRemote() {
  try {
    remote.info = await api.get<RemoteInfo>('/api/remote');
    if (remote.info.configured && !remote.url) await loadPairing();
  } catch (e) {
    fail(e);
  }
}

async function loadPairing() {
  const r = await api.get<{ url: string; svg: string }>('/api/remote/pairing');
  remote.url = r.url;
  remote.svg = r.svg;
}

export async function pairPhone(relay: string) {
  remote.busy = true;
  try {
    remote.info = await api.post<RemoteInfo>('/api/remote/pair', { relay });
    await loadPairing();
  } catch (e) {
    fail(e);
  } finally {
    remote.busy = false;
  }
}

export async function disablePhone() {
  try {
    await api.del('/api/remote');
    remote.url = '';
    remote.svg = '';
    await refreshRemote();
  } catch (e) {
    fail(e);
  }
}

// -- import from Claude Code (desktop only) -------------------------------------------

export interface ImportableSession {
  id: string;
  title: string;
  cwd: string;
  branch: string;
  mtime: number;
  size: number;
  imported: boolean;
  importable: boolean;
  reason: string;
}

export const imports = $state({ sessions: [] as ImportableSession[], loading: false, busy: false });

export const freshSessions = () => imports.sessions.filter((s) => s.importable).length;

export async function loadImportList(quiet = false) {
  imports.loading = true;
  try {
    imports.sessions = (await api.get<{ sessions: ImportableSession[] }>('/api/import/claude')).sessions;
  } catch (e) {
    if (!quiet) fail(e); // the badge is a convenience: an old server without import must not nag
  } finally {
    imports.loading = false;
  }
}

export async function importSessions(ids: string[]) {
  imports.busy = true;
  try {
    const { results } = await api.post<{ results: { ok: boolean; error?: string }[] }>('/api/import/claude', { ids });
    const done = results.filter((r) => r.ok).length;
    const failed = results.filter((r) => !r.ok);
    notify(
      failed.length ? `Импортировано: ${done}, не удалось: ${failed.length} (${failed[0].error})` : `Импортировано чатов: ${done}`,
      failed.length ? 'error' : 'info',
    );
    // New chats and possibly new projects: reload the sidebar's data.
    const data = await api.get<{ projects: Project[]; chats: Chat[] }>('/api/bootstrap');
    app.projects = data.projects;
    app.chats = data.chats;
    await loadImportList();
  } catch (e) {
    fail(e);
  } finally {
    imports.busy = false;
  }
}

// -- previews: open a site running on the Mac's localhost -------------------------------

export function openPreview(url: string) {
  if (relayMode) window.open(url, '_blank', 'noopener');
  else openExternal(url);
}

export async function loadPorts(chatId: number) {
  previews.loading = true;
  try {
    const r = await api.get<{ ports: PortInfo[]; previews: PreviewInfo[] }>(`/api/chats/${chatId}/ports`);
    previews.ports = r.ports;
    previews.list = [...previews.list.filter((p) => p.chat_id !== chatId), ...r.previews];
  } catch (e) {
    fail(e);
  } finally {
    previews.loading = false;
  }
}

export async function refreshPreviews() {
  try {
    previews.list = (await api.get<{ previews: PreviewInfo[] }>('/api/previews')).previews;
  } catch { /* the next tick tries again */ }
}

export async function loadHtmlFiles(chatId: number) {
  try {
    previews.files = (await api.get<{ files: HtmlFile[] }>(`/api/chats/${chatId}/html`)).files;
  } catch (e) {
    fail(e);
  }
}

export const shareFile = (chatId: number, file: string) => share(chatId, { file });
export const sharePort = (chatId: number, port: number) => share(chatId, { port });

async function share(chatId: number, target: { port: number } | { file: string }): Promise<PreviewInfo | null> {
  try {
    const info = await api.post<PreviewInfo>(`/api/chats/${chatId}/preview`, target);
    previews.list = [...previews.list.filter((p) => p.cap !== info.cap), info];
    return info;
  } catch (e) {
    fail(e);
    return null;
  }
}

export async function stopPreview(cap: string) {
  try {
    await api.del(`/api/previews/${cap}`);
    previews.list = previews.list.filter((p) => p.cap !== cap);
  } catch (e) {
    fail(e);
  }
}
