/**
 * 🤖 Supervisor — Apps Script button layer over the sheet-agent control plane.
 *
 * BOUNDARY (openspec/specs/sheet-buttons-appsscript): this code runs in Google's
 * cloud. It can touch ONLY the spreadsheet — never the server FS, git or `claude`.
 * Its sole job is to APPEND intent rows to the `_control` tab. The Python daemon on
 * the server polls `_control` and does all the real work.
 *
 * CONTRACT (`_control`, openspec/specs/sheet-control-queue): columns A=id, B=ts,
 * C=action, D=args(JSON),
 * E=status, F=result. Apps Script owns A-E and sets status=`pending`; the daemon
 * owns E/F and sets the final status + result. Dialogs MUST NEVER write F.
 *
 * Git is the source of truth for this source. Deploy with `clasp push` — a manual
 * step that needs interactive OAuth, out of scope of the daemon.
 */

// --- contract constants (must mirror src/sheet_agent/config.py) -------------
var CONTROL_TAB = '_control';
var REPOS_TAB = '_repos';
var SKILLS_TAB = '_skills';
var CTL_PENDING = 'pending';

var ACTION_ADD_REPO = 'add_repo';
var ACTION_CREATE_REPO = 'create_repo';
var ACTION_RUN_SKILL = 'run_skill';
var ACTION_SHARE_REPOS = 'share_repos';

var META_PREFIX = '_';        // meta/reference tabs (never a repo tab)
var CHAT_TAB_PREFIX = '_chat ';

/**
 * Build the 🤖 Supervisor menu with EXACTLY three items wired to the three dialog
 * handlers. Runs automatically when the spreadsheet is opened.
 */
function onOpen(e) {
  SpreadsheetApp.getUi()
    .createMenu('🤖 Supervisor')
    .addItem('➕ Добавить репо', 'showAddRepoDialog')
    .addItem('🆕 Создать репо', 'showCreateRepoDialog')
    .addItem('▶️ Запустить скилл', 'showRunSkillDialog')
    .addItem('📤 Поделиться репо', 'showShareReposDialog')
    .addToUi();
}

/**
 * Append ONE row to `_control`, writing ONLY columns A-E (id, ts, action, args,
 * status=pending). Column F (result) is daemon-owned and is left blank — this
 * function never writes it. `id` is `<ts>-<rand>` (the §4.1 idempotency key).
 *
 * @param {string} action  one of add_repo|create_repo|run_skill
 * @param {Object} args     JSON-serializable args object
 * @return {string}         the generated control-row id
 */
function appendControlRow_(action, args) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(CONTROL_TAB);
  if (!sheet) {
    throw new Error('Контрольная вкладка "' + CONTROL_TAB + '" не найдена. ' +
      'Запусти демон (он создаёт _control) и обнови лист.');
  }
  var ts = new Date().toISOString();
  var rand = Math.random().toString(36).slice(2, 8);
  var id = ts + '-' + rand;
  // EXACTLY columns A-E. F (result) intentionally omitted — daemon owns it.
  sheet.appendRow([id, ts, action, JSON.stringify(args), CTL_PENDING]);
  return id;
}

/**
 * ➕ Добавить репо — read the repo reference list from `_repos!A2:C`, let the human
 * pick one or more repos, and append ONE `add_repo` control row per selected repo
 * with args {repo, path}. Never writes any repo tab's A/D/H/B1 — only `_control`.
 */
function showAddRepoDialog() {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var repos = ss.getSheetByName(REPOS_TAB);
  if (!repos) {
    ui.alert('Вкладка "' + REPOS_TAB + '" не найдена.');
    return;
  }
  var last = repos.getLastRow();
  if (last < 2) {
    ui.alert('В "' + REPOS_TAB + '" нет репозиториев (A2:C пусто).');
    return;
  }
  // _repos!A2:C — A=repo (last path segment / name), B=path, C=meta.
  var values = repos.getRange('A2:C' + last).getValues();
  var html = HtmlService.createHtmlOutput(buildAddRepoHtml_(values))
    .setWidth(420)
    .setHeight(460);
  ui.showModalDialog(html, '➕ Добавить репо');
}

/** HTML-escape a string for safe interpolation into the dialog markup. */
function escapeHtml_(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** Build the multi-select HTML for the add-repo dialog from `_repos!A2:C` rows. */
function buildAddRepoHtml_(rows) {
  var items = '';
  for (var i = 0; i < rows.length; i++) {
    var repo = String(rows[i][0] || '').trim();
    var path = String(rows[i][1] || '').trim();
    // The daemon's add_repo handler REQUIRES a path (it errors on a path-less row),
    // so a row without one would fail silently in the queue — skip it here.
    if (!path) {
      continue;
    }
    var label = repo || path;
    // value carries both repo + path so the client posts the exact arg shape; the
    // label/path are escaped so a name with quotes/angle-brackets can't break the
    // attribute or inject markup.
    var val = escapeHtml_(JSON.stringify({ repo: repo, path: path }));
    items += '<label style="display:block;margin:4px 0">' +
      '<input type="checkbox" name="repo" value="' + val + '"> ' +
      escapeHtml_(label) + ' <span style="color:#888">' + escapeHtml_(path) + '</span></label>';
  }
  return '' +
    '<div style="font-family:sans-serif;font-size:13px">' +
    '<p>Выбери репозитории для привязки (по одной строке-намерению на каждый):</p>' +
    '<form id="f">' + items + '</form>' +
    '<div style="margin-top:12px">' +
    '<button onclick="submit_()">Добавить</button> ' +
    '<button onclick="google.script.host.close()">Отмена</button>' +
    '</div>' +
    '<script>' +
    'function submit_(){' +
    ' var boxes=document.getElementsByName("repo");' +
    ' var picked=[];' +
    ' for(var i=0;i<boxes.length;i++){if(boxes[i].checked){picked.push(JSON.parse(boxes[i].value));}}' +
    ' if(!picked.length){alert("Ничего не выбрано");return;}' +
    ' google.script.run' +
    '   .withSuccessHandler(function(){google.script.host.close();})' +
    '   .withFailureHandler(function(e){alert("Ошибка: "+e.message);})' +
    '   .enqueueAddRepos(picked);' +
    '}' +
    '</script>' +
    '</div>';
}

/**
 * Server-side callback from the add-repo dialog: append ONE `add_repo` control row
 * per selected repo, args {repo, path}.
 * NOTE: callable from google.script.run, so the name MUST NOT end with `_`
 * (a trailing underscore marks a function PRIVATE in Apps Script — the client
 * bridge silently refuses to call it).
 * @param {Array<{repo:string,path:string}>} picked
 */
function enqueueAddRepos(picked) {
  for (var i = 0; i < picked.length; i++) {
    appendControlRow_(ACTION_ADD_REPO, { repo: picked[i].repo, path: picked[i].path });
  }
}

/**
 * 🆕 Создать репо — collect name/template/vision in a dialog, then (on the client)
 * confirm `Создать beelink-<name>?` BEFORE posting. The server append happens only
 * inside enqueueCreateRepo_, which is reached only after the confirm — the
 * irreversibility gate (§4.5).
 */
function showCreateRepoDialog() {
  var html = HtmlService.createHtmlOutput(buildCreateRepoHtml_())
    .setWidth(440)
    .setHeight(480);
  SpreadsheetApp.getUi().showModalDialog(html, '🆕 Создать репо');
}

/** Build the create-repo form. The confirm text includes `Создать beelink-` (gate). */
function buildCreateRepoHtml_() {
  return '' +
    '<div style="font-family:sans-serif;font-size:13px">' +
    '<form id="f">' +
    '<p>Имя (голое, без префикса). Везде станет <code>beelink-&lt;name&gt;</code>:</p>' +
    '<input id="name" style="width:100%" placeholder="foo">' +
    '<p>Шаблон:</p>' +
    '<select id="template" style="width:100%"><option value="init_project">init_project</option></select>' +
    '<p>Product Vision:</p>' +
    '<textarea id="vision" style="width:100%;height:120px" placeholder="Что и для кого строим"></textarea>' +
    '</form>' +
    '<div style="margin-top:12px">' +
    '<button onclick="submit_()">Создать</button> ' +
    '<button onclick="google.script.host.close()">Отмена</button>' +
    '</div>' +
    '<script>' +
    'function submit_(){' +
    ' var name=document.getElementById("name").value.trim();' +
    ' var template=document.getElementById("template").value;' +
    ' var vision=document.getElementById("vision").value;' +
    ' if(!name){alert("Введи имя");return;}' +
    // Irreversibility gate: explicit confirm whose text includes "Создать beelink-".
    ' if(!confirm("Создать beelink-"+name+"? Действие необратимо.")){return;}' +
    ' google.script.run' +
    '   .withSuccessHandler(function(){google.script.host.close();})' +
    '   .withFailureHandler(function(e){alert("Ошибка: "+e.message);})' +
    '   .enqueueCreateRepo(name,template,vision);' +
    '}' +
    '</script>' +
    '</div>';
}

/**
 * Server-side callback from the create-repo dialog, reached ONLY after the client
 * `Создать beelink-<name>?` confirm. Append ONE `create_repo` control row with
 * args {name, template, vision}.
 * NOTE: callable from google.script.run → name MUST NOT end with `_` (trailing
 * underscore = PRIVATE in Apps Script; the client bridge won't call it).
 */
function enqueueCreateRepo(name, template, vision) {
  appendControlRow_(ACTION_CREATE_REPO, { name: name, template: template, vision: vision });
}

/**
 * ▶️ Запустить скилл — read the skills catalog from `_skills!A2:B`, let the human pick
 * ONE skill (with optional free-text extra context) to run against the ACTIVE repo tab,
 * and append ONE `run_skill` control row with args {skill, tab, detail}. The active
 * sheet's title is captured here (dialog-open time) so the modal callback posts the
 * exact tab the human is on. Writes only to `_control` — never a repo tab's A/D/H/B1.
 */
function showRunSkillDialog() {
  var ui = SpreadsheetApp.getUi();
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tab = ss.getActiveSheet().getName();
  var skillsSheet = ss.getSheetByName(SKILLS_TAB);
  if (!skillsSheet) {
    ui.alert('Вкладка "' + SKILLS_TAB + '" не найдена. Запусти демон (он создаёт ' +
      SKILLS_TAB + ') и обнови лист.');
    return;
  }
  var last = skillsSheet.getLastRow();
  if (last < 2) {
    ui.alert('В "' + SKILLS_TAB + '" нет скилов (A2:B пусто).');
    return;
  }
  // _skills!A2:B — A=skill name (picker value), B=description.
  var values = skillsSheet.getRange('A2:B' + last).getValues();
  var html = HtmlService.createHtmlOutput(buildRunSkillHtml_(values, tab))
    .setWidth(440)
    .setHeight(440);
  ui.showModalDialog(html, '▶️ Запустить скилл');
}

/** Build the run-skill form: a skill dropdown (+description), an optional context box. */
function buildRunSkillHtml_(rows, tab) {
  var options = '';
  for (var i = 0; i < rows.length; i++) {
    var name = String(rows[i][0] || '').trim();
    if (!name) {
      continue;
    }
    var desc = String(rows[i][1] || '').trim();
    var label = desc ? (name + ' — ' + desc) : name;
    options += '<option value="' + escapeHtml_(name) + '">' + escapeHtml_(label) + '</option>';
  }
  return '' +
    '<div style="font-family:sans-serif;font-size:13px">' +
    '<p>Запустить скилл на вкладке <b>' + escapeHtml_(tab) + '</b>:</p>' +
    '<form id="f">' +
    '<select id="skill" style="width:100%">' + options + '</select>' +
    '<p>Доп. контекст (необязательно):</p>' +
    '<textarea id="detail" style="width:100%;height:120px" placeholder="Что именно сделать / на что обратить внимание"></textarea>' +
    '</form>' +
    '<div style="margin-top:12px">' +
    '<button onclick="submit_()">Запустить</button> ' +
    '<button onclick="google.script.host.close()">Отмена</button>' +
    '</div>' +
    '<script>' +
    'var TAB=' + JSON.stringify(tab) + ';' +
    'function submit_(){' +
    ' var skill=document.getElementById("skill").value;' +
    ' var detail=document.getElementById("detail").value;' +
    ' if(!skill){alert("Выбери скилл");return;}' +
    ' google.script.run' +
    '   .withSuccessHandler(function(){google.script.host.close();})' +
    '   .withFailureHandler(function(e){alert("Ошибка: "+e.message);})' +
    '   .enqueueRunSkill(skill,TAB,detail);' +
    '}' +
    '</script>' +
    '</div>';
}

/**
 * Server-side callback from the run-skill dialog: append ONE `run_skill` control row
 * with args {skill, tab, detail}.
 * NOTE: callable from google.script.run → name MUST NOT end with `_` (trailing
 * underscore = PRIVATE in Apps Script; the client bridge won't call it).
 */
function enqueueRunSkill(skill, tab, detail) {
  appendControlRow_(ACTION_RUN_SKILL, { skill: skill, tab: tab, detail: detail });
}

/**
 * List the repo bindings on the master sheet: for every non-meta tab (skipping
 * `_`-prefixed and `_chat ` tabs) read its B1 binding. Returns [{tab, binding}] for
 * tabs that carry a binding — exactly the repos `share_repos` can validate against
 * (its daemon handler checks each against a master tab's B1).
 */
function listRepoBindings_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheets = ss.getSheets();
  var out = [];
  for (var i = 0; i < sheets.length; i++) {
    var name = sheets[i].getName();
    if (name.indexOf(META_PREFIX) === 0 || name.indexOf(CHAT_TAB_PREFIX) === 0) {
      continue;  // _repos / _control / _skills / _friends / _chat … — not repo tabs
    }
    var binding = String(sheets[i].getRange('B1').getValue() || '').trim();
    if (binding) {
      out.push({ tab: name, binding: binding });
    }
  }
  return out;
}

/**
 * 📤 Поделиться репо — pick a recipient e-mail and one or more repos to share into a
 * NEW partner file. Appends ONE `share_repos` control row; the daemon mints + shares
 * the file and records it in `_friends`. Writes only to `_control` — never a repo tab.
 */
function showShareReposDialog() {
  var ui = SpreadsheetApp.getUi();
  var repos = listRepoBindings_();
  if (!repos.length) {
    ui.alert('Нет привязанных репозиториев (ни на одной вкладке нет B1).');
    return;
  }
  var html = HtmlService.createHtmlOutput(buildShareReposHtml_(repos))
    .setWidth(460)
    .setHeight(520);
  ui.showModalDialog(html, '📤 Поделиться репо');
}

/** Build the share-repos form: recipient e-mail, repo multi-select, autonomy select. */
function buildShareReposHtml_(repos) {
  var items = '';
  for (var i = 0; i < repos.length; i++) {
    var val = escapeHtml_(repos[i].binding);
    items += '<label style="display:block;margin:4px 0">' +
      '<input type="checkbox" name="repo" value="' + val + '"> ' +
      escapeHtml_(repos[i].tab) +
      ' <span style="color:#888">' + escapeHtml_(repos[i].binding) + '</span></label>';
  }
  return '' +
    '<div style="font-family:sans-serif;font-size:13px">' +
    '<p>E-mail партнёра (кому расшарить новый файл):</p>' +
    '<input id="recipient" style="width:100%" placeholder="partner@example.com">' +
    '<p style="margin-top:10px">Какие репозитории дать (можно несколько):</p>' +
    '<form id="f">' + items + '</form>' +
    '<p style="margin-top:10px">Автономия friend-листа:</p>' +
    '<select id="autonomy" style="width:100%">' +
    '<option value="">по умолчанию (gated — ревью владельца)</option>' +
    '<option value="gated">gated — партнёр ставит задачу, владелец ревьюит</option>' +
    '<option value="ship">ship — полная автономия (доверенный партнёр)</option>' +
    '</select>' +
    '<div style="margin-top:12px">' +
    '<button onclick="submit_()">Поделиться</button> ' +
    '<button onclick="google.script.host.close()">Отмена</button>' +
    '</div>' +
    '<script>' +
    'function submit_(){' +
    ' var recipient=document.getElementById("recipient").value.trim();' +
    ' if(!recipient){alert("Введи e-mail партнёра");return;}' +
    ' var autonomy=document.getElementById("autonomy").value;' +
    ' var boxes=document.getElementsByName("repo");' +
    ' var picked=[];' +
    ' for(var i=0;i<boxes.length;i++){if(boxes[i].checked){picked.push(boxes[i].value);}}' +
    ' if(!picked.length){alert("Выбери хотя бы один репозиторий");return;}' +
    ' google.script.run' +
    '   .withSuccessHandler(function(){google.script.host.close();})' +
    '   .withFailureHandler(function(e){alert("Ошибка: "+e.message);})' +
    '   .enqueueShareRepos(recipient,picked,autonomy);' +
    '}' +
    '</script>' +
    '</div>';
}

/**
 * Server-side callback from the share-repos dialog: append ONE `share_repos` control
 * row with args {recipient, repos, autonomy}. `repos` is the array of picked B1
 * bindings; `autonomy` is '' (daemon default) or one of spec|code|ship|gated.
 * NOTE: callable from google.script.run → name MUST NOT end with `_`.
 */
function enqueueShareRepos(recipient, repos, autonomy) {
  appendControlRow_(ACTION_SHARE_REPOS, {
    recipient: recipient, repos: repos, autonomy: autonomy || ''
  });
}
