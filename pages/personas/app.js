const bridge = window.AstrBotPluginPage;

const listEl = document.getElementById("persona-list");
const nameEl = document.getElementById("persona-name");
const yamlEl = document.getElementById("yaml");
const outputEl = document.getElementById("output");
const previewOutputEl = document.getElementById("preview-output");
const sessionInput = document.getElementById("preview-session");
const panel = document.getElementById("preview-panel");
let currentName = "";

/* ── CodeMirror 编辑器 ──────────────────────────────────────── */

let cm;
try {
  cm = CodeMirror.fromTextArea(yamlEl, {
    mode: "yaml",
    theme: "material-darker",
    lineNumbers: true,
    lineWrapping: true,
    tabSize: 2,
    indentWithTabs: false,
    viewportMargin: Infinity,
  });
} catch (_err) {
  cm = null;
}

/* ── 辅助函数 ────────────────────────────────────────────── */

await bridge.ready();

function show(value, isError) {
  outputEl.className = isError ? "error" : "";
  if (typeof value === "string") {
    outputEl.textContent = value;
    return;
  }
  outputEl.textContent = JSON.stringify(value, null, 2);
}

function getYaml() {
  return cm ? cm.getValue() : yamlEl.value;
}

function setYaml(text) {
  if (cm) {
    cm.setValue(text);
    cm.clearHistory();
  } else {
    yamlEl.value = text;
  }
}

/* ── 列表 ───────────────────────────────────────────────── */

async function loadList() {
  try {
    const result = await bridge.apiGet("personas");
    const items = result?.items || [];
    listEl.innerHTML = "";
    for (const item of items) {
      const button = document.createElement("button");
      button.className = "item";
      button.type = "button";
      const title = document.createElement("strong");
      title.textContent = item.display_name || item.name || "";
      const meta = document.createElement("span");
      meta.textContent = `${item.name || ""} · ${item.file || ""}`;
      if (item.error) {
        meta.textContent += ` (${item.error})`;
        meta.style.color = "var(--danger, #e0556a)";
      }
      button.append(title, meta);
      button.addEventListener("click", () => loadPersona(item.name));
      listEl.appendChild(button);
    }
  } catch (err) {
    show(err.message || String(err), true);
  }
}

async function loadPersona(name) {
  try {
    const result = await bridge.apiGet(`persona/${encodeURIComponent(name)}`);
    nameEl.value = result.name;
    setYaml(result.content);
    currentName = result.name;
    show(`Loaded ${result.file}`);
  } catch (err) {
    show(err.message || String(err), true);
  }
}

/* ── 工具栏 ────────────────────────────────────────────── */

document.getElementById("new").addEventListener("click", () => {
  currentName = "";
  nameEl.value = "";
  setYaml(
    "name: new_persona\n" +
      "display_name: 新建人格\n" +
      'description: ""\n\n' +
      "activation:\n" +
      "  global_default: false\n" +
      "  session_bindings: []\n\n" +
      "segments:\n" +
      "  - id: identity\n" +
      "    role: SYSTEM\n" +
      "    depth: 0\n" +
      '    condition: ""\n' +
      "    template: |\n" +
      "      你是一个乐于助人的助手。\n",
  );
  outputEl.textContent = "新建人格草稿";
});

document.getElementById("refresh").addEventListener("click", loadList);

document.getElementById("save").addEventListener("click", async () => {
  const name = nameEl.value.trim();
  if (!name) {
    show("persona name is required", true);
    return;
  }
  try {
    const result = await bridge.apiPost(`persona/${encodeURIComponent(name)}`, {
      content: getYaml(),
    });
    show(`Saved ${result.name}`);
    currentName = result.name;
    nameEl.value = result.name;
    await loadList();
  } catch (err) {
    show(err.message || String(err), true);
  }
});

document.getElementById("rename").addEventListener("click", async () => {
  if (!currentName) {
    show("load a persona before renaming", true);
    return;
  }
  const newName = nameEl.value.trim();
  if (!newName) {
    show("new persona name is required", true);
    return;
  }
  try {
    const result = await bridge.apiPost(
      `persona/${encodeURIComponent(currentName)}/rename`,
      { new_name: newName },
    );
    show(`Renamed to ${result.name}`);
    currentName = result.name;
    nameEl.value = result.name;
    await loadPersona(result.name);
    await loadList();
  } catch (err) {
    show(err.message || String(err), true);
  }
});

document.getElementById("delete").addEventListener("click", async () => {
  const name = nameEl.value.trim();
  if (!name) {
    show("persona name is required", true);
    return;
  }
  try {
    const result = await bridge.apiPost(
      `persona/${encodeURIComponent(name)}/delete`,
      {},
    );
    show(`Deleted ${result.name}`);
    currentName = "";
    nameEl.value = "";
    setYaml("");
    await loadList();
  } catch (err) {
    show(err.message || String(err), true);
  }
});

/* ── 预览 ────────────────────────────────────────────── */

document.getElementById("preview").addEventListener("click", async () => {
  panel.style.display = "block";
  await doPreview();
});

document.getElementById("preview-refresh").addEventListener("click", doPreview);

async function doPreview() {
  const sid = sessionInput.value.trim() || undefined;
  const payload = { content: getYaml() };
  if (sid) {
    payload.variables = { session_id: sid };
  }
  try {
    const result = await bridge.apiPost("preview", payload);
    const { system_prompt, contexts, segments } = result;
    let html = "";

    html += '<div class="preview-section">';
    html += "<h3>系统提示词</h3>";
    html += `<pre class="preview-block">${esc(system_prompt) || "（空）"}</pre>`;
    html += "</div>";

    html += '<div class="preview-section">';
    html += "<h3>上下文 (" + (contexts?.length || 0) + ")</h3>";
    if (contexts) {
      for (const ctx of contexts) {
        html += `<pre class="preview-block msg-${ctx.role}"><span class="role-tag">${esc(ctx.role)}</span> ${esc(ctx.content)}</pre>`;
      }
    }
    html += "</div>";

    html += '<div class="preview-section">';
    html += "<h3>已渲染片段 (" + (segments?.length || 0) + ")</h3>";
    if (segments) {
      for (const seg of segments) {
        html += `<pre class="preview-block msg-${seg.role}"><span class="role-tag">${esc(seg.role)}</span> <span class="seg-id">[${esc(seg.id)} d=${seg.depth}]</span> ${esc(seg.content)}</pre>`;
      }
    }
    html += "</div>";

    previewOutputEl.innerHTML = html;
  } catch (err) {
    previewOutputEl.innerHTML = `<pre class="preview-block" style="color:var(--danger)">${esc(err.message || String(err))}</pre>`;
  }
}

function esc(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/* ── 初始化 ───────────────────────────────────────────────── */

panel.style.display = "none";
await loadList();
