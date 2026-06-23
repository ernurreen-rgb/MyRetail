export function getApiBaseUrl() {
  return process.env.MYRETAIL_API_URL ?? "http://localhost:8000";
}
