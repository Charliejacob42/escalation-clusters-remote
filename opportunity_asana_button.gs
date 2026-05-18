/**
 * Append-only "Review YYYY-MM-DD" tab builder. Bound to the Escalation cluster
 * tracking master sheet.
 *
 * Behavior:
 *   - onEdit fires on any cell change.
 *   - We act only when column G of a tab matching ^Taxonomy output \d{4}-\d{2}-\d{2}$
 *     is set to TRUE.
 *   - When a checkbox flips TRUE for a (cluster, subcluster) pair NOT yet present
 *     in the Review tab, we APPEND those findings rows to the bottom.
 *   - We NEVER clear, delete, or reorder rows in the Review tab. Unchecking a box
 *     does nothing. Re-checking a box whose rows are already present does nothing.
 *   - Rows accumulate in the sequence the boxes were clicked.
 *
 * Install:
 *   1. Open the master sheet.
 *   2. Extensions -> Apps Script.
 *   3. Replace contents of Code.gs with this file's contents.
 *   4. Save (cmd-S). Click Run once on `syncReviewTabForLatest` to authorize.
 *   5. Toggle a checkbox in any "Taxonomy output YYYY-MM-DD" tab to confirm.
 *
 *   Also: run `installAsanaTrigger` once to enable the Opportunities tab Asana
 *   ticket button (requires the ASANA_PAT script property -- see Asana section).
 */

const TAGS_DROPDOWN = [
  'knowledge', 'carrier_site', 'crm', 'request_not_possible',
  'carrier_callout', 'human_preferred', 'other', 'legal',
  'reconnecting_to_agent', 'app_issues'
];

const REVIEW_HEADERS = [
  'Propelix', 'Front', 'CRM', 'Display name', 'Cluster', 'Subcluster',
  'User intent', 'Where bot got stuck', 'What agent did',
  'Carrier', 'How did agent solve?', 'Self-serve opportunity', 'Tags', 'Notes'
];

const TAX_PATTERN = /^Taxonomy output (\d{4}-\d{2}-\d{2})$/;
const CLUSTER_COL = 5;
const SUB_COL = 6;
const TAGS_COL = 13;
const WRAP_COLS = [4, 5, 6, 7, 8, 9, 11, 12, 14];
const COL_WIDTHS = [
  [1, 100], [2, 100], [3, 100], [4, 160],
  [5, 200], [6, 220], [7, 320], [8, 320], [9, 320],
  [10, 140], [11, 280], [12, 200], [13, 160], [14, 280]
];

function onEdit(e) {
  if (!e || !e.range) return;
  const sheet = e.range.getSheet();
  const m = sheet.getName().match(TAX_PATTERN);
  if (!m) return;
  if (e.range.getColumn() !== 7) return;
  if (e.range.getValue() !== true) return;
  const weekDate = m[1];
  const row = e.range.getRow();
  const pair = readClusterSubAtRow(sheet, row);
  if (!pair) return;
  appendSubclusterToReview(weekDate, pair.cluster, pair.sub);
}

/**
 * One-shot sync: for the latest week, append rows for every TRUE checkbox whose
 * (cluster, sub) is not already in the Review tab. Used to authorize the script
 * and to recover if onEdit missed a toggle. Never deletes existing content.
 */
function syncReviewTabForLatest() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let latestDate = '';
  for (const s of ss.getSheets()) {
    const m = s.getName().match(TAX_PATTERN);
    if (m && m[1] > latestDate) latestDate = m[1];
  }
  if (!latestDate) return;
  syncReviewTabForDate(latestDate);
}

function syncReviewTabForDate(weekDate) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const taxSheet = ss.getSheetByName('Taxonomy output ' + weekDate);
  if (!taxSheet) return;
  const taxLastRow = taxSheet.getLastRow();
  if (taxLastRow < 2) return;

  const taxRange = taxSheet.getRange(2, 1, taxLastRow - 1, 7).getValues();
  let currentCluster = '';
  for (const row of taxRange) {
    const a = row[0];
    const b = row[1];
    const t = row[6];
    if (a && !b) currentCluster = a;
    if (b && t === true && currentCluster) {
      appendSubclusterToReview(weekDate, currentCluster, b);
    }
  }
}

function readClusterSubAtRow(sheet, row) {
  const sub = sheet.getRange(row, 2).getValue();
  if (!sub) return null;
  let cluster = '';
  for (let r = row; r >= 2; r--) {
    const a = sheet.getRange(r, 1).getValue();
    const b = sheet.getRange(r, 2).getValue();
    if (a && !b) { cluster = a; break; }
  }
  if (!cluster) return null;
  return { cluster: cluster, sub: sub };
}

/**
 * Append findings rows for one (cluster, subcluster) pair to the Review tab.
 * No-op if the pair is already present in the Review tab.
 * Creates the Review tab with headers/formatting if it doesn't exist.
 */
function appendSubclusterToReview(weekDate, cluster, sub) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const findingsSheet = ss.getSheetByName('Findings ' + weekDate);
  if (!findingsSheet) return;
  const reviewName = 'Review ' + weekDate;

  const fData = findingsSheet.getDataRange().getValues();
  const fFormulas = findingsSheet.getDataRange().getFormulas();
  if (fData.length < 2) return;
  const fHeader = fData[0];
  const colIdx = {};
  for (let i = 0; i < fHeader.length; i++) colIdx[fHeader[i]] = i;

  const required = ['Display name', 'Propelix', 'Front', 'CRM', 'Cluster', 'Subcluster',
                    'User intent', 'Where bot got stuck', 'What agent did'];
  for (const c of required) {
    if (!(c in colIdx)) return;
  }

  const matchingIdx = [];
  for (let i = 1; i < fData.length; i++) {
    if (fData[i][colIdx['Cluster']] === cluster && fData[i][colIdx['Subcluster']] === sub) {
      matchingIdx.push(i);
    }
  }
  if (matchingIdx.length === 0) return;

  let reviewSheet = ss.getSheetByName(reviewName);
  let isNew = false;
  if (!reviewSheet) {
    reviewSheet = ss.insertSheet(reviewName);
    isNew = true;
    initReviewSheetHeader(reviewSheet);
  } else {
    if (reviewPairAlreadyPresent(reviewSheet, cluster, sub)) return;
  }

  const newRows = [];
  for (const i of matchingIdx) {
    const propelix = fFormulas[i][colIdx['Propelix']] || fData[i][colIdx['Propelix']];
    const front = fFormulas[i][colIdx['Front']] || fData[i][colIdx['Front']];
    const crm = fFormulas[i][colIdx['CRM']] || fData[i][colIdx['CRM']];
    newRows.push([
      propelix, front, crm,
      fData[i][colIdx['Display name']],
      fData[i][colIdx['Cluster']],
      fData[i][colIdx['Subcluster']],
      fData[i][colIdx['User intent']],
      fData[i][colIdx['Where bot got stuck']],
      fData[i][colIdx['What agent did']],
      '', '', '', '', ''
    ]);
  }

  const startRow = reviewSheet.getLastRow() + 1;
  reviewSheet.getRange(startRow, 1, newRows.length, REVIEW_HEADERS.length).setValues(newRows);
  applyRowFormatting(reviewSheet, startRow, newRows.length);

  if (isNew) {
    positionReviewTabAfterTaxonomy(ss, reviewSheet, weekDate);
  }
}

function reviewPairAlreadyPresent(reviewSheet, cluster, sub) {
  const lastRow = reviewSheet.getLastRow();
  if (lastRow < 2) return false;
  const pairs = reviewSheet.getRange(2, CLUSTER_COL, lastRow - 1, 2).getValues();
  for (const p of pairs) {
    if (p[0] === cluster && p[1] === sub) return true;
  }
  return false;
}

function initReviewSheetHeader(reviewSheet) {
  reviewSheet.getRange(1, 1, 1, REVIEW_HEADERS.length).setValues([REVIEW_HEADERS]);
  reviewSheet.setFrozenRows(1);
  const header = reviewSheet.getRange(1, 1, 1, REVIEW_HEADERS.length);
  header.setFontWeight('bold');
  header.setBackground('#1F2937');
  header.setFontColor('#FFFFFF');
  header.setHorizontalAlignment('left');
  header.setVerticalAlignment('middle');
  header.setWrap(true);
  for (const [col, w] of COL_WIDTHS) reviewSheet.setColumnWidth(col, w);
}

function applyRowFormatting(reviewSheet, startRow, numRows) {
  const tagsRange = reviewSheet.getRange(startRow, TAGS_COL, numRows, 1);
  const rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(TAGS_DROPDOWN, true)
    .setAllowInvalid(false)
    .build();
  tagsRange.setDataValidation(rule);

  for (const c of WRAP_COLS) {
    reviewSheet.getRange(startRow, c, numRows, 1)
      .setWrap(true)
      .setVerticalAlignment('top');
  }
}

function positionReviewTabAfterTaxonomy(ss, reviewSheet, weekDate) {
  const taxName = 'Taxonomy output ' + weekDate;
  const sheets = ss.getSheets();
  for (let i = 0; i < sheets.length; i++) {
    if (sheets[i].getName() === taxName) {
      ss.setActiveSheet(reviewSheet);
      ss.moveActiveSheet(i + 2);
      return;
    }
  }
}

// ============================================================================
// Opportunities tab -> Asana ticket creator
//
// When the "Create ticket?" checkbox is flipped to TRUE on a row in the
// Opportunities tab, this installable trigger builds an Asana task in the
// Prompt Updates section of the Chatbot Team project and writes the permalink
// back into the "Asana ticket" column.
//
// One-time setup:
//   1. Project Settings (gear icon) > Script Properties > add key ASANA_PAT,
//      value = the contents of ~/.claude/asana-pat.txt.
//   2. Select function `installAsanaTrigger` from the dropdown and click Run.
//      Authorize the UrlFetchApp scope when prompted.
//
// This installable trigger coexists with the simple onEdit above; each function
// bails out on the other's tab via early-return checks.
// ============================================================================

const ASANA_PROJECT_GID = '1201602424812416';   // Chatbot Team [auto-]
const ASANA_SECTION_GID = '1204515908618145';   // Prompt Updates
const ASANA_ASSIGNEE_GID = '1211055619190425';  // Charlie Jacob
const ASANA_WORKSPACE_GID = '228627491304908';  // Jerry workspace
const OPP_TAB = 'Opportunities';
const CHECKBOX_HEADER = 'Create ticket?';
const TICKET_HEADER = 'Asana ticket';

/** One-time setup: install the installable onEdit trigger for the Asana button. */
function installAsanaTrigger() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const t of triggers) {
    if (t.getHandlerFunction() === 'onEditAsanaTrigger') {
      ScriptApp.deleteTrigger(t);
    }
  }
  ScriptApp.newTrigger('onEditAsanaTrigger')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onEdit()
    .create();
  console.log('Installed onEditAsanaTrigger.');
}

/** Installable onEdit handler for the Opportunities tab Create ticket? checkbox. */
function onEditAsanaTrigger(e) {
  try {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet();
    if (sheet.getName() !== OPP_TAB) return;
    const row = e.range.getRow();
    if (row < 2) return;

    const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
    const checkboxCol = headers.indexOf(CHECKBOX_HEADER) + 1;
    const ticketCol = headers.indexOf(TICKET_HEADER) + 1;
    if (checkboxCol === 0 || ticketCol === 0) return;
    if (e.range.getColumn() !== checkboxCol) return;
    if (e.value !== 'TRUE' && e.value !== true) return;

    const rowValues = sheet.getRange(row, 1, 1, sheet.getLastColumn()).getValues()[0];
    const get = (name) => {
      const i = headers.indexOf(name);
      return i >= 0 ? String(rowValues[i] != null ? rowValues[i] : '') : '';
    };

    const existing = get(TICKET_HEADER);
    if (existing && existing.indexOf('https://') === 0) {
      SpreadsheetApp.getActive().toast('Ticket already exists for row ' + row + '.', 'Asana', 5);
      return;
    }

    const pat = PropertiesService.getScriptProperties().getProperty('ASANA_PAT');
    if (!pat) {
      SpreadsheetApp.getActive().toast('ASANA_PAT script property is not set.', 'Asana', 8);
      return;
    }

    const themeName = get('Theme name') || ('Theme ' + get('Theme ID'));
    const taskBody = buildAsanaTaskBody({
      themeId: get('Theme ID'),
      type: get('Type'),
      description: get('Description'),
      ruleSummary: get('Rule summary'),
      suggestedChange: get('Suggested change'),
      firstSeen: get('First seen'),
      lastSeen: get('Last seen'),
      weeksObserved: get('Weeks observed'),
      totalVolume: get('Total volume'),
      latestWeekVolume: get('Latest week volume'),
      dominantCluster: get('Dominant cluster'),
      dominantSubcluster: get('Dominant subcluster'),
      samples: get('Sample conversations'),
      sheetUrl: SpreadsheetApp.getActive().getUrl(),
    });

    const payload = {
      data: {
        name: themeName,
        notes: taskBody,
        projects: [ASANA_PROJECT_GID],
        memberships: [{ project: ASANA_PROJECT_GID, section: ASANA_SECTION_GID }],
        assignee: ASANA_ASSIGNEE_GID,
        workspace: ASANA_WORKSPACE_GID,
      },
    };

    const resp = UrlFetchApp.fetch('https://app.asana.com/api/1.0/tasks', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + pat },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
    const code = resp.getResponseCode();
    const body = JSON.parse(resp.getContentText() || '{}');
    if (code < 200 || code >= 300) {
      const msg = (body.errors && body.errors[0] && body.errors[0].message) || resp.getContentText();
      SpreadsheetApp.getActive().toast('Asana ' + code + ': ' + msg, 'Error', 10);
      sheet.getRange(row, checkboxCol).setValue(false);
      return;
    }

    const task = body.data || {};
    const url = task.permalink_url || ('https://app.asana.com/0/' + ASANA_PROJECT_GID + '/' + task.gid);
    sheet.getRange(row, ticketCol).setValue(url);
    const statusCol = headers.indexOf('Status') + 1;
    if (statusCol > 0) sheet.getRange(row, statusCol).setValue('filed');
    SpreadsheetApp.getActive().toast('Filed: ' + task.name, 'Asana', 5);
  } catch (err) {
    SpreadsheetApp.getActive().toast('Trigger error: ' + err.message, 'Error', 10);
    console.error(err);
  }
}

function buildAsanaTaskBody(t) {
  const lines = [];
  lines.push('Auto-filed from the escalation cluster tracker (' + t.themeId + ').');
  lines.push('');
  if (t.type) lines.push('Signal type: ' + t.type);
  if (t.dominantCluster) {
    lines.push('Dominant cluster: ' + t.dominantCluster + (t.dominantSubcluster ? ' > ' + t.dominantSubcluster : ''));
  }
  lines.push('');
  if (t.description) {
    lines.push('WHAT IS HAPPENING');
    lines.push(t.description);
    lines.push('');
  }
  if (t.ruleSummary) {
    lines.push('RULE / KNOWLEDGE THE BOT SHOULD KNOW');
    lines.push(t.ruleSummary);
    lines.push('');
  }
  if (t.suggestedChange) {
    lines.push('SUGGESTED PROMPT CHANGE');
    lines.push(t.suggestedChange);
    lines.push('');
  }
  lines.push('VOLUME');
  lines.push('Total observed: ' + t.totalVolume + ' across ' + t.weeksObserved + ' week(s)');
  if (t.latestWeekVolume) lines.push('Latest week: ' + t.latestWeekVolume);
  if (t.firstSeen && t.lastSeen) lines.push('First seen: ' + t.firstSeen + ' | Last seen: ' + t.lastSeen);
  lines.push('');
  if (t.samples) {
    lines.push('SAMPLE CONVERSATIONS');
    lines.push('See indices in the Findings tabs of the master tracker: ' + t.samples);
    lines.push('');
  }
  lines.push('Tracker sheet: ' + t.sheetUrl);
  return lines.join('\n');
}
