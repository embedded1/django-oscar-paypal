from django.db import models
from paypal import base

class IPNResponse(base.ResponseModel):
    is_sandbox = models.BooleanField(
        default=True)
    transaction_id = models.CharField(
        max_length=32, db_index=True)
    payment_status = models.CharField(
        max_length=32, db_index=True)
    fraud_management_filters = models.CharField(
        max_length=512, blank=True, null=True)

    class Meta:
        ordering = ('-date_created',)
        app_label = 'paypal'

    def __unicode__(self):
        return self.transaction_id

