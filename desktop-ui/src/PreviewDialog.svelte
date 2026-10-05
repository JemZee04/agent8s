<script lang="ts">
  import { onMount } from 'svelte';
  import {
    app, loadHtmlFiles, loadPorts, notify, openPreview, previews, refreshPreviews, selectedChat, setDesktopView, shareFile, sharePort, stopPreview,
    type HtmlFile, type PortInfo,
  } from './lib/state.svelte';

  const chat = $derived(selectedChat());
  let manual = $state('');
  let busy = $state(0);

  let manualFile = $state('');
  let busyFile = $state('');

  onMount(() => {
    if (chat) {
      void loadPorts(chat.id);
      void loadHtmlFiles(chat.id);
    }
    // What a page asked for and did not get shows up here while you look at it on the phone.
    const timer = window.setInterval(() => void refreshPreviews(), 4000);
    return () => window.clearInterval(timer);
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

  async function open(file: string) {
    if (!chat) return;
    busyFile = file;
    const info = await shareFile(chat.id, file);
    busyFile = '';
    if (info && app.relayMode) openPreview(info.url);
    else if (info) notify('Файл отправлен на телефон — там появится уведомление «Открыть».', 'info');
  }

  const sharedFiles = $derived(new Set(mine.filter((p) => p.kind === 'file').map((p) => p.name)));
  const ago = (t: number) => {
    const m = Math.round((app.now / 1000 - t) / 60);
    return m < 1 ? 'только что' : m < 60 ? `${m} мин назад` : m < 1440 ? `${Math.round(m / 60)} ч назад` : `${Math.round(m / 1440)} дн назад`;
  };
  const kb = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} МБ` : `${Math.max(1, Math.round(n / 1e3))} КБ`);

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
    <h2>Превью сайта и HTML-файлов</h2>
    <p class="dim">
      Откройте в браузере телефона сайт, который агент запустил на компьютере (<code>localhost</code>). Ссылка живёт 8 часов.
      Страницы идут через ваше реле — оно их видит (в отличие от чатов, превью не шифруется сквозным шифрованием).
    </p>

    {#if mine.length}
      <h3>Открытые превью</h3>
      {#each mine as p (p.cap)}
        <div class="row">
          <span class="grow"><b>{p.kind === 'file' ? '📄 ' + p.name : p.name}</b> <span class="dim">· {left(p.exp)}</span></span>
          <button class="primary" onclick={() => openPreview(p.url)}>Открыть</button>
          <button class="btn" onclick={() => copy(p.url)}>Ссылка</button>
          <button class="btn danger" onclick={() => stopPreview(p.cap)}>Остановить</button>
        </div>
        <label class="dim opt" title="Для сайтов без мобильной вёрстки: страница строится в ширину компьютера, её можно приближать">
          <input type="checkbox" checked={!!p.desktop} onchange={(e) => setDesktopView(p.cap, e.currentTarget.checked)} />
          Показывать как на компьютере (если у сайта нет мобильной версии)
        </label>
        {#if p.kind === 'file' && p.root}
          <div class="dim note">Раздаётся папка: <code>{p.root}</code></div>
        {/if}
        {#if p.missing?.length}
          <div class="warn">
            Страница запросила, но не получила: {p.missing.join(', ')}.
            Скорее всего, это файлы вне раздаваемой папки или не веб-типа (ключи, базы — не отдаются намеренно).
          </div>
        {/if}
      {/each}
    {/if}

    <h3>HTML-файлы для чтения</h3>
    {#if !previews.files.length}
      <p class="dim">В папке этого чата HTML-файлов нет. Можно указать путь к файлу ниже.</p>
    {/if}
    {#each previews.files.slice(0, 6) as f (f.path)}
      {@render fileRow(f)}
    {/each}
    {#if previews.files.length > 6}
      <details>
        <summary class="dim">Ещё файлов: {previews.files.length - 6}</summary>
        {#each previews.files.slice(6) as f (f.path)}
          {@render fileRow(f)}
        {/each}
      </details>
    {/if}
    <div class="row">
      <input type="text" bind:value={manualFile} placeholder="/путь/к/файлу.html" class="grow" onkeydown={(e) => e.key === 'Enter' && manualFile.trim() && open(manualFile.trim())} />
      <button class="btn" disabled={!manualFile.trim()} onclick={() => open(manualFile.trim())}>Открыть</button>
    </div>
    <p class="dim">Вместе с файлом доступны картинки, стили и скрипты из его папки — но не другие файлы (ключи, базы, .env).</p>

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
      <button class="btn" onclick={() => chat && (loadPorts(chat.id), loadHtmlFiles(chat.id))}>Обновить список</button>
      <button class="primary" onclick={() => (app.previewOpen = false)}>Закрыть</button>
    </div>
  </div>
</div>

{#snippet fileRow(f: HtmlFile)}
  <div class="row">
    <span class="grow"><b class="fname" title={f.path}>📄 {f.rel}</b> <span class="dim">{ago(f.mtime)} · {kb(f.size)}</span></span>
    <button class="btn" disabled={busyFile === f.path || sharedFiles.has(f.rel.split('/').pop() ?? '')} onclick={() => open(f.path)}>
      {sharedFiles.has(f.rel.split('/').pop() ?? '') ? 'Открыто' : app.relayMode ? 'Открыть здесь' : 'Открыть на телефоне'}
    </button>
  </div>
{/snippet}

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
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; grid-template-columns: minmax(0, 1fr); place-items: center; z-index: 50; }
  .dialog { min-width: 0; width: min(560px, 94vw); max-height: 92vh; overflow-y: auto; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 8px; font-size: 17px; }
  h3 { margin: 16px 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-faint); }
  .dim { color: var(--text-dim); font-size: 12.5px; line-height: 1.45; }
  p { margin: 0 0 6px; }
  code { font: 12px var(--mono); background: var(--bg-code); border-radius: 5px; padding: 0 5px; }
  .row { display: flex; align-items: center; gap: 8px; padding: 6px 0; flex-wrap: wrap; }
  .grow { flex: 1; min-width: 0; }
  .opt { display: flex; gap: 8px; align-items: flex-start; margin: 0 0 6px; }
  .opt input { margin-top: 2px; }
  .note { margin: -2px 0 4px; word-break: break-all; }
  .warn { background: var(--err-soft); color: var(--err); border-radius: 8px; padding: 6px 10px; font-size: 12.5px; margin-bottom: 6px; word-break: break-all; }
  .fname { word-break: break-all; }
  .badge { font-size: 11px; background: var(--accent-soft); color: var(--accent); border-radius: 8px; padding: 0 7px; margin-left: 6px; }
  details { margin-top: 6px; }
  summary { cursor: default; padding: 4px 0; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  @media (max-width: 760px) {
    .scrim { place-items: end center; }
    .dialog { width: 100%; max-height: 92dvh; border-radius: 18px 18px 0 0; padding-bottom: max(20px, env(safe-area-inset-bottom)); }
  }
</style>
