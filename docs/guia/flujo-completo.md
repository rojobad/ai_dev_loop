# Flujo completo de trabajo

Esta guía describe el recorrido desde una conversación de diseño en un proyecto
target hasta un PR revisado por Codex. `ai_dev_loop` automatiza la ejecución y
las revisiones; no hace merge ni sustituye la última validación humana antes de
integrar en `master`.

## Roles

- **Usuario:** decide el alcance, aprueba el plan, autoriza crear/iniciar el
  ciclo de PR y resuelve los casos inciertos.
- **Codex A:** chat principal y controlador. Conserva la conversación de diseño
  y controla el run.
- **Codex B:** revisor técnico en el flujo A/B. Su sesión exacta se reanuda para
  todas las revisiones; debe permanecer inactiva mientras el worker corre.
- **Cursor:** implementa y corrige en un único chat por run.
- **Worker local:** conserva artefactos fuera del repositorio, controla los
  límites y ejecuta las acciones Git/GitHub autorizadas.

El flujo A/B es el recomendado. También se admite una única sesión Codex, pero
esa sesión debe quedar inactiva antes de iniciar un worker que vaya a reanudarla.

## 1. Preparar el repositorio una vez

El proyecto target debe contar con:

- un `ai_dev_loop.yaml` válido;
- plan y prompt separados, habitualmente bajo `docs/.../plans/`;
- un skill de revisión staged configurado en `codex.review_skill`;
- para el ciclo GitHub, `github.enabled: true`, el skill
  `github.external_review_skill`, `gh` autenticado y Git por SSH funcional;
- las integraciones globales instaladas y el hook `ai_dev_loop` confiado desde
  `/hooks` de la superficie Codex que se use.

No se guardan tokens, sesiones, prompts completos, diffs ni artefactos de los
agentes en el repositorio target. Esos datos viven bajo el estado XDG local.

Comprueba la configuración y, si se usará GitHub, el entorno antes de empezar:

```bash
ai_dev_loop config validate --repo /ruta/al/proyecto
ai_dev_loop github doctor --repo-path /ruta/al/proyecto
```

El `ssh-agent` con la llave cargada debe estar disponible en el entorno que
lanzará el worker. `gh` autentica la API GitHub; el push sigue usando SSH.

## 2. Diseñar el cambio con Codex

Abre un chat Codex en el repositorio target, discute el cambio y refina el
alcance. Antes de implementar, Codex debe inspeccionar las reglas del proyecto,
identificar riesgos y resolver los `OpenQuestions` con el usuario.

Cuando el cambio esté aprobado, pide un plan para Cursor. El resultado debe ser:

```text
<directorio-de-planes>/<plan>.md
<directorio-de-planes>/prompt_<plan>.txt
```

El segundo archivo contiene el prompt exacto de la primera ejecución de Cursor.
No se debe depender de la memoria de la conversación para reconstruirlo.

## 3. Preparar y lanzar el loop local

Después de aprobar plan y prompt, usa `$ai-dev-loop-handoff`. En A/B, A crea un
fork B y B ejecuta `ai_dev_loop prepare` con su sesión como revisor y con la
sesión de A como controlador. B devuelve un `run-id` y un comando similar a:

```bash
ai_dev_loop launch <run-id> --controller-session-id <sesion-A>
```

A ejecuta ese comando. No se usa `start` en B ni se continúa conversando en B
mientras el run está activo.

El worker valida rama, HEAD, estado Git, hashes de plan/prompt, compatibilidad de
herramientas y modelos antes de ejecutar:

```text
Cursor implementa
→ staging controlado
→ Codex B revisa el diff staged
```

### Decisiones de la revisión local

| Resultado | Acción |
| --- | --- |
| No hay hallazgos accionables y tests correctos | El run queda `completed`; los cambios aceptados siguen staged. |
| No hay hallazgos accionables, pero los tests fallaron o están bloqueados y Codex concluye que no corresponde corregirlos | El run queda `completed_with_residual_risk`; se conserva el riesgo documentado. |
| Hay hallazgos accionables | Codex genera el prompt de corrección; Cursor corrige en el mismo chat y se repite staging/revisión hasta el límite configurado. |
| Se alcanza el límite local | El run se detiene con los artefactos y el prompt de corrección preservados. |

El worker no corrige ni interpreta por sí mismo: transporta el prompt exacto que
redactó Codex.

### Interrupciones y recuperación local

Timeouts, límites de Cursor, modelos incompatibles, aborts, cambios manuales en
el worktree/index o fallos de procesos dejan un checkpoint seguro. Según el
estado, se usa `resume` o `recover`; no se deben repetir turnos terminados ni
cambiar la sesión Codex o el chat Cursor registrado. Un fallback de modelo Cursor
se registra explícitamente, en vez de seleccionarse de forma silenciosa.

## 4. Publicar un PR desde un run terminado

Cuando el run local esté `completed` o `completed_with_residual_risk`, crear el
PR sigue siendo una autorización explícita del usuario:

```bash
ai_dev_loop pr-review create <source-run-id>
```

El worker obtiene de Codex el mensaje de commit y el título/descripción del PR,
hace commit sólo del patch staged aceptado, realiza push normal (sin `--force`),
crea o actualiza el PR hacia `master`, publica `@codex review` y empieza a
vigilarlo. Nunca hace merge, retarget, cierre de PR ni force-push.

## 5. Adoptar un PR existente

Para un PR que no nació en el loop local —por ejemplo, un run histórico fallido o
un cambio publicado manualmente— usa el origen `independent_pr`. Requiere un PR
abierto, rama y número explícitos, worktree limpio y HEAD local igual al SHA del
PR:

```bash
ai_dev_loop pr-review prepare \
  --repo-path /ruta/al/proyecto \
  --pr <numero> \
  --branch <rama-del-head> \
  --plan-path <plan> \
  --prompt-source-path <prompt> \
  --codex-session-id <sesion-B> \
  --controller-session-id <sesion-A> \
  < <prompt>
```

`prepare` no publica comentarios, no crea un chat Cursor y no realiza turnos de
agentes. Antes de iniciar, se puede cambiar el modelo de Cursor si todavía no se
ha creado ese chat:

```bash
ai_dev_loop pr-review set-cursor-model <run-id> --cursor-model <modelo>
```

Sólo A inicia el ciclo A/B:

```bash
ai_dev_loop pr-review start <run-id> --controller-session-id <sesion-A>
```

El modelo y la sesión de revisión de Codex permanecen ligados a la sesión exacta
indicada durante `prepare`. Cambiar el modelo Cursor no cambia esa identidad.

## 6. Revisión externa en GitHub

El worker acepta exclusivamente hilos no resueltos que cumplan toda esta
proveniencia:

- autor incluido en `github.reviewer_logins`;
- mismo PR y SHA de cabecera persistido;
- posteriores al marcador idempotente de `@codex review`;
- no procesados antes por ese ciclo.

Si no hay hilos elegibles y
`github.no_findings_completion.enabled: true`, el worker puede completar el ciclo
cuando el reviewer permitido publica un comentario general que cumple el contrato
configurado (prefijo exacto + línea `Reviewed commit:` ligada al
`bound_head_sha`, posterior al trigger). Esa ruta no crea chat Cursor, no
adjudica con Codex, no hace commit/push ni resuelve hilos. La ausencia de hilos,
la reacción `eyes` o su retirada **no** son señales de éxito.

Con `github.acknowledgement.enabled: true`, la reacción configurada (`eyes` por
defecto) sobre el comentario trigger es telemetría best-effort: `status` puede
mostrar acuse observado o un diagnóstico de timeout, pero el worker sigue
esperando hilos o el comentario de finalización. El timeout de acuse no reintenta
el trigger. Mantén **Automatic reviews** de Codex apagado en este flujo para
evitar revisiones duplicadas.

Codex B evalúa todos esos hilos y devuelve una decisión estructurada por hilo.

### Todos son accionables

```text
Codex B adjudica los hilos
→ Cursor corrige en su mismo chat
→ revisión local Codex B
→ commit y push no-force
→ verifica el nuevo SHA del PR
→ resuelve sólo los hilos corregidos
→ solicita otra revisión con @codex review
```

El número máximo de rondas externas se controla mediante
`github.max_external_cycles`.

### Alguno es incierto o no aplica

El worker no envía nada a Cursor ni resuelve esos hilos. Codex publica la
explicación exacta en línea, empezando por `@rojobad`, y el ciclo queda en
`waiting_for_user_attention`.

Después de evaluarlo, el usuario autoriza una continuación únicamente con este
comentario exacto en el PR:

```text
@rojobad /ai-dev-loop continue
```

Entonces se ejecuta:

```bash
ai_dev_loop pr-review continue <run-id>
```

El mismo comando, con un comentario exacto **nuevo**, también cubre un caso
histórico excepcional: un freeze `expected_eligible_thread_ids` heredado de un
ciclo `external_adjudication` anterior que choca con hilos válidos del ciclo ya
publicado (`eligible_thread_set_drift`). La evidencia lineage puede ser directa
en el run actual, o un único salto a un source terminal con recovery
`external_adjudication` cuando el actual es sucesor `reviewing`. La limpieza es
estrictamente lineage-bound: mismo run, PR, SHA, marker, sesiones A/B y chat
Cursor; no republica `@codex review` ni acepta drift real del ciclo actual.
Tras un continue previo hace falta un comentario nuevo. Phase 15.9 ya evita
este patrón en runs nuevos al resetear el freeze al publicar el siguiente
trigger.

### Continuidad post-publicación y aislamiento por ciclo

Cuando el worker publica un fix externo y deja el run en `awaiting_bot_review`,
**el mismo proceso continúa el polling**. No depende de auto-spawnearse mientras
aún posee su propio PID. Las publicaciones síncronas desde `create`/comandos sí
programan un poller detached al quedar en awaiting.

Al publicar el trigger del siguiente ciclo externo se reinicia el snapshot
operativo `expected_eligible_thread_ids` (`None`). Los IDs
`processed_thread_ids` / `resolved_thread_ids` siguen siendo acumulativos para
impedir reprocesos. Los hilos nuevos del ciclo N+1 no son drift del ciclo N.

### Fallos o drift externos

Un PR cerrado, cambio externo de SHA/rama, error de `gh`, llave SSH no disponible,
rate limit, red, timeout o fallo de push deja un checkpoint recuperable o fallido.
El worker no hace reset, pull, rebase ni fuerza la publicación para resolverlo.
Tras corregir la causa, usa:

```bash
ai_dev_loop pr-review resume <run-id> [--controller-session-id <sesion-A>]
```

Si el run ya está en `awaiting_bot_review` pero el worker registrado está ausente
o stale (por ejemplo tras un bug histórico de auto-spawn), el mismo comando
desde el controlador A **reengancha solo el polling**: puede hacer validación
read-only de PR/head, pero no escribe en GitHub, no republica `@codex review`,
no crea Cursor ni invoca Codex. Si el worker ya está vivo (identidad PID +
`pid_starttime` verificada), la respuesta es idempotente sin mutación.

Si la adjudicación falló porque Codex rechazó el schema de salida
(`invalid_json_schema` / `uniqueItems`) **antes** de responder o resolver hilos y
sin crear Cursor, el origen `failed` no se edita in-place. Recupera así:

```bash
ai_dev_loop pr-review recover <failed-run-id> --dry-run
ai_dev_loop pr-review recover <failed-run-id> --output json
# Desde el controlador A del sucesor:
ai_dev_loop pr-review resume <successor-run-id> --controller-session-id <sesion-A>
```

Si, tras una adjudicación ya recuperada, Cursor corrigió y el staging quedó
listo pero faltó persistir `codex/reviews/NN.json` (por ejemplo porque el
directorio de artefactos no existía antes de `--output-last-message`), usa el
mismo `pr-review recover` sobre ese run `failed`: el checkpoint será
`reviewing` y el resume desde A reintentará solo la revisión local con la
sesión B exacta, sin reejecutar Cursor ni republicar `@codex review`.

El sucesor reutiliza PR, SHA, trigger, hilos elegibles, sesión B, chat Cursor y
controlador A. **No** se publica otro `@codex review`. Si el conjunto de hilos
elegibles cambió, el worker se detiene en atención de usuario sin writes GitHub.

Para detenerlo sin reescribir Git:

```bash
ai_dev_loop pr-review abort <run-id>
```

## 7. Cierre y merge

El ciclo automatizado termina cuando las correcciones aceptadas han pasado la
revisión local y no quedan hilos externos accionables pendientes dentro de la
ventana de revisión configurada. Consulta siempre el estado seguro del ciclo:

```bash
ai_dev_loop pr-review status <run-id> --output json
```

Una revisión sin nuevos hilos debe tener una señal de finalización verificable;
no se interpreta el silencio del bot como una aprobación sin evidencia. Si la
ventana de polling expira, el ciclo se interrumpe para intervención humana.

La última revisión humana y el merge a `master` permanecen fuera de la
automatización.

## Límites de seguridad permanentes

- No usar `--last` ni inferir sesiones Codex.
- No crear otro chat Cursor dentro del mismo run.
- No guardar credenciales GitHub en YAML o artefactos.
- No exponer prompts completos, transcript, parches, JSONL ni tokens en la salida
  normal.
- No hacer merge, force-push, reset, clean, stash ni unstage automático.
- No convertir comentarios inciertos en correcciones automáticas.
