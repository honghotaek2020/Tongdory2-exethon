function _loadMessages() {
  const el = document.getElementById("tongdory-messages");
  if (!el) return {};
  try { return JSON.parse(el.textContent || "{}"); } catch (e) { return {}; }
}
const _MSG = _loadMessages();
function M(id, params) {
  const tpl = _MSG[id];
  if (tpl === undefined) return id;
  if (!params) return tpl;
  return tpl.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? String(params[k]) : ""));
}

(() => {
  const dialog = document.getElementById("folder-browser");
  const pathLabel = document.getElementById("browser-path");
  const list = document.getElementById("browser-list");
  const error = document.getElementById("browser-error");
  const parent = document.getElementById("browser-parent");
  const choose = document.getElementById("browser-use");
  let input;
  let current;
  let request;

  function setPath(field, path) {
    if (!field) return;
    const previous = field.value;
    if (field.tagName === "TEXTAREA") {
      const paths = field.value.split("\n").map(value => value.trim()).filter(Boolean);
      if (!paths.includes(path)) paths.push(path);
      field.value = paths.join("\n");
    } else {
      field.value = path;
    }
    if (field.value !== previous) field.dispatchEvent(new Event("input", { bubbles: true }));
  }

  async function openPath(path) {
    request?.abort();
    const controller = new AbortController();
    request = controller;
    error.textContent = "";
    choose.disabled = true;
    parent.disabled = true;
    list.replaceChildren();
    try {
      const response = await fetch("/setup/browse?path=" + encodeURIComponent(path || "~"), { signal: controller.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      current = data;
      pathLabel.textContent = data.path;
      for (const child of data.children) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = child.name;
        button.addEventListener("click", () => openPath(child.path));
        list.appendChild(button);
      }
      choose.disabled = false;
      parent.disabled = !data.parent;
    } catch (err) {
      if (err.name !== "AbortError") error.textContent = err.message;
    }
  }

  function finishPicked(target, paths) {
    if (!target || !paths || !paths.length) return;
    for (const p of paths) setPath(target, p);
    // #42 기존 폴더를 고르면 바로 목록을 가져온다 (편집 중인 목록이 있으면 확인창을 띄운다)
    if (target.id === "reference" && target.form) {
      if (window.__tongdorySubmitPreview) {
        window.__tongdorySubmitPreview(target.form, M("setup.confirm.replace_folder"));
      } else {
        target.form.requestSubmit();
      }
      return;
    }
    if (target.type !== "hidden") target.focus();
  }

  async function pickNativeFolder(initial, multiple, promptText) {
    try {
      const fd = new FormData();
      fd.set("initial", initial || "");
      fd.set("multiple", multiple ? "true" : "false");
      if (promptText) fd.set("prompt", promptText);
      const resp = await fetch("/setup/pick-folder", { method: "POST", body: fd });
      if (!resp.ok) return { fallback: true };
      return await resp.json();
    } catch (e) {
      return { fallback: true };
    }
  }

  if (dialog) {
    document.querySelectorAll("[data-browse]").forEach(button => {
      button.addEventListener("click", async () => {
        input = document.getElementById(button.dataset.browse);
        const start = input && input.tagName !== "TEXTAREA" && input.value.trim().startsWith("~") ? input.value.trim() : "~";
        const multi = button.dataset.browseMulti === "1";
        button.disabled = true;
        const picked = await pickNativeFolder(start, multi, button.textContent.trim() || "폴더를 선택하세요");
        button.disabled = false;
        if (picked && picked.cancelled) return;
        if (picked && Array.isArray(picked.paths) && picked.paths.length) {
          finishPicked(input, picked.paths);
          return;
        }
        dialog.showModal();
        openPath(start);
      });
    });
    parent.addEventListener("click", () => openPath(current.parent));
    choose.addEventListener("click", () => {
      dialog.close();
      finishPicked(input, [current.path]);
    });
    document.getElementById("browser-close").addEventListener("click", () => dialog.close());
  }

  document.querySelectorAll("[data-add]").forEach(button => {
    button.addEventListener("click", () => setPath(document.getElementById("source-paths"), button.dataset.add));
  });
})();

// #12 저장하지 않은 입력 보존: [이전]·상황판·잘못된 파일 가져오기 오류 뒤에도 입력을 잃지 않게
(function () {
  const fields = ["structure-list", "source-paths", "destination-path"]
    .map(id => document.getElementById(id)).filter(Boolean);
  const key = f => "setup-draft:" + f.id;
  const read = f => { try { return sessionStorage.getItem(key(f)); } catch (e) { return null; } };
  const write = (f, v) => { try { v === null ? sessionStorage.removeItem(key(f)) : sessionStorage.setItem(key(f), v); } catch (e) {} };
  const error = document.querySelector(".notice.error, .setup-error, [role=alert]:not(#save-struct-error)");
  const listCard = document.getElementById("setup-list-card");
  const imported = listCard?.dataset.imported === "1";
  const preserveDraft = listCard?.dataset.preserveDraft === "1";
  fields.forEach(f => {
    const saved = f.value;
    const draft = read(f);
    if (preserveDraft && f.id === "structure-list") {
      write(f, f.value ? f.value : null);
    } else if (error || (imported && f.id === "structure-list")) {
      write(f, null);
    } else if (draft !== null && draft !== saved) {
      const bar = document.createElement("div");
      bar.className = "notice";
      bar.textContent = M("setup.draft.notice");
      const use = document.createElement("button");
      use.type = "button"; use.textContent = M("setup.draft.restore");
      use.addEventListener("click", () => {
        f.value = draft;
        f.dispatchEvent(new Event("input", { bubbles: true }));
        bar.remove();
      });
      const drop = document.createElement("button");
      drop.type = "button"; drop.className = "link-btn"; drop.textContent = M("setup.draft.discard");
      drop.addEventListener("click", () => { write(f, null); bar.remove(); });
      bar.append(use, " ", drop);
      f.parentNode.insertBefore(bar, f);
    }
    f.addEventListener("input", () => {
      if (preserveDraft && f.id === "structure-list") write(f, f.value ? f.value : null);
      else write(f, f.value === saved ? null : f.value);
    });
    if (f.form) f.form.addEventListener("submit", () => write(f, null));
  });
})();

// 1단계 폴더 목록: 교체 확인·편집본 보존, 서버 일치 경로 검증, 실시간 트리 미리보기, 단순 저장 창(방식 1)
(function () {
  const textarea = document.getElementById("structure-list");
  if (!textarea) return;

  const REQ_RE = /\s*`?\[필수\]`?\s*$/;
  const BULLET_RE = /^[-*+]\s+/;
  const HR_RE = /^([-*_])\1{2,}\s*$/;
  const BAD_SEG_RE = /[\\:*?"<>|\x00-\x1f]/;
  const BAD_FILENAME_RE = /[\\/:*?"<>|\x00-\x1f]+/g;
  const MAX_DEPTH = 8;
  const MAX_NODES = 1000;
  const SKIP_DIRS = new Set([
    ".git", "node_modules", "build", "dist", "target", ".venv", "venv",
    "__pycache__", ".idea", ".gradle", ".next", ".nuxt", "coverage",
  ]);
  const MESSY_RE = /^(tmp|temp|backup|bak|old|misc|etc|임시|백업|새\s*폴더(\s*\(\d+\))?)$/i;
  const SRC_KEY_RE = /^(?:\d+[._\-\s]*)?(src|source|소스|소스코드)$/i;

  const listCard = document.getElementById("setup-list-card");
  if (listCard && listCard.dataset.imported === "1") {
    listCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setTimeout(() => {
      listCard.classList.remove("is-fresh");
      document.getElementById("tree-panel")?.classList.remove("is-fresh");
    }, 2400);
  }

  // 기존 입력이 있을 때 교체 전 확인창
  const confirmDlg = document.getElementById("import-confirm-dlg");
  const confirmMsg = document.getElementById("import-confirm-msg");
  const confirmOk = document.getElementById("import-confirm-ok");
  const confirmCancel = document.getElementById("import-confirm-cancel");
  const confirmClose = document.getElementById("import-confirm-close");
  let pendingConfirm = null;
  let pendingCancel = null;

  function closeConfirm(accepted) {
    const fn = accepted ? pendingConfirm : pendingCancel;
    pendingConfirm = null;
    pendingCancel = null;
    if (confirmDlg?.open) confirmDlg.close();
    if (fn) fn();
  }

  if (confirmDlg) {
    confirmOk.addEventListener("click", () => closeConfirm(true));
    confirmCancel.addEventListener("click", () => closeConfirm(false));
    confirmClose.addEventListener("click", () => closeConfirm(false));
    confirmDlg.addEventListener("cancel", e => { e.preventDefault(); closeConfirm(false); });
  }

  function askReplace(message, onConfirm, onCancel) {
    if (!textarea.value.trim() || !confirmDlg) {
      onConfirm();
      return;
    }
    confirmMsg.textContent = message + " " + M("setup.confirm.keep_note");
    pendingConfirm = onConfirm;
    pendingCancel = onCancel || null;
    confirmDlg.showModal();
  }

  function syncCurrentPaths(form) {
    const hidden = form.querySelector('input[name="current_paths"]');
    if (hidden) hidden.value = textarea.value;
    try {
      if (textarea.value) sessionStorage.setItem("setup-draft:structure-list", textarea.value);
    } catch (e) {}
  }

  function submitPreviewForm(form, message, onCancel) {
    if (!form) return;
    askReplace(message, () => {
      form.dataset.confirmedReplace = "1";
      syncCurrentPaths(form);
      form.requestSubmit();
    }, onCancel);
  }
  window.__tongdorySubmitPreview = submitPreviewForm;

  const refForm = document.getElementById("reference-import-form");
  const docForm = document.getElementById("document-import-form");
  const docInput = document.getElementById("document");

  [refForm, docForm].filter(Boolean).forEach(form => {
    form.addEventListener("submit", e => {
      syncCurrentPaths(form);
      if (form.dataset.confirmedReplace === "1") {
        delete form.dataset.confirmedReplace;
        return;
      }
      if (textarea.value.trim()) {
        e.preventDefault();
        const msg = form === docForm
          ? M("setup.confirm.replace_file_generic")
          : M("setup.confirm.replace_folder");
        submitPreviewForm(form, msg, () => {
          if (form === docForm && docInput) docInput.value = "";
        });
      }
    });
  });

  if (docInput && docForm) {
    const chooseFile = document.getElementById("choose-structure-file");
    if (chooseFile) {
      docInput.hidden = true;
      chooseFile.hidden = false;
      chooseFile.addEventListener("click", () => docInput.click());
    }
    docInput.addEventListener("change", () => {
      if (!docInput.files || docInput.files.length === 0) return;
      const fname = docInput.files[0].name;
      submitPreviewForm(
        docForm,
        M("setup.confirm.replace_file", { filename: fname }),
        () => { docInput.value = ""; }
      );
    });
  }

  function allowedSegment(seg) {
    if (!seg || seg.startsWith(".") || BAD_SEG_RE.test(seg)) return false;
    if (SKIP_DIRS.has(seg.toLowerCase())) return false;
    if (MESSY_RE.test(seg.trim())) return false;
    if (SRC_KEY_RE.test(seg.trim())) return false;
    return true;
  }

  function normalizeLine(raw) {
    let text = raw.replace(/^\ufeff/, "").trim();
    let required = false;
    if (REQ_RE.test(text)) {
      required = true;
      text = text.replace(REQ_RE, "").trim();
    }
    text = text.replace(/^`+|`+$/g, "").trim();
    while (text.endsWith("/")) text = text.slice(0, -1).trimEnd();
    if (!text) return { path: "", parts: [], required };
    const parts = text.split("/").map(p => p.trim());
    return { path: parts.join("/"), parts, required };
  }

  function parseEntries(rawText) {
    const ordered = new Map();
    const seen = new Map();
    const nodes = new Map();
    const errors = [];
    let maxDepth = 0;
    const rawLines = rawText.split(/\r?\n/);

    for (let idx = 0; idx < rawLines.length; idx++) {
      const line = rawLines[idx];
      if (!line.trim()) continue;
      const lineno = idx + 1;
      const { path, parts, required } = normalizeLine(line);
      if (!path || path.length > 240 || parts.length > MAX_DEPTH || !parts.every(allowedSegment)) {
        errors.push(M("setup.err.invalid_line", { lineno, raw: line.trim().slice(0, 80) }));
        continue;
      }
      if (seen.has(path)) {
        errors.push(M("setup.err.duplicate_line", { lineno, first: seen.get(path), path }));
        continue;
      }
      seen.set(path, lineno);
      ordered.set(path, required);
      if (parts.length > maxDepth) maxDepth = parts.length;
      for (let i = 1; i <= parts.length; i++) {
        const sub = parts.slice(0, i).join("/");
        nodes.set(sub, Boolean(nodes.get(sub)) || (i === parts.length && required));
      }
      if (nodes.size > MAX_NODES) {
        errors.push(M("setup.err.max_nodes"));
        break;
      }
    }

    const parents = new Set();
    for (const p of ordered.keys()) {
      const parts = p.split("/");
      for (let i = 1; i < parts.length; i++) parents.add(parts.slice(0, i).join("/"));
    }
    const compact = [];
    for (const [path, required] of ordered.entries()) {
      if (!parents.has(path) || required) compact.push({ path, required });
    }
    return { compact, nodes, errors, maxDepth };
  }

  function tryConvertMarkdownPaste(pasted) {
    const lines = pasted.replace(/^\ufeff/, "").split(/\r?\n/);
    const hasIndentedBullet = lines.some(l => /^ {2,}[-*+]\s+/.test(l));
    if (!hasIndentedBullet) return null;
    const rawEntries = [];
    let stack = [];
    let unit = null;
    let inFence = false;
    for (const raw of lines) {
      const stripped = raw.trim();
      if (stripped.startsWith("```") || stripped.startsWith("~~~")) { inFence = !inFence; continue; }
      if (inFence || !stripped || stripped.startsWith("#") || stripped.startsWith(">") || stripped.startsWith("|") || HR_RE.test(stripped)) continue;
      const leading = raw.slice(0, raw.length - raw.trimStart().length);
      if (leading.includes("\t")) return null;
      const indent = leading.length;
      if (!BULLET_RE.test(stripped)) continue;
      const itemRaw = stripped.replace(BULLET_RE, "");
      let depth = 0;
      if (indent > 0) {
        if (!stack.length) return null;
        if (unit === null) {
          if (indent !== 2 && indent !== 4) return null;
          unit = indent;
        }
        if (indent % unit !== 0) return null;
        depth = indent / unit;
        if (depth > stack.length) return null;
      }
      const { path, parts, required } = normalizeLine(itemRaw);
      if (!path || parts.some(p => !p)) continue;
      if (depth === 0) {
        stack = [path];
        rawEntries.push({ path, required });
      } else {
        stack = stack.slice(0, depth).concat([path]);
        rawEntries.push({ path: stack.join("/"), required });
      }
    }
    if (!rawEntries.length) return null;
    const ordered = new Map();
    for (const e of rawEntries) ordered.set(e.path, Boolean(ordered.get(e.path)) || e.required);
    const parents = new Set();
    for (const p of ordered.keys()) {
      const parts = p.split("/");
      for (let i = 1; i < parts.length; i++) parents.add(parts.slice(0, i).join("/"));
    }
    const out = [];
    for (const [path, required] of ordered.entries()) {
      if (!parents.has(path) || required) out.push(required ? `${path} [필수]` : path);
    }
    return out.join("\n");
  }

  textarea.addEventListener("paste", e => {
    const text = e.clipboardData?.getData("text/plain");
    if (!text) return;
    const converted = tryConvertMarkdownPaste(text);
    if (!converted) return;
    e.preventDefault();
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? textarea.value.length;
    textarea.value = textarea.value.slice(0, start) + converted + textarea.value.slice(end);
    textarea.selectionStart = textarea.selectionEnd = start + converted.length;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    if (window.showToast) window.showToast(M("setup.toast.md_converted"));
  });

  const sourceEl = document.getElementById("structure-source");
  const totalEl = document.getElementById("structure-total");
  const requiredEl = document.getElementById("structure-required");
  const liveErrEl = document.getElementById("structure-live-error");
  const depthChip = document.getElementById("tree-depth-chip");
  const linesChip = document.getElementById("tree-lines-chip");
  const panelBody = document.getElementById("tree-panel-body");
  const emptyMsg = document.getElementById("tree-empty-msg");
  const panelFoot = document.getElementById("tree-panel-foot");
  const filtersBar = document.getElementById("tree-filters");
  const zoomBtn = document.getElementById("open-tree-zoom");

  function renderLiveTree() {
    const { compact, nodes, errors, maxDepth } = parseEntries(textarea.value);
    const totalFolders = nodes.size;
    const reqCount = [...nodes.values()].filter(Boolean).length;
    if (sourceEl) sourceEl.hidden = !totalFolders || !sourceEl.textContent.trim();
    if (totalEl) {
      totalEl.textContent = totalFolders
        ? M("setup.list.total_folders", { count: totalFolders })
        : M("setup.list.total_empty");
      totalEl.hidden = false;
    }
    if (requiredEl) {
      requiredEl.textContent = M("setup.list.required_count", { count: reqCount });
      requiredEl.hidden = !totalFolders;
    }
    if (depthChip) {
      depthChip.textContent = M("setup.tree.depth", { depth: maxDepth });
      depthChip.hidden = !totalFolders;
    }
    if (linesChip) {
      linesChip.textContent = M("setup.tree.lines", { lines: compact.length });
      linesChip.hidden = !totalFolders;
    }
    if (liveErrEl) {
      if (errors.length > 0) {
        liveErrEl.textContent = errors.length === 1
          ? errors[0]
          : M("setup.err.more_errors", { first: errors[0], extra: errors.length - 1 });
        liveErrEl.hidden = false;
      } else {
        liveErrEl.textContent = "";
        liveErrEl.hidden = true;
      }
    }
    if (!panelBody) return;
    const sortedPaths = [...nodes.keys()].sort((a, b) => a.localeCompare(b));
    const hasChild = new Set();
    for (const p of sortedPaths) {
      const idx = p.lastIndexOf("/");
      if (idx > 0) hasChild.add(p.slice(0, idx));
    }
    const frag = document.createDocumentFragment();
    for (const p of sortedPaths) {
      const parts = p.split("/");
      const depth = parts.length - 1;
      const isLeaf = !hasChild.has(p);
      const isReq = Boolean(nodes.get(p));
      const row = document.createElement("div");
      row.className = "trow";
      row.dataset.treePath = p;
      row.dataset.new = "0";
      row.dataset.changed = "0";
      row.dataset.depth = String(depth);
      row.style.paddingLeft = `${depth * 14}px`;

      const nameSpan = document.createElement(isLeaf ? "span" : "button");
      if (!isLeaf) { nameSpan.type = "button"; nameSpan.setAttribute("aria-expanded", "true"); }
      nameSpan.className = "tname";
      if (!isLeaf) nameSpan.style.fontWeight = "700";
      const arrowSpan = document.createElement("span");
      arrowSpan.className = "tarrow";
      arrowSpan.setAttribute("aria-hidden", "true");
      arrowSpan.textContent = !isLeaf ? "▾ " : (depth ? "└ " : "· ");
      nameSpan.appendChild(arrowSpan);
      nameSpan.append(parts[parts.length - 1]);
      if (isReq) {
        nameSpan.append(" ");
        const chip = document.createElement("span");
        chip.className = "chip ok";
        chip.textContent = "필수";
        nameSpan.appendChild(chip);
      }
      row.appendChild(nameSpan);
      frag.appendChild(row);
    }
    panelBody.replaceChildren(frag);
    const hasAny = sortedPaths.length > 0;
    panelBody.hidden = !hasAny;
    if (emptyMsg) emptyMsg.hidden = hasAny;
    if (panelFoot) panelFoot.hidden = !hasAny;
    if (filtersBar) filtersBar.hidden = !hasAny;
    if (zoomBtn) zoomBtn.hidden = !hasAny;
    document.querySelectorAll("[data-tree-total]").forEach(el => {
      el.textContent = M("setup.tree.folder_count", { count: sortedPaths.length });
    });
    document.dispatchEvent(new Event("tongdory:tree-reset"));
  }

  textarea.addEventListener("input", () => renderLiveTree());
  textarea.addEventListener("change", () => renderLiveTree());
  renderLiveTree();

  const fillBtn = document.getElementById("fill-default-template");
  if (fillBtn) {
    fillBtn.addEventListener("click", () => {
      const tpl = fillBtn.dataset.template || "";
      if (textarea.value.trim() === tpl.trim()) {
        if (window.showToast) window.showToast(M("setup.toast.template_same"));
        return;
      }
      askReplace(M("setup.confirm.replace_template"), () => {
        textarea.value = tpl;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        if (window.showToast) window.showToast(M("setup.toast.template_filled"));
      });
    });
  }

  // 단순 저장 창 (방식 1)
  const saveDlg = document.getElementById("save-structure-dlg");
  const openSaveBtn = document.getElementById("open-structure-save");
  if (!saveDlg || !openSaveBtn) return;

  const fmtRadios = [...saveDlg.querySelectorAll('input[name="save_struct_fmt"]')];
  const nameInput = document.getElementById("save-struct-name");
  const dirInput = document.getElementById("save-struct-dir");
  const locEl = document.getElementById("save-struct-loc");
  const dirResetBtn = document.getElementById("save-struct-dir-reset");
  const summaryEl = document.getElementById("save-struct-summary");
  const previewEl = document.getElementById("save-struct-preview");
  const errEl = document.getElementById("save-struct-error");
  const submitBtn = document.getElementById("save-struct-submit");
  const statusEl = document.getElementById("structure-save-status");

  function selectedFmt() {
    return fmtRadios.find(r => r.checked)?.value || "json";
  }

  function cleanFilename(raw, ext) {
    let base = (raw || "").trim().replace(BAD_FILENAME_RE, "_").replace(/^[.\s]+|[.\s]+$/g, "");
    for (const s of [`.${ext}`, ".json", ".txt", ".md"]) {
      if (base.toLowerCase().endsWith(s)) { base = base.slice(0, -s.length).trim(); break; }
    }
    if (!base) base = nameInput.dataset.defaultStem || "파일 통돌이_폴더목록";
    return `${base.slice(0, 120)}.${ext}`;
  }

  function updateModalState() {
    const ext = selectedFmt();
    const fname = cleanFilename(nameInput.value, ext);
    const chosenDir = dirInput.value.trim();
    locEl.textContent = chosenDir
      ? M("setup.save.modal.loc_custom", { folder: chosenDir, filename: fname })
      : M("setup.save.modal.loc_default", { filename: fname });
    dirResetBtn.hidden = !chosenDir;

    const { compact, nodes, errors } = parseEntries(textarea.value);
    const reqCount = compact.filter(e => e.required).length;
    summaryEl.textContent = M("setup.save.modal.summary", {
      lines: compact.length,
      folders: nodes.size,
      required: reqCount,
    });
    if (ext === "json") {
      previewEl.textContent = JSON.stringify({
        format: "tongdory-folder-list",
        version: 1,
        folders: compact.map(e => ({ path: e.path, required: e.required })),
      }, null, 2) + "\n";
    } else {
      previewEl.textContent = compact.map(e => e.required ? `${e.path} [필수]` : e.path).join("\n") + "\n";
    }
    if (errors.length > 0) {
      errEl.textContent = errors[0];
      errEl.hidden = false;
    } else {
      errEl.hidden = true;
      errEl.textContent = "";
    }
  }

  openSaveBtn.addEventListener("click", () => {
    if (!textarea.value.trim()) {
      if (window.showToast) window.showToast(M("setup.save.empty_warn"));
      textarea.focus();
      return;
    }
    updateModalState();
    saveDlg.showModal();
  });

  fmtRadios.forEach(r => r.addEventListener("change", updateModalState));
  nameInput.addEventListener("input", updateModalState);
  dirInput.addEventListener("input", updateModalState);
  dirResetBtn.addEventListener("click", () => { dirInput.value = ""; updateModalState(); });
  document.getElementById("save-struct-close").addEventListener("click", () => saveDlg.close());
  document.getElementById("save-struct-cancel").addEventListener("click", () => saveDlg.close());

  submitBtn.addEventListener("click", async () => {
    errEl.hidden = true;
    errEl.textContent = "";
    submitBtn.disabled = true;
    const ext = selectedFmt();
    const chosenDir = dirInput.value.trim();
    const form = new FormData();
    form.append("paths", textarea.value);
    form.append("fmt", ext);
    form.append("filename", nameInput.value);
    form.append("save_dir", chosenDir);
    try {
      const resp = await fetch("/setup/structure/export", { method: "POST", body: form });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || M("setup.err.save_failed"));
      }
      if (chosenDir) {
        const data = await resp.json();
        saveDlg.close();
        const msg = M("setup.save.done.disk", { path: data.saved_path });
        if (statusEl) { statusEl.textContent = msg; statusEl.hidden = false; }
        if (window.showToast) window.showToast(msg);
      } else {
        const blob = await resp.blob();
        const fname = cleanFilename(nameInput.value, ext);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        saveDlg.close();
        const msg = M("setup.save.done.download", { filename: fname });
        if (statusEl) { statusEl.textContent = msg; statusEl.hidden = false; }
        if (window.showToast) window.showToast(msg);
      }
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
