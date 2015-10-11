"""
Adaptive payments:

https://www.x.com/developers/paypal/documentation-tools/adaptive-payments/gs_AdaptivePayments
"""
from collections import OrderedDict
from django.utils.translation import ugettext as _
from decimal import Decimal as D
from django.conf import settings
from paypal import gateway, models
from paypal import exceptions
import logging



logger = logging.getLogger('paypal.adaptive')

# Enum class for who pays the fees
# See pg 80 of the guide
Fees = type('Fees', (), {
    'SENDER': 'SENDER',
    'PRIMARY_RECEIVER': 'PRIMARYRECEIVER',
    'EACH_RECEIVER': 'EACHRECEIVER',
    'SECONDARY_ONLY': 'SECONDARYONLY',
})

#PayPal APIs
Adaptive_Payments = 'AdaptivePayments'
Adaptive_Accounts = 'AdaptiveAccounts'

# PayPal methods
Pay = 'Pay'
Payment_Details = 'PaymentDetails'
Set_Payment_Options = 'SetPaymentOptions'
Execute_Payment = 'ExecutePayment'
Get_Verified_Status = 'GetVerifiedStatus'
REFUND = 'Refund'

#Pay actions
PAY, CREATE, PAY_PRIMARY = 'PAY', 'CREATE', 'PAY_PRIMARY'

def _format_currency(amt):
    return amt.quantize(D('0.01'))

def payment_details(txn_id):
    """
    Fetch the payment details for a given transaction
    """
    params = [("transactionId", txn_id)]
    return _request(Payment_Details, params)

def refund_payment(pay_key):
    """
    refund full amount
    """
    params = [("payKey", pay_key)]
    return _request(REFUND, params)

def set_payment_option(basket, pay_key, shipping_address=None):
    """
    Submit shipping address and order items to PayPal
    """

    params = [("payKey", pay_key)]

    if shipping_address:
        #add shipping address
        params.append(('senderOptions.shippingAddress.addresseeName', shipping_address.name))
        params.append(('senderOptions.shippingAddress.street1', shipping_address.line1))
        params.append(('senderOptions.shippingAddress.city', shipping_address.line4))
        params.append(('senderOptions.shippingAddress.zip', shipping_address.postcode))
        params.append(('senderOptions.shippingAddress.country', shipping_address.country.iso_3166_1_a2))
        if shipping_address.line2:
            params.append(('senderOptions.shippingAddress.street2', shipping_address.line2))
        if shipping_address.state:
            params.append(('senderOptions.shippingAddress.state', shipping_address.state))
        #add phone number
        if shipping_address.phone_number:
            params.append(('senderOptions.shippingAddress.phone.countryCode', str(shipping_address.phone_number.country_code)))
            params.append(('senderOptions.shippingAddress.phone.phoneNumber',
                           ''.join(i for i in shipping_address.phone_number.as_national if i.isdigit())))
            params.append(('senderOptions.shippingAddress.phone.type', 'MOBILE'))

    if basket:
        index = 0
        for index, line in enumerate(basket.all_lines()):
            product = line.product
            params.append(('receiverOptions[0].invoiceData.item[%d].name' % index, product.get_title()))
            params.append(('receiverOptions[0].invoiceData.item[%d].identifier' % index, product.upc if
                                                             product.upc else ''))
            # Note, we don't include discounts here - they are handled as separate
            # lines - see below
            params.append(('receiverOptions[0].invoiceData.item[%d].price' % index, _format_currency(
                line.unit_price_incl_tax)))
            params.append(('receiverOptions[0].invoiceData.item[%d].itemCount' % index, line.quantity))

        # Iterate over the 3 types of discount that can occur
        for discount in basket.offer_discounts:
            index += 1
            name = _("Special Offer: %s") % discount['name']
            params.append(('receiverOptions[0].invoiceData.item[%d].name' % index, name))
            params.append(('receiverOptions[0].invoiceData.item[%d].price' % index, _format_currency(
                -discount['discount'])))
            params.append(('receiverOptions[0].invoiceData.item[%d].itemCount' % index, 1))
        for discount in basket.voucher_discounts:
            index += 1
            name = "%s (%s)" % (discount['voucher'].name,
                                discount['voucher'].code)
            params.append(('receiverOptions[0].invoiceData.item[%d].name' % index, name))
            params.append(('receiverOptions[0].invoiceData.item[%d].price' % index, _format_currency(
                -discount['discount'])))
            params.append(('receiverOptions[0].invoiceData.item[%d].itemCount' % index, 1))
        for discount in basket.shipping_discounts:
            index += 1
            name = _("Shipping Offer: %s") % discount['name']
            params.append(('receiverOptions[0].invoiceData.item[%d].name' % index, name))
            params.append(('receiverOptions[0].invoiceData.item[%d].price' % index, _format_currency(
                -discount['discount'])))
            params.append(('receiverOptions[0].invoiceData.item[%d].itemCount' % index, 1))

    return _request(Set_Payment_Options, params)



def pay(receivers, currency, return_url, cancel_url,
        action=PAY_PRIMARY, sender_email=None, tracking_id=None,
        fees_payer='EACHRECEIVER', memo=None, ipn_url=None):
    """
    Submit a 'Pay' transaction to PayPal
    """
    assert 0 < len(receivers) <= 6, "PayPal only supports up to 6 receivers"

    # Set core params
    params = [
        ("actionType", action),
        ("currencyCode", currency),
        ("returnUrl", return_url),
        ("cancelUrl", cancel_url),
    ]

    # Chained payment?
    is_chained = any([r['is_primary'] for r in receivers])

    total = D('0.00')
    for index, receiver in enumerate(receivers):
        params.append(('receiverList.receiver(%d).amount' % index,
                       str(receiver['amount'])))
        params.append(('receiverList.receiver(%d).email' % index,
                       receiver['email']))
        params.append(('receiverList.receiver(%d).primary' % index,
                       'true' if receiver['is_primary'] else 'false'))
        # The primary receiver should have the total amount as their amount
        if is_chained:
            if receiver['is_primary']:
                total = receiver['amount']
        else:
            total += receiver['amount']

    # Add optional params
    if fees_payer:
        params.append(('feesPayer', fees_payer))

    if tracking_id:
        params.append(('trackingId', tracking_id))

    if memo:
        params.append(('memo', memo))

    if sender_email:
        params.append(('senderEmail', sender_email))

    if ipn_url:
        params.append(('ipnNotificationUrl', ipn_url))

    # We pass the total so it can be added to the txn model for better audit
    return _request(Pay, params, txn_fields={'amount': total})

def execute_payment(pay_key):
    """
    Finish the adaptive payments transaction
    """
    params = [("payKey", pay_key)]
    return _request(Execute_Payment, params)


def get_account_info(first_name, last_name, email):
    """
    Fetch payer status and personal details
    """
    params = [
        ("emailAddress", email),
        ("firstName", first_name),
        ("lastName", last_name),
        ("matchCriteria", "NAME")
    ]

    txn = _request(Get_Verified_Status, params, api=Adaptive_Accounts)
    return (
        txn.value("accountStatus"),
        txn.value("userInfo.emailAddress"),
        txn.value("userInfo.name.firstName"),
        txn.value("userInfo.name.lastName")
    )


def _request(action, params, api=Adaptive_Payments, headers=None, txn_fields=None):
    """
    Make a request to PayPal
    """
    if headers is None:
        headers = {}
    if txn_fields is None:
        txn_fields = {}
    request_headers = {
        'X-PAYPAL-SECURITY-USERID': settings.PAYPAL_API_USERNAME,
        'X-PAYPAL-SECURITY-PASSWORD': settings.PAYPAL_API_PASSWORD,
        'X-PAYPAL-SECURITY-SIGNATURE': settings.PAYPAL_API_SIGNATURE,
        'X-PAYPAL-APPLICATION-ID': settings.PAYPAL_API_APPLICATION_ID,
        # Use NVP so we can re-used code from Express and Payflow Pro
        'X-PAYPAL-REQUEST-DATA-FORMAT': 'NV',
        'X-PAYPAL-RESPONSE-DATA-FORMAT': 'NV',
    }
    request_headers.update(headers)

    common_params = [
        ("requestEnvelope.errorLanguage", "en_US"),
        ("requestEnvelope.detailLevel", "ReturnAll"),
    ]
    params.extend(common_params)

    if getattr(settings, 'PAYPAL_SANDBOX_MODE', False):
        url = 'https://svcs.sandbox.paypal.com/%s/%s'
        is_sandbox = True
    else:
        url = 'https://svcs.paypal.com/%s/%s'
        is_sandbox = False

    url = url % (api, action)

    # We use an OrderedDict as the key-value pairs have to be in the correct
    # order(!).  Otherwise, PayPal returns error 'Invalid request: {0}'
    # with errorId 580001.  All very silly.
    param_dict = OrderedDict(params)
    pairs = gateway.post(url, param_dict, request_headers)

    # Record transaction data - we save this model whether the txn
    # was successful or not
    txn = models.AdaptiveTransaction(
        action=action,
        is_sandbox=is_sandbox,
        raw_request=pairs['_raw_request'],
        raw_response=pairs['_raw_response'],
        response_time=pairs['_response_time'],
        currency=param_dict.get('currencyCode', None),
        ack=pairs.get('responseEnvelope.ack', None),
        pay_key=pairs.get('payKey', None),
        correlation_id=pairs.get('responseEnvelope.correlationId', None),
        payment_exec_status=pairs.get('paymentExecStatus', None),
        error_code=pairs.get('error(0).errorId', None),
        error_message=pairs.get('error(0).message', None),
        **txn_fields)

    txn.save()

    if not txn.is_successful or \
        action == Pay and not txn.is_payment_successful:
        msg = "Error %s - %s" % (txn.error_code, txn.error_message)
        logger.error(msg)
        raise exceptions.PayPalError(msg)

    return txn


