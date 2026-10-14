() => {
 const root = document.querySelector('#modal-root .rd-sheet, #modal-root .dialog, #modal-root > *') ? document.querySelector('#modal-root') : document.body;
 const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
   return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'; };
 const fam = e => getComputedStyle(e).fontFamily.split(',')[0].replace(/["']/g, '').trim();
 const lab = e => (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\s+/).slice(0,2).join('.') : '') || e.tagName.toLowerCase();
 // --- colour maths (rgb(), rgba(), color(srgb …))
 const parse = s => { let m = s.match(/rgba?\(([^)]+)\)/); if (m) { const v = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return [v[0], v[1], v[2], v[3] ?? 1]; }
   m = s.match(/color\(srgb ([^)]+)\)/); if (m) { const v = m[1].split(/[ \/]+/).filter(Boolean).map(Number); return [v[0]*255, v[1]*255, v[2]*255, v[3] ?? 1]; } return null; };
 const lum = c => { const f = x => { x /= 255; return x <= .03928 ? x / 12.92 : Math.pow((x + .055) / 1.055, 2.4); }; return .2126*f(c[0]) + .7152*f(c[1]) + .0722*f(c[2]); };
 const bgOf = e => { let layers = []; for (let n = e; n; n = n.parentElement) { const cs = getComputedStyle(n);
     if (cs.backgroundImage && cs.backgroundImage !== 'none') return null; const c = parse(cs.backgroundColor);
     if (c && c[3] > 0) { layers.push(c); if (c[3] >= 1) break; } }
   let base = parse(getComputedStyle(document.body).backgroundColor) || [255,255,255,1];
   for (const c of layers.reverse()) base = [c[0]*c[3]+base[0]*(1-c[3]), c[1]*c[3]+base[1]*(1-c[3]), c[2]*c[3]+base[2]*(1-c[3]), 1];
   return base; };
 const texts = [...root.querySelectorAll('body *, #modal-root *')].filter(e => vis(e) && [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim()));
 const under12 = [], outfitSmall = [], contrast = [];
 for (const e of texts) { const cs = getComputedStyle(e), fs = parseFloat(cs.fontSize);
   if (fs < 12) under12.push(lab(e) + ' ' + fs + 'px');
   if (fam(e) === 'Outfit' && fs < 20) outfitSmall.push(lab(e) + ' ' + fs + 'px');
   const fg = parse(cs.color), bg = bgOf(e); if (!fg || !bg) continue;
   const f = [fg[0]*fg[3]+bg[0]*(1-fg[3]), fg[1]*fg[3]+bg[1]*(1-fg[3]), fg[2]*fg[3]+bg[2]*(1-fg[3])];
   const L1 = lum(f), L2 = lum(bg), ratio = (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05);
   const large = fs >= 24 || (fs >= 18.66 && +cs.fontWeight >= 700);
   if (ratio < (large ? 3 : 4.5) && !e.closest('[disabled],[aria-disabled=true],.disabled')) contrast.push(lab(e) + ' ' + ratio.toFixed(2) + ' "' + e.textContent.trim().slice(0, 24) + '"'); }
 const ctl = [...root.querySelectorAll('a.btn,button,input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea,summary')].filter(vis);
 const small = ctl.filter(e => e.getBoundingClientRect().height < 32).map(e => lab(e) + ' ' + Math.round(e.getBoundingClientRect().height) + 'px');
 const okR = new Set(['0px', '6px', '10px', '16px', '24px']);
 const radii = {}; for (const e of [...ctl, ...root.querySelectorAll('.card,.rd-sheet,.tag,.pill,.chip')].filter(vis)) {
   const r = getComputedStyle(e).borderTopLeftRadius; if (okR.has(r) || parseFloat(r) >= 999 || r.includes('%')) continue;
   const k = lab(e) + ' ' + r; radii[k] = (radii[k] || 0) + 1; }
 const prim = [...root.querySelectorAll('.btn-primary,.act.primary,button.primary')].filter(vis).length;
 const txt = root.innerText || ''; const emoji = (txt.match(/\p{Extended_Pictographic}/gu) || []).filter(c => !'©®™↗→←↑↓'.includes(c)).slice(0, 6);
 const h = root.querySelector('h1,h2');
 const uniq = a => [...new Set(a)];
 return { title: document.title, bodyFont: fam(document.body), headFont: h ? fam(h) : null,
   ctl: ctl.length, small: small.length, smallList: uniq(small).slice(0, 8), primary: prim, emoji,
   under12: under12.length, under12List: uniq(under12).slice(0, 10), outfitSmall: uniq(outfitSmall).slice(0, 6),
   contrast: contrast.length, contrastList: uniq(contrast).slice(0, 8),
   radii: Object.keys(radii).slice(0, 10), overflowX: document.documentElement.scrollWidth > innerWidth + 1 };
}
