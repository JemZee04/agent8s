<script lang="ts">
  import { onMount } from 'svelte';
  import { api, native } from './lib/api';
  import { addProject, app, createChat } from './lib/state.svelte';
  import AgentPicker from './AgentPicker.svelte';

  let projectId = $state<number | null>(app.newChatProject ?? app.projects[0]?.id ?? null);
  let agent = $state(app.agents[0]?.id ?? 'claude');
  let model = $state('');
  let effort = $state('');
  let mode = $state<'worktree' | 'direct'>('worktree');
  let adding = $state(app.projects.length === 0);
  let discovered = $state<{ name: string; path: string }[]>([]);
  let manualPath = $state('');
  let busy = $state(false);

  onMount(async () => {
    try {
      discovered = (await api.get<{ repos: { name: string; path: string }[] }>('/api/discover')).repos;
    } catch { /* optional convenience */ }
  });

  const basename = (p: string) => p.replace(/\/+$/, '').split('/').pop() || p;

  async function register(name: string, path: string) {
    const project = await addProject(name, path);
    if (project) {
      projectId = project.id;
      adding = false;
      discovered = discovered.filter((d) => d.path !== project.path);
      manualPath = '';
    }
  }

  async function browse() {
    const picked = await native()?.pick_folder();
    if (picked) manualPath = picked;
  }

  async function create() {
    if (projectId === null || busy) return;
    busy = true;
    const chat = await createChat(projectId, agent, model, effort, mode);
    busy = false;
    if (chat) app.newChatOpen = false;
  }

  function change(patch: { agent?: string; model?: string; effort?: string }) {
    if (patch.agent !== undefined) { agent = patch.agent; model = ''; effort = ''; }
    if (patch.model !== undefined) { model = patch.model; effort = ''; }
    if (patch.effort !== undefined) effort = patch.effort;
  }
</script>

<div class="scrim" role="presentation" onclick={() => (app.newChatOpen = false)} onkeydown={(e) => e.key === 'Escape' && (app.newChatOpen = false)}>
  <div class="dialog" role="dialog" aria-modal="true" aria-label="Новый чат" tabindex="-1" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
    <h2>Новый чат</h2>

    <div class="field">
      <span class="label">Проект</span>
      {#if app.projects.length && !adding}
        <div class="line">
          <select bind:value={projectId}>
            {#each app.projects as p (p.id)}<option value={p.id}>{p.name}</option>{/each}
          </select>
          <button class="ghost" onclick={() => (adding = true)}>＋ Добавить проект</button>
        </div>
      {:else}
        <div class="adder">
          {#if discovered.length}
            <div class="sub">Найденные репозитории — нажмите, чтобы добавить:</div>
            <div class="chips">
              {#each discovered.slice(0, 24) as d (d.path)}
                <button class="chip" onclick={() => register(d.name, d.path)} title={d.path}>{d.name}</button>
              {/each}
            </div>
          {/if}
          <div class="sub">Или укажите путь к git-репозиторию:</div>
          <div class="line">
            <input type="text" bind:value={manualPath} placeholder="/Users/you/projects/app" style="flex:1" />
            {#if native()}<button class="btn" onclick={browse}>Обзор…</button>{/if}
            <button class="btn" disabled={!manualPath.trim()} onclick={() => register(basename(manualPath), manualPath.trim())}>Добавить</button>
          </div>
          {#if app.projects.length}<button class="ghost" onclick={() => (adding = false)}>← К списку проектов</button>{/if}
        </div>
      {/if}
    </div>

    <div class="field">
      <span class="label">Агент и модель</span>
      <AgentPicker {agent} {model} {effort} onchange={change} />
      <div class="sub">Агента и модель можно менять прямо в чате — контекст сохранится.</div>
    </div>

    <div class="field">
      <span class="label">Где работает агент</span>
      <label class="radio"><input type="radio" bind:group={mode} value="worktree" />
        <span><b>Отдельная ветка</b> (рекомендуется) — изолированная копия проекта; изменения не затрагивают вашу рабочую папку, пока вы не нажмёте «Влить».</span></label>
      <label class="radio"><input type="radio" bind:group={mode} value="direct" />
        <span><b>Прямо в проекте</b> — агент правит ваши файлы на текущей ветке сразу.</span></label>
    </div>

    <div class="actions">
      <button class="btn" onclick={() => (app.newChatOpen = false)}>Отмена</button>
      <button class="primary" disabled={projectId === null || busy} onclick={create}>{busy ? 'Создаю…' : 'Создать чат'}</button>
    </div>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: grid; grid-template-columns: minmax(0, 1fr); place-items: center; z-index: 50; }
  .dialog { min-width: 0; width: min(560px, 92vw); max-height: 90vh; overflow-y: auto; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 14px; padding: 20px 22px; box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3); }
  h2 { margin: 0 0 14px; font-size: 17px; }
  .field { margin-bottom: 16px; }
  .label { display: block; font-size: 12px; font-weight: 600; color: var(--text-dim); margin-bottom: 6px; }
  .line { display: flex; gap: 8px; align-items: center; }
  .sub { color: var(--text-dim); font-size: 12.5px; margin: 6px 0; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .chip { background: var(--accent-soft); color: var(--accent); border: 0; border-radius: 14px; padding: 3px 11px; }
  .chip:hover { filter: brightness(1.15); }
  .radio { display: flex; gap: 9px; align-items: flex-start; padding: 6px 0; line-height: 1.4; }
  .radio input { margin-top: 3px; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
  select { min-width: 200px; }
  @media (max-width: 760px) {
    .scrim { place-items: end center; }
    .dialog { width: 100%; max-height: 92dvh; border-radius: 18px 18px 0 0; padding-bottom: max(20px, env(safe-area-inset-bottom)); }
    select, input[type='text'] { font-size: 16px; }
  }
</style>
