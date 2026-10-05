<script lang="ts">
  import { onMount } from 'svelte';
  import { openExternal } from './lib/api';
  import { app, groups, notify, select, start, stop, toggleDiff } from './lib/state.svelte';
  import ChatView from './ChatView.svelte';
  import ConfirmDialog from './ConfirmDialog.svelte';
  import DiffPanel from './DiffPanel.svelte';
  import ImportClaude from './ImportClaude.svelte';
  import NewChat from './NewChat.svelte';
  import PairedSheet from './PairedSheet.svelte';
  import PhonePairing from './PhonePairing.svelte';
  import PreviewDialog from './PreviewDialog.svelte';
  import Unpaired from './Unpaired.svelte';
  import Sidebar from './Sidebar.svelte';
  import Toast from './Toast.svelte';

  const WIDTH_KEY = 'agent8s-diff-width';
  let diffWidth = $state(clamp(Number(safeGet(WIDTH_KEY)) || 520));
  let dragging = $state(false);

  function safeGet(key: string): string | null {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  function clamp(w: number) {
    return Math.min(Math.max(w, 340), Math.max(380, window.innerWidth - 640));
  }

  onMount(() => {
    // One bundle, two shapes: a single-pane stack on phones, three panes on a desktop window.
    const mq = window.matchMedia('(max-width: 760px)');
    const sync = () => (app.mobile = mq.matches);
    sync();
    mq.addEventListener('change', sync);
    start();
    return () => mq.removeEventListener('change', sync);
  });

  function onKey(e: KeyboardEvent) {
    if (!(e.metaKey || e.ctrlKey)) return;
    const key = e.key.toLowerCase();
    if (key === 'n' && !e.shiftKey) {
      e.preventDefault();
      app.newChatProject = null;
      app.newChatOpen = true;
    } else if (/^[1-9]$/.test(key)) {
      const chat = groups().flatMap((g) => g.chats)[Number(key) - 1];
      if (chat) { e.preventDefault(); void select(chat.id); }
    } else if (key === 'd' && e.shiftKey) {
      e.preventDefault();
      toggleDiff();
    } else if (key === '.') {
      e.preventDefault();
      void stop();
    }
  }

  // Links must never navigate the app window; code blocks get a copy button.
  function onClick(e: MouseEvent) {
    const target = e.target as HTMLElement;
    const link = target.closest<HTMLAnchorElement>('a[href]');
    if (link) {
      e.preventDefault();
      if (/^(https?:|mailto:)/.test(link.href)) openExternal(link.href);
      return;
    }
    const copy = target.closest<HTMLElement>('[data-copy]');
    if (copy) {
      const code = copy.closest('.code')?.querySelector('code')?.textContent ?? '';
      navigator.clipboard.writeText(code).then(
        () => { copy.textContent = 'Скопировано'; setTimeout(() => (copy.textContent = 'Копировать'), 1500); },
        () => notify('Не удалось скопировать'),
      );
    }
  }

  function drag(e: PointerEvent) {
    dragging = true;
    const startX = e.clientX;
    const startW = diffWidth;
    const move = (ev: PointerEvent) => (diffWidth = clamp(startW + (startX - ev.clientX)));
    const up = () => {
      dragging = false;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      try { localStorage.setItem(WIDTH_KEY, String(diffWidth)); } catch { /* storage may be blocked */ }
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }
</script>

<svelte:window onkeydown={onKey} onclick={onClick} />

{#if app.needsPairing}
  <Unpaired />
{:else}
  <div
    class="shell"
    class:mobile={app.mobile}
    class:dragging
    style:grid-template-columns={app.mobile ? null : app.diffOpen && app.selectedId !== null ? `264px minmax(0, 1fr) ${diffWidth}px` : '264px minmax(0, 1fr)'}
  >
    {#if !app.mobile || app.mobileView === 'list'}<Sidebar />{/if}
    {#if !app.mobile || app.mobileView === 'chat'}<ChatView />{/if}
    {#if app.diffOpen && app.selectedId !== null}
      <div class="diffwrap" class:overlay={app.mobile}>
        {#if !app.mobile}<div class="grip" role="separator" aria-orientation="vertical" tabindex="-1" onpointerdown={drag}></div>{/if}
        <DiffPanel />
      </div>
    {/if}
  </div>
{/if}

{#if app.newChatOpen}<NewChat />{/if}
{#if app.phoneDialogOpen}<PhonePairing />{/if}
{#if app.importOpen}<ImportClaude />{/if}
{#if app.previewOpen}<PreviewDialog />{/if}
{#if app.freshPairLink}<PairedSheet />{/if}
<ConfirmDialog />
<Toast />

<style>
  .shell { display: grid; height: 100vh; height: 100dvh; }
  .shell.mobile { display: block; }
  .shell.mobile > :global(aside), .shell.mobile > :global(main) { height: 100%; }
  .diffwrap.overlay { position: fixed; inset: 0; z-index: 30; background: var(--bg); }
  .shell.dragging { cursor: col-resize; user-select: none; }
  .diffwrap { position: relative; display: flex; min-width: 0; min-height: 0; }
  .diffwrap > :global(aside) { flex: 1; min-width: 0; }
  .grip { position: absolute; left: -3px; top: 0; bottom: 0; width: 7px; cursor: col-resize; z-index: 5; }
  .grip:hover { background: var(--accent-soft); }
</style>
