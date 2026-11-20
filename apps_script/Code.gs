/**
 * Code.gs — يستقبل بيانات من GitHub Actions ويكتبها في Google Sheet (مجاني).
 * النشر: Extensions ▸ Apps Script ▸ الصق الكود ▸ Deploy ▸ New deployment ▸ Web app
 *        Execute as: Me   |   Who has access: Anyone
 * ثم ضع رابط الـ Web app في GitHub Secret باسم SHEETS_WEBHOOK_URL
 * وضع نفس قيمة TOKEN أدناه في Secret باسم SHEETS_TOKEN.
 */
const TOKEN = 'غيّر-هذه-القيمة-إلى-نص-سري-طويل';

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (body.token !== TOKEN) return out_({ ok: false, error: 'bad token' });
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    upsert_(ss, 'predictions', body.predictions || [], ['pair', 'created_ts']);
    replaceAll_(ss, 'model_metrics', body.metrics || []);
    replaceAll_(ss, 'summary', body.summary ? [body.summary] : []);
    return out_({ ok: true });
  } catch (err) {
    return out_({ ok: false, error: String(err) });
  }
}

// للتجربة من المتصفح
function doGet() { return out_({ ok: true, msg: 'webhook alive' }); }

function out_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function sheet_(ss, name) { return ss.getSheetByName(name) || ss.insertSheet(name); }

function replaceAll_(ss, name, rows) {
  const sh = sheet_(ss, name);
  sh.clearContents();
  if (!rows.length) return;
  const cols = Object.keys(rows[0]);
  const data = [cols].concat(rows.map(r => cols.map(c => r[c] === undefined ? '' : r[c])));
  sh.getRange(1, 1, data.length, cols.length).setValues(data);
}

// يحدّث الصف إن وُجد المفتاح، وإلا يضيف صفًا جديدًا
function upsert_(ss, name, rows, keyCols) {
  if (!rows.length) return;
  const sh = sheet_(ss, name);
  const cols = Object.keys(rows[0]);
  if (sh.getLastRow() === 0) sh.getRange(1, 1, 1, cols.length).setValues([cols]);
  const header = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
  const keyIdx = keyCols.map(k => header.indexOf(k));
  const last = sh.getLastRow();
  const existing = last > 1 ? sh.getRange(2, 1, last - 1, header.length).getValues() : [];
  const map = {};
  existing.forEach((r, i) => { map[keyIdx.map(k => r[k]).join('|')] = i + 2; });
  const toAppend = [];
  rows.forEach(r => {
    const vals = header.map(h => r[h] === undefined || r[h] === null ? '' : r[h]);
    const key = keyCols.map(k => r[k]).join('|');
    if (map[key]) sh.getRange(map[key], 1, 1, header.length).setValues([vals]);
    else toAppend.push(vals);
  });
  if (toAppend.length) sh.getRange(sh.getLastRow() + 1, 1, toAppend.length, header.length).setValues(toAppend);
}
