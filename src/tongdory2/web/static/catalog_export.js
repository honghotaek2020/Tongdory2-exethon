(() => {
  const dlg = document.getElementById("final-catalog-dlg");
  if (!dlg) return;

  const msgEl = document.getElementById("tongdory-messages");
  let MSG = {};
  if (msgEl) {
    try { MSG = JSON.parse(msgEl.textContent || "{}"); } catch (e) { MSG = {}; }
  }
  function M(id, params) {
    const tpl = MSG[id];
    if (tpl === undefined) return id;
    if (!params) return tpl;
    return tpl.replace(/\{(\w+)\}/g, (_, k) => (params[k] !== undefined ? String(params[k]) : ""));
  }

  const BAD_FILENAME_RE = /[\\/:*?"<>|\x00-\x1f]+/g;
  const fmtRadios = [...dlg.querySelectorAll('input[name="final_cat_fmt"]')];
  const nameInput = document.getElementById("final-cat-name");
  const dirInput = document.getElementById("final-cat-dir");
  const locEl = document.getElementById("final-cat-loc");
  const dirResetBtn = document.getElementById("final-cat-dir-reset");
  const previewDetails = document.getElementById("final-cat-preview-details");
  const previewEl = document.getElementById("final-cat-preview");
  const summaryEl = document.getElementById("final-cat-summary");
  const truncEl = document.getElementById("final-cat-trunc");
  const stateWarnEl = document.getElementById("final-cat-state-warn");
  const errEl = document.getElementById("final-cat-error");
  const submitBtn = document.getElementById("final-cat-submit");
  const statusEl = document.getElementById("final-catalog-save-status");
  const batchId = dlg.dataset.batch || "";

  // 폴더 선택 모달
  const browserDlg = document.getElementById("catalog-folder-browser");
  const pathLabel = document.getElementById("cat-browser-path");
  const list = document.getElementById("cat-browser-list");
  const browseErr = document.getElementById("cat-browser-error");
  const parentBtn = document.getElementById("cat-browser-parent");
  const useBtn = document.getElementById("cat-browser-use");
  let currentFolder = null;
  let browseAbort = null;

  async function openFolder(path) {
    browseAbort?.abort();
    const controller = new AbortController();
    browseAbort = controller;
    browseErr.textContent = "";
    useBtn.disabled = true;
    parentBtn.disabled = true;
    list.replaceChildren();
    try {
      const resp = await fetch("/setup/browse?path=" + encodeURIComponent(path || "~"), { signal: controller.signal });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error);
      currentFolder = data;
      pathLabel.textContent = data.path;
      for (const child of data.children) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = child.name;
        btn.addEventListener("click", () => openFolder(child.path));
        list.appendChild(btn);
      }
      useBtn.disabled = false;
      parentBtn.disabled = !data.parent;
    } catch (err) {
      if (err.name !== "AbortError") browseErr.textContent = err.message;
    }
  }

  document.getElementById("final-cat-browse").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const start = dirInput.value.trim().startsWith("~") ? dirInput.value.trim() : "~";
    btn.disabled = true;
    let picked = { fallback: true };
    try {
      const fd = new FormData();
      fd.set("initial", start);
      fd.set("multiple", "false");
      fd.set("prompt", "저장할 폴더를 선택하세요");
      const resp = await fetch("/setup/pick-folder", { method: "POST", body: fd });
      if (resp.ok) picked = await resp.json();
    } catch (e) {}
    btn.disabled = false;
    if (picked && picked.cancelled) return;
    if (picked && Array.isArray(picked.paths) && picked.paths.length) {
      dirInput.value = picked.paths[0];
      updateLocation();
      return;
    }
    browserDlg.showModal();
    openFolder(start);
  });
  parentBtn.addEventListener("click", () => currentFolder && openFolder(currentFolder.parent));
  useBtn.addEventListener("click", () => {
    if (currentFolder) {
      dirInput.value = currentFolder.path;
      updateLocation();
    }
    browserDlg.close();
  });
  document.getElementById("cat-browser-close").addEventListener("click", () => browserDlg.close());

  function selectedFmt() {
    return fmtRadios.find(r => r.checked)?.value || "md";
  }

  function cleanFilename(raw, ext) {
    let base = (raw || "").trim().replace(BAD_FILENAME_RE, "_").replace(/^[.\s]+|[.\s]+$/g, "");
    for (const s of [`.${ext}`, ".md", ".json", ".txt"]) {
      if (base.toLowerCase().endsWith(s)) { base = base.slice(0, -s.length).trim(); break; }
    }
    if (!base) base = nameInput.dataset.defaultStem || "정리결과";
    return `${base.slice(0, 120)}.${ext}`;
  }

  const dirLabelEl = document.getElementById("final-cat-dir-label");
  function updateLocation() {
    const ext = selectedFmt();
    const fname = cleanFilename(nameInput.value, ext);
    const chosenDir = dirInput.value.trim();
    if (dirLabelEl) dirLabelEl.textContent = chosenDir || "기본 다운로드 폴더";
    locEl.textContent = fname;
    dirResetBtn.hidden = !chosenDir;
  }

  let previewAbort = null;
  async function loadPreview() {
    previewAbort?.abort();
    const controller = new AbortController();
    previewAbort = controller;
    previewEl.textContent = M("catalog.modal.loading_preview");
    const params = new URLSearchParams({ fmt: selectedFmt() });
    if (batchId) params.set("batch", batchId);
    try {
      const resp = await fetch("/s4/catalog/preview?" + params.toString(), { signal: controller.signal });
      const text = await resp.text();
      if (!resp.ok) throw new Error(text || M("catalog.err.preview_failed"));
      previewEl.textContent = text;
    } catch (err) {
      if (err.name !== "AbortError") previewEl.textContent = err.message;
    }
  }

  const loadingText = M("catalog.modal.loading_summary");
  let summaryLoaded = summaryEl && summaryEl.textContent.trim() !== loadingText;
  async function loadSummary() {
    if (summaryLoaded || !summaryEl) return;
    const params = new URLSearchParams();
    if (batchId) params.set("batch", batchId);
    try {
      const qs = params.toString();
      const resp = await fetch("/s4/catalog/summary" + (qs ? "?" + qs : ""));
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || M("catalog.err.summary_failed"));
      summaryEl.textContent = data.summary_text || "";
      if (truncEl) {
        if (data.truncated_folders > 0) {
          truncEl.textContent = M("catalog.card.truncated", { count: data.truncated_folders });
          truncEl.hidden = false;
        } else {
          truncEl.hidden = true;
        }
      }
      if (stateWarnEl) {
        if (data.is_undone) {
          stateWarnEl.textContent = M("catalog.modal.undone_warn");
          stateWarnEl.hidden = false;
        } else if (data.is_partial_undo) {
          stateWarnEl.textContent = M("catalog.modal.partial_undo_warn", { count: data.undone_count });
          stateWarnEl.hidden = false;
        }
      }
      summaryLoaded = true;
    } catch (err) {
      summaryEl.textContent = err.message;
    }
  }

  document.querySelectorAll("[data-open-final-catalog]").forEach(btn => {
    btn.addEventListener("click", () => {
      errEl.hidden = true;
      errEl.textContent = "";
      const preset = btn.dataset.presetFmt;
      if (preset) {
        fmtRadios.forEach(r => { r.checked = r.value === preset; });
      }
      updateLocation();
      loadSummary();
      if (previewDetails.open) loadPreview();
      dlg.showModal();
    });
  });

  fmtRadios.forEach(r => r.addEventListener("change", () => {
    updateLocation();
    if (previewDetails.open) loadPreview();
  }));
  nameInput.addEventListener("input", updateLocation);
  dirResetBtn.addEventListener("click", () => { dirInput.value = ""; updateLocation(); });
  previewDetails.addEventListener("toggle", () => { if (previewDetails.open) loadPreview(); });
  document.getElementById("final-cat-close").addEventListener("click", () => dlg.close());
  document.getElementById("final-cat-cancel").addEventListener("click", () => dlg.close());

  submitBtn.addEventListener("click", async () => {
    errEl.hidden = true;
    errEl.textContent = "";
    submitBtn.disabled = true;
    const ext = selectedFmt();
    const chosenDir = dirInput.value.trim();
    const form = new FormData();
    form.append("fmt", ext);
    form.append("filename", nameInput.value);
    form.append("save_dir", chosenDir);
    if (batchId) form.append("batch", batchId);
    try {
      const resp = await fetch("/s4/catalog/export", { method: "POST", body: form });
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || M("catalog.err.export_failed"));
      }
      if (chosenDir) {
        const data = await resp.json();
        dlg.close();
        const msg = M("catalog.done.disk", { path: data.saved_path });
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
        dlg.close();
        const msg = M("catalog.done.download", { filename: fname });
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
