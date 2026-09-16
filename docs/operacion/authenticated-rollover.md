# Rollover authenticated (Phase 20.6.5)

El rollover authenticated reemplaza un run `max_iterations_reached` del scheduler con un sucesor que:

1. congela el árbol staged autenticado y el resultado final de revisión en `scheduler rollover prepare`;
2. materializa un worktree gestionado y un reviewer Codex nuevo en `scheduler rollover start`;
3. crea un chat Cursor nuevo solo si la revisión fresh devuelve findings;
4. integra el árbol aceptado en el repositorio objetivo con autorización explícita;
5. marca el run fuente como superseded mediante linaje inmutable, nunca como aceptado.

## Comandos

```bash
ai_dev_loop scheduler rollover prepare <source-run-id> --commit-message "<mensaje>"
ai_dev_loop scheduler rollover start <rollover-id>
ai_dev_loop scheduler rollover status <rollover-id>
ai_dev_loop scheduler rollover abort <rollover-id>
ai_dev_loop scheduler tick
```

`prepare` es de solo lectura respecto al worktree fuente. El cálculo del árbol staged usa un índice y un object store temporales aislados; no muta el índice, HEAD, refs ni los objetos del repositorio fuente.

`start` persiste un intento durable antes de crear el worktree gestionado, reacquiere la reserva del run fuente capped y la retiene durante la integración. El sucesor arranca en `awaiting_codex_review` con un reviewer Codex fresh; no reutiliza la sesión Codex ni el chat Cursor del run fuente.

La integración reutiliza la autoridad Git estrecha de Phase 20.6: un commit local sin firmar y un CAS de rama solo dentro de este rollover explícito.

`abort` durante `integration_pending` o `cleanup_pending` reconcilia primero un posible CAS exitoso en la rama objetivo. Si la integración ya quedó probada, registra el éxito y no revierte el commit.

Los holds de reconciliación de checkpoint solo se liberan tras un CAS verificado o cuando un abort pre-CAS queda probado sin avance de rama; un hold con el mismo intento de integración se reconcilia en reintentos de `scheduler tick` y no se trata como abort ajeno.

## Capacidad Codex

Cuando el App Server devuelve `rateLimitReachedType: null` con ventanas disponibles, la sonda clasifica capacidad como disponible. Un run en `waiting_codex_capacity` puede reanudarse automáticamente en el siguiente `scheduler tick` sin comando manual de retry, conservando el mismo reviewer enlazado.

## Límites de fase

- Sin publicación remota, resolución de conflictos ni `git push`.
- Sin modelos reales en tests automatizados.
- No convierte `max_iterations_reached` ordinario en reintento automático; el rollover requiere `prepare` explícito.
