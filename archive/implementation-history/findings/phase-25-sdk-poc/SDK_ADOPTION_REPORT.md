# Prueba de concepto: Cursor SDK para ai_dev_loop

Fecha de cierre: 2026-10-09 UTC. Insumo técnico para futuros planes de Fase 25.

**Resultado:** el SDK Python local permite implementar el flujo básico: crear un agente, obtener su identidad inmediatamente, enviar varios turnos, editar archivos, cargar instrucciones del proyecto, conservar conversación entre procesos y recuperar métricas. La adopción es viable, pero exige adaptar los contratos del scheduler y probar su supervisión; cambiar solamente el comando de Cursor sería insuficiente.

Se ejecutaron **27 verificaciones satisfactorias**: 22 comprobaciones de evidencia obtenida y 5 contratos de transporte con respuestas simuladas. Estas verificaciones incluyen limitaciones observadas del SDK; que pasen no significa que la integración de producción esté implementada o validada.

## Alcance, aislamiento y reproducibilidad

Repositorio inspeccionado: /home/rojobad/Projects/ai_dev_loop. HEAD observado: 58944326a06cf319df521d595050fec091d870da. Los planes de Fase 24 existentes no se ejecutaron ni se usaron como dependencia.

Todo lo creado por esta prueba está en /tmp/cursor-sdk-poc-o80OgDfU: venv, dependencias, cache, repositorio mínimo independiente, scripts, estado nativo del SDK y evidencia. No se modificaron archivos del repositorio principal, su Git, su instalación, el ledger vivo, sus artifacts, los runs de Cursor CLI ni el timer. No se lanzaron subagentes ni agentes cloud, ni se ejecutó otro reviewer Codex.

Entorno observado:

| Componente | Versión / detalle |
| --- | --- |
| Python de la POC | 3.13.14 |
| cursor-sdk | 1.0.37, instalado únicamente en venv temporal |
| Bridge | bridgeVersion 1.0.0, protocolVersion sdk.v1 |
| Dependencias principales | httpx 0.28.1, anyio 4.15.1 |
| Runtime | local, Linux/WSL, paquete con Node incluido |
| Python del proyecto | requires-python >=3.11; falta probar SDK en ese mínimo |
| Modelo | grok-4.7, context=256k, reasoning_effort=high, fast=false |
| Fuentes de configuración | setting_sources=['project'] explícito |
| Persistencia nativa | JSONL en state/agents dentro de la POC |
| Transporte | max_retries=0; timeout de cliente explícito |

La instalación descargó un wheel de aproximadamente 56.4 MiB. La POC funcionó sin Node instalado en el PATH: el paquete usa su propio binario. Esto elimina una instalación externa de Node para este entorno, aunque el SDK sigue teniendo procesos auxiliares nativos. Para producción hace falta fijar versión, verificar distribución/plataforma y probar el Python mínimo del proyecto.

La API key se suministró sólo para autenticar la prueba. El harness la quitó de su entorno antes de lanzar el bridge y la pasó mediante api_key. El audit final encontró cero copias de la clave en los 102 archivos de la prueba revisados, excluyendo venv/cache, y cero procesos propios restantes. También se retiró del almacenamiento temporal del agente que conducía la prueba. El usuario puede revocarla; no se conservó una credencial reutilizable.

Evidencia: [resumen de verificaciones](/tmp/cursor-sdk-poc-o80OgDfU/verification-summary.json), [audit final](/tmp/cursor-sdk-poc-o80OgDfU/final-audit.json), [harness](/tmp/cursor-sdk-poc-o80OgDfU/probe.py), [verificaciones](/tmp/cursor-sdk-poc-o80OgDfU/test_evidence.py).

Para repetir sólo las verificaciones de evidencia y transporte, sin llamadas al modelo:

    /tmp/cursor-sdk-poc-o80OgDfU/venv/bin/python /tmp/cursor-sdk-poc-o80OgDfU/test_evidence.py

Los scripts que llaman al SDK necesitan una nueva credencial y conviene ejecutarlos con un directorio nuevo; algunos escenarios usan marcadores de inicio y no son idempotentes como scripts completos.

## Matriz de pruebas reales

| Caso | Resultado observado | Evidencia |
| --- | --- | --- |
| Salud y autenticación | ping, versión, me() y catálogo funcionan con la key suministrada | preflight.json |
| Identidad inicial | Agent.create devuelve agent_id antes del primer turno | identity.json, first.json |
| Turno sin herramientas | respuesta y métricas completas | first.json |
| Reanudación entre procesos | un proceso/bridge nuevo conserva exactamente el mismo agent_id y recuerda ORCHID-731 | resume.json |
| Modelo congelado | mismo ID y parámetros explícitos en los turnos comprobados | first.json, resume.json, metadata.json |
| Reglas de proyecto | marcador POC-RULE-905 en respuesta | first.json, tools.json |
| Skills del proyecto | invoca skill solicitada; devuelve POC-SKILL-472 | tools.json |
| Edición | corrige calc.py de resta a suma; 4 casos aritméticos independientes pasan | arithmetic-checks.json, workspace/calc.py |
| AGENTS.md | agente nuevo responde con POC-AGENTS-193 además de la regla | metadata.json |
| MCP del proyecto | carga .cursor/mcp.json y llama exactamente una vez al servidor stdio sintético | mcp.json, mcp-calls.ndjson |
| Herramienta Python propia | callback ejecutado exactamente una vez con contexto de tool call; devuelve POC-CUSTOM-364 | custom.json, custom-calls.json |
| Cancelación normal | cancel(), estado cancelled, termina stream y comando activo; sin cleanup adicional | cancel.json, cancel-run.json |
| Reanudación tras cancelar | misma identidad; conserva POC-CANCEL-616; no repite comando | cancel-resume.json |
| Consulta de turnos anteriores | get_run conserva resultado y usage en un proceso nuevo | inspect.json |
| Replay completo | observe devuelve eventos guardados, incluyendo usage; sin inferencia nueva | first-replay.json, resume-replay.json |
| Replay con cursor | 69 envelopes; después de offset 5 devuelve exactamente los 64 restantes desde 6 | replay-cursor.json |
| Key inválida | AuthenticationError | negative.json |
| Identidad inexistente | AgentNotFoundError; no crea agente por sustitución | negative.json |
| Uso facturado | get_usage rechazado por feature_unavailable de esta cuenta | billed-usage.json |
| Segundo turno concurrente | rechaza con InternalServerError y mensaje already has active run | runtime-crash.json |
| Muerte del runtime | NetworkError en stream; herramienta sigue viva; registro persiste running | runtime-crash.json, runtime-crash-reopened-snapshot.json |
| Recuperación tras esa muerte | get_run + cancel en bridge nuevo libera turno; resume conserva identidad y memoria | runtime-crash-cancel.json, runtime-crash-recovered.json |
| Restricción estricta de key | guard del cliente clasifica incorrectamente replay local como cloud | inspect-strict.json |
| Lectura histórica propia | valida snapshot anterior y ledger fixture con 14 eventos, sin CLI ni modificar bytes | historical-read.json |

Los eventos guardados incluyen status, thinking, assistant, tool_call y usage. Los archivos de envelopes preservan offsets para comprobaciones de replay. Los JSON contienen material sintético de la POC, no conversaciones reales de runs del proyecto.

## Métricas: qué podemos garantizar

Primer turno real:

| Campo nativo | Tokens |
| --- | ---: |
| input_tokens | 4820 |
| output_tokens | 139 |
| cache_read_tokens | 2560 |
| cache_write_tokens | 0 |
| total_tokens | 7519 |
| reasoning_tokens | 118 |

En los turnos terminados comprobados, usage del evento, run.usage y resultado terminal coinciden. El total observado es input + output + cache_read + cache_write; reasoning ya forma parte de output y sumarlo otra vez duplicaría tokens.

Los turnos con varias herramientas producen usage acumulado para el turno completo. No se observó un contador independiente por cada herramienta o cada llamada interna al modelo. Para ai_dev_loop se puede atribuir el uso al intento/etapa que envió ese prompt; no prometer desglose interno que la evidencia no ofrece.

**La cancelación y la muerte del runtime dejaron usage=null pese a haber comenzado inferencia.** Guardar cero falsearía el consumo. La métrica de una fase/run/sequence debe distinguir total conocido y cobertura: completa cuando corresponde, parcial cuando faltan intentos, desconocida si no hay counters. Un intento interrumpido puede tener coste no cuantificado.

El replay vuelve a entregar el evento usage original: no sumarlo nuevamente. Tampoco sumar evento y resultado terminal como si fueran consumos distintos. Sugerencia de contrato: una observación canónica por proveedor + intento del scheduler + SDK run_id, con procedencia/offset, counters nativos, disponibilidad y versión; relecturas idempotentes. Sumar intentos reales, incluidas correcciones, retries y fallos que tengan métricas, una única vez.

El uso conocido de toda la POC es **245062 tokens en 11 turnos terminados distintos**. Hay además **2 turnos interrumpidos sin counters**: este número es un límite inferior, no el total facturado. Incluye el primer intento de custom tool que no logró ejecutarla y el turno que terminó después del experimento de matar sólo el launcher. El resumen deduplica SDK run_id.

get_usage(), que pretende consultar uso facturado, devolvió InternalServerError con feature_unavailable. La captura de tokens por turno sí funciona en esta cuenta. **No condicionar las métricas a billing ni calcular USD con total_tokens multiplicado por una tarifa única.** La POC no validó importes, planes de facturación ni paridad económica con CLI.

Los datos antiguos de Cursor que carecen de counters siguen teniendo uso desconocido. La migración no crea datos faltantes. Para Codex, la captura/normalización de usage de sus eventos requiere un contrato y pruebas separados; no se ejecutó una prueba nueva de Codex SDK/CLI ni se validaron aquí sus categorías de cache.

Evidencia: [primer turno](/tmp/cursor-sdk-poc-o80OgDfU/first.json), [cancelación](/tmp/cursor-sdk-poc-o80OgDfU/cancel.json), [uso facturado](/tmp/cursor-sdk-poc-o80OgDfU/billed-usage.json), [totales deduplicados](/tmp/cursor-sdk-poc-o80OgDfU/verification-summary.json).

## Caídas, cancelación y reintentos: contrato necesario

La cancelación normal del SDK detuvo el comando lento de esta prueba en aproximadamente 0.089 segundos. Esto confirma el escenario probado, no una garantía universal sobre cualquier descendiente o comando.

Hay dos procesos distintos al iniciar el bridge: launcher shell y runtime Node. El launcher incluido ejecuta Node sin exec. El primer experimento mató sólo el launcher; Node siguió ejecutando el turno. Tras limpiar nuestro comando de prueba, el turno terminó y conservó métricas. El segundo experimento mató el PID real del runtime Node: el stream terminó con NetworkError, pero el comando Python siguió vivo y hubo que terminarlo por separado.

Por tanto, ai_dev_loop debe mantener supervisión del árbol completo mediante su worker aislado y mecanismo de procesos/unidad, comprobar terminación real y conservar la reserva del repositorio hasta que sea segura. No bastará con matar el PID guardado por el launcher del SDK. La POC no ejecutó un systemd/cgroup real; esta integración debe cubrir timeout, abort y muerte inesperada con pruebas del backend correspondiente.

Después de matar Node y limpiar el comando propio, un bridge nuevo podía cargar el run, pero seguía running. Agent.resume conservaba la identidad y un send inmediato fallaba porque seguía activo.

**Recuperación que sí se probó, sin editar el store nativo:**

1. Confirmar que los procesos/herramientas del intento anterior terminaron.
2. Lanzar un bridge nuevo apuntando al mismo workspace y store.
3. Obtener el turno exacto con get_run(run_id, runtime=local, agentId y apiKey explícitos).
4. Cancelar ese handle ya registrado: run.cancel().
5. Reconsultar el snapshot y comprobar cancelled.
6. Reanudar el mismo agent_id, reaplicando opciones congeladas, y enviar sólo el turno permitido por la política del scheduler.

El nuevo turno conservó POC-CRASH-852. No se creó un agente de reemplazo. La llamada detached cancel_run sin registrar el run devolvió BadRequestError not found; no debe sustituir el procedimiento probado sin pruebas adicionales.

La documentación también ofrece SendOptions.local.force para expirar un turno activo atascado; no se probó esa alternativa. **Tiene semántica de recuperación**, distinta del --force del CLI que habilita edición sin confirmación. No copiar automáticamente cursor.force=True de la configuración actual a ese campo. [SDK Python](https://cursor.com/docs/sdk/python), [CLI headless](https://cursor.com/docs/cli/headless).

Persistir identidad del agente antes de inferir y vincular SDK run_id al intento tan pronto como se recibe. Todavía hace falta probar la ventana en que send fue aceptado pero el worker murió antes de guardar run_id. No asumir que una excepción significa que el proveedor no comenzó trabajo. No reenviar prompts automáticamente por errores de transporte ambiguos ni afirmar exactly-once local sin comprobarlo.

Los retries del SDK se dejaron deshabilitados. Las cinco pruebas offline validan: 401 sin repetición; 429 tipado con retry_after/request_id; retry de catálogo de sólo lectura cuando se habilita explícitamente; CreateAgent no repetido aunque max_retries=2; ReadTimeout tipado. El scheduler debe seguir decidiendo cuándo se permite otro intento.

## Identidades y modelo: cambios que no se pueden omitir

Los IDs habituales observados son agent-UUID y run-UUID. Con callbacks custom, el SDK Python creó agentes con UUID sin prefijo. Preservar exactamente la identidad devuelta y usar un tipo propio para Cursor SDK. No cortar prefijos, inventar UUID, buscar el último agente ni relajar globalmente el tipo de identidad del reviewer Codex.

El catálogo expone grok-4.7 con parámetros separados; el YAML actual usa grok-4.7-high. Las defaults del catálogo observado eran context=500k, reasoning_effort=high y fast=true. La POC eligió 256k/high/false explícitos. Esto demuestra cómo congelar parámetros, **no equivalencia exacta con los defaults del alias CLI**. La futura migración debe decidir y documentar las selecciones permitidas y no heredar defaults silenciosamente.

El contrato congelado debería registrar proveedor/runtime, versión del SDK, modelo+parámetros, workspace, fuentes de configuración, sandbox, herramientas, ruta del store y referencia de credencial sin su valor. Reaplicarlo al crear y reanudar. Mantener el modelo y reasoning del reviewer B exactamente bajo las reglas actuales de submit/sequence.

Fuentes actuales donde estos cambios impactan:

- [estado Cursor del scheduler](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/domain/state.py:503) usa chat_id con UuidSessionId.
- [tipos comunes](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/domain/common.py:20) comparte el tipo UUID con otras identidades: requiere separación, no modificación indiscriminada.
- [runner de intentos](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/cursor_attempt_runner.py:1) y [runner CLI](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/runners/cursor.py:1) usan create-chat/subprocess/stream-json.
- [evidencia autenticada](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/application/cursor_evidence.py:375), attempt_service y cursor_workflow_service vinculan prompt, dispatch, ejecución y outcome.
- submission, preflight y sequence_prepare congelan comandos y configuración: deben representar el backend SDK y sus inputs verificables.
- recuperación inicial, corrección, timeout y retry contienen identidades/capturas propias del CLI: deben adaptarse preservando las decisiones durables y las políticas aprobadas.

## Herramientas, permisos y configuración

La POC de edición usó read/edit/ls/glob/grep y ningún shell. edit es el nombre válido del builtin para escribir; write fue rechazado como herramienta desconocida. Las opciones del SDK son dataclasses congeladas; el harness usa dataclasses.replace.

Rules y skills del proyecto funcionaron. AGENTS.md funcionó en un agente nuevo. MCP file-based funcionó al cargar explícitamente project. No se probaron todas las reglas del proyecto real, instrucciones de usuario/team, hooks, cambios de instrucciones después de crear agente, OAuth ni las herramientas reales de trabajo.

Custom tools son callbacks Python, pero su descubrimiento y llamada necesitaron los builtins mcp y getMcpTools en esta versión. Con tools=[] la herramienta custom no estuvo disponible y la inferencia igualmente consumió tokens. Con la configuración corregida hubo exactamente un callback. No confundir registrar un callback con garantizar que el modelo pueda usarlo.

La documentación señala que las restricciones de herramientas no sobreviven resume y que MCP inline debe suministrarse otra vez. El listado/descarga de artifacts del SDK local no reemplaza los artifacts propios del scheduler. [SDK Python](https://cursor.com/docs/sdk/python).

La POC pasó SandboxOptions(enabled=True) para las pruebas con shell. No intentó escapes, restricciones adversariales de red/filesystem ni equivalencia con cursor.sandbox/trust_workspace del CLI. El plan debe definir explícitamente la política de permisos y comprobarla en los escenarios soportados; no extrapolar aislamiento sólo porque la prueba respetó su prompt.

## Particularidades de cursor-sdk 1.0.37

1. get_usage feature_unavailable llega como InternalServerError: no tratar cualquier 500 como retryable.
2. Segundo send local estando busy también llegó como InternalServerError. No depender únicamente de AgentBusyError.
3. run.supports('observe') devolvió false para snapshots cuyo observe sí funcionó.
4. run.supports('cancel') devolvió true para snapshots ya terminados; cancel() tiene una validación separada que rechaza estados terminales.
5. allow_api_key_env_fallback=False bloqueó conversation/observe de runs locales incluso con apiKey explícita al recuperarlos: el guard los consideró cloud. Con el valor default True y sin key en el entorno, estas operaciones locales funcionaron; crear/auth siguió usando key explícita. Falta un contrato de versión y configuración que proteja este comportamiento, sin modificar internals privados en producción.
6. Matar el launcher no equivale a matar el runtime; matar el runtime no garantiza matar sus herramientas.
7. Una caída del runtime no marca automáticamente su run terminal: requiere reconciliación por operaciones soportadas.

Estos hallazgos deben convertirse en pruebas de regresión de la versión adoptada. Los accesos a atributos privados usados para identificar/matar el runtime fueron instrumentación de la POC, no una API propuesta para el producto.

## Lectura histórica y corte de instalación

Se leyó el snapshot genuino manual_wait_state_pre_phase22_v1.json con parse_scheduler_state. Se abrió dual_failure_engine.sqlite3 con mode=ro&immutable=1, validando su snapshot blocked y 14 eventos con los parsers actuales y sus hashes. Ambas fuentes conservaron los mismos bytes; no se invocó Cursor CLI.

Esto confirma que los datos históricos propios se pueden interpretar independientemente del proveedor en el código actual. **La futura modificación todavía debe demostrar esa compatibilidad.** status, list y history cargan snapshots validados antes de proyectarlos; cambiar tipos o schemas y romper esa lectura también rompe consulta histórica, aunque los artifacts estén intactos.

Mantener lectores/versiones para los schemas anteriores, los bytes e integridad de artifacts y eventos, enlaces a evidencia y respuesta coherente cuando no hay métricas. No exigir que existan CLI, bridge, login o SDK store para leer un run antiguo. La lectura histórica puede mantenerse sin ejecutar o reanudar viejas sesiones de Cursor.

El corte propuesto por el usuario simplifica el runtime: esperar a que terminen los runs anteriores e instalar para que las nuevas admisiones usen SDK. Verificar también secuencias y trabajos pendientes preparados bajo la versión anterior; definir qué debe volver a prepararse, preservando sus inputs congelados. No reescribir esos inputs en silencio ni construir un backend CLI de ejecución paralela por precaución.

Fuentes actuales: [status](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/application/status.py:94), [history](/home/rojobad/Projects/ai_dev_loop/src/ai_dev_loop/scheduler/application/history.py:193), [script de lectura histórica](/tmp/cursor-sdk-poc-o80OgDfU/historical_read.py).

## Integración propuesta a partir de la evidencia

Estas son recomendaciones de diseño para los planes posteriores, no cambios implementados.

Mantener el SDK dentro del worker de cada intento, para conservar control de duración/procesos y los límites del scheduler. Usar cliente explícito, workspace y store propios del run, en lugar de defaults globales que mezclen sesiones. Crear el agente una sola vez, persistir su ID exacto y continuar esa conversación en implementación/correcciones/recovery.

El store JSONL del SDK es conversación operativa del proveedor: protegerlo, conservarlo según la retención del run y usarlo para resume. engine.sqlite3 y artifacts protegidos siguen siendo autoridad del scheduler. Registrar en ellos identidad, invocación, resultado, métrica y evidencia verificable; no mover la máquina de estados al store del SDK ni tratar un status nativo como aceptación.

Normalizar eventos SDK al contrato propio de captura; no fabricar stream-json CLI para ocultar la diferencia. Extraer el resultado terminal, evitando concatenar mensajes parciales, reemplazos y estados como si fueran texto final. Preservar envelopes/offsets originales como evidencia privada y emitir sólo diagnósticos seguros. Mantener captura incremental limitada y hashes/atomicidad propios.

Adaptar fallos con clases/códigos, estado nativo y prueba autenticada. Conservar el comportamiento aprobado de capacity waits, cursor-retry, retry de review, límites de review, abort y reserva de repositorio. Un mensaje textual aislado no debe autorizar reejecutar.

Después del turno SDK, mantener la normalización de staging y el loop con Codex reviewer. El SDK no altera la autoridad para commit, push, PR o merge; la política actual para standalone y fases finales/intermedias de sequences debe mantenerse.

## Oportunidades nuevas o más accesibles que en la integración CLI actual

| Oportunidad | Evidencia / estado | Utilidad posible |
| --- | --- | --- |
| Tokens estructurados por turno, cache y reasoning | probado | consumo por implementación, corrección, intento, fase y sequence; comparar configuraciones con cobertura explícita |
| Releer resultado y usage sin repetir inferencia | probado entre procesos | recuperar evidencia tras perder conexión y completar observaciones pendientes |
| Replay por offsets | probado con sufijo exacto | seguir desde un cursor durable y evitar duplicar eventos/métricas |
| Handles explícitos agent/run | probado | correlación exacta, cancelación del turno y mejor diagnóstico frente al parseo de stdout |
| Catálogo con parámetros tipados | probado | preflight y congelación del modelo/effort/context/fast sin interpretar listados de consola |
| Herramientas permitidas por invocación | edición sin shell probada | limitar turnos a lo necesario cuando el flujo aprobado lo permita |
| Callbacks Python propios | probado | herramientas que entreguen contexto o consultas concretas del orquestador sin comandos shell auxiliares |
| MCP/configuración seleccionados explícitamente | MCP project probado | reproducir qué contexto de proyecto recibe el implementador |
| Errores tipados y metadata de transporte | reales y simulados, con excepciones descritas | clasificar auth/capacidad/red y registrar retry_after/request_id seguros |
| API sync integrada en Python | probado | adaptador directo en el lenguaje del proyecto, con menos parsing de comandos |

No todas estas capacidades eran imposibles en el producto Cursor CLI: CLI ya ofrece streaming, edición, reglas, skills y MCP. La mejora relevante es la superficie soportada y su integración explícita. Las métricas nativas y la consulta/replay de runs permiten contratos que la integración actual no implementa.

Los callbacks son una oportunidad posterior, no un requisito para sustituir el implementador. Deben respetar permisos/decisiones durables; no exponer una herramienta que cree commits o autorice staging por una petición arbitraria del modelo.

El cliente async y APIs cloud también existen en la superficie publicada, pero no se probaron y no son necesarios para esta adopción local. No ampliar esta fase a provider switching, Cursor master/executor o paralelismo de agentes sin nueva aprobación.

## Pruebas que deben exigir los futuros planes

| Contrato | Cobertura necesaria antes de adopción |
| --- | --- |
| Instalación y preflight | SDK fijado, Python >=3.11 real, bridge sano, key disponible en entorno del servicio, catálogo/modelo/parámetros válidos; sin instalar/tocar timer en tests |
| Identidad | agente exacto persistido antes del turno; prefijo y UUID custom soportados; no reutilización accidental entre runs; reviewer B sin cambios |
| Flujo del scheduler | submit/start/tick, implementación, staging, review, correcciones, aceptación/rechazo, ceiling, secuencias y checkpoints con backend SDK falso |
| Captura | envelopes y texto terminal correctos, parciales/bounds, crash durante escritura, integridad y redacción de key/bridge bearer/MCP secretos |
| Proceso y recuperación | cancel normal, timeout, abort, SIGKILL de worker/runtime, herramientas descendientes, cleanup y reserva; luego get_run/cancel/resume exactos sin reenvío ambiguo |
| Ventanas de incertidumbre | create/send aceptado sin persistencia del ID; resultado terminal recibido sin envelope propio final; retry seguro sin duplicar efectos |
| Fallos | auth, capacidad con/sin retry_after, red/timeout, busy, missing agent/store, schema desconocido; no retry por cualquier InternalServerError |
| Configuración | reglas/skills reales, AGENTS, MCP requerido, setting_sources y herramientas reaplicados en resume; sandbox/permisos definidos |
| Tokens | evento/result/replay deduplicados, retries y correcciones incluidos, None distinto de 0, cobertura parcial, categorías proveedor específicas |
| Codex | uso de eventos del reviewer probado por separado, sin cambiar identidad, bootstrap/resume ni modelo/effort congelados |
| Historia | fixtures genuinos anteriores: status/list/history/inspect y eventos/artifacts con hashes y sin CLI/login/store; sin reescribir fixtures ni ledger |
| Entrega | pipeline/integración del repositorio separado de tests de cada subfase; smoke SDK local mínimo al final en entorno de prueba |

No se ejecutó la suite completa del repositorio: la POC no cambió producción. Tampoco se validaron runs/sequence reales del scheduler con SDK, CLI remoto, facturación, OAuth, hooks, service accounts, Windows nativo, otras versiones/modelos, ni aislamiento adversarial. El informe proporciona evidencia para especificar esas validaciones, no sustituye el gate de integración.

## Aspectos que el chat padre debe resolver al redactar planes

1. Dónde vivirá el store nativo y cómo se relaciona su retención con artifacts protegidos; diseño de identidad/schema versionados.
2. Modelos/parámetros admitidos y mapeo explícito de aliases CLI existentes; política de settings/herramientas/sandbox.
3. Cómo usará el worker el mecanismo existente de supervisión para terminar todo el árbol y cómo autentica/reconcilia el run nativo tras crash.
4. Tratamiento de send/create ambiguos y la ventana anterior a persistir ID, sin retries implícitos.
5. Schema de métricas por intento/proveedor con cobertura, agregación y deduplicación; alcance separado de Codex.
6. Estrategia de lectores históricos y corte para admisiones/manifest preparados anteriores.
7. Pin de versión y pruebas para las particularidades 1.0.37; elegir APIs públicas y no depender del PID privado usado en la POC.

Si se conserva la idea de cinco subfases, estos contratos pueden agruparse como: fundamento SDK/configuración; identidad/worker/evidencia; continuidad/recovery; métricas/consulta histórica; integración/documentación/corte. Es una agrupación de evidencia para considerar al planificar Fase 25 desde la base actual.

## Referencias oficiales consultadas

El SDK Python admite runtime local y credenciales explícitas, requiere Python >=3.10 y documenta las opciones de agente/run. [Cursor SDK Python](https://cursor.com/docs/sdk/python).

Para comparar con el contrato publicado de stdout CLI: [Output format](https://cursor.com/docs/cli/reference/output-format). La POC no demostró que ninguna versión de CLI/transcript pueda contener counters; demostró que el SDK probado sí entrega un contrato utilizable.

## Entrega al chat padre

No se encontró una herramienta habilitada para enviar un mensaje entre este chat lateral y su chat padre. Este archivo y PARENT_CHAT_HANDOFF.md quedan disponibles en el filesystem compartido, pero **no se afirma que el padre los haya recibido**. El usuario puede referenciarlos en el chat padre para incorporarlos al crear los planes.

Los artifacts están en /tmp; deben incorporarse o copiarse a una ubicación durable cuando se redacten los planes, antes de una limpieza/reinicio del entorno.
