// Replaces inline onclick/onsubmit/onchange handlers (blocked by our Content-Security-Policy).
//   <form data-confirm="Are you sure?">         asks before submitting
//   <select data-autosubmit>                    submits its form when changed
//   <a data-submit-form="formId">               submits that form instead of following the link
(function () {
    'use strict';
    document.addEventListener('submit', function (e) {
        var msg = e.target && e.target.getAttribute && e.target.getAttribute('data-confirm');
        if (msg && !window.confirm(msg)) e.preventDefault();
    });
    document.addEventListener('change', function (e) {
        var el = e.target;
        if (el && el.hasAttribute && el.hasAttribute('data-autosubmit') && el.form) el.form.submit();
    });
    document.addEventListener('click', function (e) {
        var link = e.target.closest && e.target.closest('[data-submit-form]');
        if (!link) return;
        var form = document.getElementById(link.getAttribute('data-submit-form'));
        if (form) { e.preventDefault(); form.submit(); }
    });
})();
