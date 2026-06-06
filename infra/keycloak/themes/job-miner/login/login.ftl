<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=false displayMessage=!messagesPerField.existsError('username','password') displayRequiredFields=false; section>
  <#if section = "header">
    <span class="jm-page-title">Sign in to your Job Miner account</span>
  <#elseif section = "form">
    <div class="jm-shell">
      <aside class="jm-benefits">
        <div class="jm-illustration" aria-hidden="true">☑</div>
        <h2>On signing in, you can</h2>
        <ul>
          <li>Build your profile and let recruiters find you</li>
          <li>Get matching job recommendations</li>
          <li>Save, apply, and track jobs from one place</li>
        </ul>
      </aside>
      <section class="jm-card">
        <div class="jm-card-main">
          <h1>Sign in to your account</h1>
          <p class="jm-subtitle">Access your candidate profile and recommendations.</p>
          <#if message?has_content && (message.type != 'warning' || !isAppInitiatedAction??)>
            <div class="jm-alert jm-alert-${message.type}">${kcSanitize(message.summary)?no_esc}</div>
          </#if>
          <form id="kc-form-login" class="jm-form" onsubmit="login.disabled = true; return true;" action="${url.loginAction}" method="post">
            <label for="username"><#if !realm.loginWithEmailAllowed>${msg("username")}<#else>Username or email</#if></label>
            <input tabindex="1" id="username" name="username" value="${(login.username!'')}" type="text" autofocus autocomplete="username" aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>" />

            <label for="password">${msg("password")}</label>
            <input tabindex="2" id="password" name="password" type="password" autocomplete="current-password" aria-invalid="<#if messagesPerField.existsError('username','password')>true</#if>" />

            <#if messagesPerField.existsError('username','password')>
              <div class="jm-field-error">${kcSanitize(messagesPerField.getFirstError('username','password'))?no_esc}</div>
            </#if>

            <div class="jm-form-row">
              <#if realm.rememberMe && !usernameEditDisabled??>
                <label class="jm-checkbox"><input tabindex="3" id="rememberMe" name="rememberMe" type="checkbox" <#if login.rememberMe??>checked</#if> /> ${msg("rememberMe")}</label>
              </#if>
              <#if realm.resetPasswordAllowed>
                <a href="${url.loginResetCredentialsUrl}">${msg("doForgotPassword")}</a>
              </#if>
            </div>

            <input type="hidden" id="id-hidden-input" name="credentialId" <#if auth.selectedCredential?has_content>value="${auth.selectedCredential}"</#if>/>
            <div class="jm-action-row">
              <button tabindex="4" class="jm-primary jm-action-btn" name="login" id="kc-login" type="submit">Sign in</button>
              <#if realm.registrationAllowed && !registrationDisabled??>
                <a class="jm-secondary jm-action-btn" href="${url.registrationUrl}">Sign up</a>
              </#if>
            </div>
          </form>
        </div>
      </section>
    </div>
  <#elseif section = "socialProviders">
    <#if realm.password && social.providers?? && social.providers?has_content>
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
  </#if>
</@layout.registrationLayout>
