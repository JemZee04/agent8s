<script lang="ts">
  import { app, ask, diff, gitAction, refreshDiff, selectedChat, toggleDiff } from './lib/state.svelte';

  const chat = $derived(selectedChat());
  const totals = $derived({
    add: diff.files.reduce((n, f) => n + f.add, 0),
    del: diff.files.reduce((n, f) => n + f.del, 0),
  });
  const LINE_CAP = 600;

  let opened = $state<Record<string, boolean>>({});
  let showAll = $state<Record<string, boolean>>({});
  let committing = $state(false);
  let message = $state('');
  let busy = $state(false);

  // Open the first files by default, as long as the whole thing stays small.
  $effect(() => {
    const next: Record<string, boolean> = {};
    let budget = 300;
    for (const f of diff.files) {
      next[f.path] = opened[f.path] ?? budget > 0;
      budget -= f.lines.length;
    }
    opened = next;
  });

  async function commit() {
    busy = true;
    if (await gitAction('commit', message)) { committing = false; message = ''; }
    busy = false;
  }

  async function merge() {
    if (!chat) return;
    const ok = await ask(
      `Влить ${chat.branch} в ${chat.default_branch}?`,
      'Несохранённые изменения будут закоммичены, затем ветка вольётся в основной репозиторий проекта. При конфликте слияние откатится.',
      'Влить',
    );
    if (!ok) return;
    busy = true;
    await gitAction('merge', '');
    busy = false;
  }

  const label: Record<string, string> = { added: 'новый', deleted: 'удалён', renamed: 'переименован', binary: 'бинарный', modified: '' };
</script>

<aside>
  <header>
    <div class="ttl">
      {#if app.mobile}<button class="ghost back" aria-label="Назад к чату" onclick={toggleDiff}>‹</button>{/if}
      <b>Изменения</b>
      {#if diff.files.length}
        <span class="sum"><span class="a">+{totals.add}</span> <span class="d">−{totals.del}</span> · {diff.files.length} файл.</span>
      {/if}
    </div>
    <button class="ghost" onclick={refreshDiff} disabled={diff.loading} title="Обновить">{diff.loading ? '…' : '↻'}</button>
  </header>

  {#if diff.files.length}
    <div class="actions">
      {#if committing}
        <input type="text" bind:value={message} placeholder="Сообщение коммита (можно пустое)" onkeydown={(e) => e.key === 'Enter' && commit()} />
        <button class="btn" disabled={busy} onclick={commit}>Коммит</button>
        <button class="ghost" onclick={() => (committing = false)}>✕</button>
      {:else}
        <button class="btn" disabled={busy} onclick={() => (committing = true)}>Закоммитить…</button>
        {#if chat?.mode === 'worktree'}
          <button class="primary" disabled={busy || chat.running} onclick={merge}>Влить в {chat.default_branch}</button>
        {/if}
      {/if}
    </div>
  {:else if chat?.mode === 'worktree'}
    <div class="actions"><button class="primary" disabled={busy || chat.running} onclick={merge}>Влить в {chat.default_branch}</button></div>
  {/if}

  <div class="files">
    {#if diff.error}
      <div class="msg err">{diff.error}</div>
    {:else if !diff.files.length && !diff.loading}
      <div class="msg">Изменений нет. Закоммиченное уже в ветке {chat?.branch}.</div>
    {/if}
    {#if diff.truncated}<div class="msg err">Дифф очень большой — показана только его часть.</div>{/if}

    {#each diff.files as f (f.path)}
      <section>
        <button class="fhead" onclick={() => (opened[f.path] = !opened[f.path])}>
          <span class="chev" class:open={opened[f.path]}>›</span>
          <span class="path" title={f.path}>{f.path}</span>
          {#if label[f.status]}<span class="tag">{label[f.status]}</span>{/if}
          <span class="a">+{f.add}</span><span class="d">−{f.del}</span>
        </button>
        {#if opened[f.path]}
          <div class="code selectable">
            {#if f.status === 'binary'}<div class="line note">бинарный файл</div>{/if}
            {#each showAll[f.path] ? f.lines : f.lines.slice(0, LINE_CAP) as l}
              <div class="line {l.t}">
                <span class="no">{l.a ?? ''}</span><span class="no">{l.b ?? ''}</span>
                <span class="sign">{l.t === 'add' ? '+' : l.t === 'del' ? '−' : ''}</span><span class="txt">{l.text}</span>
              </div>
            {/each}
            {#if f.lines.length > LINE_CAP && !showAll[f.path]}
              <button class="more" onclick={() => (showAll[f.path] = true)}>Показать ещё {f.lines.length - LINE_CAP} строк</button>
            {/if}
          </div>
        {/if}
      </section>
    {/each}
  </div>
</aside>

<style>
  aside { display: flex; flex-direction: column; min-height: 0; background: var(--bg); border-left: 1px solid var(--border); }
  header { display: flex; align-items: center; justify-content: space-between; padding: max(10px, env(safe-area-inset-top)) 12px 6px 16px; }
  .back { font-size: 26px; line-height: 1; padding: 0 10px 2px; margin-left: -12px; align-self: center; }
  .ttl { display: flex; align-items: baseline; gap: 10px; }
  .sum { color: var(--text-dim); font-size: 12px; }
  .a { color: var(--ok); } .d { color: var(--err); }
  .actions { display: flex; gap: 6px; padding: 0 14px 10px; align-items: center; }
  .actions input { flex: 1; }
  .files { flex: 1; overflow: auto; padding: 0 0 max(20px, env(safe-area-inset-bottom)); }
  .msg { color: var(--text-dim); padding: 10px 16px; font-size: 12.5px; }
  .msg.err { color: var(--err); }
  section { border-top: 1px solid var(--border); }
  .fhead { display: flex; gap: 8px; align-items: center; width: 100%; text-align: left; background: var(--bg-side); border: 0; padding: 5px 12px; font: 12px var(--mono); min-width: 0; position: sticky; top: 0; z-index: 1; }
  .fhead:hover { background: var(--bg-active); }
  .chev { transition: transform 0.12s; color: var(--text-faint); width: 8px; flex: none; }
  .chev.open { transform: rotate(90deg); }
  .path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; }
  .tag { font: 11px var(--sans); color: var(--text-dim); background: var(--bg-active); border-radius: 8px; padding: 0 7px; }
  .code { font: 12px/1.5 var(--mono); overflow-x: auto; }
  .line { display: flex; white-space: pre; min-width: max-content; }
  .line.add { background: var(--add-bg); } .line.del { background: var(--del-bg); }
  .line.hunk { color: var(--text-faint); background: var(--bg-code); padding: 1px 0 1px 8px; }
  .line.note { color: var(--text-faint); padding-left: 8px; }
  .no { width: 38px; text-align: right; color: var(--text-faint); padding-right: 8px; flex: none; user-select: none; }
  .sign { width: 14px; flex: none; color: var(--text-dim); user-select: none; }
  .txt { padding-right: 16px; }
  .more { margin: 6px 12px; background: transparent; border: 0; color: var(--accent); }
</style>
