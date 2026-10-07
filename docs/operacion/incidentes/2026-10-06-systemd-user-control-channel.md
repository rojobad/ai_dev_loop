# INC-2026-10-06-001: pérdida del canal de control de systemd de usuario

## Estado y alcance

- Fecha: **2026-10-06**. Todas las horas de esta entrada usan
  **America/La_Paz (UTC−04:00)**.
- Estado operativo: **recuperado a las 16:33**; el scheduler recogió el
  resultado existente y lanzó el primer intento de revisión de Codex.
- Causa inicial: **no determinada**.
- Decisión del operador: registrar el incidente y observar recurrencias.
  Esta entrada no incorpora safeguards, parches ni cambios de política.

## Contexto

El incidente ocurrió durante la implementación de Phase 8.9 de
`ai_dev_loop_hub`, encargada del ciclo de vida del servicio `systemd --user`
del Bridge. El scheduler ejecutaba una secuencia de diez subfases desde el
worktree `ai_dev_loop_hub-phase8-execution`.

| Referencia | Valor |
| --- | --- |
| Secuencia | `ai-dev-loop-hub-seq-0c863d6b067f` |
| Run de Phase 8.9 | `ai-dev-loop-hub-6cc700e831da` |
| Intento Cursor afectado | `att-d0b8737693807d718f9fbfffaadd87a9` |
| Unidad del intento | `ai-dev-loop-attempt-att-d0b8737693807d718f9fbfffaadd87a9.service` |
| Entorno | WSL, usuario local UID 1000, `systemd` como PID 1, `Linger=yes` |
| Versiones observadas | systemd `249.11-0ubuntu3.22`; D-Bus `1.12.20` |

## Síntomas e impacto

Cursor terminó su implementación con resultado `success` y código de salida
**0**, pero el scheduler conservaba `cursor_ready` y el intento figuraba como
`active`. La primera revisión de Codex no comenzaba y Phase 8.10 no podía
materializarse hasta aceptar 8.9.

El timer continuaba ejecutando ticks. El journal repetía:

```text
attempt_observation_unavailable (att-d0b8737693807d718f9fbfffaadd87a9)
```

`systemctl --user` fallaba con `Failed to connect to bus: No such file or
directory`. Faltaba `/run/user/1000/bus`; el socket
`/run/user/1000/systemd/private` existía, pero la conexión directa devolvía
`Connection refused`. El gestor de usuario seguía vivo.

Se conservaron el resultado, la respuesta final y el transcript de Cursor.
El retraso entre la escritura del resultado a las 13:49 y su conciliación a
las 16:33 fue de aproximadamente **2 horas y 43 minutos**. No se observó pérdida
de esos artefactos ni fue necesario repetir la implementación.

## Cronología verificada

| Hora | Hecho |
| --- | --- |
| 13:07:08 | El scheduler solicitó el lanzamiento del intento Cursor afectado. |
| 13:13:39–13:14:28 | El transcript registra diagnósticos del agente sobre el gestor real: bus de usuario ausente y conexión al socket privado rechazada. |
| 13:17:56–13:19:13 | El agente envió `SIGUSR1` y `SIGHUP` al gestor, movió temporalmente el socket privado a `/tmp/p89-systemd-private.bak` y lo restauró al no regenerarse. El problema persistió. |
| 13:18:22 | El journal consultado por el agente ya mostraba `attempt_observation_unavailable`. |
| 13:29:45–13:29:48 | Cursor tuvo una desconexión breve y se reconectó automáticamente. |
| 13:49:54–13:49:55 | Cursor produjo su respuesta final y el runner escribió el resultado exitoso. |
| 16:23–16:25 | La inspección del operador confirmó que el proceso Cursor había terminado, sus artefactos indicaban éxito y el scheduler seguía sin poder observar la unidad. |
| 16:31:52 | Tras autorización de recuperación, el gestor comenzó a activar un bus D-Bus de usuario mediante unidades temporales. |
| Antes de 16:33:01 | Se resolvió un bloqueo del helper de arranque del socket; `systemctl --user` volvió a responder. |
| 16:33:01 | El timer registró `attempt_completed` y `cursor_turn_completed` para el intento original. |
| 16:33:35 | Un tick lanzó el primer intento Codex de 8.9. El run pasó a `awaiting_codex_review`. |

La hora de `completed_at` en la timeline del scheduler corresponde aquí a la
conciliación tardía. El timestamp de escritura y el transcript muestran que
Cursor ya había terminado a las 13:49; no estuvo ejecutando hasta las 16:33.

## Diagnóstico y límites de la evidencia

La condición inmediata está confirmada: el scheduler no podía observar la
unidad del intento mediante `systemctl --user`, aunque existía un resultado
exitoso en disco. En el código inspeccionado,
`SystemdUserBackend.observe()` devuelve `UNAVAILABLE` si esa consulta falla o
vence su timeout. `_reconcile_existing_attempt()` devuelve entonces
`attempt_observation_unavailable` antes de procesar el resultado del runner.

El problema del bus y del socket privado se observó **antes** de las señales y
del movimiento del socket efectuados por Cursor. Estas intervenciones constan
en el transcript, pero no prueban quién o qué produjo la pérdida inicial del
canal de control. Tampoco se estableció si las verificaciones anteriores del
agente influyeron en ella.

Las unidades estándar `dbus.service` y `dbus.socket` de usuario no estaban
presentes en las rutas inspeccionadas durante la recuperación. Esto permitió
usar un bus de usuario como vía alternativa; no demuestra por sí solo la causa
de la avería del socket privado.

La desconexión de Cursor a las 13:29 se recuperó y no impidió que terminara.
Los timeouts de routing de Codex observados previamente en Phase 8.8 pertenecen
a otro evento; no se estableció una relación causal con este incidente.

## Recuperación realizada

La recuperación fue una intervención operativa autorizada. Esta descripción
registra lo ejecutado; no es una receta de reparación automática para otros
equipos o incidentes.

1. Se verificaron la identidad del gestor de usuario, la ausencia del bus y
   la finalización de Cursor. Se registraron hashes SHA-256 del resultado,
   transcript y respuesta final antes de intervenir.
2. Se crearon `dbus.service` y `dbus.socket` bajo
   `/run/user/1000/systemd/user/`, usando las unidades de usuario de D-Bus 1.12
   como base y los ejecutables ya instalados. `systemd-analyze --user verify`
   devolvió código 0.
3. Se enviaron `SIGHUP` y `SIGUSR1` al gestor identificado para cargar las
   unidades y activar/reconectar el bus, conservando sus unidades existentes.
4. El `ExecStartPost` estándar del socket invocaba
   `systemctl --user set-environment`. En este arranque de recuperación quedó
   esperando el canal que se estaba recuperando. Se retiró ese helper de la
   unidad temporal y se terminó únicamente ese proceso de recuperación,
   después de comprobar su identidad y pertenencia a `dbus.socket`.
5. `dbus.service` y `dbus.socket` quedaron `active/running` y
   `systemctl --user` volvió a responder. Se hizo `daemon-reload` para cargar
   el ajuste de la unidad temporal.
6. El timer recogió el resultado existente. Un
   `ai_dev_loop scheduler tick --output json` lanzó la revisión de Codex
   dentro del mismo run y de la misma secuencia.
7. Se verificó que los tres artefactos originales seguían coincidiendo con
   sus hashes. El timer quedó `active/waiting`.

El gestor de usuario no se reinició ni se terminó; tampoco se repitió el turno
Cursor, se creó un run de reemplazo o se editó el ledger manualmente.
Las unidades añadidas son **temporales del runtime de WSL**, no una instalación
persistente del bus. La recuperación no acredita el resultado futuro del
review ni la aceptación de Phase 8.9.

Fuentes de las unidades usadas como base:
[dbus.service.in](https://github.com/d-bus/dbus/blob/dbus-1.12/bus/systemd-user/dbus.service.in)
y [dbus.socket.in](https://github.com/d-bus/dbus/blob/dbus-1.12/bus/systemd-user/dbus.socket.in).

## Referencias de evidencia

Raíz del run en el equipo afectado, relativa al state home nativo:

```text
~/.local/state/ai_dev_loop/artifacts/runs/
  42284ae2a1c3a9826a33cbee6f51580f7dd57122fe3ae222181c7984d3f52f58/
```

Artefactos relativos a esa raíz:

- `cursor/iterations/01/att-d0b8737693807d718f9fbfffaadd87a9/events.jsonl`:
  245 llamadas a herramientas iniciadas y completadas; un resultado final
  `success`. Líneas 2748–2964: diagnóstico inicial; 4003–4298: intervenciones
  sobre el gestor; 10759: resultado final.
- `cursor/iterations/01/att-d0b8737693807d718f9fbfffaadd87a9/metadata.json`:
  `exit_code: 0`, `timed_out: false`, `parse_ok: true`,
  `has_completion_signal: true`, sin errores estructurados.
- `cursor/iterations/01/att-d0b8737693807d718f9fbfffaadd87a9/final.txt`:
  respuesta final del implementador.
- `attempts/att-d0b8737693807d718f9fbfffaadd87a9/result.json`:
  `exit_code: 0`, `termination_class: success`.
- `locks/active-process.json`: proceso Cursor marcado como terminado con
  `cleared_reason: completed`; el PID registrado ya no existía al inspeccionar.

El journal de `ai-dev-loop-scheduler-tick.service` registró tanto los ticks sin
observación como la conciliación posterior. La timeline del run confirmó el
intento Cursor completado y el intento Codex activo a las 16:33.

La intervención dejó un registro local en
`/tmp/ai-dev-loop-bus-recovery-lvpks91_/`, con
`preserved-artifacts.json` y `recovery.json`. Esta ruta es temporal y puede
desaparecer; no es un archivo durable del scheduler ni reemplaza esta entrada.

No se incorporan al repositorio los transcripts completos, prompts, respuestas
completas de agentes, credenciales ni IDs de chats.

## Seguimiento

La causa inicial sigue abierta. No se implementó un safeguard ni un parche de
`ai_dev_loop` como parte de este incidente. Si se repite, una nueva entrada debe
relacionar los casos y conservar evidencia del canal de control y de los ticks
antes de cualquier recuperación, para que el operador pueda decidir medidas.
