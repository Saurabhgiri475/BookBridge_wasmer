// auth-ui.js
// Progressive enhancement for the signup / login forms (see accounts/forms.py for the data- attributes):
//   [data-password-toggle]  -> show/hide button beside a password input
//   [data-strength]         -> live password strength meter (advisory only - Django's validators still decide)
//   [data-availability]     -> live username/email availability check against the form's data-availability-url
// Everything degrades to a normal form if this script fails to load.
(function () {
    'use strict';

    // ---- show / hide password ------------------------------------------------
    document.querySelectorAll('input[data-password-toggle]').forEach(function (input) {
        var group = document.createElement('div');
        group.className = 'input-group';
        input.parentNode.insertBefore(group, input);
        group.appendChild(input);
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-outline-secondary';
        btn.setAttribute('aria-label', 'Show password');
        btn.setAttribute('aria-pressed', 'false');
        btn.innerHTML = '<i class="bi bi-eye" aria-hidden="true"></i>';
        btn.addEventListener('click', function () {
            var show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            btn.setAttribute('aria-pressed', String(show));
            btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
            btn.firstChild.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
            input.focus();
        });
        group.appendChild(btn);
    });

    // ---- password strength -----------------------------------------------------
    var COMMON = ['password', 'passw0rd', 'qwerty', 'letmein', 'welcome', 'admin', 'iloveyou', 'abc123', 'bookbridge', '123456', '111111'];
    var LABELS = ['Too weak', 'Weak', 'Fair', 'Good', 'Strong'];
    var COLORS = ['bg-danger', 'bg-danger', 'bg-warning', 'bg-info', 'bg-success'];

    function hasSequence(p) {
        var lower = p.toLowerCase();
        for (var i = 0; i < lower.length - 3; i++) {
            var a = lower.charCodeAt(i), b = lower.charCodeAt(i + 1), c = lower.charCodeAt(i + 2), d = lower.charCodeAt(i + 3);
            if (b - a === 1 && c - b === 1 && d - c === 1) return true;
            if (a - b === 1 && b - c === 1 && c - d === 1) return true;
        }
        return false;
    }

    function assess(pw, personal) {
        var tips = [];
        if (!pw) return { score: 0, tips: [], empty: true };
        var lower = pw.toLowerCase();
        var classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter(function (re) { return re.test(pw); }).length;
        var score = 0;
        if (pw.length >= 8) score++;
        if (pw.length >= 12) score++;
        if (classes >= 3) score++;
        if (pw.length >= 16 || (pw.length >= 12 && classes === 4)) score++;
        if (pw.length < 8) tips.push('Use at least 8 characters.');
        if (classes < 3) tips.push('Mix upper and lower case, numbers and symbols.');
        if (pw.length < 12) tips.push('Longer is stronger - try 12+ characters or a few random words.');
        var capped = false;
        if (/^\d+$/.test(pw)) { capped = true; tips.unshift('Don\u2019t use only numbers.'); }
        if (COMMON.some(function (w) { return lower.indexOf(w) !== -1; })) { capped = true; tips.unshift('Avoid common words like "password" or "123456".'); }
        if (/(.)\1{2,}/.test(pw)) { capped = true; tips.push('Avoid repeating the same character.'); }
        if (hasSequence(pw)) { capped = true; tips.push('Avoid sequences like "abcd" or "4321".'); }
        if (personal.some(function (w) { return w.length >= 3 && lower.indexOf(w) !== -1; })) { capped = true; tips.unshift('Don\u2019t include your username or email.'); }
        if (capped) score = Math.min(score, 1);
        return { score: Math.max(0, Math.min(4, score)), tips: tips.slice(0, 2) };
    }

    document.querySelectorAll('input[data-strength]').forEach(function (input) {
        var holder = input.closest('.input-group') || input;
        var wrap = document.createElement('div');
        wrap.className = 'mt-2';
        wrap.innerHTML =
            '<div class="progress" style="height:6px" role="progressbar" aria-label="Password strength" aria-valuemin="0" aria-valuemax="4" aria-valuenow="0">' +
            '<div class="progress-bar" style="width:0%"></div></div>' +
            '<div class="small mt-1 text-muted" aria-live="polite"><strong class="bb-strength-label"></strong> <span class="bb-strength-tips"></span></div>';
        holder.parentNode.insertBefore(wrap, holder.nextSibling);
        var bar = wrap.querySelector('.progress-bar'), meter = wrap.querySelector('.progress');
        var label = wrap.querySelector('.bb-strength-label'), tipsEl = wrap.querySelector('.bb-strength-tips');
        var form = input.form;

        function personalWords() {
            var out = [];
            ['username', 'email'].forEach(function (n) {
                var el = form && form.elements[n];
                if (el && el.value) out.push(el.value.split('@')[0].toLowerCase());
            });
            return out.filter(Boolean);
        }
        function update() {
            var r = assess(input.value, personalWords());
            bar.className = 'progress-bar ' + (r.empty ? '' : COLORS[r.score]);
            bar.style.width = r.empty ? '0%' : ((r.score + 1) * 20) + '%';
            meter.setAttribute('aria-valuenow', String(r.empty ? 0 : r.score));
            label.textContent = r.empty ? '' : LABELS[r.score];
            tipsEl.textContent = r.tips.join(' ');
        }
        input.addEventListener('input', update);
        if (form) ['username', 'email'].forEach(function (n) { if (form.elements[n]) form.elements[n].addEventListener('input', update); });
        update();
    });

    // ---- live availability -------------------------------------------------------
    document.querySelectorAll('input[data-availability]').forEach(function (input) {
        var url = input.form && input.form.getAttribute('data-availability-url');
        if (!url) return;
        var kind = input.getAttribute('data-availability');
        var msg = document.createElement('div');
        msg.className = 'small mt-1';
        msg.setAttribute('aria-live', 'polite');
        input.parentNode.insertBefore(msg, input.nextSibling);
        var timer = null, controller = null, cache = {};

        function show(state, text) {
            msg.className = 'small mt-1 ' + (state === true ? 'text-success' : state === false ? 'text-danger' : 'text-muted');
            msg.innerHTML = text ? '<i class="bi ' + (state === true ? 'bi-check-circle' : state === false ? 'bi-x-circle' : 'bi-hourglass-split') + '"></i> ' : '';
            msg.appendChild(document.createTextNode(text || ''));
            input.classList.toggle('is-invalid', state === false);
            input.classList.toggle('is-valid', state === true);
        }
        function ready(v) { return kind === 'email' ? /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) : v.length >= 3; }

        function check() {
            var v = input.value.trim();
            if (controller) controller.abort();
            if (!v) { show(null, ''); return; }
            if (!ready(v)) { show(null, ''); return; }
            if (cache[v]) { show(cache[v].available, cache[v].message); return; }
            show(null, 'Checking...');
            controller = new AbortController();
            fetch(url + '?field=' + encodeURIComponent(kind) + '&value=' + encodeURIComponent(v), {
                signal: controller.signal, headers: { 'Accept': 'application/json' }, credentials: 'same-origin',
            }).then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
              .then(function (res) {
                  if (res.status === 429 || res.data.available === null) { show(null, res.data.message || ''); return; }
                  cache[v] = res.data;
                  if (input.value.trim() === v) show(res.data.available, res.data.message);
              })
              .catch(function (err) { if (err.name !== 'AbortError') show(null, ''); });
        }
        input.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(check, 450); });
        input.addEventListener('blur', function () { clearTimeout(timer); check(); });
    });
})();
