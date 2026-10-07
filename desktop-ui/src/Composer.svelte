<script lang="ts">
  import { agentLabel, agentLogin, app, notify, patchChat, refreshAuth, send, stop } from './lib/state.svelte';
  import AgentPicker from './AgentPicker.svelte';
  import type { Chat } from './lib/types';

  let { chat, hasHistory }: { chat: Chat; hasHistory: boolean } = $props();
  let el: HTMLTextAreaElement | undefined = $state();

  const draft = $derived(app.drafts[chat.id] ?? '');
  const signedOut = $derived(app.auth[chat.agent]?.ok === false ? app.auth[chat.agent] : null);

  async function copyLogin(command: string) {
    try {
      await navigator.clipboard.writeText(command);
      notify('Команда скопирована', 'info');
    } catch {
      notify('Не удалось скопировать');
    }
  }

  // Only meaningful between turns: during the very first turn the message just
  // sent counts as "history" although there is nothing to hand over yet.
  const handover = $derived(!chat.running && hasHistory && !chat.sessions.includes(chat.agent));

  function autosize() {
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, window.innerHeight * 0.4) + 'px';
  }

  $effect(() => {
    chat.id;
    el?.focus();
    autosize();
  });

  async function submit() {
    const text = draft.trim();
    if (!text || chat.running) return;
    app.drafts[chat.id] = '';
    queueMicrotask(autosize);
    if (!(await send(text))) app.drafts[chat.id] = text;
  }

  // On a touch screen Enter is a line break (there is a Send button); on a keyboard it sends.
  const touch = window.matchMedia('(pointer: coarse)').matches;

  function onKey(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !touch) {
      e.preventDefault();
      void submit();
    }
  }
</script>

<div class="composer">
  {#if signedOut}
    <div class="auth" role="alert">
      <b>{signedOut.label} не авторизован</b> для фонового сервиса — его вход хранится отдельно от приложения Claude и мог истечь.
      {#if app.relayMode}
        Войдите на компьютере: <code>{signedOut.login}</code>.
      {:else}
        Нажмите «Войти» (откроется Терминал и браузер) или выполните <code>{signedOut.login}</code>.
      {/if}
      <span class="auth-actions">
        {#if !app.relayMode}<button class="primary" onclick={() => agentLogin(chat.agent)}>Войти…</button>{/if}
        {#if !app.relayMode}<button class="btn" onclick={() => copyLogin(signedOut.login)}>Скопировать команду</button>{/if}
        <button class="btn" onclick={() => refreshAuth(true)}>Проверить снова</button>
      </span>
    </div>
  {/if}
  {#if handover}
    <div class="handover">{agentLabel(chat.agent)} ещё не работал в этом чате — получит предыдущую историю и состояние файлов текстом.</div>
  {/if}
  <div class="box">
    <textarea
      bind:this={el}
      rows="1"
      placeholder={chat.running ? 'Агент работает… можно готовить следующее сообщение' : 'Напишите задачу или вопрос…'}
      value={draft}
      oninput={(e) => { app.drafts[chat.id] = e.currentTarget.value; autosize(); }}
      onkeydown={onKey}
    ></textarea>
    {#if chat.running}
      <button class="stop" onclick={stop} title="Остановить (⌘.)">■ Стоп</button>
    {:else}
      <button class="primary send" disabled={!draft.trim()} onclick={submit} title="Отправить (Enter)">Отправить</button>
    {/if}
  </div>
  <div class="foot">
    <AgentPicker agent={chat.agent} model={chat.model} effort={chat.effort} disabled={chat.running}
      onchange={(patch) => patchChat(chat.id, patch)} />
    {#if !touch}<span class="keys">Enter — отправить · Shift+Enter — новая строка</span>{/if}
  </div>
</div>

<style>
  .composer { padding: 6px 20px max(12px, env(safe-area-inset-bottom)); min-width: 0; }
  .auth { background: var(--err-soft); color: var(--text); border: 1px solid color-mix(in srgb, var(--err) 40%, var(--border)); border-radius: 10px; padding: 8px 12px; margin-bottom: 8px; font-size: 12.5px; line-height: 1.5; }
  .auth code { font: 12px var(--mono); background: var(--bg-code); border-radius: 5px; padding: 0 5px; user-select: text; }
  .auth-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .handover { font-size: 12px; color: var(--text-dim); background: var(--accent-soft); border-radius: 8px; padding: 6px 11px; margin-bottom: 7px; }
  .box { min-width: 0; display: flex; align-items: flex-end; gap: 8px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 12px; padding: 7px 8px 7px 12px; }
  .box:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
  textarea { flex: 1; min-width: 0; width: 100%; border: 0; outline: 0; resize: none; background: transparent; line-height: 1.5; padding: 4px 0; max-height: 40vh; user-select: text; }
  .send, .stop { flex: none; height: 30px; }
  .stop { background: var(--err); color: #fff; border: 0; border-radius: 7px; padding: 0 14px; font-weight: 500; }
  .foot { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; color: var(--text-faint); font-size: 11.5px; padding: 7px 2px 0; }
  .keys { margin-left: auto; }
  @media (max-width: 760px) {
    .composer { padding-left: 10px; padding-right: 10px; }
    textarea { font-size: 16px; }
    .send, .stop { height: 36px; padding: 0 16px; }
  }
</style>
