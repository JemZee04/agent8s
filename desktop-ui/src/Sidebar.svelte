<script lang="ts">
  import { app, ask, freshSessions, groups, select, unpair } from './lib/state.svelte';

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

  {#if app.link === 'offline'}
    <div class="offline">Компьютер не в сети — как только он включится, чаты появятся.</div>
  {:else if app.link === 'connecting'}
    <div class="offline soft">Подключаюсь…</div>
  {/if}

  <div class="bottom">
    <button class="ghost" onclick={() => (app.importOpen = true)} title="Перенести чаты из Claude Code">
      ⇩ Импорт{#if freshSessions()} <span class="count">{freshSessions()}</span>{/if}
    </button>
    {#if app.relayMode}
      <button class="ghost" onclick={async () => (await ask('Забыть этот компьютер?', 'Ключ будет удалён с телефона. Чтобы подключиться снова, нужно будет заново отсканировать QR-код.', 'Забыть', true)) && unpair()}>Отключить телефон</button>
    {:else}
      <button class="ghost" onclick={() => (app.phoneDialogOpen = true)} title="Подключить телефон">📱 Телефон</button>
    {/if}
  </div>
</aside>

<style>
  aside { background: var(--bg-side); border-right: 1px solid var(--border); display: flex; flex-direction: column; min-height: 0; }
  .top { display: flex; align-items: center; justify-content: space-between; padding: max(14px, env(safe-area-inset-top)) 12px 10px 16px; }
  .bottom { display: flex; flex-wrap: wrap; gap: 4px; border-top: 1px solid var(--border); padding: 6px 10px max(8px, env(safe-area-inset-bottom)); }
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
  .count { background: var(--accent); color: var(--accent-text); font-size: 11px; border-radius: 9px; padding: 0 6px; margin-left: 4px; }
  .chg { font-size: 11px; color: var(--text-dim); background: var(--bg-active); border-radius: 9px; padding: 0 6px; }
  .ag { font-size: 10.5px; font-weight: 600; color: var(--text-faint); }
  .ag.claude { color: var(--claude); }
  .ag.codex { color: var(--codex); }
  .empty, .hint { color: var(--text-faint); font-size: 12.5px; padding: 4px 10px; }
  .offline { background: var(--err-soft); color: var(--err); font-size: 12px; padding: 8px 14px; }
  .offline.soft { background: var(--bg-active); color: var(--text-dim); }
  @media (hover: none) { .add { opacity: 1; } .row { padding: 10px 10px; } }
  @media (max-width: 760px) { aside { border-right: 0; } .row { font-size: 15px; } .chg, .ag { font-size: 12px; } }
</style>
