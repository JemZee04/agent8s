<script lang="ts">
  import { notify, pairWith } from './lib/state.svelte';

  let text = $state('');
  let busy = $state(false);

  async function paste() {
    try {
      text = await navigator.clipboard.readText();
    } catch {
      notify('Не удалось прочитать буфер обмена — вставьте ключ вручную в поле ниже.');
      return;
    }
    if (text.trim()) await submit();
  }

  async function submit() {
    busy = true;
    await pairWith(text);
    busy = false;
  }
</script>

<main>
  <h1>agent8s</h1>
  <p>Телефон ещё не подключён к компьютеру.</p>
  <ol>
    <li>На компьютере откройте agent8s → «Телефон» и покажите QR-код.</li>
    <li>Наведите на него камеру iPhone и откройте ссылку в Safari — подключение произойдёт само.</li>
  </ol>
  <h2>Приложение на экране «Домой»</h2>
  <p class="dim">iOS хранит данные такого приложения отдельно от Safari, поэтому ключ нужно передать один раз: на странице в Safari нажмите «Скопировать ключ», затем откройте приложение и нажмите «Вставить ключ».</p>
  <button class="primary big" disabled={busy} onclick={paste}>Вставить ключ</button>
  <details>
    <summary>Ввести вручную</summary>
    <textarea rows="3" bind:value={text} placeholder="Ссылка или ключ сопряжения"></textarea>
    <button class="btn" disabled={busy || !text.trim()} onclick={submit}>Подключить</button>
  </details>
</main>

<style>
  main { max-width: 460px; margin: 0 auto; padding: max(28px, env(safe-area-inset-top)) 22px 28px; overflow-y: auto; height: 100%; }
  h1 { font-size: 26px; margin: 12px 0 6px; }
  h2 { font-size: 15px; margin: 26px 0 6px; }
  p { line-height: 1.5; }
  ol { padding-left: 20px; line-height: 1.5; }
  li { margin-bottom: 6px; }
  .dim { color: var(--text-dim); font-size: 13.5px; }
  .big { width: 100%; padding: 12px; font-size: 16px; margin-top: 8px; border-radius: 12px; }
  details { margin-top: 18px; color: var(--text-dim); }
  textarea { display: block; width: 100%; margin: 10px 0; font-size: 16px; border-radius: 10px; border: 1px solid var(--border); background: var(--bg-elev); padding: 8px; user-select: text; }
</style>
