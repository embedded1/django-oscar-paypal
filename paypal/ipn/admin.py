from django.contrib import admin
from . import models


class IPNAdmin(admin.ModelAdmin):
    list_display = ['transaction_id', 'payment_status', 'date_created']
    list_filter = ['payment_status']
    readonly_fields = [
        'is_sandbox',
        'transaction_id',
        'raw_request',
        'raw_response',
        'response_time',
        'payment_status',
        'date_created',
    ]


admin.site.register(models.IPNResponse, IPNAdmin)

