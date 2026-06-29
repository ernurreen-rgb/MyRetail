export const DEFAULT_TENANT = "myretail";

export const AUTH_COOKIE_NAMES = {
  accessToken: "myretail_access_token",
  tenant: "myretail_tenant",
} as const;

export type LoginFormValues = {
  tenant: string;
  email: string;
  password: string;
};

export type AuthUser = {
  email: string;
  full_name: string | null;
  roles: string[];
};

export type LoginResponse = {
  access_token: string;
  token_type: string;
  expires_in: number;
  tenant: string;
  user: AuthUser;
};

export type SessionResponse = {
  tenant: string;
  user: AuthUser;
};

export type AuthSessionCredentials = {
  accessToken: string;
  tenant: string;
};

export type AuthSession = AuthSessionCredentials & {
  user: AuthUser;
};

export type LoginClientResult =
  | {
      status: "success";
    }
  | {
      status: "error";
      message: string;
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isAuthUser(value: unknown): value is AuthUser {
  if (!isRecord(value)) {
    return false;
  }

  return (
    typeof value.email === "string" &&
    isNullableString(value.full_name) &&
    isStringArray(value.roles)
  );
}

export function isLoginResponse(value: unknown): value is LoginResponse {
  if (!isRecord(value)) {
    return false;
  }

  return (
    typeof value.access_token === "string" &&
    value.access_token.length > 0 &&
    value.token_type === "bearer" &&
    typeof value.expires_in === "number" &&
    Number.isFinite(value.expires_in) &&
    value.expires_in > 0 &&
    typeof value.tenant === "string" &&
    value.tenant.length > 0 &&
    isAuthUser(value.user)
  );
}

export function isSessionResponse(value: unknown): value is SessionResponse {
  return (
    isRecord(value) &&
    typeof value.tenant === "string" &&
    value.tenant.length > 0 &&
    isAuthUser(value.user)
  );
}

export function canManageProducts(roles: string[]) {
  return roles.some((role) => role === "Owner" || role === "Admin");
}

export function canManageStock(roles: string[]) {
  return roles.some((role) => role === "Owner" || role === "Admin");
}

function getMessageFromPayload(value: unknown): string | null {
  if (!isRecord(value) || typeof value.message !== "string") {
    return null;
  }

  return value.message;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export async function login(values: LoginFormValues): Promise<LoginClientResult> {
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        tenant: values.tenant.trim() || DEFAULT_TENANT,
        email: values.email.trim(),
        password: values.password,
      }),
    });

    if (response.ok) {
      return { status: "success" };
    }

    const payload = await readJson(response);

    return {
      status: "error",
      message: getMessageFromPayload(payload) ?? "Не удалось войти. Попробуйте ещё раз.",
    };
  } catch {
    return {
      status: "error",
      message: "Не удалось связаться с веб-приложением. Проверьте подключение и попробуйте ещё раз.",
    };
  }
}
