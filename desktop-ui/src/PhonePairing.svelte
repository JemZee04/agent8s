<script lang="ts">
  import { onMount } from 'svelte';
  import { app, ask, disablePhone, pairPhone, refreshRemote, remote } from './lib/state.svelte';

  let relay = $state('');

  onMount(() => {
    void refreshRemote().then(() => (relay = remote.info?.relay ?? ''));
    const timer = window.setInterval(() => void refreshRemote(), 2500);
    return () => window.clearInterval(timer);
  });

  const info = $derived(remote.info);
  const status = $derived.by(() => {
    if (!info?.configured) return '';
    if (info.state === 'connected') return info.clients ? `Реле подключено · телефонов онлайн: ${info.clients}` : 'Реле подключено · телефон пока не подключился';
    return `Подключаюсь к реле…${info.error ? ' (' + info.error + ')' : ''}`;
  });

  async function regenerate() {
    const ok = await ask(
      'Создать новый QR-код?',
      'Старый ключ перестанет работать: все телефоны, подключённые по нему, придётся подключить заново.',
      'Создать новый',
      true,
    );
    if (ok) await pairPhone(info?.relay ?? relay);
  }

  async function disable() {
    if (await ask('Отключить телефон?', 'Ключ будет удалён, подключение к реле закроется.', 'Отключить', true)) await disablePhone();
  }
</script>

<div class="scrim" role="presentation" onclick={() => (app.phoneDialogOpen = false)} onkeydown={(e) => e.key === 'Escape' && (app.phoneDialogOpen = false)}>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Подключить телефон" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
    <h2>Телефон</h2>

    {#if !info}
      <p class="dim">Загружаю…</p>
    {:else if !info.configured}
      <p>Телефон показывает те же чаты и проекты, что и этот компьютер, и управляет агентами через ваше реле. Всё шифруется ключом из QR-кода — реле видит только зашифрованный трафик.</p>
      <label class="field">
        <span>Адрес реле</span>
        <input type="text" bind:value={relay} placeholder="https://example.com/agent8s" />
      </label>
      <p class="dim">Компьютер должен быть включён и не спать, пока вы им пользуетесь с телефона — приложение удерживает его от засыпания.</p>
      <div class="actions">
        <button class="btn" onclick={() => (app.phoneDialogOpen = false)}>Закрыть</button>
        <button class="primary" disabled={remote.busy || !relay.trim()} onclick={() => pairPhone(relay)}>{remote.busy ? 'Создаю…' : 'Создать QR-код'}</button>
      </div>
    {:else}
      <div class="qrwrap">
        <div class="qr">{@html remote.svg}</div>
        <ol>
          <li>Наведите камеру iPhone на QR-код и откройте ссылку в Safari.</li>
          <li>Чтобы поставить как приложение: «Поделиться» → «На экран „Домой“». Приложение попросит ключ — подсказки покажет страница.</li>
        </ol>
      </div>
      <div class="status" class:ok={info.state === 'connected'}>{status}</div>
      <p class="dim">QR-код содержит ключ шифрования. Не показывайте его другим.</p>
      <div class="actions">
        <button class="btn danger" onclick={disable}>Отключить</button>
        <button class="btn" onclick={regenerate}>Новый QR-код…</button>
        <button class="primary" onclick={() => (app.phoneDialogOpen = false)}>Готово</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; grid-template-columns: minmax(0, 1fr); place-items: center; z-index: 50; }
  .dialog { min-width: 0; width: min(520px, 92vw); max-height: 92vh; overflow-y: auto; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 12px; font-size: 17px; }
  p { margin: 0 0 12px; line-height: 1.45; }
  .dim { color: var(--text-dim); font-size: 12.5px; }
  .field { display: block; margin: 12px 0; }
  .field span { display: block; font-size: 12px; font-weight: 600; color: var(--text-dim); margin-bottom: 6px; }
  .field input { width: 100%; }
  .qrwrap { display: flex; gap: 18px; align-items: flex-start; }
  .qr { flex: none; width: 190px; background: #fff; padding: 8px; border-radius: 10px; line-height: 0; }
  .qr :global(svg) { width: 100%; height: auto; }
  ol { margin: 0; padding-left: 18px; line-height: 1.45; color: var(--text-dim); }
  ol li { margin-bottom: 8px; }
  .status { margin: 14px 0 8px; font-size: 12.5px; color: var(--text-dim); }
  .status.ok { color: var(--ok); }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
  @media (max-width: 520px) { .qrwrap { flex-direction: column; align-items: center; } }
</style>
