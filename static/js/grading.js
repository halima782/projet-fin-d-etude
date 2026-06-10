(function () {
  const root = document.getElementById("grading-root");
  if (!root) return;

  const msId = root.dataset.msId;
  const yearId = root.dataset.yearId;
  const role = root.dataset.role;
  const sessionType = root.dataset.sessionType;
  const finalUrl = root.dataset.finalUrl;
  const reportUrl = root.dataset.reportUrl;
  const staffMode = root.dataset.staffMode === "1";

  function getCookie(name) {
    const v = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return v ? v.pop() : "";
  }
  const csrftoken = getCookie("csrftoken");

  async function readJsonSafe(res) {
    try {
      return await res.json();
    } catch (_) {
      return null;
    }
  }

  function openExternalWithFallback(url, purpose) {
    const win = window.open(url, "_blank");
    if (win) {
      try {
        win.opener = null;
      } catch (_) {}
      return true;
    }
    alert(
      `Popup blocked while opening ${purpose}. A link will be shown now; open it manually.`
    );
    window.prompt(`Copy/open this ${purpose} link:`, url);
    return false;
  }

  /** Opens a blank tab synchronously (user gesture) so we can navigate after async save without popup blockers. */
  function openReservedPopup() {
    const win = window.open("about:blank", "_blank");
    if (!win) return null;
    try {
      win.opener = null;
    } catch (_) {}
    return win;
  }

  function closePopupSafe(win) {
    if (!win) return;
    try {
      win.close();
    } catch (_) {}
  }

  function navigateReservedPopup(win, url) {
    if (!win || !url) return;
    try {
      win.location.href = url;
    } catch (_) {
      closePopupSafe(win);
    }
  }

  let questions = [];
  let exams = [];
  let marks = {};
  let firstFinalizeDone = false;

  const rubricBody = document.getElementById("rubric-body");
  const marksHead = document.getElementById("marks-head");
  const marksBody = document.getElementById("marks-body");
  const totalsBody = document.getElementById("totals-body");
  const waitBanner = document.getElementById("wait-banner");
  const rubricSection = document.getElementById("rubric-section");
  const linkFinal = document.getElementById("link-final");
  const btnOpenReport = document.getElementById("btn-open-report");
  const firstSendPanel = document.getElementById("first-send-panel");
  const firstSendText = document.getElementById("first-send-text");
  const btnSendHandoff = document.getElementById("btn-send-handoff");

  function updateFirstSendPanel(data) {
    if (role !== "first" || !firstSendPanel || !firstSendText || !btnSendHandoff) return;
    if (!data.first_marks_complete) {
      firstSendPanel.classList.add("hidden");
      firstSendText.innerHTML = "";
      btnSendHandoff.classList.add("hidden");
      return;
    }
    firstSendPanel.classList.remove("hidden");
    if (data.first_handoff_sent) {
      firstSendText.innerHTML =
        "<strong>EN:</strong> You have submitted your marks to the administration and notified the " +
        "second corrector. They can now enter their marks.<br><em>If you change any mark later, click " +
        "<strong>Save marks</strong> — the second corrector gets an in-app notification and " +
        "<strong>WhatsApp opens</strong> (after the first handoff) if their phone number is on file.</em><br><br>" +
        '<strong>عربي:</strong> تم تسليم العلامات للإدارة وإشعار المصحّح الثاني؛ يمكنه الآن إدخال علاماته.<br>' +
        "<em>إذا عدّلت أي علامة لاحقاً، اضغط <strong>حفظ العلامات</strong> — يصل للمصحّح الثاني إشعار داخل النظام " +
        "ويُفتَح واتساب تلقائياً (بعد أول تسليم) إذا كان رقمه مسجّلاً.</em>";
      btnSendHandoff.classList.add("hidden");
    } else {
      firstSendText.textContent =
        "You have entered marks for all students. Click Send to notify the second corrector via WhatsApp and inform the administration that the examination papers are submitted.";
      btnSendHandoff.classList.remove("hidden");
    }
  }

  function handleSecondFinalizeFromPayload(data) {
    if (!data || !data.second_finalize || !data.second_finalize.ok) return;
    const sf = data.second_finalize;
    const msg = document.getElementById("export-msg");
    if (msg && sf.files) {
      msg.textContent =
        "Second round closed. Exported: " +
        sf.files.final +
        (sf.final_printable_url
          ? " | Printable: " + sf.final_printable_url
          : " | Final average printable is on your dashboard.");
    }
    if (linkFinal) {
      linkFinal.href = finalUrl;
      linkFinal.classList.remove("hidden");
    }
    const wlist = sf.recorrection_whatsapp || [];
    if (wlist.length) {
      const manualLinks = [];
      const sentUsers = [];
      const failedUsers = [];
      wlist.forEach((w) => {
        if (!w) return;
        if (w.auto_sent) {
          sentUsers.push(w.username || "unknown");
          return;
        }
        if (w.wa_url) {
          manualLinks.push(w);
          openExternalWithFallback(w.wa_url, "WhatsApp");
        }
        if (w.auto_error) failedUsers.push(w.username || "unknown");
      });
      const parts = [
        "Second round saved. Final average printable is on your dashboard.",
        "Some exams need recorrection (difference >= 10/100).",
      ];
      if (sentUsers.length) {
        parts.push("WhatsApp sent automatically to: " + sentUsers.join(", ") + ".");
      }
      if (manualLinks.length) {
        parts.push("Manual WhatsApp links were opened for the remaining doctors.");
      }
      if (failedUsers.length) {
        parts.push("Auto-send failed for: " + failedUsers.join(", ") + ".");
      }
      alert(parts.join(" "));
    } else {
      alert(
        "Second round saved. Final average printable is on your dashboard. Open final results for details."
      );
    }
  }

  function key(examId, qid) {
    return examId + ":" + qid;
  }

  function maxSum() {
    return questions.reduce((s, q) => s + Number(q.part_mark || 0), 0);
  }

  function totalForExam(examId) {
    const denom = maxSum();
    if (denom <= 0) return 0;
    let earned = 0;
    questions.forEach((q) => {
      const k = key(examId, q.id);
      earned += Number(marks[k] != null ? marks[k] : 0);
    });
    return Math.round((earned / denom) * 10000) / 100;
  }

  function renderRubricEditor() {
    rubricBody.innerHTML = "";
    const rows = questions.length
      ? questions
      : [{ question_title: "", part_title: "", part_mark: "" }];
    rows.forEach((q, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><input type="text" data-f="question_title" value="${escapeAttr(q.question_title || "")}"></td>
        <td><input type="text" data-f="part_title" value="${escapeAttr(q.part_title || "")}"></td>
        <td><input type="number" step="0.5" data-f="part_mark" value="${q.part_mark != null ? q.part_mark : ""}"></td>
        <td><button type="button" class="btn small ghost btn-rm">✕</button></td>`;
      rubricBody.appendChild(tr);
    });
    rubricBody.querySelectorAll(".btn-rm").forEach((btn) => {
      btn.addEventListener("click", () => {
        btn.closest("tr").remove();
      });
    });
  }

  function readRubricFromDom() {
    const parts = [];
    rubricBody.querySelectorAll("tr").forEach((tr) => {
      const ins = tr.querySelectorAll("input");
      if (ins.length < 3) return;
      parts.push({
        question_title: ins[0].value.trim(),
        part_title: ins[1].value.trim(),
        part_mark: parseFloat(ins[2].value) || 0,
      });
    });
    return parts;
  }

  function escapeAttr(s) {
    return s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }

  function renderMarksGrid() {
    marksHead.innerHTML = "";
    marksBody.innerHTML = "";
    if (!questions.length) {
      marksBody.innerHTML =
        '<tr><td colspan="99" class="muted">Save a rubric first.</td></tr>';
      return;
    }
    const grouped = [];
    const byQuestion = {};
    questions.forEach((q) => {
      const keyQ = (q.question_title || "?").trim();
      if (!byQuestion[keyQ]) {
        byQuestion[keyQ] = [];
        grouped.push({ question: keyQ, items: byQuestion[keyQ] });
      }
      byQuestion[keyQ].push(q);
    });

    const hr1 = document.createElement("tr");
    const thNo = document.createElement("th");
    thNo.rowSpan = 2;
    thNo.textContent = "No";
    hr1.appendChild(thNo);
    grouped.forEach((g) => {
      const total = g.items.reduce((s, x) => s + Number(x.part_mark || 0), 0);
      const th = document.createElement("th");
      th.colSpan = g.items.length;
      th.textContent = `${g.question} (${total} points)`;
      hr1.appendChild(th);
    });
    const thNote = document.createElement("th");
    thNote.rowSpan = 2;
    thNote.textContent = "Note/100";
    hr1.appendChild(thNote);

    const hr2 = document.createElement("tr");
    questions.forEach((q) => {
      const th = document.createElement("th");
      const pm = Number(q.part_mark || 0);
      th.textContent = `${q.part_title || "part"} (${pm})`;
      hr2.appendChild(th);
    });
    marksHead.appendChild(hr1);
    marksHead.appendChild(hr2);

    exams.forEach((ex) => {
      const tr = document.createElement("tr");
      const td0 = document.createElement("td");
      td0.textContent = ex.exam_number;
      tr.appendChild(td0);
      questions.forEach((q) => {
        const td = document.createElement("td");
        const inp = document.createElement("input");
        inp.type = "number";
        inp.step = "0.5";
        inp.dataset.examId = ex.id;
        inp.dataset.qid = q.id;
        const k = key(ex.id, q.id);
        if (marks[k] != null) inp.value = marks[k];
        inp.addEventListener("input", () => {
          marks[k] = inp.value === "" ? null : parseFloat(inp.value);
          const totalCell = tr.querySelector(".total-cell");
          if (totalCell) totalCell.textContent = totalForExam(ex.id).toFixed(2);
        });
        td.appendChild(inp);
        tr.appendChild(td);
      });
      const tdTotal = document.createElement("td");
      tdTotal.className = "total-cell";
      tdTotal.textContent = totalForExam(ex.id).toFixed(2);
      tr.appendChild(tdTotal);
      marksBody.appendChild(tr);
    });
  }

  function renderTotals() {
    totalsBody.innerHTML = "";
    exams.forEach((ex) => {
      const tr = document.createElement("tr");
      const t = totalForExam(ex.id);
      tr.innerHTML = `<td>${ex.exam_number}</td><td>${t}</td>`;
      totalsBody.appendChild(tr);
    });
  }

  async function loadSession() {
    const res = await fetch(
      `/api/session/${msId}/${yearId}/${encodeURIComponent(sessionType)}/?role=${encodeURIComponent(role)}`
    );
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || "Could not load session");
      return null;
    }
    questions = data.questions || [];
    exams = data.exams || [];
    marks = {};
    Object.keys(data.marks || {}).forEach((k) => {
      marks[k] = data.marks[k];
    });
    firstFinalizeDone = !!data.first_finalize_done;
    if (data.staff_editor && role === "second") {
      firstFinalizeDone = true;
    }

    if (role === "second" && !firstFinalizeDone && !staffMode) {
      waitBanner.classList.remove("hidden");
      rubricSection.classList.add("hidden");
      document.getElementById("marks-section").classList.add("hidden");
      document.getElementById("totals-section").classList.add("hidden");
      return data;
    }
    waitBanner.classList.add("hidden");

    if (role === "second") {
      rubricSection.classList.add("hidden");
      if (linkFinal) {
        if (data.second_round_complete) {
          linkFinal.href = finalUrl;
          linkFinal.classList.remove("hidden");
        } else {
          linkFinal.classList.add("hidden");
        }
      }
    }

    renderRubricEditor();
    renderMarksGrid();

    if (role === "first") {
      updateFirstSendPanel(data);
    }
    return data;
  }

  document.getElementById("btn-add-part").addEventListener("click", () => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="text" data-f="question_title" value=""></td>
      <td><input type="text" data-f="part_title" value=""></td>
      <td><input type="number" step="0.5" data-f="part_mark" value=""></td>
      <td><button type="button" class="btn small ghost btn-rm">✕</button></td>`;
    rubricBody.appendChild(tr);
    tr.querySelector(".btn-rm").addEventListener("click", () => tr.remove());
  });

  document.getElementById("btn-save-rubric").addEventListener("click", async () => {
    const parts = readRubricFromDom().filter(
      (p) => p.question_title || p.part_title || p.part_mark
    );
    const res = await fetch("/api/save-rubric/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrftoken,
      },
      body: JSON.stringify({
        material_section_id: parseInt(msId, 10),
        academic_year_id: parseInt(yearId, 10),
        session_type: sessionType,
        parts,
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert(data.error || "Could not save rubric");
      return;
    }
    questions = data.questions || [];
    await loadSession();
  });

  async function saveMarks() {
    const expectedCells =
      questions.length && exams.length ? questions.length * exams.length : 0;
    const payload = [];
    marksBody.querySelectorAll("input[type='number']").forEach((inp) => {
      const examId = parseInt(inp.dataset.examId, 10);
      const qid = parseInt(inp.dataset.qid, 10);
      if (!Number.isFinite(examId) || !Number.isFinite(qid)) return;
      const ex = exams.find((e) => e.id === examId);
      if (!ex) return;
      let v = inp.value === "" ? 0 : parseFloat(inp.value);
      if (!Number.isFinite(v)) v = 0;
      payload.push({
        exam_number: ex.exam_number,
        question_id: qid,
        mark: v,
      });
    });
    if (expectedCells && payload.length !== expectedCells) {
      alert(
        "Could not read every mark cell (" +
          payload.length +
          " of " +
          expectedCells +
          "). Refresh the page, then save or send again."
      );
      return { ok: false, data: null };
    }
    const res = await fetch("/api/save-marks/", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrftoken,
      },
      body: JSON.stringify({
        material_section_id: parseInt(msId, 10),
        academic_year_id: parseInt(yearId, 10),
        session_type: sessionType,
        role,
        marks: payload,
      }),
    });
    const data = await readJsonSafe(res);
    if (!res.ok) {
      if (res.status === 403) {
        alert("Session expired (CSRF). Refresh the page and sign in again, then retry.");
      } else {
        alert((data && data.error) || "Could not save marks");
      }
      return { ok: false, data: null };
    }
    return { ok: true, data };
  }

  document.getElementById("btn-save-marks").addEventListener("click", async () => {
    const waPopup = role === "first" ? openReservedPopup() : null;
    const sm = await saveMarks();
    if (!sm.ok) {
      closePopupSafe(waPopup);
      return;
    }
    await loadSession();
    if (sm.data && sm.data.second_finalize && sm.data.second_finalize.ok === false) {
      closePopupSafe(waPopup);
      alert(
        sm.data.second_finalize.error ||
          "Marks were saved, but the second round could not be closed automatically."
      );
      return;
    }
    handleSecondFinalizeFromPayload(sm.data);
    if (sm.data && sm.data.final_printable_url && !sm.data.second_finalize) {
      alert(
        "Final average sheet is available on your dashboard under My saved printable marks sheets."
      );
    }
    const wts = sm.data && sm.data.whatsapp_to_second;
    if (role === "first" && wts && wts.ok && wts.wa_url) {
      if (waPopup) {
        navigateReservedPopup(waPopup, wts.wa_url);
      } else {
        openExternalWithFallback(wts.wa_url, "WhatsApp");
      }
    } else {
      closePopupSafe(waPopup);
      if (role === "first" && wts && !wts.ok && wts.error) {
        alert("Marks saved. WhatsApp: " + wts.error);
      }
    }
  });

  document.getElementById("btn-show-totals").addEventListener("click", async () => {
    const reportPopup = openReservedPopup();
    const sm = await saveMarks();
    if (!sm.ok) {
      closePopupSafe(reportPopup);
      return;
    }
    await loadSession();
    if (sm.data && sm.data.second_finalize && sm.data.second_finalize.ok === false) {
      closePopupSafe(reportPopup);
      alert(
        sm.data.second_finalize.error ||
          "Marks were saved, but the second round could not be closed automatically."
      );
    } else {
      handleSecondFinalizeFromPayload(sm.data);
    }
    renderTotals();
    document.getElementById("totals-section").classList.remove("hidden");
    btnOpenReport.href = reportUrl;
    btnOpenReport.classList.remove("hidden");
    if (reportPopup) {
      navigateReservedPopup(reportPopup, reportUrl);
    } else {
      openExternalWithFallback(reportUrl, "printable report");
    }
  });

  if (btnSendHandoff) {
    btnSendHandoff.addEventListener("click", async () => {
      const waPopup = openReservedPopup();
      const sm = await saveMarks();
      if (!sm.ok) {
        closePopupSafe(waPopup);
        return;
      }
      if (role === "first" && sm.data && sm.data.first_marks_complete === false) {
        closePopupSafe(waPopup);
        alert(
          "Marks were not fully stored on the server. Refresh the page, use Save marks once, then try Send again."
        );
        await loadSession();
        return;
      }
      const res = await fetch("/api/first-corrector-send/", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": csrftoken,
        },
        body: JSON.stringify({
          material_section_id: parseInt(msId, 10),
          academic_year_id: parseInt(yearId, 10),
          session_type: sessionType,
        }),
      });
      const data = await readJsonSafe(res);
      if (!res.ok) {
        closePopupSafe(waPopup);
        if (res.status === 403) {
          alert("Session expired (CSRF). Refresh the page and sign in again, then retry.");
        } else {
          alert((data && data.error) || "Send failed");
        }
        return;
      }
      const msg = document.getElementById("export-msg");
      if (msg && data.files) {
        msg.textContent =
          "Exported: rubric & marks → " +
          data.files.rubric_marks +
          " | totals → " +
          data.files.totals;
      }
      if (data.whatsapp && data.whatsapp.ok) {
        if (waPopup) {
          navigateReservedPopup(waPopup, data.whatsapp.wa_url);
        } else {
          openExternalWithFallback(data.whatsapp.wa_url, "WhatsApp");
        }
      } else {
        closePopupSafe(waPopup);
        if (data.whatsapp && data.whatsapp.error) {
          alert("Saved and administration notified, but WhatsApp: " + data.whatsapp.error);
        }
      }
      if (data.already_sent_before) {
        alert("Handoff was already recorded before; exports were refreshed.");
      } else {
        alert("Handoff sent. Second corrector was notified (in-app and by WhatsApp when possible).");
      }
      await loadSession();
    });
  }

  loadSession();
})();
