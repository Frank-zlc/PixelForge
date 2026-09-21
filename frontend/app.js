/**
 * PixelForge IDE shell.
 *
 * No build step: plain ES modules the browser loads directly. For a tool whose
 * hard parts are video decoding and coordinate maths rather than component trees,
 * that is a better trade than a bundler -- edit, refresh, done, and deployment is
 * a handful of static files.
 */

import { api, openSocket } from './api.js';
import { Player } from './player.js';
import * as geo from './geometry.js';

const $ = (id) => document.getElementById(id);
const OWNER = `web-${Math.random().toString(36).slice(2, 8)}`;

const state = {
  devices: [],
  selected: null,
  session: null, // {token, capabilities, display, frame, rotation, notes}
  frame: null,
  display: null,
  nodes: [],
  lastPoint: null, // describe() output for the last click
  lastNode: null, // accessibility node under the last click, if any
  selection: null, // {x,y,width,height} in CSS space
  project: null,
  script: null,
  runId: null,
  heartbeat: null,
};

const player = new Player($('screen'), onPlayerStatus);
const overlay = $('overlay');
const overlayContext = overlay.getContext('2d');

// ---------------------------------------------------------------- devices

async function loadDevices() {
  try {
    state.devices = await api.devices();
  } catch (error) {
    state.devices = [];
    toast(`设备列表读取失败：${error.message}`);
  }
  renderDevices();
}

function renderDevices() {
  const host = $('device-list');
  host.textContent = '';
  if (!state.devices.length) {
    host.innerHTML = '<div class="muted">没有设备。用 USB 连接手机并允许调试授权。</div>';
    return;
  }
  for (const device of state.devices) {
    const node = document.createElement('div');
    node.className = 'device';
    node.setAttribute('aria-selected', String(device.serial === state.selected));
    const held = device.lease ? `<span class="pill pill--warn">${device.lease.owner} 占用</span>` : '';
    // `unauthorized` is worth calling out separately: the fix is tapping Allow on
    // the phone, which is very different from "the device is broken".
    const stateClass =
      device.state === 'device' ? 'pill--ok' : device.state === 'unauthorized' ? 'pill--warn' : 'pill--bad';
    node.innerHTML = `
      <div class="device__name">${escapeHtml(device.label)}</div>
      <div class="device__meta">${escapeHtml(device.serial)}</div>
      <div class="row" style="margin-top:5px">
        <span class="pill ${stateClass}">${device.state}</span>
        ${device.display ? `<span class="pill">${device.display.width}×${device.display.height}</span>` : ''}
        ${device.android_release ? `<span class="pill">Android ${device.android_release}</span>` : ''}
        ${held}
      </div>`;
    node.onclick = () => selectDevice(device.serial);
    host.append(node);
  }
}

function selectDevice(serial) {
  state.selected = serial;
  renderDevices();
  const device = state.devices.find((d) => d.serial === serial);
  $('connect').disabled = !device || device.state !== 'device';
}

// ---------------------------------------------------------------- session

async function connect(force = false) {
  const serial = state.selected;
  if (!serial) return;
  try {
    const opener = force ? api.forceSession : api.openSession;
    state.session = await opener(serial, OWNER, state.project?.id);
  } catch (error) {
    if (error.status === 409 && !force && confirm(`${error.message}\n\n强制接管？`)) {
      return connect(true);
    }
    toast(`获取设备失败：${error.message}`);
    return;
  }
  const session = state.session;
  state.display = session.display ? { width: session.display[0], height: session.display[1] } : null;
  state.frame = session.frame ? { width: session.frame[0], height: session.frame[1] } : state.display;

  $('stage-empty').hidden = true;
  setSessionControlsEnabled(true);
  renderCapabilities();

  if (session.capabilities.video) {
    await player.connect(serial);
  } else {
    // No video does not mean no session: screenshots and the accessibility tree
    // still work, so fall back to a still image rather than an empty pane.
    onPlayerStatus({ state: 'novideo', detail: session.notes.join(' / ') });
  }
  startHeartbeat(serial);
  if (session.capabilities.a11y) loadHierarchy();
  if (session.capabilities.secure_screen) {
    showBanner(session.notes.find((n) => n.includes('FLAG_SECURE')) || '画面受保护');
  }
}

function startHeartbeat(serial) {
  stopHeartbeat();
  // TTL is 60s; renewing every 20 tolerates one lost request without dropping
  // the lease.
  state.heartbeat = setInterval(async () => {
    if (!state.session) return;
    try {
      const renewed = await api.renewSession(serial, state.session.token);
      state.session = { ...state.session, ...renewed };
    } catch (error) {
      stopHeartbeat();
      const reason =
        error.status === 409 ? '设备被他人接管' : error.status === 410 ? '会话超时' : error.message;
      toast(`会话结束：${reason}`);
      teardown();
    }
  }, 20000);
}

function stopHeartbeat() {
  if (state.heartbeat) clearInterval(state.heartbeat);
  state.heartbeat = null;
}

async function release() {
  const serial = state.selected;
  const token = state.session?.token;
  teardown();
  if (serial && token) {
    try {
      await api.closeSession(serial, token);
    } catch {
      /* already gone */
    }
  }
  loadDevices();
}

function teardown() {
  stopHeartbeat();
  player.close();
  state.session = null;
  state.nodes = [];
  state.selection = null;
  state.lastPoint = null;
  setSessionControlsEnabled(false);
  $('stage-empty').hidden = false;
  $('session-state').textContent = '未连接';
  $('session-state').className = 'pill';
  $('stage-banner').hidden = true;
  clearOverlay();
}

function setSessionControlsEnabled(enabled) {
  for (const id of ['release', 'key-back', 'key-home', 'diagnose', 'load-hierarchy', 'save-template', 'add-tap-step']) {
    $(id).disabled = !enabled;
  }
  $('connect').disabled = enabled;
  $('run-script').disabled = !enabled || !state.script;
}

function renderCapabilities() {
  const caps = state.session?.capabilities;
  if (!caps) return;
  const parts = [
    caps.video ? '视频' : '无视频',
    caps.control ? '控制' : '无控制',
    caps.a11y ? '控件树' : '无控件树',
  ];
  const element = $('session-state');
  element.textContent = parts.join(' · ');
  element.className = `pill ${caps.video && caps.control ? 'pill--ok' : 'pill--warn'}`;
  if (state.session.notes?.length) {
    showBanner(state.session.notes.join('\n'));
  }
}

function onPlayerStatus(status) {
  const element = $('session-state');
  if (status.state === 'playing') {
    element.textContent = `播放中 ${status.fps ?? 0} fps`;
    element.className = 'pill pill--ok';
    if (player.frame) state.frame = player.frame;
    if (player.display) state.display = player.display;
  } else if (status.state === 'ready') {
    element.textContent = `已连接 ${status.detail || ''}`.trim();
    element.className = 'pill pill--ok';
  } else if (status.state === 'unsupported' || status.state === 'error' || status.state === 'novideo') {
    element.textContent = status.state === 'novideo' ? '无视频' : '视频错误';
    element.className = 'pill pill--warn';
    if (status.detail) showBanner(status.detail);
  }
  syncOverlaySize();
}

// ---------------------------------------------------------------- overlay

function syncOverlaySize() {
  const canvas = $('screen');
  const box = canvas.getBoundingClientRect();
  const host = overlay.parentElement.getBoundingClientRect();
  overlay.width = Math.max(1, Math.round(host.width));
  overlay.height = Math.max(1, Math.round(host.height));
  overlay.dataset.left = String(box.left - host.left);
  overlay.dataset.top = String(box.top - host.top);
  overlay.dataset.width = String(box.width);
  overlay.dataset.height = String(box.height);
}

function canvasPoint(event) {
  const box = $('screen').getBoundingClientRect();
  return { x: event.clientX - box.left, y: event.clientY - box.top };
}

function canvasSize() {
  const box = $('screen').getBoundingClientRect();
  return { width: box.width, height: box.height };
}

function clearOverlay() {
  overlayContext.clearRect(0, 0, overlay.width, overlay.height);
}

function drawOverlay(cursor) {
  clearOverlay();
  const left = Number(overlay.dataset.left || 0);
  const top = Number(overlay.dataset.top || 0);

  if (cursor) {
    overlayContext.strokeStyle = 'rgba(90,200,250,.75)';
    overlayContext.lineWidth = 1;
    overlayContext.beginPath();
    overlayContext.moveTo(left + cursor.x, top);
    overlayContext.lineTo(left + cursor.x, top + Number(overlay.dataset.height || 0));
    overlayContext.moveTo(left, top + cursor.y);
    overlayContext.lineTo(left + Number(overlay.dataset.width || 0), top + cursor.y);
    overlayContext.stroke();
  }
  if (state.selection) {
    const s = state.selection;
    overlayContext.strokeStyle = '#fbbf24';
    overlayContext.setLineDash([4, 3]);
    overlayContext.strokeRect(left + s.x, top + s.y, s.width, s.height);
    overlayContext.setLineDash([]);
  }
  const node = state.lastNode;
  if (node && state.frame && state.display) {
    // Highlight in frame space so the box tracks the video, not the window.
    const [bx, by, bw, bh] = node.bounds;
    const element = canvasSize();
    const toCss = (x, y) =>
      geo.frameToCss(
        { x: (x / state.display.width) * state.frame.width, y: (y / state.display.height) * state.frame.height },
        element,
        state.frame,
      );
    const a = toCss(bx, by);
    const b = toCss(bx + bw, by + bh);
    overlayContext.strokeStyle = '#4ade80';
    overlayContext.lineWidth = 2;
    overlayContext.strokeRect(left + a.x, top + a.y, b.x - a.x, b.y - a.y);
  }
}

// ---------------------------------------------------------------- pointer

let dragStart = null;

function pointerContext() {
  const element = canvasSize();
  return { element, frame: state.frame, display: state.display };
}

$('screen').addEventListener('mousemove', (event) => {
  if (!state.session || !state.frame || !state.display) return;
  const point = canvasPoint(event);
  const { element, frame, display } = pointerContext();
  const described = geo.describe(point, element, frame, display);
  const readout = $('readout');
  if (!described) {
    // In the letterbox: say so rather than showing a clamped value that looks
    // like a real coordinate.
    readout.hidden = false;
    readout.textContent = '（黑边区域，不对应任何设备像素）';
  } else {
    readout.hidden = false;
    readout.textContent =
      `css    ${described.css[0]}, ${described.css[1]}\n` +
      `frame  ${described.frame[0]}, ${described.frame[1]}\n` +
      `device ${described.device[0]}, ${described.device[1]}\n` +
      `norm   ${described.norm[0].toFixed(4)}, ${described.norm[1].toFixed(4)}`;
  }
  if (dragStart && $('mode-select').checked) {
    state.selection = geo.normaliseRect(dragStart, point);
  }
  drawOverlay(point);
});

$('screen').addEventListener('mouseleave', () => {
  $('readout').hidden = true;
  drawOverlay(null);
});

$('screen').addEventListener('mousedown', (event) => {
  if (!state.session) return;
  dragStart = canvasPoint(event);
  if ($('mode-select').checked) state.selection = null;
});

$('screen').addEventListener('mouseup', async (event) => {
  if (!state.session || !dragStart) return;
  const point = canvasPoint(event);
  const moved = Math.hypot(point.x - dragStart.x, point.y - dragStart.y);
  const start = dragStart;
  dragStart = null;

  if ($('mode-select').checked && moved > 4) {
    state.selection = geo.normaliseRect(start, point);
    drawOverlay(point);
    return;
  }
  await describePoint(point);
  if ($('mode-tap').checked && moved <= 4) await tapAt(point);
});

async function describePoint(point) {
  const { element, frame, display } = pointerContext();
  const described = geo.describe(point, element, frame, display);
  if (!described) return;
  state.lastPoint = described;
  try {
    // The server is authoritative for anything that gets recorded, and it is the
    // only side that can name the control under the cursor.
    const info = await api.point(state.selected, {
      token: state.session.token,
      x: point.x,
      y: point.y,
      space: 'css',
      element_width: Math.round(element.width),
      element_height: Math.round(element.height),
    });
    state.lastNode = info.node;
    renderPointInfo(described, info);
    drawOverlay(point);
  } catch (error) {
    renderPointInfo(described, { node: null, error: error.message });
  }
}

function renderPointInfo(described, info) {
  const rows = [
    ['css', described.css.join(', ')],
    ['frame', described.frame.join(', ')],
    ['device', described.device.join(', ')],
    ['norm', described.norm.map((v) => v.toFixed(4)).join(', ')],
  ];
  let html = rows.map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`).join('');
  if (info.node) {
    const n = info.node;
    html += '<div style="margin-top:8px"></div>';
    for (const [k, v] of [
      ['class', n.class?.split('.').pop()],
      ['resource-id', n.resource_id],
      ['text', n.text],
      ['desc', n.content_desc],
      ['clickable', String(n.clickable)],
    ]) {
      if (v) html += `<div><span>${k}</span><span>${escapeHtml(String(v))}</span></div>`;
    }
  } else if (info.a11y_available === false) {
    html += '<div class="note">无控件树（游戏 / Canvas 界面），请用模板或 OCR 定位</div>';
  }
  $('point-info').innerHTML = html;
}

async function tapAt(point) {
  const element = canvasSize();
  try {
    await api.tap(state.selected, {
      token: state.session.token,
      x: point.x,
      y: point.y,
      space: 'css',
      element_width: Math.round(element.width),
      element_height: Math.round(element.height),
    });
  } catch (error) {
    toast(`点击失败：${error.message}`);
  }
}

// ---------------------------------------------------------------- templates

$('save-template').onclick = async () => {
  if (!state.selection || !state.session) {
    toast('先用「框选取材」在画面上拖一个区域');
    return;
  }
  const name = $('template-name').value.trim();
  if (!name) {
    toast('给模板起个名字');
    return;
  }
  if (!state.project) {
    toast('先选择或新建一个项目');
    return;
  }
  const element = canvasSize();
  try {
    const result = await api.crop(state.selected, {
      token: state.session.token,
      project_id: state.project.id,
      name,
      x: state.selection.x,
      y: state.selection.y,
      width: state.selection.width,
      height: state.selection.height,
      space: 'css',
      element_width: Math.round(element.width),
      element_height: Math.round(element.height),
    });
    $('template-result').innerHTML =
      `<div><span>文件</span><span>${escapeHtml(result.file)}</span></div>` +
      `<div><span>像素</span><span>${result.width}×${result.height}</span></div>` +
      `<div><span>device</span><span>${result.device_rect.join(', ')}</span></div>` +
      `<div><span>中心 norm</span><span>${result.center_norm.map((v) => v.toFixed(4)).join(', ')}</span></div>` +
      (result.warning ? `<div class="note">${escapeHtml(result.warning)}</div>` : '');
  } catch (error) {
    toast(`取材失败：${error.message}`);
  }
};

// ---------------------------------------------------------------- hierarchy

async function loadHierarchy() {
  if (!state.session) return;
  try {
    const result = await api.hierarchy(state.selected, state.session.token);
    state.nodes = result.nodes || [];
    const host = $('node-list');
    host.textContent = '';
    if (!result.available) {
      host.innerHTML = `<li class="muted">${escapeHtml(result.reason || '控件树不可用')}</li>`;
      return;
    }
    for (const node of state.nodes) {
      const item = document.createElement('li');
      item.textContent = `${'  '.repeat(Math.min(node.depth, 6))}${node.label}`;
      item.title = `${node.class}\n${node.resource_id || ''}`;
      item.onclick = () => {
        state.lastNode = node;
        drawOverlay(null);
      };
      host.append(item);
    }
  } catch (error) {
    toast(`控件树读取失败：${error.message}`);
  }
}

$('load-hierarchy').onclick = loadHierarchy;

$('diagnose').onclick = async () => {
  try {
    const result = await api.diagnose(state.selected, state.session.token);
    if (result.capture_usable) {
      toast('画面正常，未检测到 FLAG_SECURE');
      $('stage-banner').hidden = true;
    } else {
      showBanner(result.message);
    }
  } catch (error) {
    toast(`诊断失败：${error.message}`);
  }
};

$('key-back').onclick = () => api.key(state.selected, state.session.token, 4).catch(() => {});
$('key-home').onclick = () => api.key(state.selected, state.session.token, 3).catch(() => {});

// ---------------------------------------------------------------- projects

async function loadProjects() {
  const projects = await api.projects().catch(() => []);
  const select = $('project-select');
  select.textContent = '';
  for (const project of projects) {
    const option = document.createElement('option');
    option.value = project.id;
    option.textContent = project.name;
    select.append(option);
  }
  if (projects.length) {
    state.project = projects.find((p) => p.id === select.value) || projects[0];
    select.value = state.project.id;
    renderScripts();
  } else {
    state.project = null;
    state.script = null;
    $('script-select').textContent = '';
    renderSteps();
  }
}

$('project-select').onchange = async () => {
  const projects = await api.projects().catch(() => []);
  state.project = projects.find((p) => p.id === $('project-select').value) || null;
  renderScripts();
};

$('new-project').onclick = async () => {
  const name = prompt('项目名称（例：电商下单）');
  if (!name) return;
  const id = prompt('项目 id（字母数字-_）', slug(name));
  if (!id) return;
  try {
    await api.createProject({ id, name });
    await loadProjects();
    $('project-select').value = id;
    $('project-select').onchange();
  } catch (error) {
    toast(`新建失败：${error.message}`);
  }
};

function renderScripts() {
  const select = $('script-select');
  select.textContent = '';
  const scripts = state.project?.scripts || [];
  for (const script of scripts) {
    const option = document.createElement('option');
    option.value = script.id;
    option.textContent = script.name;
    select.append(option);
  }
  state.script = scripts.find((s) => s.id === select.value) || scripts[0] || null;
  if (state.script) select.value = state.script.id;
  renderSteps();
  $('run-script').disabled = !state.session || !state.script;
}

$('script-select').onchange = () => {
  state.script = (state.project?.scripts || []).find((s) => s.id === $('script-select').value) || null;
  renderSteps();
};

$('new-script').onclick = async () => {
  if (!state.project) {
    toast('先新建一个项目');
    return;
  }
  const name = prompt('脚本名称（例：登录流程）');
  if (!name) return;
  const id = prompt('脚本 id（字母数字-_）', slug(name));
  if (!id) return;
  try {
    const project = await api.saveScript(state.project.id, { id, name, steps: [] });
    state.project = project;
    renderScripts();
    $('script-select').value = id;
    $('script-select').onchange();
  } catch (error) {
    toast(`新建失败：${error.message}`);
  }
};

const breakpoints = new Set();

function renderSteps(results = {}) {
  const host = $('step-list');
  host.textContent = '';
  const steps = state.script?.steps || [];
  if (!steps.length) {
    host.innerHTML = '<li class="muted">还没有步骤。在画面上点一个位置，然后按下面的按钮加入。</li>';
    return;
  }
  for (const step of steps) {
    const item = document.createElement('li');
    item.className = 'step';
    const result = results[step.id];
    if (result) item.dataset.status = result.status;
    const strategies = step.target
      ? Object.keys(step.target).filter((k) => ['a11y', 'template', 'ocr', 'coord'].includes(k) && step.target[k])
      : [];
    // A coordinate-only step is the one that breaks on the next device, so it is
    // flagged at authoring time rather than at 3am in CI.
    const coordOnly = strategies.length === 1 && strategies[0] === 'coord';
    item.innerHTML = `
      <span class="step__bp" role="button" aria-pressed="${breakpoints.has(step.id)}"></span>
      <span class="step__body">
        <span class="step__name">${escapeHtml(step.name)}</span>
        <span class="step__meta">${step.action}${strategies.length ? ' · ' + strategies.join('→') : ''}${
          coordOnly ? ' · 仅坐标' : ''
        }${result ? ` · ${result.status}${result.score != null ? ' ' + result.score.toFixed(3) : ''}` : ''}</span>
      </span>`;
    item.querySelector('.step__bp').onclick = async (event) => {
      event.stopPropagation();
      if (breakpoints.has(step.id)) breakpoints.delete(step.id);
      else breakpoints.add(step.id);
      renderSteps(results);
      if (state.runId) await api.run(state.runId).catch(() => {});
    };
    host.append(item);
  }
}

$('add-tap-step').onclick = async () => {
  if (!state.script || !state.lastPoint) {
    toast('先选一个脚本，并在画面上点一个位置');
    return;
  }
  const [nx, ny] = state.lastPoint.norm;
  const target = { strategy: ['a11y', 'coord'], coord: { x: nx, y: ny } };
  if (state.lastNode?.resource_id) {
    // Recording the accessibility selector alongside the coordinate is what makes
    // the step survive a different device.
    target.a11y = { resource_id: state.lastNode.resource_id };
  } else if (state.lastNode?.text) {
    target.a11y = { text: state.lastNode.text };
  } else {
    target.strategy = ['coord'];
  }
  if (state.display) {
    target.recorded_on = {
      width: state.display.width,
      height: state.display.height,
      rotation: state.session?.rotation ?? 0,
    };
  }
  const id = `s${(state.script.steps?.length || 0) + 1}`;
  const name = state.lastNode?.label || `点击 ${nx.toFixed(3)},${ny.toFixed(3)}`;
  const script = {
    ...state.script,
    steps: [...(state.script.steps || []), { id, name, action: 'tap', target }],
  };
  try {
    state.project = await api.saveScript(state.project.id, script);
    state.script = state.project.scripts.find((s) => s.id === script.id);
    renderSteps();
  } catch (error) {
    toast(`保存步骤失败：${error.message}`);
  }
};

// ---------------------------------------------------------------- runs

$('run-script').onclick = async () => {
  if (!state.session || !state.script) return;
  try {
    const run = await api.startRun({
      token: state.session.token,
      serial: state.selected,
      project_id: state.project.id,
      script_id: state.script.id,
      breakpoints: [...breakpoints],
    });
    state.runId = run.run_id;
    setRunControls(true);
    pollRun();
  } catch (error) {
    toast(`运行失败：${error.message}`);
  }
};

$('pause-run').onclick = () => state.runId && api.pauseRun(state.runId).catch(() => {});
$('resume-run').onclick = () => state.runId && api.resumeRun(state.runId).catch(() => {});
$('stop-run').onclick = () => state.runId && api.stopRun(state.runId).catch(() => {});

function setRunControls(running) {
  $('pause-run').disabled = !running;
  $('resume-run').disabled = !running;
  $('stop-run').disabled = !running;
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const run = await api.run(state.runId);
    const element = $('run-state');
    if (run.paused) {
      // The message matters: while paused the device is genuinely free to touch,
      // which is the whole point of the feature.
      element.textContent = `已暂停于 ${run.paused_at} — 设备可手动操作`;
      element.className = 'pill pill--warn';
    } else if (run.done) {
      const status = run.result?.status || 'done';
      element.textContent = `运行结束：${status}`;
      element.className = `pill ${status === 'ok' ? 'pill--ok' : 'pill--bad'}`;
      setRunControls(false);
      if (run.result) {
        renderSteps(Object.fromEntries(run.result.steps.map((s) => [s.step_id, s])));
      }
      state.runId = null;
      return;
    } else {
      element.textContent = '运行中';
      element.className = 'pill';
    }
    setTimeout(pollRun, 400);
  } catch {
    setRunControls(false);
    state.runId = null;
  }
}

// ---------------------------------------------------------------- export

async function loadExporters() {
  const exporters = await api.exporters().catch(() => []);
  const select = $('exporter-select');
  select.textContent = '';
  for (const exporter of exporters) {
    const option = document.createElement('option');
    option.value = exporter.name;
    option.textContent = exporter.name;
    option.title = exporter.description;
    select.append(option);
  }
}

$('export-script').onclick = async () => {
  if (!state.project || !state.script) return;
  try {
    const result = await api.exportScript(state.project.id, state.script.id, $('exporter-select').value);
    const warnings = result.warnings
      .map((w) => `<div class="note">${escapeHtml(`${w.step_id || '脚本'}: ${w.lost} → ${w.suggestion}`)}</div>`)
      .join('');
    $('export-result').innerHTML =
      `<div><span>${escapeHtml(result.filename)}</span><span>${result.lossless ? '无损' : `${result.warnings.length} 处降级`}</span></div>` +
      warnings;
    const blob = new Blob([result.content], { type: 'application/json' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = result.filename;
    link.click();
    URL.revokeObjectURL(link.href);
  } catch (error) {
    toast(`导出失败：${error.message}`);
  }
};

// ---------------------------------------------------------------- timeline

const KIND_LABEL = {
  run_start: '运行开始', run_end: '运行结束', step_start: '步骤开始',
  step_locate: '定位', step_action: '动作', step_assert: '断言',
  step_end: '步骤结束', step_paused: '暂停', step_resumed: '继续',
  capture: '截图', network: '网络', log: '日志', device: '设备',
};

function appendEvent(event) {
  const host = $('timeline');
  const row = document.createElement('div');
  row.className = `event event--${event.kind}`;
  row.innerHTML =
    `<span class="event__t">${event.t.toFixed(2)}s</span>` +
    `<span class="event__kind">${KIND_LABEL[event.kind] || event.kind}</span>` +
    `<span class="event__msg">${escapeHtml(
      [event.step_id, event.message].filter(Boolean).join('  '),
    )}</span>`;
  host.append(row);
  while (host.children.length > 600) host.firstChild.remove();
  if ($('follow-timeline').checked) host.scrollTop = host.scrollHeight;
}

$('clear-timeline').onclick = () => {
  $('timeline').textContent = '';
};

openSocket('/ws/timeline', {
  onJson: (message) => {
    if (message.type === 'history') message.events.forEach(appendEvent);
    else if (message.type === 'event') appendEvent(message.event);
  },
});

openSocket('/ws/events', {
  onJson: (message) => {
    if (message.type === 'snapshot') {
      state.devices = message.devices;
      renderDevices();
      return;
    }
    if (!message.device) return;
    const index = state.devices.findIndex((d) => d.serial === message.device.serial);
    if (message.type === 'removed') {
      if (index >= 0) state.devices.splice(index, 1);
      if (message.device.serial === state.selected) {
        toast('设备已断开');
        teardown();
      }
    } else if (index >= 0) state.devices[index] = message.device;
    else state.devices.push(message.device);
    renderDevices();
  },
});

// ---------------------------------------------------------------- misc

async function loadHealth() {
  try {
    const health = await api.health();
    const caps = health.capabilities || {};
    $('health').innerHTML =
      `<div><span>adb</span><span>${health.adb_available ? '就绪' : '未找到'}</span></div>` +
      `<div><span>scrcpy jar</span><span>${caps.scrcpy_jar ? '就绪' : '缺失'}</span></div>` +
      `<div><span>OCR</span><span>${caps.ocr ? '就绪' : '未安装'}</span></div>`;
  } catch {
    $('health').innerHTML = '<div class="muted">后端未响应</div>';
  }
}

function showBanner(text) {
  const banner = $('stage-banner');
  banner.hidden = false;
  banner.textContent = text;
}

function toast(text) {
  showBanner(text);
  setTimeout(() => {
    if ($('stage-banner').textContent === text) $('stage-banner').hidden = true;
  }, 6000);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function slug(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'item';
}

$('refresh-devices').onclick = loadDevices;
$('connect').onclick = () => connect(false);
$('release').onclick = release;
$('mode-tap').onchange = () => {
  if ($('mode-tap').checked) $('mode-select').checked = false;
};
$('mode-select').onchange = () => {
  if ($('mode-select').checked) $('mode-tap').checked = false;
};
window.addEventListener('resize', () => {
  syncOverlaySize();
  drawOverlay(null);
});
// No beacon on unload. sendBeacon can only POST, the release endpoint is a DELETE,
// and the earlier version fired at a /api/noop that never existed -- a 404 on every
// page close. The lease TTL is the mechanism for "the browser went away": it expires
// in 60s and the sweeper frees the device. An explicit close is the Release button.

loadHealth();
loadDevices();
loadProjects();
loadExporters();
syncOverlaySize();
