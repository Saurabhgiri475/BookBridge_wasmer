from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import render

from .models import WalletTransaction
from .services import get_wallet, CASHBACK_PERCENT, REFERRAL_SIGNUP_BONUS, REFERRAL_REWARD


@login_required
def wallet_detail(request):
    wallet = get_wallet(request.user)
    txns = WalletTransaction.objects.filter(wallet=wallet).select_related('order')
    page_obj = Paginator(txns, 15).get_page(request.GET.get('page'))

    referral_link = request.build_absolute_uri(f'/accounts/signup/?ref={request.user.referral_code}')
    referrals_count = request.user.referrals.count()

    context = {
        'wallet': wallet,
        'page_obj': page_obj,
        'referral_link': referral_link,
        'referrals_count': referrals_count,
        'cashback_percent': CASHBACK_PERCENT,
        'referral_signup_bonus': REFERRAL_SIGNUP_BONUS,
        'referral_reward': REFERRAL_REWARD,
    }
    return render(request, 'wallet/detail.html', context)
