<script lang="ts">
  import { onMount } from 'svelte';
  import { app, loadPorts, notify, openPreview, previews, selectedChat, sharePort, stopPreview, type PortInfo } from './lib/state.svelte';

  const chat = $derived(selectedChat());
  let manual = $state('');
  let busy = $state(0);

  onMount(() => {
    if (chat) void loadPorts(chat.id);
  });

  const mine = $derived(previews.list.filter((p) => p.chat_id === chat?.id));
  const shared = $derived(new Set(mine.map((p) => p.port)));
  // Likely candidates first; system helpers and other apps stay out of the way.
  const likely = $derived(previews.ports.filter((p) => p.kind !== 'other'));
  const others = $derived(previews.ports.filter((p) => p.kind === 'other'));

  async function share(port: number) {
    if (!chat) return;
    busy = port;
    const info = await sharePort(chat.id, port);
    busy = 0;
    if (info && app.relayMode) openPreview(info.url); // on the phone: straight to the page
    else if (info) notify('Ссылка отправлена на телефон — там появится уведомление «Открыть».', 'info');
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      notify('Ссылка скопирована', 'info');
    } catch {
      notify('Не удалось скопировать');
    }
  }

  const left = (exp: number) => {
    const m = Math.max(0, Math.round((exp * 1000 - app.now) / 60000));
    return m >= 60 ? `ещё ${Math.floor(m / 60)} ч ${m % 60} мин` : `ещё ${m} мин`;
  };
</script>

<div class="scrim" role="presentation" onclick={() => (app.previewOpen = false)} onkeydown={(e) => e.key === 'Escape' && (app.previewOpen = false)}>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Превью сайта" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
    <h2>Превью сайта</h2>
    <p class="dim">
      Откройте в браузере телефона сайт, который агент запустил на компьютере (<code>localhost</code>). Ссылка живёт 8 часов.
      Страницы идут через ваше реле — оно их видит (в отличие от чатов, превью не шифруется сквозным шифрованием).
    </p>

    {#if mine.length}
      <h3>Открытые превью</h3>
      {#each mine as p (p.cap)}
        <div class="row">
          <span class="grow"><b>localhost:{p.port}</b> <span class="dim">· {left(p.exp)}</span></span>
          <button class="primary" onclick={() => openPreview(p.url)}>Открыть</button>
          <button class="btn" onclick={() => copy(p.url)}>Ссылка</button>
          <button class="btn danger" onclick={() => stopPreview(p.cap)}>Остановить</button>
        </div>
      {/each}
    {/if}

    <h3>Найденные сайты на компьютере</h3>
    {#if previews.loading && !previews.ports.length}
      <p class="dim">Ищу…</p>
    {:else if !likely.length}
      <p class="dim">Похожих на сайт процессов нет. Попросите агента: «запусти сайт на localhost».</p>
    {/if}
    {#each likely as p (p.port)}
      {@render portRow(p)}
    {/each}
    {#if others.length}
      <details>
        <summary class="dim">Остальные порты ({others.length})</summary>
        {#each others as p (p.port)}
          {@render portRow(p)}
        {/each}
      </details>
    {/if}

    <h3>Другой порт</h3>
    <div class="row">
      <input type="text" inputmode="numeric" bind:value={manual} placeholder="например 3000" class="grow" onkeydown={(e) => e.key === 'Enter' && manual && share(Number(manual))} />
      <button class="btn" disabled={!/^\d+$/.test(manual)} onclick={() => share(Number(manual))}>Открыть</button>
    </div>

    <div class="actions">
      <button class="btn" onclick={() => chat && loadPorts(chat.id)}>Обновить список</button>
      <button class="primary" onclick={() => (app.previewOpen = false)}>Закрыть</button>
    </div>
  </div>
</div>

{#snippet portRow(p: PortInfo)}
  <div class="row">
    <span class="grow">
      <b>localhost:{p.port}</b> <span class="dim">{p.command}</span>
      {#if p.kind === 'mine'}<span class="badge">из этого чата</span>{:else if p.kind === 'web'}<span class="badge">отвечает веб-страницей</span>{/if}
    </span>
    <button class="btn" disabled={busy === p.port || shared.has(p.port)} onclick={() => share(p.port)}>
      {shared.has(p.port) ? 'Открыто' : app.relayMode ? 'Открыть здесь' : 'Открыть на телефоне'}
    </button>
  </div>
{/snippet}

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; place-items: center; z-index: 50; }
  .dialog { width: min(560px, 94vw); max-height: 92vh; overflow-y: auto; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 8px; font-size: 17px; }
  h3 { margin: 16px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-faint); }
  .dim { color: var(--text-dim); font-size: 12.5px; line-height: 1.45; }
  p { margin: 0 0 6px; }
  code { font: 12px var(--mono); background: var(--bg-code); border-radius: 5px; padding: 0 5px; }
  .row { display: flex; align-items: center; gap: 8px; padding: 6px 0; flex-wrap: wrap; }
  .grow { flex: 1; min-width: 0; }
  .badge { font-size: 11px; background: var(--accent-soft); color: var(--accent); border-radius: 8px; padding: 0 7px; margin-left: 6px; }
  details { margin-top: 6px; }
  summary { cursor: default; padding: 4px 0; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  @media (max-width: 760px) {
    .scrim { place-items: end center; }
    .dialog { width: 100%; max-height: 92dvh; border-radius: 18px 18px 0 0; padding-bottom: max(20px, env(safe-area-inset-bottom)); }
  }
</style>
