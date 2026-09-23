/**
 * Pane sizing: draggable splitters and a collapsible timeline.
 *
 * The grid tracks are CSS custom properties on `.shell`, so a drag is a little
 * arithmetic and one style write. The CSS stays the single source of truth for
 * what the grid looks like; this file only moves three numbers. Sizes persist
 * per browser, and the defaults are the sizes the layout used to hard-code.
 */

const KEY = 'pixelforge.layout.v1';

/**
 * `grow` is which way the track gets bigger as the handle moves in the positive
 * direction of its axis: the left column grows rightwards, the right column and
 * the timeline grow the other way.
 */
const TRACKS = {
  left: { prop: '--col-left', axis: 'x', grow: 1, min: 180, max: 560, fallback: 260 },
  right: { prop: '--col-right', axis: 'x', grow: -1, min: 240, max: 680, fallback: 340 },
  timeline: { prop: '--row-timeline', axis: 'y', grow: -1, min: 90, max: 760, fallback: 210 },
};

/** Room left for the stage matters more than any stored number. */
function limitsFor(spec) {
  const room = spec.axis === 'x'
    ? window.innerWidth - 460
    : window.innerHeight - 260;
  return { min: spec.min, max: Math.max(spec.min, Math.min(spec.max, room)) };
}

const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

function readStored() {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function writeStored(value) {
  try {
    localStorage.setItem(KEY, JSON.stringify(value));
  } catch {
    // Private mode, quota, a locked-down profile -- none of it is worth a toast.
  }
}

export function initLayout({ onResize } = {}) {
  const shell = document.querySelector('.shell');
  if (!shell) return null;
  const stored = readStored();
  const sizes = {};

  let frameRequest = 0;
  const notify = () => {
    if (!onResize || frameRequest) return;
    frameRequest = requestAnimationFrame(() => {
      frameRequest = 0;
      onResize();
    });
  };

  function setSize(name, px, { persist = true } = {}) {
    const spec = TRACKS[name];
    const { min, max } = limitsFor(spec);
    const value = Math.round(clamp(px, min, max));
    sizes[name] = value;
    shell.style.setProperty(spec.prop, `${value}px`);
    if (persist) writeStored({ ...readStored(), [name]: value, collapsed: isCollapsed() });
    notify();
  }

  function currentSize(name) {
    const spec = TRACKS[name];
    const raw = getComputedStyle(shell).getPropertyValue(spec.prop).trim();
    const parsed = Number.parseFloat(raw);
    return Number.isFinite(parsed) ? parsed : spec.fallback;
  }

  function isCollapsed() {
    return shell.dataset.timeline === 'collapsed';
  }

  function setCollapsed(collapsed, { persist = true } = {}) {
    shell.dataset.timeline = collapsed ? 'collapsed' : 'expanded';
    const button = document.getElementById('toggle-timeline');
    if (button) {
      button.setAttribute('aria-expanded', String(!collapsed));
      button.textContent = collapsed ? '▸' : '▾';
      button.title = collapsed ? '展开时间线' : '折叠时间线';
    }
    if (persist) writeStored({ ...readStored(), collapsed });
    notify();
  }

  for (const name of Object.keys(TRACKS)) {
    setSize(name, Number(stored[name]) || TRACKS[name].fallback, { persist: false });
  }
  setCollapsed(Boolean(stored.collapsed), { persist: false });

  for (const handle of shell.querySelectorAll('.gutter')) {
    const name = handle.dataset.track;
    const spec = TRACKS[name];
    if (!spec) continue;

    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      if (name === 'timeline' && isCollapsed()) return;
      event.preventDefault();
      const origin = spec.axis === 'x' ? event.clientX : event.clientY;
      const startSize = currentSize(name);
      handle.setPointerCapture(event.pointerId);
      handle.classList.add('is-active');
      document.body.classList.add(spec.axis === 'x' ? 'is-resizing-x' : 'is-resizing-y');

      const move = (moveEvent) => {
        const now = spec.axis === 'x' ? moveEvent.clientX : moveEvent.clientY;
        setSize(name, startSize + (now - origin) * spec.grow, { persist: false });
      };
      const done = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', done);
        handle.removeEventListener('pointercancel', done);
        handle.releasePointerCapture?.(event.pointerId);
        handle.classList.remove('is-active');
        document.body.classList.remove('is-resizing-x', 'is-resizing-y');
        writeStored({ ...readStored(), [name]: sizes[name], collapsed: isCollapsed() });
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', done);
      handle.addEventListener('pointercancel', done);
    });

    handle.addEventListener('dblclick', () => setSize(name, spec.fallback));
  }

  const toggle = document.getElementById('toggle-timeline');
  if (toggle) toggle.addEventListener('click', () => setCollapsed(!isCollapsed()));

  // A window resize can invalidate a stored width that was fine on a bigger
  // screen; re-clamping keeps the stage from being squeezed out of existence.
  window.addEventListener('resize', () => {
    for (const name of Object.keys(TRACKS)) setSize(name, sizes[name], { persist: false });
  });

  return { setSize, setCollapsed, isCollapsed };
}
