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
  selection: null, // {x,y,width,height} in DEVICE pixels (survives resize)
  project: null,
  script: null,
  runId: null,
  heartbeat: null,
  lastCapture: null,
  listeners: [],
  controlSocket: null,
  controlReady: false,
  storage: null,
  saveDirectoryHandle: null,
  saveDirectoryRestored: false,
};

const player = new Player($('screen'), onPlayerStatus);
const overlay = $('overlay');
const overlayContext = overlay.getContext('2d');
let tapFeedback = null;

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
  const ready = Boolean(device) && device.state === 'device';
  $('connect').disabled = !ready;
  // A serial that already looks like host:port is wireless; switching it again
  // would be a no-op at best.
  $('wireless-tcpip').disabled = !ready || /:\d+$/.test(serial) || Boolean(state.session);
  if (ready && !/:\d+$/.test(serial) && !$('wireless-address').value.trim()) {
    $('wireless-result').innerHTML =
      '<div class="muted">USB 已连接。要改用无线，请点“开启 5555 并自动连接”。</div>';
  }
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
  state.listeners = session.listeners || [];
  state.display = session.display ? { width: session.display[0], height: session.display[1] } : null;
  state.frame = session.frame ? { width: session.frame[0], height: session.frame[1] } : state.display;

  $('stage-empty').hidden = true;
  setSessionControlsEnabled(true);
  renderListenerStatus();
  renderCapabilities();
  connectControl(serial, session);

  if (session.capabilities.video) {
    await player.connect(serial);
    // Do not leave the authoring canvas black when the first keyframe is late
    // or the browser advertises WebCodecs but cannot decode this stream.
    setTimeout(() => {
      if (state.session === session && player.decoded === 0) {
        refreshScreenshot({ quiet: true });
      }
    }, 1200);
  } else {
    // No video does not mean no session: screenshots and the accessibility tree
    // still work, so immediately fall back to a lossless still image.
    onPlayerStatus({ state: 'novideo', detail: session.notes.join(' / ') });
    await refreshScreenshot({ quiet: false });
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
      state.listeners = renewed.listeners || state.listeners;
      renderListenerStatus();
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
  closeControl();
  player.close();
  state.session = null;
  state.nodes = [];
  state.selection = null;
  state.lastPoint = null;
  state.lastCapture = null;
  state.listeners = [];
  setSessionControlsEnabled(false);
  $('stage-empty').hidden = false;
  $('session-state').textContent = '未连接';
  $('session-state').className = 'pill';
  $('stage-banner').hidden = true;
  clearOverlay();
  syncSelectionEditor();
  renderListenerStatus();
}

function closeControl() {
  if (state.controlReady && dragViaControl && dragCurrent) {
    sendLiveTouch('up', dragCurrent);
  }
  state.controlReady = false;
  state.controlSocket?.close();
  state.controlSocket = null;
}

function connectControl(serial, session) {
  closeControl();
  if (!session.capabilities.control || !session.capabilities.video) return;

  // The live channel mirrors a real finger (DOWN/MOVE/UP). REST remains the
  // fallback for ADB-only sessions and for the brief authentication window.
  const token = session.token;
  let control = null;
  control = openSocket(`/ws/control/${encodeURIComponent(serial)}`, {
    onOpen: (socket) => socket.send(JSON.stringify({ token })),
    onJson: (message) => {
      if (state.session?.token !== token || state.controlSocket !== control) return;
      if (message.type === 'ready') state.controlReady = true;
      if (message.type === 'error') {
        state.controlReady = false;
        toast(`实时控制不可用：${message.message}`);
      }
    },
    onClose: () => {
      if (state.session?.token === token && state.controlSocket === control) {
        state.controlReady = false;
      }
    },
  });
  state.controlSocket = control;
}

function setSessionControlsEnabled(enabled) {
  const caps = enabled ? state.session?.capabilities || {} : {};
  $('release').disabled = !enabled;
  for (const id of ['capture-screen', 'save-screen', 'copy-screen', 'diagnose']) {
    $(id).disabled = !enabled || !caps.screenshot;
  }
  for (const id of ['key-back', 'key-home', 'send-text', 'device-text']) {
    $(id).disabled = !enabled || !caps.control;
  }
  $('mode-tap').disabled = !enabled || !caps.control;
  $('mode-select').disabled = !enabled || !caps.screenshot;
  $('load-hierarchy').disabled = !enabled || !caps.a11y;
  syncStoragePanel();
  $('add-tap-step').disabled = !enabled || !state.script || !state.lastPoint;
  $('start-listeners').disabled = !enabled || !state.project;
  $('stop-listeners').disabled = !enabled;
  $('connect').disabled = enabled;
  $('run-script').disabled = !enabled || !state.script;
  const selectedIsUsb = Boolean(state.selected) && !/:\d+$/.test(state.selected);
  $('wireless-tcpip').disabled = enabled || !selectedIsUsb;
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

function renderListenerStatus() {
  const host = $('listener-status');
  if (!state.session) {
    host.innerHTML = '<div class="muted">获取设备后按项目配置自动启动</div>';
    return;
  }
  if (!state.listeners.length) {
    host.innerHTML = '<div class="muted">当前没有运行中的监听器</div>';
    return;
  }
  host.innerHTML = state.listeners.map((item) => {
    const stateText = item.running ? '运行中' : item.error ? '启动失败' : '已停止';
    const detail = item.error ? ` · ${item.error}` : '';
    return `<div><span>${escapeHtml(item.name)}</span><span class="${item.error ? 'note' : ''}">${escapeHtml(stateText + detail)}</span></div>`;
  }).join('');
}

$('start-listeners').onclick = async () => {
  if (!state.session || !state.project) return;
  try {
    state.listeners = await api.startListeners(
      state.selected,
      state.session.token,
      state.project.id,
    );
    renderListenerStatus();
  } catch (error) {
    toast(`启动监听失败：${error.message}`);
  }
};

$('stop-listeners').onclick = async () => {
  if (!state.session) return;
  try {
    await api.stopListeners(state.selected, state.session.token);
    state.listeners = [];
    renderListenerStatus();
  } catch (error) {
    toast(`停止监听失败：${error.message}`);
  }
};

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
    if (state.session && player.decoded === 0) refreshScreenshot({ quiet: true });
  } else if (status.state === 'closed' && state.session) {
    element.textContent = '视频已断开 · 静态截图';
    element.className = 'pill pill--warn';
    refreshScreenshot({ quiet: true });
  }
  syncOverlaySize();
}

// ---------------------------------------------------------------- screenshots

async function fetchScreenshot() {
  if (!state.session || !state.selected) throw new Error('请先获取设备');
  const capture = await api.capture(state.selected, state.session.token, true);
  state.lastCapture = capture;
  return capture;
}

async function drawScreenshot(capture) {
  const bitmap = await createImageBitmap(capture.blob);
  const width = capture.width || bitmap.width;
  const height = capture.height || bitmap.height;
  const canvas = $('screen');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d', { alpha: false }).drawImage(bitmap, 0, 0, width, height);
  bitmap.close();
  state.frame = { width, height };
  state.display = { width, height };
  $('stage-empty').hidden = true;
  const element = $('session-state');
  element.textContent = `静态截图 ${width}×${height}`;
  element.className = 'pill pill--ok';
  syncOverlaySize();
  drawOverlay(null);
}

async function refreshScreenshot({ quiet = false } = {}) {
  try {
    const capture = await fetchScreenshot();
    await drawScreenshot(capture);
    return capture;
  } catch (error) {
    if (!quiet) toast(`获取截图失败：${error.message}`);
    return null;
  }
}

function refreshAfterControl() {
  if (state.session && !state.session.capabilities.video) {
    setTimeout(() => refreshScreenshot({ quiet: true }), 250);
  }
}

$('capture-screen').onclick = () => refreshScreenshot();

$('save-screen').onclick = async () => {
  const capture = await refreshScreenshot();
  if (!capture) return;
  const url = URL.createObjectURL(capture.blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `pixelforge-${state.selected}-${new Date().toISOString().replace(/[:.]/g, '-')}.png`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
};

$('copy-screen').onclick = async () => {
  try {
    const capture = await fetchScreenshot();
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': capture.blob })]);
    toast('原图已复制到剪贴板');
  } catch (error) {
    toast(`复制原图失败：${error.message}`);
  }
};

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

function canvasPoint(event, { clamp = false } = {}) {
  const box = $('screen').getBoundingClientRect();
  let x = event.clientX - box.left;
  let y = event.clientY - box.top;
  const inside = x >= 0 && y >= 0 && x <= box.width && y <= box.height;
  if (!inside && !clamp) return null;
  if (clamp) {
    x = Math.min(Math.max(x, 0), box.width);
    y = Math.min(Math.max(y, 0), box.height);
  }
  return { x, y };
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
  if (dragStart && dragCurrent && $('mode-tap').checked) {
    const startX = left + dragStart.x;
    const startY = top + dragStart.y;
    const endX = left + dragCurrent.x;
    const endY = top + dragCurrent.y;
    overlayContext.strokeStyle = '#4ade80';
    overlayContext.fillStyle = '#4ade80';
    overlayContext.lineWidth = 3;
    overlayContext.beginPath();
    overlayContext.arc(startX, startY, 8, 0, Math.PI * 2);
    overlayContext.moveTo(startX, startY);
    overlayContext.lineTo(endX, endY);
    overlayContext.stroke();
    overlayContext.beginPath();
    overlayContext.arc(endX, endY, 5, 0, Math.PI * 2);
    overlayContext.fill();
  }
  if (tapFeedback) {
    overlayContext.strokeStyle = '#4ade80';
    overlayContext.lineWidth = 3;
    overlayContext.beginPath();
    overlayContext.arc(left + tapFeedback.x, top + tapFeedback.y, 12, 0, Math.PI * 2);
    overlayContext.stroke();
  }
  if (state.selection) {
    const s = geo.deviceRectToCss(state.selection, canvasSize(), state.frame, state.display);
    overlayContext.strokeStyle = '#fbbf24';
    overlayContext.fillStyle = 'rgba(251,191,36,.10)';
    overlayContext.setLineDash([4, 3]);
    overlayContext.fillRect(left + s.x, top + s.y, s.width, s.height);
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
let activePointer = null;
let dragCurrent = null;
let dragStartedAt = 0;
let dragViaControl = false;
let lastMoveSentAt = 0;

function pointerContext() {
  const element = canvasSize();
  return { element, frame: state.frame, display: state.display };
}

const selectionInputs = [
  ['selection-x', 'x'],
  ['selection-y', 'y'],
  ['selection-width', 'width'],
  ['selection-height', 'height'],
];

function syncSelectionEditor() {
  const selection = state.selection;
  for (const [id, key] of selectionInputs) {
    $(id).disabled = !selection;
    $(id).value = selection ? String(selection[key]) : '';
  }
  $('apply-selection').disabled = !selection;
  $('clear-selection').disabled = !selection;
  $('selection-space').textContent = selection
    ? `设备像素：${selection.x},${selection.y} · ${selection.width}×${selection.height}`
    : '设备像素：未框选';
  syncStoragePanel();
}

function setSelectionFromCss(rect, { render = true } = {}) {
  if (!state.frame || !state.display) return;
  const selection = geo.cssRectToDevice(
    rect,
    canvasSize(),
    state.frame,
    state.display,
  );
  if (!selection) return;
  state.selection = selection;
  syncSelectionEditor();
  if (render) drawOverlay(null);
}

function applySelectionInputs() {
  if (!state.display) return;
  const values = Object.fromEntries(selectionInputs.map(([id, key]) => [key, Number($(id).value)]));
  if (!Object.values(values).every(Number.isFinite) || values.width < 1 || values.height < 1) {
    toast('框选参数必须是有效数字，宽和高至少为 1');
    return;
  }
  const x = Math.max(0, Math.min(Math.round(values.x), state.display.width - 1));
  const y = Math.max(0, Math.min(Math.round(values.y), state.display.height - 1));
  const right = Math.min(state.display.width, x + Math.round(values.width));
  const bottom = Math.min(state.display.height, y + Math.round(values.height));
  state.selection = { x, y, width: Math.max(1, right - x), height: Math.max(1, bottom - y) };
  syncSelectionEditor();
  drawOverlay(null);
}

$('apply-selection').onclick = applySelectionInputs;
$('clear-selection').onclick = () => {
  state.selection = null;
  syncSelectionEditor();
  drawOverlay(null);
};
for (const [id] of selectionInputs) {
  $(id).addEventListener('keydown', (event) => {
    if (event.key === 'Enter') applySelectionInputs();
  });
}

const stageBody = document.querySelector('.stage-body');

function sendLiveTouch(type, point) {
  if (!state.controlReady || !state.controlSocket || !state.frame) return false;
  const framePoint = geo.cssToFrame(point, canvasSize(), state.frame);
  if (!framePoint) return false;
  return state.controlSocket.send({
    type,
    x: framePoint.x,
    y: framePoint.y,
    pointer: -2,
  });
}

function cancelPointerGesture() {
  if (dragViaControl && dragCurrent) sendLiveTouch('up', dragCurrent);
  dragStart = null;
  dragCurrent = null;
  dragViaControl = false;
  activePointer = null;
  drawOverlay(null);
}

stageBody.addEventListener('pointermove', (event) => {
  if (!state.session || !state.frame || !state.display) return;
  const point = canvasPoint(event, { clamp: dragStart !== null });
  if (!point) {
    $('readout').hidden = true;
    drawOverlay(null);
    return;
  }
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
    setSelectionFromCss(geo.normaliseRect(dragStart, point), { render: false });
  } else if (dragStart && $('mode-tap').checked) {
    dragCurrent = point;
    const now = performance.now();
    // Pointer events can exceed the phone refresh rate. Capping MOVE traffic
    // keeps the control channel responsive while remaining visually smooth.
    if (dragViaControl && now - lastMoveSentAt >= 16) {
      sendLiveTouch('move', point);
      lastMoveSentAt = now;
    }
  }
  drawOverlay(point);
});

stageBody.addEventListener('pointerleave', () => {
  if (dragStart) return;
  $('readout').hidden = true;
  drawOverlay(null);
});

stageBody.addEventListener('pointerdown', (event) => {
  if (!state.session || event.button !== 0) return;
  const point = canvasPoint(event);
  if (!point) return;
  event.preventDefault();
  dragStart = point;
  dragCurrent = point;
  dragStartedAt = performance.now();
  activePointer = event.pointerId;
  stageBody.setPointerCapture?.(event.pointerId);
  if ($('mode-select').checked) {
    state.selection = null;
    syncSelectionEditor();
  } else if ($('mode-tap').checked) {
    dragViaControl = sendLiveTouch('down', point);
    lastMoveSentAt = dragStartedAt;
  }
});

stageBody.addEventListener('pointerup', async (event) => {
  if (!state.session || !dragStart || event.pointerId !== activePointer) return;
  const point = canvasPoint(event, { clamp: true });
  const moved = Math.hypot(point.x - dragStart.x, point.y - dragStart.y);
  const start = dragStart;
  const durationMs = Math.max(50, Math.min(10000, Math.round(performance.now() - dragStartedAt)));
  const usedLiveControl = dragViaControl;
  dragStart = null;
  dragCurrent = null;
  dragViaControl = false;
  activePointer = null;
  stageBody.releasePointerCapture?.(event.pointerId);

  if ($('mode-select').checked) {
    if (moved > 4) {
      setSelectionFromCss(geo.normaliseRect(start, point));
    } else {
      // Selection-mode clicks still give visible feedback: seed a small box
      // that can then be sized precisely in the inspector.
      const width = Math.min(80, canvasSize().width * 0.15);
      const height = Math.min(80, canvasSize().height * 0.15);
      setSelectionFromCss({
        x: Math.max(0, point.x - width / 2),
        y: Math.max(0, point.y - height / 2),
        width,
        height,
      });
    }
    drawOverlay(point);
    return;
  }
  if (!$('mode-tap').checked) return;
  if (usedLiveControl) {
    // DOWN+UP is already the tap. Calling REST here too would double-click.
    const released = sendLiveTouch('up', point);
    await describePoint(point);
    if (!released) {
      // If the live socket dropped mid-gesture, a complete REST swipe also
      // guarantees the virtual finger is not left pressed on the phone.
      if (moved <= 4) await tapAt(point);
      else await swipeAt(start, point, durationMs);
      return;
    }
    showGestureFeedback(start, point, moved <= 4 ? '点击已发送' : `拖拽已发送 · ${durationMs} ms`);
    return;
  }
  await describePoint(point);
  if (moved <= 4) await tapAt(point);
  else await swipeAt(start, point, durationMs);
});

stageBody.addEventListener('pointercancel', () => {
  cancelPointerGesture();
});

window.addEventListener('blur', cancelPointerGesture);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) cancelPointerGesture();
});

async function describePoint(point) {
  const { element, frame, display } = pointerContext();
  const described = geo.describe(point, element, frame, display);
  if (!described) return;
  state.lastPoint = described;
  $('add-tap-step').disabled = !state.script;
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
  } else if (info.error) {
    html += `<div class="note">坐标读取失败：${escapeHtml(info.error)}</div>`;
  }
  $('point-info').innerHTML = html;
}

async function tapAt(point) {
  const element = canvasSize();
  tapFeedback = point;
  drawOverlay(point);
  setTimeout(() => {
    if (tapFeedback === point) {
      tapFeedback = null;
      drawOverlay(null);
    }
  }, 450);
  try {
    await api.tap(state.selected, {
      token: state.session.token,
      x: point.x,
      y: point.y,
      space: 'css',
      element_width: Math.round(element.width),
      element_height: Math.round(element.height),
    });
    $('point-info').insertAdjacentHTML('beforeend', '<div><span>动作</span><span>点击已发送</span></div>');
    refreshAfterControl();
  } catch (error) {
    toast(`点击失败：${error.message}`);
  }
}

function showGestureFeedback(start, end, label) {
  tapFeedback = end;
  drawOverlay(end);
  setTimeout(() => {
    if (tapFeedback === end) {
      tapFeedback = null;
      drawOverlay(null);
    }
  }, 450);
  $('point-info').insertAdjacentHTML(
    'beforeend',
    `<div><span>动作</span><span>${escapeHtml(label)}</span></div>` +
      `<div><span>路径</span><span>${Math.round(start.x)}, ${Math.round(start.y)} → ` +
      `${Math.round(end.x)}, ${Math.round(end.y)}</span></div>`,
  );
}

async function swipeAt(start, end, durationMs) {
  const element = canvasSize();
  try {
    await api.swipe(state.selected, {
      token: state.session.token,
      x1: start.x,
      y1: start.y,
      x2: end.x,
      y2: end.y,
      duration_ms: durationMs,
      space: 'css',
      element_width: Math.round(element.width),
      element_height: Math.round(element.height),
    });
    showGestureFeedback(start, end, `滑动已发送 · ${durationMs} ms`);
    refreshAfterControl();
  } catch (error) {
    toast(`拖拽失败：${error.message}`);
  }
}

// ---------------------------------------------------------------- templates

const SAVE_DIRECTORY_DB = 'pixelforge-preferences';
const SAVE_DIRECTORY_STORE = 'handles';

function openPreferenceDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(SAVE_DIRECTORY_DB, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(SAVE_DIRECTORY_STORE)) {
        request.result.createObjectStore(SAVE_DIRECTORY_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function storedDirectoryHandle(value) {
  const db = await openPreferenceDb();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(SAVE_DIRECTORY_STORE, 'readwrite');
    const store = transaction.objectStore(SAVE_DIRECTORY_STORE);
    const request = value === undefined
      ? store.get('crop-directory')
      : value === null
        ? store.delete('crop-directory')
        : store.put(value, 'crop-directory');
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(request.error);
    transaction.oncomplete = () => db.close();
  });
}

async function restoreSaveDirectory() {
  if (state.saveDirectoryRestored) return;
  state.saveDirectoryRestored = true;
  try {
    const handle = await storedDirectoryHandle(undefined);
    if (handle?.kind === 'directory') state.saveDirectoryHandle = handle;
  } catch {
    // IndexedDB or persisted file handles may be unavailable in private mode.
  }
}

async function loadStorage() {
  await restoreSaveDirectory();
  try {
    state.storage = await api.storage();
  } catch (error) {
    state.storage = null;
    $('template-result').innerHTML =
      `<div class="note">保存目录读取失败：${escapeHtml(error.message)}</div>`;
  }
  syncStoragePanel();
}

function syncStoragePanel() {
  const handle = state.saveDirectoryHandle;
  const path = handle ? `系统目录：${handle.name}` : state.storage?.asset_root;
  $('save-path').value = path || '';
  $('save-path').title = path || '保存目录不可用';
  $('reset-save-directory').disabled = !handle;
  const pickerSupported = typeof window.showDirectoryPicker === 'function';
  $('choose-save-directory').disabled = !pickerSupported;
  $('choose-save-directory').title = pickerSupported
    ? '打开系统目录选择器，并记住这个位置'
    : '当前浏览器不支持系统目录选择器，请使用 Chrome 或 Edge';
  const canSave = Boolean(
    state.selection &&
    state.session?.capabilities?.screenshot &&
    path,
  );
  $('save-template').disabled = !canSave;
}

$('choose-save-directory').onclick = async () => {
  if (typeof window.showDirectoryPicker !== 'function') {
    toast('当前浏览器不支持系统目录选择器，请使用 Chrome 或 Edge');
    return;
  }
  try {
    const options = { id: 'pixelforge-crops', mode: 'readwrite' };
    if (state.saveDirectoryHandle) options.startIn = state.saveDirectoryHandle;
    const handle = await window.showDirectoryPicker(options);
    state.saveDirectoryHandle = handle;
    await storedDirectoryHandle(handle).catch(() => null);
    syncStoragePanel();
  } catch (error) {
    if (error.name !== 'AbortError') toast(`选择目录失败：${error.message}`);
  }
};

$('reset-save-directory').onclick = async () => {
  state.saveDirectoryHandle = null;
  await storedDirectoryHandle(null).catch(() => null);
  syncStoragePanel();
};

function automaticCropName() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, '0');
  const stamp =
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const random = crypto.getRandomValues(new Uint16Array(1))[0].toString(16).padStart(4, '0');
  return `crop_${stamp}_${random}`;
}

function cropFileStem() {
  let name = $('template-name').value.trim();
  if (name.toLowerCase().endsWith('.png')) name = name.slice(0, -4).trim();
  if (!name) return automaticCropName();
  if (/[<>:"/\\|?*\u0000-\u001f]/.test(name) || name === '.' || name === '..') {
    throw new Error('文件名不能包含 < > : " / \\ | ? *');
  }
  return name;
}

function cropRequestBody(selection) {
  return {
    token: state.session.token,
    x: selection.x,
    y: selection.y,
    width: selection.width,
    height: selection.height,
    space: 'device',
  };
}

async function saveToSystemDirectory(handle, name, selection) {
  const permission = await handle.queryPermission({ mode: 'readwrite' });
  if (permission !== 'granted') {
    const granted = await handle.requestPermission({ mode: 'readwrite' });
    if (granted !== 'granted') throw new Error('没有所选目录的写入权限');
  }
  const capture = await api.cropImage(state.selected, cropRequestBody(selection));
  const fileName = `${name}.png`;
  const file = await handle.getFileHandle(fileName, { create: true });
  const writer = await file.createWritable();
  try {
    await writer.write(capture.blob);
  } finally {
    await writer.close();
  }
  return {
    path: `系统目录/${handle.name}/${fileName}`,
    width: capture.width || selection.width,
    height: capture.height || selection.height,
  };
}

$('save-template').onclick = async () => {
  if (!state.selection || !state.session) {
    toast('先用「框选取材」在画面上拖一个区域');
    return;
  }
  try {
    const name = cropFileStem();
    const selection = { ...state.selection };
    const handle = state.saveDirectoryHandle;
    const saved = handle
      ? await saveToSystemDirectory(handle, name, selection)
      : await api.crop(state.selected, {
          ...cropRequestBody(selection),
          project_id: null,
          folder: null,
          name,
        });
    const path = handle ? saved.path : `${saved.directory}/${saved.file}`;
    $('template-result').innerHTML =
      `<div><span>已保存</span><span>${escapeHtml(path)}</span></div>` +
      `<div><span>文件名</span><span>${escapeHtml(`${name}.png`)}</span></div>` +
      `<div><span>像素</span><span>${saved.width}×${saved.height}</span></div>` +
      `<div><span>device</span><span>${selection.x}, ${selection.y}, ` +
      `${selection.width}, ${selection.height}</span></div>` +
      (saved.warning ? `<div class="note">${escapeHtml(saved.warning)}</div>` : '');
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

async function sendKey(keycode) {
  try {
    await api.key(state.selected, state.session.token, keycode);
    refreshAfterControl();
  } catch (error) {
    toast(`按键失败：${error.message}`);
  }
}

$('key-back').onclick = () => sendKey(4);
$('key-home').onclick = () => sendKey(3);

async function sendDeviceText() {
  const value = $('device-text').value;
  if (!value) {
    toast('请输入要发送的文字');
    return;
  }
  try {
    await api.text(state.selected, state.session.token, value);
    refreshAfterControl();
  } catch (error) {
    toast(`发送文字失败：${error.message}`);
  }
}

$('send-text').onclick = sendDeviceText;
$('device-text').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') sendDeviceText();
});

// ---------------------------------------------------------------- projects

async function loadProjects() {
  const projects = await api.projects().catch(() => []);
  const select = $('project-select');
  select.textContent = '';
  select.disabled = projects.length === 0;
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
    select.innerHTML = '<option>未创建项目</option>';
    $('script-select').innerHTML = '<option>未创建脚本</option>';
    $('script-select').disabled = true;
    $('export-script').disabled = true;
    renderSteps();
  }
  $('new-script').disabled = !state.project;
  syncSelectionEditor();
  await loadStorage();
}

$('project-select').onchange = async () => {
  const projects = await api.projects().catch(() => []);
  state.project = projects.find((p) => p.id === $('project-select').value) || null;
  renderScripts();
  setSessionControlsEnabled(Boolean(state.session));
  await loadStorage();
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
  select.disabled = scripts.length === 0;
  for (const script of scripts) {
    const option = document.createElement('option');
    option.value = script.id;
    option.textContent = script.name;
    select.append(option);
  }
  state.script = scripts.find((s) => s.id === select.value) || scripts[0] || null;
  if (state.script) select.value = state.script.id;
  else select.innerHTML = '<option>未创建脚本</option>';
  renderSteps();
  $('run-script').disabled = !state.session || !state.script;
  $('add-tap-step').disabled = !state.session || !state.script || !state.lastPoint;
  $('export-script').disabled = !state.script;
  $('new-script').disabled = !state.project;
  syncSelectionEditor();
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
      `<div><span>设备源</span><span>${escapeHtml(health.device_provider || 'local-adb')}</span></div>` +
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

// ---------------------------------------------------------------- wireless

function wirelessFailureMessage(address, error) {
  if (/connection refused/i.test(error.message)) {
    return `${address} 可以到达，但手机没有开放 ADB 端口。` +
      '请保持 USB 调试连接，先点“开启 5555 并自动连接”。';
  }
  if (/timed out|timeout|no route|unreachable/i.test(error.message)) {
    return `无法到达 ${address}。请确认电脑和手机在同一局域网，且手机 IP 没有变化。`;
  }
  return error.message;
}

async function connectWirelessAddress(address, { quiet = false } = {}) {
  if (!quiet) $('wireless-result').innerHTML = '<div class="muted">连接中…</div>';
  try {
    const result = await api.connectWireless(address);
    // The tracker picks the device up on its own; no refresh needed, but doing
    // it makes the list update feel immediate rather than a beat later.
    $('wireless-result').innerHTML = `<div><span>已连接</span><span>${escapeHtml(result.address)}</span></div>`;
    loadDevices();
    return true;
  } catch (error) {
    if (!quiet) {
      $('wireless-result').innerHTML =
        `<div class="note">${escapeHtml(wirelessFailureMessage(address, error))}</div>`;
    }
    return false;
  }
}

$('wireless-connect').onclick = async () => {
  const address = $('wireless-address').value.trim();
  if (!address) {
    toast('填一个 host:port，例如 192.168.2.5:5555');
    return;
  }
  await connectWirelessAddress(address);
};

$('wireless-tcpip').onclick = async () => {
  if (!state.selected) return;
  if (state.session) {
    toast('先释放当前设备，再切换无线连接；重启 adbd 会中断当前会话');
    return;
  }
  $('wireless-result').innerHTML = '<div class="muted">正在读取手机 IP 并开启 5555 端口…</div>';
  try {
    const result = await api.enableTcpip(state.selected);
    if (result.suggested_address) {
      // Pre-fill so the address does not have to be hunted down in Settings.
      $('wireless-address').value = result.suggested_address;
      $('wireless-result').innerHTML =
        `<div><span>端口已开启</span><span>${escapeHtml(result.suggested_address)}</span></div>` +
        '<div class="muted" style="font-size:12px">等待手机 adbd 重启并自动连接…</div>';
      let connected = false;
      for (let attempt = 0; attempt < 6 && !connected; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        connected = await connectWirelessAddress(result.suggested_address, { quiet: true });
      }
      if (connected) {
        $('wireless-result').innerHTML =
          `<div><span>无线已连接</span><span>${escapeHtml(result.suggested_address)}</span></div>` +
          '<div class="muted" style="font-size:12px">现在可以拔掉数据线。</div>';
      } else {
        $('wireless-result').innerHTML =
          `<div class="note">端口已开启，但自动连接 ${escapeHtml(result.suggested_address)} 失败。` +
          '保持数据线连接，稍等几秒后点“直接连接”。</div>';
      }
    } else {
      $('wireless-result').innerHTML =
        `<div class="note">已切到 TCP 模式（端口 ${result.port}），但读不到手机的 wlan0 地址。` +
        '请在「设置 → 关于手机 → 状态」里找到 IP 后手动填入。</div>';
    }
  } catch (error) {
    $('wireless-result').innerHTML = `<div class="note">${escapeHtml(error.message)}</div>`;
  }
};

$('refresh-devices').onclick = loadDevices;
$('connect').onclick = () => connect(false);
$('release').onclick = release;
$('mode-tap').onchange = () => { $('screen').style.cursor = 'crosshair'; };
$('mode-select').onchange = () => { $('screen').style.cursor = 'cell'; };
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
