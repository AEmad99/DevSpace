// static/js/userAvatar.js
// Deterministic generated SVG avatar (GitHub-style identicon) so every user
// gets a unique, non-empty icon derived from their name — no uploads, no
// external service, no blank circle.
//
// The same name always yields the same icon. Used by the sidebar user chip
// (app.js) and the Account settings panel (settings.js).

// 32-bit FNV-1a hash of a string → unsigned int. Stable across runs.
function _hash(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

// Return an <svg> string: a 5x5 left-right-mirrored identicon, colored from a
// hue derived from the seed. viewBox-based + width/height 100% so it fills its
// (circular) container regardless of pixel size.
export function userAvatarSVG(seed) {
  const s = String(seed == null ? '?' : seed) || '?';
  const h = _hash(s);
  const hue = h % 360;
  const fg = `hsl(${hue} 60% 58%)`;
  const bg = `hsl(${hue} 32% 16%)`;
  const CELL = 20; // 100 / 5

  let rects = '';
  // Columns 0,1,2 decide the pattern; 3,4 mirror 1,0 → vertical symmetry.
  for (let col = 0; col < 3; col++) {
    for (let row = 0; row < 5; row++) {
      const bit = (h >>> (col * 5 + row)) & 1; // 15 bits, one per decided cell
      if (!bit) continue;
      const y = row * CELL;
      rects += `<rect x="${col * CELL}" y="${y}" width="${CELL}" height="${CELL}" fill="${fg}"/>`;
      if (col !== 2) {
        rects += `<rect x="${(4 - col) * CELL}" y="${y}" width="${CELL}" height="${CELL}" fill="${fg}"/>`;
      }
    }
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100%" height="100%" `
    + `preserveAspectRatio="xMidYMid meet" role="img" aria-label="user avatar">`
    + `<rect width="100" height="100" fill="${bg}"/>${rects}</svg>`;
}

// Inject a generated avatar into an element and mark it so CSS can drop the
// placeholder background/dimming. No-op if the element is missing.
export function setUserAvatar(el, seed) {
  if (!el) return;
  el.innerHTML = userAvatarSVG(seed);
  el.classList.add('has-avatar');
}
