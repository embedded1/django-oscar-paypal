import logging
from django.contrib import messages
from django.http import HttpResponseRedirect
from oscar.core.loading import get_class
from decimal import ROUND_FLOOR, Decimal as D
from django.core.urlresolvers import reverse
from django.conf import settings
from django.utils.translation import ugettext as _
from paypal.adaptive.exceptions import GeneralException

CheckoutSessionMixin = get_class('checkout.session', 'CheckoutSessionMixin')
logger = logging.getLogger('paypal.adaptive')

TWO_PLACES = D('0.01')

class PaymentSourceMixin(CheckoutSessionMixin):
    def dispatch(self, request, *args, **kwargs):
        self.basket = request.basket
        if not getattr(self, 'preview', False):
            self.package = self.basket.get_package()
            if self.package is None:
                logger.error("couldn't get package"
                             " from basket for partner share calculations,"
                             " basket id = %s" % self.basket.id)
                messages.error(
                    request, _("Something went terribly wrong, please try again later"))
                return HttpResponseRedirect(reverse('customer:pending-packages'))
        return super(PaymentSourceMixin, self).dispatch(request, *args, **kwargs)


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
            logger.error("Shipping repository could not be fetched from cache")
            raise GeneralException()

        #return the selected shipping method
        code = self.checkout_session.shipping_method_code(self.basket)
        return repo.get_shipping_method_by_code(code)

    def get_shipping_discounts(self):
        return self.get_shipping_vouchers() + self.get_shipping_offers()

    def get_shipping_offers(self):
        """
        This function returns the sum of all shipping offers
        currently we have only 1 such offer: referral program
        """
        shipping_offers = self.basket.offer_discounts
        discount = D('0.00')
        for offer in shipping_offers:
            range_name = offer['offer'].benefit.range.name
            if range_name == 'shipping_method':
                discount += offer['discount']
        return discount

    def get_shipping_vouchers(self):

        shipping_vouchers = self.basket.voucher_discounts
        discount = D('0.00')
        for voucher in shipping_vouchers:
            range_name = voucher['voucher'].benefit.range.name
            if range_name == 'shipping_method':
                discount += voucher['discount']
        return discount

    def get_partner_payment_info(self, basket):
        """
        This functions returns:
            1 - partner share
            2 - partner_payment_settings
        """
        partner = self.package.stockrecords.all().prefetch_related(
            'partner', 'partner__payments_settings')[0].partner
        partner_order_payment_settings = partner.active_payment_settings
        parnter_share = D('0.0')

        if partner_order_payment_settings:
            parnter_share += self.calc_partner_share(basket, partner_order_payment_settings)

        return parnter_share, partner_order_payment_settings

    def calc_partner_share(self, basket, partner_order_payment_settings):
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
        partner_share = shipping_charge_incl_revenue = bank_fee = D('0.0')
        order_total_no_discounts =  self.basket.total_excl_tax_excl_discounts

        #get all shipping discounts
        shipping_discounts = self.get_shipping_discounts()
        #service discounts is all_discounts - shipping_discounts
        services_discounts = self.basket.total_discount - shipping_discounts

        #selected shipping method is not available for prepaid return labels
        if not self.checkout_session.is_return_to_store_prepaid_enabled():
            selected_method = self.get_selected_shipping_method()
            #we couldn't find selected shipping method in cache, need to cancel
            #the transaction and show error message
            if selected_method is None:
                logger.error("couldn't get selected shipping method from cache")
                raise GeneralException()

            easypost_charge = D('0.05')
            shipping_margin = selected_method.shipping_revenue

            bank_fee_line = self.basket.get_item_at_position(settings.BANK_FEE_POSITION)
            if bank_fee_line is None:
                logger.error("couldn't get bank fee line from basket")
                raise GeneralException()

            bank_fee = bank_fee_line.price_incl_tax

            #check if shipping insurance is needed
            if self.basket.contains_line_at_position(settings.INSURANCE_FEE_POSITION):
                insurance_charge_incl_revenue = selected_method.shipping_insurance_cost()
                #check if partner pays for shipping insurance
                if partner_order_payment_settings.is_paying_shipping_insurance:
                    partner_share += selected_method.shipping_insurance_base_rate()

            shipping_charge_incl_revenue = selected_method.shipping_method_cost()
            #if partner pays for postage we need to transfer him the postage costs
            #and the bank fee we received for the postage, which is the bank_fee - 0.3
            #otherwise, we only transfer him 0.3
            if partner_order_payment_settings.postage_paid_by_partner(selected_method.carrier):
                partner_bank_fee = (bank_fee - D('0.3')) if bank_fee > D('0.3') else D('0.0')
                partner_share += selected_method.partner_postage_cost() + partner_bank_fee
            else:
                partner_share += D('0.3')

        #zero out the shipping discounts if they don't apply to partner
        if not partner_order_payment_settings.are_shipping_offers_apply:
            shipping_discounts = D('0.0')

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

        if partner_share > basket.total_incl_tax:
            return basket.total_incl_tax

        return partner_share.quantize(TWO_PLACES, rounding=ROUND_FLOOR)


    # Warning: This method can be removed when we drop support for Oscar 0.6
    def get_error_response(self):
        # We bypass the normal session checks for shipping address and shipping
        # method as they don't apply here.
        pass

    def handle_successful_order(self, order):
        """
        We don't want to shoot the email confirmation just yet, so
        here we do all the order processing stuff
        """
        # Save order id in session so thank-you page can load it
        self.request.session['checkout_order_id'] = order.id
        #send the post_checkout signal
        self.view_signal.send(
            sender=self, order=order, user=self.request.user,
            request=self.request, response=None, package=self.package)
