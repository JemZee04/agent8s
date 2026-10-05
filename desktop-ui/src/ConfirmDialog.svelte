<script lang="ts">
  import { app } from './lib/state.svelte';

  const c = $derived(app.confirm);
</script>

{#if c}
  <div class="scrim" role="presentation" onclick={() => c.resolve(false)}>
    <div
      class="dialog"
      role="alertdialog"
      aria-modal="true"
      aria-label={c.title}
      tabindex="-1"
      onclick={(e) => e.stopPropagation()}
      onkeydown={(e) => e.key === 'Escape' && c.resolve(false)}
    >
      <h3>{c.title}</h3>
      <p>{c.body}</p>
      <div class="actions">
        <button class="btn" onclick={() => c.resolve(false)}>Отмена</button>
        <!-- svelte-ignore a11y_autofocus -->
        <button class="primary" class:danger-bg={c.danger} autofocus onclick={() => c.resolve(true)}>{c.okLabel}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; grid-template-columns: minmax(0, 1fr); place-items: center; z-index: 60; }
  .dialog { min-width: 0; width: min(440px, 90vw); background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 18px 20px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h3 { margin: 0 0 8px; font-size: 15px; }
  p { margin: 0 0 16px; color: var(--text-dim); line-height: 1.45; word-break: break-word; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  .danger-bg { background: var(--err); }
</style>
