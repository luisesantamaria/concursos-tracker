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
3. **`README.md`**: badge de fase (`fase-X%20de%208`), tabla **Status**
   (misma cifra que PLAN §0), y la fecha si aparece.

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
