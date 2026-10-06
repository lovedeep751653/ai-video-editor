# App API (phone screen ⇄ editor engine)

The editor engine is a small web server. Inside the Android app it runs on the
phone itself at `http://127.0.0.1:<port>/`; on a computer it runs with
`./run.sh`. The phone screen in `app/static/` talks to it only through the
routes below. All bodies are JSON unless noted; errors are
`{"detail": "plain-language message"}` with a 4xx/5xx status. A 401 with
`{"login": true}` means an access code is needed (only when hosted with
`APP_PASSWORD`).

## Health and settings

- `GET /api/health` → `{ok, app_mode, encoder}`
  - `app_mode`: true when running inside the Android app.
  - `encoder`: `"hardware"` or `"software"` (what the last render used, or what will be tried first).
- `GET /api/settings` → `{ai_ready, key_hint, can_text, can_image, can_video, quality, speed}`
  - `quality`: `"720"` | `"1080"` (default `"1080"`). Height of the short side of the finished video.
  - `speed`: `"fast"` (phone's hardware video encoder, default) | `"best"` (slower software encoder, smaller files).
- `POST /api/settings` with any of `{gemini_key, quality, speed}` → same as GET.
  `gemini_key: ""` removes the key. A wrong key returns 400 with a message.

## Sources (the user's videos, photos and music)

- `POST /api/sources` — multipart form, one or more `files` fields (browser mode).
- `POST /api/sources/local` — `{items: [{path, name}]}` (app mode: paths given by the Android picker).

Both return `{sources: [Source]}`, one per file, in order:

```
Source = {id, name, kind: "video"|"image"|"audio", duration, width, height,
          thumb: "/api/sources/<id>/thumb" | null, error: string | null}
```

A source with `error` set could not be opened; show the message and leave it out.

## Projects (one edit and all its versions)

- `POST /api/projects` —
  ```
  {sources: [id, ...], music: id | null,
   mode: "highlight" | "cleanup",
   request: "typed wishes, optional",
   options: {format, length, style, look, transitions, slowmo, title,
             captions, caption_lang, caption_style, caption_pos}}
  ```
  → `{project, job}`. Every option may be left out or `"auto"`.
  - `mode: "highlight"` = short best-moments video (default length auto, 10–60 s; up to 180 s when chosen).
  - `mode: "cleanup"` = keep the whole story in order and remove pauses, silences and bad parts
    (for 5–30 minute videos; output can be as long as the input).
  - Option values: `format` auto|9:16|16:9|1:1|original; `length` auto or seconds;
    `style` auto|smooth|fast; `look` auto|none|cinematic|vivid|warm|cool|vintage|bw|dramatic;
    `transitions` auto|soft|dynamic|cinematic|cuts; `slowmo` auto|on|off;
    `captions` auto|on|off; `caption_lang` auto|en|hi|pa|hinglish;
    `caption_style` one of `/api/caption-styles`; `caption_pos` bottom|middle|top.
- `GET /api/projects` → `{projects: [Summary]}` newest first.
  `Summary = {id, title, mode, created, updated, state: "working"|"ready"|"failed", duration, format, thumb, job}`
- `GET /api/projects/<id>` → `Project`:
  ```
  {id, title, mode, created, updated, state, error, job,          // job = running job id or null
   version, versions: [{n, label, created, duration}],
   video: "/api/projects/<id>/video?v=<n>" | null,
   download: "/api/projects/<id>/download?v=<n>" | null,
   thumb,
   sources_available: bool,      // false after the phone restarted the app: re-editing needs the files again
   plan: {format, width, height, total, look, transition_kind, title,
          music: {name} | null, notes: [string],
          clips: [{index, name, kind, start, duration, speed, out_start, thumb}],
          texts: [{index, text, start, end, position}]},
   chat: [{role: "user" | "assistant", text, time, version}],
   suggestions: [string]}        // short ideas for the next chat message
  ```
- `DELETE /api/projects/<id>`.
- `POST /api/projects/<id>/chat` `{message}` → `{job}`. When the job finishes the
  assistant's answer is in `chat` and, if the video changed, a new version exists.
  Works without a Google AI key for common commands; with a key it understands anything.
- `POST /api/projects/<id>/clips` `{action, clip, to}` → `{job}`. Direct buttons on the
  timeline: `action` = `remove` | `move` (needs `to`) | `slowmo` | `normal_speed` | `zoom`.
- `POST /api/projects/<id>/undo` → `{job}` (goes back one version; instant when the old file is kept).
- `POST /api/projects/<id>/restore` `{version}` → `{job}`.
- `GET /api/projects/<id>/video?v=<n>` → MP4 (supports Range for seeking).
- `GET /api/projects/<id>/download?v=<n>` → MP4 as an attachment.
- `GET /api/projects/<id>/clips/<index>/thumb` → JPEG.

## Jobs (anything that takes time)

- `GET /api/jobs/<id>` →
  `{id, kind, state: "waiting"|"working"|"done"|"failed"|"cancelled", stage, progress (0..1), eta, error, project, result}`
  - `eta`: seconds left, or null when unknown.
  - `kind`: `edit` | `chat` | `image` | `video` | `story`.
- `POST /api/jobs/<id>/cancel` → `{ok}`.

## AI creation (needs a Google AI key)

- `POST /api/create` `{mode: "image"|"video"|"story", prompt, format, source: "pictures"|"clips", count, request}` → `{job}`.
  Result: `{files: [{name, kind, url, source}], project}` where `source` is a source id
  (so the files can go straight into `POST /api/projects`) and `project` is set for `story`.

## Captions

- `GET /api/caption-styles` → `{styles: [{id, name, anim, ...}], langs, default}`
- `GET /api/caption-preview/<style>?lang=en|hi|pa|hinglish` → PNG made by the real renderer.

## Android bridge (`window.Android`, only inside the app)

Calls from the page into the app. Answers come back by calling global functions on the page.

- `Android.pickMedia(kind)` — `kind` = `"visual"` (videos and photos) or `"audio"`.
  Answer: `window.onNativePicked({kind, items: [{path, name, size, mime}]})`,
  or `{kind, cancelled: true}`, or `{kind, error}`. Send `items` to `POST /api/sources/local`.
- `Android.saveToGallery(urlPath, fileName, mime)` — copies the file into the phone's gallery
  (Movies or Pictures / "AI editor open source"). Answer: `window.onNativeSaved({ok, message})`.
- `Android.share(urlPath, fileName, mime)` — opens the phone's share sheet.
- `Android.openExternal(url)` — opens a link in the phone's browser.
- `Android.appVersion()` → string (synchronous).
