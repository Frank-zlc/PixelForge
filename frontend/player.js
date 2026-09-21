/**
 * H.264 playback with WebCodecs.
 *
 * The backend forwards scrcpy's packets untouched and this decodes them on the
 * GPU. That split is what keeps the server's CPU cost per device near zero, which
 * is what makes running several phones from one process possible -- decoding even
 * two 1080p streams in Python would saturate the same event loop that serves adb,
 * control and the API.
 *
 * Each frame arrives with a one-byte prefix: bit 0 = parameter sets, bit 1 =
 * keyframe. The server replays the retained parameter sets and newest keyframe to
 * every new subscriber, because a decoder handed only mid-stream deltas produces
 * nothing at all -- not a glitch, a permanently black canvas.
 */

const FLAG_CONFIG = 0x01;
const FLAG_KEYFRAME = 0x02;

export class Player {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {(status: {state: string, detail?: string, fps?: number}) => void} onStatus
   */
  constructor(canvas, onStatus) {
    this.canvas = canvas;
    this.context = canvas.getContext('2d', { alpha: false, desynchronized: true });
    this.onStatus = onStatus;
    this.decoder = null;
    this.socket = null;
    this.frame = null;
    this.display = null;
    this.rotation = 0;
    this.decoded = 0;
    this.dropped = 0;
    this._fpsWindow = [];
    // Nothing can be decoded before the parameter sets arrive, so deltas that
    // precede them are discarded rather than queued into an error.
    this._configured = false;
  }

  get supported() {
    return typeof window.VideoDecoder === 'function';
  }

  async connect(serial) {
    if (!this.supported) {
      this.onStatus({
        state: 'unsupported',
        detail:
          'This browser has no WebCodecs VideoDecoder. Use Chrome, Edge, or Safari 16.4+.',
      });
      return;
    }
    this.close();
    const url = new URL(`/ws/screen/${encodeURIComponent(serial)}`, location.href);
    url.protocol = url.protocol.replace('http', 'ws');
    this.socket = new WebSocket(url);
    this.socket.binaryType = 'arraybuffer';
    this.onStatus({ state: 'connecting' });

    this.socket.onmessage = (event) => {
      if (typeof event.data === 'string') {
        this._onControlMessage(JSON.parse(event.data));
      } else {
        this._onPacket(new Uint8Array(event.data));
      }
    };
    this.socket.onclose = () => this.onStatus({ state: 'closed' });
    this.socket.onerror = () =>
      this.onStatus({ state: 'error', detail: 'video socket failed' });
  }

  _onControlMessage(message) {
    if (message.type === 'error') {
      this.onStatus({ state: 'error', detail: message.message, notes: message.notes });
      return;
    }
    if (message.type !== 'config') return;
    this.frame = message.frame ? { width: message.frame[0], height: message.frame[1] } : null;
    this.display = message.display
      ? { width: message.display[0], height: message.display[1] }
      : null;
    this.rotation = message.rotation ?? 0;
    if (this.frame) {
      this.canvas.width = this.frame.width;
      this.canvas.height = this.frame.height;
    }
    this._startDecoder(message.codec || 'avc1.42E01E');
    this.onStatus({ state: 'ready', detail: message.device_name || '' });
  }

  _startDecoder(codec) {
    this._configured = false;
    this.decoder = new VideoDecoder({
      output: (frame) => this._draw(frame),
      error: (error) => {
        // A decoder that has errored cannot recover, so surface it rather than
        // leaving a frozen canvas that looks like a stalled device.
        this.onStatus({ state: 'error', detail: `decoder: ${error.message}` });
      },
    });
    // No description: scrcpy sends Annex-B with in-band parameter sets, which is
    // what the browser expects when `description` is omitted.
    this.decoder.configure({ codec, optimizeForLatency: true });
  }

  _onPacket(bytes) {
    if (!this.decoder || this.decoder.state === 'closed') return;
    const flags = bytes[0];
    const payload = bytes.subarray(1);
    const isConfig = (flags & FLAG_CONFIG) !== 0;
    const isKey = (flags & FLAG_KEYFRAME) !== 0;

    if (isConfig) this._configured = true;
    if (!this._configured && !isKey) {
      this.dropped += 1;
      return;
    }
    if (isKey) this._configured = true;

    try {
      this.decoder.decode(
        new EncodedVideoChunk({
          type: isKey || isConfig ? 'key' : 'delta',
          timestamp: performance.now() * 1000,
          data: payload,
        }),
      );
    } catch (error) {
      this.dropped += 1;
    }
  }

  _draw(frame) {
    if (this.canvas.width !== frame.displayWidth) {
      this.canvas.width = frame.displayWidth;
      this.canvas.height = frame.displayHeight;
      this.frame = { width: frame.displayWidth, height: frame.displayHeight };
    }
    this.context.drawImage(frame, 0, 0);
    frame.close();
    this.decoded += 1;

    const now = performance.now();
    this._fpsWindow.push(now);
    while (this._fpsWindow.length && now - this._fpsWindow[0] > 1000) this._fpsWindow.shift();
    if (this.decoded % 15 === 0) {
      this.onStatus({ state: 'playing', fps: this._fpsWindow.length });
    }
  }

  close() {
    if (this.socket) {
      this.socket.onclose = null;
      this.socket.close();
      this.socket = null;
    }
    if (this.decoder && this.decoder.state !== 'closed') {
      try {
        this.decoder.close();
      } catch {
        /* already closing */
      }
    }
    this.decoder = null;
    this._configured = false;
  }
}
