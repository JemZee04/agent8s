<script lang="ts">
  import { app } from './lib/state.svelte';
</script>

{#if app.toast}
  <div class="toast" class:error={app.toast.kind === 'error'} role="status">
    <span class="selectable">{app.toast.text}</span>
    <button class="ghost" onclick={() => (app.toast = null)} aria-label="Закрыть">✕</button>
  </div>
{/if}

<style>
  .toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%); z-index: 70; display: flex; gap: 10px; align-items: center; max-width: min(640px, 90vw); background: var(--bg-elev); border: 1px solid var(--border); border-radius: 10px; padding: 8px 8px 8px 14px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.25); }
  .toast.error { border-color: var(--err); color: var(--err); }
  span { word-break: break-word; }
</style>
