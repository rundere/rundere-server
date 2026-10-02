/**
 * Run·dere — Gmail capture add-on (D-03).
 *
 * Runs inside the user's own Gmail session using the narrow
 * gmail.addons.current.message.readonly scope — NOT gmail.readonly — so no
 * Google verification/CASA is required for personal use (D-03).
 *
 * Sends the RAW message source (D-15: keep the original forever; nothing is
 * parsed here — that is the server's job). Config (API key,
 * ingest URL) lives in User Properties, never in code.
 */

// Placeholder only — each user sets their own public /ingest URL in the
// add-on's homepage card (stored per-account in User Properties, never in this
// shared script). Left as an obvious placeholder so a fork does not
// accidentally POST someone else's data to the original author's endpoint.
const DEFAULT_INGEST_URL = 'https://YOUR-INGEST-HOST.example.com/ingest';
const PROP_API_KEY = 'TX_API_KEY';
const PROP_INGEST_URL = 'TX_INGEST_URL';

// ------------------------------------------------------------ homepage/config

function buildHomepageCard(e) {
  const props = PropertiesService.getUserProperties();
  const configured = !!props.getProperty(PROP_API_KEY);
  const ingestUrl = props.getProperty(PROP_INGEST_URL) || DEFAULT_INGEST_URL;

  const section = CardService.newCardSection()
    .setHeader('Run·dere capture')
    .addWidget(CardService.newTextParagraph().setText(configured
      ? 'Configured. Open an email and use the side panel to capture it.'
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

// ------------------------------------------------------------ contextual card

function buildContextualCard(e) {
  const accessToken = e.gmail.accessToken;
  const messageId = e.gmail.messageId;
  GmailApp.setCurrentMessageAccessToken(accessToken);
  const message = GmailApp.getMessageById(messageId);

  const section = CardService.newCardSection()
    .addWidget(CardService.newKeyValue()
      .setTopLabel('Subject').setContent(message.getSubject() || '(no subject)'))
    .addWidget(CardService.newKeyValue()
      .setTopLabel('From').setContent(message.getFrom()))
    .addWidget(CardService.newKeyValue()
      .setTopLabel('Date').setContent(message.getDate().toString()))
    .addWidget(CardService.newTextInput()
      .setFieldName('notes').setTitle('Notes (optional)'))
    .addWidget(CardService.newTextInput()
      .setFieldName('collectionId')
      .setTitle('Force collection ID (optional, advanced — D-05)'))
    // Deliberately no .setParameters() for messageId/accessToken: Gmail
    // re-supplies a FRESH e.gmail.{messageId,accessToken} on every action
    // event fired from a contextual card. Snapshotting them here instead
    // caused "Expired access token" once any time passed before the click.
    .addWidget(CardService.newTextButton()
      .setText('Capture to Run·dere')
      .setOnClickAction(CardService.newAction()
        .setFunctionName('captureMessage')));

  return CardService.newCardBuilder().addSection(section).build();
}

function captureMessage(e) {
  const props = PropertiesService.getUserProperties();
  const apiKey = props.getProperty(PROP_API_KEY);
  const ingestUrl = props.getProperty(PROP_INGEST_URL) || DEFAULT_INGEST_URL;
  if (!apiKey) {
    return notify_('Not configured — open the add-on homepage, paste your '
      + 'API key, and Save first.');
  }

  const messageId = e.gmail.messageId;
  const accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);
  const message = GmailApp.getMessageById(messageId);

  const inputs = (e.commonEventObject && e.commonEventObject.formInputs) || {};
  const notes = (inputs.notes && inputs.notes.stringInputs.value[0]) || null;
  const collectionId =
    (inputs.collectionId && inputs.collectionId.stringInputs.value[0]) || null;

  // D-15: the raw RFC 2822 source is the artifact worth keeping forever.
  // Nothing is parsed client-side — that is the (not-yet-built) extractor's job.
  const rawContent = message.getRawContent();
  const rawBase64 = Utilities.base64Encode(rawContent, Utilities.Charset.UTF_8);

  const payload = {
    capture_channel: 'gmail',
    message_id: messageId,
    thread_id: message.getThread().getId(),
    subject: message.getSubject(),
    from: message.getFrom(),
    internal_date: message.getDate().toISOString(),
    raw_message_base64: rawBase64,
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

  // The email is never relabelled or moved: capture only reads it (D-164 drops the
  // old Trip Planning label workflow of D-26). The ingest result is already durably
  // recorded server-side by this point.
  return notify_(result.ok
    ? 'Captured. batch_id=' + (result.body && result.body.batch_id)
    : 'Capture FAILED (' + result.status + '): '
      + (result.body && result.body.error || result.text));
}

// ------------------------------------------------------------ ingest client

function postToIngest_(ingestUrl, apiKey, payload) {
  const bodyStr = JSON.stringify(payload);
  const timestamp = String(Math.floor(Date.now() / 1000));

  // Convert to explicit UTF-8 bytes ONCE and reuse that exact byte array for
  // both the signature and the literal wire payload. Signing a JS string via
  // computeHmacSha256Signature and separately handing that same string to
  // UrlFetchApp as `payload` relies on two independent string->bytes
  // conversions matching — they didn't, for a large real email with
  // non-ASCII header content (2026-08-03 bring-up bug: signatures always
  // mismatched on real captures, body_len matched but hashes didn't).
  const bodyBytes = Utilities.newBlob(bodyStr).getBytes();
  const prefixBytes = Utilities.newBlob(timestamp + '.').getBytes();
  const messageBytes = prefixBytes.concat(bodyBytes);
  const keyBytes = Utilities.newBlob(apiKey).getBytes();
  const signature = bytesToHex_(
    Utilities.computeHmacSha256Signature(messageBytes, keyBytes));

  const response = UrlFetchApp.fetch(ingestUrl, {
    method: 'post',
    contentType: 'application/json',
    payload: bodyBytes,          // exact same bytes just signed — no re-encoding
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
    // non-JSON error body; leave parsed as null, caller falls back to `text`
  }
  return {ok: status === 202, status: status, body: parsed, text: text};
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
