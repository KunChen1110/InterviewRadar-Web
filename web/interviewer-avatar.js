(() => {
  'use strict';

  const STATES = new Set(['speaking', 'listening', 'thinking', 'idle']);
  const observers = new WeakMap();

  function normalizeState(state) {
    return STATES.has(state) ? state : 'idle';
  }

  function setState(container, state) {
    if (!(container instanceof Element)) {
      throw new TypeError('setState(container, state) requires a DOM element');
    }
    container.dataset.state = normalizeState(state);
  }

  function render(container) {
    if (container.querySelector('.ir-avatar__portrait')) return;

    const image = document.createElement('img');
    image.className = 'ir-avatar__portrait';
    image.src = container.dataset.avatarSrc || '../assets/interviewer-avatar.png';
    image.alt = container.dataset.avatarAlt || 'AI 面试官';

    const overlay = document.createElement('span');
    overlay.className = 'ir-avatar__overlay';
    overlay.setAttribute('aria-hidden', 'true');
    overlay.innerHTML = [
      '<span class="ir-avatar__halo"></span>',
      '<span class="ir-avatar__blink"></span>',
      '<span class="ir-avatar__equalizer"><i></i><i></i><i></i><i></i><i></i></span>',
      '<span class="ir-avatar__thinking"><i></i><i></i><i></i></span>',
    ].join('');

    container.append(image, overlay);
  }

  function init(container) {
    if (!(container instanceof Element)) {
      throw new TypeError('init(container) requires a DOM element');
    }
    if (observers.has(container)) return observers.get(container).destroy;

    container.classList.add('ir-avatar');
    render(container);
    setState(container, container.dataset.state);

    let applying = false;
    const observer = new MutationObserver(() => {
      if (applying) return;
      const normalized = normalizeState(container.dataset.state);
      if (container.dataset.state !== normalized) {
        applying = true;
        container.dataset.state = normalized;
        applying = false;
      }
    });
    observer.observe(container, { attributes: true, attributeFilter: ['data-state'] });

    const destroy = () => {
      observer.disconnect();
      observers.delete(container);
    };
    observers.set(container, { destroy });
    return destroy;
  }

  function initAll(root = document) {
    return [...root.querySelectorAll('.ir-avatar')].map(init);
  }

  window.InterviewRadarAvatar = { init, initAll, setState };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll, { once: true });
  } else {
    initAll();
  }
})();
