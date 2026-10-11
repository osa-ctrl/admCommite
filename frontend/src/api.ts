export type JobStatus = "queued" | "downloading" | "joining" | "done" | "error" | "cancelled"

export interface Job {
  id: string
  url: string
  filename: string
  status: JobStatus
  done: number
  total: number | null
  error: string | null
  log: string[]
  size: number | null
  created: number
  finished: number | null
  expires: number | null
}

export interface NewJob {
  url: string
  filename: string
  headers: string
  workers: number
  variant: number | null
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let message = res.statusText
    try {
      const data = await res.json()
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)
    } catch {
      // not JSON, keep statusText
    }
    throw new ApiError(res.status, message)
  }
  return res.json()
}

export const api = {
  me: () => request<{ ok: boolean }>("GET", "/api/me"),
  login: (password: string) => request<{ ok: boolean }>("POST", "/api/login", { password }),
  logout: () => request<{ ok: boolean }>("POST", "/api/logout"),
  jobs: () => request<Job[]>("GET", "/api/jobs"),
  create: (job: NewJob) => request<Job>("POST", "/api/jobs", job),
  remove: (id: string) => request<{ ok: boolean }>("DELETE", `/api/jobs/${id}`),
  fileUrl: (id: string) => `/api/jobs/${id}/file`,
}
