---
name: actualizar-docs
description: Actualiza los docs vivos (README, ROADMAP, PLAN_MAESTRO §0) tras cerrar un paso, correr una evaluación o cambiar el estado del proyecto. Úsala SIEMPRE antes de terminar una sesión que avanzó el plan, para que el próximo agente no lea estado obsoleto.
---

# Actualizar docs vivos

Los tres documentos de estado son la fuente de verdad de "dónde estamos".
Si avanzaste el proyecto y no los actualizaste, el trabajo NO está terminado:
el próximo agente leerá estado viejo y dará vueltas en círculos.

## Cuándo disparar esta skill (cualquiera de estas)

- Cerraste (o fallaste con aprendizaje) un paso del PLAN_MAESTRO (Fx.Py).
- Corriste una evaluación nueva (golden/holdout/fixture) con números nuevos.
- Cambió el estado de una fase (⬜→🔄→✅) o una decisión pendiente de Luis
  se resolvió.
- Se fusionó un PR que cambia arquitectura, rutas o comandos.

## Qué actualizar (los TRES, siempre consistentes entre sí)

1. **`PLAN_MAESTRO.md` §0 "Estado verificado"** (la fuente primaria):
   - Fecha del estado, números de la última corrida (match/FP/tests verdes),
     rutas de artefactos nuevos en `staging/`.
   - Marcar pasos completados: añadir `✅ (fecha)` al título del paso
     (`### F2.P1 ✅ (13-jul)`). Un paso solo se marca con su PRUEBA cumplida
     y el artefacto que lo demuestra.
   - Actualizar "Decisiones pendientes" si alguna se resolvió.
2. **`ROADMAP.md`**: estados de fase (✅/🔄/⬜) en el timeline y en las
   secciones; los números clave de la fase actual (los MISMOS del PLAN §0).
3. **`README.md`**: la tabla **Status** (misma cifra que PLAN §0) y los
   badges de métricas. Cada badge tiene una fuente de verdad — actualiza SOLO
   con su artefacto:

   | Badge | Patrón shields | Fuente de verdad |
   |---|---|---|
   | Fase | `fase-X%20de%208%20·%20<ámbito>` | primer paso sin ✅ en PLAN_MAESTRO |
   | Golden | `golden%20RS-NN%2F36` | `v2_only_differential` de la ÚLTIMA corrida en staging (color: yellow en curso, brightgreen al gate) |
   | Falsos positivos | `falsos%20positivos-0%20em%20NNN%20auditadas` | suma acumulada de confirmaciones AUDITADAS por humano sin FP (hoy 22; meta n≥300 para afirmar 0-FP público). Si aparece 1 FP: badge en rojo + protocolo STOP |
   | Tests V2 | `tests%20V2-NNN%20verdes` | salida real de `pytest scripts/fase2_municipios/v2 -q` |
   | Estado verificado | `estado%20verificado-YYYY--MM--DD` | fecha de ESTA actualización — se cambia SIEMPRE que la skill corre (es el sello de frescura: si está vieja, el estado está viejo) |

   Los badges de CI y Python no se tocan (automático / estable).

## Reglas duras

- **Solo hechos verificados**: cada número que escribas debe tener artefacto
  (ruta en staging, salida de pytest, PR mergeado). NUNCA proyectes estado
  futuro ni marques ✅ sin la PRUEBA del paso cumplida.
- **Consistencia triple**: el mismo número no puede diferir entre los tres
  docs. Verifica con: `grep -n "22/36\|<número nuevo>" README.md ROADMAP.md PLAN_MAESTRO.md`
  (ajusta el patrón a las cifras que cambiaste).
- **No reescribas historia**: las fases pasadas y sus lecciones no se tocan;
  solo estados, números y fechas.
- **Idiomas**: README en pt-BR; ROADMAP y PLAN_MAESTRO en español.
- Si el cambio fue grande (fase cerrada), registra también el checkpoint en
  la memoria del proyecto (R-T7 del PLAN_MAESTRO).

## Cierre

Commit con los tres archivos juntos, mensaje `docs: estado <Fx.Py> — <resumen
de números>`, en la misma rama/PR del trabajo que los generó (nunca un PR
aparte solo para docs, para que estado y código viajen juntos).
