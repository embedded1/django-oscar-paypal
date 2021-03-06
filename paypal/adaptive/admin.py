from django.contrib import admin
from . import models


class TxnAdmin(admin.ModelAdmin):
    list_display = ['correlation_id', 'action', 'ack', 'pay_key', 'payment_exec_status', 'date_created']
    list_filter = ['payment_exec_status', 'action']
    readonly_fields = [
        'is_sandbox',
        'pay_key',
        'action',
        'currency',
        'ack',
        'correlation_id',
        'error_code',
        'error_message',
        'request',
        'response',
        'raw_request',
        'raw_response',
        'response_time',
        'payment_exec_status',
        'date_created',
    ]


admin.site.register(models.AdaptiveTransaction, TxnAdmin)
