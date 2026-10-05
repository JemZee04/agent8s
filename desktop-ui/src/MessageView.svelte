<script lang="ts">
  import { agentLabel, app, modelLabel } from './lib/state.svelte';
  import { renderMarkdown } from './lib/markdown';
  import type { Message } from './lib/types';
  import ToolCall from './ToolCall.svelte';

  let { message }: { message: Message } = $props();

  const userText = $derived(message.parts.map((p) => ('text' in p ? p.text : '')).join(''));

  function clock(seconds: number): string {
    const s = Math.max(0, seconds);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  const activity = $derived.by(() => {
    const last = message.parts[message.parts.length - 1];
    if (!last) return 'Запускается…';
    if (last.type === 'tool' && last.status === 'running') {
      const input = last.input as Record<string, unknown> | null;
      const detail = input && typeof input === 'object' ? String(input.command ?? input.file_path ?? input.pattern ?? input.path ?? '') : '';
      return `${last.name}${detail ? ': ' + detail : ''}`;
    }
    if (last.type === 'thinking') return 'Думает…';
    if (last.type === 'text') return 'Пишет ответ…';
    return 'Работает…';
  });
  const elapsed = $derived(clock(Math.floor((app.now - Date.parse(message.created_at)) / 1000)));

  function fmtUsage(p: Extract<Message['parts'][number], { type: 'usage' }>): string {
    const bits: string[] = [];
    if (p.input_tokens != null) bits.push(`${p.input_tokens.toLocaleString('ru')} вх.`);
    if (p.output_tokens != null) bits.push(`${p.output_tokens.toLocaleString('ru')} исх.`);
    if (p.cost_usd != null) bits.push(`$${p.cost_usd.toFixed(3)}`);
    if (p.duration_ms != null) bits.push(`${Math.round(p.duration_ms / 1000)} с`);
    return bits.join(' · ');
  }
</script>

{#if message.role === 'system'}
  <div class="system"><span>{userText}</span></div>
{:else if message.role === 'user'}
  <div class="user"><div class="bubble selectable">{userText}</div></div>
{:else}
  <div class="assistant">
    <div class="who">
      <span class="ag {message.agent}">{agentLabel(message.agent)}</span>
      <span class="model">{modelLabel(message.agent ?? '', message.model)}</span>
    </div>
    {#each message.parts as part, i (i)}
      {#if part.type === 'text'}
        <div class="md selectable">{@html renderMarkdown(part.text)}</div>
      {:else if part.type === 'thinking'}
        <details class="thinking">
          <summary>Размышления</summary>
          <div class="selectable">{part.text}</div>
        </details>
      {:else if part.type === 'tool'}
        <ToolCall {part} />
      {:else if part.type === 'error'}
        <div class="error selectable">{part.text}</div>
      {:else if part.type === 'usage'}
        <div class="usage">{fmtUsage(part)}</div>
      {/if}
    {/each}
    {#if message.status === 'running'}
      <div class="working"><i class="spinner"></i><span class="act">{activity}</span><span class="time">{elapsed}</span></div>
    {:else if message.status === 'interrupted'}
      <div class="note">Остановлено</div>
    {/if}
  </div>
{/if}

<style>
  .system { display: flex; align-items: center; gap: 12px; color: var(--text-faint); font-size: 12px; margin: 18px 0; }
  .system::before, .system::after { content: ''; flex: 1; height: 1px; background: var(--border); }
  .user { display: flex; justify-content: flex-end; margin: 18px 0 10px; }
  .bubble { max-width: min(80%, 640px); background: var(--accent-soft); border-radius: 14px 14px 4px 14px; padding: 8px 13px; white-space: pre-wrap; word-break: break-word; }
  .assistant { margin: 10px 0 18px; }
  .who { display: flex; align-items: baseline; gap: 8px; margin-bottom: 4px; font-size: 12px; }
  .ag { font-weight: 650; }
  .ag.claude { color: var(--claude); }
  .ag.codex { color: var(--codex); }
  .model { color: var(--text-faint); }
  .thinking { margin: 4px 0; color: var(--text-dim); font-size: 12.5px; }
  .thinking summary { cursor: default; color: var(--text-faint); }
  .thinking div { white-space: pre-wrap; border-left: 2px solid var(--border); padding-left: 10px; margin-top: 4px; max-height: 240px; overflow: auto; }
  .error { background: var(--err-soft); color: var(--err); border-radius: 8px; padding: 8px 12px; white-space: pre-wrap; word-break: break-word; margin: 6px 0; }
  .usage { color: var(--text-faint); font-size: 11.5px; margin-top: 6px; }
  .working { display: flex; align-items: center; gap: 9px; margin: 8px 0 2px; color: var(--text-dim); font-size: 12.5px; min-width: 0; }
  .act { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; font-family: var(--mono); font-size: 12px; }
  .time { color: var(--text-faint); font-variant-numeric: tabular-nums; flex: none; }
  .note { color: var(--text-faint); font-size: 12px; margin-top: 4px; }

  .md { line-height: 1.58; word-break: break-word; }
  .md :global(p) { margin: 0 0 9px; }
  .md :global(p:last-child) { margin-bottom: 0; }
  .md :global(h2), .md :global(h3), .md :global(h4), .md :global(h5) { margin: 14px 0 6px; line-height: 1.3; }
  .md :global(h2) { font-size: 1.2em; } .md :global(h3) { font-size: 1.08em; } .md :global(h4), .md :global(h5) { font-size: 1em; }
  .md :global(ul), .md :global(ol) { margin: 4px 0 9px; padding-left: 22px; }
  .md :global(li) { margin: 2px 0; }
  .md :global(a) { color: var(--accent); text-decoration: none; }
  .md :global(a:hover) { text-decoration: underline; }
  .md :global(code) { font: 0.9em var(--mono); background: var(--bg-code); border-radius: 5px; padding: 1px 5px; }
  .md :global(.code) { margin: 8px 0; border: 1px solid var(--border); border-radius: 9px; overflow: hidden; background: var(--bg-code); }
  .md :global(.code-head) { display: flex; justify-content: space-between; align-items: center; padding: 3px 10px; font-size: 11.5px; color: var(--text-faint); border-bottom: 1px solid var(--border); }
  .md :global(.code-head .copy) { background: transparent; border: 0; color: var(--text-dim); border-radius: 5px; padding: 1px 7px; }
  .md :global(.code-head .copy:hover) { background: var(--bg-hover); }
  .md :global(pre) { margin: 0; padding: 9px 12px; overflow-x: auto; }
  .md :global(pre code) { font: 12.5px/1.5 var(--mono); background: none; padding: 0; }
  .md :global(blockquote) { margin: 6px 0; padding: 0 12px; border-left: 3px solid var(--border); color: var(--text-dim); }
  .md :global(hr) { border: 0; border-top: 1px solid var(--border); margin: 12px 0; }
  .md :global(.table-wrap) { overflow-x: auto; margin: 8px 0; }
  .md :global(table) { border-collapse: collapse; font-size: 0.95em; }
  .md :global(th), .md :global(td) { border: 1px solid var(--border); padding: 4px 10px; text-align: left; }
  .md :global(th) { background: var(--bg-code); }
</style>
