import { api } from './api.js';

const $ = (id) => document.getElementById(id);
const state = { items: [] };

function status(message, error = false) {
  const el = $('notebook-count');
  el.textContent = message;
  el.style.color = error ? '#f1a5a5' : '#b8c8d8';
}

function render() {
  const host = $('notebook-list');
  host.replaceChildren();
  if (!state.items.length) {
    host.innerHTML = '<div class="hint">还没有实验本，从左侧创建一个。</div>';
    return;
  }
  for (const item of state.items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'notebook-item';
    const title = document.createElement('strong');
    title.textContent = item.title || '未命名';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = `rev ${item.revision} · ${item.step_count} 步 · ${new Date(item.updated_at).toLocaleString()}`;
    const st = document.createElement('div');
    st.className = 'status';
    st.textContent = item.status === 'active' ? '活跃' : item.status;
    button.append(title, meta, st);
    button.onclick = () => { location.href = `notebook.html?id=${encodeURIComponent(item.id)}`; };
    host.append(button);
  }
  status(`${state.items.length} 个实验本`);
}

async function refresh() {
  try {
    state.items = (await api.notebooks()).items;
    render();
  } catch (error) {
    status(`读取失败：${error.message}`, true);
  }
}

async function create() {
  const title = $('new-title').value.trim();
  if (!title) {
    status('请输入标题', true);
    return;
  }
  try {
    const item = await api.createNotebook({ title, description: $('new-description').value.trim() });
    location.href = `notebook.html?id=${encodeURIComponent(item.id)}`;
  } catch (error) {
    status(`创建失败：${error.message}`, true);
  }
}

$('create-notebook').onclick = create;
$('refresh-notebooks').onclick = refresh;
refresh();
