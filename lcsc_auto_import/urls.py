from django.urls import path

from .views import BulkImportAPIView, CSVImportAPIView

urlpatterns = [
    path("bulk/", BulkImportAPIView.as_view(), name="lcsc-autoimport-bulk"),
    path("csv/", CSVImportAPIView.as_view(), name="lcsc-autoimport-csv"),
]
