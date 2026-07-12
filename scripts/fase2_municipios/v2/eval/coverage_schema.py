"""Canonical schema for golden units intentionally not executed in live V2."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from scripts.eval import medir_golden_set as golden_evaluator


SIN_COBERTURA_V1_MOTIVO = "sin_cobertura_v1"
VALID_BUCKETS = frozenset({"concurso_publico", "processo_seletivo"})
EXCLUSION_FIELDS = frozenset({"municipio", "bucket", "executed", "motivo"})


class CoverageSchemaError(ValueError):
    """Coverage metadata is ambiguous or violates the canonical contract."""


@dataclass(frozen=True)
class SinCoberturaV1Unit:
    municipio: str
    bucket: str
    executed: bool = False
    motivo: str = SIN_COBERTURA_V1_MOTIVO

    def __post_init__(self) -> None:
        if not isinstance(self.municipio, str) or not self.municipio:
            raise CoverageSchemaError("sin_cobertura_v1_municipio_invalid")
        if self.bucket not in VALID_BUCKETS:
            raise CoverageSchemaError("sin_cobertura_v1_bucket_invalid")
        if self.executed is not False:
            raise CoverageSchemaError("sin_cobertura_v1_must_not_be_executed")
        if self.motivo != SIN_COBERTURA_V1_MOTIVO:
            raise CoverageSchemaError("sin_cobertura_v1_motivo_invalid")

    @property
    def key(self) -> tuple[str, str]:
        return (golden_evaluator.muni_key(self.municipio), self.bucket)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "municipio": self.municipio,
            "bucket": self.bucket,
            "executed": False,
            "motivo": SIN_COBERTURA_V1_MOTIVO,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SinCoberturaV1Unit":
        if set(raw) != EXCLUSION_FIELDS:
            raise CoverageSchemaError("sin_cobertura_v1_fields_invalid")
        return cls(
            municipio=raw.get("municipio"),
            bucket=raw.get("bucket"),
            executed=raw.get("executed"),
            motivo=raw.get("motivo"),
        )


def canonical_sin_cobertura_v1(
    units: Iterable[SinCoberturaV1Unit | Mapping[str, Any]],
) -> tuple[SinCoberturaV1Unit, ...]:
    unique: dict[tuple[str, str], SinCoberturaV1Unit] = {}
    municipality_names: dict[str, str] = {}
    for raw in units:
        unit = raw if isinstance(raw, SinCoberturaV1Unit) else SinCoberturaV1Unit.from_mapping(raw)
        muni_key = golden_evaluator.muni_key(unit.municipio)
        previous_name = municipality_names.get(muni_key)
        if previous_name is not None and previous_name != unit.municipio:
            raise CoverageSchemaError(f"muni_key_collision:{muni_key}")
        municipality_names[muni_key] = unit.municipio
        if unit.key in unique:
            raise CoverageSchemaError(
                f"duplicate_sin_cobertura_v1:{unit.municipio}/{unit.bucket}"
            )
        unique[unit.key] = unit
    return tuple(unique[key] for key in sorted(unique))


def coverage_summary(
    *, total: int, covered: int, sin_cobertura_v1: int
) -> dict[str, int]:
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (
        total, covered, sin_cobertura_v1
    )):
        raise CoverageSchemaError("coverage_counts_invalid")
    if covered + sin_cobertura_v1 != total:
        raise CoverageSchemaError("coverage_counts_inconsistent")
    return {
        "total": total,
        "covered": covered,
        "sin_cobertura_v1": sin_cobertura_v1,
    }


__all__ = [
    "CoverageSchemaError",
    "SIN_COBERTURA_V1_MOTIVO",
    "SinCoberturaV1Unit",
    "canonical_sin_cobertura_v1",
    "coverage_summary",
]
