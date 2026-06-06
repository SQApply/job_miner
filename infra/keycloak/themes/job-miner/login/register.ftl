<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=false displayMessage=true displayRequiredFields=true; section>
  <#if section = "header">
    <span class="jm-page-title">Create your Job Miner account</span>
  <#elseif section = "form">
    <div class="jm-shell">
      <aside class="jm-benefits">
        <div class="jm-illustration" aria-hidden="true">✓</div>
        <h2>After registering, you will</h2>
        <ul>
          <li>Use email verification when SMTP is configured</li>
          <li>Upload a resume before recommendations are generated</li>
          <li>Use the same email in your resume and account</li>
        </ul>
      </aside>
      <section class="jm-card jm-register-card">
        <div class="jm-card-main">
          <h1>Create your account</h1>
          <p class="jm-subtitle">Resume upload is mandatory after your first login. Email verification can be enabled after SMTP is configured.</p>
          <#if message?has_content>
            <div class="jm-alert jm-alert-${message.type}">${kcSanitize(message.summary)?no_esc}</div>
          </#if>
          <form id="kc-register-form" class="jm-form" action="${url.registrationAction}" method="post" novalidate>
            <label for="fullName">Full name</label>
            <input
              type="text"
              id="fullName"
              name="fullName"
              value="${((register.formData.firstName!'') + ' ' + (register.formData.lastName!''))?trim}"
              autocomplete="name"
              minlength="2"
              maxlength="80"
              pattern="^[A-Za-z][A-Za-z .'-]{1,79}$"
              required
              aria-describedby="fullNameHint"
            />
            <small id="fullNameHint" class="jm-field-hint">Use letters only. Numbers are not allowed.</small>
            <div class="jm-field-error" id="fullNameError" aria-live="polite"></div>
            <input type="hidden" id="firstName" name="firstName" value="${(register.formData.firstName!'')}" />
            <input type="hidden" id="lastName" name="lastName" value="${(register.formData.lastName!'')}" />

            <label for="email">Email</label>
            <input
              type="email"
              id="email"
              name="email"
              value="${(register.formData.email!'')}"
              autocomplete="email"
              maxlength="254"
              required
              aria-describedby="emailHint"
            />
            <small id="emailHint" class="jm-field-hint">Use a real email. In production, you must verify this email before logging in.</small>
            <#if messagesPerField.existsError('email')>
              <div class="jm-field-error">${kcSanitize(messagesPerField.getFirstError('email'))?no_esc}</div>
            </#if>

            <#if !realm.registrationEmailAsUsername>
              <input type="hidden" id="username" name="username" value="${(register.formData.username!'')}" />
            </#if>

            <#if passwordRequired??>
              <label for="password">Password</label>
              <input
                type="password"
                id="password"
                name="password"
                autocomplete="new-password"
                minlength="8"
                pattern="^(?=.*[A-Z])(?=.*[^A-Za-z0-9]).{8,}$"
                required
                aria-describedby="passwordHint"
              />
              <small id="passwordHint" class="jm-field-hint">Minimum 8 characters, at least one uppercase letter, and at least one special character.</small>
              <#if messagesPerField.existsError('password')>
                <div class="jm-field-error">${kcSanitize(messagesPerField.getFirstError('password'))?no_esc}</div>
              </#if>

              <label for="password-confirm">Confirm password</label>
              <input type="password" id="password-confirm" name="password-confirm" autocomplete="new-password" required />
              <div class="jm-field-error" id="passwordConfirmError" aria-live="polite"></div>
              <#if messagesPerField.existsError('password-confirm')>
                <div class="jm-field-error">${kcSanitize(messagesPerField.getFirstError('password-confirm'))?no_esc}</div>
              </#if>
            </#if>

            <div class="jm-info-box">
              <strong>Resume upload is mandatory after signup.</strong>
              <span>The email in your resume must be the same as this account email. Job recommendations are generated only after the resume passes this check.</span>
            </div>

            <label class="jm-checkbox jm-confirmation">
              <input id="resumeEmailConfirm" name="resumeEmailConfirm" type="checkbox" required />
              I understand that my resume email must match my signup email.
            </label>
            <div class="jm-field-error" id="resumeConfirmError" aria-live="polite"></div>

            <button class="jm-primary" type="submit">Register now</button>
          </form>
        </div>
      </section>
    </div>
    <script>
      (function () {
        const form = document.getElementById('kc-register-form');
        const fullName = document.getElementById('fullName');
        const firstName = document.getElementById('firstName');
        const lastName = document.getElementById('lastName');
        const password = document.getElementById('password');
        const passwordConfirm = document.getElementById('password-confirm');
        const resumeConfirm = document.getElementById('resumeEmailConfirm');
        const fullNameError = document.getElementById('fullNameError');
        const passwordConfirmError = document.getElementById('passwordConfirmError');
        const resumeConfirmError = document.getElementById('resumeConfirmError');

        if (!form) return;

        function setError(node, message) {
          if (node) node.textContent = message || '';
        }

        function normalizeName(value) {
          return (value || '').replace(/\s+/g, ' ').trim();
        }

        form.addEventListener('submit', function (event) {
          let valid = true;
          const nameValue = normalizeName(fullName.value);
          const namePattern = /^[A-Za-z][A-Za-z .'-]{1,79}$/;
          setError(fullNameError, '');
          setError(passwordConfirmError, '');
          setError(resumeConfirmError, '');

          if (!namePattern.test(nameValue) || /\d/.test(nameValue)) {
            setError(fullNameError, 'Enter a valid full name using letters only. Numbers are not allowed.');
            valid = false;
          } else {
            const parts = nameValue.split(' ');
            firstName.value = parts.slice(0, Math.max(1, parts.length - 1)).join(' ');
            lastName.value = parts.length > 1 ? parts.slice(-1).join('') : '';
            const username = document.getElementById('username');
            if (username) username.value = document.getElementById('email').value;
          }

          if (password && passwordConfirm && password.value !== passwordConfirm.value) {
            setError(passwordConfirmError, 'Passwords do not match.');
            valid = false;
          }

          if (resumeConfirm && !resumeConfirm.checked) {
            setError(resumeConfirmError, 'You must confirm the resume-email requirement.');
            valid = false;
          }

          if (!form.checkValidity() || !valid) {
            event.preventDefault();
            form.reportValidity();
          }
        });
      })();
    </script>
  <#elseif section = "socialProviders">
    <#if social.providers?? && social.providers?has_content>
      <div class="jm-social-wrap">
        <div class="jm-divider"><span>Or continue with</span></div>
        <#list social.providers as p>
          <a id="social-${p.alias}" class="jm-google" href="${p.loginUrl}">
            <span class="jm-google-mark">G</span><span>${p.displayName!p.alias}</span>
          </a>
        </#list>
      </div>
    </#if>
  <#elseif section = "info">
    <div class="jm-register-link">Already registered? <a href="${url.loginUrl}">Login</a> here</div>
  </#if>
</@layout.registrationLayout>
