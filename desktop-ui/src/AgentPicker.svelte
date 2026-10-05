<script lang="ts">
  import { app, agentSpec } from './lib/state.svelte';

  interface Props {
    agent: string;
    model: string;
    effort: string;
    disabled?: boolean;
    onchange: (patch: { agent?: string; model?: string; effort?: string }) => void;
  }
  let { agent, model, effort, disabled = false, onchange }: Props = $props();

  const spec = $derived(agentSpec(agent));
  const efforts = $derived(spec?.models.find((m) => m.id === model)?.efforts ?? []);
</script>

<div class="picker" class:disabled>
  <div class="seg" role="group" aria-label="Агент">
    {#each app.agents as a (a.id)}
      <button class:on={a.id === agent} class={a.id} {disabled} onclick={() => a.id !== agent && onchange({ agent: a.id })}>
        {app.mobile ? a.label.split(' ')[0] : a.label}
      </button>
    {/each}
  </div>
  <select {disabled} value={model} onchange={(e) => onchange({ model: e.currentTarget.value })} aria-label="Модель">
    {#each spec?.models ?? [] as m (m.id)}
      <option value={m.id}>{m.label}</option>
    {/each}
  </select>
  {#if efforts.length}
    <select {disabled} value={effort} onchange={(e) => onchange({ effort: e.currentTarget.value })} aria-label="Уровень рассуждений">
      <option value="">усилие: авто</option>
      {#each efforts as level (level)}
        <option value={level}>{level}</option>
      {/each}
    </select>
  {/if}
</div>

<style>
  .picker { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; min-width: 0; }
  .picker.disabled { opacity: 0.55; }
  .seg { display: inline-flex; background: var(--bg-active); border-radius: 8px; padding: 2px; }
  .seg button { border: 0; background: transparent; border-radius: 6px; padding: 3px 10px; color: var(--text-dim); font-weight: 500; }
  .seg button.on { background: var(--bg-elev); color: var(--text); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18); }
  .seg button.on.claude { color: var(--claude); }
  .seg button.on.codex { color: var(--codex); }
  .seg button:disabled { cursor: not-allowed; }
  select { max-width: 150px; }
  @media (max-width: 760px) {
    .picker { flex-wrap: nowrap; width: 100%; }
    .seg { flex: none; }
    .seg button { padding: 5px 9px; white-space: nowrap; }
    select { flex: 1 1 0; min-width: 0; max-width: none; min-height: 34px; padding-left: 6px; text-overflow: ellipsis; }
  }
</style>
