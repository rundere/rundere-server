/**
 * Travel Extractor — Google Maps directions capture (n8n Code node).
 *
 * The thin capture client for the maps_url channel (D-61). n8n gives us a hosted
 * form for free; this node turns the submission into a SIGNED POST to the same
 * public /ingest the Apps Script add-ons use. It parses nothing — the server's
 * extract/maps_url.py does that. n8n is only a capture surface here, NOT the
 * extraction/parsing role D-01/D-02 removed.
 *
 * Signs EXACTLY like app/auth.py sign():  hex(HMAC_SHA256(key, ts + "." + body)).
 * The body string signed here is the exact string the HTTP node must send — set
 * that node's body to "Raw" so nothing re-serializes it (the byte-mismatch bug
 * the Gmail add-on hit with non-ASCII content).
 *
 * Upstream nodes (rename to match, or edit the two $() calls below):
 *   "Trip form"  — the Form Trigger (its fields become form[...] keys)
 *   "Config"     — a Set/Edit-Fields node holding { apiKey, ingestUrl }
 */
const crypto = require('crypto');

const form = $('Trip form').first().json;
const cfg = $('Config').first().json;

const apiKey = String(cfg.apiKey || '').trim();
const ingestUrl = String(cfg.ingestUrl || '').trim();
if (!apiKey || apiKey.startsWith('REPLACE')) {
  throw new Error('Set your API key in the Config node — create one with '
    + '"python -m app.cli create-key maps-n8n".');
}

const get = (label) => {
  const v = form[label];
  return v === undefined || v === null || v === '' ? null : v;
};
const asDate = (v) => (v ? String(v).slice(0, 10) : null);   // 'YYYY-MM-DD'

const url = get('Google Maps URL');
const travelDate = asDate(get('Travel date'));
if (!url) throw new Error('Google Maps URL is required.');
if (!travelDate) {
  throw new Error('Travel date is required — a directions URL carries no date (RULE-31).');
}

// capture_context: the human-supplied metadata a directions URL cannot carry.
// Empty fields are dropped so the server sees clean nulls, not "".
const cc = {
  travel_date: travelDate,
  travel_time: get('Travel time (HH:MM)'),      // optional
  end_date: asDate(get('End date (multi-day)')), // optional
  timezone: get('Timezone') || 'America/Toronto',
  collection_id: get('Collection ID'),           // optional override (D-05)
  title: get('Trip title'),                      // optional
  notes: get('Notes'),                           // optional
  submitted_by: 'maps-n8n',
};
for (const k of Object.keys(cc)) if (cc[k] === null) delete cc[k];

const payload = { capture_channel: 'maps_url', url, capture_context: cc };

// Sign the SAME string we send. Never JSON.stringify again downstream.
const bodyString = JSON.stringify(payload);
const timestamp = Math.floor(Date.now() / 1000).toString();
const signature = crypto
  .createHmac('sha256', apiKey)
  .update(timestamp + '.' + bodyString, 'utf8')
  .digest('hex');

return [{
  json: {
    ingestUrl,
    bodyString,
    xApiKey: apiKey,
    xTimestamp: timestamp,
    xSignature: signature,
  },
}];
