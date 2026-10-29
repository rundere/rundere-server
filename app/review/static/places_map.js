// The Places Map lens (SPEC-11 D-218). Leaflet + Leaflet.markercluster are vendored
// (no CDN); tiles come through the server (/places/map/tiles), never from a tile
// server directly. The map follows the visible area: on load and on `moveend` it asks
// /places/map/data for that area and draws geohash clusters (low zoom) or single pins.
// First drafted by the homelab's local model, then reviewed and fixed by hand.
(function () {
  'use strict';

  var map = null;            // the live Leaflet map, if the Map lens is on screen
  var mapEl = null;          // the #pl-map element it was built on
  var layer = null;          // everything drawn for the last response
  var controller = null;     // aborts a request a newer move has made stale
  var timer = null;          // debounce for moveend
  var lastView = { center: [20, 0], zoom: 2 };   // kept across lens switches and reloads
  var MAX_DRAWN = 1000;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function setStatus(text) {
    var el = document.getElementById('pl-map-status');
    if (el) el.textContent = text;       // textContent only: never server text as HTML
  }

  function destroy() {
    clearTimeout(timer);
    if (controller) controller.abort();
    if (map) map.remove();
    map = mapEl = layer = controller = null;
  }

  // htmx swaps the lens area, so #pl-map comes and goes: build, keep or drop the map to
  // match what is on the page now.
  function init() {
    var el = document.getElementById('pl-map');
    if (!el) { if (map) destroy(); return; }
    if (map && mapEl === el) return;
    if (map) destroy();
    if (!window.L || !L.markerClusterGroup) { setStatus('The map could not load.'); return; }

    mapEl = el;
    map = L.map(el, {
      minZoom: 1, maxZoom: 19, worldCopyJump: false,
      maxBounds: [[-85, -180], [85, 180]], maxBoundsViscosity: 1
    });
    L.tileLayer(el.dataset.tiles, {
      maxZoom: 19, noWrap: true,
      attribution: escapeHtml(el.dataset.attribution || '')   // Leaflet takes HTML
    }).addTo(map);
    map.setView(lastView.center, lastView.zoom);
    L.Icon.Default.imagePath = '/review/static/vendor/leaflet/images/';
    // The container may have only just been laid out by the swap.
    requestAnimationFrame(function () { if (map) map.invalidateSize(); });

    map.on('moveend', function () {
      var c = map.getCenter();
      lastView = { center: [c.lat, c.lng], zoom: map.getZoom() };
      clearTimeout(timer);
      timer = setTimeout(refresh, 250);
    });
    refresh();
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function refresh() {
    if (!map || !mapEl) return;
    if (controller) controller.abort();
    var mine = controller = new AbortController();
    var b = map.getBounds();
    var q = 's=' + clamp(b.getSouth(), -90, 90).toFixed(5) +
            '&w=' + clamp(b.getWest(), -180, 180).toFixed(5) +
            '&n=' + clamp(b.getNorth(), -90, 90).toFixed(5) +
            '&e=' + clamp(b.getEast(), -180, 180).toFixed(5) +
            '&zoom=' + Math.round(map.getZoom());
    var thisMap = map;
    fetch(mapEl.dataset.data + '?' + q, {
      credentials: 'same-origin', headers: { 'Accept': 'application/json' }, signal: mine.signal
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function (data) {
      if (mine !== controller || map !== thisMap) return;      // stale, or the map is gone
      setStatus('');
      draw(data || {});
    }).catch(function (err) {
      if (err && err.name === 'AbortError') return;
      setStatus('The places could not be loaded. Move the map to try again.');
    });
  }

  function draw(data) {
    var next = L.layerGroup();
    var items = data.clusters || data.places || [];
    if (data.clusters) {
      items.slice(0, MAX_DRAWN).forEach(function (c) { addCluster(next, c); });
    } else {
      var pins = L.markerClusterGroup({
        maxClusterRadius: 50, showCoverageOnHover: false, spiderfyOnMaxZoom: true
      });
      items.slice(0, MAX_DRAWN).forEach(function (p) { addPlace(pins, p); });
      next.addLayer(pins);
    }
    if (layer) map.removeLayer(layer);
    layer = next.addTo(map);
  }

  function finite(a, b) { return typeof a === 'number' && isFinite(a) && typeof b === 'number' && isFinite(b); }

  function addCluster(group, c) {
    var count = Number(c.count);
    if (!finite(c.lat, c.lon) || !isFinite(count)) return;
    var size = count < 10 ? 'sm' : count < 100 ? 'md' : 'lg';
    var m = L.marker([c.lat, c.lon], {
      title: count + (count === 1 ? ' place' : ' places'), keyboard: true,
      icon: L.divIcon({
        className: 'pl-cluster-icon', iconSize: [40, 40],
        html: '<div class="pl-cluster ' + size + '"><span>' + count + '</span></div>'  // a number only
      })
    });
    m.on('click', function () {
      map.setView([c.lat, c.lon], Math.min(map.getZoom() + 2, 19));
    });
    group.addLayer(m);
  }

  function addPlace(group, p) {
    if (!finite(p.lat, p.lon) || !p.id) return;
    var name = typeof p.name === 'string' ? p.name : '';
    var m = L.marker([p.lat, p.lon], { title: name, alt: name, keyboard: true });
    m.on('click', function () {      // the same panel the other lenses open
      htmx.ajax('GET', '/places/' + encodeURIComponent(p.id), { target: '#pl-sheet', swap: 'innerHTML' });
    });
    group.addLayer(m);
  }

  document.addEventListener('DOMContentLoaded', init);
  document.body.addEventListener('htmx:afterSwap', init);
})();
