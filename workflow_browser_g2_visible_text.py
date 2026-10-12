"""Static JavaScript used only to extract text pixels actually visible inside links."""

VISIBLE_LINK_TEXT_JS = r"""
function visibleLinkText(anchor) {
  const walk = document.createTreeWalker(anchor, NodeFilter.SHOW_TEXT);
  const chunks = [];
  let count = 0, node;
  while ((node = walk.nextNode()) && count++ < 200) {
    const raw = node.nodeValue || '';
    if (!raw.trim()) { if (chunks.length) chunks.push(raw); continue; }
    let visible = true, opacity = 1;
    for (let el = node.parentElement; el && el.nodeType === 1; el = el.parentElement) {
      const style = getComputedStyle(el);
      opacity *= Number(style.opacity);
      if (el.hidden || style.display === 'none' || style.visibility !== 'visible' ||
          style.clipPath !== 'none' || opacity < 0.1) { visible = false; break; }
      if (el === anchor) break;
    }
    if (!visible) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    let rendered = false;
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const hit = document.elementFromPoint(x, y);
      if (hit && hit.closest('a') === anchor) { rendered = true; break; }
    }
    if (rendered) chunks.push(raw);
  }
  return chunks.join('').replace(/\s+/g, ' ').trim().slice(0, 100);
}
"""
