# Fase 16: contexto y dirección para la reestructuración de PR Review v2

Fecha de la decisión: 2026-07-19

## Propósito de este documento

Este documento conserva el contexto, las decisiones y la dirección técnica acordada para la
**Fase 16**, que reemplazará el ciclo actual de GitHub PR review de `ai_dev_loop`. Está pensado para
que otros agentes puedan comprender el problema y continuar el trabajo sin reconstruir toda la
discusión.

No es un plan implementable ni un prompt de ejecución. Cada subfase 16.x descrita aquí deberá
convertirse posteriormente en un plan de implementación acotado, con archivos concretos, pruebas,
criterios de aceptación y `OpenQuestions` propios antes de entregar trabajo a Cursor u otro agente.

Las instrucciones posteriores del usuario prevalecen sobre este documento.

## Decisiones ya tomadas

Estas decisiones son parte del alcance aprobado y no deben reabrirse sin una nueva indicación del
usuario:

1. El ciclo actual de PR review será reemplazado, no reparado incrementalmente.
2. No se mantendrá retrocompatibilidad con runs, estados, checkpoints, recovery paths ni
   artefactos históricos del PR review actual.
3. No se migrarán runs históricos de PR review a v2.
4. El usuario es el único usuario del sistema y dejará de usar el ciclo de PR review durante toda
   la reestructuración.
5. No se debe invertir tiempo en corregir riesgos del ciclo viejo solo para mantenerlo operativo.
   Los cambios sobre código viejo deben limitarse a desacoplarlo, sustituirlo o eliminarlo.
6. El ciclo de implementación local que comienza con el Handoff A/B sí debe seguir disponible y
   estable durante el trabajo.
7. No se usará una librería externa de máquinas de estados ni un framework externo de durable
   workflows. La solución debe construirse con Python, Pydantic, `sqlite3` y las primitivas locales
   necesarias.
8. PR review v2 debe tener resiliencia explícita ante timeouts, rate limits e inaccesibilidad
   temporal de GitHub.
9. La política predeterminada será un intento inicial y hasta cinco reintentos adicionales con
   espera incremental. Un error temporal no debe marcar el workflow como fallido en el primer
   intento.
10. El código legacy solo se eliminará después de que v2 complete la aceptación automatizada y un
    ciclo real controlado en la Fase 16.8. Esto no implica mantenerlo ni usarlo durante la
    implementación.

## Contexto del sistema actual

`ai_dev_loop` contiene dos responsabilidades relacionadas pero diferentes:

- El ciclo de implementación local: Handoff A/B, Cursor, staging, revisión Codex, correcciones,
  `resume`, `recover`, `extend` y abort.
- El ciclo posterior de GitHub PR review: publicación, polling del bot, adjudicación de threads,
  correcciones locales, replies, resolución de threads y nueva publicación.

El ciclo local A/B se usa con frecuencia y tiene valor independiente. El ciclo de PR review se fue
incorporando sobre el mismo `RunState`, los mismos estados del workflow local y el mismo
`workflow_engine`. Con el tiempo acumuló múltiples dimensiones de estado:

- `RunStatus`;
- `github_pr_review.lifecycle`;
- fases de publicación;
- estados del checkpoint de adjudicación;
- estados individuales de replies;
- recovery lineage y sucesores;
- liveness del worker;
- evidencia implícita en archivos y hashes.

Aunque existen tablas y validaciones de transición parciales, el agregado no funciona como una
única máquina de estados cerrada. La validez depende de combinaciones manuales de campos y de la
presencia de determinados artefactos. Esto produjo funciones de reanudación, worker y recovery muy
grandes y una sucesión de correcciones específicas para ventanas de fallo históricas.

En la revisión que originó esta decisión:

- `ruff` pasó sin hallazgos;
- `mypy --strict` pasó sobre el paquete;
- los 817 tests existentes pasaron;
- se confirmó que el código posee buenas defensas de seguridad, hashes, persistencia atómica,
  locks y validación conservadora;
- también se confirmó que el problema principal es arquitectónico y de complejidad accidental, no
  una ausencia general de pruebas o disciplina.

Referencias relevantes del diseño actual:

- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/iterations.py`
- `src/ai_dev_loop/commands/pr_review.py`
- `src/ai_dev_loop/commands/pr_review_independent.py`
- `src/ai_dev_loop/commands/pr_review_recover.py`
- `src/ai_dev_loop/external_adjudication.py`
- `src/ai_dev_loop/runners/github.py`
- `src/ai_dev_loop/runners/publish.py`

El contenido legacy de esos archivos es evidencia para entender contratos y garantías, pero no es
la arquitectura que debe preservarse.

## Objetivo general

Construir un nuevo orquestador de PR review que sea:

- independiente del estado interno del ciclo local A/B;
- gobernado por una única máquina de estados explícita;
- durable y reiniciable en el mismo run;
- transaccional para estado, eventos e intenciones de side effects;
- seguro frente a workers duplicados, aborts concurrentes y resultados tardíos;
- idempotente o reconciliable para todas las escrituras externas;
- resiliente a fallos temporales de red y GitHub;
- observable mediante estado, historial y una acción segura siguiente;
- pequeño y modular, sin ramas de compatibilidad con fases históricas.

El ciclo de PR review debe poder usar el loop local Cursor -> staging -> Codex como una capacidad,
pero el loop local no debe conocer GitHub ni estados del PR.

## No objetivos

Quedan fuera de alcance salvo nueva decisión explícita:

- migrar o reanudar runs legacy de PR review;
- preservar comandos internos, schemas o layouts legacy de Phase 15;
- reparar `pr_review_recover.py` o ampliar sus recovery paths;
- mantener dos implementaciones de PR review operativas en paralelo;
- introducir Temporal, DBOS, `transitions`, `python-statemachine` u otro framework;
- migrar el estado del ciclo A/B a SQLite solo por uniformidad;
- cambiar el contrato público del Handoff A/B sin necesidad demostrada;
- añadir ejecución distribuida o soporte multiusuario;
- hacer merge automático de PRs;
- ocultar drift real de branch, SHA, patch, threads o identidad del repositorio mediante retries.

## Guardrail principal: preservar el ciclo de implementación A/B

El ciclo A/B debe permanecer utilizable durante todas las fases. Antes de hacer una extracción se
deben congelar mediante tests sus contratos observables:

- preparación con identidades distintas de controller A y reviewer B;
- lanzamiento detachado;
- uso de la sesión Codex B exacta;
- uso y reanudación del chat Cursor exacto;
- Cursor -> staging -> Codex;
- múltiples iteraciones locales de review/fix;
- `resume`, `recover`, `extend` y abort;
- hashes y snapshots relevantes;
- seguridad de procesos, permisos y redacción;
- resultados `completed`, `completed_with_residual_risk`, `max_iterations_reached`, `interrupted`,
  `failed` y `aborted` según el contrato local existente.

La extracción interna puede cambiar módulos y dependencias, pero no debe cambiar comandos, salidas,
artefactos o semántica A/B salvo que un plan posterior lo autorice expresamente.

## Separación arquitectónica objetivo

### 1. Loop local reutilizable

El loop local debe convertirse en una capacidad independiente de GitHub. Conceptualmente recibirá
una solicitud tipada semejante a:

```python
LocalReviewFixRequest(
    repository=...,
    cursor_chat_id=...,
    codex_session_id=...,
    initial_prompt=...,
    max_iterations=...,
    runtime=...,
)
```

Y devolverá un resultado de dominio semejante a:

```text
accepted
accepted_with_residual_risk
max_iterations_reached
paused
failed
aborted
```

La forma final de estos modelos se decidirá en el plan de su fase. El contrato importante es:

- no importar ni interpretar modelos GitHub;
- no publicar ni consultar GitHub;
- no decidir qué ocurre después de una aceptación local;
- conservar identidades exactas de Cursor y Codex;
- producir checkpoints locales suficientes para `resume`;
- permitir que el ciclo A/B y PR review v2 lo invoquen sin compartir sus máquinas de estados.

### 2. Orquestador PR review v2

PR review v2 tendrá modelos, store, reducer, efectos, worker y comandos propios. Podrá referenciar un
run local o crear una ejecución local de corrección, pero no añadirá campos GitHub al `RunState` del
ciclo A/B.

Una separación esperada, todavía no vinculante a nombres exactos, es:

```text
domain/pr_review_state.py
domain/pr_review_events.py
domain/pr_review_effects.py
domain/pr_review_reducer.py
application/pr_review_engine.py
infrastructure/pr_review_store.py
infrastructure/github_gateway.py
infrastructure/git_publication_gateway.py
workers/pr_review_effect_worker.py
commands/pr_review_v2.py
```

Los adapters de Cursor/Codex/Git existentes pueden reutilizarse cuando sus contratos sean
independientes del lifecycle legacy.

## Máquina de estados v2

Debe existir una sola representación autoritativa del estado. Se recomienda una unión discriminada
de variantes Pydantic, no varios strings ortogonales que puedan formar combinaciones inválidas.

Un conjunto inicial de estados conceptuales es:

```text
PREPARED
PUBLISHING_INITIAL
WAITING_FOR_BOT
ADJUDICATING
WAITING_RETRY
WAITING_FOR_USER
RUNNING_LOCAL_FIX
PUBLISHING_FIX
COMPLETED
PAUSED
FAILED
ABORTED
```

`WAITING_RETRY` debe identificar el efecto pendiente, el estado al que retorna, el siguiente
instante elegible y el intento actual. `PAUSED` debe identificar una razón tipada y la acción segura
para continuar. `FAILED` se reserva para corrupción, violación de invariantes o una condición que no
pueda repararse de manera segura. Los problemas operativos corregibles deben terminar en `PAUSED`,
no en `FAILED` ni en un run sucesor.

Toda transición debe suceder a través de un reducer puro:

```text
estado actual + evento -> nuevo estado + efectos solicitados
```

El reducer no ejecuta subprocesses, Git, GitHub, Cursor ni Codex. Debe poder probarse con una tabla
exhaustiva de eventos y estados, incluyendo que eventos inválidos no muten estado ni creen efectos.

## Eventos, efectos y persistencia

### SQLite

PR review v2 usará una base SQLite local mediante `sqlite3`. El ciclo A/B puede conservar su
persistencia actual.

El schema conceptual mínimo incluye:

```text
pr_review_runs
  run_id
  state_kind
  state_payload
  version
  created_at
  updated_at

pr_review_events
  event_id
  run_id
  sequence
  event_type
  payload
  created_at

pr_review_effects
  effect_id
  run_id
  effect_type
  payload
  status
  idempotency_key
  attempt_count
  max_retries
  next_attempt_at
  last_error_kind
  last_error_summary
  lease_generation
  created_at
  updated_at

pr_review_worker_leases
  run_id
  owner_token
  generation
  pid_identity
  acquired_at
  heartbeat_at
  expires_at
```

Los nombres y columnas finales pueden variar, pero deben conservar estas garantías:

- la transición de estado, el evento y los nuevos efectos se confirman en una sola transacción;
- cada run tiene una versión monotónica para compare-and-swap;
- cada efecto tiene identidad estable e idempotency key;
- un worker solo puede reclamar efectos con una lease vigente;
- cada nueva lease incrementa una generación de fencing;
- resultados de un worker viejo o de una versión anterior se descartan sin mutar el run;
- un restart reconstruye la siguiente acción desde SQLite, no desde inferencias sobre logs;
- los artefactos grandes o sensibles pueden seguir en archivos protegidos, referenciados por path y
  hash desde el estado durable.

### Protocolo de ejecución de efectos

El flujo esperado para un efecto es:

```text
pending
  -> claimed
  -> succeeded
  -> retry_wait
  -> uncertain
  -> permanently_failed o paused
```

Antes de cualquier side effect se debe persistir la intención. Después de una llamada externa larga
se debe validar bajo transacción:

```text
run_id + run version + effect_id + worker lease generation + cycle + bound head SHA
```

Si el run fue abortado, pausado, avanzado por otro worker o cambió de versión, el resultado tardío
no puede producir nuevos side effects.

## Resiliencia de red y retries

### Semántica del presupuesto

La política predeterminada es:

- un intento inicial;
- hasta cinco reintentos adicionales;
- seis intentos totales como máximo por tanda;
- estado y temporizador persistidos;
- backoff incremental con jitter aproximado de +/-20 %.

Intervalos predeterminados propuestos:

```text
reintento 1:  10 segundos
reintento 2:  30 segundos
reintento 3:  90 segundos
reintento 4: 180 segundos
reintento 5: 300 segundos
```

Los valores deben ser configurables. Si GitHub entrega `Retry-After`, `X-RateLimit-Reset` u otra
indicación confiable de espera, esa información tiene prioridad sobre el backoff local. Debe
definirse un límite razonable para que un header anómalo no deje un efecto suspendido
indefinidamente sin visibilidad.

Al agotar la tanda, el run pasa a `PAUSED` con razón `github_retry_exhausted`, efecto fallido, último
error seguro y comando sugerido. Un `pr-review resume` explícito puede iniciar una nueva tanda sobre
el mismo efecto y la misma idempotency key. No debe repetir efectos ya confirmados.

Abortar cancela cualquier retry pendiente. Un worker que despierte después del abort debe fallar el
fencing check antes de ejecutar la llamada externa.

### Clasificación de errores

Errores normalmente reintentables:

- timeout;
- fallo DNS;
- conexión rechazada o interrumpida;
- red temporalmente inaccesible;
- HTTP 429;
- rate limit primario o secundario;
- HTTP 502, 503 o 504;
- errores temporales de `gh` que el gateway pueda clasificar de manera tipada.

Errores que no deben reintentarse ciegamente:

- autenticación inválida;
- permisos insuficientes;
- PR cerrado;
- cambio de branch o head SHA;
- cambio del repository identity;
- validación HTTP 422;
- artefactos ausentes o corruptos;
- hashes inconsistentes;
- drift de patch o de threads;
- violación de una transición o invariante interna.

La clasificación debe depender de tipos y datos estructurados del gateway, no de búsquedas de
substrings dispersas en el engine.

### Polling normal no es retry

Se deben distinguir dos condiciones:

- GitHub responde correctamente y todavía no existe review del bot: espera normal; no consume
  presupuesto de retries.
- GitHub no puede consultarse: fallo temporal; consume un intento del efecto de consulta.

El tiempo normal de espera del bot y el presupuesto de errores de red son conceptos distintos y
deben aparecer separados en estado y observabilidad.

## Lecturas y escrituras GitHub

### Lecturas

Las lecturas se pueden reintentar después de verificar lease, versión y abort:

- obtener PR y head SHA;
- listar review threads;
- obtener comentarios y reactions;
- consultar estado de un thread;
- consultar refs remotos;
- buscar un marcador idempotente.

### Escrituras

Una escritura que termina en timeout o error de conexión es ambigua: GitHub puede haberla aplicado.
Nunca debe repetirse inmediatamente. El protocolo obligatorio es:

```text
persistir intención
-> ejecutar escritura
-> timeout/error ambiguo
-> marcar uncertain
-> consultar GitHub o Git remoto
-> si ya se aplicó: succeeded
-> si se prueba que no se aplicó: retry_wait
-> si no puede determinarse: PAUSED
```

Cada operación necesita una estrategia de reconciliación explícita:

- Crear o actualizar PR: buscar el PR por repositorio, head y base, y verificar SHA.
- Publicar trigger de review: incluir y buscar un marcador único derivado del `effect_id`.
- Reply inline: consultar comentarios del thread y comparar identidad, marcador cuando sea posible,
  hash del body y ventana temporal de la intención.
- Resolver thread: consultar `isResolved` antes de repetir.
- Commit local: verificar HEAD, patch hash y checkpoint de commit.
- Push: consultar el ref remoto y confirmar el commit SHA exacto antes de repetir.
- Actualizar texto del PR: volver a leer título/body o aceptar la actualización existente cuando
  corresponde al mismo efecto.

Ejemplos conceptuales de idempotency keys:

```text
pr-review:{run_id}:cycle:02:review-trigger
pr-review:{run_id}:cycle:02:reply:{thread_id}
pr-review:{run_id}:cycle:02:resolve:{thread_id}
pr-review:{run_id}:cycle:02:push:{commit_sha}
```

Los bodies sensibles no deben persistirse en eventos ni logs por comodidad. Se conservarán en
artefactos protegidos y se referenciarán mediante hashes.

## Entradas funcionales que v2 debe contemplar

La implementación final debe contemplar, salvo que un plan de subfase reduzca explícitamente el
alcance temporal:

1. Crear/publicar un PR a partir de un run A/B completado y un patch aceptado.
2. Adoptar un PR existente, validando repositorio, branch, base y head SHA.
3. Solicitar review del bot con un trigger idempotente.
4. Esperar review sin confundir ausencia de feedback con error.
5. Congelar el conjunto elegible de threads de una ronda.
6. Adjudicar todos los threads mediante la sesión Codex reviewer exacta.
7. Pausar y responder de forma segura ante findings no aplicables o inciertos.
8. Ejecutar correcciones accionables mediante el loop local reutilizable.
9. Revisar localmente el patch corregido antes de publicar.
10. Commit/push sin force-push, resolver threads confirmados y solicitar la siguiente ronda.
11. Completar cuando existe evidencia verificable de que no hay findings.
12. Respetar límites configurados de rondas externas e iteraciones locales.
13. Permitir status, resume y abort seguros.
14. Nunca hacer merge, cerrar o retargetear automáticamente el PR.

## Observabilidad requerida

`pr-review status` debe explicar al menos:

- estado autoritativo actual;
- PR, ciclo y prefijo del bound head SHA;
- efecto activo o pendiente;
- intento actual y máximo;
- `next_attempt_at` cuando espera retry;
- clasificación segura del último error;
- liveness y generación de la lease sin exponer token o PID innecesario;
- si existe un resultado ambiguo pendiente de reconciliación;
- siguiente acción segura para el usuario;
- si el run está pausado, qué condición debe corregirse antes de `resume`.

El event journal debe permitir reconstruir por qué se tomó una decisión, pero el snapshot
transaccional es la fuente rápida del estado actual. Los eventos no deben almacenar tokens, bodies
completos de comentarios, prompts completos ni patches.

## Subfases de implementación de la Fase 16

Cada subfase debe tener su propio plan implementable. Los tests, documentación y criterios de
salida forman parte de cada subfase; no deben postergarse todos hasta el final.

### Fase 16.1: blindaje del ciclo A/B

Objetivo: establecer una barrera de regresión antes de mover responsabilidades compartidas.

Alcance esperado:

- inventariar el contrato observable de prepare/launch/start/resume/recover/extend/abort;
- añadir o consolidar tests de caracterización A/B sin GitHub;
- demostrar que el ciclo local puede ejecutarse con `github.enabled` desactivado;
- registrar dependencias actuales desde `workflow_engine` e `iterations` hacia PR review;
- definir métricas/baseline de tests que las subfases 16.2-16.9 deben conservar.

No incluye refactor productivo relevante ni cambios al ciclo legacy de PR.

Criterio de salida: existe una suite A/B explícita, estable y suficiente para detectar cambios en
identidades, prompts, iteraciones, staging, review, checkpoints y resultados.

### Fase 16.2: extracción del loop local

Objetivo: hacer que Cursor -> staging -> Codex sea una capacidad reutilizable e independiente de
GitHub, sin cambiar el comportamiento A/B.

Alcance esperado:

- definir request/result tipados para el loop local;
- mover fuera del engine local las decisiones de publicación GitHub;
- retirar del planner de iteraciones la semántica específica de external feedback;
- introducir un boundary o callback de aplicación para entregar el resultado al caller;
- conservar exactamente sesión B, chat Cursor, runtime, hashes y checkpoints;
- adaptar el ciclo A/B al nuevo boundary;
- ejecutar toda la suite A/B y pruebas de equivalencia.

Criterio de salida: el loop local no importa módulos PR/GitHub, el flujo A/B sigue operativo y un
test puede invocar el loop local mediante su nueva interfaz sin construir un `GithubPrReviewState`.

### Fase 16.3: dominio y reducer de PR review v2

Objetivo: definir la nueva máquina de estados como lógica pura antes de integrar I/O.

Alcance esperado:

- modelos Pydantic v2 independientes del `RunState` legacy;
- estados discriminados, eventos tipados y efectos declarativos;
- tabla explícita de transiciones;
- reducer puro;
- razones tipadas de pause/failure/retry;
- semántica de ciclos externos y límites;
- pruebas exhaustivas de transiciones válidas e inválidas;
- pruebas de invariantes y determinismo del reducer.

No incluye SQLite, GitHub real, Cursor ni Codex.

Criterio de salida: desde eventos simulados se puede recorrer el ciclo completo hasta completed,
paused, failed y aborted sin side effects ni combinaciones de estado inválidas.

### Fase 16.4: motor durable

Objetivo: ejecutar la máquina v2 sobre SQLite con transacciones, outbox, leases y timers durables.

Alcance esperado:

- schema y migración inicial únicamente para v2;
- store transaccional;
- journal y snapshots versionados;
- creación/reclamo/finalización de efectos;
- worker lease, heartbeat, expiry, generación y fencing;
- scheduler de `next_attempt_at`;
- política de un intento más cinco reintentos;
- restart/replay usando efectos fake;
- status básico desde SQLite;
- abort que invalida efectos y leases pendientes.

Criterio de salida: un workflow completamente simulado sobrevive crashes antes/después de cada
commit transaccional, un segundo worker no puede aplicar resultados viejos y los retries sobreviven
al restart.

### Fase 16.5: gateway GitHub de lectura

Objetivo: integrar observación de GitHub con errores tipados y backoff durable, sin escrituras.

Alcance esperado:

- adapter único para `gh`/GraphQL de lectura;
- PR identity, head SHA, estado, threads, comentarios y reactions;
- clasificación estructurada de auth, permission, not found, drift, timeout, network, rate limit y
  errores de servidor;
- respeto de headers/metadata de rate limit;
- distinción entre polling normal y retry;
- integración con effects, leases y scheduler;
- contract tests con respuestas simuladas y fixtures sanitizados.

Criterio de salida: el engine puede adoptar/observar un PR y esperar feedback, sobrevivir cinco
fallos temporales y pausar correctamente sin realizar ninguna escritura GitHub.

### Fase 16.6: escrituras idempotentes y reconciliación

Objetivo: implementar todos los side effects mutantes sin duplicación ante crashes o timeouts.

Alcance esperado:

- intent durable antes de cada write;
- idempotency keys estables;
- create/update PR;
- review trigger con marcador;
- replies inline;
- resolución de threads;
- generación de texto de publicación;
- commit y push con precondiciones;
- reconciliador específico por tipo de efecto;
- estado `uncertain` y pausa segura cuando no existe prueba suficiente;
- fault injection antes, durante y después de cada write.

Criterio de salida: repetir cualquier test después de un crash no crea PRs, comentarios, replies,
commits, pushes o resoluciones duplicadas.

### Fase 16.7: orquestación completa de PR review v2

Objetivo: conectar dominio, motor durable, GitHub, Codex y el loop local extraído.

Alcance esperado:

- creación desde run A/B completado;
- preparación/adopción de PR independiente;
- comandos CLI v2 de prepare/create/start/status/resume/abort según corresponda;
- adjudicación externa con la sesión Codex exacta;
- congelación y verificación de thread sets;
- ejecución de correcciones mediante el boundary local;
- aceptación local y publicación;
- siguiente ciclo y finalización no-findings;
- límites de ciclos e iteraciones;
- observabilidad completa y mensajes operativos seguros.

Criterio de salida: un escenario end-to-end totalmente simulado recorre varias rondas, incluye una
corrección local, publica el nuevo SHA y completa sin invocar código de lifecycle/recovery legacy.

### Fase 16.8: resiliencia y aceptación

Objetivo: demostrar que el sistema completo cumple las garantías bajo fallos reales y simulados.

Alcance esperado:

- fault injection sistemático en cada boundary;
- timeout/network/rate-limit en lecturas y escrituras;
- restart durante polling, adjudicación, Cursor, review local y publicación;
- abort durante una llamada larga;
- resultados tardíos después de abort o nueva lease;
- dos workers competidores;
- corrupción y drift de PR/SHA/patch/thread sets;
- agotamiento y reinicio explícito de la tanda de retries;
- pruebas de privacidad/redacción/permisos;
- ejecución real controlada sobre un PR de prueba;
- documentación operativa de status/resume/abort/troubleshooting v2.

Criterio de salida: suite completa verde, ciclo A/B verde, aceptación real exitosa y evidencia de
que ninguna ventana de crash conocida duplica side effects o deja un run sin acción segura.

### Fase 16.9: cutover y limpieza legacy

Objetivo: convertir v2 en la única implementación y reducir de verdad la complejidad del sistema.

Alcance esperado:

- dirigir los comandos públicos `pr-review` a v2;
- eliminar código del engine PR legacy;
- eliminar `pr_review_recover` y recovery paths exclusivos de Phase 15;
- retirar campos GitHub del estado local cuando ya no tengan consumidores válidos;
- eliminar schemas y tests exclusivamente legacy;
- retirar imports circulares y helpers sin uso;
- actualizar README, MkDocs, CLI reference y troubleshooting;
- verificar instalación/packaging y ausencia de artefactos temporales;
- volver a ejecutar toda la suite relevante.

No se implementa migración ni adapter de lectura v1 salvo que el usuario lo solicite expresamente
en el futuro.

Criterio de salida: solo existe un ciclo de PR review productivo, el ciclo A/B permanece operativo,
no quedan ramas de compatibilidad Phase 15 y la documentación describe únicamente el flujo v2.

## Estrategia de pruebas transversal

Cada plan de subfase debe seleccionar pruebas proporcionales a su riesgo. Para la Fase 16 completa
se esperan al menos:

- unit tests del reducer y modelos discriminados;
- tests de transición generados o parametrizados sobre toda la tabla;
- tests transaccionales SQLite;
- tests de fencing/leases con dos workers;
- contract tests del gateway GitHub;
- fault injection en cada side effect;
- tests de idempotencia y reconciliación;
- tests de restart con DB persistida;
- tests de abort concurrente;
- tests end-to-end con GitHub simulado;
- aceptación real controlada;
- suite de regresión A/B en todas las subfases que toquen código compartido.

Los tests llamados `integration` deben evitar depender exclusivamente de monkeypatches de funciones
internas. Siempre que sea práctico, deben ejercitar boundaries reales contra procesos fake,
repositorios Git temporales y respuestas GitHub contractuales.

## Reglas para agentes que continúen este trabajo

1. Leer este documento completo antes de proponer una subfase.
2. Inspeccionar el código y tests actuales correspondientes a esa subfase; este documento no sustituye
   evidencia del repositorio.
3. Elaborar un plan implementable solo para una subfase 16.x a la vez.
4. Preservar el ciclo A/B y ejecutar su barrera de regresión cuando se toque código compartido.
5. No añadir nuevas ramas de compatibilidad o recovery al ciclo PR legacy.
6. No usar el ciclo PR legacy para validar la implementación; usar tests/fakes y, en la Fase 16.8,
   un PR controlado con v2.
7. No introducir una librería externa de FSM/durable workflow sin nueva aprobación del usuario.
8. Mantener side effects fuera del reducer.
9. Persistir intención antes de cualquier write y reconciliar toda salida ambigua.
10. Tratar `FAILED` como excepción grave; usar retries y `PAUSED` para condiciones operativas.
11. No almacenar tokens, bodies completos, prompts o patches en SQLite/events/logs salvo que exista
    una necesidad de seguridad revisada; usar artefactos protegidos y hashes.
12. No avanzar a la limpieza legacy hasta completar la aceptación real de v2.

## Preguntas que cada plan de subfase debe resolver

No son decisiones para responder todas de antemano; son temas que no deben quedar implícitos:

- ¿Cuál es el contrato público exacto que cambia en esta subfase?
- ¿Qué invariantes A/B podrían verse afectadas?
- ¿Cuál es la unidad transaccional y qué ocurre si el proceso muere en cada boundary?
- ¿Qué efectos son read-only, idempotentes, reconciliables o inherentemente ambiguos?
- ¿Qué error es retryable, pausable o terminal?
- ¿Cómo se identifica y descarta un resultado tardío?
- ¿Qué información sensible existe y dónde puede persistirse?
- ¿Qué comando o acción segura verá el usuario después de un fallo?
- ¿Qué tests demuestran restart, abort e idempotencia?
- ¿Qué código legacy puede eliminarse en lugar de adaptarse?

## Resultado esperado al finalizar

Al terminar las nueve subfases de la Fase 16, `ai_dev_loop` tendrá dos componentes claramente
separados:

1. Un ciclo local A/B estable y reutilizable para implementación y review/fix.
2. Un orquestador PR review v2 durable que usa el ciclo local como subproceso de dominio, controla
   GitHub mediante efectos idempotentes y se recupera de timeouts o caídas de red sin perder el
   estado ni crear side effects duplicados.

El sistema no conservará la complejidad histórica de Phase 15, no requerirá sucesores para fallos
operativos normales y permitirá que un usuario comprenda y retome cualquier pausa desde el mismo
run.
