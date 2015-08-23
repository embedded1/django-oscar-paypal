from oscar.core.loading import get_class
from decimal import ROUND_FLOOR, Decimal as D
from django.http import HttpResponseRedirect, HttpResponseNotAllowed
from django.db.models import get_model
from django.views import generic
from django.shortcuts import get_object_or_404
from django.conf import settings
from paypal.exceptions import PayPalError
from paypal.adaptive.exceptions import (
    EmptyBasketException, MissingShippingAddressException,
    MissingShippingMethodException, InvalidBasket, PayPalFailedValidationException)
from paypal.adaptive.facade import (
    get_pay_request_attrs, fetch_account_info )
from paypal.express.facade import fetch_address_details
from django.contrib import messages
from django.utils import six
from django.core.urlresolvers import reverse
from django.utils.translation import ugettext as _
import logging

TWO_PLACES = D('0.01')

# Load views dynamically
PaymentDetailsView = get_class('checkout.views', 'PaymentDetailsView')
CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
Repository = get_class('shipping.repository', 'Repository')
Applicator = get_class('offer.utils', 'Applicator')
Selector = get_class('partner.strategy', 'Selector')
Source = get_model('payment', 'Source')
Order = get_model('order', 'Order')
SourceType = get_model('payment', 'SourceType')
Basket = get_model('basket', 'Basket')
logger = logging.getLogger('paypal.adaptive')


class RedirectView(CheckoutSessionMixin, generic.RedirectView):
    """
    Initiate the transaction with Paypal and redirect the user
    to PayPal's adaptive payments to perform the transaction.
    """
    permanent = False

    # Setting to distinguish if the site has already collected a shipping
    # address.  This is False when redirecting to PayPal straight from the
    # basket page but True when redirecting from checkout.
    as_payment_method = False

    def dispatch(self, request, *args, **kwargs):
        self.basket = request.basket
        self.package = request.basket.get_package()
        if self.package is None:
            logger.error("Paypal Adaptive Payments: couldn't get package"
                         " from basket for partner share calculations")
            messages.error(
                request, _("An error occurred communicating with PayPal"))
            return HttpResponseRedirect(reverse('customer:pending-packages'))
        return super(RedirectView, self).dispatch(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        try:
            url = self._get_redirect_url(**kwargs)
        except PayPalError:
            messages.error(
                self.request, _("An error occurred communicating with PayPal"))
            if self.as_payment_method:
                url = reverse('checkout:payment-details')
            else:
                url = reverse('customer:pending-packages')
            return url
        except InvalidBasket as e:
            messages.warning(self.request, six.text_type(e))
            return reverse('customer:pending-packages')
        except EmptyBasketException:
            messages.error(self.request, _("Your basket is empty"))
            return reverse('customer:pending-packages')
        except MissingShippingAddressException:
            messages.error(
                self.request, _("A shipping address must be specified"))
            return reverse('checkout:shipping-address')
        except MissingShippingMethodException:
            messages.error(
                self.request, _("A shipping method must be specified"))
            return reverse('checkout:shipping-method')
        except PayPalFailedValidationException:
            return reverse('customer:pending-packages')
        else:
            # Transaction successfully registered with PayPal.  Now freeze the
            # basket so it can't be edited while the customer is on the PayPal
            # site.
            self.basket.freeze()

            logger.info("Basket #%s - redirecting to %s", self.basket.id, url)

            return url

    def store_pay_transaction_id(self, transaction_id):
        """
        We save Pay request correlation id to identify the Pay transaction
        """
        self.checkout_session.store_pay_transaction_id(transaction_id)

    def get_selected_shipping_method(self):
        key = self.package.upc
        #make special key for return to store checkout where we need to
        #show only domestic methods
        if self.checkout_session.is_return_to_store_enabled():
            key += "_return-to-store"
        repo = self.checkout_session.get_shipping_repository(key)

        #we should never get here - but if we do show error message and redirect to
        #pending packages page
        if not repo:
            logger.error("Paypal Adaptive Payments: We could not fetch shipping repository from cache")
            raise PayPalError()

        #return the selected shipping method
        code = self.checkout_session.shipping_method_code(self.basket)
        return repo.get_shipping_method_by_code(code)

    def calc_partner_share(self, partner_order_payment_settings):
        """
        Partner's share is calculated as follows:
        1 - Payment for the shipping is made through partner's account so we need to pay him back
            (based on settings attribute)
        2 - % of total revenue
        3 - We refer to shipping discounts as vouchers and to services discounts as offers
        4 - We pay $0.3 for partner in case he doesn't pay for the postage, otherwise we pay
            him amount equals to bank_fee - 0.3
        """
        easypost_charge = shipping_margin = insurance_charge_incl_revenue = \
        partner_share = shipping_discounts = shipping_charge_incl_revenue = bank_fee = D('0.0')
        order_total_no_discounts =  self.basket.total_excl_tax_excl_discounts
        services_discounts = sum(offer['discount'] for offer in self.basket.offer_discounts) or D('0.0')

        #selected shipping method is not available for prepaid return labels
        if not self.checkout_session.is_return_to_store_prepaid_enabled():
            selected_method = self.get_selected_shipping_method()
            #we couldn't find selected shipping method in cache, need to cancel
            #the transaction and show error message
            if selected_method is None:
                logger.error("Paypal Adaptive Payments: couldn't get selected shipping method from cache")
                raise PayPalError()

            easypost_charge = D('0.05')
            shipping_discounts = sum(voucher['discount'] for voucher in self.basket.voucher_discounts) or D('0.0')
            shipping_margin = selected_method.shipping_revenue

            bank_fee_line = self.basket.get_item_at_position(settings.BANK_FEE_POSITION)
            if bank_fee_line is None:
                logger.error("Paypal adaptive payments: couldn't get back fee line from basket")
                raise PayPalError()

            bank_fee = bank_fee_line.price_incl_tax

            #check if shipping insurance is needed
            if self.basket.contains_line_at_position(settings.INSURANCE_FEE_POSITION):
                insurance_charge_incl_revenue = selected_method.ins_charge_incl_revenue

            shipping_charge_incl_revenue = selected_method.ship_charge_incl_revenue
            carrier = selected_method.carrier.upper()
            #if partner pays for postage we need to transfer him the postage costs
            #and the bank fee we received for the postage, which is the bank_fee - 0.3
            #otherwise, we only transfer him 0.3
            if partner_order_payment_settings.postage_paid_by_partner(carrier):
                partner_bank_fee = (bank_fee - D('0.3')) if bank_fee > D('0.3') else D('0.0')
                partner_share += selected_method.ship_charge_excl_revenue + partner_bank_fee
            else:
                partner_share += D('0.3')

        #Calculate shipping margin, we decrease easypost charge and any shipping discounts
        shipping_revenue = shipping_margin - easypost_charge - shipping_discounts
        shipping_revenue = D('0.0') if shipping_revenue < D('0.0') else shipping_revenue
        #Partner doesn't get any revenue from shipping insurance
        no_revenues = insurance_charge_incl_revenue
        #Services revenue is what left in basket after we decrease the shipping part,
        #the lines that the partner doesn't get any revenue from, the bank fee that we already
        #calculated partner's share above and finally the services discounts
        services_revenue = order_total_no_discounts - shipping_charge_incl_revenue - \
                           no_revenues - services_discounts - bank_fee
        services_revenue = D('0.0') if services_revenue < D('0.0') else services_revenue
        #Calculate partner's share that consists of 2 parts: shipping revenue and services revenue
        partner_share += ( (shipping_revenue * partner_order_payment_settings.shipping_margin) +
                           (services_revenue * partner_order_payment_settings.services_margin) ) / D('100.0')
        return partner_share.quantize(TWO_PLACES, rounding=ROUND_FLOOR)

    def store_partner_share(self, share):
        """
        we save partner's share in session so we could audit it in db
        once payment completes
        """
        self.checkout_session.store_partner_share(share)

    def get_receivers(self):
        """
        This function returns the payment receivers, we support 2 options:
        1 - Order payment is split between USendHome and the partner, in that case
            we calculate partner's share
        2 - Order payment isn't split and transferred as a whole to USendHome
        To determine in what option are we, we check if PartnerOrderPaymentSettings object
        is available for package's partner, if it exists, we follow it and divide the payment
        between UsendHome and the partner, otherwise, we take it all.
        """
        receivers = [
            {
                'email': settings.PAYPAL_PRIMARY_RECEIVER_EMAIL,
                'is_primary': True,
                'amount': self.basket.total_incl_tax
            }
        ]

        #Find out if we need to transfer funds to partner
        partner = self.package.stockrecords.all().prefetch_related(
            'partner', 'partner__payments_settings')[0].partner
        partner_order_payment_settings = partner.active_payment_settings

        if partner_order_payment_settings:
            partner_share = self.calc_partner_share(partner_order_payment_settings)
            #store partner's share in sessions as we
            #would like to store it in DB for audit
            self.store_partner_share(partner_share)
            receivers.append({
                'email': partner_order_payment_settings.billing_email,
                'is_primary': False,
                'amount': partner_share
            })

        return receivers

    def align_receivers(self, params):
        #The default Pay actionType is PAY_PRIMARY for chained payments
        #we need to set it to Pay in case no secondary receiver exists
        if len(params['receivers']) == 1:
            params['action'] = 'PAY'
            #Only 1 receiver, need to change is_primary to False
            params['receivers'][0]['is_primary'] = False
        else:
            params['action'] = 'PAY_PRIMARY'

    def _get_redirect_url(self, **kwargs):
        if self.basket.is_empty:
            raise EmptyBasketException()

        user = self.request.user
        customer_shipping_address = self.get_shipping_address(self.basket)
        is_return_to_merchant = self.checkout_session.is_return_to_store_enabled()

        #check that shipping address exists
        if not customer_shipping_address and not is_return_to_merchant:
            # we could not get shipping address - redirect to basket page with warning message
            logger.warning("customer's shipping address not found while verifying PayPal account")
            self.unfreeze_basket(kwargs['basket_id'])
            raise MissingShippingMethodException()

        #Run some validations on the user
        if not self.validate_txn(sender_email=user.email,
                                 sender_first_name=user.first_name,
                                 sender_last_name=user.last_name,
                                 sender_shipping_address=customer_shipping_address,
                                 return_to_merchant=is_return_to_merchant,
                                 skip_account_status_test=user.get_profile().skip_pp_verified_account_test,):
            raise PayPalFailedValidationException()

        params = {
            'basket': self.basket,
            'sender_email': self.request.user.email
        }

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        params['paypal_params'] = self._get_paypal_params()

        params['receivers'] = self.get_receivers()
        self.align_receivers(params)

        redirect_url, pay_correlation_id = get_pay_request_attrs(**params)
        self.store_pay_transaction_id(pay_correlation_id)
        return redirect_url


    def _get_paypal_params(self):
        """
        Return any additional PayPal parameters
        """
        return {}

    def validate_txn(self, sender_email, sender_first_name, sender_last_name, return_to_merchant,
                     skip_account_status_test, sender_shipping_address):

        #make sure user has verified Paypal account and account data is valid
        if not self.validate_paypal_account(
                sender_email, sender_first_name,
                sender_last_name, skip_account_status_test):
            return False

        #check shipping address only for non merchant addresses
        #this check is not needed for US addresses where the package is returned to store
        if not return_to_merchant:
            if not self.validate_shipping_address(sender_email, sender_shipping_address):
                return False

        #all went fine continue with payment
        return True

    def get_account_status(self, first_name, last_name, email):
        return fetch_account_info(first_name, last_name, email)

    def validate_paypal_account(self, sender_email, sender_first_name,
                                sender_last_name, skip_account_status_test):
        #Get account info from PP
        try:
            (pp_account_status,
             pp_account_email,
             pp_account_first_name,
             pp_account_last_name) = self.get_account_status(first_name=sender_first_name,
                                                             last_name=sender_last_name,
                                                             email=sender_email)
        except PayPalError:
            #we couldn't get account status, this is probably because the credentials
            #don't match the data on file at PayPal
            logger.error("Cannot determine PayPal account status: %s %s" % (sender_first_name, sender_last_name))
            # unverified payer - redirect to pending packages page with error message
            messages.error(self.request, _("Please make sure your USendHome account name and email address"
                                           " match the ones on file at PayPal."))
            return False

        #check payer status (In some countries it is hard to get PayPal account verified
        #therefore we added an exception where we can skip this test for specific users)
        if not skip_account_status_test:
            if pp_account_status.lower() != 'verified':
                logger.error("unverified payer found: %s %s" % (sender_first_name, sender_last_name))
                # unverified payer - redirect to pending packages page with error message
                messages.error(self.request, _("Your PayPal account isn't verified, please verify your account before"
                                               " proceeding to checkout."))
                return False


        #make sure the paypal email address is identical to the email address the customer
        #uses on site
        if sender_email != pp_account_email:
            logger.error("paypal email address %s does not match on site email address: %s"
                         % (pp_account_email, sender_email))
            messages.error(self.request, _("PayPal email address does not match the email address on USendHome.com."
                                           " Please edit your settings and try again."))
            return False

        #check that the paypal account name is same as the one on site
        #if pp_account_first_name != sender_first_name or \
        #   pp_account_last_name != sender_last_name:
        #    logger.error("PayPal account name does not match USendHome account name: %s %s, paypal name: %s %s"
        #                 % (sender_first_name, sender_last_name, pp_account_first_name, pp_account_last_name))
        #    messages.error(self.request, _("PayPal account name doesn't match the account name at USendHome.com<br/>"
        #                                   "Please edit your settings and try again."), extra_tags='safe block')
        #    return False

        return True

    def validate_shipping_address(self, email, shipping_address):
        try:
            conf_code, street_match, zip_match, country_code = fetch_address_details(email, shipping_address)
        except PayPalError:
            logger.critical("PayPal address_verify api call failed")
            messages.error(self.request, _("Either the postal code or the street address is invalid.<br/>"
                                           "Make sure they both match the format you have on file at PayPal"),
            extra_tags='safe block')
            return False

        #invalid paypal email address
        if street_match == 'none':
            logger.error("PayPal: invalid email address")
            # we should not get here - redirect to basket page with warning message
            messages.error(self.request, _("Email address was not found on file at PayPal."))
            return False

        #check street match
        if street_match.lower() != 'matched':
            logger.error("PayPal: Unmatched street found")
            # unmatched street - redirect to basket page with warning message
            messages.error(self.request, _("The street address doesn't match any street address on file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                           extra_tags='safe block')
            return False

        #check postal code match
        if zip_match.lower() != 'matched':
            logger.error("PayPal: Unmatched postal code found")
            # unmatched zip code - redirect to basket page with warning message
            messages.error(self.request, _("The postal code doesn't match any postal code on file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                           extra_tags='safe block')
            return False

        usendhome_country_code = shipping_address.country.iso_3166_1_a2
        #check country match
        if country_code != usendhome_country_code:
            logger.error("PayPal: Unmatched shipping country, paypal country code:%s, "
                         "USendHome country code: %s" % (country_code, usendhome_country_code))
            # unmatched country - redirect to basket page with warning message
            messages.error(self.request, _("The destination country doesn't match any destination country on"
                                           " file at PayPal.<br/>"
                                           "Make sure you deliver your package to an address listed on your PayPal account."),
                            extra_tags='safe block')
            return False

        return True

class GuestRedirectView(RedirectView):
    def _get_redirect_url(self, **kwargs):
        """
        Guest payment allows customer with no PayPal account to
        transfer the payment using a credit card instead of using their
        PayPal account balance. We don't run the standard PayPal validations
        as there's no such account.
        """
        if self.basket.is_empty:
            raise EmptyBasketException()

        customer_shipping_address = self.get_shipping_address(self.basket)
        is_return_to_merchant = self.checkout_session.is_return_to_store_enabled()

        #check that shipping address exists
        if not customer_shipping_address and not is_return_to_merchant:
            # we could not get shipping address - redirect to basket page with warning message
            logger.warning("customer's shipping address not found while verifying PayPal account")
            self.unfreeze_basket(kwargs['basket_id'])
            raise MissingShippingMethodException()

        params = {
            'basket': self.basket
        }

        if settings.DEBUG:
            # Determine the localserver's hostname to use when
            # in testing mode
            params['host'] = self.request.META['HTTP_HOST']

        params['paypal_params'] = self._get_paypal_params()

        params['receivers'] = self.get_receivers()
        self.align_receivers(params)

        redirect_url, pay_correlation_id = get_pay_request_attrs(**params)
        self.store_pay_transaction_id(pay_correlation_id)
        return redirect_url

class SuccessResponseView(PaymentDetailsView):
    has_error = False
    preview = True
    err_msg = _("A problem occurred communicating with PayPal "
                "- please try again later")

    def get_pay_transaction_id(self):
        self.pay_transaction_id = self.checkout_session.get_pay_transaction_id()
        if self.pay_transaction_id is None:
            # Manipulation - redirect to basket page with warning message
            logger.error("SuccessResponseView: Missing pay transaction id in session")
            messages.error(
                self.request,
                _("Unable to determine PayPal transaction details."))

    def load_frozen_basket(self, basket_id):
        # Lookup the frozen basket that this txn corresponds to
        try:
            basket = Basket.objects.get(id=basket_id, status=Basket.FROZEN)
        except Basket.DoesNotExist:
            return None

        # Assign strategy to basket instance
        if Selector:
            basket.strategy = Selector().strategy(self.request)

        # Re-apply any offers
        Applicator().apply(self.request, basket)

        return basket

    def post(self, request, *args, **kwargs):
        """
        We only support GET request
        """
        return HttpResponseNotAllowed(permitted_methods='GET')

    def get(self, request, *args, **kwargs):
        """
        Place an order.

        We fetch the txn details again and then proceed with oscar's standard
        payment details view for placing the order.
        """
        self.get_pay_transaction_id()
        if self.pay_transaction_id is None:
            return HttpResponseRedirect(reverse('customer:pending-packages'))
        error_msg = _(
            "A problem occurred communicating with PayPal "
            "- please try again later"
        )

        # Reload frozen basket which is specified in the URL
        basket = self.load_frozen_basket(kwargs['basket_id'])
        if not basket:
            logger.error("SuccessResponseView: no basket found")
            messages.error(self.request, error_msg)
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        submission = self.build_submission(basket=basket)
        self.submit(**submission)
        #we don't display the error message here but we redirect to
        #pending packages page
        if self.has_error:
            logger.critical("SuccessResponseView, error in submitting the order, the paypal transaction"
                            " has already benn carried")
            return HttpResponseRedirect(reverse('customer:pending-packages'))
        #Order placement process has successfully finished,
        #redirect to thank you page
        return HttpResponseRedirect(reverse('checkout:thank-you'))

    # Warning: This method can be removed when we drop support for Oscar 0.6
    def get_error_response(self):
        # We bypass the normal session checks for shipping address and shipping
        # method as they don't apply here.
        pass

    def get_context_data(self, **kwargs):
        ctx = super(SuccessResponseView, self).get_context_data(**kwargs)
        if 'error' in ctx:
            messages.error(self.request, ctx['error'])
            #Mark that order placement process has encountered an error
            #need to redirect to pending packages page with the error
            self.has_error = True
        return ctx

    def get_partner_share(self):
        """
        We would like to save in db the amount we've payed our partner
        we keep that saved under the 'amount_debited' attribute
        """
        return self.checkout_session.get_partner_share()


    def handle_payment(self, order_number, total, **kwargs):
        """
        Save order related data into DB
        We keep the pay key in the payment source reference attribute so we
        could finish the payment for the secondary receiver.
        Payment event contains the Pay request transaction id for audit
        The payment source stores the following:
        1 - amount_allocated = Order total value
        2 - amount_debited = Partner's share
        3 - amount_refunded = USendHome's share
        """
        # Record payment source and event
        partner_share = self.get_partner_share()
        source_type, is_created = SourceType.objects.get_or_create(name='PayPal')
        source = Source(source_type=source_type,
                        currency=getattr(settings, 'PAYPAL_CURRENCY', 'USD'),
                        amount_allocated=total.incl_tax,
                        amount_debited=partner_share,
                        amount_refunded=total.incl_tax - partner_share,
                        reference=self.pay_transaction_id)
        self.add_payment_source(source)
        self.add_payment_event('Settled', total.incl_tax)
        #delete partner's share from session
        self.checkout_session.delete_partner_share()
        #delete the paypal_ap attributes from session
        self.checkout_session.delete_pay_transaction_id()


    def unfreeze_basket(self, basket_id):
        basket = self.load_frozen_basket(basket_id)
        basket.thaw()


    def get_shipping_method(self, basket, shipping_address=None, **kwargs):
        """
        Return the shipping method used in session
        """
        shipping_method = super(SuccessResponseView, self).get_shipping_method(basket)
        return shipping_method

    def get_shipping_address(self, basket):
        """
        Return the shipping address as entered on our site
        """
        shipping_addr = super(SuccessResponseView, self).get_shipping_address(basket)
        return shipping_addr

    def submit(self, user, basket, shipping_address, shipping_method,
               order_total, payment_kwargs=None, order_kwargs=None):
        """
        Since we fallback to no shipping required, we must enforce that the only case its allowed
        is when customer returns items back to merchant and he provided us with a return label
        in all other cases, we must redirect to pending packages page, display a message to the customer
        and log this incident
        """
        if shipping_method.code == 'no-shipping-required' \
            and not self.checkout_session.is_return_to_store_prepaid_enabled():
            logger.critical("Placing an order, no shipping method was found, fallback to no shipping required,"
                            " user #%s" % user.id)
            messages.error(self.request, _("It seems that you've been idle for too long, please re-place your order."))
            return HttpResponseRedirect(reverse('customer:pending-packages'))

        return super(SuccessResponseView, self).submit(user, basket, shipping_address, shipping_method,
                                                      order_total, payment_kwargs, order_kwargs)


class CancelResponseView(generic.RedirectView):
    permanent = False

    def get(self, request, *args, **kwargs):
        basket = get_object_or_404(Basket, id=kwargs['basket_id'],
                                   status=Basket.FROZEN)
        basket.thaw()
        logger.info("Payment cancelled - basket #%s thawed", basket.id)
        return super(CancelResponseView, self).get(request, *args, **kwargs)

    def get_redirect_url(self, **kwargs):
        messages.error(self.request, _("PayPal transaction cancelled"))
        return reverse('customer:pending-packages')
