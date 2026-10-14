from . import twofactor

_EXEMPT = ('/admin/login/', '/admin/logout/')


class StaffTwoFactorAdminMiddleware:
    """The Django admin has its own login page and its own views, so the staff_required
    decorator doesn't cover it. A staff user who hasn't passed 2FA this session is sent to the
    2FA page instead of reaching anything under /admin/."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path
        if (path.startswith('/admin/') and path not in _EXEMPT
                and request.user.is_authenticated and request.user.is_staff
                and twofactor.required() and not twofactor.is_verified(request)):
            return twofactor.challenge_redirect(request)
        return self.get_response(request)
