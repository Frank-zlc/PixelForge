/** Thin REST/WebSocket client. */

async function request(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return null;
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }
  if (!response.ok) {
    const detail =
      (payload && (payload.detail || payload.message)) || `${response.status}`;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  return payload;
}

async function requestImage(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text || `${response.status}`;
    try {
      const payload = JSON.parse(text);
      detail = payload.detail || payload.message || detail;
    } catch {
      /* The server may return plain text for an unexpected failure. */
    }
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  const blob = await response.blob();
  return {
    blob,
    width: Number(response.headers.get('X-PixelForge-Width')) || 0,
    height: Number(response.headers.get('X-PixelForge-Height')) || 0,
    lossless: response.headers.get('X-PixelForge-Lossless') === '1',
  };
}

export const api = {
  health: () => request('GET', '/api/health'),
  adbProbe: () => request('GET', '/api/adb/probe'),
  devices: () => request('GET', '/api/devices'),
  storage: (projectId) =>
    request('GET', `/api/storage${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''}`),

  openSession: (serial, owner, projectId) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/session`, {
      owner,
      ttl_s: 60,
      project_id: projectId || null,
    }),
  forceSession: (serial, owner, projectId) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/session`, {
      owner,
      ttl_s: 60,
      force: true,
      project_id: projectId || null,
    }),
  renewSession: (serial, token) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/session/renew`, { token }),
  closeSession: (serial, token) =>
    request('DELETE', `/api/devices/${encodeURIComponent(serial)}/session`, { token }),
  sessionStatus: (serial) =>
    request('GET', `/api/devices/${encodeURIComponent(serial)}/session/status`),
  listenerStatus: (serial) =>
    request('GET', `/api/devices/${encodeURIComponent(serial)}/listeners`),
  startListeners: (serial, token, projectId) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/listeners/start`, {
      token,
      project_id: projectId,
    }),
  stopListeners: (serial, token) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/listeners/stop`, { token }),

  connectWireless: (address) => request('POST', '/api/devices/connect', { address }),
  disconnectWireless: (address) => request('POST', '/api/devices/disconnect', { address }),
  enableTcpip: (serial, port = 5555) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/tcpip`, { port }),

  point: (serial, body) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/point`, body),
  capture: (serial, token, fresh = true) =>
    requestImage('POST', `/api/devices/${encodeURIComponent(serial)}/capture`, { token, fresh }),
  crop: (serial, body) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/crop`, body),
  cropImage: (serial, body) =>
    requestImage('POST', `/api/devices/${encodeURIComponent(serial)}/crop-image`, body),
  hierarchy: (serial, token) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/hierarchy`, { token }),
  diagnose: (serial, token) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/diagnose`, { token }),

  tap: (serial, body) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/tap`, body),
  swipe: (serial, body) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/swipe`, body),
  key: (serial, token, keycode) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/key`, { token, keycode }),
  text: (serial, token, text) =>
    request('POST', `/api/devices/${encodeURIComponent(serial)}/text`, { token, text }),

  projects: () => request('GET', '/api/projects'),
  createProject: (project) => request('POST', '/api/projects', project),
  saveScript: (projectId, script) =>
    request('PUT', `/api/projects/${projectId}/scripts/${script.id}`, script),
  templates: (projectId) => request('GET', `/api/projects/${projectId}/templates`),
  exporters: () => request('GET', '/api/exporters'),
  exportScript: (projectId, scriptId, exporter) =>
    request('POST', `/api/projects/${projectId}/scripts/${scriptId}/export`, { exporter }),

  startRun: (body) => request('POST', '/api/runs', body),
  run: (runId) => request('GET', `/api/runs/${runId}`),
  pauseRun: (runId) => request('POST', `/api/runs/${runId}/pause`),
  resumeRun: (runId) => request('POST', `/api/runs/${runId}/resume`),
  stopRun: (runId) => request('POST', `/api/runs/${runId}/stop`),
};

/** Reconnecting WebSocket. Device work involves unplugging things; drops are normal. */
export function openSocket(path, { onJson, onOpen, onClose } = {}) {
  let socket = null;
  let closed = false;
  let delay = 500;

  const connect = () => {
    if (closed) return;
    const url = new URL(path, location.href);
    url.protocol = url.protocol.replace('http', 'ws');
    socket = new WebSocket(url);
    socket.onopen = () => {
      delay = 500;
      onOpen?.(socket);
    };
    socket.onmessage = (event) => {
      if (typeof event.data === 'string') onJson?.(JSON.parse(event.data));
    };
    socket.onclose = () => {
      onClose?.();
      if (closed) return;
      setTimeout(connect, delay);
      delay = Math.min(delay * 2, 10000);
    };
    socket.onerror = () => socket?.close();
  };

  connect();
  return {
    send: (payload) => {
      if (!socket || socket.readyState !== WebSocket.OPEN) return false;
      socket.send(JSON.stringify(payload));
      return true;
    },
    close: () => {
      closed = true;
      socket?.close();
    },
  };
}
