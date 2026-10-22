# Desinstalacion y limpieza

## Desinstalar integraciones

Para WSL CLI:

```bash
ai_dev_loop integrations uninstall --target wsl-cli
```

Para Codex Desktop + WSL:

```bash
ai_dev_loop integrations uninstall --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
```

La desinstalacion preserva por defecto:

- hooks no relacionados;
- skills no relacionados;
- run history del scheduler (`engine.sqlite3` y `artifacts/`);
- config, cache, sesiones Codex, hooks y skills instalados;
- puente `from-desktop` del target desktop.

## Remover puente de sesiones

```bash
ai_dev_loop integrations sessions remove
```

Esto elimina solo:

```text
~/.codex/sessions/from-desktop
```

No elimina sesiones de Windows ni archivos rollout.

## Desinstalar el comando

Antes de retirar el ejecutable, deshabilita y detén el timer si estaba activo:

```bash
ai_dev_loop scheduler timer disable --output json
ai_dev_loop scheduler timer status --output json
```

Esto evita que una unidad systemd habilitada intente ejecutar un comando ya
desinstalado. `disable` conserva los archivos de unidad owned; consulta
[Timer del scheduler en WSL](timer-systemd-wsl.md) para el procedimiento
completo y la decisión independiente sobre `linger`.

Si instalaste con `uv tool`:

```bash
uv tool uninstall ai_dev_loop
```

Si instalaste con `pipx`:

```bash
pipx uninstall ai_dev_loop
```

## Cutover: eliminar estado legacy

Tras validar el scheduler central, puedes eliminar **solo** los dos roots legacy
con confirmacion explicita:

```bash
ai_dev_loop scheduler cutover cleanup --confirm delete-legacy-state
```

El comando muestra las rutas exactas antes de borrar y rechaza:

- targets symlink o fuera del state root resuelto;
- trabajo activo del scheduler (lease, attempt o capacity holder);
- tokens de confirmacion incorrectos.

Usa `--dry-run` para validar sin borrar.

Rutas afectadas exclusivamente:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/
$XDG_STATE_HOME/ai_dev_loop/pr-review-v2/
```

Se preservan `engine.sqlite3`, `artifacts/`, config, cache, hooks, skills y
demas paths bajo `$XDG_STATE_HOME/ai_dev_loop`. Este comando no borra
`cursor-sdk/`.

## Conversaciones nativas de Cursor SDK

La política efectiva está en el ledger: limpieza automática habilitada, 168
horas desde la terminación UTC durable de todo el trabajo de esa conversación,
decisión `operator_2026-10-20`. No se infiere de la antigüedad del directorio
ni de un recibo de borrado anterior. No forma parte de `ai_dev_loop.yaml`.

```bash
ai_dev_loop scheduler cursor-store policy
ai_dev_loop scheduler cursor-store policy --automatic disabled
ai_dev_loop scheduler cursor-store cleanup --conversation-key <sha256-owner>
ai_dev_loop scheduler cursor-store cleanup \
  --owner-run-id <run-id> \
  --confirm delete-cursor-sdk-conversation
```

La previsualización y `--dry-run` no borran. Tampoco crean el ledger, migran un
esquema histórico ni cambian permisos; `policy` sin `--automatic` tiene el
mismo límite. Apply manual exige la confirmación
distinta del cutover y un solo owner. Borra solo el root de aplicación
`$XDG_STATE_HOME/ai_dev_loop/cursor-sdk/<sha256-owner>/` si la propiedad
autentica y no queda trabajo vivo, incierto o recuperable. Un `blocked`
histórico tampoco retiene el root cuando un sucesor autenticado ya terminó
ese linaje. La intención queda
antes del borrado; el recibo y el tombstone `cursor-sdk-disposals/<key>.json`
quedan después. El tombstone no sigue enlaces simbólicos. Un apply interrumpido,
o un worker cuya generación del fence ya venció, converge al mismo recibo. Si
el recibo quedó confirmado y faltó el tombstone, el siguiente tick de
mantenimiento lo republica con esos bytes, sin repetir el borrado.

Con la política habilitada, `scheduler tick` aplica esa misma elegibilidad en
lotes acotados cuando han pasado exactamente 168 horas o más desde la última
terminación autenticada, incluida la reconciliación nativa posterior a un
abort. No hay un timer
aparte ni un cambio de `linger`. Deshabilitar la política detiene solo el
borrado automático. La evidencia de terminación ausente, un payload dañado, un
intento vivo o incierto, una reserva, un wait, un presupuesto agotado, un
checkpoint, un handoff, un recovery pendiente o una continuación SDK por resume
sin progreso en `reserved` o `publishing` —incluido su linaje— conservan el root
aunque los archivos sean antiguos. La terminación definitiva de ese linaje
vuelve a iniciar el plazo ordinario de 168 horas.

El ledger, los artefactos, las credenciales, los repositorios, los backups del
operador, Codex y las demás conversaciones permanecen.

## Limpiar estado XDG manualmente

Para config, cache o el ledger del scheduler, sigue siendo una decision manual.
Revisa antes de borrar:

```text
$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3
$XDG_STATE_HOME/ai_dev_loop/artifacts/
$XDG_CONFIG_HOME/ai_dev_loop
$XDG_CACHE_HOME/ai_dev_loop
```

No borres estado si necesitas auditar o continuar runs del scheduler activos.
