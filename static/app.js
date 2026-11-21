let summaryData = null;
let sourcesList = [];
let wasJobActive = false;
let lastJobType = null;

const MONTH_NAMES = [
  "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
  "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
];

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 MB";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let val = bytes;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(i >= 2 ? 1 : 0)} ${units[i]}`;
}

function formatNum(n) {
  return Number(n || 0).toLocaleString("es-ES");
}

function formatDate(ts) {
  if (!ts || ts <= 0) return "Sin fecha";
  const d = new Date(ts * 1000);
  return d.toLocaleString("es-ES", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit"
  });
}

async function apiGet(url) {
  const res = await fetch(url);
  return res.json();
}

async function apiPost(url, payload = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Ocurrió un error al procesar la solicitud.");
    throw new Error(data.error || "Request failed");
  }
  return data;
}

async function pollStatus() {
  try {
    const st = await apiGet("/api/status");
    const job = st.job;

    const exBadge = document.getElementById("exiftool-badge");
    if (st.has_exiftool) {
      exBadge.textContent = "✓ ExifTool Activo";
      exBadge.className = "badge badge-ok";
    }

    const zipInput = document.getElementById("zip-path-input");
    if (!zipInput.value && st.takeout_root_path) {
      zipInput.value = st.takeout_root_path;
    }

    const folderInput = document.getElementById("folder-path-input");
    if (!folderInput.value && st.last_custom_folder_path) {
      folderInput.value = st.last_custom_folder_path;
    }

    if (st.last_icloud_scan) {
      const dt = new Date(Number(st.last_icloud_scan) * 1000).toLocaleTimeString("es-ES");
      document.getElementById("icloud-last-scan").innerHTML =
        `✅ Catálogo leído a las <strong>${dt}</strong> (${st.last_icloud_method || "OK"})`;
    }

    // Mounted Volumes pills (/Volumes/...)
    const volBar = document.getElementById("volumes-bar-zip");
    if (st.volumes && st.volumes.length > 0 && volBar.children.length === 0) {
      volBar.innerHTML = st.volumes.map(v =>
        `<span class="vol-pill" onclick="document.getElementById('zip-path-input').value='${v.replace(/'/g, "\\'")}'">🖴 ${v.split('/').pop()}</span>`
      ).join("");
    }

    if (job.logs && job.logs.length > 0) {
      const logBox = document.getElementById("live-logs");
      logBox.textContent = job.logs.join("\n");
      logBox.scrollTop = logBox.scrollHeight;
    }

    const banner = document.getElementById("job-banner");
    if (job.active || job.status === "paused") {
      banner.classList.remove("hidden");
      const labels = {
        icloud_scan: "Paso 1 · Escaneando iCloud Photos",
        takeout_scan: "Apartado 2A · Analizando ZIPs de Takeout",
        folder_scan: "Apartado 2B · Analizando Carpeta sin Comprimir",
        matching: "Comparando con tu iCloud",
        batch: "Subiendo / Procesando Tanda"
      };
      document.getElementById("job-type-label").textContent = labels[job.type] || "Procesando";
      document.getElementById("job-message").textContent = job.message || "";
      document.getElementById("job-counter").textContent = `${job.percent}%`;
      document.getElementById("job-progress-bar").style.width = `${job.percent}%`;

      const btnPause = document.getElementById("btn-pause");
      const btnResume = document.getElementById("btn-resume");
      if (job.control === "pause" || job.status === "paused") {
        btnPause.classList.add("hidden");
        btnResume.classList.remove("hidden");
      } else {
        btnPause.classList.remove("hidden");
        btnResume.classList.add("hidden");
      }
      wasJobActive = true;
      lastJobType = job.type;
    } else {
      if (wasJobActive) {
        wasJobActive = false;
        await loadSources();
        await refreshSummary();
        await loadMissingFiles(1);
        await loadBatches();
        await loadImportedGallery(1);

        if (job.status === "error" && job.error) {
          alert(`Atención: ${job.error}`);
        } else if (lastJobType === "batch" && job.result && job.result.batch_id) {
          await showCelebrationBanner(job.result);
        }
      }
      banner.classList.add("hidden");
    }
  } catch (e) {
    // Ignore transient network poll error
  }
}

async function showCelebrationBanner(batchResult) {
  const modal = document.getElementById("batch-finished-modal");
  const title = document.getElementById("celeb-title");
  const sub = document.getElementById("celeb-subtitle");
  const gallery = document.getElementById("celeb-gallery");

  title.textContent = `🎉 ¡Tanda #${batchResult.batch_id} Finalizada! (${batchResult.succeeded} archivos importados)`;
  sub.textContent = `Aquí tienes las fotos y vídeos que se acaban de procesar con su fecha original restaurada:`;

  const data = await apiGet(`/api/missing-items?batch_id=${batchResult.batch_id}&page=1&page_size=24`);
  gallery.innerHTML = renderMediaCardsHtml(data.items || []);
  modal.classList.remove("hidden");
  modal.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeCelebration() {
  document.getElementById("batch-finished-modal").classList.add("hidden");
}

async function loadSources() {
  const data = await apiGet("/api/sources");
  sourcesList = data.sources || [];

  const zipSources = sourcesList.filter(s => s.source_type === "zip");
  const folderSources = sourcesList.filter(s => s.source_type === "folder");

  const zipBox = document.getElementById("zip-sources-list");
  if (zipSources.length === 0) {
    zipBox.innerHTML = `<div style="font-size:12px;color:#64748b;">Ningún archivo .ZIP añadido aún.</div>`;
  } else {
    zipBox.innerHTML = zipSources.map(s => {
      const name = s.source_path.split("/").pop();
      return `
        <div class="source-row">
          <div class="source-row-info" title="${s.source_path}">
            <strong>📦 ${name}</strong>
            <div class="source-row-meta">
              ${formatNum(s.total_items)} arch. · ❌ ${formatNum(s.missing_items)} faltan · ✅ ${formatNum(s.matched_items)} ya en iCloud
            </div>
          </div>
          <button class="btn btn-outline btn-sm" onclick="removeSource('${s.source_path.replace(/'/g, "\\'")}')" title="Quitar de la lista">✕</button>
        </div>
      `;
    }).join("");
  }

  const folderBox = document.getElementById("folder-sources-list");
  if (folderSources.length === 0) {
    folderBox.innerHTML = `<div style="font-size:12px;color:#64748b;">Ninguna carpeta suelta añadida aún.</div>`;
  } else {
    folderBox.innerHTML = folderSources.map(s => {
      const name = s.source_path.split("/").filter(Boolean).pop() || s.source_path;
      return `
        <div class="source-row">
          <div class="source-row-info" title="${s.source_path}">
            <strong>📂 ${name}</strong>
            <div class="source-row-meta">
              ${formatNum(s.total_items)} arch. · ❌ ${formatNum(s.missing_items)} faltan · ✅ ${formatNum(s.matched_items)} ya en iCloud
            </div>
          </div>
          <button class="btn btn-outline btn-sm" onclick="removeSource('${s.source_path.replace(/'/g, "\\'")}')" title="Quitar de la lista">✕</button>
        </div>
      `;
    }).join("");
  }

  // Update source filter dropdown in explorer
  const srcSelect = document.getElementById("inspector-source");
  const prevVal = srcSelect.value;
  srcSelect.innerHTML = `<option value="">Todos los orígenes (${sourcesList.length})</option>` +
    sourcesList.map(s => {
      const icon = s.source_type === "zip" ? "📦" : "📂";
      const name = s.source_path.split("/").filter(Boolean).pop();
      return `<option value="${s.source_path}">${icon} ${name} (${s.missing_items} faltan)</option>`;
    }).join("");
  if (prevVal) srcSelect.value = prevVal;
}

async function removeSource(sourcePath) {
  if (!confirm(`¿Quitar este origen del análisis?\n${sourcePath}\n\n(No se borrará ningún archivo de tu disco).`)) return;
  await apiPost("/api/remove-source", { source_path: sourcePath });
  await loadSources();
  await refreshSummary();
  await loadMissingFiles(1);
}

async function pickFolder(inputId) {
  const res = await apiPost("/api/pick-folder", {
    prompt: "Selecciona la carpeta en tu Mac, Disco Externo o NAS"
  });
  if (res.status === "ok" && res.path) {
    document.getElementById(inputId).value = res.path;
  }
}

async function pickZipFile() {
  const res = await apiPost("/api/pick-zip-file", {
    prompt: "Selecciona el archivo .ZIP de Google Takeout"
  });
  if (res.status === "ok" && res.path) {
    document.getElementById("zip-path-input").value = res.path;
  }
}

async function scanZips() {
  const p = document.getElementById("zip-path-input").value.trim();
  if (!p) {
    alert("Elige primero un archivo .ZIP o una carpeta con ZIPs de Google Takeout.");
    return;
  }
  await apiPost("/api/scan-zips", { root_path: p, force_rescan: false });
  pollStatus();
}

async function scanCustomFolder() {
  const p = document.getElementById("folder-path-input").value.trim();
  if (!p) {
    alert("Elige primero la carpeta con imágenes o vídeos sin comprimir usando el botón 'Elegir Carpeta en Finder'.");
    return;
  }
  const sub = document.getElementById("folder-subfolders").checked;
  await apiPost("/api/scan-custom-folder", { folder_path: p, include_subfolders: sub });
  pollStatus();
}

async function openSqliteHelp() {
  await apiPost("/api/open-photos-sqlite-help");
  alert(
    "Se han abierto dos carpetas en Finder:\n\n" +
    "1) La carpeta 'database' de tu biblioteca de Fotos.\n" +
    "2) La carpeta 'icloud_snapshot' de este proyecto.\n\n" +
    "Mantén pulsada la tecla Alt (⌥) y arrastra 'Photos.sqlite' a 'icloud_snapshot' para hacer una copia de lectura instantánea."
  );
}

async function openOutputFinder() {
  await apiPost("/api/open-finder", {});
}

async function openPhotosApp() {
  await apiPost("/api/open-photos-app", {});
}

async function revealFile(filePath) {
  await apiPost("/api/reveal-file", { path: filePath });
}

async function scanICloud(method) {
  await apiPost("/api/scan-icloud", { method });
  pollStatus();
}

async function controlJob(action) {
  await apiPost("/api/job-control", { action });
  pollStatus();
}

async function refreshSummary() {
  summaryData = await apiGet("/api/summary");

  document.getElementById("kpi-icloud-total").textContent = formatNum(summaryData.icloud.total);
  document.getElementById("kpi-icloud-sub").textContent =
    `${formatNum(summaryData.icloud.photos)} fotos · ${formatNum(summaryData.icloud.videos)} vídeos`;

  document.getElementById("kpi-takeout-total").textContent = formatNum(summaryData.takeout.total);
  document.getElementById("kpi-takeout-sub").textContent =
    `${sourcesList.length} origen(es) añadido(s)`;

  document.getElementById("kpi-matched").textContent = formatNum(summaryData.takeout.matched);

  document.getElementById("kpi-missing").textContent = formatNum(summaryData.takeout.missing);
  document.getElementById("kpi-missing-sub").textContent =
    `${formatNum(summaryData.takeout.missing_photos)} fotos · ${formatNum(summaryData.takeout.missing_videos)} vídeos (${formatBytes(summaryData.takeout.missing_bytes)})`;

  document.getElementById("kpi-completed").textContent = formatNum(summaryData.takeout.completed);
  document.getElementById("tab-imported-count").textContent = formatNum(summaryData.takeout.completed);

  populateFilterDropdowns();
  renderYearMonthTable();
}

function populateFilterDropdowns() {
  if (!summaryData) return;
  const batchSelect = document.getElementById("batch-ym");
  const inspSelect = document.getElementById("inspector-ym");
  const prevBatch = batchSelect.value;
  const prevInsp = inspSelect.value;

  let batchHtml = `<option value="ALL">Todos los meses con faltantes (${formatNum(summaryData.takeout.missing)} archivos)</option>`;
  let inspHtml = `<option value="ALL">Todos los años y meses</option>`;

  for (const yObj of summaryData.by_year) {
    const yKey = yObj.year === "Sin Fecha" ? "Sin-Fecha" : `YEAR:${yObj.year}`;
    if (yObj.missing > 0) {
      batchHtml += `<option value="${yKey}">📅 Todo el Año ${yObj.year} (${formatNum(yObj.missing)} faltantes · ${formatBytes(yObj.missing_bytes)})</option>`;
    }
    if (yObj.takeout_total > 0) {
      inspHtml += `<option value="${yKey}">📅 Año ${yObj.year} (${formatNum(yObj.missing)} faltan / ${formatNum(yObj.takeout_total)} total)</option>`;
    }

    for (const mObj of yObj.months) {
      if (mObj.missing > 0) {
        batchHtml += `<option value="${mObj.year_month}">   ↳ ${mObj.year_month} (${formatNum(mObj.missing)} faltantes: ${mObj.missing_photos}📷, ${mObj.missing_videos}🎬)</option>`;
      }
      if (mObj.takeout_total > 0) {
        inspHtml += `<option value="${mObj.year_month}">   ↳ ${mObj.year_month} (${formatNum(mObj.missing)} faltan)</option>`;
      }
    }
  }

  batchSelect.innerHTML = batchHtml;
  inspSelect.innerHTML = inspHtml;
  if (prevBatch) batchSelect.value = prevBatch;
  if (prevInsp) inspSelect.value = prevInsp;
}

function renderYearMonthTable() {
  const container = document.getElementById("ym-container");
  if (!summaryData || !summaryData.by_year || summaryData.by_year.length === 0) {
    container.innerHTML = `<p class="empty-msg">Añade un archivo .ZIP en el Apartado 2A o una Carpeta en el Apartado 2B para ver la comparativa.</p>`;
    return;
  }

  const onlyMissing = document.getElementById("only-missing-months").checked;
  let html = "";

  for (const yObj of summaryData.by_year) {
    if (onlyMissing && yObj.missing === 0) continue;

    const yId = `year-body-${yObj.year}`;
    const yFilterVal = yObj.year === "Sin Fecha" ? "Sin-Fecha" : `YEAR:${yObj.year}`;
    const missingBadge = yObj.missing > 0
      ? `<span class="pill-stat pill-missing">Faltan ${formatNum(yObj.missing)} (${formatNum(yObj.missing_photos)} 📷 / ${formatNum(yObj.missing_videos)} 🎬 · ${formatBytes(yObj.missing_bytes)})</span>`
      : `<span class="pill-stat pill-complete">✓ Todo en iCloud</span>`;

    html += `
      <div class="year-block">
        <div class="year-header" onclick="toggleYear('${yId}')">
          <div class="year-title">
            <span>▾ Año ${yObj.year}</span>
            ${missingBadge}
          </div>
          <div class="year-badges">
            <span class="pill-stat">☁️ En iCloud: <strong>${formatNum(yObj.icloud_total)}</strong></span>
            <span class="pill-stat">📦 Analizados: <strong>${formatNum(yObj.takeout_total)}</strong></span>
            <span class="pill-stat">✅ Coinciden: <strong>${formatNum(yObj.matched)}</strong></span>
            ${yObj.done > 0 ? `<span class="pill-stat pill-complete">🎉 Ya Importados: ${formatNum(yObj.done)}</span>` : ""}
            ${yObj.missing > 0 ? `
              <button class="btn btn-success btn-sm" onclick="event.stopPropagation(); quickBatch('${yFilterVal}')">
                🚀 Subir Año ${yObj.year}
              </button>
            ` : ""}
          </div>
        </div>
        <div id="${yId}" class="months-table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th>Mes</th>
                <th>Estado en iCloud</th>
                <th>Tienes en iCloud</th>
                <th>En tus ZIPs/Carpetas</th>
                <th>Ya Coinciden</th>
                <th>Faltan por Subir</th>
                <th>Ya Importados</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody>
    `;

    for (const m of yObj.months) {
      if (onlyMissing && m.missing === 0) continue;
      const mName = m.month > 0 ? `${m.year_month} (${MONTH_NAMES[m.month]})` : m.year_month;

      let diagHtml = `<span class="status-ok">✓ Completo</span>`;
      if (m.missing > 0 && m.icloud_total === 0) {
        diagHtml = `<span class="status-empty-icloud">🔴 No tienes este mes en iCloud</span>`;
      } else if (m.missing > 0) {
        diagHtml = `<span class="status-partial">🟡 Faltan ${formatNum(m.missing)} archivos</span>`;
      }

      html += `
        <tr>
          <td><strong>${mName}</strong></td>
          <td>${diagHtml}</td>
          <td>${formatNum(m.icloud_total)} <small style="color:#94a3b8">(${m.icloud_photos}📷/${m.icloud_videos}🎬)</small></td>
          <td>${formatNum(m.takeout_total)}</td>
          <td>${formatNum(m.matched)}</td>
          <td>
            ${m.missing > 0
              ? `<strong style="color:#fda4af">${formatNum(m.missing)}</strong> <small>(${m.missing_photos}📷 / ${m.missing_videos}🎬 · ${formatBytes(m.missing_bytes)})</small>`
              : `<span style="color:#6ee7b7">0</span>`}
          </td>
          <td>${m.done > 0 ? `<strong style="color:#34d399">${formatNum(m.done)}</strong>` : "0"}</td>
          <td>
            <div class="btn-group">
              <button class="btn btn-outline btn-sm" onclick="inspectMonth('${m.year_month}')">
                🖼️ Ver Fotos
              </button>
              ${m.missing > 0 ? `
                <button class="btn btn-primary btn-sm" onclick="quickBatch('${m.year_month}')">
                  🚀 Subir Mes
                </button>
              ` : ""}
            </div>
          </td>
        </tr>
      `;
    }

    html += `
            </tbody>
          </table>
        </div>
      </div>
    `;
  }

  container.innerHTML = html || `<p class="empty-msg">¡No hay ningún mes con fotos o vídeos pendientes! Desmarca la casilla superior si quieres ver todos los años de tu iCloud.</p>`;
}

function toggleYear(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle("hidden");
}

function goToMissingGallery() {
  document.getElementById("inspector-status").value = "missing";
  switchTab("tab-files", document.getElementById("tabbtn-missing"));
}

function goToImportedGallery() {
  switchTab("tab-imported", document.getElementById("tabbtn-imported"));
}

function inspectMonth(ym) {
  document.getElementById("inspector-ym").value = ym;
  document.getElementById("inspector-status").value = "missing";
  switchTab("tab-files", document.getElementById("tabbtn-missing"));
}

async function quickBatch(ymValue) {
  const batchSelect = document.getElementById("batch-ym");
  batchSelect.value = ymValue;
  const limit = document.getElementById("batch-limit").value;
  const mode = document.getElementById("batch-mode").value;
  const modeLabel = mode === "import_photos_app"
    ? "Meter directo en la App Fotos de tu Mac (Sube fotos y vídeos a iCloud)"
    : "Extraer en carpeta en tu Mac con fecha EXIF restaurada";

  if (confirm(`¿Empezar tanda para ${ymValue}?\n\n• Acción: ${modeLabel}\n• Tamaño máximo de tanda: ${limit === "0" ? "Todos" : limit + " archivos"}`)) {
    await startBatchFromForm();
  }
}

async function startBatchFromForm() {
  const ym = document.getElementById("batch-ym").value;
  const kind = document.getElementById("batch-kind").value;
  const limit = Number(document.getElementById("batch-limit").value);
  const mode = document.getElementById("batch-mode").value;

  await apiPost("/api/start-batch", {
    mode,
    year_month: ym,
    kind,
    batch_limit: limit,
    folder_chunk_size: 500
  });
  pollStatus();
}

function renderMediaCardsHtml(items) {
  if (!items || items.length === 0) {
    return `<p class="empty-msg">No hay fotos ni vídeos en esta vista.</p>`;
  }

  return items.map(item => {
    const kindBadge = item.kind === "video" ? "🎬 Vídeo" : "📷 Foto";
    let statusBadge = "";
    if (item.status === "missing") {
      statusBadge = `<span class="media-status-badge badge-missing">Falta en iCloud</span>`;
    } else if (item.status === "imported") {
      statusBadge = `<span class="media-status-badge badge-imported">✓ En App Fotos</span>`;
    } else if (item.status === "exported") {
      statusBadge = `<span class="media-status-badge badge-imported">✓ Extraído</span>`;
    } else if (item.status === "matched") {
      statusBadge = `<span class="media-status-badge badge-matched">Ya en iCloud</span>`;
    }

    const srcName = escapeHtml(item.source_path.split("/").filter(Boolean).pop());
    const srcLabel = item.source_type === "zip" ? `📦 ${srcName}` : `📂 ${srcName}`;
    const safeFn = escapeHtml(item.filename);
    const safeInner = escapeHtml(item.inner_path);
    const revealBtn = item.exported_path
      ? `<button class="btn btn-outline btn-sm" style="margin-top:6px;width:100%" onclick="revealFile('${item.exported_path.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}')">📂 Mostrar en Finder</button>`
      : "";

    return `
      <div class="media-card">
        <div class="media-thumb-wrap">
          <img src="/api/thumbnail?id=${item.id}" loading="lazy" alt="${safeFn}" onerror="this.style.display='none'" />
          <span class="media-kind-badge">${kindBadge} · ${formatBytes(item.filesize)}</span>
          ${statusBadge}
        </div>
        <div class="media-info">
          <div class="media-filename" title="${safeFn}">${safeFn}</div>
          <div class="media-date">📅 ${formatDate(item.timestamp)}</div>
          <div class="media-source" title="${safeInner}">${srcLabel} (${escapeHtml(item.date_source)})</div>
          ${item.match_reason && item.status === "matched" ? `<div style="font-size:11px;color:#34d399;">${escapeHtml(item.match_reason)}</div>` : ""}
          ${revealBtn}
        </div>
      </div>
    `;
  }).join("");
}

async function loadMissingFiles(page = 1) {
  const status = document.getElementById("inspector-status").value;
  const source = document.getElementById("inspector-source").value;
  const ym = document.getElementById("inspector-ym").value;
  const kind = document.getElementById("inspector-kind").value;

  const data = await apiGet(
    `/api/missing-items?status=${encodeURIComponent(status)}&source_path=${encodeURIComponent(source)}&year_month=${encodeURIComponent(ym)}&kind=${encodeURIComponent(kind)}&page=${page}&page_size=36`
  );

  document.getElementById("inspector-gallery").innerHTML = renderMediaCardsHtml(data.items || []);

  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  const pag = document.getElementById("inspector-pagination");
  pag.innerHTML = data.total > 0 ? `
    <span>Mostrando página ${data.page} de ${totalPages} (${formatNum(data.total)} archivos)</span>
    <div class="btn-group">
      ${data.page > 1 ? `<button class="btn btn-outline btn-sm" onclick="loadMissingFiles(${data.page - 1})">← Anterior</button>` : ""}
      ${data.page < totalPages ? `<button class="btn btn-outline btn-sm" onclick="loadMissingFiles(${data.page + 1})">Siguiente →</button>` : ""}
    </div>
  ` : "";
}

async function loadImportedGallery(page = 1) {
  const batchId = document.getElementById("imported-batch-filter").value;
  const url = batchId
    ? `/api/missing-items?batch_id=${encodeURIComponent(batchId)}&page=${page}&page_size=36`
    : `/api/missing-items?status=imported_or_exported&page=${page}&page_size=36`;

  const data = await apiGet(url);
  const gallery = document.getElementById("imported-gallery");
  if (!data.items || data.items.length === 0) {
    gallery.innerHTML = `<p class="empty-msg">Todavía no has importado ninguna tanda. Cuando lances una tanda, aquí verás las miniaturas de todas las fotos y vídeos importados.</p>`;
    document.getElementById("imported-pagination").innerHTML = "";
    return;
  }

  gallery.innerHTML = renderMediaCardsHtml(data.items);
  const totalPages = Math.max(1, Math.ceil(data.total / data.page_size));
  document.getElementById("imported-pagination").innerHTML = `
    <span>Mostrando ${data.items.length} de ${formatNum(data.total)} archivos importados (Página ${data.page}/${totalPages})</span>
    <div class="btn-group">
      ${data.page > 1 ? `<button class="btn btn-outline btn-sm" onclick="loadImportedGallery(${data.page - 1})">← Anterior</button>` : ""}
      ${data.page < totalPages ? `<button class="btn btn-outline btn-sm" onclick="loadImportedGallery(${data.page + 1})">Siguiente →</button>` : ""}
    </div>
  `;
}

async function loadBatches() {
  const data = await apiGet("/api/batches");
  const batches = data.batches || [];
  const box = document.getElementById("batches-list");
  const filterSelect = document.getElementById("imported-batch-filter");
  const prevFilter = filterSelect.value;

  filterSelect.innerHTML = `<option value="">Todas las tandas realizadas</option>` +
    batches.map(b => `<option value="${b.id}">Tanda #${b.id} · ${b.name} (${b.success_items} importados)</option>`).join("");
  if (prevFilter) filterSelect.value = prevFilter;

  if (batches.length === 0) {
    box.innerHTML = `<p class="empty-msg">Aún no se ha ejecutado ninguna tanda.</p>`;
    return;
  }

  box.innerHTML = batches.map(b => `
    <div class="batch-item">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <strong>Tanda #${b.id} · ${b.name}</strong>
        <button class="btn btn-outline btn-sm" onclick="filterImportedByBatch(${b.id})">🖼️ Ver fotos de esta tanda</button>
      </div>
      <div style="font-size:12.5px;color:#94a3b8;margin-top:4px;">
        Álbum en Fotos: <strong>${b.album_name || "Carpeta local"}</strong> · Estado: <strong>${b.status.toUpperCase()}</strong>
      </div>
      <div style="font-size:13px;margin-top:6px;">
        Progreso: <strong>${b.processed_items} / ${b.total_items}</strong>
        (✅ ${b.success_items} importados · ❌ ${b.failed_items} errores)
      </div>
    </div>
  `).join("");
}

function filterImportedByBatch(batchId) {
  document.getElementById("imported-batch-filter").value = String(batchId);
  loadImportedGallery(1);
}

function switchTab(tabId, btnEl) {
  document.querySelectorAll(".tab-content").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".tab-btn").forEach(el => el.classList.remove("active"));
  document.getElementById(tabId).classList.add("active");
  if (btnEl) btnEl.classList.add("active");
  if (tabId === "tab-files") loadMissingFiles(1);
  if (tabId === "tab-imported") {
    loadBatches();
    loadImportedGallery(1);
  }
  if (tabId === "tab-geotag") {
    loadGeotagOverview();
  }
}

// ==================== GEOTAGGING TAB LOGIC ====================
async function loadGeotagOverview() {
  try {
    const res = await fetch("/api/geotag/overview");
    if (!res.ok) return;
    const data = await res.json();
    const elAnchors = document.getElementById("geo-kpi-anchors");
    const elToNoGps = document.getElementById("geo-kpi-takeout-nogps");
    const elToSub = document.getElementById("geo-kpi-takeout-sub");
    const elIcNoGps = document.getElementById("geo-kpi-icloud-nogps");
    const elIcSub = document.getElementById("geo-kpi-icloud-sub");

    if (elAnchors) elAnchors.textContent = (data.total_gps_anchors || 0).toLocaleString();
    if (elToNoGps) elToNoGps.textContent = (data.takeout_without_gps || 0).toLocaleString();
    if (elToSub && data.takeout_inferred > 0) {
      elToSub.textContent = `${data.takeout_inferred.toLocaleString()} ya geolocalizadas`;
    }
    if (elIcNoGps) elIcNoGps.textContent = (data.icloud_without_gps || 0).toLocaleString();
    if (elIcSub && data.icloud_inferred > 0) {
      elIcSub.textContent = `${data.icloud_inferred.toLocaleString()} ya geolocalizadas`;
    }
  } catch (e) {
    console.error("Error loading geotag overview:", e);
  }
}

async function runGeotagSimulation() {
  const windowMins = parseInt(document.getElementById("geo-window-select").value, 10) || 45;
  const scope = document.getElementById("geo-scope-select").value || "both";
  const tbody = document.getElementById("geo-preview-table-body");
  tbody.innerHTML = `<tr><td colspan="6" style="padding: 20px; text-align: center; color: #38bdf8;"><span class="spinner"></span> Calculando correlación espacial y temporal entre fotos...</td></tr>`;

  try {
    const res = await fetch("/api/geotag/simulate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ window_minutes: windowMins, scope: scope })
    });
    if (!res.ok) throw new Error("Error en la simulación");
    const data = await res.json();

    const simBox = document.getElementById("geo-sim-box");
    simBox.classList.remove("hidden");

    let desc = "";
    if (scope === "both" || scope === "takeout") {
      desc += `<strong>Takeout / Carpetas:</strong> ${data.takeout_matchable.toLocaleString()} de ${data.takeout_total.toLocaleString()} fotos pueden geolocalizarse. `;
    }
    if (scope === "both" || scope === "icloud") {
      desc += `<br><strong>Apple Fotos (Mac):</strong> ${data.icloud_matchable.toLocaleString()} de ${data.icloud_total.toLocaleString()} fotos sin ubicación pueden situarse en el mapa.`;
    }
    document.getElementById("geo-sim-desc").innerHTML = desc;

    document.getElementById("geo-preview-count").textContent = `${(data.sample_matches || []).length} ejemplos mostrados`;

    if (!data.sample_matches || data.sample_matches.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" style="padding: 20px; text-align: center; color: #f59e0b;">No se encontraron fotos sin GPS en este rango temporal (prueba a ampliar a ±60 min o ±120 min).</td></tr>`;
      return;
    }

    tbody.innerHTML = data.sample_matches.map(m => `
      <tr style="border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 0.9rem;">
        <td style="padding: 10px; font-weight: 500;">${escapeHtml(m.filename)}</td>
        <td style="padding: 10px; color: #94a3b8;">${m.date_str}</td>
        <td style="padding: 10px;">
          <a href="https://www.google.com/maps?q=${m.assigned_lat},${m.assigned_lon}" target="_blank" style="color: #38bdf8; text-decoration: underline;" title="Ver en Google Maps">
            📍 ${m.assigned_lat.toFixed(4)}, ${m.assigned_lon.toFixed(4)}
          </a>
        </td>
        <td style="padding: 10px;"><span class="badge" style="background: rgba(16,185,129,0.15); color: #34d399;">±${m.delta_min} min</span></td>
        <td style="padding: 10px; color: #cbd5e1;"><small>${escapeHtml(m.desc)}</small></td>
        <td style="padding: 10px;"><span class="badge">${m.target_scope}</span></td>
      </tr>
    `).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" style="padding: 20px; text-align: center; color: #ef4444;">Error ejecutando la simulación: ${e.message}</td></tr>`;
  }
}

async function applyGeotagTakeout() {
  const windowMins = parseInt(document.getElementById("geo-window-select").value, 10) || 45;
  if (!confirm(`¿Deseas asignar ubicación por proximidad temporal (margen ±${windowMins} min) a las fotos pendientes de Takeout / Carpetas?\n\nAl procesar las tandas, ExifTool grabará estas coordenadas en los archivos para que entren con mapa en Apple Fotos.`)) {
    return;
  }
  try {
    const res = await fetch("/api/geotag/apply-takeout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ window_minutes: windowMins })
    });
    const data = await res.json();
    alert(`¡Hecho! Se han geolocalizado ${data.updated_count.toLocaleString()} fotos en Takeout / Carpetas. Cuando las importes, se subirán con estas coordenadas.`);
    await loadGeotagOverview();
    runGeotagSimulation();
  } catch (e) {
    alert("Error aplicando coordenadas a Takeout: " + e.message);
  }
}

async function applyGeotagPhotosApp() {
  const windowMins = parseInt(document.getElementById("geo-window-select").value, 10) || 45;
  if (!confirm(`¿Deseas asignar ubicación a las fotos que YA están dentro de la app Fotos de Apple en tu Mac (margen ±${windowMins} min)?\n\nEl sistema usará la automatización oficial de Photos.app para situarlas en el mapa.`)) {
    return;
  }
  try {
    const res = await fetch("/api/geotag/apply-photos-app", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ window_minutes: windowMins })
    });
    if (!res.ok) throw new Error("No se pudo iniciar el proceso");
    alert("¡Proceso iniciado en segundo plano! Puedes seguir el progreso en la barra superior.");
    pollStatus();
  } catch (e) {
    alert("Error iniciando geolocalización en Fotos.app: " + e.message);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  await loadSources();
  await pollStatus();
  await refreshSummary();
  setInterval(pollStatus, 1500);
});
