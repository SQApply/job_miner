import Keycloak from 'keycloak-js';
import { api } from './api';

let keycloak = null;

export async function initKeycloak() {
  if (keycloak) return keycloak;
  const config = await api('/auth/keycloak-config');
  keycloak = new Keycloak({ url: config.url, realm: config.realm, clientId: config.clientId });
  await keycloak.init({ onLoad: 'login-required', pkceMethod: 'S256', checkLoginIframe: false });
  setInterval(() => keycloak.updateToken(30).catch(() => keycloak.login()), 20000);
  return keycloak;
}

export function getToken() {
  return keycloak?.token || null;
}

export function logout() {
  return keycloak?.logout({ redirectUri: window.location.origin });
}
