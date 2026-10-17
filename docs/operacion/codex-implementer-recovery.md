# Recuperación del implementer Codex

Esta guía describe la recuperación soportada para un run cuyo implementer
congelado es Codex. El ledger central y los artefactos protegidos son la
autoridad. La recuperación conserva la sesión implementer autenticada, el
comando/modelo/effort/timeout congelados, los bytes del prompt y el trabajo
parcial. No crea otro reviewer B, no cambia de provider y no repite la admisión
del worktree.

La instalación del paquete, el timer, los modelos reales y cualquier cambio de
WSL son acciones separadas del operador. Los tests automatizados usan CLIs falsos
y no demuestran aceptación live.

## Diagnóstico antes de actuar

Consulta primero el checkpoint durable:

```bash
ai_dev_loop scheduler status <run-id> --output json
ai_dev_loop scheduler history <run-id> --order newest --limit 50 --output json
ai_dev_loop scheduler timeline <run-id> --order newest --limit 50 --output json
```

Para una hoja de secuencia consulta además:

```bash
ai_dev_loop scheduler sequence status <sequence-id> --output json
ai_dev_loop integration sequence phase-runs <sequence-id> --ordinal <n> --output json
```

Los resúmenes no muestran IDs completos, prompts, patches ni stdout/stderr. Si
hace falta inspeccionar output sensible de un intento, usa la lectura explícita
y autenticada `integration run output`; no copies ese contenido a un informe
público.

| Evidencia durable | Acción soportada | Resultado esperado |
| --- | --- | --- |
| Timeout Codex confirmado, proceso terminado, sesión ligada y espera activa o reintentos automáticos agotados | `scheduler implementer-retry <run-id>` | Continúa el mismo turno en el mismo run y sesión. |
| `waiting_codex_capacity` con rol `implementer` | `scheduler tick` | `EXHAUSTED`/`UNAVAILABLE` conserva la espera; solo `AVAILABLE` programa la continuación exacta. |
| `blocked` por `codex_implementer_failure`, terminación y evidencia autenticadas | `scheduler implementer-retry <run-id> --force` | Publica o reutiliza un sucesor; el origen queda inmutable. |
| Identidad conflictiva/ausente, evidencia corrupta o terminación incierta | Ningún retry | Conserva el hold requerido; diagnostica o aborta explícitamente si el estado lo permite. |
| Decisión de abandonar el run o la secuencia | `scheduler abort` o `scheduler sequence abort` | Cancela de forma durable sin borrar cambios ni artefactos. |

No clasifiques elegibilidad desde `last_error` ni desde texto libre. Usa el
estado, la acción segura y los eventos autenticados que proyecta el producto.

## Timeout confirmado

Un timeout elegible programa hasta tres reintentos automáticos por turno con 30
minutos de separación. El contador acompaña al turno aunque haya espera de
capacidad o un sucesor `--force`; solo un turno inicial/corrección genuinamente
nuevo lo reinicia. Un retry operativo no consume una review completada.

```bash
ai_dev_loop scheduler implementer-retry <run-id> --output json
ai_dev_loop scheduler tick --output json
```

El comando manual adelanta una espera programada sin consumir esa autorización
automática, o permite continuar después de agotar las tres. La CLI persiste la
autorización; el proceso se lanza en un tick posterior. Repetir la autorización
antes del lanzamiento no crea otro intento.

Rechaza el retry si falta identidad autenticada, el intento anterior podría
seguir activo, hay abort pendiente o falló la integridad. No limpies el repo, no
liberes reservas a mano y no edites SQLite para forzar progreso.

## Espera de capacidad

La capacidad no tiene bypass manual. Una cuota estructurada/provider o una sonda
post-fallo autenticada puede llevar el run a `waiting_codex_capacity`. Comprueba
en status el rol `implementer`, el turno `initial` o `correction` y la hoja/ordinal
actuales cuando aplica.

```bash
ai_dev_loop scheduler status <run-id> --output json
ai_dev_loop scheduler tick --output json
```

`EXHAUSTED` y `UNAVAILABLE` conservan sesión, prompt, trabajo parcial, reserva y
presupuesto. Solo `AVAILABLE` autoriza un intento fenced del mismo rol. No uses
`scheduler review retry` para una espera implementer y no agotes cuota
intencionalmente para probar esta ruta.

## Fallo genérico y sucesor `--force`

Un fallo `codex_implementer_failure` es elegible únicamente si el intento terminó,
la sesión quedó ligada y la invocación, resultado y artefactos se autentican. No
confundas un timeout, una espera de capacidad, un abort o una terminación incierta
con este caso.

```bash
ai_dev_loop scheduler implementer-retry <run-id> --force --output json
ai_dev_loop scheduler tick --output json
```

La publicación guarda intent y record inmutables antes del dispatch. Un estado
`pending` no ejecuta; los ticks reconcilian hasta `ready`. Repetir `--force` sobre
el mismo origen devuelve el mismo sucesor. El origen y sus artefactos no se
reescriben. Una corrección conserva reviewer B, fix crudo, patch revisado, reviews
completadas y techo efectivo, incluidas extensiones explícitas.

En secuencia, solo la hoja vigente puede adoptar el sucesor. Conserva el mismo
`sequence_id`, ordinal y entrada congelada, y lineage registra
`implementer_retry`. Una hoja obsoleta sin relación existente se rechaza. Tras
aceptación, la secuencia usa el checkpoint local ordinario para una fase no final;
la fase final queda staged. El recovery no autoriza otro commit, push, PR o merge.

## Abort y reinicio

Para abandonar un run standalone:

```bash
ai_dev_loop scheduler abort <run-id> --output json
```

Para abandonar una secuencia y cancelar fases futuras:

```bash
ai_dev_loop scheduler sequence abort <sequence-id> --output json
```

Abort persiste primero, conserva cambios parciales/artefactos y detiene solo la
unidad de intento owned exacta. No restaura el index ni hace reset, clean, stash o
commit. Si la terminación o un CAS de checkpoint sigue incierto, el hold y la
reserva permanecen hasta reconciliar el outcome. Tras reiniciar el proceso del
scheduler, usa status y ticks normales; no existe `scheduler resume`.

No apagues WSL mientras haya un intento activo. Si necesitas validar reinicio del
worker, espera un checkpoint sin proceso activo y sigue la guía del
[timer systemd en WSL](timer-systemd-wsl.md).

## Checklist de aceptación live del operador

Esta lista se ejecuta únicamente con autorización separada. Al publicarse esta
guía todos sus escenarios live están **pendientes**. Marca cada uno con fecha,
versión instalada y evidencia; no conviertas un test con fakes en resultado live.

### Preparación

- [ ] Obtener autorización explícita para modelos reales, instalación local y,
  si aplica, cambios de timer/linger.
- [ ] Crear un repositorio Git desechable y limpio fuera del checkout de
  `ai_dev_loop`; no usar un repositorio con trabajo valioso.
- [ ] Confirmar `git status --short`, branch y HEAD iniciales.
- [ ] Validar build/gates del checkout aceptado y después actualizar el paquete
  local de forma explícita con `uv tool install --force .`.
- [ ] Ejecutar `ai_dev_loop --version` y
  `ai_dev_loop scheduler timer validate --output json`. Si el operador decidió
  refrescar las unidades, usar `scheduler timer install` y `timer status` según
  la [guía del timer](timer-systemd-wsl.md); no cambiar linger implícitamente.
- [ ] Preparar plan/prompt inocuos dentro del repo de prueba y elegir de forma
  explícita modelo/effort implementer y reviewer. No usar defaults heredados ni
  `--last`.

Ejemplo de submit standalone (sustituye rutas y modelos por los aprobados):

```bash
ai_dev_loop scheduler submit \
  --short-name "Recovery smoke con Codex" \
  --repo-path /ruta/al/repo-desechable \
  --plan-path docs/plans/recovery-smoke.md \
  --prompt-source-path docs/plans/prompt_recovery-smoke.txt \
  --implementer-provider codex \
  --implementer-model gpt-5.6-sol \
  --implementer-reasoning-effort xhigh \
  --implementer-timeout-minutes 1 \
  --codex-review-model gpt-6.1-sol \
  --codex-review-reasoning-effort high \
  --output json < docs/plans/prompt_recovery-smoke.txt
```

### Escenarios

- [ ] Iniciar con `scheduler start`, avanzar con ticks y comprobar que las
  identidades implementer/reviewer son distintas y que modelos/efforts coinciden
  con los valores explícitos. Registrar solo prefijos/redacciones públicas.
- [ ] En un timeout controlado del repo desechable, verificar espera, contador y
  reserva; ejecutar `scheduler implementer-retry`, reiniciar el proceso del
  scheduler en un checkpoint sin proceso activo y confirmar la misma sesión y
  prompt tras el siguiente tick.
- [ ] Si la capacidad se agota **de forma natural**, verificar que el rol
  implementer permanece esperando tras `EXHAUSTED`/`UNAVAILABLE` y continúa solo
  tras `AVAILABLE`. Si no ocurre naturalmente, dejar este punto pendiente; no
  consumir cuota para fabricarlo.
- [ ] Validar `--force` solo cuando exista un fallo genérico controlado con sesión
  ya autenticada y terminación resuelta. El producto no incluye un comando de
  fault injection: no corromper el ledger/artefactos ni matar procesos por nombre
  para fabricar elegibilidad. Si no puede producirse de forma segura y
  reproducible, dejar el escenario pendiente.
- [ ] Repetir `--force` y comprobar que devuelve el mismo sucesor. Confirmar origen
  inmutable, misma sesión/settings/prompt, reviewer independiente y presupuesto
  de reviews conservado.
- [ ] Ejecutar una secuencia mixta v2 equivalente. Confirmar adopción en el mismo
  ordinal, un único checkpoint por fase no final aceptada, siguiente provider
  congelado y fase final staged sin commit.
- [ ] En otro run desechable, abortar durante espera de capacidad o antes del
  dispatch recuperado. Reiniciar y comprobar que no revive trabajo ni materializa
  fases futuras, y que cambios/artefactos parciales permanecen.

### Cierre y evidencia

- [ ] Capturar status/history/timeline finales, lineage de secuencia, número de
  checkpoints y `git status --short`; no capturar prompts, patches, JSONL, auth o
  IDs completos en el informe.
- [ ] Confirmar que standalone y fase final quedan staged, sin push/PR/merge, y
  que no quedan reservas/attempts activos después de una terminación resuelta.
- [ ] Marcar separadamente `passed`, `failed`, `blocked` o `pending` para timeout,
  capacidad, fallo genérico, restart, abort, corrección y secuencia mixta.
- [ ] Si el timer se habilitó solo para la prueba, aplicar la decisión explícita
  del operador sobre conservarlo o ejecutar `scheduler timer disable`; esto no
  aborta runs ni elimina estado.

## Límites y compatibilidad

- Los contextos históricos sin metadata de implementer siguen siendo Cursor.
  `scheduler cursor-retry` permanece compatible y Cursor-only; el comando
  compartido delega a esa política cuando el provider congelado es Cursor.
- Manifest v1 permanece Cursor-only. Manifest v2 congela provider/settings por
  fase; no hay switching dentro de un run.
- No se recuperan bootstrap sin identidad, evidencia corrupta, hojas obsoletas,
  abortos ni outcomes inciertos mediante `--force`.
- No se implementan fallback de provider/modelo, billing/quota management,
  reparación manual del ledger, instalación automática ni comandos legacy
  `resume`/`recover`.
- La sonda de capacidad sigue siendo model-agnostic y conservadora.
- Los tests automatizados no instalan paquete/timer, no controlan runs live y no
  invocan modelos reales.
