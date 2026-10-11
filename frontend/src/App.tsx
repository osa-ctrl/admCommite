import { useCallback, useEffect, useState, type FormEvent } from "react"
import { AlertCircle, ChevronDown, Download, LogOut, Loader2, Lock, Trash2, X } from "lucide-react"

import { api, ApiError, type Job, type JobStatus } from "@/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Progress } from "@/components/ui/progress"
import { Textarea } from "@/components/ui/textarea"

type Auth = "checking" | "out" | "in"

export default function App() {
  const [auth, setAuth] = useState<Auth>("checking")

  useEffect(() => {
    api.me().then(() => setAuth("in"), () => setAuth("out"))
  }, [])

  if (auth === "checking") {
    return (
      <Shell>
        <Loader2 className="mx-auto size-6 animate-spin text-muted-foreground" />
      </Shell>
    )
  }
  if (auth === "out") return <LoginScreen onLogin={() => setAuth("in")} />
  return <Downloader onLogout={() => setAuth("out")} />
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <main className="min-h-svh bg-muted px-4 py-10 sm:py-16">
      <div className="mx-auto flex w-full max-w-2xl flex-col gap-6">{children}</div>
    </main>
  )
}

function LoginScreen({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(password)
      onLogin()
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Неверный пароль" : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Shell>
      <Card className="mx-auto w-full max-w-sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="size-4" /> TS Downloader
          </CardTitle>
          <CardDescription>Введите пароль, чтобы продолжить.</CardDescription>
        </CardHeader>
        <form onSubmit={submit}>
          <CardContent className="flex flex-col gap-3">
            <Label htmlFor="password">Пароль</Label>
            <Input
              id="password"
              type="password"
              autoFocus
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={!!error}
            />
            {error && <p className="text-sm text-destructive">{error}</p>}
          </CardContent>
          <CardFooter className="pt-6">
            <Button type="submit" className="w-full" disabled={busy || !password}>
              {busy && <Loader2 className="animate-spin" />} Войти
            </Button>
          </CardFooter>
        </form>
      </Card>
    </Shell>
  )
}

function Downloader({ onLogout }: { onLogout: () => void }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [error, setError] = useState<string | null>(null)

  const handleError = useCallback(
    (err: unknown) => {
      if (err instanceof ApiError && err.status === 401) onLogout()
      else setError(err instanceof Error ? err.message : String(err))
    },
    [onLogout]
  )

  const refresh = useCallback(() => api.jobs().then(setJobs, handleError), [handleError])

  useEffect(() => {
    refresh()
  }, [refresh])

  const anyActive = jobs.some((j) => isActive(j.status))
  useEffect(() => {
    if (!anyActive) return
    const timer = setInterval(refresh, 1000)
    return () => clearInterval(timer)
  }, [anyActive, refresh])

  async function logout() {
    await api.logout().catch(() => {})
    onLogout()
  }

  async function remove(id: string) {
    await api.remove(id).then(refresh, handleError)
  }

  return (
    <Shell>
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">TS Downloader</h1>
          <p className="text-sm text-muted-foreground">Сегменты .ts или плейлист .m3u8 → один MP4</p>
        </div>
        <Button variant="ghost" size="sm" onClick={logout}>
          <LogOut /> Выйти
        </Button>
      </header>

      <NewJobForm
        onCreated={() => {
          setError(null)
          refresh()
        }}
        onError={handleError}
      />

      {error && (
        <Alert variant="destructive">
          <AlertCircle />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {jobs.map((job) => (
        <JobCard key={job.id} job={job} onRemove={() => remove(job.id)} />
      ))}
    </Shell>
  )
}

function NewJobForm({ onCreated, onError }: { onCreated: () => void; onError: (err: unknown) => void }) {
  const [url, setUrl] = useState("")
  const [filename, setFilename] = useState("video.mp4")
  const [headers, setHeaders] = useState("")
  const [workers, setWorkers] = useState(8)
  const [variant, setVariant] = useState("")
  const [advanced, setAdvanced] = useState(false)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await api.create({ url, filename, headers, workers, variant: variant ? Number(variant) : null })
      setUrl("")
      onCreated()
    } catch (err) {
      onError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Новая загрузка</CardTitle>
        <CardDescription>
          Ссылка на любой сегмент (например, …/seg-2-v1-a1.ts) или на плейлист .m3u8. Только для видео, на которое у
          вас есть права.
        </CardDescription>
      </CardHeader>
      <form onSubmit={submit}>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="url">URL</Label>
            <Input
              id="url"
              type="url"
              required
              placeholder="https://host/path/seg-1.ts или https://host/path/index.m3u8"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              className="font-mono"
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="filename">Имя файла</Label>
            <Input id="filename" value={filename} onChange={(e) => setFilename(e.target.value)} />
          </div>

          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="w-fit px-0 text-muted-foreground hover:bg-transparent has-[>svg]:px-0"
            onClick={() => setAdvanced(!advanced)}
            aria-expanded={advanced}
          >
            <ChevronDown className={advanced ? "rotate-180 transition-transform" : "transition-transform"} />
            Дополнительно
          </Button>

          {advanced && (
            <div className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <Label htmlFor="headers">HTTP-заголовки</Label>
                <Textarea
                  id="headers"
                  placeholder={"Referer: https://site/\nCookie: …"}
                  value={headers}
                  onChange={(e) => setHeaders(e.target.value)}
                  className="font-mono"
                />
                <p className="text-xs text-muted-foreground">По одному на строку, в виде «Имя: значение».</p>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="flex flex-col gap-2">
                  <Label htmlFor="workers">Потоков</Label>
                  <Input
                    id="workers"
                    type="number"
                    min={1}
                    max={32}
                    value={workers}
                    onChange={(e) => setWorkers(Number(e.target.value) || 1)}
                  />
                </div>
                <div className="flex flex-col gap-2">
                  <Label htmlFor="variant">Вариант (m3u8)</Label>
                  <Input
                    id="variant"
                    type="number"
                    min={1}
                    placeholder="лучший"
                    value={variant}
                    onChange={(e) => setVariant(e.target.value)}
                  />
                </div>
              </div>
            </div>
          )}
        </CardContent>
        <CardFooter className="pt-6">
          <Button type="submit" disabled={busy || !url}>
            {busy ? <Loader2 className="animate-spin" /> : <Download />} Скачать
          </Button>
        </CardFooter>
      </form>
    </Card>
  )
}

const STATUS: Record<JobStatus, { label: string; variant: "default" | "secondary" | "destructive" | "outline" }> = {
  queued: { label: "В очереди", variant: "secondary" },
  downloading: { label: "Скачивается", variant: "default" },
  joining: { label: "Склеивается", variant: "default" },
  done: { label: "Готово", variant: "outline" },
  error: { label: "Ошибка", variant: "destructive" },
  cancelled: { label: "Отменено", variant: "secondary" },
}

function isActive(status: JobStatus) {
  return status === "queued" || status === "downloading" || status === "joining"
}

function JobCard({ job, onRemove }: { job: Job; onRemove: () => void }) {
  const status = STATUS[job.status]
  const active = isActive(job.status)
  const percent =
    job.status === "done" || job.status === "joining" ? 100 : job.total ? (job.done / job.total) * 100 : undefined

  return (
    <Card className="gap-4">
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle className="truncate">{job.filename}</CardTitle>
          <Badge variant={status.variant}>
            {active && <Loader2 className="animate-spin" />}
            {status.label}
          </Badge>
        </div>
        <CardDescription className="truncate font-mono text-xs" title={job.url}>
          {job.url}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {(active || job.status === "done") && (
          <>
            {percent === undefined ? (
              <div className="h-2 w-full overflow-hidden rounded-full bg-primary/20">
                <div className="h-full w-1/3 animate-pulse rounded-full bg-primary" />
              </div>
            ) : (
              <Progress value={percent} />
            )}
            <p className="text-sm text-muted-foreground tabular-nums">
              {job.status === "done"
                ? `${job.done} сегментов · ${formatSize(job.size)}`
                : job.status === "joining"
                  ? `Склеиваю ${job.done} сегментов…`
                  : job.total
                    ? `${job.done} из ${job.total} сегментов`
                    : `${job.done} сегментов`}
            </p>
          </>
        )}
        {job.error && (
          <Alert variant="destructive">
            <AlertCircle />
            <AlertDescription>{job.error}</AlertDescription>
          </Alert>
        )}
        {job.log.length > 0 && (
          <details className="text-sm">
            <summary className="cursor-pointer text-muted-foreground">Журнал</summary>
            <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-muted p-3 font-mono text-xs whitespace-pre-wrap">
              {job.log.join("\n")}
            </pre>
          </details>
        )}
      </CardContent>
      <CardFooter className="gap-2">
        {job.status === "done" && (
          <Button asChild>
            <a href={api.fileUrl(job.id)} download={job.filename}>
              <Download /> Скачать файл
            </a>
          </Button>
        )}
        <Button variant="outline" onClick={onRemove}>
          {active ? (
            <>
              <X /> Отменить
            </>
          ) : (
            <>
              <Trash2 /> Удалить
            </>
          )}
        </Button>
        {job.expires && job.status === "done" && (
          <span className="ml-auto text-xs text-muted-foreground">
            Удалится в {new Date(job.expires * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </span>
        )}
      </CardFooter>
    </Card>
  )
}

function formatSize(bytes: number | null) {
  if (!bytes) return "0 Б"
  const units = ["Б", "КБ", "МБ", "ГБ"]
  let i = 0
  let n = bytes
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i++
  }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`
}
