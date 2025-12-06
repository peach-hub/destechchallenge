from django.urls import path

from .views import (
    AssistanceRequestCreateView,
    AssistanceRequestCompleteView,
    AssistanceRequestCancelView,
)

urlpatterns = [
    path("requests/", AssistanceRequestCreateView.as_view(), name="assistance-request-create"),
    path("requests/<int:request_id>/complete/", AssistanceRequestCompleteView.as_view(), name="assistance-request-complete"),
    path("requests/<int:request_id>/cancel/", AssistanceRequestCancelView.as_view(), name="assistance-request-cancel"),
]
