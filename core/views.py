import json
import mimetypes
import re
from base64 import b64encode
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static as static_asset_url
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods, require_POST

from .models import (
    AcademicYear,
    Department,
    Exam,
    FinalResult,
    Mark,
    MarksReport,
    Material,
    MaterialSection,
    MaterialSectionCorrector,
    Notification,
    Question,
)
from .exporters import (
    build_average_only_workbook,
    build_final_workbook,
    build_rubric_and_marks_workbook,
    build_totals_workbook,
    total_on_100,
)
STUDENT_COUNT = getattr(settings, "EXAM_STUDENT_COUNT", 80)
VALID_SESSION_TYPES = frozenset(("partial", "first_final", "second_final"))
# Two-column printables: fictitious #1–36 on the first sheet; #37 onward on the second (e.g. through 88).
PRINTABLE_PAGE1_LAST_EXAM_NUMBER = 36

_LOGO_SRC_CACHE: dict[str, str] = {}
_INSTITUTION_LOGO_MAX_BYTES = 2_500_000


def _bytes_to_data_uri(body: bytes, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if not ct.startswith("image/"):
        if len(body) >= 3 and body[:3] == b"\xff\xd8\xff":
            ct = "image/jpeg"
        elif body.startswith(b"\x89PNG\r\n\x1a\n"):
            ct = "image/png"
        elif body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
            ct = "image/gif"
        elif body.startswith(b"RIFF") and len(body) >= 12 and body[8:12] == b"WEBP":
            ct = "image/webp"
        else:
            ct = "image/png"
    return f"data:{ct};base64,{b64encode(body).decode('ascii')}"


def _institution_logo_url() -> str:
    """Value for <img src>: embed logo as data URI when possible so print/PDF works without fetching the host."""
    raw = (getattr(settings, "INSTITUTION_LOGO_STATIC", None) or "").strip()
    if not raw:
        return ""
    embed = getattr(settings, "INSTITUTION_LOGO_EMBED", True)
    cache_key = f"{embed!s}|{raw}"
    if cache_key in _LOGO_SRC_CACHE:
        return _LOGO_SRC_CACHE[cache_key]

    def fallback_url() -> str:
        if raw.startswith(("http://", "https://")):
            return raw
        if raw.startswith("//"):
            return f"https:{raw}"
        rel = raw.replace("\\", "/").lstrip("/")
        return static_asset_url(rel)

    out = ""
    if embed:
        try:
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            if raw.startswith(("http://", "https://")):
                req = Request(raw, headers={"User-Agent": ua})
                try:
                    with urlopen(req, timeout=20) as resp:
                        ctype = resp.headers.get("Content-Type")
                        body = resp.read(_INSTITUTION_LOGO_MAX_BYTES + 1)
                except (URLError, HTTPError, OSError, ValueError):
                    if raw.startswith("https://"):
                        alt = "http://" + raw[8:]
                        req2 = Request(alt, headers={"User-Agent": ua})
                        with urlopen(req2, timeout=20) as resp:
                            ctype = resp.headers.get("Content-Type")
                            body = resp.read(_INSTITUTION_LOGO_MAX_BYTES + 1)
                    else:
                        raise
                if len(body) > _INSTITUTION_LOGO_MAX_BYTES:
                    raise ValueError("logo too large")
                out = _bytes_to_data_uri(body, ctype)
            elif raw.startswith("//"):
                req = Request(f"https:{raw}", headers={"User-Agent": ua})
                with urlopen(req, timeout=20) as resp:
                    ctype = resp.headers.get("Content-Type")
                    body = resp.read(_INSTITUTION_LOGO_MAX_BYTES + 1)
                if len(body) > _INSTITUTION_LOGO_MAX_BYTES:
                    raise ValueError("logo too large")
                out = _bytes_to_data_uri(body, ctype)
            else:
                rel = raw.replace("\\", "/").lstrip("/")
                path = Path(settings.BASE_DIR) / "static" / rel
                if path.is_file():
                    body = path.read_bytes()
                    if len(body) > _INSTITUTION_LOGO_MAX_BYTES:
                        raise ValueError("logo too large")
                    ctype, _ = mimetypes.guess_type(path.name)
                    out = _bytes_to_data_uri(body, ctype)
        except Exception:
            out = ""

    if not out:
        out = fallback_url()

    _LOGO_SRC_CACHE[cache_key] = out
    return out


def _merge_rows_two_columns(rows_slice: list) -> list:
    """Split a slice of row dicts into left/right columns (official form layout)."""
    if not rows_slice:
        return []
    half = (len(rows_slice) + 1) // 2
    left = rows_slice[:half]
    right = rows_slice[half:]
    max_len = max(len(left), len(right))
    merged = []
    for i in range(max_len):
        merged.append(
            {
                "left": left[i] if i < len(left) else None,
                "right": right[i] if i < len(right) else None,
            }
        )
    return merged


def _build_printable_pages_first36_then_rest(rows: list) -> list:
    """Exactly two pages when roster is large enough: exams 1–36, then 37..end."""
    first = rows[:PRINTABLE_PAGE1_LAST_EXAM_NUMBER]
    rest = rows[PRINTABLE_PAGE1_LAST_EXAM_NUMBER:]
    pages = []
    m0 = _merge_rows_two_columns(first)
    if m0:
        pages.append(m0)
    m1 = _merge_rows_two_columns(rest)
    if m1:
        pages.append(m1)
    return pages or [[]]


def _session_label_short(st: str) -> str:
    return {
        "partial": "Partial",
        "first_final": "First final",
        "second_final": "Second final",
    }.get(st, st)


def _arabic_sheet_title(st: str) -> str:
    """Official-style subtitle on printables: distinct line for each session type."""
    return {
        "partial": "علامات الامتحانات الخطية الجزئية",
        "first_final": "علامات الامتحانات الخطية النهائية الأولى",
        "second_final": "علامات الامتحانات الخطية النهائية الثانية",
    }.get(st, "علامات الامتحانات الخطية")


def _session_slug_for_files(st: str) -> str:
    return {
        "partial": "partial",
        "first_final": "1st-final",
        "second_final": "2nd-final",
    }.get(st, st)


def _parse_session_type(raw: str | None) -> str | None:
    if raw and raw in VALID_SESSION_TYPES:
        return raw
    return None


def _is_tp_material(material: Material) -> bool:
    """Practical (TP) rows duplicate the lecture material with 'tp' in the name."""
    name = (material.material_name or "").strip()
    return bool(re.search(r"\btp\b", name, re.IGNORECASE))


def _parse_dashboard_session_filter(raw: str | None) -> tuple[str | None, bool | None]:
    """Map dashboard session picker to exam session (for grading URLs) + TP-only filter."""
    if not raw:
        return None, None
    key = raw.strip().lower()
    if key == "tp":
        return Exam.SESSION_PARTIAL, True
    if key in VALID_SESSION_TYPES:
        return key, False
    return None, None


def _mark_values_equivalent(a, b) -> bool:
    """True if two mark values match for grading (UI step 0.5; ignore float noise)."""
    fa = round(float(a) * 2) / 2
    fb = round(float(b) * 2) / 2
    return fa == fb


def _rubric_locked_for_material_session(
    material_id: int, year: AcademicYear, session_type: str
) -> bool:
    """Rubric for this material + session is locked after second round is done for any section."""
    for ms in MaterialSection.objects.filter(material_id=material_id):
        if _session_second_round_complete(ms, year, session_type):
            return True
    return False


def _first_round_done_for_session(
    ms: MaterialSection, year: AcademicYear, session_type: str
) -> bool:
    return (
        FinalResult.objects.filter(
            exam__material_section=ms,
            exam__academic_year=year,
            exam__session_type=session_type,
        ).count()
        >= STUDENT_COUNT
    )


def _first_marks_saved_for_session(
    ms: MaterialSection, year: AcademicYear, session_type: str
) -> bool:
    """True once first corrector has saved the full marks grid for this session."""
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=session_type
        ).values_list("id", flat=True)
    )
    if not exams:
        return False
    qids = list(
        Question.objects.filter(
            material=ms.material, session_type=session_type
        ).values_list("id", flat=True)
    )
    if not qids:
        return False
    expected = len(exams) * len(qids)
    if expected == 0:
        return False
    first_marks_count = Mark.objects.filter(
        exam_id__in=exams,
        question_id__in=qids,
        corrector_role="first",
    ).count()
    return first_marks_count >= expected


def _second_gate_open(ms: MaterialSection, year: AcademicYear, session_type: str) -> bool:
    """Second corrector can start after first finalizes OR first saves full marks once."""
    return _first_round_done_for_session(ms, year, session_type) or _first_marks_saved_for_session(
        ms, year, session_type
    )


def _second_stage_should_hear_first_edits(ms: MaterialSection, year: AcademicYear, st: str) -> bool:
    """True once second round may be affected: first finalized, or any FinalResult row exists, or second entered marks."""
    if _first_round_done_for_session(ms, year, st):
        return True
    if FinalResult.objects.filter(
        exam__material_section=ms,
        exam__academic_year=year,
        exam__session_type=st,
    ).exists():
        return True
    if Mark.objects.filter(
        exam__material_section=ms,
        exam__academic_year=year,
        exam__session_type=st,
        corrector_role="second",
    ).exists():
        return True
    return False


def _second_marks_saved_for_session(
    ms: MaterialSection, year: AcademicYear, session_type: str
) -> bool:
    """True once second corrector has saved the full marks grid for this session."""
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=session_type
        ).values_list("id", flat=True)
    )
    if not exams:
        return False
    qids = list(
        Question.objects.filter(
            material=ms.material, session_type=session_type
        ).values_list("id", flat=True)
    )
    if not qids:
        return False
    expected = len(exams) * len(qids)
    if expected == 0:
        return False
    second_marks_count = Mark.objects.filter(
        exam_id__in=exams,
        question_id__in=qids,
        corrector_role="second",
    ).count()
    return second_marks_count >= expected


def _session_second_round_complete(
    ms: MaterialSection, year: AcademicYear, session_type: str
) -> bool:
    exams = Exam.objects.filter(
        material_section=ms, academic_year=year, session_type=session_type
    )
    if exams.count() < STUDENT_COUNT:
        return False
    frs = list(FinalResult.objects.filter(exam__in=exams))
    if len(frs) < STUDENT_COUNT:
        return False
    return all(fr.second_round_complete for fr in frs)


def _media_url_from_path(abs_path: str) -> str:
    rel = Path(abs_path).relative_to(settings.MEDIA_ROOT)
    base = settings.MEDIA_URL
    if not base.startswith("/"):
        base = "/" + base
    return base.rstrip("/") + "/" + str(rel).replace("\\", "/")


def _print_sheet_url(url: str) -> str:
    """For dashboard “Print out”: open print/PDF flow (template listens for print=1)."""
    if not url or "print=1" in url:
        return url
    return f"{url}{'&' if '?' in url else '?'}print=1"


_MARKS_REPORT_PRINTBAR_MARKERS = ('class="no-print"', "class='no-print'")


def _inject_saved_marks_print_toolbar(html: str, *, autoprint: bool) -> str:
    """Older saved HTML files omit the screen-only Print bar; inject it when missing."""
    needs_bar = not any(m in html for m in _MARKS_REPORT_PRINTBAR_MARKERS)
    out = html
    if needs_bar:
        inject = (
            '<div class="no-print" id="saved-report-printbar" style="padding:10px 12px;text-align:center;'
            "background:#e2e8f0;border-bottom:1px solid #cbd5e1;direction:ltr\">"
            '<button type="button" style="padding:8px 20px;font:14px system-ui,sans-serif;cursor:pointer;'
            "background:#2563eb;color:#fff;border:none;border-radius:6px;\" "
            'onclick="window.print()">Print</button></div>'
            "<style>@media print{#saved-report-printbar{display:none!important}}</style>"
        )
        out, n = re.subn(r"(?i)<body[^>]*>", lambda m: m.group(0) + inject, out, count=1)
        if not n:
            return html
    if autoprint and "URLSearchParams(window.location.search).get(\"print\")" not in out:
        script = (
            "<script>(function(){try{"
            'if(new URLSearchParams(window.location.search).get("print")!=="1")return;'
            "window.addEventListener(\"load\",function(){"
            "window.setTimeout(function(){window.print();},900);"
            "});}catch(e){}})();</script>"
        )
        out, _ = re.subn(r"(?i)</body>", lambda m: script + m.group(0), out, count=1)
    return out


@login_required
def saved_marks_report_view(request, report_id: int):
    """Serve a saved marks HTML file; add Print UI to snapshots generated before the toolbar existed."""
    rep = get_object_or_404(MarksReport, pk=report_id)
    if rep.user_id != request.user.id:
        return render(
            request,
            "core/error.html",
            {"message": "You cannot open this report."},
            status=403,
        )
    path = Path(rep.file_path)
    if not path.is_file():
        raise Http404("Report file missing")
    try:
        resolved = path.resolve()
        resolved.relative_to(Path(settings.MEDIA_ROOT).resolve())
    except (OSError, ValueError):
        raise Http404("Invalid report path")
    html = resolved.read_text(encoding="utf-8")
    autoprint = request.GET.get("print") == "1"
    html = _inject_saved_marks_print_toolbar(html, autoprint=autoprint)
    return HttpResponse(html, content_type="text/html; charset=utf-8")


def _normalize_phone_for_whatsapp(phone: str) -> str:
    # Keep a WhatsApp-compatible number: digits only, no leading plus.
    return "".join(ch for ch in (phone or "") if ch.isdigit())


def _notification_whatsapp_contact(user, role_label: str = "") -> str:
    """One readable WhatsApp line for in-app notifications (wa.me uses digits only)."""
    if not user:
        return ""
    digits = _normalize_phone_for_whatsapp(getattr(user, "phone_number", ""))
    if not digits:
        return ""
    name = user.get_full_name() or user.username
    prefix = f"{role_label} " if role_label else ""
    return f"{prefix}{name}: +{digits} — https://wa.me/{digits}"


def _notification_whatsapp_contacts_lines(*pairs: tuple) -> str:
    """pairs: (user, role_label) — skips missing users or empty phones."""
    parts = []
    for u, lbl in pairs:
        line = _notification_whatsapp_contact(u, lbl)
        if line:
            parts.append(line)
    if not parts:
        return ""
    return " | " + " · ".join(parts)

def _send_whatsapp_message(phone: str, message: str) -> dict:
    """
    Auto-send WhatsApp via Twilio when configured.
    Fallback behavior is manual wa.me links (handled by frontend).
    """
    provider = (getattr(settings, "WHATSAPP_PROVIDER", "link") or "link").strip().lower()
    if provider != "twilio":
        return {"ok": False, "error": "Auto-send disabled (WHATSAPP_PROVIDER is not 'twilio')."}

    account_sid = (getattr(settings, "TWILIO_ACCOUNT_SID", "") or "").strip()
    auth_token = (getattr(settings, "TWILIO_AUTH_TOKEN", "") or "").strip()
    from_number = (getattr(settings, "TWILIO_WHATSAPP_FROM", "") or "").strip()
    if not account_sid or not auth_token or not from_number:
        return {
            "ok": False,
            "error": "Missing Twilio config (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM).",
        }

    to_number = f"whatsapp:+{phone}"
    from_number = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
    endpoint = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = urlencode({"To": to_number, "From": from_number, "Body": message}).encode("utf-8")
    auth = b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    req = Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as resp:
            if 200 <= resp.status < 300:
                return {"ok": True}
            return {"ok": False, "error": f"Twilio returned status {resp.status}."}
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return {"ok": False, "error": f"Twilio HTTP {exc.code}: {body[:220]}".strip()}
    except URLError as exc:
        return {"ok": False, "error": f"Twilio connection error: {exc.reason}"}
    except Exception as exc:
        return {"ok": False, "error": f"Twilio send failed: {exc}"}


def _json_body(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except json.JSONDecodeError:
        return {}


def _notify_users(users, message: str):
    for u in users:
        if not u:
            continue
        Notification.objects.create(user=u, message=message)


def _notify_admins(message: str):
    UserModel = get_user_model()
    admins = UserModel.objects.filter(
        Q(is_superuser=True) | Q(panel_manager=True), is_active=True
    ).distinct()
    _notify_users(list(admins), message)


def _build_whatsapp_payload(
    ms: MaterialSection, year: AcademicYear, st: str, *, revision: bool = False
):
    second = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="second"
        )
        .select_related("corrector")
        .first()
    )
    if not second or not second.corrector:
        return {"ok": False, "error": "No second corrector is assigned for this material/year."}
    phone = _normalize_phone_for_whatsapp(getattr(second.corrector, "phone_number", ""))
    if not phone:
        return {
            "ok": False,
            "error": f"Second corrector ({second.corrector.username}) has no phone number.",
        }
    if revision:
        msg = (
            f"Hello {second.corrector.get_full_name() or second.corrector.username}, "
            f"the first corrector has updated marks for {ms.material.material_name} "
            f"({ms.section.name}) — {year.year_label} — {_session_label_short(st)} exam. "
            "Please open the grading system and refresh the page to review the latest first-corrector marks."
        )
    else:
        msg = (
            f"Hello {second.corrector.get_full_name() or second.corrector.username}, "
            f"the first corrector has finished entering marks for {ms.material.material_name} "
            f"({ms.section.name}) — {year.year_label} — {_session_label_short(st)} exam. "
            "The examination papers have been submitted to the administration. "
            "Please open the grading system and enter your marks using the same rubric."
        )
    wa_url = f"https://wa.me/{phone}?text={quote(msg)}"
    return {
        "ok": True,
        "phone": phone,
        "corrector": second.corrector.get_full_name() or second.corrector.username,
        "message": msg,
        "wa_url": wa_url,
    }


def _store_saved_marks_report(user, ms: MaterialSection, year: AcademicYear, st: str, role: str):
    ctx = _corrector_marks_sheet_render_context(ms, year, st, role)
    html = render_to_string("core/report_final_marks.html", ctx)
    folder = (
        Path(settings.MEDIA_ROOT)
        / "saved_marks_reports"
        / f"user_{user.id}"
        / f"ms_{ms.id}"
        / year.year_label.replace("/", "-")
    )
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{st}_{role}_{ms.section.name}_{ms.material.material_name}_{int(user.id)}.html"
    safe_filename = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in filename)
    out_path = folder / safe_filename
    out_path.write_text(html, encoding="utf-8")

    base_qs = MarksReport.objects.filter(
        user=user,
        material_section=ms,
        academic_year=year,
        role=role,
        session_type=st,
        report_kind=MarksReport.REPORT_CORRECTOR,
    ).order_by("-id")
    rec = base_qs.first()
    if rec:
        # Keep a single dashboard row for this context; overwrite file/path.
        base_qs.exclude(id=rec.id).delete()
        rec.file_path = str(out_path)
        rec.save(update_fields=["file_path"])
    else:
        rec = MarksReport.objects.create(
            user=user,
            material_section=ms,
            academic_year=year,
            role=role,
            session_type=st,
            report_kind=MarksReport.REPORT_CORRECTOR,
            file_path=str(out_path),
        )
    return rec


def _is_panel_manager(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(getattr(user, "panel_manager", False))


def _doctor_display_name(user) -> str:
    if not user:
        return "—"
    return (user.get_full_name() or "").strip() or user.username


def _report_saved_at(report: MarksReport | None):
    if not report:
        return None
    if report.file_path:
        path = Path(report.file_path)
        if path.is_file():
            return datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.get_current_timezone()
            )
    return report.created_at


def _marks_saved_at(
    ms: MaterialSection, year: AcademicYear, session_type: str, role: str
):
    msc = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role=role
        )
        .select_related("corrector")
        .first()
    )
    if not msc or not msc.corrector:
        return None
    report = (
        MarksReport.objects.filter(
            user=msc.corrector,
            material_section=ms,
            academic_year=year,
            session_type=session_type,
            role=role,
            report_kind=MarksReport.REPORT_CORRECTOR,
        )
        .order_by("-id")
        .first()
    )
    return _report_saved_at(report)


def _average_saved_at(ms: MaterialSection, year: AcademicYear, session_type: str):
    report = (
        MarksReport.objects.filter(
            material_section=ms,
            academic_year=year,
            session_type=session_type,
            report_kind=MarksReport.REPORT_FINAL_AVERAGE,
        )
        .order_by("-id")
        .first()
    )
    return _report_saved_at(report)


def _build_admin_grading_overview_rows():
    """One row per material / year / session with both correctors and shared average."""
    rows = []
    seen_ctx = set()
    for msc in (
        MaterialSectionCorrector.objects.select_related(
            "material_section",
            "material_section__material",
            "material_section__section",
            "academic_year",
        ).all()
    ):
        ms = msc.material_section
        y = msc.academic_year
        if _is_tp_material(ms.material):
            continue
        ctx_key = (ms.id, y.id)
        if ctx_key in seen_ctx:
            continue
        seen_ctx.add(ctx_key)

        first_msc = (
            MaterialSectionCorrector.objects.filter(
                material_section=ms, academic_year=y, role="first"
            )
            .select_related("corrector")
            .first()
        )
        second_msc = (
            MaterialSectionCorrector.objects.filter(
                material_section=ms, academic_year=y, role="second"
            )
            .select_related("corrector")
            .first()
        )

        for st in ("partial", "first_final", "second_final"):
            has_rubric = Question.objects.filter(
                material=ms.material, session_type=st
            ).exists()
            first_ready = _first_marks_saved_for_session(
                ms, y, st
            ) or _first_round_done_for_session(ms, y, st)
            second_ready = _second_marks_saved_for_session(
                ms, y, st
            ) or _session_second_round_complete(ms, y, st)
            average_ready = _session_second_round_complete(ms, y, st)
            rows.append(
                {
                    "ms_id": ms.id,
                    "year_id": y.id,
                    "session_type": st,
                    "session_label": _session_label_short(st),
                    "label": f"{ms.material.material_name} — {ms.section.name}",
                    "year_label": y.year_label,
                    "first_doctor": _doctor_display_name(
                        first_msc.corrector if first_msc else None
                    ),
                    "second_doctor": _doctor_display_name(
                        second_msc.corrector if second_msc else None
                    ),
                    "first_ready": first_ready,
                    "second_ready": second_ready,
                    "average_ready": average_ready,
                    "first_marks_date": (
                        _marks_saved_at(ms, y, st, "first") if first_ready else None
                    ),
                    "second_marks_date": (
                        _marks_saved_at(ms, y, st, "second") if second_ready else None
                    ),
                    "average_date": (
                        _average_saved_at(ms, y, st) if average_ready else None
                    ),
                    "print_first": bool(has_rubric and first_ready),
                    "print_second": bool(has_rubric and second_ready),
                    "print_average": bool(has_rubric and average_ready),
                }
            )
    rows.sort(key=lambda r: (r["year_label"], r["label"], r["session_type"]))
    return rows


def _build_my_marks_report_pages(
    ms: MaterialSection, year: AcademicYear, session_type: str, role: str
):
    """Two-column printable layout (used with report_final_marks.html)."""
    ensure_exams(ms, year, session_type)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=session_type
        ).order_by("exam_number")
    )
    rows = []
    for ex in exams:
        total = total_on_100(ms.material, ex, role)
        rows.append(
            {
                "number": ex.exam_number,
                "total": f"{total:.2f}",
                "words": "",
            }
        )
    return _build_printable_pages_first36_then_rest(rows)


def _corrector_marks_sheet_render_context(
    ms: MaterialSection, year: AcademicYear, session_type: str, role: str
) -> dict:
    """Same official Arabic two-column sheet as final averages (report_final_marks.html)."""
    pages = _build_my_marks_report_pages(ms, year, session_type, role)
    first_c = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="first"
        )
        .select_related("corrector")
        .first()
    )
    second_c = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="second"
        )
        .select_related("corrector")
        .first()
    )
    corrector_first = (
        first_c.corrector.get_full_name() or first_c.corrector.username
        if first_c and first_c.corrector
        else ""
    )
    corrector_second = (
        second_c.corrector.get_full_name() or second_c.corrector.username
        if second_c and second_c.corrector
        else ""
    )
    corrector_meta_line = corrector_first if role == "first" else corrector_second
    session_ar = (
        "جزئية"
        if session_type == "partial"
        else ("نهائية أولى" if session_type == "first_final" else "نهائية ثانية")
    )
    return {
        "material_section": ms,
        "academic_year": year,
        "session_ar": session_ar,
        "sheet_title_ar": _arabic_sheet_title(session_type),
        "pages": pages,
        "corrector_first": corrector_first,
        "corrector_second": corrector_second,
        "corrector_meta_line": corrector_meta_line,
        "corrector_checkbox_role": role,
        "heading_main_ar": "علامات المصحّح / 100",
        "institution_logo_url": _institution_logo_url(),
    }


def _validate_export_access(
    request, ms_id: int, year_id: int, session_type: str, role: str, kind: str
):
    st = _parse_session_type(session_type)
    if not st or role not in ("first", "second") or kind not in ("my_marks", "averages"):
        raise Http404

    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    staff = _is_panel_manager(request.user)
    if not staff:
        assigned = MaterialSectionCorrector.objects.filter(
            material_section=ms,
            academic_year=year,
            corrector=request.user,
            role=role,
        ).exists()
        if not assigned:
            return None, None, None, None, render(
                request,
                "core/error.html",
                {"message": "You are not assigned to this material and year."},
                status=403,
            )

        first_done = _first_round_done_for_session(ms, year, st)
        second_done = _session_second_round_complete(ms, year, st)
        if kind == "my_marks":
            my_marks_ready = first_done if role == "first" else second_done
            if not my_marks_ready:
                return None, None, None, None, render(
                    request,
                    "core/error.html",
                    {
                        "message": "Your marks file is not ready yet. Finalize your correction stage first."
                    },
                    status=400,
                )
        else:
            if not second_done:
                return None, None, None, None, render(
                    request,
                    "core/error.html",
                    {
                        "message": "The averages file is available only after the second corrector finalizes."
                    },
                    status=400,
                )

    material = ms.material
    if not staff and not Question.objects.filter(
        material=material, session_type=st
    ).exists():
        return None, None, None, None, render(
            request,
            "core/error.html",
            {"message": "No rubric found for this material and exam type."},
            status=400,
        )

    ensure_exams(ms, year, st)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        ).order_by("exam_number")
    )
    return st, ms, year, exams, None


def ensure_exams(
    material_section: MaterialSection,
    academic_year: AcademicYear,
    session_type: str,
):
    for n in range(1, STUDENT_COUNT + 1):
        Exam.objects.get_or_create(
            material_section=material_section,
            academic_year=academic_year,
            exam_number=n,
            session_type=session_type,
        )


@require_http_methods(["GET", "POST"])
def login_page(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user:
            login(request, user)
            return redirect("dashboard")
        return render(
            request,
            "core/login.html",
            {"error": "Invalid username or password."},
        )
    return render(request, "core/login.html")


@require_http_methods(["GET", "POST"])
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def adminn_view(request):
    return render(request, 'adminn.html')

# --- CORRECTOR DASHBOARD ---
@login_required
def corrector_dashboard(request):
    ready_exports = []
    saved_reports = []
    by_ctx = {}
    for msc in (
        MaterialSectionCorrector.objects.filter(corrector=request.user)
        .select_related(
            "material_section",
            "material_section__material",
            "material_section__section",
            "academic_year",
        )
        .order_by(
            "academic_year__year_label",
            "material_section__material__material_name",
        )
    ):
        key = (msc.material_section_id, msc.academic_year_id)
        row = by_ctx.get(key)
        if row is None:
            row = {
                "ms": msc.material_section,
                "year": msc.academic_year,
                "roles": set(),
            }
            by_ctx[key] = row
        row["roles"].add(msc.role)
    for row in by_ctx.values():
        ms = row["ms"]
        year = row["year"]
        roles = sorted(row["roles"])
        for role in roles:
            role_label = "First corrector" if role == "first" else "Second corrector"
            for st in VALID_SESSION_TYPES:
                first_done = _first_round_done_for_session(ms, year, st)
                second_done = _session_second_round_complete(ms, year, st)
                my_marks_ready = first_done if role == "first" else second_done
                averages_ready = second_done
                if not my_marks_ready and not averages_ready:
                    continue
                ready_exports.append(
                    {
                        "ms_id": ms.id,
                        "year_id": year.id,
                        "role": role,
                        "role_label": role_label,
                        "session_type": st,
                        "label": f"{ms.material.material_name} — {ms.section.name}",
                        "year_label": year.year_label,
                        "session_label": _session_label_short(st),
                        "my_marks_ready": my_marks_ready,
                        "averages_ready": averages_ready,
                    }
                )

    rep_list = list(
        MarksReport.objects.filter(user=request.user)
        .select_related(
            "material_section",
            "material_section__material",
            "material_section__section",
            "academic_year",
        )
        .order_by("-created_at")[:40]
    )
    final_keys_from_reports = {
        (r.material_section_id, r.academic_year_id, r.session_type)
        for r in rep_list
        if r.report_kind == MarksReport.REPORT_FINAL_AVERAGE
    }
    for rep in rep_list:
        if rep.report_kind == MarksReport.REPORT_FINAL_AVERAGE:
            rlabel = "Final average /100"
        else:
            rlabel = "First corrector" if rep.role == "first" else "Second corrector"
        saved_reports.append(
            {
                "url": _print_sheet_url(reverse("saved_marks_report", args=[rep.id])),
                "label": f"{rep.material_section.material.material_name} — {rep.material_section.section.name}",
                "year_label": rep.academic_year.year_label,
                "session_label": _session_label_short(rep.session_type),
                "role_label": rlabel,
                "created_at": rep.created_at,
            }
        )

    computed_by_key: dict[tuple[int, int, str], dict] = {}
    for msc in MaterialSectionCorrector.objects.filter(corrector=request.user).select_related(
        "material_section",
        "material_section__material",
        "material_section__section",
        "academic_year",
    ):
        ms = msc.material_section
        year = msc.academic_year
        for st in VALID_SESSION_TYPES:
            if not _session_second_round_complete(ms, year, st):
                continue
            key = (ms.id, year.id, st)
            if key in final_keys_from_reports or key in computed_by_key:
                continue
            rel = reverse("printable_final_averages", args=[ms.id, year.id, st])
            computed_by_key[key] = {
                "url": _print_sheet_url(request.build_absolute_uri(rel)),
                "label": f"{ms.material.material_name} — {ms.section.name}",
                "year_label": year.year_label,
                "session_label": _session_label_short(st),
                "role_label": "Final average /100 (open to print)",
                "created_at": timezone.now(),
            }

    saved_reports = list(computed_by_key.values()) + saved_reports
    saved_reports.sort(key=lambda r: r["created_at"], reverse=True)
    saved_reports = saved_reports[:40]

    departments = Department.objects.all().order_by("name")
    academic_years = AcademicYear.objects.order_by("-year_label")
    notifications = list(
        Notification.objects.filter(user=request.user).order_by("-created_at")[:20]
    )
    unread = Notification.objects.filter(user=request.user, is_read=False).count()

    return render(
        request,
        "core/dashboard.html",
        {
            "departments": departments,
            "academic_years": academic_years,
            "notifications": notifications,
            "unread_count": unread,
            "ready_exports": ready_exports,
            "saved_reports": saved_reports,
        },
    )


@login_required
def download_grading_export(
    request, ms_id: int, year_id: int, session_type: str, role: str, kind: str
):
    st, ms, year, exams, error_response = _validate_export_access(
        request, ms_id, year_id, session_type, role, kind
    )
    if error_response:
        return error_response
    st_label = _session_slug_for_files(st)
    material = ms.material
    label = f"{material.material_name} ({ms.section.name}) — {st_label}"

    if kind == "my_marks":
        path = build_totals_workbook(
            material=material,
            material_label=label,
            year_label=year.year_label,
            exams=exams,
            corrector_role=role,
            role_label=role,
        )
    else:
        fr_map = {
            fr.exam_id: fr
            for fr in FinalResult.objects.filter(exam__in=exams)
        }
        path = build_average_only_workbook(
            material_label=label,
            year_label=year.year_label,
            exams=exams,
            results=fr_map,
        )

    fp = Path(path)
    return FileResponse(
        fp.open("rb"),
        as_attachment=False,
        filename=fp.name,
    )


@login_required
def preview_grading_export(
    request, ms_id: int, year_id: int, session_type: str, role: str, kind: str
):
    st, ms, year, exams, error_response = _validate_export_access(
        request, ms_id, year_id, session_type, role, kind
    )
    if error_response:
        return error_response

    if kind == "my_marks":
        rows = [
            {
                "exam_number": ex.exam_number,
                "total": f"{total_on_100(ms.material, ex, role):.2f}",
            }
            for ex in exams
        ]
        return render(
            request,
            "core/export_preview.html",
            {
                "kind": kind,
                "rows": rows,
                "material_name": ms.material.material_name,
                "section_name": ms.section.name,
                "year_label": year.year_label,
                "session_label": _session_label_short(st),
                "role_label": "First corrector" if role == "first" else "Second corrector",
                "ms_id": ms.id,
                "year_id": year.id,
                "session_type": st,
                "role": role,
            },
        )

    fr_map = {fr.exam_id: fr for fr in FinalResult.objects.filter(exam__in=exams)}
    rows = []
    staff = _is_panel_manager(request.user)
    for ex in exams:
        fr = fr_map.get(ex.id)
        if not fr:
            if staff:
                rows.append(
                    {
                        "exam_number": ex.exam_number,
                        "first_total": "—",
                        "second_total": "—",
                        "average": "—",
                        "difference": "—",
                        "highlight": False,
                        "pending": True,
                    }
                )
            continue
        if not fr.second_round_complete and staff:
            rows.append(
                {
                    "exam_number": ex.exam_number,
                    "first_total": round(fr.first_total, 2),
                    "second_total": "—",
                    "average": "—",
                    "difference": "—",
                    "highlight": False,
                    "pending": True,
                }
            )
            continue
        if not fr.second_round_complete:
            continue
        rows.append(
            {
                "exam_number": ex.exam_number,
                "first_total": round(fr.first_total, 2),
                "second_total": round(fr.second_total, 2),
                "average": round(fr.average, 2),
                "difference": round(fr.difference, 2),
                "highlight": fr.difference >= 10,
                "pending": False,
            }
        )
    return render(
        request,
        "core/export_preview.html",
        {
            "kind": kind,
            "rows": rows,
            "material_name": ms.material.material_name,
            "section_name": ms.section.name,
            "year_label": year.year_label,
            "session_label": _session_label_short(st),
            "role_label": "First corrector" if role == "first" else "Second corrector",
            "ms_id": ms.id,
            "year_id": year.id,
            "session_type": st,
            "role": role,
        },
    )


@login_required
@require_POST
def api_materials(request):
    """List material sections for dashboard filters (TP vs lecture by name)."""
    data = _json_body(request)
    dept_ids = data.get("department_ids") or []
    section_name = (data.get("section") or "").strip()
    level = (data.get("level") or "").strip()
    semester = (data.get("semester") or "").strip()
    year_raw = data.get("academic_year_id")
    session_raw = (data.get("session_type") or data.get("exam_type") or "").strip()
    st, tp_only = _parse_dashboard_session_filter(session_raw)
    if (
        not dept_ids
        or not section_name
        or not level
        or not semester
        or year_raw in (None, "")
    ):
        return JsonResponse(
            {
                "error": "Select at least one department, section, level, semester, and academic year.",
            },
            status=400,
        )
    if not st:
        return JsonResponse(
            {
                "error": "Select exam session (partial, TP, first final, or second final).",
            },
            status=400,
        )
    try:
        year_id = int(year_raw)
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid academic year."}, status=400)
    if not AcademicYear.objects.filter(pk=year_id).exists():
        return JsonResponse({"error": "Invalid academic year."}, status=400)
    # Accept equivalent naming used by different departments.
    section_aliases = {
        "french": ["french", "francais", "français"],
        "francais": ["french", "francais", "français"],
        "français": ["french", "francais", "français"],
        "english": ["english", "anglais"],
        "anglais": ["english", "anglais"],
    }
    section_key = section_name.lower()
    allowed_sections = section_aliases.get(section_key, [section_key])
    section_filter = Q()
    for name in allowed_sections:
        section_filter |= Q(section__name__iexact=name)
    qs = MaterialSection.objects.select_related(
        "material", "material__department", "section"
    ).filter(
        material__department_id__in=dept_ids,
        material__level=level,
        material__semester=semester,
    ).filter(section_filter)
    ms_list = list(qs)
    ms_ids = [ms.id for ms in ms_list]
    roles_by_ms: dict[int, set[str]] = {}
    if ms_ids:
        for ms_id, role in MaterialSectionCorrector.objects.filter(
            material_section_id__in=ms_ids,
            academic_year_id=year_id,
            corrector=request.user,
        ).values_list("material_section_id", "role"):
            roles_by_ms.setdefault(ms_id, set()).add(role)
    out = []
    panel_mgr = _is_panel_manager(request.user)
    for ms in ms_list:
        m = ms.material
        is_tp = _is_tp_material(m)
        if tp_only is True and not is_tp:
            continue
        if tp_only is False and is_tp:
            continue
        r = roles_by_ms.get(ms.id, frozenset())
        out.append(
            {
                "id": ms.id,
                "label": (
                    f"{m.material_name} — {ms.section.name} "
                    f"({m.department.name}, Year {m.level}, Semester {m.semester})"
                ),
                "material_id": m.id,
                "can_first": "first" in r,
                "can_second": "second" in r,
            }
        )
    return JsonResponse({"materials": out, "panel_manager": panel_mgr})


@ensure_csrf_cookie
@login_required
def grading_page(request, ms_id: int, year_id: int, session_type: str, role: str):
    st = _parse_session_type(session_type)
    if not st:
        return render(
            request,
            "core/error.html",
            {"message": "Invalid exam type. Use partial, first final, or second final."},
            status=400,
        )
    if role not in ("first", "second"):
        return render(
            request,
            "core/error.html",
            {"message": "Invalid corrector role."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role=role,
    ).first()
    if not msc and not _is_panel_manager(request.user):
        return render(
            request,
            "core/error.html",
            {"message": "You are not assigned as this corrector for this material and year."},
            status=403,
        )
    ensure_exams(ms, year, st)
    session_label = _session_label_short(st)
    return render(
        request,
        "core/grading.html",
        {
            "material_section": ms,
            "academic_year": year,
            "session_type": st,
            "session_label": session_label,
            "role": role,
            "role_label": "First corrector" if role == "first" else "Second corrector",
            "material_name": ms.material.material_name,
            "year_label": year.year_label,
            "staff_mode": _is_panel_manager(request.user),
        },
    )


@login_required
def api_session(request, ms_id: int, year_id: int, session_type: str):
    st = _parse_session_type(session_type)
    if not st:
        return JsonResponse(
            {"error": "Invalid exam type (partial, first_final, second_final)."},
            status=400,
        )
    role = request.GET.get("role")
    if role not in ("first", "second"):
        return JsonResponse({"error": "Invalid role."}, status=400)
    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role=role,
    ).first()
    staff = _is_panel_manager(request.user)
    if not msc and not staff:
        return JsonResponse({"error": "Forbidden."}, status=403)

    ensure_exams(ms, year, st)
    material = ms.material
    questions = list(
        Question.objects.filter(material=material, session_type=st).values(
            "id", "question_title", "part_title", "part_mark", "session_type"
        )
    )
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        )
        .order_by("exam_number")
        .values("id", "exam_number")
    )

    marks = {}
    if questions:
        qids = [q["id"] for q in questions]
        eids = [e["id"] for e in exams]
        for row in Mark.objects.filter(
            exam_id__in=eids,
            question_id__in=qids,
            corrector_role=role,
        ).values("exam_id", "question_id", "mark"):
            marks[f"{row['exam_id']}:{row['question_id']}"] = row["mark"]

    first_finalize_done = _second_gate_open(ms, year, st)
    if staff and role == "second":
        first_finalize_done = True

    first_marks_complete = (
        _first_marks_saved_for_session(ms, year, st) if role == "first" else False
    )
    first_handoff_sent = (
        _first_round_done_for_session(ms, year, st) if role == "first" else False
    )
    second_round_complete = (
        _session_second_round_complete(ms, year, st) if role == "second" else False
    )

    return JsonResponse(
        {
            "questions": questions,
            "exams": exams,
            "marks": marks,
            "role": role,
            "session_type": st,
            "first_finalize_done": first_finalize_done,
            "first_marks_complete": first_marks_complete,
            "first_handoff_sent": first_handoff_sent,
            "second_round_complete": second_round_complete,
            "student_count": STUDENT_COUNT,
            "staff_editor": staff,
        }
    )


@login_required
@require_POST
def api_save_rubric(request):
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role="first",
    ).first()
    if not msc and not staff:
        return JsonResponse({"error": "Only the assigned first corrector can edit the rubric."}, status=403)

    material = ms.material
    if not staff and _rubric_locked_for_material_session(material.id, year, st):
        return JsonResponse(
            {
                "error": "Rubric for this exam type is locked after the second round is finalized "
                "for this course."
            },
            status=400,
        )

    parts = data.get("parts") or []
    if not parts:
        return JsonResponse({"error": "Add at least one question part."}, status=400)

    with transaction.atomic():
        Question.objects.filter(material=material, session_type=st).delete()
        for p in parts:
            Question.objects.create(
                material=material,
                session_type=st,
                question_title=(p.get("question_title") or "")[:100],
                part_title=(p.get("part_title") or "")[:100],
                part_mark=float(p.get("part_mark") or 0),
            )

    questions = list(
        Question.objects.filter(material=material, session_type=st).values(
            "id", "question_title", "part_title", "part_mark", "session_type"
        )
    )
    return JsonResponse({"ok": True, "questions": questions})


@login_required
@require_POST
def api_save_marks(request):
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    role = data.get("role")
    if role not in ("first", "second"):
        return JsonResponse({"error": "Invalid role."}, status=400)
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role=role,
    ).first()
    if not msc and not staff:
        return JsonResponse({"error": "Forbidden."}, status=403)

    if role == "second" and not staff:
        if not _second_gate_open(ms, year, st):
            return JsonResponse(
                {
                    "error": "Wait until the first corrector saves marks (or finalizes) for this exam type."
                },
                status=400,
            )

    ensure_exams(ms, year, st)
    material = ms.material
    rows = data.get("marks") or []
    exam_by_num = {
        e.exam_number: e
        for e in Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        )
    }

    changed_marks = 0
    with transaction.atomic():
        for r in rows:
            num = int(r.get("exam_number"))
            qid = int(r.get("question_id"))
            val = float(r.get("mark") or 0)
            exam = exam_by_num.get(num)
            if not exam:
                continue
            q = Question.objects.filter(
                id=qid, material=material, session_type=st
            ).first()
            if not q:
                continue
            max_m = q.part_mark
            if val > max_m:
                val = max_m
            if val < 0:
                val = 0
            existing = Mark.objects.filter(
                exam=exam, question=q, corrector_role=role
            ).first()
            old_mark = float(existing.mark) if existing else None
            old_corrector_id = existing.corrector_id if existing else None
            if (
                old_mark is None
                or not _mark_values_equivalent(old_mark, val)
                or old_corrector_id != request.user.id
            ):
                changed_marks += 1
            Mark.objects.update_or_create(
                exam=exam,
                question=q,
                corrector_role=role,
                defaults={"mark": val, "corrector": request.user},
            )

    # If second round is already closed, keep FinalResult in sync with any later edits
    # so the final page (/final/...) reflects current marks immediately.
    if changed_marks > 0 and _session_second_round_complete(ms, year, st):
        exams_for_sync = list(
            Exam.objects.filter(
                material_section=ms, academic_year=year, session_type=st
            ).order_by("exam_number")
        )
        with transaction.atomic():
            for ex in exams_for_sync:
                fr, _ = FinalResult.objects.get_or_create(exam=ex)
                fr.first_total = total_on_100(material, ex, "first")
                fr.second_total = total_on_100(material, ex, "second")
                fr.second_round_complete = True
                fr.save()
        try:
            _persist_final_average_printables(ms, year, st)
        except Exception:
            pass

    second_notified_of_first_revision = False
    if (
        role == "first"
        and changed_marks > 0
        and _second_gate_open(ms, year, st)
        and _second_stage_should_hear_first_edits(ms, year, st)
    ):
        second_notified_of_first_revision = True
        request.user.refresh_from_db(
            fields=["phone_number", "first_name", "last_name", "username"]
        )
        st_label = _session_slug_for_files(st)
        label = f"{material.material_name} ({ms.section.name}) — {st_label}"
        session_en = _session_label_short(st)
        second = MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="second"
        ).select_related("corrector")
        second_users = [s.corrector for s in second if s.corrector]
        _notify_users(
            second_users,
            (
                f"The first corrector has updated marks for the {session_en} session — {label} ({year.year_label}). "
                "Please open or refresh the grading page for this exam and review the first-corrector grid and totals before continuing."
                + _notification_whatsapp_contacts_lines((request.user, "First corrector"))
            ),
        )
        _notify_admins(
            f"The first corrector updated marks after handoff for {label} ({year.year_label}, {session_en})."
            + _notification_whatsapp_contacts_lines(
                (request.user, "First corrector"),
                (_get_second_corrector(ms, year), "Second corrector"),
            )
        )

    whatsapp_to_second = None
    if (
        second_notified_of_first_revision
        and _first_round_done_for_session(ms, year, st)
    ):
        whatsapp_to_second = _build_whatsapp_payload(ms, year, st, revision=True)

    report = _store_saved_marks_report(request.user, ms, year, st, role)
    payload = {"ok": True, "saved_report_url": _media_url_from_path(report.file_path)}
    if role == "first":
        payload["first_marks_complete"] = _first_marks_saved_for_session(ms, year, st)
    if whatsapp_to_second is not None:
        payload["whatsapp_to_second"] = whatsapp_to_second

    # Close second round when the first stage is open (saved grid or finalized), not only when
    # 80+ FinalResult rows exist — those rows are created on first "Send"/finalize, so requiring
    # _first_round_done here blocked closure when the first corrector only saved marks.
    if role == "second" and _second_gate_open(ms, year, st):
        if (
            _second_marks_saved_for_session(ms, year, st)
            and not _session_second_round_complete(ms, year, st)
        ):
            fin = _finalize_second_round(request, ms, year, st)
            payload["second_finalize"] = fin
        elif _session_second_round_complete(ms, year, st):
            # Session already closed (e.g. earlier finalize) but printable rows may be missing — repair.
            try:
                path = _persist_final_average_printables(ms, year, st)
                payload["final_printable_url"] = _media_url_from_path(path)
            except Exception:
                pass

    return JsonResponse(payload)


def _get_first_corrector(ms, year):
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms, academic_year=year, role="first"
    ).select_related("corrector").first()
    return msc.corrector if msc else None


def _get_second_corrector(ms, year):
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms, academic_year=year, role="second"
    ).select_related("corrector").first()
    return msc.corrector if msc else None


def _corrector_role_for_user(ms: MaterialSection, year: AcademicYear, user):
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms, academic_year=year, corrector=user
    ).first()
    return msc.role if msc else "second"


def _execute_finalize_first_round(ms: MaterialSection, year: AcademicYear, st: str):
    """Persist first-round /100 totals and produce first-corrector exports."""
    material = ms.material
    questions = list(Question.objects.filter(material=material, session_type=st))
    if not questions:
        return {"ok": False, "error": "Define the rubric for this exam type first."}

    ensure_exams(ms, year, st)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        ).order_by("exam_number")
    )
    first_round_was_done = _first_round_done_for_session(ms, year, st)

    with transaction.atomic():
        for ex in exams:
            fr, _ = FinalResult.objects.get_or_create(exam=ex)
            fr.first_total = total_on_100(material, ex, "first")
            fr.second_round_complete = False
            fr.save()

    st_label = _session_slug_for_files(st)
    label = f"{material.material_name} ({ms.section.name}) — {st_label}"
    path1 = build_rubric_and_marks_workbook(
        material=material,
        material_label=label,
        year_label=year.year_label,
        exams=exams,
        questions=questions,
        corrector_role="first",
        role_label="first",
    )
    path2 = build_totals_workbook(
        material=material,
        material_label=label,
        year_label=year.year_label,
        exams=exams,
        corrector_role="first",
        role_label="first",
    )
    whatsapp = _build_whatsapp_payload(ms, year, st)
    return {
        "ok": True,
        "first_round_was_done": first_round_was_done,
        "label": label,
        "st_label": st_label,
        "files": {
            "rubric_marks": _media_url_from_path(path1),
            "totals": _media_url_from_path(path2),
        },
        "whatsapp": whatsapp,
    }


def _persist_final_average_printables(ms: MaterialSection, year: AcademicYear, st: str) -> str:
    """Write final-average /100 HTML and attach one MarksReport row per corrector (same file)."""
    pages = _build_averages_report_pages(ms, year, st)
    session_ar = (
        "جزئية"
        if st == "partial"
        else ("نهائية أولى" if st == "first_final" else "نهائية ثانية")
    )
    first_c = MaterialSectionCorrector.objects.filter(
        material_section=ms, academic_year=year, role="first"
    ).select_related("corrector").first()
    second_c = MaterialSectionCorrector.objects.filter(
        material_section=ms, academic_year=year, role="second"
    ).select_related("corrector").first()
    corrector_first = (
        first_c.corrector.get_full_name() or first_c.corrector.username
        if first_c and first_c.corrector
        else ""
    )
    corrector_second = (
        second_c.corrector.get_full_name() or second_c.corrector.username
        if second_c and second_c.corrector
        else ""
    )
    html = render_to_string(
        "core/report_final_marks.html",
        {
            "material_section": ms,
            "academic_year": year,
            "session_ar": session_ar,
            "sheet_title_ar": _arabic_sheet_title(st),
            "pages": pages,
            "corrector_first": corrector_first,
            "corrector_second": corrector_second,
            "institution_logo_url": _institution_logo_url(),
        },
    )
    folder = (
        Path(settings.MEDIA_ROOT)
        / "saved_marks_reports"
        / f"ms_{ms.id}"
        / year.year_label.replace("/", "-")
    )
    folder.mkdir(parents=True, exist_ok=True)
    safe_st = st.replace("/", "_")
    out_path = folder / f"final_average_{safe_st}.html"
    out_path.write_text(html, encoding="utf-8")
    abs_path = str(out_path)

    # Attach one dashboard row per assigned corrector (same file). Using assignment rows
    # avoids missing users when helper lookups differ from who is actually assigned.
    for msc in MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        role__in=("first", "second"),
        corrector__isnull=False,
    ).select_related("corrector"):
        u = msc.corrector
        role = msc.role
        base_qs = MarksReport.objects.filter(
            user=u,
            material_section=ms,
            academic_year=year,
            session_type=st,
            report_kind=MarksReport.REPORT_FINAL_AVERAGE,
        ).order_by("-id")
        rec = base_qs.first()
        if rec:
            base_qs.exclude(id=rec.id).delete()
            rec.role = role
            rec.file_path = abs_path
            rec.save(update_fields=["file_path", "role"])
        else:
            MarksReport.objects.create(
                user=u,
                material_section=ms,
                academic_year=year,
                role=role,
                session_type=st,
                report_kind=MarksReport.REPORT_FINAL_AVERAGE,
                file_path=abs_path,
            )
    return abs_path


@login_required
@require_POST
def api_finalize_first(request):
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role="first",
    ).first()
    if not msc and not staff:
        return JsonResponse(
            {"error": "Only the assigned first corrector can finalize this step."},
            status=403,
        )

    result = _execute_finalize_first_round(ms, year, st)
    if not result.get("ok"):
        return JsonResponse({"error": result.get("error")}, status=400)

    first_round_was_done = result["first_round_was_done"]
    if not staff and not first_round_was_done:
        _notify_admins(
            f"First corrector finalized {result['label']} ({year.year_label}). "
            "Second-corrector stage is ready; WhatsApp message is prepared."
            + _notification_whatsapp_contacts_lines(
                (request.user, "First corrector"),
                (_get_second_corrector(ms, year), "Second corrector"),
            )
        )

    return JsonResponse(
        {
            "ok": True,
            "ready_for_second": not first_round_was_done,
            "whatsapp": result["whatsapp"],
            "files": result["files"],
        }
    )


@login_required
@require_POST
def api_first_corrector_send(request):
    """After all first-corrector marks are saved: submit round + notify admin + return WhatsApp for second."""
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role="first",
    ).first()
    if not msc and not staff:
        return JsonResponse(
            {"error": "Only the assigned first corrector can send this handoff."},
            status=403,
        )
    if not _first_marks_saved_for_session(ms, year, st):
        return JsonResponse(
            {"error": "Enter and save marks for all students and all rubric parts first."},
            status=400,
        )

    result = _execute_finalize_first_round(ms, year, st)
    if not result.get("ok"):
        return JsonResponse({"error": result.get("error")}, status=400)

    label = result["label"]
    if not result["first_round_was_done"]:
        in_app = (
            f"First corrector submitted marks for {label} ({year.year_label}). "
            "Examination papers are with the administration. Please open grading and enter second-corrector marks."
            + _notification_whatsapp_contacts_lines((request.user, "First corrector"))
        )
        second_slot = MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="second"
        ).select_related("corrector")
        second_users = [s.corrector for s in second_slot if s.corrector]
        _notify_users(second_users, in_app)
        second_u = _get_second_corrector(ms, year)
        _notify_admins(
            f"Administration: first corrector submitted exams for {label} ({year.year_label}). "
            "Papers received; second corrector has been notified (in-app and via WhatsApp when you open the link)."
            + _notification_whatsapp_contacts_lines(
                (request.user, "First corrector"),
                (second_u, "Second corrector"),
            )
        )

    return JsonResponse(
        {
            "ok": True,
            "already_sent_before": result["first_round_was_done"],
            "whatsapp": result["whatsapp"],
            "files": result["files"],
        }
    )


def _finalize_second_round(request, ms: MaterialSection, year: AcademicYear, st: str):
    """Complete second correction: totals, exports, final-average printables, dispute alerts."""
    staff = _is_panel_manager(request.user)
    if not staff and not _second_gate_open(ms, year, st):
        return {
            "ok": False,
            "error": (
                "The first corrector must save the full marks grid or submit (Send) "
                "before the second round can close."
            ),
        }

    material = ms.material
    questions = list(Question.objects.filter(material=material, session_type=st))
    if not questions:
        return {"ok": False, "error": "Rubric missing for this exam type."}

    ensure_exams(ms, year, st)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        ).order_by("exam_number")
    )

    with transaction.atomic():
        for ex in exams:
            fr, _ = FinalResult.objects.get_or_create(exam=ex)
            fr.first_total = total_on_100(material, ex, "first")
            fr.second_total = total_on_100(material, ex, "second")
            fr.second_round_complete = True
            fr.save()

    # Persist printable final averages immediately so dashboard links exist even if Excel export fails.
    final_printable_path = None
    try:
        final_printable_path = _persist_final_average_printables(ms, year, st)
    except Exception as exc:
        _notify_admins(
            f"Final average HTML could not be saved for {material.material_name} ({ms.section.name}) "
            f"({year.year_label}, {st}): {exc}"
        )

    st_label = _session_slug_for_files(st)
    label = f"{material.material_name} ({ms.section.name}) — {st_label}"
    path1 = build_rubric_and_marks_workbook(
        material=material,
        material_label=label,
        year_label=year.year_label,
        exams=exams,
        questions=questions,
        corrector_role="second",
        role_label="second",
    )
    path2 = build_totals_workbook(
        material=material,
        material_label=label,
        year_label=year.year_label,
        exams=exams,
        corrector_role="second",
        role_label="second",
    )

    fr_map = {fr.exam_id: fr for fr in FinalResult.objects.filter(exam__in=exams)}
    path_final = build_final_workbook(
        material_label=label,
        year_label=year.year_label,
        exams=exams,
        results=fr_map,
    )

    first_notifier = _get_first_corrector(ms, year)
    second_notifier = _get_second_corrector(ms, year)
    if first_notifier and not staff:
        _notify_users(
            [first_notifier],
            (
                f"Second corrector finished for {label} ({year.year_label}). "
                f"Final Excel is ready ({st_label} exam)."
                + _notification_whatsapp_contacts_lines((second_notifier, "Second corrector"))
            ),
        )
    if not staff:
        _notify_admins(
            f"Second corrector finalized {label} ({year.year_label}). "
            f"Final average file is now available."
            + _notification_whatsapp_contacts_lines(
                (_get_first_corrector(ms, year), "First corrector"),
                (_get_second_corrector(ms, year), "Second corrector"),
            )
        )

    disputed_numbers = []
    for ex in exams:
        fr = FinalResult.objects.filter(exam=ex).first()
        if fr and fr.second_round_complete and fr.difference >= 10:
            disputed_numbers.append(ex.exam_number)
    disputed_numbers.sort()
    recorrection_whatsapp = []
    if disputed_numbers:
        nums_txt = ", ".join(str(n) for n in disputed_numbers)
        wa_body = (
            f"Recorrection required ({label}, {year.year_label}) for exam number(s): {nums_txt}. "
            "You have to reenter marks because you have exams that present a difference >= 10."
        )
        for u in (_get_first_corrector(ms, year), _get_second_corrector(ms, year)):
            if not u:
                continue
            phone = _normalize_phone_for_whatsapp(getattr(u, "phone_number", ""))
            if not phone:
                continue
            auto_send = _send_whatsapp_message(phone, wa_body)
            recorrection_whatsapp.append(
                {
                    "username": u.username,
                    "wa_url": f"https://wa.me/{phone}?text={quote(wa_body)}",
                    "auto_sent": bool(auto_send.get("ok")),
                    "auto_error": auto_send.get("error"),
                }
            )
        _notify_users(
            [
                u
                for u in (_get_first_corrector(ms, year), _get_second_corrector(ms, year))
                if u
            ],
            (
                f"Recorrection needed: for {label} ({year.year_label}), "
                f"exam number(s) {nums_txt} have |difference| >= 10/100 between correctors."
                + _notification_whatsapp_contacts_lines(
                    (_get_first_corrector(ms, year), "First corrector"),
                    (_get_second_corrector(ms, year), "Second corrector"),
                )
            ),
        )

    return {
        "ok": True,
        "files": {
            "rubric_marks": _media_url_from_path(path1),
            "totals": _media_url_from_path(path2),
            "final": _media_url_from_path(path_final),
        },
        "final_printable_url": (
            _media_url_from_path(final_printable_path) if final_printable_path else None
        ),
        "recorrection_whatsapp": recorrection_whatsapp,
        "disputed_exam_numbers": disputed_numbers,
    }


@login_required
@require_POST
def api_finalize_second(request):
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role="second",
    ).first()
    if not msc and not staff:
        return JsonResponse(
            {"error": "Only the assigned second corrector can finalize this step."},
            status=403,
        )

    result = _finalize_second_round(request, ms, year, st)
    if not result.get("ok"):
        return JsonResponse({"error": result.get("error")}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "files": result["files"],
            "final_printable_url": result.get("final_printable_url"),
            "recorrection_whatsapp": result["recorrection_whatsapp"],
            "disputed_exam_numbers": result["disputed_exam_numbers"],
        }
    )


@login_required
@require_POST
def api_prepare_whatsapp_second(request):
    data = _json_body(request)
    st = _parse_session_type(data.get("session_type"))
    if not st:
        return JsonResponse(
            {"error": "Invalid or missing exam type (partial, first_final, second_final)."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=data.get("material_section_id"))
    year = get_object_or_404(AcademicYear, pk=data.get("academic_year_id"))
    staff = _is_panel_manager(request.user)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role="first",
    ).first()
    if not msc and not staff:
        return JsonResponse({"error": "Only first corrector can send this WhatsApp message."}, status=403)
    if not _first_round_done_for_session(ms, year, st):
        return JsonResponse(
            {"error": "Finalize first correction before preparing WhatsApp."},
            status=400,
        )
    payload = _build_whatsapp_payload(ms, year, st)
    if not payload.get("ok"):
        return JsonResponse({"error": payload.get("error")}, status=400)
    return JsonResponse({"ok": True, **payload})


@login_required
@require_POST
def api_notifications_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({"ok": True})


@login_required
def final_page(request, ms_id: int, year_id: int, session_type: str):
    st = _parse_session_type(session_type)
    if not st:
        return render(
            request,
            "core/error.html",
            {"message": "Invalid exam type. Use partial, first final, or second final."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    assignments = list(
        MaterialSectionCorrector.objects.filter(
            material_section=ms,
            academic_year=year,
            corrector=request.user,
        ).values_list("role", flat=True)
    )
    staff = _is_panel_manager(request.user)
    if not assignments and not staff:
        return render(
            request,
            "core/error.html",
            {"message": "You are not assigned to this material for this year."},
            status=403,
        )

    ensure_exams(ms, year, st)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=st
        ).order_by("exam_number")
    )
    fr_list = FinalResult.objects.filter(exam__in=exams).select_related("exam")
    fr_by_exam = {fr.exam_id: fr for fr in fr_list}

    rows = []
    for ex in exams:
        fr = fr_by_exam.get(ex.id)
        if not fr or not fr.second_round_complete:
            rows.append(
                {
                    "exam_number": ex.exam_number,
                    "pending": True,
                }
            )
        else:
            rows.append(
                {
                    "exam_number": ex.exam_number,
                    "pending": False,
                    "first_total": round(fr.first_total, 2),
                    "second_total": round(fr.second_total, 2),
                    "average": round(fr.average, 2),
                    "difference": round(fr.difference, 2),
                    "highlight": fr.difference >= 10,
                }
            )

    assigned_roles = (
        ["first", "second"]
        if staff
        else sorted(set(assignments))
    )
    simple_final_print_only = (
        not staff
        and len(assigned_roles) == 1
        and assigned_roles[0] == "second"
    )
    return render(
        request,
        "core/final.html",
        {
            "material_section": ms,
            "academic_year": year,
            "session_type": st,
            "session_label": _session_label_short(st),
            "rows": rows,
            "material_name": ms.material.material_name,
            "downloads_available": _session_second_round_complete(ms, year, st) or staff,
            "assigned_roles": assigned_roles,
            "staff_mode": staff,
            "simple_final_print_only": simple_final_print_only,
        },
    )


@login_required
def report_page(request, ms_id: int, year_id: int, session_type: str, role: str):
    st = _parse_session_type(session_type)
    if not st:
        return render(
            request,
            "core/error.html",
            {"message": "Invalid exam type. Use partial, first final, or second final."},
            status=400,
        )
    if role not in ("first", "second"):
        return render(
            request,
            "core/error.html",
            {"message": "Invalid role for report."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    msc = MaterialSectionCorrector.objects.filter(
        material_section=ms,
        academic_year=year,
        corrector=request.user,
        role=role,
    ).first()
    if not msc and not _is_panel_manager(request.user):
        return render(
            request,
            "core/error.html",
            {"message": "You are not assigned as this corrector for this material and year."},
            status=403,
        )

    return render(
        request,
        "core/report_final_marks.html",
        _corrector_marks_sheet_render_context(ms, year, st, role),
    )


@login_required
def printable_final_marks(request, ms_id: int, year_id: int, session_type: str, role: str):
    """Printable /100 sheet (same layout as Show totals /100) with export-preview access rules."""
    st, ms, year, exams, error_response = _validate_export_access(
        request, ms_id, year_id, session_type, role, "my_marks"
    )
    if error_response:
        return error_response
    return render(
        request,
        "core/report_final_marks.html",
        _corrector_marks_sheet_render_context(ms, year, st, role),
    )


def _build_averages_report_pages(ms: MaterialSection, year: AcademicYear, session_type: str):
    """Same two-column table as official marks sheet: No | figure /100 | letters; values = average /100."""
    ensure_exams(ms, year, session_type)
    exams = list(
        Exam.objects.filter(
            material_section=ms, academic_year=year, session_type=session_type
        ).order_by("exam_number")
    )
    fr_map = {fr.exam_id: fr for fr in FinalResult.objects.filter(exam__in=exams)}
    rows = []
    for ex in exams:
        fr = fr_map.get(ex.id)
        if fr and fr.second_round_complete:
            rows.append(
                {
                    "number": ex.exam_number,
                    "total": f"{fr.average:.2f}",
                    "words": "",
                    "highlight": fr.difference >= 10,
                }
            )
        elif fr:
            rows.append(
                {
                    "number": ex.exam_number,
                    "total": "—",
                    "words": "",
                    "highlight": False,
                }
            )
        else:
            rows.append(
                {
                    "number": ex.exam_number,
                    "total": "—",
                    "words": "",
                    "highlight": False,
                }
            )
    return _build_printable_pages_first36_then_rest(rows)


@login_required
def printable_final_averages(request, ms_id: int, year_id: int, session_type: str):
    st = _parse_session_type(session_type)
    if not st:
        return render(
            request,
            "core/error.html",
            {"message": "Invalid exam type. Use partial, first final, or second final."},
            status=400,
        )
    ms = get_object_or_404(MaterialSection, pk=ms_id)
    year = get_object_or_404(AcademicYear, pk=year_id)
    staff = _is_panel_manager(request.user)
    if not staff:
        assigned = MaterialSectionCorrector.objects.filter(
            material_section=ms,
            academic_year=year,
            corrector=request.user,
        ).exists()
        if not assigned:
            return render(
                request,
                "core/error.html",
                {"message": "You are not assigned to this material and year."},
                status=403,
            )
        if not _session_second_round_complete(ms, year, st):
            return render(
                request,
                "core/error.html",
                {
                    "message": "The averages printable is available after the second corrector finalizes."
                },
                status=400,
            )
    pages = _build_averages_report_pages(ms, year, st)
    first_c = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="first"
        )
        .select_related("corrector")
        .first()
    )
    second_c = (
        MaterialSectionCorrector.objects.filter(
            material_section=ms, academic_year=year, role="second"
        )
        .select_related("corrector")
        .first()
    )
    corrector_first = (
        first_c.corrector.get_full_name() or first_c.corrector.username
        if first_c and first_c.corrector
        else ""
    )
    corrector_second = (
        second_c.corrector.get_full_name() or second_c.corrector.username
        if second_c and second_c.corrector
        else ""
    )
    session_ar = (
        "جزئية"
        if st == "partial"
        else ("نهائية أولى" if st == "first_final" else "نهائية ثانية")
    )
    return render(
        request,
        "core/report_final_marks.html",
        {
            "material_section": ms,
            "academic_year": year,
            "session_ar": session_ar,
            "sheet_title_ar": _arabic_sheet_title(st),
            "pages": pages,
            "corrector_first": corrector_first,
            "corrector_second": corrector_second,
            "institution_logo_url": _institution_logo_url(),
        },
    )


@login_required
def admin_panel(request):
    if not _is_panel_manager(request.user):
        return render(
            request,
            "core/error.html",
            {"message": "Administration access only."},
            status=403,
        )

    UserModel = get_user_model()
    msg = None
    corrector_doctors = UserModel.objects.filter(
        panel_manager=False, is_superuser=False, is_active=True
    ).order_by("username")
    teachers = corrector_doctors
    panel_staff_users = UserModel.objects.filter(
        panel_manager=True, is_superuser=False, is_active=True
    ).order_by("username")

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "add_user":
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip().lower()
            phone_number = (request.POST.get("phone_number") or "").strip()
            password = request.POST.get("password") or ""
            grant_panel = request.POST.get("grant_staff") == "1"
            if not username or not email or not password:
                msg = "All fields are required."
            else:
                user, created = UserModel.objects.get_or_create(
                    username=username,
                    defaults={"email": email},
                )
                if not created:
                    msg = "Username already exists."
                else:
                    user.email = email
                    user.phone_number = phone_number
                    user.set_password(password)
                    user.panel_manager = grant_panel
                    user.is_staff = False
                    user.is_superuser = False
                    user.save()
                    msg = (
                        "Administrator account created."
                        if grant_panel
                        else "Teacher account created."
                    )

        elif action == "update_teacher_phone":
            raw_id = request.POST.get("user_id")
            phone_number = (request.POST.get("phone_number") or "").strip()
            if raw_id:
                try:
                    target = UserModel.objects.get(
                        pk=int(raw_id),
                        panel_manager=False,
                        is_superuser=False,
                    )
                    target.phone_number = phone_number
                    target.save(update_fields=["phone_number"])
                    msg = f"WhatsApp / phone number saved for {target.username}."
                except UserModel.DoesNotExist:
                    msg = "User not found."
                except Exception:
                    msg = "Could not update phone number."

        elif action == "delete_user":
            raw_id = request.POST.get("user_id")
            if raw_id:
                try:
                    target = UserModel.objects.get(pk=int(raw_id))
                    if target.is_superuser:
                        msg = "Superuser accounts cannot be removed here."
                    elif target.id == request.user.id:
                        msg = "You cannot delete your own account."
                    else:
                        target.delete()
                        msg = "User removed (grading data kept)."
                except Exception:
                    msg = "Could not remove user."

        elif action == "set_corrector":
            try:
                ms_id = int(request.POST.get("material_section_id") or 0)
                year_id = int(request.POST.get("academic_year_id") or 0)
            except (TypeError, ValueError):
                ms_id = year_id = 0
            role = (request.POST.get("role") or "").strip()
            doctor_raw = (request.POST.get("doctor_id") or "").strip()
            if not ms_id or not year_id or role not in ("first", "second"):
                msg = "Choose material, academic year, and role."
            else:
                corrector = None
                if doctor_raw:
                    try:
                        corrector = UserModel.objects.get(
                            pk=int(doctor_raw),
                            panel_manager=False,
                            is_superuser=False,
                            is_active=True,
                        )
                    except (UserModel.DoesNotExist, ValueError):
                        msg = "Invalid teacher."
                if not msg:
                    try:
                        ms = MaterialSection.objects.get(pk=ms_id)
                        year = AcademicYear.objects.get(pk=year_id)
                    except (MaterialSection.DoesNotExist, AcademicYear.DoesNotExist):
                        msg = "Invalid material or year."
                    else:
                        MaterialSectionCorrector.objects.update_or_create(
                            material_section=ms,
                            academic_year=year,
                            role=role,
                            defaults={"corrector": corrector},
                        )
                        msg = "Assignment saved."

        elif action == "clear_corrector":
            raw_id = request.POST.get("assignment_id")
            if raw_id:
                try:
                    assignment = MaterialSectionCorrector.objects.get(pk=int(raw_id))
                    assignment.corrector = None
                    assignment.save(update_fields=["corrector"])
                    msg = "Slot cleared."
                except Exception:
                    msg = "Could not clear assignment."

    printable_report_rows = []
    printable_tp_report_rows = []
    seen_ctx = set()
    seen_tp_ctx = set()
    for msc in (
        MaterialSectionCorrector.objects.select_related(
            "material_section",
            "material_section__material",
            "material_section__section",
            "academic_year",
        )
        .order_by("role")
        .all()
    ):
        key = (msc.material_section_id, msc.academic_year_id)
        ms = msc.material_section
        y = msc.academic_year
        is_tp = _is_tp_material(ms.material)
        if is_tp:
            if key in seen_tp_ctx:
                continue
            seen_tp_ctx.add(key)
            st = Exam.SESSION_PARTIAL
            has_rubric = Question.objects.filter(
                material=ms.material, session_type=st
            ).exists()
            marks_ready = _first_marks_saved_for_session(
                ms, y, st
            ) or _first_round_done_for_session(ms, y, st)
            printable_tp_report_rows.append(
                {
                    "ms_id": ms.id,
                    "year_id": y.id,
                    "session_type": st,
                    "label": f"{ms.material.material_name} — {ms.section.name}",
                    "year_label": y.year_label,
                    "print_marks": bool(has_rubric and marks_ready),
                }
            )
            continue
        if key in seen_ctx:
            continue
        seen_ctx.add(key)
        for st in ("partial", "first_final", "second_final"):
            has_rubric = Question.objects.filter(
                material=ms.material, session_type=st
            ).exists()
            first_ready = _first_marks_saved_for_session(
                ms, y, st
            ) or _first_round_done_for_session(ms, y, st)
            second_ready = _second_marks_saved_for_session(
                ms, y, st
            ) or _session_second_round_complete(ms, y, st)
            averages_ready = _session_second_round_complete(ms, y, st)
            printable_report_rows.append(
                {
                    "ms_id": ms.id,
                    "year_id": y.id,
                    "session_type": st,
                    "session_label": _session_label_short(st),
                    "label": f"{ms.material.material_name} — {ms.section.name}",
                    "year_label": y.year_label,
                    "print_first": bool(has_rubric and first_ready),
                    "print_second": bool(has_rubric and second_ready),
                    "print_averages": bool(has_rubric and averages_ready),
                }
            )
    printable_report_rows.sort(
        key=lambda r: (r["year_label"], r["label"], r["session_type"])
    )
    printable_tp_report_rows.sort(
        key=lambda r: (r["year_label"], r["label"])
    )

    material_sections = MaterialSection.objects.select_related(
        "material",
        "material__department",
        "section",
    ).order_by(
        "material__department__name",
        "material__material_name",
        "section__name",
    )
    academic_years = AcademicYear.objects.order_by("-year_label")
    corrector_assignments = MaterialSectionCorrector.objects.select_related(
        "material_section",
        "material_section__material",
        "material_section__section",
        "academic_year",
        "corrector",
    ).order_by(
        "academic_year__year_label",
        "material_section__material__material_name",
        "role",
    )

    return render(
        request,
        "core/admin_panel.html",
        {
            "msg": msg,
            "teachers": teachers,
            "panel_staff_users": panel_staff_users,
            "corrector_doctors": corrector_doctors,
            "grading_overview_rows": _build_admin_grading_overview_rows(),
            "printable_report_rows": printable_report_rows,
            "printable_tp_report_rows": printable_tp_report_rows,
            "material_sections": material_sections,
            "academic_years": academic_years,
            "corrector_assignments": corrector_assignments,
        },
    )


# --- CONFRONTATION REPORT ---
@login_required
def confrontation_report(request, material_id):
    material_section = get_object_or_404(MaterialSection, id=material_id)
    results = FinalResult.objects.filter(
        exam__material_section=material_section
    ).order_by('exam__exam_number')
    
    return render(request, 'report.html', {
        'final_results': results,
        'material_section': material_section
    })
