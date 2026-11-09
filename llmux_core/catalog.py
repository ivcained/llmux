"""Canonical model identity and cross-marketplace catalog union."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import ModelRecord

MODEL_ALIASES = {
    "kimi-k3-1-pro": "kimi-k3.1-pro",
    # Solid writes version dots as dashes; Surplus uses dots for the same model.
    "claude-opus-5-5": "claude-opus-5.5",
    "claude-sonnet-5-5": "claude-sonnet-5.5",
}


def canonical_model_id(raw: str) -> str:
    """Normalize a marketplace model id into the shared catalog key."""

    cleaned = raw.strip().lower()
    return MODEL_ALIASES.get(cleaned, cleaned)


@dataclass(frozen=True)
class Catalog:
    records: tuple[ModelRecord, ...]

    @classmethod
    def merge(cls, groups: list[list[ModelRecord]]) -> "Catalog":
        merged: dict[str, ModelRecord] = {}
        for records in groups:
            for record in records:
                existing = merged.get(record.id)
                if existing is None:
                    merged[record.id] = record
                    continue
                aliases = tuple(dict.fromkeys([*existing.aliases, *record.aliases]))
                metadata = {**record.metadata, **existing.metadata}
                merged[record.id] = replace(
                    existing,
                    name=existing.name or record.name,
                    offers=(*existing.offers, *record.offers),
                    aliases=aliases,
                    metadata=metadata,
                )
        return cls(
            records=tuple(
                record
                for record in merged.values()
                if any(offer.healthy for offer in record.offers)
            )
        )

    def get(self, model_id: str) -> ModelRecord | None:
        canonical = canonical_model_id(model_id)
        for record in self.records:
            if record.id == canonical or model_id in record.aliases or canonical in record.aliases:
                return record
        return None

    def to_openai(self) -> dict:
        data = []
        for record in self.records:
            sources = sorted({offer.source for offer in record.offers})
            data.append(
                {
                    "id": record.id,
                    "object": "model",
                    "created": 0,
                    "owned_by": "llmux",
                    "name": record.name,
                    "sources": sources,
                    "context_length": record.metadata.get("context_length"),
                }
            )
        return {"object": "list", "data": data}
