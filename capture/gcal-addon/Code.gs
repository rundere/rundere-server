/**
 * Yonder — Google Calendar capture add-on.
 *
 * The Calendar sibling of the Gmail add-on (capture/gmail-addon/). Opens a
 * contextual card on any calendar event with a "Capture to Yonder"
 * button; the button sends the Google Calendar API *Event resource* as JSON to
 * the same signed /ingest endpoint (capture_channel: 'gcal'). The server maps it
 * to an event Payload in extract/gcal.py — the SAME shape a Gmail-captured .ics
 * invite produces — so both resolve and commit through identical code.
 *
 * Nothing is parsed client-side beyond selecting the fields the server reads.
 * Config (API key, ingest URL) lives in User Properties, never in code — the
 * exact convention the Gmail add-on uses, so one operator can configure both the
 * same way.
 */

const DEFAULT_INGEST_URL = 'https://YOUR-INGEST-HOST.example.com/ingest';
const PROP_API_KEY = 'TX_API_KEY';
const PROP_INGEST_URL = 'TX_INGEST_URL';

// ------------------------------------------------------------ homepage/config

function buildHomepageCard(e) {
  const props = PropertiesService.getUserProperties();
  const configured = !!props.getProperty(PROP_API_KEY);
  const ingestUrl = props.getProperty(PROP_INGEST_URL) || DEFAULT_INGEST_URL;

  const section = CardService.newCardSection()
    .setHeader('Yonder capture')
    .addWidget(CardService.newTextParagraph().setText(configured
      ? 'Configured. Open a calendar event and use the side panel to capture it.'
      : 'Not configured yet. Paste the API key created via '
        + '"python -m app.cli create-key" and Save.'))
    .addWidget(CardService.newTextInput()
      .setFieldName('apiKey')
      .setTitle('API key')
      .setHint('tx_...'))
    .addWidget(CardService.newTextInput()
      .setFieldName('ingestUrl')
      .setTitle('Ingest URL')
      .setValue(ingestUrl))
    .addWidget(CardService.newTextButton()
      .setText('Save')
      .setOnClickAction(CardService.newAction().setFunctionName('saveConfig')));

  return CardService.newCardBuilder().addSection(section).build();
}

function saveConfig(e) {
  const props = PropertiesService.getUserProperties();
  const inputs = (e.commonEventObject && e.commonEventObject.formInputs) || {};
  const apiKey = inputs.apiKey && inputs.apiKey.stringInputs.value[0];
  const ingestUrl = inputs.ingestUrl && inputs.ingestUrl.stringInputs.value[0];
  if (apiKey) props.setProperty(PROP_API_KEY, apiKey.trim());
  if (ingestUrl) props.setProperty(PROP_INGEST_URL, ingestUrl.trim());

  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText('Saved.'))
    .setNavigation(CardService.newNavigation().updateCard(buildHomepageCard(e)))
    .build();
}

// ------------------------------------------------------------ event card

function buildEventCard(e) {
  const cal = (e && e.calendar) || {};
  const title = cal.id ? '' : '(open an event to capture it)';

  const section = CardService.newCardSection();
  if (!cal.id) {
    section.addWidget(CardService.newTextParagraph()
      .setText('Open a calendar event, then use this panel to capture it.'));
    return CardService.newCardBuilder().addSection(section).build();
  }

  section
    .addWidget(CardService.newKeyValue()
      .setTopLabel('Event').setContent(cal.title || '(untitled)'))
    .addWidget(CardService.newTextInput()
      .setFieldName('notes').setTitle('Notes (optional)'))
    .addWidget(CardService.newTextInput()
      .setFieldName('collectionId')
      .setTitle('Force collection ID (optional, advanced — D-05)'))
    .addWidget(CardService.newTextButton()
      .setText('Capture to Yonder')
      .setOnClickAction(CardService.newAction()
        .setFunctionName('captureEvent')));

  return CardService.newCardBuilder().addSection(section).build();
}

function captureEvent(e) {
  const props = PropertiesService.getUserProperties();
  const apiKey = props.getProperty(PROP_API_KEY);
  const ingestUrl = props.getProperty(PROP_INGEST_URL) || DEFAULT_INGEST_URL;
  if (!apiKey) {
    return notify_('Not configured — open the add-on homepage, paste your '
      + 'API key, and Save first.');
  }

  const cal = (e && e.calendar) || {};
  const eventId = cal.id;
  const calendarId = cal.calendarId || 'primary';
  if (!eventId) return notify_('No event is open.');

  // The Advanced Calendar Service returns the full Event resource — start/end
  // with IANA timeZone, iCalUID, attendees, recurrence, status — which the
  // trigger's e.calendar payload does not fully carry. That resource is exactly
  // what extract/gcal.py maps, so we send a curated subset of it.
  let ev;
  try {
    ev = Calendar.Events.get(calendarId, eventId);
  } catch (err) {
    return notify_('Could not read the event: ' + err);
  }

  const inputs = (e.commonEventObject && e.commonEventObject.formInputs) || {};
  const notes = (inputs.notes && inputs.notes.stringInputs.value[0]) || null;
  const collectionId =
    (inputs.collectionId && inputs.collectionId.stringInputs.value[0]) || null;

  const payload = {
    capture_channel: 'gcal',
    // Only the fields the server reads — keeps the stored artifact tight and
    // free of guest lists we don't need. Mirrors the Google Calendar API Event
    // resource so extract/gcal.py maps it directly.
    event: {
      id: ev.id || null,
      iCalUID: ev.iCalUID || null,
      status: ev.status || null,
      summary: ev.summary || null,
      location: ev.location || null,
      description: ev.description || null,
      start: ev.start || null,
      end: ev.end || null,
      organizer: ev.organizer
        ? { email: ev.organizer.email || null,
            displayName: ev.organizer.displayName || null }
        : null,
      attendees: (ev.attendees || [])
        .filter(function (a) { return a && a.email; })
        .map(function (a) { return { email: a.email }; }),
      recurrence: ev.recurrence || null,
    },
    capture_context: {
      submitted_by: currentUserEmail_(),
      notes: notes,
      collection_id: collectionId,
    },
  };

  let result;
  try {
    result = postToIngest_(ingestUrl, apiKey, payload);
  } catch (err) {
    return notify_('Capture failed: ' + err);
  }

  return notify_(result.ok
    ? 'Captured. batch_id=' + (result.body && result.body.batch_id)
    : 'Capture FAILED (' + result.status + '): '
      + (result.body && result.body.error || result.text));
}

// ------------------------------------------------------------ ingest client
// Byte-for-byte identical to the Gmail add-on: sign the EXACT bytes sent, so the
// server's HMAC check (app/auth.py sign()) matches. See that file's long comment
// about why the shared byte array matters for non-ASCII content.

function postToIngest_(ingestUrl, apiKey, payload) {
  const bodyStr = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));

  const bodyBytes = Utilities.newBlob(bodyStr).getBytes();
  const prefixBytes = Utilities.newBlob(timestamp + '.').getBytes();
  const messageBytes = prefixBytes.concat(bodyBytes);
  const keyBytes = Utilities.newBlob(apiKey).getBytes();
  const signature = bytesToHex_(
    Utilities.computeHmacSha256Signature(messageBytes, keyBytes));

  const response = UrlFetchApp.fetch(ingestUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: bodyBytes,
    headers: {
      'X-Api-Key': apiKey,
      'X-Timestamp': timestamp,
      'X-Signature': signature,
    },
    muteHttpExceptions: true,
  });

  const status = response.getResponseCode();
  const text = response.getContentText();
  let parsed = null;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    // non-JSON error body; leave parsed null, caller falls back to `text`
  }
  return { ok: status === 202, status: status, body: parsed, text: text };
}

// Signature must match app/auth.py sign(): hex(HMAC_SHA256(key, "<ts>." + body)).
function bytesToHex_(bytes) {
  return bytes.map(function (b) {
    const v = (b < 0 ? b + 256 : b).toString(16);
    return v.length === 1 ? '0' + v : v;
  }).join('');
}

function currentUserEmail_() {
  try {
    return Session.getEffectiveUser().getEmail() || null;
  } catch (err) {
    return null;
  }
}

function notify_(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text))
    .build();
}
