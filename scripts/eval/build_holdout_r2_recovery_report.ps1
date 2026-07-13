$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$recovery = Join-Path $repo 'staging\fase2_v2\eval\holdout50_20260712\recovery_holdout_r2'
$generatedAt = (Get-Date).ToUniversalTime().ToString('o')
$utf8 = [System.Text.UTF8Encoding]::new($false)

function Read-Json([string]$path) {
    Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json
}

function Write-Json([string]$path, $value) {
    $json = $value | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($path, $json + "`n", $utf8)
}

$audit = @()
foreach ($file in Get-ChildItem -LiteralPath $recovery -Filter 'audit_lote?.json' | Sort-Object Name) {
    $doc = Read-Json $file.FullName
    foreach ($unit in $doc.unidades) {
        $audit += [pscustomobject][ordered]@{
            lote = [int]$doc.lote
            unidad = [string]$unit.unidad
            veredicto = [string]$unit.veredicto
            evidencia = [string]$unit.evidencia
            fuentes = @($unit.fuentes)
            fuentes_texto = (@($unit.fuentes) -join ' | ')
        }
    }
}

$diagnostics = @()
foreach ($file in Get-ChildItem -LiteralPath $recovery -Filter 'diag_mod?.json' | Sort-Object Name) {
    $doc = Read-Json $file.FullName
    foreach ($unit in $doc.unidades) {
        $diagnostics += [pscustomobject][ordered]@{
            mod = [int]$doc.mod
            unidad = [string]$unit.unidad
            palanca = [string]$unit.palanca
            evidencia = [string]$unit.evidencia
            fuentes = @($unit.fuentes)
            fuentes_texto = (@($unit.fuentes) -join ' | ')
        }
    }
}

$auditDistribution = @(
    $audit | Group-Object veredicto | Sort-Object Count -Descending | ForEach-Object {
        [pscustomobject][ordered]@{
            veredicto = $_.Name
            cantidad = $_.Count
            proporcion = [math]::Round($_.Count / 56, 4)
        }
    }
)
$diagnosticDistribution = @(
    $diagnostics | Group-Object palanca | Sort-Object Count -Descending | ForEach-Object {
        [pscustomobject][ordered]@{
            palanca = $_.Name
            cantidad = $_.Count
            proporcion = [math]::Round($_.Count / 32, 4)
        }
    }
)

$auditExceptions = @($audit | Where-Object { $_.veredicto -ne 'RATIFICAR' })
$fpSuspected = @($audit | Where-Object { $_.veredicto -eq 'FP_SOSPECHADO' })
$doubt = @($audit | Where-Object { $_.veredicto -eq 'DUDA' })
$ratified = @($audit | Where-Object { $_.veredicto -eq 'RATIFICAR' })

$coverage = [ordered]@{
    auditoria_esperadas = 56
    auditoria_completadas = $audit.Count
    auditoria_unicas = @($audit.unidad | Sort-Object -Unique).Count
    diagnostico_esperadas = 32
    diagnostico_completadas = $diagnostics.Count
    diagnostico_unicas = @($diagnostics.unidad | Sort-Object -Unique).Count
    auditoria_sin_fuentes = @($audit | Where-Object { @($_.fuentes).Count -eq 0 }).Count
    diagnostico_sin_fuentes = @($diagnostics | Where-Object { @($_.fuentes).Count -eq 0 }).Count
}

$consolidated = [ordered]@{
    generated_at = $generatedAt
    scope = 'holdout r2 cierre: 56 confirmadas auditadas y 32 no-confirmadas diagnosticadas'
    source_run = 'staging/fase2_v2/eval/holdout50_20260712/run_r2_postpalancas'
    coverage = $coverage
    audit_distribution = $auditDistribution
    diagnostic_distribution = $diagnosticDistribution
    audit = $audit
    diagnostics = $diagnostics
}
Write-Json (Join-Path $recovery 'consolidated.json') $consolidated

# Portable artifact tables require scalar cells; preserve source arrays only in consolidated.json.
$auditWidget = @($audit | Select-Object lote, unidad, veredicto, evidencia, fuentes_texto)
$auditExceptionsWidget = @($auditExceptions | Select-Object lote, unidad, veredicto, evidencia, fuentes_texto)
$diagnosticsWidget = @($diagnostics | Select-Object mod, unidad, palanca, evidencia, fuentes_texto)

$headline = @([ordered]@{
    auditadas = 56
    ratificadas = $ratified.Count
    fp_sospechados = $fpSuspected.Count
    dudas = $doubt.Count
    diagnosticadas = 32
})

$sourceId = 'holdout_r2_recovery'
$sourcePath = 'staging/fase2_v2/eval/holdout50_20260712/recovery_holdout_r2/consolidated.json'
$source = [ordered]@{
    id = $sourceId
    label = 'Consolidación de auditoría y diagnóstico del holdout r2'
    path = $sourcePath
    query = [ordered]@{
        engine = 'duckdb'
        language = 'sql'
        sql = "SELECT * FROM read_json_auto('$sourcePath')"
        description = 'Consolida siete lotes de auditoría y cuatro módulos de diagnóstico, preservando evidencia y URLs oficiales.'
        executed_at = $generatedAt
        filters = @('Auditoría: las 56 filas con final en indice_oficial o indice_oficial_combinado', 'Diagnóstico: las 32 filas restantes')
        metric_definitions = @('RATIFICAR: índice oficial con evidencia positiva de ítems reales', 'DUDA: URL defendible con evidencia insuficiente o calidad degradada', 'FP_SOSPECHADO: confirmación incompatible con la evidencia congelada o viva')
        tables_used = @('run_r2_postpalancas/progress.csv', 'run_r2_postpalancas/observability/*.json', 'recovery_holdout_r2/audit_lote0..6.json', 'recovery_holdout_r2/diag_mod0..3.json')
    }
}

$artifact = [ordered]@{
    surface = 'report'
    manifest = [ordered]@{
        version = 1
        surface = 'report'
        title = 'Cierre recuperado del holdout r2'
        description = 'Auditoría anti-falsos-positivos y diagnóstico de las unidades no confirmadas.'
        generatedAt = $generatedAt
        cards = @(
            [ordered]@{ id='audited'; description='Confirmaciones r2 revisadas una por una.'; dataset='headline'; sourceId=$sourceId; metrics=@([ordered]@{label='Auditadas';field='auditadas';format='number'}) },
            [ordered]@{ id='ratified'; description='Confirmaciones con índice y evidencia positiva de ítems.'; dataset='headline'; sourceId=$sourceId; metrics=@([ordered]@{label='Ratificadas';field='ratificadas';format='number'}) },
            [ordered]@{ id='fp'; description='Confirmaciones que no deben entrar en una métrica de cero FP.'; dataset='headline'; sourceId=$sourceId; metrics=@([ordered]@{label='FP sospechados';field='fp_sospechados';format='number'}) },
            [ordered]@{ id='diagnosed'; description='Unidades no confirmadas clasificadas por palanca.'; dataset='headline'; sourceId=$sourceId; metrics=@([ordered]@{label='Diagnosticadas';field='diagnosticadas';format='number'}) }
        )
        charts = @(
            [ordered]@{
                id = 'audit_verdict_chart'
                title = 'Solo 42 de 56 confirmaciones quedaron ratificadas'
                subtitle = 'Veredictos de la auditoría recuperada del holdout r2'
                type = 'bar'
                dataset = 'audit_distribution'
                sourceId = $sourceId
                encodings = [ordered]@{
                    x = [ordered]@{field='veredicto';type='nominal';label='Veredicto'}
                    y = [ordered]@{field='cantidad';type='quantitative';format='number';label='Unidades'}
                }
                xAxisTitle = 'Veredicto'
                yAxisTitle = 'Unidades'
                valueFormat = 'number'
                layout = 'full'
                maxRows = 3
            }
        )
        tables = @(
            [ordered]@{ id='audit_distribution'; title='Distribución de veredictos'; subtitle='56 confirmaciones del holdout r2, revisión item-positiva y portal oficial vivo'; dataset='audit_distribution'; sourceId=$sourceId; density='spacious'; defaultSort=[ordered]@{field='cantidad';direction='desc'}; columns=@([ordered]@{field='veredicto';label='Veredicto';type='text'},[ordered]@{field='cantidad';label='Unidades';format='number'},[ordered]@{field='proporcion';label='Proporción';format='percent'}) },
            [ordered]@{ id='audit_exceptions'; title='Confirmaciones que requieren acción'; subtitle='Siete FP sospechados y siete dudas antes de cualquier afirmación de cero falsos positivos'; dataset='audit_exceptions'; sourceId=$sourceId; density='dense'; defaultSort=[ordered]@{field='veredicto';direction='asc'}; columns=@([ordered]@{field='unidad';label='Unidad';type='text'},[ordered]@{field='veredicto';label='Veredicto';type='text'},[ordered]@{field='evidencia';label='Evidencia';type='text'},[ordered]@{field='fuentes_texto';label='Fuentes oficiales';type='text'}) },
            [ordered]@{ id='audit_all'; title='Auditoría completa de confirmaciones'; subtitle='Las 56 unidades revisadas; tabla de detalle para trazabilidad y control de regresión'; dataset='audit_all'; sourceId=$sourceId; density='dense'; defaultSort=[ordered]@{field='unidad';direction='asc'}; columns=@([ordered]@{field='unidad';label='Unidad';type='text'},[ordered]@{field='veredicto';label='Veredicto';type='text'},[ordered]@{field='evidencia';label='Evidencia';type='text'},[ordered]@{field='fuentes_texto';label='Fuentes oficiales';type='text'}) },
            [ordered]@{ id='diagnostic_distribution'; title='Palancas de las no confirmadas'; subtitle='32 unidades no confirmadas, clasificación mutuamente exclusiva por causa principal'; dataset='diagnostic_distribution'; sourceId=$sourceId; density='spacious'; defaultSort=[ordered]@{field='cantidad';direction='desc'}; columns=@([ordered]@{field='palanca';label='Palanca';type='text'},[ordered]@{field='cantidad';label='Unidades';format='number'},[ordered]@{field='proporcion';label='Proporción';format='percent'}) },
            [ordered]@{ id='diagnostics'; title='Diagnóstico completo de las no confirmadas'; subtitle='32 unidades con causa, evidencia y fuente oficial para priorizar la r3'; dataset='diagnostics'; sourceId=$sourceId; density='dense'; defaultSort=[ordered]@{field='palanca';direction='asc'}; columns=@([ordered]@{field='unidad';label='Unidad';type='text'},[ordered]@{field='palanca';label='Palanca';type='text'},[ordered]@{field='evidencia';label='Evidencia';type='text'},[ordered]@{field='fuentes_texto';label='Fuentes oficiales';type='text'}) }
        )
        sources = @($source)
        blocks = @(
            [ordered]@{id='title';type='markdown';body='# Cierre recuperado del holdout r2'},
            [ordered]@{id='technical_summary';type='markdown';sourceId=$sourceId;body="## Resultado técnico: la r3 aún no está lista para afirmar 0 FP`n`nLa recuperación cubrió **56/56 confirmaciones** y **32/32 no-confirmadas**. De las confirmaciones, **42 quedaron ratificadas**, **7 en duda** y **7 como FP sospechados**. La corrida r3 debe aplicar el gate item-positivo y corregir o excluir esos siete FP antes de usar la tasa de confirmación como criterio de cierre."},
            [ordered]@{id='metrics';type='metric-strip';cardIds=@('audited','ratified','fp','diagnosed')},
            [ordered]@{id='audit_finding';type='markdown';sourceId=$sourceId;body="## Una de cada cuatro confirmaciones necesita revisión o corrección`n`nEl 75% de las 56 confirmaciones fue ratificado. El 12,5% quedó en duda y otro 12,5% presenta evidencia suficiente para sospechar un falso positivo. El riesgo no es cosmético: siete casos contradicen el objetivo de cero FP."},
            [ordered]@{id='audit_verdict_visual';type='chart';chartId='audit_verdict_chart';layout='full'},
            [ordered]@{id='audit_distribution_block';type='table';tableId='audit_distribution'},
            [ordered]@{id='exceptions_intro';type='markdown';body="## Los catorce casos no ratificados concentran el riesgo de cierre`n`nLos FP sospechados deben salir del numerador o corregirse antes de la r3. Las dudas requieren re-citación, render o una URL más completa; no deben contarse como evidencia de cero FP hasta resolverse."},
            [ordered]@{id='exceptions_table';type='table';tableId='audit_exceptions'},
            [ordered]@{id='scope';type='markdown';body="## Alcance y definiciones`n`n**Grano:** municipio/bucket. **Población auditada:** 56 filas de r2 con resultado `indice_oficial` o `indice_oficial_combinado`. **Población diagnosticada:** las 32 filas restantes. **RATIFICAR** exige URL oficial correcta y evidencia positiva de ítems reales; **DUDA** indica una ruta defendible con evidencia incompleta; **FP_SOSPECHADO** indica incompatibilidad material entre la confirmación y la evidencia congelada o viva."},
            [ordered]@{id='method';type='markdown';body="## Método: evidencia congelada más portal oficial vivo`n`nCada auditor contrastó citas y `evidence_snapshot` del observability JSON con el portal oficial vivo, buscando edital, número, año o fecha y descartando noticias, licitaciones, homónimos, índices vacíos y páginas anuales obsoletas. Los diagnósticos combinaron stages A/B/C, errores de validación, indicadores de render/transporte y una verificación oficial acotada."},
            [ordered]@{id='diagnostic_finding';type='markdown';sourceId=$sourceId;body="## Autoridad residual y dificultad legítima explican dos tercios del rezago`n`nLa causa principal en 11/32 casos fue `autoridad_residual`; otros 10/32 son `legitimamente_dificil`. Render interactivo explica 5/32. Citas, transporte, varianza y `nao_encontrado` correcto completan los seis casos restantes."},
            [ordered]@{id='diagnostic_distribution_block';type='table';tableId='diagnostic_distribution'},
            [ordered]@{id='diagnostic_detail_intro';type='markdown';body="## La matriz de diagnóstico permite reintentos dirigidos`n`nLa r3 debe priorizar autoridad residual, render interactivo y reparación de citas. Los casos legítimamente difíciles y los `nao_encontrado` correctos no deberían forzarse a confirmar."},
            [ordered]@{id='diagnostic_detail_table';type='table';tableId='diagnostics'},
            [ordered]@{id='robustness';type='markdown';body="## Limitaciones y robustez`n`nLas páginas oficiales pueden cambiar después del snapshot y algunos portales cargan ítems mediante XHR o JavaScript. Por eso una DUDA no equivale a FP. Los siete FP sospechados sí tienen una contradicción concreta, pero deben convertirse en fixtures de regresión y confirmarse nuevamente durante la r3. La evidencia web se verificó con solicitudes acotadas; no se ejecutaron APIs de modelos."},
            [ordered]@{id='full_audit_intro';type='markdown';body="## Trazabilidad completa de las 56 confirmaciones`n`nLa tabla preserva el veredicto, la justificación y las fuentes oficiales por unidad. Debe usarse como lista de control al comparar la r3 con r2."},
            [ordered]@{id='full_audit_table';type='table';tableId='audit_all'},
            [ordered]@{id='next_steps';type='markdown';body="## Próximos pasos recomendados`n`n1. Convertir los 7 FP sospechados y las 7 dudas en fixtures de regresión.`n2. Ejecutar la r3 con gate item-positivo, reparación de citas y rutas de render habilitadas.`n3. Reauditar toda confirmación nueva y verificar que ninguno de los 7 FP reaparezca.`n4. Medir ≥80% solo después de restar FP y mantener el denominador explícito de 88 unidades."},
            [ordered]@{id='questions';type='markdown';body="## Preguntas que debe responder la r3`n`n- ¿Los siete FP sospechados quedan bloqueados de forma determinista?`n- ¿Las siete dudas se recuperan mediante re-citación/render sin abrir nuevos FP?`n- ¿La mejora en autoridad residual ocurre sin degradar identidad o municipio correcto?`n- ¿La tasa neta de confirmación alcanza 80% con cero FP revalidados?"}
        )
    }
    snapshot = [ordered]@{
        version = 1
        generatedAt = $generatedAt
        status = 'ready'
        datasets = [ordered]@{
            headline = $headline
            audit_distribution = $auditDistribution
            audit_exceptions = $auditExceptionsWidget
            audit_all = $auditWidget
            diagnostic_distribution = $diagnosticDistribution
            diagnostics = $diagnosticsWidget
        }
        accessIssues = @()
    }
    sources = @($source)
}
Write-Json (Join-Path $recovery 'artifact.json') $artifact

$validation = @"
# Validación del cierre recuperado del holdout r2

## Evaluación general: necesita revisión antes de r3

- Cobertura de auditoría: $($coverage.auditoria_completadas)/$($coverage.auditoria_esperadas), unidades únicas: $($coverage.auditoria_unicas).
- Cobertura de diagnóstico: $($coverage.diagnostico_completadas)/$($coverage.diagnostico_esperadas), unidades únicas: $($coverage.diagnostico_unicas).
- Registros sin fuente: auditoría=$($coverage.auditoria_sin_fuentes), diagnóstico=$($coverage.diagnostico_sin_fuentes).
- Suma de veredictos: $($ratified.Count) + $($doubt.Count) + $($fpSuspected.Count) = $($audit.Count).
- Suma de palancas: $(($diagnosticDistribution | ForEach-Object { $_.cantidad } | Measure-Object -Sum).Sum) = $($diagnostics.Count).

## Bloqueadores

1. Siete confirmaciones r2 son FP sospechados; no se puede afirmar cero FP.
2. Siete confirmaciones permanecen en duda y requieren re-citación, render o URL más completa.
3. La r3 todavía no existe en el directorio de holdout y debe ejecutarse después de aplicar estas correcciones.

## Confianza

Alta para cobertura, conteos y trazabilidad; media-alta para los veredictos web, sujetos a cambios posteriores de los portales oficiales.
"@
[System.IO.File]::WriteAllText((Join-Path $recovery 'validation.md'), $validation.Trim() + "`n", $utf8)

$handoff = @"
# Traspaso para Claude: holdout r2 recuperado

No repetir los 7 auditores ni los 4 diagnosticadores: la recuperación ya terminó y quedó consolidada en este directorio.

- Auditoría: 56/56 unidades, 42 RATIFICAR, 7 DUDA y 7 FP_SOSPECHADO.
- Diagnóstico: 32/32 unidades clasificadas, sin faltantes ni registros sin fuente.
- Fuente consolidada: `consolidated.json`.
- Informe técnico: `report.html` (se genera a partir de `artifact.json`).
- Control de calidad: `validation.md`.
- Próximo paso: preparar la r3 con el gate item-positivo y fixtures para los 14 casos no ratificados; no relanzar la auditoría r2.

Los JSON `audit_lote0.json` a `audit_lote6.json` y `diag_mod0.json` a `diag_mod3.json` conservan el detalle original por agente.
"@
[System.IO.File]::WriteAllText((Join-Path $recovery 'HANDOFF_CLAUDE.md'), $handoff.Trim() + "`n", $utf8)

Write-Output ([ordered]@{
    audit = $audit.Count
    ratified = $ratified.Count
    doubts = $doubt.Count
    fp_suspected = $fpSuspected.Count
    diagnostics = $diagnostics.Count
    output = $recovery
} | ConvertTo-Json -Compress)
