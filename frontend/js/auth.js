// Cognito sign-in with plain fetch (InitiateAuth, USER_PASSWORD_AUTH). No SDK, no hosted UI.
import { CONFIG } from "./config.js";

const STORAGE_KEY = "v2f.session";
const EXPIRY_SKEW_MS = 60_000;

const MESSAGES = {
  NotAuthorizedException: "Incorrect email or password.",
  UserNotFoundException: "Incorrect email or password.",
  TooManyRequestsException: "Too many attempts. Wait a moment and try again.",
  LimitExceededException: "Too many attempts. Wait a moment and try again.",
  PasswordResetRequiredException: "This account needs a password reset. Ask the demo administrator.",
  NEW_PASSWORD_REQUIRED:
    "This account needs a new password, which the demo UI does not support. Redeploy with a new DemoUserPassword.",
  NETWORK: "Could not reach the sign-in service. Check your connection and try again.",
};

export class AuthError extends Error {
  constructor(code) {
    super(MESSAGES[code] || `Sign-in failed (${code}).`);
    this.code = code;
  }
}

export function isConfigured() {
  return Boolean(CONFIG.userPoolClientId) && CONFIG.userPoolClientId !== "REPLACE_ME";
}

function decodeJwtPayload(token) {
  const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
  const padded = part + "=".repeat((4 - (part.length % 4)) % 4);
  const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

export async function signIn(email, password) {
  let res;
  try {
    res = await fetch(`${CONFIG.cognitoEndpoint}/`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
      },
      body: JSON.stringify({
        AuthFlow: "USER_PASSWORD_AUTH",
        ClientId: CONFIG.userPoolClientId,
        AuthParameters: { USERNAME: email, PASSWORD: password },
      }),
    });
  } catch {
    throw new AuthError("NETWORK");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new AuthError(String(body.__type || `HTTP_${res.status}`).split("#").pop());
  }
  if (body.ChallengeName) throw new AuthError(body.ChallengeName);

  const idToken = body.AuthenticationResult.IdToken;
  const claims = decodeJwtPayload(idToken);
  // Header: the user's Cognito `name` attribute (e.g. "John Smith"), falling back to the Cognito username
  // (with email as the sign-in attribute, Cognito generates the username as an id).
  const username = claims["cognito:username"] || "";
  const session = { idToken, username, displayName: claims.name || username, expiresAt: claims.exp * 1000 };
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
  return session;
}

export function currentSession() {
  try {
    const session = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
    if (session && session.username && session.expiresAt - EXPIRY_SKEW_MS > Date.now()) return session;
  } catch {
    /* fall through */
  }
  signOut();
  return null;
}

export function signOut() {
  sessionStorage.removeItem(STORAGE_KEY);
}
