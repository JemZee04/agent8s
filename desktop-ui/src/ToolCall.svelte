<script lang="ts">
  import type { Part } from './lib/types';

  type Tool = Extract<Part, { type: 'tool' }>;
  let { part }: { part: Tool } = $props();
  let open = $state(false);

  const KEYS = ['command', 'file_path', 'path', 'pattern', 'description', 'url', 'query'];

  const summary = $derived.by(() => {
    const input = part.input as Record<string, unknown> | string | null;
    if (!input) return '';
    if (typeof input === 'string') return input;
    for (const key of KEYS) if (input[key]) return String(input[key]);
    if (Array.isArray(input.changes)) return input.changes.join(', ');
    if (Array.isArray(input.items)) return `${input.items.length} пунктов`;
    return '';
  });

  const detail = $derived.by(() => {
    const input = part.input as Record<string, unknown> | string | null;
    if (input === null || input === undefined) return '';
    if (typeof input === 'string') return input;
    if (typeof input.command === 'string' && Object.keys(input).length === 1) return input.command;
    return JSON.stringify(input, null, 2);
  });
</script>

<div class="tool" class:err={part.is_error}>
  <button class="head" onclick={() => (open = !open)} aria-expanded={open}>
    <span class="chev" class:open>›</span>
    <span class="name">{part.name}</span>
    <span class="sum">{summary}</span>
    <span class="end">
      {#if part.status === 'running'}<i class="spinner"></i>
      {:else if part.is_error}<span class="bad">✕</span>
      {:else}<span class="ok">✓</span>{/if}
    </span>
  </button>
  {#if open}
    <div class="body selectable">
      {#if detail}<pre class="in">{detail}</pre>{/if}
      {#if part.output}<pre class="out">{part.output}</pre>
      {:else if part.status === 'done'}<div class="none">нет вывода</div>{/if}
    </div>
  {/if}
</div>

<style>
  .tool { margin: 3px 0; border: 1px solid var(--border); border-radius: 8px; background: var(--bg); overflow: hidden; }
  .tool.err { border-color: color-mix(in srgb, var(--err) 45%, var(--border)); }
  .head { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left; background: transparent; border: 0; padding: 4px 10px; min-width: 0; }
  .head:hover { background: var(--bg-hover); }
  .chev { color: var(--text-faint); transition: transform 0.12s; flex: none; width: 8px; }
  .chev.open { transform: rotate(90deg); }
  .name { font: 600 11.5px var(--mono); color: var(--text-dim); background: var(--bg-active); border-radius: 5px; padding: 0 6px; flex: none; }
  .sum { font: 12px var(--mono); color: var(--text-dim); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .end { flex: none; display: grid; place-items: center; width: 14px; }
  .ok { color: var(--ok); font-size: 12px; }
  .bad { color: var(--err); font-size: 12px; }
  .body { border-top: 1px solid var(--border); padding: 6px 10px 8px; }
  pre { margin: 0; font: 12px/1.45 var(--mono); white-space: pre-wrap; word-break: break-word; max-height: 280px; overflow: auto; }
  .in { color: var(--text-dim); padding-bottom: 6px; }
  .out { background: var(--bg-code); border-radius: 6px; padding: 6px 8px; }
  .none { color: var(--text-faint); font-size: 12px; }
</style>
