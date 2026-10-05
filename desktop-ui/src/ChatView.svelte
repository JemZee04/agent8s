<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { native } from './lib/api';
  import {
    addWritableDir, app, deleteChat, notify, openFolder, patchChat, selectedChat, toggleDiff,
  } from './lib/state.svelte';
  import Composer from './Composer.svelte';
  import MessageView from './MessageView.svelte';

  const chat = $derived(selectedChat());
  const messages = $derived(chat ? app.messages[chat.id] : undefined);
  const hasHistory = $derived(!!messages?.some((m) => m.role !== 'system'));

  let scroller: HTMLElement | undefined = $state();
  let content: HTMLElement | undefined = $state();
  let stick = true;
  let editing = $state(false);
  let titleDraft = $state('');
  let menu = $state(false);

  function onScroll() {
    if (scroller) stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90;
  }

  onMount(() => {
    // Follow streaming growth without tracking every reactive value.
    const observer = new ResizeObserver(() => {
      if (stick && scroller) scroller.scrollTop = scroller.scrollHeight;
    });
    if (content) observer.observe(content);
    return () => observer.disconnect();
  });

  $effect(() => {
    chat?.id;
    stick = true;
    void tick().then(() => scroller && (scroller.scrollTop = scroller.scrollHeight));
  });

  function startEdit() {
    if (!chat) return;
    titleDraft = chat.title;
    editing = true;
  }
  async function saveTitle() {
    editing = false;
    if (chat && titleDraft.trim() && titleDraft.trim() !== chat.title) await patchChat(chat.id, { title: titleDraft.trim() });
  }

  const shortPath = (p: string) => (p.split('/').length > 3 ? '…/' + p.split('/').slice(-2).join('/') : p);

  async function copy(text: string, what: string) {
    try {
      await navigator.clipboard.writeText(text);
      notify(`${what} скопирован`, 'info');
    } catch {
      notify('Не удалось скопировать');
    }
  }

  async function addDir() {
    menu = false;
    const picked = (await native()?.pick_folder()) ?? window.prompt('Абсолютный путь к папке, куда агенту разрешена запись:');
    if (picked) await addWritableDir(picked);
  }
</script>

<svelte:window onclick={() => (menu = false)} />

<main>
  {#if chat}
    <header>
      <div class="row1">
        {#if app.mobile}
          <button class="ghost back" aria-label="К списку чатов" onclick={() => (app.mobileView = 'list')}>‹</button>
        {/if}
        {#if editing}
          <!-- svelte-ignore a11y_autofocus -->
          <input type="text" class="title-edit" bind:value={titleDraft} autofocus onblur={saveTitle}
            onkeydown={(e) => (e.key === 'Enter' ? saveTitle() : e.key === 'Escape' && (editing = false))} />
        {:else}
          <button class="title" ondblclick={startEdit} title="Двойной клик — переименовать">{chat.title}</button>
        {/if}
        <div class="right">
          <button class="btn diffbtn" class:on={app.diffOpen} onclick={toggleDiff} title="Изменения (⇧⌘D)">
            Изменения{#if chat.changes}<span class="badge">{chat.changes}</span>{/if}
          </button>
          <div class="menuwrap">
            <button class="ghost" aria-label="Ещё" onclick={(e) => { e.stopPropagation(); menu = !menu; }}>⋯</button>
            {#if menu}
              <div class="menu" role="menu" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={() => {}}>
                {#if !app.relayMode}
                  <button role="menuitem" onclick={() => { menu = false; void openFolder('finder'); }}>Показать в Finder</button>
                  <button role="menuitem" onclick={() => { menu = false; void openFolder('terminal'); }}>Открыть в Терминале</button>
                  <button role="menuitem" onclick={() => { menu = false; void openFolder('code'); }}>Открыть в VS Code</button>
                {/if}
                <button role="menuitem" onclick={addDir}>Разрешить запись в папку…</button>
                <hr />
                <button role="menuitem" class="danger" disabled={chat.running} onclick={() => { menu = false; void deleteChat(chat); }}>Удалить чат…</button>
              </div>
            {/if}
          </div>
        </div>
      </div>
      <div class="row2">
        <span class="chip">{chat.project_name}</span>
        <button class="chip click" onclick={() => copy(chat.branch, 'Название ветки')} title="Скопировать ветку">
          ⎇ {chat.branch}{chat.mode === 'direct' ? ' · прямо в проекте' : ''}
        </button>
        <button class="chip click path" onclick={() => copy(chat.worktree_path, 'Путь')} title="{chat.worktree_path} — нажмите, чтобы скопировать">
          {shortPath(chat.worktree_path)}
        </button>
        {#if !app.relayMode}<button class="chip click" onclick={() => openFolder('finder')}>Открыть папку</button>{/if}
        {#each chat.extra_dirs as d (d)}<span class="chip" title="Разрешена запись">＋ {shortPath(d)}</span>{/each}
      </div>
    </header>

    <div class="scroller" bind:this={scroller} onscroll={onScroll}>
      <div class="content" bind:this={content}>
        {#if !messages}
          <div class="placeholder">Загружаю…</div>
        {:else if !messages.length}
          <div class="placeholder">
            <p>Опишите задачу — агент будет работать в папке</p>
            <code>{chat.worktree_path}</code>
          </div>
        {/if}
        {#each messages ?? [] as m (m.id)}
          <MessageView message={m} />
        {/each}
      </div>
    </div>

    <Composer {chat} {hasHistory} />
  {:else}
    <div class="empty">
      <h2>agent8s</h2>
      <p>Чаты с Claude Code и Codex в изолированных ветках. Агента и модель можно менять прямо в чате — контекст не теряется.</p>
      <button class="primary" onclick={() => (app.newChatOpen = true)}>Создать первый чат</button>
    </div>
  {/if}
</main>

<style>
  main { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--bg); }
  header { border-bottom: 1px solid var(--border); padding: max(10px, env(safe-area-inset-top)) 16px 8px 20px; }
  .back { font-size: 26px; line-height: 1; padding: 0 10px 2px; margin-left: -12px; flex: none; }
  .row1 { display: flex; align-items: center; gap: 12px; justify-content: space-between; }
  .title, .title-edit { font-size: 15px; font-weight: 600; border: 0; background: transparent; padding: 2px 6px; margin-left: -6px; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: left; border-radius: 6px; }
  .title:hover { background: var(--bg-hover); }
  .title-edit { background: var(--bg-elev); border: 1px solid var(--accent); flex: 1; }
  .right { display: flex; align-items: center; gap: 8px; flex: none; }
  .diffbtn.on { background: var(--accent-soft); border-color: transparent; color: var(--accent); }
  .badge { margin-left: 6px; background: var(--accent); color: var(--accent-text); font-size: 11px; border-radius: 9px; padding: 0 6px; }
  .menuwrap { position: relative; }
  .menu { position: absolute; right: 0; top: 30px; z-index: 20; min-width: 230px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 10px; padding: 5px; box-shadow: 0 10px 34px rgba(0, 0, 0, 0.25); }
  .menu button { display: block; width: 100%; text-align: left; background: transparent; border: 0; border-radius: 6px; padding: 6px 10px; }
  .menu button:hover:not(:disabled) { background: var(--bg-hover); }
  .menu button:disabled { opacity: 0.4; }
  .menu hr { border: 0; border-top: 1px solid var(--border); margin: 4px 2px; }
  .row2 { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 7px; }
  .chip { font-size: 12px; color: var(--text-dim); background: var(--bg-side); border: 0; border-radius: 6px; padding: 1px 8px; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chip.click:hover { background: var(--bg-active); color: var(--text); }
  .path { font-family: var(--mono); font-size: 11.5px; }
  .scroller { flex: 1; overflow-y: auto; min-height: 0; }
  .content { max-width: 820px; margin: 0 auto; padding: 8px 24px 20px; }
  .placeholder { color: var(--text-dim); text-align: center; padding: 70px 0; }
  .placeholder code { font: 12px var(--mono); background: var(--bg-code); border-radius: 6px; padding: 2px 8px; user-select: text; }
  .empty { margin: auto; text-align: center; max-width: 420px; padding: 0 20px; }
  .empty h2 { font-size: 24px; margin: 0 0 8px; }
  .empty p { color: var(--text-dim); line-height: 1.5; margin: 0 0 18px; }
  @media (max-width: 760px) {
    header { padding-left: 14px; padding-right: 10px; }
    .row2 { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; }
    .row2::-webkit-scrollbar { display: none; }
    .chip { flex: none; }
    .content { padding: 4px 14px 16px; }
    .title { font-size: 16px; }
  }
</style>
