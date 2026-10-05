<script lang="ts">
  import { app, groups, select } from './lib/state.svelte';

  function newChat(projectId: number | null = null) {
    app.newChatProject = projectId;
    app.newChatOpen = true;
  }
</script>

<aside>
  <div class="top">
    <span class="brand">agent8s</span>
    <button class="ghost" onclick={() => newChat()} title="Новый чат (⌘N)" aria-label="Новый чат">＋ Новый чат</button>
  </div>

  <nav>
    {#each groups() as g (g.project_id)}
      <section>
        <div class="ghead">
          <span class="gname">{g.name}</span>
          <button class="ghost add" onclick={() => newChat(g.project_id)} title="Новый чат в {g.name}" aria-label="Новый чат в {g.name}">＋</button>
        </div>
        {#each g.chats as c (c.id)}
          <button class="row" class:active={c.id === app.selectedId} onclick={() => select(c.id)}>
            <span class="state">
              {#if c.running}<i class="spinner"></i>
              {:else if c.status === 'error'}<i class="dot err" title="Последний ход завершился ошибкой"></i>
              {:else if app.unread[c.id]}<i class="dot unread" title="Есть новый ответ"></i>{/if}
            </span>
            <span class="title">{c.title}</span>
            {#if c.changes}<span class="chg" title="Изменённых файлов">{c.changes}</span>{/if}
            <span class="ag {c.agent}">{c.agent}</span>
          </button>
        {:else}
          <div class="empty">нет чатов</div>
        {/each}
      </section>
    {/each}
    {#if app.ready && app.projects.length === 0}
      <div class="hint">Добавьте проект, создав первый чат.</div>
    {/if}
  </nav>

  {#if !app.online}
    <div class="offline">Нет связи с сервером — переподключаюсь…</div>
  {/if}
</aside>

<style>
  aside { background: var(--bg-side); border-right: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; }
  .top { display: flex; align-items: center; justify-content: space-between; padding: 14px 12px 10px 16px; }
  .brand { font-weight: 650; letter-spacing: 0.01em; }
  nav { overflow-y: auto; padding: 0 8px 12px; flex: 1; }
  .ghead { display: flex; align-items: center; justify-content: space-between; padding: 12px 8px 4px; }
  .gname { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-faint); font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .add { padding: 0 6px; opacity: 0; }
  .ghead:hover .add, .add:focus-visible { opacity: 1; }
  .row { display: flex; align-items: center; gap: 7px; width: 100%; text-align: left; border: 0; background: transparent; border-radius: 8px; padding: 6px 8px; }
  .row:hover { background: var(--bg-hover); }
  .row.active { background: var(--bg-active); }
  .state { width: 11px; display: grid; place-items: center; flex: none; }
  .title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: block; }
  .dot.err { background: var(--err); }
  .dot.unread { background: var(--accent); }
  .chg { font-size: 11px; color: var(--text-dim); background: var(--bg-active); border-radius: 9px; padding: 0 6px; }
  .ag { font-size: 10.5px; font-weight: 600; color: var(--text-faint); }
  .ag.claude { color: var(--claude); }
  .ag.codex { color: var(--codex); }
  .empty, .hint { color: var(--text-faint); font-size: 12.5px; padding: 4px 10px; }
  .offline { background: var(--err-soft); color: var(--err); font-size: 12px; padding: 8px 14px; }
</style>
