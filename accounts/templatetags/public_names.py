"""Template filter that keeps email addresses out of public pages.

Usernames are shown to other users (book pages, requests, chats, ...). Accounts created before
signup stopped accepting "@" in usernames may use their email address as the username, so this
filter masks such names ("saurabhgiri95893@gmail.com" -> "sau***") wherever another person's name
is displayed. Staff screens keep showing the real username.
"""
from django import template

register = template.Library()


@register.filter
def public_name(value):
    """Accepts a User (or a plain string) and returns a name that is safe to show publicly."""
    name = getattr(value, 'username', value) or ''
    name = str(name)
    if '@' not in name:
        return name
    local = name.split('@', 1)[0]
    return (local[:3] if len(local) > 3 else local[:1]) + '***'
