# Dashboard local

El dashboard es una vista de solo lectura del ledger central y de sus artefactos
protegidos. Se entrega dentro del wheel de `ai_dev_loop`: para usar una
instalación validada no se necesita Node, un checkout del repositorio ni un
servidor de desarrollo.

## Iniciar y detener

```bash
ai_dev_loop dashboard serve --port 8765
```

Abre `http://127.0.0.1:8765/`. El proceso corre en primer plano y solo enlaza
IPv4 `127.0.0.1`; `Ctrl-C` cierra el socket y cancela sus lecturas. El puerto
puede ser cualquier entero libre entre 1 y 65535.

El comando usa el mismo estado XDG que el scheduler instalado. Si se ejecuta con
otro `XDG_STATE_HOME`, mostrará ese otro ledger. Un ledger ausente se representa
como datos vacíos y no se crea. El dashboard no inicia, reintenta, aborta, migra
ni repara runs, tampoco modifica reservas, Git, timers o artefactos.

No publiques este puerto mediante un proxy, túnel o interfaz LAN. El servidor no
tiene login: su límite de seguridad es el loopback local, además de validar
`Host` y `Origin` same-origin.

## Elegir una vista

La barra superior permite elegir proyecto, instante inicial, instante final
exclusivo y zona IANA. Las horas se interpretan en esa zona y el rango máximo es
366 días. Escribe cada instante como `DD/MM/yyyy HH:mm`. Si un cambio DST vuelve
ambigua la hora, añade `Z` o el offset `±HH:mm` correspondiente; horas ambiguas o
inexistentes no se envían como UTC adivinado. No hay botón Apply: elegir proyecto
o preset crea el scope válido de inmediato; las ediciones escritas esperan 400 ms
y solo se publican cuando fecha, zona y rango forman un scope coherente. Un
borrador inválido conserva la última vista válida y explica el error. Los
resultados tardíos del scope anterior se descartan.

La vista reúne:

- KPIs y estados por proyecto para runs cuyo primer lanzamiento de ejecución cae
  en `[from,to)`;
- workload por secuencia o standalone;
- capacidad Codex actual, observada independientemente del rango histórico;
- mapa de runs con carga automática de la cohorte completa, provider Cursor/Codex
  y linaje de secuencia, incluido `implementer_retry`; empieza sin detalle y un
  click o selector accesible muestra exactamente un run sin salto de página;
- timeline paginado con ordinal estable por run y títulos compartidos entre
  gráfico, controles accesibles y modal;
- detalle del intento con contenido disponible bajo carga deliberada;
- actividad diaria por submissions, rounds de review o process hours.

Los indicadores de cobertura distinguen datos completos, parciales y vacíos. Un
run sin lanzamiento no entra en la cohorte. Un intento sin duración resuelta no
recibe una duración inventada.

Cada panel ofrece un botón con una *i* en un círculo para consultar por teclado
sus definiciones y procedencia sin mantener párrafos instructivos abiertos.
Escape/cerrar devuelve el foco. Warnings, progreso, frescura y errores
accionables siguen visibles. La ficha del run y su linaje se consultan desde
ese botón; el timeline muestra solo el nombre seleccionado, el contador y el
gráfico. Sus tarjetas de intentos quedan cerradas por defecto en `Attempt details`.
Los dos gráficos de resumen ocupan cada uno el ancho completo. La actividad
diaria mantiene su equivalente textual dentro de un disclosure bajo demanda.
Los tooltips de los gráficos aparecen sobre los paneles y se confinan al
viewport también en pantallas estrechas. Muestran nombres legibles; los IDs
técnicos permanecen en los detalles de inspección. El mapa de secuencias tiene scroll
interno vertical y horizontal, con un alto máximo de 500 px. Al cambiar la
métrica de actividad, su espacio se reserva durante la carga para evitar
saltos de página.

Los instantes se muestran como `DD/MM/yyyy HH:mm` en la zona elegida, los días
como `DD/MM/yyyy` y las duraciones resueltas como horas/minutos (`<1 m` cuando
son positivas pero menores a un minuto). Los valores UTC y segundos del API no se
reescriben.

## Refresco y contenido sensible

El refresco automático usa como máximo dos lecturas activas. Summary, la
primera página visible del mapa, la actividad y el uso de tokens de la cohorte
usan 30 segundos. El run/timeline seleccionado y su consumo directo de tokens
usan 10 segundos. Capacidad usa 60 segundos. Esas dos lecturas de tokens son
independientes: la de cohorte alimenta los tres gráficos y la del run solo
aparece cuando hay un run seleccionado. Elegir modelo o rol filtra los tres
gráficos en memoria y no cambia el scope, el mapa ni el run. Un productor sin
`dashboardTokenUsage` deja el uso como no disponible; no lo muestra como cero
ni detiene el resto del dashboard. La lectura de tokens no abre el prompt, la
review ni la salida de proceso. El mapa solicita hasta
1000 runs y sigue las páginas automáticamente. Si una página no cabe en el sobre
de 240 KiB, reduce ese lote de forma acotada; las páginas históricas cargadas no
se convierten en polling periódico. Progreso, cobertura incompleta y `Retry`
permanecen visibles y no existe un botón `Load more` en el flujo normal.

`Pause automatic refresh` cancela trabajo periódico y continuaciones propias.
`Resume` hace una lectura fresca por clave visible, sin reproducir intervalos
perdidos. `Refresh current scope` funciona incluso durante Pause, retira la
generación del mapa y reinicia su carga automática sin abrir contenido sensible.
Ocultar la pestaña también retira ese trabajo; al volver visible se leen de nuevo
las claves visibles. El polling de la primera página conserva la historia ya
cargada y reconcilia IDs exactos. Ante un error se conserva solamente el último
éxito compatible en memoria, con sus fechas originales y el error visible.

El prompt inicial, el contenido de review y stdout/stderr se leen solamente al
presionar su acción en el modal. Se autentican por run, intento, componente y
rol; un intento de implementer Codex nunca se acepta como review. El contenido
es paginado, permanece en memoria y se retira al cerrar/cambiar selección o
scope. La evidencia histórica ausente se muestra como `not_recorded`; no se
reconstruye. No existe contrato público para mostrar el prompt exacto de cada
intento de implementer.

La única preferencia durable del navegador es el tema visual. No hay service
worker, telemetría ni cache durable de consultas o contenido.

## Construir desde fuentes

Node.js 24 LTS se usa solo para construir y probar el frontend:

```bash
cd dashboard-web
npm ci
npm run typecheck
npm test -- --run
npm run build
cd ..
uv run python -m build
```

El build escribe assets same-origin y su manifiesto bajo
`src/ai_dev_loop/dashboard/web_dist`. La construcción del paquete falla si el
manifiesto falta, si los inputs cambiaron o si el inventario de assets no
coincide. El wheel no incluye `node_modules`, source maps, ledgers, artefactos ni
capturas de tests.

La aceptación reproducible del checkout usa recursos temporales propios,
providers fake y una instalación aislada del wheel:

```bash
uv run python scripts/verify-phase26-dashboard.py --list
uv run python scripts/verify-phase26-dashboard.py accept --python /ruta/a/python3.11
```

`--python` selecciona explícitamente el runtime `>=3.11` de la instalación
aislada. Para cubrir la matriz soportada se repite `accept` con el Python de
desarrollo más reciente disponible. El harness descubre `node`, `npm` y `npx`
desde `PATH`, o acepta su directorio mediante `PHASE26_NODE_BIN`. Playwright
respeta `PLAYWRIGHT_BROWSERS_PATH` y las librerías existentes en
`LD_LIBRARY_PATH`; no depende de rutas históricas bajo `/tmp`.

`prepare` deja un sitio fixture y un registro privado para un walkthrough;
`cleanup` es una acción separada y solo elimina el recurso si uid, token, PID,
inicio de proceso, comando y grupo siguen coincidiendo. Esos modos son para la
aceptación desde fuentes, no para controlar el scheduler del operador.

Consulta [Referencia del dashboard](../referencia/dashboard.md) para las
definiciones y límites, [Gobernanza de UX](../referencia/dashboard-ux.md) para
los criterios de futuras modificaciones, y [Seguridad y privacidad](seguridad-privacidad.md) para
la frontera de exposición local.
