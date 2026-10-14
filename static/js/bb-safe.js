// Tiny shared helper: HTML-escape anything that came from the server or a user
// before it is placed inside an innerHTML template. Prefer textContent when no
// markup is needed.
(function () {
    'use strict';
    var MAP = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '`': '&#96;'};
    window.bbEsc = function (value) {
        return String(value === null || value === undefined ? '' : value).replace(/[&<>"'`]/g, function (c) { return MAP[c]; });
    };
})();
