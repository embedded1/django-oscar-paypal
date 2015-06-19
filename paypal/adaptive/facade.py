from django.conf import settings
from django.contrib.sites.models import Site
from django.core.urlresolvers import reverse
from paypal.adaptive.gateway import (
    pay, payment_details, #set_payment_option,
    execute_payment, get_verified_status
)


def get_paypal_url_and_pay_key(receivers, basket, user, host=None, scheme=None, paypal_params=None):
    """
    Return the URL for a PayPal Adaptive Payment transaction.

    This involves registering the txn with PayPal to get a one-time
    URL.  If a shipping method and shipping address are passed, then these are
    given to PayPal directly - this is used within when using PayPal as a
    payment method.
    """
    currency = getattr(settings, 'PAYPAL_CURRENCY', 'GBP')
    if host is None:
        host = Site.objects.get_current().domain
    if scheme is None:
        use_https = getattr(settings, 'PAYPAL_CALLBACK_HTTPS', True)
        scheme = 'https' if use_https else 'http'
    return_url = '%s://%s%s' % (
        scheme, host, reverse('paypal-success-response', kwargs={
            'basket_id': basket.id}))
    cancel_url = '%s://%s%s' % (
        scheme, host, reverse('paypal-cancel-response', kwargs={
            'basket_id': basket.id}))

    #first create the Pay transaction
    txn = pay(receivers=receivers,
              currency=currency,
              return_url=return_url,
              cancel_url=cancel_url,
              sender_email=user.email)


    #now redirect the customer to PayPal to complete the payment
    return txn.redirect_url, txn.pay_key


def fetch_transaction_details(pay_key):
    """
    Fetch the completed details about the PayPal transaction.
    """
    return payment_details(pay_key)

def fetch_account_status(first_name, last_name, email):
    return get_verified_status(first_name, last_name, email)

def confirm_transaction(pay_key):
    return execute_payment(pay_key)