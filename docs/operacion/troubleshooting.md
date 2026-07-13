# Troubleshooting

## MkDocs: `ERR_SSL_PROTOCOL_ERROR` en `localhost:8000`

Sintoma:

- el navegador muestra `ERR_SSL_PROTOCOL_ERROR` o "This site can't provide a secure connection";
- `uv run mkdocs serve` registra lineas ilegibles con `code 400`.

Causa:

- `mkdocs serve` expone HTTP plano en `http://127.0.0.1:8000/`;
- algunos navegadores, incluido Opera GX, fuerzan HTTPS cuando escribes `localhost:8000` sin esquema.

Accion:

1. Abre `http://127.0.0.1:8000/` (con `http://`, no `https://`).
2. O ejecuta `uv run mkdocs serve -o` para abrir la URL correcta automaticamente.
3. Si el navegador sigue forzando HTTPS, desactiva "Always use secure connections" / HTTPS-First para localhost o borra el estado HSTS de `localhost`.

Los warnings `code 400` con texto binario desaparecen cuando el navegador deja de enviar handshakes TLS al servidor HTTP.

## `prepare` rechaza el worktree

Causas comunes:

- staged changes preexistentes;
- archivos tracked modificados no relacionados;
- untracked files no permitidos;
- prompt source tracked o no ignorado;
- plan o config fuera del repo;
- symlink que escapa de la raiz del repo.

Acciones:

```bash
git status --short
git diff --cached --name-only
```

Limpia o mueve trabajo no relacionado antes de preparar el run. No uses `git reset` o `git clean` sin revisar manualmente.

## `start` falla por drift

`start` revalida el contrato de `prepare`. Falla si cambiaron:

- branch;
- HEAD;
- plan;
- prompt;
- baseline del worktree;
- repo path.

Accion: si el plan o prompt cambiaron legitimamente, ejecuta `prepare` de nuevo.

## Cursor auth/model probe falla

Verifica:

```bash
agent status --format json
agent models
```

El parser real acepta salidas como:

```text
composer-2.5-fast - Composer 2.5 Fast
```

Configura `cursor.model` con el identificador exacto, por ejemplo `composer-2.5-fast`.

## Codex review model no soportado

Sintoma comun:

- `start` o `resume` clasifica el modelo requerido como incompatible;
- `codex exec resume` indica que el modelo requiere una version mas nueva de Codex CLI.

Accion:

1. Consulta `codex --version` y `codex debug models`.
2. En TTY, acepta el prompt de update solo si quieres actualizar esa CLI WSL; la respuesta por defecto es no.
3. En non-TTY, usa `--update-tools` para autorizar el updater o ejecuta `codex update` manualmente.
4. El update puede agregar soporte, pero no garantiza que exista una version compatible. `ai_dev_loop` vuelve a probar version y catalogo.
5. `--allow-incompatible-tools` permite continuar bajo tu responsabilidad; no corrige la incompatibilidad.

Los updates no modifican Codex Desktop ni Cursor Desktop en Windows.

Para reasoning:

- Omite `review_reasoning_effort` para capturarlo de la sesion durante `prepare`.
- Si fijas un valor, usa solo `minimal`, `low`, `medium`, `high`, `xhigh`, `max` o `ultra`.

## Sesion grabada con un modelo y resume usa otro

En runs nuevos, esto no debe depender del default WSL: `prepare` captura modelo/reasoning de la sesion y cada review los pasa explicitamente.

Acciones:

1. Ejecuta `ai_dev_loop inspect <run-id>` y compara runtime de sesion, runtime efectivo y procedencia.
2. Si el run es historico de Fase 9 con ambos valores `null` y sin procedencia, su camino legacy omite overrides. Prepara un run nuevo; no interpretes esos `null` como session-derived.
3. Si existe un override explicito, revisa YAML/flags y vuelve a preparar para cambiarlo.

## `Failed to run pre-sampling compact`

Reanudar una sesion GPT-5.5 con GPT-5.6, o viceversa, puede requerir compactacion previa por diferencias entre familias. En versiones validadas se observo `pre-sampling compact`; no es un comportamiento universal.

Acciones:

1. Evita cambiar de familia dentro del mismo run; omite el override para usar el modelo capturado de la sesion.
2. Verifica que la Codex CLI WSL soporte ese modelo.
3. Si cambias YAML o flags, prepara un run nuevo.

Un mismatch de familia genera una advertencia operativa, no bloquea por si solo.

## Compatibilidad desconocida u offline

Si `agent models` o `codex debug models` no puede confirmar soporte, `ai_dev_loop` distingue `unknown` de incompatibilidad confirmada. Un resultado `unknown` no dispara el updater ni bloquea como una incompatibilidad confirmada, y no afirma que haya un update disponible.

Revisa conectividad/autenticacion y ejecuta los probes manualmente. `--update-tools` solo actua sobre incompatibilidades detectadas; `--allow-incompatible-tools` autoriza continuar cuando la incompatibilidad si fue confirmada.

## Falta SessionStart context

Verifica integracion:

```bash
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

Luego:

- abre `/hooks`;
- confia el hook;
- reinicia o reanuda la sesion;
- confirma que la sesion actual tiene el ID exacto.

Para Desktop, recuerda que la confianza se hace en Codex Desktop, no en WSL.

## WSL distro ambiguo

Pasa el distro explicitamente:

```bash
ai_dev_loop integrations status \
  --target codex-desktop-wsl \
  --wsl-distro Ubuntu-22.04
```

Tambien puedes exportar:

```bash
export AI_DEV_LOOP_WSL_DISTRO=Ubuntu-22.04
```

## Windows Codex home no detectada

Pasa la home `.codex`, no el perfil de usuario:

```bash
ai_dev_loop integrations status \
  --target codex-desktop-wsl \
  --windows-codex-home "/mnt/c/Users/<usuario>/.codex"
```

La ruta debe ser absoluta y terminar en `.codex`.

## Puente `from-desktop` mal apuntado

Verifica:

```bash
ai_dev_loop integrations sessions status
```

Si reporta que el target no coincide, remueve y reinstala:

```bash
ai_dev_loop integrations sessions remove
ai_dev_loop integrations sessions install
```

`remove` solo borra el symlink `from-desktop`. Si hay un archivo o directorio no symlink en esa ruta, lo rechaza.

## `resume` rechaza drift de staged patch

Durante correcciones, el staged patch actual debe coincidir con el patch registrado por el orquestador. Si alguien modifico el index, `resume` falla.

Acciones:

- inspecciona `git/diffs/NN.patch`;
- revisa el index actual con `git diff --cached`;
- decide manualmente si debes abandonar el run o preparar uno nuevo.

## `recover` rechaza el run o pide dry-run

`resume` sigue rechazando `failed`. Para fallos elegibles:

```bash
ai_dev_loop recover --dry-run <failed-run-id>
```

Si el blocker es `post_cursor_fingerprint_missing` y el status actual coincide con `NN-after-cursor.txt`:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output
```

Si hay otros blockers (`staged_patch_drift`, `untracked_files`, `branch_mismatch`, `cursor_output_fingerprint_drift`, etc.), corrigelos o prepara un run nuevo. No mutes el run origen.

Si dry-run es elegible:

```bash
ai_dev_loop recover <failed-run-id>
ai_dev_loop resume <recovery-run-id> [--update-tools]
```

`recover` nunca actualiza CLIs ni invoca agentes. Si un sucesor tambien fallo, recupera ese sucesor (cadena), no el abuelo.

Para staging recovery, el sucesor `resume` ejecuta `git add -A` y Codex sin re-ejecutar Cursor.

## Cursor alcanzo el limite de uso del modelo

Sintoma:

- el run termina en `failed` durante un turno Cursor;
- `status` o la salida de `start`/`resume` indican limite de uso del modelo configurado;
- existe `git/cursor-output/NN.usage-limit-failure.json` en el run origen (Phase 13), o evidencia historica compatible con adopcion explicita (`stderr.txt` protegido + status after-cursor + ambos flags).

Por que `resume` no reabre el origen:

- `failed` es terminal; `resume` rechaza runs terminales;
- el origen queda inmutable para auditoria; la continuacion requiere un sucesor via `recover`.

Accion:

```bash
ai_dev_loop recover --dry-run <failed-run-id> --cursor-model auto
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

Historico sin fingerprint contemporaneo (pre-Phase 13 o evidencia incompleta):

```bash
ai_dev_loop recover --dry-run <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover <failed-run-id> --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

No edites manualmente el repositorio entre `--dry-run` y el `recover` real; el segundo revalida status y fingerprint y rechaza drift.

Ejemplo operativo documentado (no ejecutar como validacion de implementacion):

```bash
ai_dev_loop recover --dry-run crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output --cursor-model auto
ai_dev_loop recover crypto-sentinel-20260712T205916Z-b2d828 \
  --adopt-current-cursor-output --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

En TTY, `start`/`resume` pueden ofrecer crear el sucesor con el mismo chat y modelo `auto`. En scripts o CI, debes pasar `--cursor-model auto` explicitamente; no hay cambio automatico de modelo.

Trabajo parcial:

- no descartes manualmente cambios unstaged/untracked antes de `recover`; el fingerprint captura el contenido parcial al fallo;
- el envelope de continuacion embebe el prompt exacto previo para que Cursor retome el mismo chat;
- evita editar el worktree salvo que abandones el run y prepares uno nuevo.

Casos que `recover` rechaza:

- drift del fingerprint (`usage_limit_fingerprint_drift`);
- fingerprint ausente o invalido (`usage_limit_fingerprint_missing`, `usage_limit_fingerprint_invalid`);
- fallos Cursor ordinarios (timeout, auth, exit distinto) no clasificados como `cursor_usage_limit`;
- drift de branch/HEAD/plan/prompt, chat ID faltante, o proceso hijo activo.

## Pytest falla con temporales en `/mnt/c`

Usa temporales nativos WSL:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

Para simulaciones DrvFS puntuales, usa `-s` si la captura de pytest falla antes de coleccion.
