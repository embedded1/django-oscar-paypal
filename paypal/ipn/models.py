from django.db import models
from paypal import base
from django.utils.translation import ugettext_lazy as _

class PaymentMessage(base.IPNMessageModel):
    pay_key = models.CharField(
        max_length=64, null=True, blank=True,
        db_index=True)
    payment_status = models.CharField(
        max_length=32, db_index=True)
    fraud_management_filters = models.CharField(
        max_length=512, blank=True, null=True)

    class Meta:
        ordering = ('-date_created',)
        app_label = 'paypal'
        verbose_name = _('IPN payment message')
        verbose_name_plural = _('IPN payment messages')

    def __unicode__(self):
        return self.transaction_id

