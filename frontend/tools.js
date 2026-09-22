const $ = (id) => document.getElementById(id);
const canvas = $('source-canvas');
const context = canvas.getContext('2d');
const state = {
  tools: [], counts: null, files: [], selected: -1, bitmap: null, roi: null, drag: null,
  resultUrl: null, revision: 0, loadSeq: 0,
};

function status(message, error = false) {
  $('workbench-status').textContent = message;
  $('workbench-status').style.color = error ? '#f1a5a5' : '#82d9b2';
}

function selectedTool() {
  return state.tools.find((tool) => tool.id === $('tool-select').value);
}

function clearResult() {
  state.revision++;
  if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
  state.resultUrl = null;
  $('result-image').hidden = true;
  $('result-image').removeAttribute('src');
  $('result-empty').hidden = false;
  $('download-result').hidden = true;
}

function renderCatalog() {
  const available = state.tools.filter((tool) => tool.catalog_state === 'ready');
  const counts = state.counts;
  $('counts').textContent = `${counts.ready_algorithms} 项可运行算法 · ${counts.pending_adapter} 项待接入 · ${counts.planned_algorithms} 项规划算法 · ${counts.planned_workflows} 项规划工作流`;
  const categories = [...new Set(state.tools.map((tool) => tool.category))];
  $('category').replaceChildren(new Option('全部类别', ''), ...categories.map((name) => new Option(name, name)));
  renderCards();
  $('tool-select').replaceChildren(...available.map((tool) => new Option(tool.name, tool.id)));
  const requested = new URLSearchParams(location.search).get('id');
  if (available.some((tool) => tool.id === requested)) $('tool-select').value = requested;
  renderParameters();
}

function renderCards() {
  const host = $('cards');
  host.replaceChildren();
  const search = $('tool-search').value.trim().toLocaleLowerCase();
  const filtered = state.tools.filter((tool) =>
    (!($('category').value) || tool.category === $('category').value)
    && (!search || `${tool.name} ${tool.description} ${tool.function_name}`.toLocaleLowerCase().includes(search)));
  for (const tool of filtered) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'card';
    card.dataset.active = String(tool.id === $('tool-select').value);
    if (tool.effect_image) {
      const image = document.createElement('img');
      image.src = tool.effect_image;
      image.alt = `${tool.name}的示例效果`;
      image.loading = 'lazy';
      card.append(image);
    }
    const body = document.createElement('div');
    body.className = 'card-body';
    const title = document.createElement('div');
    title.className = 'card-title';
    const name = document.createElement('strong');
    name.textContent = tool.name;
    const badge = document.createElement('span');
    badge.className = `badge ${tool.catalog_state === 'ready' ? '' : 'device'}`;
    badge.textContent = tool.catalog_state === 'ready' ? '可试用'
      : tool.catalog_state === 'pending_adapter' ? '待接入' : '规划中';
    title.append(name, badge);
    const metadata = document.createElement('div');
    metadata.className = 'card-meta';
    metadata.textContent = `${tool.category} · ${tool.function_name}`;
    const description = document.createElement('div');
    description.className = 'card-description';
    description.textContent = tool.catalog_state === 'ready'
      ? tool.description : `${tool.description} ${tool.availability_reason || ''}`;
    body.append(title, metadata, description);
    card.append(body);
    card.onclick = () => {
      if (tool.catalog_state === 'ready') {
        $('tool-select').value = tool.id;
        renderParameters();
        renderCards();
        clearResult();
        document.querySelector('.run-panel').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } else {
        status(tool.availability_reason || '当前不可运行', true);
      }
    };
    host.append(card);
  }
}

function renderParameters() {
  const tool = selectedTool();
  const host = $('tool-params');
  host.replaceChildren();
  if (!tool) return;
  $('tool-detail-link').href = `tool.html?id=${encodeURIComponent(tool.id)}`;
  const colors = { red: '红色', yellow: '黄色', green: '绿色', blue: '蓝色', white: '白色' };
  for (const param of tool.params_schema || []) {
    const wrapper = document.createElement('label');
    wrapper.textContent = param.label;
    wrapper.dataset.when = JSON.stringify(param.when || {});
    let input;
    if (param.kind === 'choice') {
      input = document.createElement('select');
      for (const value of param.options) input.add(new Option(colors[value] || value, value));
      input.value = String(param.default);
    } else {
      input = document.createElement('input');
      input.type = param.kind === 'boolean' ? 'checkbox' : 'number';
      if (param.minimum !== null) input.min = String(param.minimum);
      if (param.maximum !== null) input.max = String(param.maximum);
      if (param.kind === 'boolean') input.checked = Boolean(param.default);
      else input.value = String(param.default);
    }
    input.dataset.param = param.name;
    input.onchange = () => { applyWhen(); clearResult(); };
    wrapper.append(input);
    host.append(wrapper);
  }
  applyWhen();
}

function applyWhen() {
  const values = Object.fromEntries([...$('tool-params').querySelectorAll('[data-param]')]
    .map((input) => [input.dataset.param, input.type === 'checkbox' ? input.checked : input.value]));
  for (const wrapper of $('tool-params').children) {
    const conditions = JSON.parse(wrapper.dataset.when || '{}');
    wrapper.hidden = !Object.entries(conditions).every(([key, value]) => values[key] === value);
  }
}

function renderFiles() {
  $('file-count').textContent = `图片列表（${state.files.length}）`;
  const host = $('files');
  host.replaceChildren();
  state.files.forEach((entry, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'file';
    button.title = entry.name;
    button.textContent = entry.name;
    button.dataset.active = String(index === state.selected);
    button.onclick = () => selectFile(index);
    host.append(button);
  });
}

function renderSource() {
  if (!state.bitmap) return;
  canvas.width = state.bitmap.width;
  canvas.height = state.bitmap.height;
  context.drawImage(state.bitmap, 0, 0);
  if (state.roi) {
    const { x, y, width, height } = state.roi;
    context.fillStyle = 'rgba(117, 212, 245, .16)';
    context.fillRect(x, y, width, height);
    context.strokeStyle = '#75d4f5';
    context.lineWidth = Math.max(2, state.bitmap.width / 700);
    context.strokeRect(x, y, width, height);
  }
  $('image-size').textContent = `${canvas.width} × ${canvas.height} px`;
}

function syncRegion() {
  for (const key of ['x', 'y', 'width', 'height']) {
    $(`roi-${key}`).value = state.roi ? String(state.roi[key]) : '';
  }
  renderSource();
  clearResult();
}

async function selectFile(index) {
  const entry = state.files[index];
  if (!entry) return;
  const loadSeq = ++state.loadSeq;
  try {
    const bitmap = await createImageBitmap(entry.blob);
    if (loadSeq !== state.loadSeq) { bitmap.close(); return; }
    state.bitmap?.close();
    state.bitmap = bitmap;
    state.selected = index;
    state.roi = null;
    $('image-name').textContent = entry.name;
    clearResult();
    renderFiles();
    syncRegion();
    status(`已打开 ${entry.name}`);
  } catch (error) {
    status(`图片无法预览：${error.message}`, true);
  }
}

function openFiles(fileList) {
  const files = [...fileList].filter((file) => file.type.startsWith('image/'));
  files.sort((a, b) => (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, 'zh-CN'));
  if (!files.length) return status('所选位置没有可用图片', true);
  state.files = files.map((file) => ({ name: file.webkitRelativePath || file.name, blob: file }));
  selectFile(0);
}

async function useDemo() {
  try {
    const response = await fetch('/api/image-tools/demo-source');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.files = [{ name: 'PixelForge 示例图.png', blob: await response.blob() }];
    await selectFile(0);
  } catch (error) {
    status(`示例图加载失败：${error.message}`, true);
  }
}

async function useSavedAsset(assetId) {
  const details = await fetch(`/api/image-lab/assets/${encodeURIComponent(assetId)}`);
  if (!details.ok) throw new Error(`素材读取失败：HTTP ${details.status}`);
  const asset = await details.json();
  const content = await fetch(asset.preview_url);
  if (!content.ok) throw new Error(`预览读取失败：HTTP ${content.status}`);
  state.files = [{ name: asset.relative_name, blob: await content.blob(), assetId }];
  await selectFile(0);
}

function point(event) {
  const bounds = canvas.getBoundingClientRect();
  return {
    x: Math.round(Math.min(canvas.width, Math.max(0, (event.clientX - bounds.left) * canvas.width / bounds.width))),
    y: Math.round(Math.min(canvas.height, Math.max(0, (event.clientY - bounds.top) * canvas.height / bounds.height))),
  };
}

function regionFromPoints(first, second) {
  const x = Math.min(first.x, second.x);
  const y = Math.min(first.y, second.y);
  return { x, y, width: Math.max(1, Math.abs(first.x - second.x)), height: Math.max(1, Math.abs(first.y - second.y)) };
}

canvas.addEventListener('pointerdown', (event) => {
  if (!state.bitmap || event.button !== 0) return;
  state.drag = point(event);
  canvas.setPointerCapture(event.pointerId);
});
canvas.addEventListener('pointermove', (event) => {
  if (!state.drag) return;
  state.roi = regionFromPoints(state.drag, point(event));
  syncRegion();
});
canvas.addEventListener('pointerup', (event) => {
  if (!state.drag) return;
  state.roi = regionFromPoints(state.drag, point(event));
  state.drag = null;
  syncRegion();
});
canvas.addEventListener('pointercancel', () => { state.drag = null; });

for (const key of ['x', 'y', 'width', 'height']) {
  $(`roi-${key}`).addEventListener('change', () => {
    if (!state.bitmap) return;
    const values = Object.fromEntries(['x', 'y', 'width', 'height'].map((field) => [field, Number($(`roi-${field}`).value)]));
    if (!Object.values(values).every(Number.isFinite) || values.width < 1 || values.height < 1) {
      return status('选区需要有效的 X、Y、宽和高', true);
    }
    const x = Math.max(0, Math.min(canvas.width - 1, Math.round(values.x)));
    const y = Math.max(0, Math.min(canvas.height - 1, Math.round(values.y)));
    state.roi = {
      x, y,
      width: Math.max(1, Math.min(canvas.width - x, Math.round(values.width))),
      height: Math.max(1, Math.min(canvas.height - y, Math.round(values.height))),
    };
    syncRegion();
  });
}

$('clear-roi').onclick = () => { state.roi = null; syncRegion(); };
$('open-files').onchange = (event) => { openFiles(event.target.files); event.target.value = ''; };
$('open-directory').onchange = (event) => { openFiles(event.target.files); event.target.value = ''; };
$('use-demo').onclick = useDemo;
$('category').onchange = renderCards;
$('tool-search').oninput = renderCards;
$('tool-select').onchange = () => { renderParameters(); renderCards(); clearResult(); };

$('run-tool').onclick = async () => {
  const tool = selectedTool();
  const entry = state.files[state.selected];
  if (!tool || !entry) return status('请先打开图片并选择工具', true);
  if (tool.id === 'crop' && !state.roi) return status('区域裁剪需要先在原图上框选', true);
  if (entry.blob.size > 20 * 1024 * 1024) return status('图片超过 20 MB 限制', true);
  const query = new URLSearchParams();
  if (state.roi) for (const [key, value] of Object.entries(state.roi)) query.set(key, String(value));
  for (const input of $('tool-params').querySelectorAll('[data-param]')) {
    query.set(input.dataset.param, input.type === 'checkbox' ? String(input.checked) : input.value);
  }
  const revision = state.revision;
  $('run-tool').disabled = true;
  status(`正在运行 ${tool.name}…`);
  try {
    const response = await fetch(`/api/image-tools/${encodeURIComponent(tool.id)}/run?${query}`, {
      method: 'POST', headers: { 'Content-Type': entry.blob.type || 'application/octet-stream' }, body: entry.blob,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    if (revision !== state.revision) return;
    if (state.resultUrl) URL.revokeObjectURL(state.resultUrl);
    state.resultUrl = URL.createObjectURL(blob);
    $('result-image').src = state.resultUrl;
    $('result-image').hidden = false;
    $('result-empty').hidden = true;
    const download = $('download-result');
    download.href = state.resultUrl;
    download.download = `${entry.name.split('/').pop().replace(/\.[^.]+$/, '')}_${tool.id}.png`;
    download.hidden = false;
    status(`${tool.name}处理完成 · ${Math.round(blob.size / 1024)} KB`);
  } catch (error) {
    status(`处理失败：${error.message}`, true);
  } finally {
    $('run-tool').disabled = false;
  }
};

try {
  const response = await fetch('/api/image-lab/tools');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const catalog = await response.json();
  state.tools = catalog.items;
  state.counts = catalog.counts;
  renderCatalog();
  const assetId = new URLSearchParams(location.search).get('asset');
  if (assetId) await useSavedAsset(assetId);
  else await useDemo();
} catch (error) {
  status(`功能列表加载失败：${error.message}`, true);
}
