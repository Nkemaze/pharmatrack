// Auth-page helpers: password visibility toggles and a one-shot loading
// state on the submit button (only fires for a valid, actually-submitted
// form, so a required-field abort leaves the button usable).
(function () {
    document.querySelectorAll('[data-pw-toggle]').forEach(function (input) {
        var btn = input.parentElement.querySelector('[data-pw-btn]');
        if (!btn) return;
        btn.addEventListener('click', function () {
            var show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            var icon = btn.querySelector('span');
            if (icon) icon.textContent = show ? 'visibility_off' : 'visibility';
            btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        });
    });

    document.querySelectorAll('form[data-submit-spinner]').forEach(function (form) {
        form.addEventListener('submit', function () {
            var btn = form.querySelector('[data-spinner]');
            if (!btn || btn.disabled) return;
            btn.disabled = true;
            btn.innerHTML =
                '<span class="spinner-inline"></span>Please wait\u2026';
        });
    });
})();