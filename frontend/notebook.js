import { api } from './api.js';

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const notebookId = params.get('id');

const state = {
  notebook: null,
  tools: [],
  toolsById: {},
  assets: [],
  selectedStepId: null,
  runs: [],
  pollingRunId: null,
  pendingSave: false,
};

function explain(error) {
  return error.message || `${error.status}`;
}

function setSaveStatus(text, error = false) {
  const el = $('save-status');
  el.textContent = text;
  el.style.color = error ? '#f1a5a5' : '#82d9b2';
}

function setRunStatus(text) {
  $('run-status').textContent = text || '';
}

function toolSpec(toolId) {
  return state.toolsById[toolId];
}

function selectedStep() {
  return state.notebook?.steps.find((s) => s.id === state.selectedStepId);
}

function selectedRun() {
  return state.runs.find((r) => r.id === state.pollingRunId);
}

function freshStep(toolId) {
  const spec = toolSpec(toolId);
  const id = crypto.randomUUID().replace(/-/g, '');
  const inputs = {};
  const prev = lastImageOutputStep();
  if (prev) {
    inputs.image = { kind: 'step', step_id: prev.id, port: 'image' };
  }
  return {
    id,
    kind: 'tool',
    title: spec?.name || toolId,
    tool_id: toolId,
    tool_version: spec?.version || '1.0.0',
    inputs,
    params: defaultParams(toolId),
    roi: null,
  };
}

function freshNote() {
  return { id: crypto.randomUUID().replace(/-/g, ''), kind: 'note', title: '说明' };
}

function lastImageOutputStep() {
  if (!state.notebook) return null;
  for (let i = state.notebook.steps.length - 1; i >= 0; i--) {
    const s = state.notebook.steps[i];
    if (s.kind === 'tool' && toolSpec(s.tool_id)?.outputs?.image === 'IMAGE_RGB8') return s;
  }
  return null;
}

function defaultParams(toolId) {
  const spec = toolSpec(toolId);
  const out = {};
  if (!spec) return out;
  for (const p of spec.params) {
    out[p.name] = p.default;
  }
  return out;
}

function inputLabel(ref) {
  if (!ref) return '无';
  if (ref.kind === 'asset') {
    const asset = state.assets.find((a) => a.id === ref.asset_id);
    return `素材：${asset ? asset.relative_name : ref.asset_id}`;
  }
  if (ref.kind === 'step') {
    const step = state.notebook.steps.find((s) => s.id === ref.step_id);
    return `步骤 ${(step?.position ?? 0) + 1} / ${ref.port}`;
  }
  return '未知';
}

async function loadNotebook() {
  if (!notebookId) {
    setSaveStatus('缺少 id 参数', true);
    return;
  }
  try {
    const [nb, toolsData, assetsData] = await Promise.all([
      api.notebook(notebookId),
      api.imageLabTools(),
      api.imageLabAssets(),
    ]);
    state.notebook = nb;
    state.tools = toolsData.items.filter((t) => t.catalog_state === 'ready');
    state.toolsById = Object.fromEntries(state.tools.map((t) => [t.id, t]));
    state.assets = assetsData.items;
    if (!state.selectedStepId && nb.steps.length) state.selectedStepId = nb.steps[0].id;
    renderHeader();
    renderAssets();
    renderSteps();
    renderFlow();
    renderInspector();
    await loadRuns();
  } catch (error) {
    setSaveStatus(`读取失败：${explain(error)}`, true);
  }
}

function renderHeader() {
  const nb = state.notebook;
  $('nb-title').textContent = nb.title || '未命名实验本';
  $('nb-meta').textContent = `rev ${nb.revision} · ${nb.steps.length} 步 · ${new Date(nb.updated_at).toLocaleString()}`;
  setSaveStatus('已保存');
}

async function save() {
  if (!state.notebook) return;
  setSaveStatus('保存中…');
  try {
    const payload = {
      expected_revision: state.notebook.revision,
      title: state.notebook.title,
      description: state.notebook.description || '',
      steps: state.notebook.steps,
    };
    state.notebook = await api.saveNotebook(notebookId, payload);
    state.pendingSave = false;
    renderHeader();
    renderSteps();
    renderFlow();
    renderInspector();
    setSaveStatus('已保存');
  } catch (error) {
    setSaveStatus(`保存失败：${explain(error)}`, true);
  }
}

async function addTool() {
  if (!state.tools.length) {
    setSaveStatus('没有可用的工具', true);
    return;
  }
  const id = prompt(`可用工具：${state.tools.map((t) => t.id).join(', ')}\n输入工具 ID：`, state.tools[0].id);
  if (!id || !toolSpec(id)) return;
  const step = freshStep(id);
  state.notebook.steps.push(step);
  renumberSteps();
  state.selectedStepId = step.id;
  markDirty();
}

async function addNote() {
  const step = freshNote();
  state.notebook.steps.push(step);
  renumberSteps();
  state.selectedStepId = step.id;
  markDirty();
}

async function loadRuns() {
  if (!state.notebook) return;
  try {
    state.runs = (await api.notebookRuns(notebookId)).items;
    renderHistory();
    if (state.pollingRunId) {
      const run = state.runs.find((r) => r.id === state.pollingRunId);
      if (run && ['running', 'pending', 'cancelling'].includes(run.status)) {
        setTimeout(loadRuns, 800);
      } else if (run) {
        run.detail = await api.notebookRun(notebookId, run.id);
        renderHistory();
        renderFlow();
        setRunStatus('');
        $('stop-run').hidden = true;
      }
    }
  } catch (error) {
    setRunStatus(`历史读取失败：${explain(error)}`);
  }
}

function renderHistory() {
  const host = $('run-history');
  host.replaceChildren();
  if (!state.runs.length) {
    host.innerHTML = '<div class="hint">尚无运行记录。</div>';
    return;
  }
  for (const run of state.runs) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'run-item';
    const status = document.createElement('span');
    status.className = `run-status ${run.status}`;
    status.textContent = run.status;
    const meta = document.createElement('span');
    meta.textContent = ` rev ${run.notebook_revision} · ${new Date(run.started_at).toLocaleString()}`;
    btn.append(status, meta);
    btn.onclick = async () => {
      state.pollingRunId = run.id;
      run.detail = await api.notebookRun(notebookId, run.id);
      renderHistory();
      renderFlow();
    };
    host.append(btn);
  }
}

async function runAll() {
  if (!state.notebook) return;
  if (state.pendingSave) {
    setRunStatus('请先保存草稿');
    return;
  }
  setRunStatus('正在提交运行…');
  try {
    const resp = await api.startNotebookRun(notebookId);
    state.pollingRunId = resp.run_id;
    $('stop-run').hidden = false;
    await loadRuns();
    pollUntilDone();
  } catch (error) {
    setRunStatus(`运行失败：${explain(error)}`);
  }
}

async function pollUntilDone() {
  if (!state.pollingRunId) return;
  try {
    const run = await api.notebookRun(notebookId, state.pollingRunId);
    state.runs = state.runs.filter((r) => r.id !== run.id);
    state.runs.unshift({
      id: run.id,
      notebook_revision: run.notebook_revision,
      status: run.status,
      started_at: run.started_at,
      finished_at: run.finished_at,
      error: run.error,
      detail: run,
    });
    renderHistory();
    renderFlow();
    if (['running', 'pending', 'cancelling'].includes(run.status)) {
      setTimeout(pollUntilDone, 800);
    } else {
      setRunStatus('');
      $('stop-run').hidden = true;
    }
  } catch (error) {
    setRunStatus(`轮询失败：${explain(error)}`);
  }
}

async function stopRun() {
  if (!state.pollingRunId) return;
  setRunStatus('正在取消…');
  try {
    await api.cancelNotebookRun(notebookId, state.pollingRunId);
  } catch (error) {
    setRunStatus(`取消失败：${explain(error)}`);
  }
}

$('save-notebook').onclick = save;
$('run-all').onclick = runAll;
$('stop-run').onclick = stopRun;
$('add-tool').onclick = addTool;
$('add-note').onclick = addNote;

loadNotebook();


function renderInspector() {
  const host = $('step-inspector');
  host.replaceChildren();
  const step = selectedStep();
  if (!step) {
    host.innerHTML = '<div class="hint">选择或添加一个步骤。</div>';
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'inspector';

  const titleLabel = document.createElement('label');
  titleLabel.textContent = '名称';
  const titleInput = document.createElement('input');
  titleInput.type = 'text';
  titleInput.value = step.title;
  titleInput.oninput = (e) => { step.title = e.target.value; markDirty(); };
  titleLabel.append(titleInput);
  wrap.append(titleLabel);

  if (step.kind === 'tool') {
    const inputLabelEl = document.createElement('label');
    inputLabelEl.textContent = '输入来源';
    const inputSelect = document.createElement('select');
    inputSelect.append(new Option('选择输入…', ''));
    for (const asset of state.assets) {
      inputSelect.append(new Option(`素材：${asset.relative_name}`, JSON.stringify({ kind: 'asset', asset_id: asset.id })));
    }
    for (const other of state.notebook.steps) {
      if (other.kind !== 'tool' || other.position >= step.position) continue;
      const spec = toolSpec(other.tool_id);
      if (!spec?.outputs) continue;
      for (const [port, type] of Object.entries(spec.outputs)) {
        if (type !== 'IMAGE_RGB8') continue;
        inputSelect.append(new Option(`步骤 ${other.position + 1} / ${port}`, JSON.stringify({ kind: 'step', step_id: other.id, port })));
      }
    }
    if (step.inputs?.image) {
      const key = JSON.stringify(step.inputs.image);
      for (const opt of inputSelect.options) {
        if (opt.value === key) inputSelect.value = key;
      }
    }
    inputSelect.onchange = (e) => {
      step.inputs.image = e.target.value ? JSON.parse(e.target.value) : null;
      markDirty();
    };
    inputLabelEl.append(inputSelect);
    wrap.append(inputLabelEl);

    const spec = toolSpec(step.tool_id);
    if (spec?.needs_roi) {
      const roiLabel = document.createElement('label');
      roiLabel.textContent = 'ROI [x, y, width, height]';
      const roiInput = document.createElement('input');
      roiInput.type = 'text';
      roiInput.value = step.roi ? step.roi.join(', ') : '';
      roiInput.placeholder = '例如：10, 20, 100, 80';
      roiInput.onchange = (e) => {
        const parts = e.target.value.split(',').map((s) => Number(s.trim())).filter((n) => !Number.isNaN(n));
        step.roi = parts.length === 4 ? parts : null;
        markDirty();
      };
      roiLabel.append(roiInput);
      wrap.append(roiLabel);
    }

    if (spec?.params?.length) {
      const paramsTitle = document.createElement('h3');
      paramsTitle.textContent = '参数';
      paramsTitle.style.fontSize = '14px';
      wrap.append(paramsTitle);
      for (const p of spec.params) {
        const row = document.createElement('div');
        row.className = 'param-row';
        const lbl = document.createElement('label');
        lbl.textContent = `${p.label} (${p.name})`;
        let input;
        if (p.kind === 'choice') {
          input = document.createElement('select');
          for (const opt of p.options) input.append(new Option(opt, opt));
          input.value = step.params[p.name] ?? p.default;
        } else {
          input = document.createElement('input');
          input.type = p.kind === 'boolean' ? 'checkbox' : (p.kind === 'integer' ? 'number' : 'text');
          if (p.kind === 'boolean') input.checked = Boolean(step.params[p.name] ?? p.default);
          else input.value = step.params[p.name] ?? p.default;
        }
        input.onchange = (e) => {
          step.params[p.name] = p.kind === 'boolean' ? e.target.checked : (p.kind === 'integer' ? Number(e.target.value) : e.target.value);
          markDirty();
        };
        lbl.append(input);
        row.append(lbl);
        wrap.append(row);
      }
    }
  }

  host.append(wrap);
}

function chooseAssetInput(assetId) {
  const step = selectedStep();
  if (!step || step.kind !== 'tool') return;
  step.inputs.image = { kind: 'asset', asset_id: assetId };
  markDirty();
}

function moveStep(id, delta) {
  const steps = state.notebook.steps;
  const idx = steps.findIndex((s) => s.id === id);
  if (idx < 0) return;
  const other = idx + delta;
  if (other < 0 || other >= steps.length) return;
  [steps[idx], steps[other]] = [steps[other], steps[idx]];
  renumberSteps();
  markDirty();
}

function deleteStep(id) {
  state.notebook.steps = state.notebook.steps.filter((s) => s.id !== id);
  if (state.selectedStepId === id) state.selectedStepId = state.notebook.steps[0]?.id || null;
  renumberSteps();
  markDirty();
}

function renumberSteps() {
  state.notebook.steps.forEach((s, i) => { s.position = i; });
}

function markDirty() {
  state.pendingSave = true;
  setSaveStatus('未保存', true);
  renderSteps();
  renderFlow();
}

