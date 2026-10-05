<script lang="ts">
  import { app, notify } from './lib/state.svelte';

  async function copy() {
    try {
      await navigator.clipboard.writeText(app.freshPairLink);
      notify('Ключ скопирован. Теперь «Поделиться» → «На экран Домой».', 'info');
    } catch {
      notify('Не удалось скопировать.');
    }
  }
</script>

<div class="scrim" role="presentation">
  <div class="sheet" role="dialog" aria-modal="true" aria-label="Телефон подключён">
    <h2>Телефон подключён</h2>
    <p>Можно пользоваться прямо в Safari. Чтобы поставить как приложение:</p>
    <ol>
      <li>Нажмите «Скопировать ключ».</li>
      <li>«Поделиться» → «На экран „Домой“».</li>
      <li>Откройте приложение с экрана «Домой» и нажмите «Вставить ключ».</li>
    </ol>
    <p class="dim">Ключ скопируется в буфер обмена — после вставки скопируйте любой другой текст.</p>
    <div class="actions">
      <button class="btn" onclick={() => (app.freshPairLink = '')}>Пропустить</button>
      <button class="primary" onclick={copy}>Скопировать ключ</button>
    </div>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.4); display: flex; align-items: flex-end; justify-content: center; z-index: 80; }
  .sheet { width: min(520px, 100%); background: var(--bg-elev); border-radius: 18px 18px 0 0; padding: 20px 20px max(20px, env(safe-area-inset-bottom)); }
  h2 { margin: 0 0 8px; font-size: 18px; }
  p, ol { line-height: 1.5; }
  ol { padding-left: 20px; }
  .dim { color: var(--text-dim); font-size: 13px; }
  .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 12px; }
  .actions button { padding: 10px 16px; font-size: 16px; border-radius: 10px; }
</style>
