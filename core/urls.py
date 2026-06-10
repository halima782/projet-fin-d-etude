from django.urls import path

from . import views

urlpatterns = [
    path("", views.corrector_dashboard, name="dashboard"),
    path("login/", views.login_page, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("admin-panel/", views.admin_panel, name="admin_panel"),
    path(
        "grading/<int:ms_id>/<int:year_id>/<str:session_type>/<str:role>/",
        views.grading_page,
        name="grading",
    ),
    path(
        "final/<int:ms_id>/<int:year_id>/<str:session_type>/",
        views.final_page,
        name="final_results",
    ),
    path(
        "report/<int:ms_id>/<int:year_id>/<str:session_type>/<str:role>/",
        views.report_page,
        name="report_page",
    ),
    path(
        "saved-report/<int:report_id>/",
        views.saved_marks_report_view,
        name="saved_marks_report",
    ),
    path(
        "print/final-marks/<int:ms_id>/<int:year_id>/<str:session_type>/<str:role>/",
        views.printable_final_marks,
        name="printable_final_marks",
    ),
    path(
        "print/final-averages/<int:ms_id>/<int:year_id>/<str:session_type>/",
        views.printable_final_averages,
        name="printable_final_averages",
    ),
    path(
        "export/<int:ms_id>/<int:year_id>/<str:session_type>/<str:role>/<str:kind>/",
        views.download_grading_export,
        name="download_grading_export",
    ),
    path(
        "export-preview/<int:ms_id>/<int:year_id>/<str:session_type>/<str:role>/<str:kind>/",
        views.preview_grading_export,
        name="preview_grading_export",
    ),
    path("api/materials/", views.api_materials, name="api_materials"),
    path(
        "api/session/<int:ms_id>/<int:year_id>/<str:session_type>/",
        views.api_session,
        name="api_session",
    ),
    path("api/save-rubric/", views.api_save_rubric, name="api_save_rubric"),
    path("api/save-marks/", views.api_save_marks, name="api_save_marks"),
    path("api/finalize-first/", views.api_finalize_first, name="api_finalize_first"),
    path(
        "api/first-corrector-send/",
        views.api_first_corrector_send,
        name="api_first_corrector_send",
    ),
    path("api/finalize-second/", views.api_finalize_second, name="api_finalize_second"),
    path(
        "api/prepare-whatsapp-second/",
        views.api_prepare_whatsapp_second,
        name="api_prepare_whatsapp_second",
    ),
    path(
        "api/notifications/read/",
        views.api_notifications_read,
        name="api_notifications_read",
    ),
]
