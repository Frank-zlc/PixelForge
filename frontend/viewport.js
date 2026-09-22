/**
 * Stage viewport -- how the device frame is placed inside the stage area.
 *
 * The canvas bitmap is the encoded frame. The canvas is absolutely positioned at
 * the wrap's top-left and moved with `transform: translate(pan) scale(zoom)` and
 * `transform-origin: 0 0`, so its rendered box is exactly:
 *
 *     left = panX,  top = panY,  width = frame.width * zoom
 *
 * That choice is what keeps `geometry.js` untouched: every coordinate helper in
 * this app measures the canvas with `getBoundingClientRect()`, and a bounding
 * rect already includes CSS transforms. Zoom and pan therefore cost nothing in
 * correctness -- a crop framed at 400% lands on the same device pixels it would
 * have at 30%.
 *
 * `zoom` is CSS px per *frame* px, because that is literally what the transform
 * scales. The percentage shown to the user is per *device* px, because someone
 * inspecting pixels means device pixels; the two differ whenever the H.264
 * encoder aligned the frame down from the display size.
 */

const MIN_ZOOM = 0.05;
const MAX_ZOOM = 16;
const ZOOM_STEP = 1.25;

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

/**
 * One axis of pan. Content smaller than the viewport is centred; content larger
 * is clamped so its edges never come inside the viewport -- you cannot drag the
 * frame off into empty space and lose it.
 */
function clampAxis(pan, content, viewport) {
  if (content <= viewport) return (viewport - content) / 2;
  return clamp(pan, viewport - content, 0);
}

function isTypingTarget(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

export class Viewport {
  /**
   * @param {object} options
   * @param {HTMLElement} options.wrap       clipping box the frame lives in
   * @param {HTMLCanvasElement} options.canvas
   * @param {() => ({width:number,height:number}|null)} options.getFrame
   * @param {() => ({width:number,height:number}|null)} options.getDisplay
   * @param {(viewport: Viewport) => void} [options.onChange] fires only on real change
   */
  constructor({ wrap, canvas, getFrame, getDisplay, onChange }) {
    this.wrap = wrap;
    this.canvas = canvas;
    this.getFrame = getFrame;
    this.getDisplay = getDisplay;
    this.onChange = onChange;
    this.mode = 'fit';
    this.zoom = 1;
    this.panX = 0;
    this.panY = 0;
    this.panning = false;
    this.spaceHeld = false;
    this.applied = '';
    this.panSession = null;
  }

  // ------------------------------------------------------------ measurements

  wrapSize() {
    const rect = this.wrap.getBoundingClientRect();
    return { width: Math.max(1, rect.width), height: Math.max(1, rect.height) };
  }

  /** Falls back to the canvas bitmap so the empty stage still lays out sanely. */
  frame() {
    const frame = this.getFrame();
    if (frame && frame.width > 0 && frame.height > 0) return frame;
    return { width: this.canvas.width || 1, height: this.canvas.height || 1 };
  }

  display() {
    const display = this.getDisplay?.();
    if (display && display.width > 0 && display.height > 0) return display;
    return this.frame();
  }

  fitZoom() {
    const wrap = this.wrapSize();
    const frame = this.frame();
    return clamp(Math.min(wrap.width / frame.width, wrap.height / frame.height), MIN_ZOOM, MAX_ZOOM);
  }

  get isFit() {
    return this.mode === 'fit';
  }

  /** CSS px per device px, as a percentage -- the number worth showing. */
  devicePercent() {
    const frame = this.frame();
    const display = this.display();
    return (this.zoom * frame.width) / display.width * 100;
  }

  label() {
    const percent = this.devicePercent();
    const rounded = percent >= 100 ? Math.round(percent) : Math.round(percent * 10) / 10;
    return `${rounded}%`;
  }

  // ------------------------------------------------------------- mutations

  /** Wrap-relative position of a pointer event, for anchoring a zoom. */
  anchorFrom(event) {
    const rect = this.wrap.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  centreAnchor() {
    const wrap = this.wrapSize();
    return { x: wrap.width / 2, y: wrap.height / 2 };
  }

  fit() {
    this.mode = 'fit';
    this.refresh();
  }

  /**
   * Set an absolute zoom, keeping the frame point under `anchor` where it is.
   * That is the whole trick behind cursor-anchored zooming: convert the anchor
   * to frame space at the old zoom, then re-derive the pan at the new one.
   */
  setZoom(zoom, anchor = null) {
    const next = clamp(zoom, MIN_ZOOM, MAX_ZOOM);
    const at = anchor || this.centreAnchor();
    const framePoint = {
      x: (at.x - this.panX) / this.zoom,
      y: (at.y - this.panY) / this.zoom,
    };
    this.zoom = next;
    this.panX = at.x - framePoint.x * next;
    this.panY = at.y - framePoint.y * next;
    this.mode = 'manual';
    this.refresh();
  }

  zoomBy(factor, anchor = null) {
    this.setZoom(this.zoom * factor, anchor);
  }

  setDevicePercent(percent, anchor = null) {
    const frame = this.frame();
    const display = this.display();
    this.setZoom((percent / 100) * (display.width / frame.width), anchor);
  }

  panBy(dx, dy) {
    if (dx === 0 && dy === 0) return;
    this.panX += dx;
    this.panY += dy;
    this.mode = 'manual';
    this.refresh();
  }

  /** Recompute for the current wrap and frame size, then write the transform. */
  refresh() {
    const wrap = this.wrapSize();
    const frame = this.frame();
    if (this.mode === 'fit') this.zoom = this.fitZoom();
    const width = frame.width * this.zoom;
    const height = frame.height * this.zoom;
    this.panX = clampAxis(this.panX, width, wrap.width);
    this.panY = clampAxis(this.panY, height, wrap.height);
    this.apply();
  }

  apply() {
    const x = Math.round(this.panX * 100) / 100;
    const y = Math.round(this.panY * 100) / 100;
    const transform = `translate(${x}px, ${y}px) scale(${this.zoom})`;
    if (transform === this.applied) return;
    this.applied = transform;
    this.canvas.style.transform = transform;
    // Below 1:1 the frame is being minified and nearest-neighbour just adds
    // shimmer; at or above it, pixelated is the point -- you came to see pixels.
    this.canvas.style.imageRendering = this.zoom >= 1 ? 'pixelated' : 'auto';
    this.onChange?.(this);
  }

  // ---------------------------------------------------------------- input

  /**
   * True when this event should pan rather than reach the device. The stage's
   * own gesture handlers ask this before claiming a pointerdown.
   */
  wantsPan(event) {
    return event.button === 1 || (event.button === 0 && this.spaceHeld);
  }

  /**
   * Wire wheel, middle-drag, space-drag and the keyboard shortcuts.
   * `stage` is the element carrying the device gestures (`.stage-body`).
   */
  attach(stage) {
    this.stage = stage;

    stage.addEventListener('wheel', (event) => {
      if (!this.getFrame()) return;
      event.preventDefault();
      // Ctrl/Cmd + wheel is the zoom idiom every canvas tool shares, and it is
      // also what a trackpad pinch arrives as. Plain wheel pans, which leaves
      // the bare scroll gesture free to be forwarded to the phone later.
      const lines = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 100 : 1;
      if (event.ctrlKey || event.metaKey) {
        const factor = Math.exp(-event.deltaY * lines * 0.002);
        this.zoomBy(factor, this.anchorFrom(event));
      } else if (event.shiftKey) {
        this.panBy(-(event.deltaY || event.deltaX) * lines, 0);
      } else {
        this.panBy(-event.deltaX * lines, -event.deltaY * lines);
      }
    }, { passive: false });

    stage.addEventListener('pointerdown', (event) => {
      if (!this.wantsPan(event)) return;
      event.preventDefault();
      event.stopPropagation();
      this.panning = true;
      this.panSession = { id: event.pointerId, x: event.clientX, y: event.clientY };
      stage.classList.add('is-panning');
      stage.setPointerCapture?.(event.pointerId);
    }, true);

    stage.addEventListener('pointermove', (event) => {
      if (!this.panning || event.pointerId !== this.panSession?.id) return;
      event.stopPropagation();
      this.panBy(event.clientX - this.panSession.x, event.clientY - this.panSession.y);
      this.panSession.x = event.clientX;
      this.panSession.y = event.clientY;
    }, true);

    const endPan = (event) => {
      if (!this.panning) return;
      if (event && this.panSession && event.pointerId !== this.panSession.id) return;
      this.panning = false;
      this.panSession = null;
      stage.classList.remove('is-panning');
      if (event) {
        // Without this the release would bubble on as the end of a device drag.
        event.stopPropagation();
        stage.releasePointerCapture?.(event.pointerId);
      }
    };
    stage.addEventListener('pointerup', endPan, true);
    stage.addEventListener('pointercancel', endPan, true);

    // Middle-click otherwise opens autoscroll on Windows/Linux.
    stage.addEventListener('auxclick', (event) => {
      if (event.button === 1) event.preventDefault();
    });

    window.addEventListener('keydown', (event) => {
      if (isTypingTarget(event.target)) return;
      if (event.code === 'Space' && !event.repeat) {
        this.spaceHeld = true;
        stage.classList.add('pan-ready');
        // Space would otherwise re-trigger whichever button has focus.
        event.preventDefault();
        return;
      }
      if (!(event.ctrlKey || event.metaKey)) return;
      if (event.key === '=' || event.key === '+') {
        event.preventDefault();
        this.zoomBy(ZOOM_STEP);
      } else if (event.key === '-' || event.key === '_') {
        event.preventDefault();
        this.zoomBy(1 / ZOOM_STEP);
      } else if (event.key === '0') {
        event.preventDefault();
        this.fit();
      } else if (event.key === '1') {
        event.preventDefault();
        this.setDevicePercent(100);
      }
    });

    window.addEventListener('keyup', (event) => {
      if (event.code !== 'Space') return;
      this.spaceHeld = false;
      stage.classList.remove('pan-ready');
    });

    // A window that loses focus mid-pan must not come back still panning.
    window.addEventListener('blur', () => {
      this.spaceHeld = false;
      stage.classList.remove('pan-ready');
      endPan(null);
    });
  }

  zoomIn(anchor) { this.zoomBy(ZOOM_STEP, anchor); }
  zoomOut(anchor) { this.zoomBy(1 / ZOOM_STEP, anchor); }
}
