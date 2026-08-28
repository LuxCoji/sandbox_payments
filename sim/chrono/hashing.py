import hashlib
import json
from typing import Any


def canonicalize(obj: Any) -> bytes:
    """
    Produce a canonical JSON byte string for the given object.
    Keys are lexicographically sorted, no spaces are used.
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')


def hash_object(obj: Any) -> str:
    """Compute a SHA-256 hash over the canonical JSON representation of an object."""
    canonical_bytes = canonicalize(obj)
    return hashlib.sha256(canonical_bytes).hexdigest()


class IncrementalHasher:
    """
    Computes a deterministic SHA-256 state hash using a Merkle-tree approach.

    Maintains hashes of individual entities so that the global state hash
    can be recomputed efficiently without re-serializing the entire world state
    every time a single account changes.
    """

    def __init__(self) -> None:
        # Maps entity_type -> { entity_id -> entity_hash_string }
        self.entity_hashes: dict[str, dict[str, str]] = {}

    def update_entity(self, entity_type: str, entity_id: str, entity_data: Any) -> None:
        """Update or delete the hash for a single entity."""
        if entity_type not in self.entity_hashes:
            self.entity_hashes[entity_type] = {}

        if entity_data is None:
            # Handle deletion
            self.entity_hashes[entity_type].pop(entity_id, None)
        else:
            self.entity_hashes[entity_type][entity_id] = hash_object(entity_data)

    def get_state_hash(self) -> str:
        """
        Compute the root hash of the entire state.

        1. Computes an aggregate hash for each entity type (e.g., 'accounts', 'devices').
        2. Computes the final root hash over all aggregate hashes.
        """
        aggregate_hashes = {}
        for entity_type, entities in self.entity_hashes.items():
            # The dictionary `entities` maps ID -> Hash.
            # Hashing this dictionary ensures we detect any addition/removal or ID change.
            aggregate_hashes[entity_type] = hash_object(entities)

        # Hash the dictionary of { entity_type: aggregate_hash } to get the root state hash.
        return hash_object(aggregate_hashes)
