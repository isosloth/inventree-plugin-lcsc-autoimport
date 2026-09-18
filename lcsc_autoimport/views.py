from django.urls import path

from .views import BulkImportAPIView

urlpatterns = [
    path("bulk/", BulkImportAPIView.as_view(), name="lcsc-autoimport-bulk"),
]
