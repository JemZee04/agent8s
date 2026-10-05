<script lang="ts">
  import { onMount } from 'svelte';
  import { app, importSessions, imports, loadImportList } from './lib/state.svelte';

  let query = $state('');
  let picked = $state<Record<string, boolean>>({});

  onMount(() => void loadImportList());

  const visible = $derived(
    imports.sessions.filter((s) => {
      const q = query.trim().toLowerCase();
      return !q || s.title.toLowerCase().includes(q) || s.cwd.toLowerCase().includes(q);
    }),
  );
  const fresh = $derived(visible.filter((s) => s.importable));
  const selected = $derived(imports.sessions.filter((s) => picked[s.id] && s.importable).map((s) => s.id));

  const short = (p: string) => p.replace(/^\/Users\/[^/]+/, '~');
  const date = (t: number) => new Date(t * 1000).toLocaleDateString('ru', { day: 'numeric', month: 'short', year: '2-digit' });
  const size = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} МБ` : `${Math.max(1, Math.round(n / 1e3))} КБ`);

  function toggleAll() {
    const all = fresh.length > 0 && fresh.every((s) => picked[s.id]);
    for (const s of fresh) picked[s.id] = !all;
  }

  async function run() {
    await importSessions(selected);
    picked = {};
  }
</script>

<div class="scrim" role="presentation" onclick={() => (app.importOpen = false)} onkeydown={(e) => e.key === 'Escape' && (app.importOpen = false)}>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Импорт из Claude Code" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
    <h2>Импорт из Claude Code</h2>
    <p class="dim">
      Сессии из <code>~/.claude</code> станут чатами: история сохранится, а дальше Claude продолжит именно эту сессию.
      Чат работает в той же папке, где шла сессия (прямо в проекте). Файлы сессий не меняются.
    </p>

    <div class="bar">
      <input type="text" bind:value={query} placeholder="Поиск по названию или папке" />
      <button class="btn" onclick={toggleAll} disabled={!fresh.length}>Выбрать все новые</button>
    </div>

    <div class="list">
      {#if imports.loading && !imports.sessions.length}
        <div class="empty">Читаю сессии…</div>
      {:else if !visible.length}
        <div class="empty">Ничего не найдено.</div>
      {/if}
      {#each visible as s (s.id)}
        <label class="row" class:off={!s.importable}>
          <input type="checkbox" disabled={!s.importable} bind:checked={picked[s.id]} />
          <span class="body">
            <span class="title">{s.title}</span>
            <span class="meta">
              {short(s.cwd) || '—'}{s.branch ? ` · ${s.branch}` : ''} · {date(s.mtime)} · {size(s.size)}
              {#if s.reason}<b class="why"> · {s.reason}</b>{/if}
            </span>
          </span>
        </label>
      {/each}
    </div>

    <div class="actions">
      <span class="dim">{selected.length ? `Выбрано: ${selected.length}` : ''}</span>
      <button class="btn" onclick={() => (app.importOpen = false)}>Закрыть</button>
      <button class="primary" disabled={!selected.length || imports.busy} onclick={run}>
        {imports.busy ? 'Импортирую…' : 'Импортировать'}
      </button>
    </div>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; place-items: center; z-index: 50; }
  .dialog { width: min(680px, 94vw); max-height: 90vh; display: flex; flex-direction: column; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 8px; font-size: 17px; }
  .dim { color: var(--text-dim); font-size: 12.5px; line-height: 1.45; margin: 0 0 12px; }
  code { font: 12px var(--mono); background: var(--bg-code); border-radius: 5px; padding: 0 5px; }
  .bar { display: flex; gap: 8px; margin-bottom: 10px; }
  .bar input { flex: 1; }
  .list { flex: 1; min-height: 120px; overflow-y: auto; border: 1px solid var(--border); border-radius: 10px; }
  .row { display: flex; gap: 10px; align-items: flex-start; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  .row:last-child { border-bottom: 0; }
  .row:hover:not(.off) { background: var(--bg-hover); }
  .row.off { opacity: 0.55; }
  .row input { margin-top: 3px; }
  .body { min-width: 0; display: flex; flex-direction: column; }
  .title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { color: var(--text-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .why { font-weight: 500; }
  .empty { color: var(--text-dim); text-align: center; padding: 30px 0; }
  .actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 12px; }
  .actions .dim { margin: 0 auto 0 0; }
  @media (max-width: 760px) { .dialog { width: 100%; max-height: 94dvh; } }
</style>
