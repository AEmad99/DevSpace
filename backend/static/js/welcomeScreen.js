// Welcome-screen state and lightweight launch actions.

const DESKTOP_TIPS = [
  'Press Ctrl+K to search across every conversation.',
  'Press Ctrl+B to quickly toggle the sidebar.',
  'Drag and drop files onto the chat to attach them.',
  'Right-click a session for rename, delete, and memory options.',
];

const MOBILE_TIPS = [
  'Long-press a session for rename, delete, and memory options.',
  'Nobody mode keeps the conversation out of history and memory.',
  'Switch to Agent mode when you want tools and code execution.',
  'Attach images or files using the + button beside the input.',
];

function randomTip() {
  const tips = window.matchMedia('(max-width: 768px)').matches ? MOBILE_TIPS : DESKTOP_TIPS;
  return tips[Math.floor(Math.random() * tips.length)];
}

export function setWelcomeModelState(hasModels) {
  const screen = document.getElementById('welcome-screen');
  const subtitle = document.getElementById('welcome-sub');
  const tip = document.getElementById('welcome-tip');
  const setupButton = document.getElementById('welcome-setup-btn');
  const startButton = document.getElementById('welcome-start-btn');
  if (!screen) return;

  screen.classList.toggle('welcome-configured', hasModels);
  if (subtitle) {
    subtitle.textContent = hasModels
      ? 'Chat, code, research, and create from one private workspace.'
      : 'Connect a local or API model, then chat, code, research, and create from one private workspace.';
  }
  if (tip) {
    tip.textContent = hasModels
      ? randomTip()
      : 'Your data stays on this device unless you connect a cloud provider.';
  }
  if (setupButton) setupButton.hidden = hasModels;
  if (startButton) startButton.hidden = !hasModels;
}

export function initWelcomeScreen() {
  const screen = document.getElementById('welcome-screen');
  if (!screen || screen.dataset.initialized === 'true') return;
  screen.dataset.initialized = 'true';

  screen.addEventListener('click', (event) => {
    const launcher = event.target.closest('[data-welcome-target]');
    if (launcher) {
      document.getElementById(launcher.dataset.welcomeTarget)?.click();
      return;
    }

    if (event.target.closest('.welcome-focus-composer')) {
      document.getElementById('message')?.focus();
    }
  });

  fetch('/api/version')
    .then(response => response.json())
    .then(data => { if (data.version) window._appVersion = data.version; })
    .catch(() => {});
}

initWelcomeScreen();
