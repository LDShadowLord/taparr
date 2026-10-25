/**
 * Taparr Single-Page Web Application Frontend
 */

class TaparrApp {
  constructor() {
    this.pools = [];
    this.tapes = [];
    this.media = [];
    this.bindNavigation();
    this.refreshAll();
  }

  bindNavigation() {
    document.querySelectorAll(".nav-item").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const tab = btn.getAttribute("data-tab");
        document.getElementById(`tab-${tab}`).classList.add("active");
        document.getElementById("page-title").textContent = btn.textContent.trim();
        this.onTabActivated(tab);
      });
    });
  }

  onTabActivated(tab) {
    if (tab === "dashboard") this.loadDashboard();
    if (tab === "media") this.loadMedia();
    if (tab === "msl4048") this.loadLibraryRack();
    if (tab === "tapes") this.loadTapesAndPools();
    if (tab === "jobs") this.loadJobs();
    if (tab === "catalog") {
      this.searchCatalog();
      this.loadBackups();
    }
    if (tab === "settings") this.loadSettings();
    if (tab === "updates") this.loadUpdatesAndChangelog();
  }

  async api(path, options = {}) {
    const res = await fetch(`/api/v1${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || `API error ${res.status}`);
    }
    return data;
  }

  toast(msg) {
    const container = document.getElementById("toast-container");
    const el = document.createElement("div");
    el.className = "toast";
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  fmtBytes(bytes) {
    const b = Number(bytes || 0);
    if (b === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(b) / Math.log(1000)));
    return `${(b / Math.pow(1000, i)).toFixed(2)} ${units[i]}`;
  }

  async refreshAll() {
    await this.loadPoolsQuiet();
    await this.loadDashboard();
    await this.loadMedia();
  }

  async loadPoolsQuiet() {
    this.pools = await this.api("/pools");
  }

  async loadDashboard() {
    const d = await this.api("/dashboard");
    document.getElementById("sidebar-version").textContent = `v${d.taparr_version} • fmt v${d.tape_format_version}`;

    const pwr = d.power;
    const pBadge = document.getElementById("power-badge");
    pBadge.textContent = pwr.power_state;
    pBadge.className = `badge ${pwr.power_state === "ONLINE" ? "badge-success" : "badge-danger"}`;
    const mBtn = document.getElementById("maint-lock-btn");
    mBtn.textContent = `Lock: ${pwr.maintenance_lock ? "ON" : "OFF"}`;
    this._maintLocked = pwr.maintenance_lock;

    const s = d.summary;
    const dbInfo = d.database || {};
    const simPill = document.getElementById("sim-mode-pill");
    if (simPill) {
      simPill.textContent = d.library.simulator_mode ? "VIRTUAL MSL4048 SIMULATOR" : "PHYSICAL SCSI HARDWARE MODE";
      simPill.className = `badge ${d.library.simulator_mode ? "badge-warning" : "badge-success"}`;
    }

    document.getElementById("dashboard-stats").innerHTML = `
      <div class="stat-card">
        <div class="stat-label">Managed Cartridges</div>
        <div class="stat-value">${s.managed_tapes} <span class="muted-text">/ ${s.total_tapes}</span></div>
        <div class="stat-sub">🔒 ${s.foreign_locked_tapes} in 'Used By Other'</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Tape Pool Capacity</div>
        <div class="stat-value">${this.fmtBytes(s.total_tape_used_bytes)}</div>
        <div class="stat-sub">of ${this.fmtBytes(s.total_tape_capacity_bytes)} (${this.fmtBytes(s.total_tape_dead_bytes)} dead)</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Archived Media Items</div>
        <div class="stat-value">${s.media_archived_count}</div>
        <div class="stat-sub">${this.fmtBytes(s.media_archived_bytes)} on LTO tape</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Database Engine</div>
        <div class="stat-value" style="text-transform:uppercase;">${dbInfo.active_dialect || "postgresql"}</div>
        <div class="stat-sub">Driver: ${dbInfo.active_driver || "psycopg"} • v${dbInfo.server_version || ""}</div>
      </div>
    `;

    // Drives
    const drivesHtml = d.library.drives
      .map(
        (drv) => `
      <div class="drive-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong>Drive ${drv.drive_index}: ${drv.name}</strong>
          <span class="badge badge-info">${drv.generation}</span>
        </div>
        <div class="muted-text" style="font-size:12px;margin:4px 0;">Path: <code>${drv.device_path}</code></div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px;">
          <span>Status: <span class="badge ${drv.status === "IDLE" ? "badge-success" : "badge-warning"}">${drv.status}</span></span>
          <span>Loaded: <strong>${drv.loaded_barcode || "Empty"}</strong> (FM #${drv.current_filemark})</span>
          <span>${drv.speed_mbps} MB/s</span>
        </div>
        <div style="display:flex;gap:8px;margin-top:8px;">
          <button class="btn btn-xs btn-secondary" onclick="window.taparrApp.cleanDrive(${drv.drive_index})">🧼 Run CLN Clean</button>
          <button class="btn btn-xs btn-outline" onclick="window.taparrApp.checkDriveAlerts(${drv.drive_index})">🩺 Check TapeAlert (sg_logs)</button>
        </div>
      </div>
    `
      )
      .join("");
    document.getElementById("dashboard-drives").innerHTML = drivesHtml;

    // Scratch telemetry
    const sc = d.scratch;
    document.getElementById("dashboard-scratch").innerHTML = `
      <div style="display:flex;justify-content:space-between;font-size:13px;">
        <span>SSD Scratch Directory: <code>${sc.scratch_dir}</code></span>
        <span>${this.fmtBytes(sc.used_bytes)} / ${this.fmtBytes(sc.max_scratch_bytes)} (${sc.usage_pct}%)</span>
      </div>
      <div class="progress-bar"><div class="progress-fill" style="width:${Math.min(100, sc.usage_pct)}%"></div></div>
    `;

    // Pools summary
    document.getElementById("dashboard-pools").innerHTML = this.pools
      .map(
        (p) => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:13px;">
        <span>
          <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px;"></span>
          <strong>${p.name}</strong> ${p.is_protected_foreign ? '<span class="badge badge-danger">LOCKED</span>' : ""}
        </span>
        <span class="muted-text">${p.tape_count} tapes • ${this.fmtBytes(p.used_bytes)} / ${this.fmtBytes(p.total_capacity_bytes)}</span>
      </div>
    `
      )
      .join("");

    // LTO Matrix Table (L1..LA)
    const specs = Object.values(d.lto_matrix.media_specs);
    const compat = d.lto_matrix.drive_compatibility;
    document.getElementById("lto-matrix-table").innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Barcode Suffix</th>
            <th>Generation</th>
            <th>Native Capacity</th>
            <th>Native Speed</th>
            <th>Drive Read/Write Support</th>
          </tr>
        </thead>
        <tbody>
          ${specs
            .map((sp) => {
              const rwDrives = Object.entries(compat)
                .filter(([, c]) => c.write.includes(sp.code))
                .map(([dName]) => dName)
                .join(", ");
              return `
              <tr>
                <td><code>${sp.code}</code></td>
                <td><strong>${sp.generation}</strong></td>
                <td>${sp.is_cleaning ? "Cleaning Cartridge" : sp.native_capacity_gb + " GB"}</td>
                <td>${sp.native_speed_mbps ? sp.native_speed_mbps + " MB/s" : "—"}</td>
                <td>${sp.is_cleaning ? "All LTO-1..LTO-10 Drives" : rwDrives}</td>
              </tr>
            `;
            })
            .join("")}
        </tbody>
      </table>
    `;
  }

  async loadMedia() {
    const q = document.getElementById("media-search")?.value || "";
    const cat = document.getElementById("media-cat-filter")?.value || "";
    const status = document.getElementById("media-status-filter")?.value || "";
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (cat) params.set("category", cat);
    if (status) params.set("status", status);

    this.media = await this.api(`/media?${params.toString()}`);
    const writablePools = this.pools.filter((p) => !p.is_protected_foreign && p.name !== "Unassigned");

    const tbody = document.getElementById("media-tbody");
    if (!this.media.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted-text">No media items registered yet. Click "+ Load Demo Media" or sync a Sonarr/Radarr instance.</td></tr>`;
      return;
    }

    tbody.innerHTML = this.media
      .map(
        (m) => `
      <tr>
        <td><input type="checkbox" class="media-cb" value="${m.id}" /></td>
        <td>
          <strong>${m.title}</strong> ${m.year ? `<span class="muted-text">(${m.year})</span>` : ""}
          <div class="muted-text" style="font-size:11px;"><code>${m.folder_path}</code></div>
        </td>
        <td><span class="badge badge-info">${m.media_category}</span></td>
        <td>${this.fmtBytes(m.size_bytes)} <span class="muted-text">(${m.file_count} files)</span></td>
        <td>
          <select class="input" onchange="window.taparrApp.queueSingleMediaPool(${m.id}, this.value)">
            ${writablePools
              .map(
                (p) => `<option value="${p.id}" ${p.id === m.target_pool_id ? "selected" : ""}>${p.name}</option>`
              )
              .join("")}
          </select>
        </td>
        <td><span class="badge ${this.statusBadgeClass(m.status)}">${m.status}</span></td>
        <td>
          ${
            m.primary_tape_barcode
              ? `<code>${m.primary_tape_barcode}</code> FM #${m.primary_filemark}${
                  m.clone_tape_barcode ? ` <span class="badge badge-neutral">Clone: ${m.clone_tape_barcode}</span>` : ""
                }`
              : "—"
          }
        </td>
        <td>
          <button class="btn btn-xs btn-primary" onclick="window.taparrApp.queueItemsArchive([${m.id}])">📼 Archive</button>
          ${
            m.primary_tape_barcode
              ? `<button class="btn btn-xs btn-accent" onclick="window.taparrApp.queueItemsRestore([${m.id}])">♻ Restore</button>`
              : ""
          }
        </td>
      </tr>
    `
      )
      .join("");
  }

  statusBadgeClass(st) {
    if (st === "ON_TAPE_STUBBED" || st === "ON_TAPE_ONLY") return "badge-success";
    if (st === "ON_DISK_AND_TAPE") return "badge-info";
    if (st.startsWith("QUEUED")) return "badge-warning";
    return "badge-neutral";
  }

  toggleAllMedia(master) {
    document.querySelectorAll(".media-cb").forEach((cb) => {
      cb.checked = master.checked;
    });
  }

  getSelectedMediaIds() {
    return Array.from(document.querySelectorAll(".media-cb:checked")).map((cb) => Number(cb.value));
  }

  async seedDemoMedia() {
    const res = await this.api("/media/seed-demo", { method: "POST" });
    this.toast(`Seeded ${res.seeded_media_ids.length} demo Movies/TV/Anime folders!`);
    await this.loadMedia();
    await this.loadDashboard();
  }

  async queueSelectedForArchive() {
    const ids = this.getSelectedMediaIds();
    if (!ids.length) return this.toast("Select at least one media item first.");
    await this.queueItemsArchive(ids);
  }

  async queueItemsArchive(ids, poolId = null) {
    const body = { media_item_ids: ids };
    if (poolId) body.target_pool_id = Number(poolId);
    const res = await this.api("/media/queue-archive", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (res.instantly_reevicted_ids?.length) {
      this.toast(`Instantly re-evicted ${res.instantly_reevicted_ids.length} unchanged item(s) without rewriting tape!`);
    } else {
      this.toast(`Queued ${res.queued_ids.length} item(s) for tape archival.`);
    }
    await this.loadMedia();
    await this.loadDashboard();
  }

  async queueSingleMediaPool(mediaId, poolId) {
    await this.queueItemsArchive([mediaId], Number(poolId));
  }

  async queueSelectedForRestore() {
    const ids = this.getSelectedMediaIds();
    if (!ids.length) return this.toast("Select at least one archived item to restore.");
    await this.queueItemsRestore(ids);
  }

  async queueItemsRestore(ids) {
    const res = await this.api("/media/queue-restore", {
      method: "POST",
      body: JSON.stringify({ media_item_ids: ids }),
    });
    if (res.vault_warnings?.length) {
      this.toast(`Warning: Tape ${res.vault_warnings[0].barcode} is in Vault (${res.vault_warnings[0].vault_note}).`);
    } else {
      this.toast(`Queued ${res.queued_restore_ids.length} item(s) for single-pass restore.`);
    }
    await this.loadMedia();
    await this.loadDashboard();
  }

  async runArchiveBatch() {
    try {
      const res = await this.api("/jobs/run-archive", {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (res.status === "empty_queue") {
        this.toast("Archive queue is empty — queue a Movie or Season first.");
      } else {
        this.toast(`Archive batch completed! Catalog backup: ${res.catalog_backup?.bundle_name || "saved"}`);
      }
      await this.refreshAll();
    } catch (e) {
      this.toast(`Archive batch error: ${e.message}`);
    }
  }

  async runRestoreBatch() {
    try {
      const res = await this.api("/jobs/run-restore", {
        method: "POST",
        body: JSON.stringify({ remonitor_in_arr: true }),
      });
      if (res.status === "empty_queue") {
        this.toast("Restore queue is empty.");
      } else {
        this.toast(`Single-pass restore completed (${res.restored_items.length} item(s) SHA-256 verified)!`);
      }
      await this.refreshAll();
    } catch (e) {
      this.toast(`Restore error: ${e.message}`);
    }
  }

  async loadLibraryRack() {
    const lib = await this.api("/library/state");
    const grid = document.getElementById("msl4048-rack-grid");
    grid.innerHTML = lib.slots
      .map((s) => {
        const t = s.tape;
        const isLocked = t && t.is_protected_foreign;
        return `
        <div class="rack-slot ${s.is_mailslot ? "mailslot" : ""} ${isLocked ? "foreign-locked" : ""}">
          <div class="slot-head">
            <span>Slot #${s.slot_number}</span>
            <span>${s.is_mailslot ? "📬 I/E" : ""}</span>
          </div>
          ${
            t
              ? `
            <div class="slot-barcode">${t.barcode}</div>
            <div>
              <span class="badge" style="background:${t.pool_color}33;color:${t.pool_color};">${t.pool_name}</span>
              <span class="badge badge-neutral">${t.media_code}</span>
            </div>
            <div class="muted-text" style="font-size:11px;">
              ${isLocked ? "🔒 DO NOT TOUCH" : `${this.fmtBytes(t.used_bytes)} • ${t.total_filemarks} FM`}
            </div>
          `
              : `<div class="muted-text" style="padding:12px 0;">Empty</div>`
          }
        </div>
      `;
      })
      .join("");

    const vEl = document.getElementById("vault-tapes-list");
    if (!lib.vaulted_tapes.length) {
      vEl.innerHTML = `<p class="muted-text">No cartridges currently vaulted offsite.</p>`;
    } else {
      vEl.innerHTML = lib.vaulted_tapes
        .map(
          (t) => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);">
          <span><strong>${t.barcode}</strong> (${t.generation_label}) — Pool: ${t.pool_name} — Note: <em>${t.vault_note}</em></span>
          <button class="btn btn-xs btn-primary" onclick="window.taparrApp.returnFromVault('${t.barcode}')">Import Back to MSL4048</button>
        </div>
      `
        )
        .join("");
    }
  }

  async runInventory() {
    await this.api("/library/inventory", { method: "POST" });
    this.toast("MSL4048 48-slot barcode inventory completed.");
    await this.loadLibraryRack();
  }

  async loadTapesAndPools() {
    await this.loadPoolsQuiet();
    this.tapes = await this.api("/tapes");

    document.getElementById("pools-table-wrap").innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Pool</th>
            <th>Type / Protection</th>
            <th>Auto-Match Regex</th>
            <th>Tapes</th>
            <th>Used / Capacity</th>
          </tr>
        </thead>
        <tbody>
          ${this.pools
            .map(
              (p) => `
            <tr>
              <td><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};margin-right:6px;"></span><strong>${p.name}</strong></td>
              <td>${
                p.is_protected_foreign
                  ? '<span class="badge badge-danger">🔒 Foreign Backup (Locked)</span>'
                  : p.is_system
                  ? '<span class="badge badge-neutral">System Holding</span>'
                  : '<span class="badge badge-info">Media Pool</span>'
              }</td>
              <td><code>${p.barcode_regex || "—"}</code></td>
              <td>${p.tape_count}</td>
              <td>${this.fmtBytes(p.used_bytes)} / ${this.fmtBytes(p.total_capacity_bytes)}</td>
            </tr>
          `
            )
            .join("")}
        </tbody>
      </table>
    `;

    const tbody = document.getElementById("tapes-tbody");
    tbody.innerHTML = this.tapes
      .map(
        (t) => `
      <tr>
        <td><strong><code>${t.barcode}</code></strong></td>
        <td><span class="badge badge-info">${t.generation_label} (${t.media_code})</span></td>
        <td>
          <select class="input" onchange="window.taparrApp.assignTapePool('${t.barcode}', this.value)">
            ${this.pools
              .map((p) => `<option value="${p.id}" ${p.id === t.pool_id ? "selected" : ""}>${p.name}</option>`)
              .join("")}
          </select>
        </td>
        <td>
          <span class="badge ${
            t.is_protected_foreign
              ? "badge-danger"
              : t.status === "CLOSED"
              ? "badge-warning"
              : t.status === "ACTIVE"
              ? "badge-success"
              : "badge-neutral"
          }">${t.status}</span>
        </td>
        <td>${t.location_type}${t.slot_number ? ` #${t.slot_number}` : ""}${t.drive_index !== null ? ` (Drive ${t.drive_index})` : ""}</td>
        <td>${this.fmtBytes(t.used_bytes)} / ${this.fmtBytes(t.native_capacity_bytes)} (${t.usage_pct}%)</td>
        <td>${t.dead_pct}%</td>
        <td>${t.total_filemarks}${t.manifest_filemark ? ` (Manifest FM #${t.manifest_filemark})` : ""}</td>
        <td>${t.header_taparr_version ? `v${t.header_taparr_version} (fmt ${t.header_format_version})` : "—"}</td>
        <td>
          ${
            t.is_protected_foreign
              ? `<span class="muted-text">🔒 Locked (Foreign Software)</span>`
              : `
            <button class="btn btn-xs btn-secondary" onclick="window.taparrApp.inspectTape('${t.barcode}')">fm0 / FMs</button>
            <button class="btn btn-xs btn-accent" onclick="window.taparrApp.closeTape('${t.barcode}')">Close Tape</button>
            <button class="btn btn-xs btn-primary" onclick="window.taparrApp.promptCloneTape('${t.barcode}')">Clone</button>
            <button class="btn btn-xs btn-outline" onclick="window.taparrApp.compactTape('${t.barcode}')">Compact</button>
            <button class="btn btn-xs btn-outline" onclick="window.taparrApp.vaultTape('${t.barcode}')">Vault</button>
          `
          }
        </td>
      </tr>
    `
      )
      .join("");
  }

  async createPool() {
    const name = document.getElementById("new-pool-name").value.trim();
    const color = document.getElementById("new-pool-color").value;
    if (!name) return this.toast("Enter a pool name.");
    await this.api("/pools", {
      method: "POST",
      body: JSON.stringify({ name, color }),
    });
    document.getElementById("new-pool-name").value = "";
    this.toast(`Created pool '${name}'.`);
    await this.loadTapesAndPools();
  }

  async registerTape() {
    const barcode = document.getElementById("new-tape-barcode").value.trim().toUpperCase();
    const slot = Number(document.getElementById("new-tape-slot").value || 0) || null;
    if (!barcode) return this.toast("Enter a cartridge barcode.");
    await this.api("/tapes", {
      method: "POST",
      body: JSON.stringify({ barcode, slot_number: slot }),
    });
    document.getElementById("new-tape-barcode").value = "";
    this.toast(`Registered cartridge ${barcode}.`);
    await this.loadTapesAndPools();
  }

  async assignTapePool(barcode, poolId) {
    await this.api(`/tapes/${barcode}/assign-pool`, {
      method: "PUT",
      body: JSON.stringify({ pool_id: Number(poolId) }),
    });
    this.toast(`Updated pool for ${barcode}.`);
    await this.loadTapesAndPools();
  }

  async closeTape(barcode) {
    try {
      const res = await this.api(`/tapes/${barcode}/close`, { method: "POST" });
      this.toast(`Tape ${barcode} sealed with final manifest at FM #${res.manifest_filemark}!`);
      await this.loadTapesAndPools();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async inspectTape(barcode) {
    const card = document.getElementById("tape-inspector-card");
    const out = document.getElementById("inspector-output");
    document.getElementById("inspector-title").textContent = `Tape Inspector: ${barcode}`;
    card.style.display = "block";
    try {
      const fms = await this.api(`/tapes/${barcode}/filemarks`);
      let header = null;
      try {
        header = await this.api(`/tapes/${barcode}/header`);
      } catch (_) {
        header = { note: "Tape is BLANK (fm0 header not written yet)" };
      }
      out.textContent = JSON.stringify({ fm0_header: header, filemarks: fms }, null, 2);
    } catch (e) {
      out.textContent = e.message;
    }
  }

  async promptCloneTape(sourceBarcode) {
    const blankTapes = this.tapes.filter((t) => t.status === "BLANK" && !t.is_protected_foreign && t.barcode !== sourceBarcode);
    if (!blankTapes.length) return this.toast("No BLANK target tape available for cloning.");
    const targetBarcode = blankTapes[0].barcode;
    try {
      const res = await this.api(`/tapes/${sourceBarcode}/clone`, {
        method: "POST",
        body: JSON.stringify({ target_barcode: targetBarcode }),
      });
      this.toast(`Cloned ${sourceBarcode} -> ${targetBarcode} (${res.filemarks_cloned} filemarks)!`);
      await this.loadTapesAndPools();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async compactTape(sourceBarcode) {
    try {
      const res = await this.api(`/tapes/${sourceBarcode}/compact`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      this.toast(`Compacted ${sourceBarcode} -> ${res.target_barcode} (${this.fmtBytes(res.reclaimed_dead_bytes)} reclaimed)!`);
      await this.loadTapesAndPools();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async vaultTape(barcode) {
    try {
      await this.api(`/tapes/${barcode}/vault`, {
        method: "POST",
        body: JSON.stringify({ vault_note: "Offsite Safe Vault" }),
      });
      this.toast(`Moved ${barcode} to Offsite Vault.`);
      await this.loadTapesAndPools();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async returnFromVault(barcode) {
    await this.api(`/tapes/${barcode}/return-vault`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    this.toast(`Returned ${barcode} from Vault to MSL4048 slot.`);
    await this.loadLibraryRack();
  }

  async loadJobs() {
    const jobs = await this.api("/jobs");
    const el = document.getElementById("jobs-list");
    if (!jobs.length) {
      el.innerHTML = `<p class="muted-text">No batch jobs recorded yet.</p>`;
      return;
    }
    el.innerHTML = jobs
      .map(
        (j) => `
      <div class="drive-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong>Job #${j.id} — ${j.job_type}</strong>
          <span class="badge ${j.status === "COMPLETED" ? "badge-success" : j.status === "FAILED" ? "badge-danger" : "badge-warning"}">${j.status} (${j.progress_pct}%)</span>
        </div>
        <div class="muted-text" style="font-size:12px;margin:4px 0;">
          Drive: ${j.drive_index !== null ? j.drive_index : "—"} | Tape: ${j.target_tape_barcode || j.source_tape_barcode || "—"} | Items: ${j.items_completed}/${j.items_total} | ${this.fmtBytes(j.bytes_processed)}
        </div>
        <pre class="code-block" style="margin-top:8px;">${j.log_text || ""}</pre>
      </div>
    `
      )
      .join("");
  }

  async searchCatalog() {
    const q = document.getElementById("catalog-search-q")?.value || "";
    const barcode = document.getElementById("catalog-barcode-q")?.value || "";
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (barcode) params.set("tape_barcode", barcode);
    const rows = await this.api(`/catalog/search?${params.toString()}`);
    const tbody = document.getElementById("catalog-tbody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="10" class="muted-text">No archived files in catalog matching filter.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows
      .map(
        (r) => `
      <tr>
        <td><code>${r.tape_barcode}</code></td>
        <td><code>${r.clone_tape_barcode || "—"}</code></td>
        <td>${r.tape_pool_name}</td>
        <td><strong>FM #${r.filemark_number}</strong></td>
        <td>${r.block_offset}</td>
        <td>
          <strong>${r.title}</strong>
          <div class="muted-text" style="font-size:11px;"><code>${r.relative_path}</code></div>
        </td>
        <td>${this.fmtBytes(r.size_bytes)}</td>
        <td><code title="${r.sha256_hash}">${r.sha256_hash.slice(0, 12)}…</code></td>
        <td>v${r.taparr_version} (fmt ${r.tape_format_version})</td>
        <td><code style="font-size:11px;">${r.bare_metal_cmd}</code></td>
      </tr>
    `
      )
      .join("");
  }

  async exportCatalogBackup() {
    const res = await this.api("/catalog/backup", { method: "POST" });
    this.toast(`Exported Disaster Recovery Bundle: ${res.bundle_name}`);
    await this.loadBackups();
  }

  async loadBackups() {
    const backups = await this.api("/catalog/backups");
    const el = document.getElementById("backups-list");
    if (!backups.length) {
      el.innerHTML = `<p class="muted-text">No catalog backup bundles exported yet.</p>`;
      return;
    }
    el.innerHTML = backups
      .map(
        (b) => `
      <div class="drive-card">
        <div><strong>${b.name}</strong> <span class="muted-text">(${b.created_at})</span></div>
        <div class="muted-text" style="font-size:12px;">Path: <code>${b.path}</code></div>
        <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;">
          ${b.files
            .map(
              (f) =>
                `<a href="/api/v1/catalog/backups/${encodeURIComponent(b.name)}/download/${encodeURIComponent(
                  f
                )}" target="_blank" class="badge badge-info" style="text-decoration:none;">⬇ ${f}</a>`
            )
            .join("")}
        </div>
      </div>
    `
      )
      .join("");
  }

  async cleanDrive(driveIndex) {
    try {
      const res = await this.api(`/drives/${driveIndex}/clean`, { method: "POST" });
      this.toast(`Drive ${driveIndex} cleaned using ${res.cleaning_barcode} (Slot #${res.slot_number})!`);
      await this.loadDashboard();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async checkDriveAlerts(driveIndex) {
    const res = await this.api(`/drives/${driveIndex}/alerts`);
    if (res.needs_cleaning) {
      this.toast(`Drive ${driveIndex} requests cleaning! Alerts: ${res.alerts.join(", ")}`);
    } else {
      this.toast(`Drive ${driveIndex} SCSI TapeAlert status healthy (no cleaning needed).`);
    }
  }

  async loadSettings() {
    const s = await this.api("/settings");
    const instances = await this.api("/arr-instances");

    document.getElementById("settings-hardware-form").innerHTML = `
      <div style="display:flex;flex-direction:column;gap:10px;font-size:13px;">
        <label>Hardware Backend Mode:
          <select id="cfg-sim-mode" class="input">
            <option value="true" ${s.hardware.simulator_mode ? "selected" : ""}>Virtual MSL4048 Simulator (Safe Testing)</option>
            <option value="false" ${!s.hardware.simulator_mode ? "selected" : ""}>Physical Linux SCSI Mode (mtx / mt-st / tar / mbuffer)</option>
          </select>
        </label>
        <label>HP MSL4048 Changer Device (/dev/tape/by-id/...):
          <input type="text" id="cfg-changer-dev" class="input" value="${s.hardware.changer_device || ""}" style="width:100%" />
        </label>
        <label>Post-Archive Disk Action:
          <select id="cfg-post-mode" class="input">
            <option value="stub_mkv" ${s.disk_cleanup.post_archive_mode === "stub_mkv" ? "selected" : ""}>Playable .mkv Video Stub for Plex</option>
            <option value="delete" ${s.disk_cleanup.post_archive_mode === "delete" ? "selected" : ""}>Clean Deletion from Disk</option>
          </select>
        </label>
        <label>Local SSD Scratch Directory:
          <input type="text" id="cfg-scratch-dir" class="input" value="${s.scratch.scratch_dir}" style="width:100%" />
        </label>
        <label>Max SSD Scratch Usage Cap (GB):
          <input type="number" id="cfg-scratch-gb" class="input" value="${s.scratch.max_scratch_gb}" />
        </label>
        <label>TAR Blocking Factor (-b):
          <input type="number" id="cfg-tar-bf" class="input" value="${s.hardware.tar_block_factor}" />
        </label>
        <button class="btn btn-sm btn-primary" onclick="window.taparrApp.saveHardwareSettings()">Save Hardware & Stub Settings</button>
      </div>
    `;

    document.getElementById("settings-power-form").innerHTML = `
      <div style="display:flex;flex-direction:column;gap:10px;font-size:13px;">
        <label>Idle Auto-Power-Off Cooldown (Minutes):
          <input type="number" id="cfg-idle-min" class="input" value="${s.power.idle_cooldown_minutes}" />
        </label>
        <label>Power-On Webhook URL (Home Assistant / Shelly):
          <input type="text" id="cfg-pon-url" class="input" value="${s.power.power_on_webhook_url || ""}" style="width:100%" />
        </label>
        <label>Power-Off Webhook URL:
          <input type="text" id="cfg-poff-url" class="input" value="${s.power.power_off_webhook_url || ""}" style="width:100%" />
        </label>
        <label>Plex Server URL:
          <input type="text" id="cfg-plex-url" class="input" value="${s.plex.url || ""}" style="width:100%" />
        </label>
        <button class="btn btn-sm btn-primary" onclick="window.taparrApp.savePowerSettings()">Save Power & Plex Settings</button>
      </div>
    `;

    document.getElementById("arr-instances-list").innerHTML = instances.length
      ? instances
          .map(
            (i) => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border);margin-top:8px;">
            <span><strong>${i.name}</strong> (<span class="badge badge-info">${i.arr_type}</span>) — <code>${i.url}</code> (Tag: <code>${i.to_tape_tag}</code>)</span>
            <button class="btn btn-xs btn-secondary" onclick="window.taparrApp.syncArr(${i.id})">↻ Sync Library</button>
          </div>
        `
          )
          .join("")
      : `<p class="muted-text" style="margin-top:10px;">No Sonarr/Radarr instances configured yet.</p>`;
  }

  async saveHardwareSettings() {
    await this.api("/settings/disk_cleanup", {
      method: "PUT",
      body: JSON.stringify({ post_archive_mode: document.getElementById("cfg-post-mode").value }),
    });
    await this.api("/settings/scratch", {
      method: "PUT",
      body: JSON.stringify({
        scratch_dir: document.getElementById("cfg-scratch-dir").value,
        max_scratch_gb: Number(document.getElementById("cfg-scratch-gb").value),
      }),
    });
    await this.api("/settings/hardware", {
      method: "PUT",
      body: JSON.stringify({
        simulator_mode: document.getElementById("cfg-sim-mode").value === "true",
        changer_device: document.getElementById("cfg-changer-dev").value,
        tar_block_factor: Number(document.getElementById("cfg-tar-bf").value),
      }),
    });
    this.toast("Hardware, SSD scratch, and stub settings saved.");
    await this.loadDashboard();
  }

  async savePowerSettings() {
    await this.api("/settings/power", {
      method: "PUT",
      body: JSON.stringify({
        idle_cooldown_minutes: Number(document.getElementById("cfg-idle-min").value),
        power_on_webhook_url: document.getElementById("cfg-pon-url").value,
        power_off_webhook_url: document.getElementById("cfg-poff-url").value,
      }),
    });
    await this.api("/settings/plex", {
      method: "PUT",
      body: JSON.stringify({ url: document.getElementById("cfg-plex-url").value }),
    });
    this.toast("Power and Plex settings saved.");
  }

  async addArrInstance() {
    const name = document.getElementById("arr-name").value.trim();
    const arr_type = document.getElementById("arr-type").value;
    const url = document.getElementById("arr-url").value.trim();
    const api_key = document.getElementById("arr-key").value.trim();
    if (!name || !url || !api_key) return this.toast("Fill in Name, URL, and API Key.");
    await this.api("/arr-instances", {
      method: "POST",
      body: JSON.stringify({ name, arr_type, url, api_key }),
    });
    this.toast(`Added ${name}.`);
    await this.loadSettings();
  }

  async syncArr(id) {
    const res = await this.api(`/arr-instances/${id}/sync`, { method: "POST" });
    this.toast(`Sync status: ${res.status} (${res.synced || 0} synced)`);
  }

  async loadUpdatesAndChangelog() {
    const info = await this.api("/system/version");
    document.getElementById("updater-status-box").innerHTML = `
      <div style="display:flex;flex-direction:column;gap:6px;font-size:13px;">
        <div>Running Taparr Version: <strong>v${info.taparr_version}</strong></div>
        <div>On-Tape Header Format Version: <strong>v${info.tape_format_version}</strong></div>
        <div>Git Branch / Commit: <code>${info.git_branch} @ ${info.git_commit}</code></div>
        <div>Drive Safety Check: <span class="badge ${info.safe_to_update ? "badge-success" : "badge-danger"}">${
          info.safe_to_update ? "All Drives Idle (Safe to Update)" : "Drives Active (Update Locked)"
        }</span></div>
      </div>
    `;

    document.getElementById("changelog-releases").innerHTML = info.releases
      .map(
        (rel) => `
      <div class="drive-card">
        <div style="display:flex;justify-content:space-between;">
          <strong>Version ${rel.version}</strong>
          <span class="badge badge-info">${rel.date}</span>
        </div>
        <pre class="code-block" style="margin-top:8px;">${rel.notes}</pre>
      </div>
    `
      )
      .join("");
  }

  async runUpdate(dryRun = false) {
    const logEl = document.getElementById("updater-log-output");
    logEl.style.display = "block";
    logEl.textContent = "Running pre-update catalog backup and executing scripts/update.sh...";
    try {
      const res = await this.api("/system/update", {
        method: "POST",
        body: JSON.stringify({ dry_run: dryRun, force: false }),
      });
      logEl.textContent = `Pre-Update Backup: ${res.pre_update_backup}\n\n${res.log_output}`;
      this.toast(`Update ${dryRun ? "(Dry Run)" : ""} completed!`);
    } catch (e) {
      logEl.textContent = `ERROR: ${e.message}`;
      this.toast(e.message);
    }
  }

  async powerOn() {
    await this.api("/library/power/on", { method: "POST" });
    this.toast("HP MSL4048 powered ON and inventory scanned.");
    await this.loadDashboard();
  }

  async powerOff() {
    try {
      await this.api("/library/power/off", { method: "POST" });
      this.toast("Drives unloaded and HP MSL4048 powered OFF.");
      await this.loadDashboard();
    } catch (e) {
      this.toast(e.message);
    }
  }

  async toggleMaintLock() {
    const next = !this._maintLocked;
    await this.api("/library/power/maintenance", {
      method: "POST",
      body: JSON.stringify({ locked: next }),
    });
    this.toast(`Maintenance Lock (Keep Awake) ${next ? "ENABLED" : "DISABLED"}.`);
    await this.loadDashboard();
  }
}

window.addEventListener("DOMContentLoaded", () => {
  window.taparrApp = new TaparrApp();
});
