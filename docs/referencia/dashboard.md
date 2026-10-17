# Referencia del dashboard

## Superficie instalada

`ai_dev_loop dashboard serve --port 8765` sirve el frontend empaquetado y
`POST /api/dashboard/read` en `127.0.0.1`. La API de integración actual es 1.11 y
publica `capabilities.dashboard=true` y `capabilities.shortNames=true`. SQLite
schema 17 almacena los nombres cortos; schema 16 sigue siendo legible mediante
fallback sin migración ni escritura. El lector abre un snapshot validado de
`engine.sqlite3` en modo solo lectura y usa los artefactos protegidos como autoridad.

Las operaciones HTTP allowlisted son:

| Operación | Resultado tipado |
| --- | --- |
| `dashboard.projects` | catálogo paginado de proyectos |
| `dashboard.summary` | KPIs, estados completos por proyecto y workload |
| `dashboard.runs` | mapa paginado de runs y linaje |
| `dashboard.activity` | valores diarios para una métrica |
| `run.inspect`, `run.timeline` | metadata y timeline del run seleccionado |
| `sequence.inspect`, `sequence.phase-runs` | contexto autoritativo de secuencia |
| `machine.codex-capacity` | capacidad actual, independiente del filtro histórico |
| `run.initial-prompt` | prompt congelado paginado |
| `run.review`, `run.review-content` | metadata y contenido reviewer autenticado |
| `run.output` | stdout/stderr autenticado por intento y componente |

No existe un dispatcher para SQL, argv, rutas arbitrarias o comandos del
scheduler. Los DTO públicos Python viven en `integration_api`; el cliente tiene
tipos y parsers explícitos y solo accede mediante `DashboardDataSource`. Los
componentes y opciones ECharts no forman parte del contrato de transporte.

## Filtros, cohorte y linaje

`fromUtc` es inclusivo, `toUtc` exclusivo, `timeZone` es un nombre IANA y
`projectName` es exacto o `null`. El servicio devuelve los filtros canónicos.
El rango debe ser positivo y no puede superar 366 días.

El frontend edita los límites como `DD/MM/yyyy HH:mm` en la zona IANA elegida y
solo transporta instantes UTC después de validarlos. Una hora local inexistente
se rechaza; una hora ambigua por cambio DST exige añadir `Z` o un offset
`±HH:mm` que coincida con esa zona. El límite final conserva semántica exclusiva.
Proyecto y presets válidos cambian el scope de inmediato. Los campos escritos de
fecha/zona esperan 400 ms y solo publican un scope completo y válido; un borrador
inválido conserva la última observación válida y muestra su error. `Refresh
current scope` vuelve a observar ese scope sin aplicar borradores parciales.

`summary` y `runs` incluyen cada run cuyo primer lanzamiento de ejecución cae
en el rango. El primer lanzamiento es la primera petición de ejecución owned;
crear chats, preflight y probes no cuenta. Proyecto, provider y linaje salen de
campos autoritativos. Provider histórico ausente significa Cursor.
`implementer_retry` es una generación de reemplazo enlazada por los campos de
secuencia; nunca se infiere del ID del run.

El mapa prefiere `shortName`/`sequenceShortName` en ejes y etiquetas. Conserva
el nombre original y los IDs completos en detalles/tooltips, y selecciona por
`runId`/`sequenceId`; aliases duplicados no colapsan runs. Las etiquetas se
tratan como texto no confiable y se escapan antes de cualquier tooltip HTML.
Sin selección no aparece metadata de run. Un click de celda o el selector
compacto accesible muestra exactamente un run y reemplaza la selección anterior,
sin enfocar una tarjeta distante ni desplazar la página.

El timeline numera todos los intentos por el orden cronológico autoritativo y el
offset de página: `N - Iteration I - Implementer|Reviewer|Unclassified`. El mismo
título aparece en el eje, control accesible y modal. Provider/componente/efecto
siguen visibles como metadata secundaria y el `attemptId` no determina el rol.
Los IDs exactos permanecen como claves de selección y aparecen al final de la
metadata humana.

Los instantes visibles usan la zona IANA seleccionada y formato
`DD/MM/yyyy HH:mm`; los buckets diarios conservan su día ya zonificado y se
muestran como `DD/MM/yyyy`. Duraciones resueltas se truncan a minutos:
`00 h 00 m`, `<1 m` o `02 h 35 m`; las desconocidas permanecen no disponibles y
las horas pueden superar 99. Esta presentación no altera timestamps almacenados,
offsets, segundos de métricas ni cálculos del productor.

Cada respuesta incluye `observedAt`. Las consultas de scope incluyen
`coverage.dataState`, conteos de lanzamientos/duraciones ausentes, clases de
ejecución desconocidas y warnings. Los totales de summary son completos para la
cohorte y no se derivan de una página de `runs`.

## Métricas

- `submissions`: un evento en la fecha de `submittedAt` de cada run.
- `review-rounds`: a lo sumo uno por `(runId, iteration)`, únicamente para
  `codex.bootstrap_review` o `codex.resume_review` con componente reviewer.
- `process-hours`: segundos observados de procesos resueltos, divididos en las
  medianoches de la zona y convertidos a horas una sola vez en presentación.
  Suma procesos paralelos.

`cursor.run_turn`, `codex.implementer_bootstrap` y
`codex.implementer_resume` son ejecución: contribuyen tiempo y timeline, pero
nunca rounds de review ni llamadas de contenido reviewer. Un intento activo o
sin evidencia suficiente no recibe una duración fabricada. Las métricas de
actividad se fechan por evento y no quedan limitadas a la cohorte de primer
lanzamiento usada por summary/runs.

## Paginación y límites

Las páginas usan `offset` y `limit`; `dashboard.runs` admite de 1 a 1000 y las
demás colecciones paginadas conservan su máximo de 100. `nextOffset` solo existe
cuando `hasMore=true` y avanza por el `limit` solicitado de esa página. El mapa
solicita 1000 inicialmente y sigue automáticamente hasta agotar la cohorte. Si
el sobre de una página supera 240 KiB, el productor devuelve
`PAGE_ENVELOPE_TOO_LARGE` y el cliente reintenta el mismo offset con la mitad del
lote, hasta 1; otros errores no activan esa reducción. Progreso, cobertura
incompleta y reintento permanecen visibles, sin botón `Load more` ni totales
derivados de una carga parcial.

El servicio limita cada observación a 10.000 runs
relevantes, 100.000 intentos contribuyentes, 5 segundos de consulta y un sobre
de 240 KiB. El request HTTP está limitado a 16 KiB y cada lectura de contenido a
65.536 bytes. Superar un límite falla sin publicar totales parciales.

El servidor admite cuatro handlers, dos trabajos de servicio y una sola sonda
de capacidad. La saturación responde `503`; no crea una cola ilimitada. El
cliente mantiene como máximo dos lecturas activas y usa cancelación más fences
de generación para que una respuesta vieja no restaure un scope retirado.

El polling actualiza la primera página sin eliminar páginas históricas ya
cargadas ni duplicar IDs. Cada página conserva su observación fechada: el
conjunto agregado no se presenta como snapshot atómico. Un Refresh explícito o
un cambio de scope retira la generación completa y reinicia la carga automática.

## Información y presentación accesible

Cada panel o gráfico conserva encabezado, datos, controles, frescura, progreso y
warnings accionables a la vista. Su botón `Information` abre en un diálogo la
explicación, definiciones y procedencia detallada; expone `aria-expanded`, admite
teclado/Escape y devuelve el foco al control que lo abrió. La actividad diaria
mantiene el calendario y ofrece la lista equivalente bajo un disclosure
accesible, sin una pared de días permanente.

Los tooltips ECharts se montan a nivel de `body`, se confinan al viewport y usan
una capa inferior a los modales. Los nombres no confiables se siguen escapando y
el overlay se elimina al desmontar el gráfico.

## Contenido y compatibilidad

La metadata puede decir si prompt, respuesta, review Markdown, fix prompt,
stdout o stderr están disponibles. Leer bytes requiere una acción deliberada y
usa `byteOffset`/`limit`, Base64, hashes y metadata de continuación. Un output
activo puede exponer el EOF observado para consultar bytes nuevos; no se marca
completo antes de que la evidencia lo pruebe.

`run.review-content` exige componente `codex` y uno de los efectos reviewer
allowlisted. `codex_implementer` se rechaza en esa ruta; su salida disponible se
lee por `run.output`. Evidencia histórica ausente devuelve indisponibilidad
tipada (`not_recorded`) y no contenido reconstruido. No hay una lectura pública
del prompt exacto por intento de implementer.

Los consumidores deben aceptar campos aditivos dentro de API major 1. La
ausencia de `dashboard` en capabilities significa que el productor instalado no
ofrece esta superficie. Consulta [API de integración
local](integration-api.md) para el sobre común y [Dashboard local](../operacion/dashboard-local.md)
para operación y refresco.
