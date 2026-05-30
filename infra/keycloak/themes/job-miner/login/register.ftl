<#import "template.ftl" as layout>
<@layout.registrationLayout displayInfo=false displayMessage=true displayRequiredFields=true; section>
  <#if section = "header">
    <span class="jm-page-title">Create your Job Miner profile</span>
  <#elseif section = "form">
    <div class="jm-shell">
      <aside class="jm-benefits">
        <div class="jm-illustration" aria-hidden="true">✓</div>
        <h2>On registering, you can</h2>
        <ul>
          <li>Build your profile and let recruiters find you</li>
          <li>Get job postings delivered to your email</li>
          <li>Find a job and grow your career</li>
        </ul>
      </aside>
      <section class="jm-card jm-register-card">
        <div class="jm-card-main">
          <h1>Create your Job Miner profile</h1>
          <p class="jm-subtitle">Search and apply to jobs from one place.</p>
          <form id="kc-register-form" class="jm-form" action="${url.registrationAction}" method="post">
            <label for="firstName">${msg("firstName")}</label>
            <input type="text" id="firstName" name="firstName" value="${(register.formData.firstName!'')}" autocomplete="given-name" />

            <label for="lastName">${msg("lastName")}</label>
            <input type="text" id="lastName" name="lastName" value="${(register.formData.lastName!'')}" autocomplete="family-name" />

            <label for="email">${msg("email")}</label>
            <input type="email" id="email" name="email" value="${(register.formData.email!'')}" autocomplete="email" />

            <#if !realm.registrationEmailAsUsername>
              <label for="username">${msg("username")}</label>
              <input type="text" id="username" name="username" value="${(register.formData.username!'')}" autocomplete="username" />
            </#if>

            <#if passwordRequired??>
              <label for="password">${msg("password")}</label>
              <input type="password" id="password" name="password" autocomplete="new-password" />
              <label for="password-confirm">${msg("passwordConfirm")}</label>
              <input type="password" id="password-confirm" name="password-confirm" autocomplete="new-password" />
            </#if>

            <p class="jm-terms">By creating an account, you agree to use Job Miner according to your organization's access policy.</p>
            <button class="jm-primary" type="submit">Register now</button>
          </form>
        </div>
      </section>
    </div>
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
