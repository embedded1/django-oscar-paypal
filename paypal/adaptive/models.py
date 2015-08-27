from django.db import models

from paypal import base


class AdaptiveTransaction(base.ResponseModel):

    # Request info
    is_sandbox = models.BooleanField(default=True)
    action = models.CharField(max_length=32)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=32, null=True, blank=True)

    # Response info
    SUCCESS, SUCCESS_WITH_WARNING, FAILURE = 'Success', 'SuccessWithWarning', 'Failure'
    ack = models.CharField(max_length=32)

    # This is PayPal's ID for the transaction.  It should be unique but we
    # don't enforce it as we don't want PayPal errors causing errors in our
    # system.
    correlation_id = models.CharField(max_length=32, db_index=True)

    # Only set if the transaction is successful.
    pay_key = models.CharField(max_length=64, null=True, blank=True,
                               db_index=True)

    #We keep the payment status as we use delayed payments we need to know
    #what transactions have been completed and what transactions we need to finish
    #the secondary receiver payment
    CREATED, COMPLETED, INCOMPLETE, ERROR, REVERSALERROR = 'CREATED', 'COMPLETED', 'INCOMPLETE',\
                                                           'ERROR', 'REVERSALERROR'
    payment_exec_status = models.CharField(max_length=32, null=True, blank=True,
                                           db_index=True)
    error_code = models.CharField(max_length=32, null=True, blank=True)
    error_message = models.CharField(max_length=256, null=True, blank=True)

    class Meta:
        ordering = ('-date_created',)
        app_label = 'paypal'

    def __unicode__(self):
        return self.correlation_id

    @property
    def is_successful(self):
        return self.ack in (self.SUCCESS, self.SUCCESS_WITH_WARNING)

    @property
    def is_payment_successful(self):
        """
        For Pay transactions we must make sure the transaction has finished
        successfully, checking the ack attribute is not enough
        """
        if not self.action == 'Pay':
            return True
        return self.payment_exec_status in (self.CREATED, self.COMPLETED)

    @property
    def redirect_url(self):
        if self.is_sandbox:
            url = 'https://www.sandbox.paypal.com/cgi-bin/webscr?cmd=_ap-payment&paykey=%s'
        else:
            url = 'https://www.paypal.com/cgi-bin/webscr?cmd=_ap-payment&paykey=%s'
        return url % self.pay_key
