const $ = (id) => document.getElementById(id);
const state = { selected: [], assets: [], activeId: null, batchId: null };

function explain(status, detail) {
  return detail?.detail || `HTTP ${status}`;
}

function choose(files) {
  state.selected = [...files].filter((file) => file.type.startsWith('image/'));
  state.selected.sort((a, b) =>
    (a.webkitRelativePath || a.name).localeCompare(b.webkitRelativePath || b.name, 'zh-CN'));
  const size = state.selected.reduce((total, file) => total + file.size, 0);
  $('selection-summary').textContent = state.selected.length
    ? `${state.selected.length} 张图片 · ${(size / 1024 / 1024).toFixed(1)} MB · ${state.selected[0].webkitRelativePath || state.selected[0].name}${state.selected.length > 1 ? ' 等' : ''}`
    : '所选位置没有可导入的图片。';
  $('import-selected').disabled = !state.selected.length;
  $('import-status').textContent = '';
}

async function refresh() {
  try {
    const response = await fetch('/api/image-lab/assets?limit=500');
    if (!response.ok) throw new Error(explain(response.status, await response.json().catch(() => ({}))));
    state.assets = (await response.json()).items;
    $('asset-count').textContent = `${state.assets.length} 张已保存图片`;
    const host = $('asset-list');
    host.replaceChildren();
    for (const asset of state.assets) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'asset-item';
      button.dataset.assetId = asset.id;
      button.dataset.active = String(state.activeId === asset.id);
      const thumbnail = document.createElement('img');
      thumbnail.src = asset.thumbnail_url;
      thumbnail.alt = '';
      thumbnail.loading = 'lazy';
      const title = document.createElement('strong');
      title.textContent = asset.relative_name;
      const detail = document.createElement('small');
      detail.textContent = `${asset.width} × ${asset.height} · ${new Date(asset.created_at).toLocaleString()}`;
      button.append(thumbnail, title, detail);
      button.onclick = () => select(asset.id);
      host.append(button);
    }
    if (state.activeId && state.assets.some((asset) => asset.id === state.activeId)) select(state.activeId);
  } catch (error) {
    $('import-status').textContent = `素材读取失败：${error.message}`;
  }
}

function select(id) {
  const asset = state.assets.find((item) => item.id === id);
  if (!asset) return;
  state.activeId = id;
  for (const button of $('asset-list').children) {
    button.dataset.active = String(button.dataset.assetId === id);
  }
  $('asset-preview').src = asset.preview_url;
  $('asset-preview').hidden = false;
  $('preview-meta').textContent = `${asset.relative_name} · ${asset.width} × ${asset.height} px · SHA-256 ${asset.sha256}`;
  const process = $('process-asset');
  process.href = `tools.html?asset=${encodeURIComponent(id)}`;
  process.hidden = false;
  const download = $('download-original');
  download.href = `/api/image-lab/assets/${encodeURIComponent(id)}/content?variant=original`;
  download.download = asset.relative_name.split('/').pop();
  download.hidden = false;
}

async function importSelected() {
  const button = $('import-selected');
  button.disabled = true;
  const batchId = crypto.randomUUID();
  state.batchId = batchId;
  let succeeded = 0;
  const failures = [];
  for (const file of state.selected) {
    const name = file.webkitRelativePath || file.name;
    $('import-status').textContent = `导入中 ${succeeded + failures.length + 1}/${state.selected.length}：${name}`;
    try {
      if (file.size > 20 * 1024 * 1024) throw new Error('单张图片超过 20 MB');
      const query = new URLSearchParams({ relative_name: name, batch_id: batchId });
      const response = await fetch(`/api/image-lab/imports?${query}`, {
        method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream' }, body: file,
      });
      if (!response.ok) throw new Error(explain(response.status, await response.json().catch(() => ({}))));
      succeeded++;
    } catch (error) {
      failures.push(`${name}: ${error.message}`);
    }
  }
  $('import-status').textContent = `${succeeded} 张导入成功${failures.length ? `，${failures.length} 张失败：${failures.slice(0, 3).join('；')}` : ''}`;
  button.disabled = !state.selected.length;
  await refresh();
}

$('pick-files').onchange = (event) => { choose(event.target.files); event.target.value = ''; };
$('pick-directory').onchange = (event) => { choose(event.target.files); event.target.value = ''; };
$('import-selected').onclick = importSelected;
$('refresh-assets').onclick = refresh;
refresh();
