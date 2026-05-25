export async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const detail = payload?.detail ?? payload;
    const message =
      typeof detail === "string" ? detail : detail?.message || text || `HTTP ${response.status}`;
    const error = new Error(message);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return payload;
}

export function isMoveTargetConflict(error) {
  return (
    error?.status === 409 &&
    (error.detail?.code === "target_file_exists" ||
      String(error.message || "").includes("same name already exists"))
  );
}
