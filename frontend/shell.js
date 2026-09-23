const VIEWS = {
  device: 'device.html',
  image: 'tools.html',
  assets: 'assets.html',
  flow: 'notebooks.html',
};

const STORAGE_KEY = 'pixelforge.shell.view';
const frame = document.getElementById('view-frame');
const buttons = [...document.querySelectorAll('.activity-icon[data-view]')];

function setActive(view) {
  if (!VIEWS[view]) return;
  for (const button of buttons) {
    button.setAttribute('aria-current', String(button.dataset.view === view));
  }
  frame.src = VIEWS[view];
  try { localStorage.setItem(STORAGE_KEY, view); } catch { /* private mode, ignore */ }
}

for (const button of buttons) {
  button.addEventListener('click', () => setActive(button.dataset.view));
}

let initial = 'device';
try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved && VIEWS[saved]) initial = saved;
} catch { /* private mode, ignore */ }

setActive(initial);
