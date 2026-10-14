from .models import ShopkeeperPartner


def partner_nav(request):
    """Lets base.html swap in the Partner navigation without every view
    passing anything. nav_partner is set only for APPROVED partners (that is
    what unlocks the partner interface); nav_partner_application is set for
    any application so pending/rejected users can still see their status."""
    if not request.user.is_authenticated:
        return {}
    application = ShopkeeperPartner.objects.filter(user=request.user).first()
    return {
        'nav_partner': application if application and application.is_approved else None,
        'nav_partner_application': application,
    }
